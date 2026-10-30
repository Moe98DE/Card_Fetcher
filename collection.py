# collection.py
"""
Card collections: lists of cards a user owns, imported from a text file or
pasted text and stored by name next to the card database.

Accepts everything the decklist parser reads ("4 Sol Ring", "Sol Ring (C21) 263 *F*",
Arena / MTGO / Moxfield / Archidekt text exports) plus CSV collection exports
with a header row (Moxfield, Deckbox, ManaBox, Dragon Shield, ...).

Only stores names and quantities; card data always comes from the card
database, so searches in a collection see the same tags, prices and text.
"""
import csv
import io
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional, Sequence, Tuple

from database import DB_FILE
from parser import ParseResult, parse_decklist_detailed

COLLECTIONS_FILE = os.path.join(os.path.dirname(DB_FILE), "collections.db")
MAX_FILE_BYTES = 50 * 1024 * 1024

# CSV header names, compared case-insensitively after trimming.
_NAME_HEADERS = ("name", "card name", "card", "card_name", "product name")
_QTY_HEADERS = ("quantity", "count", "qty", "amount", "copies")


class CollectionError(ValueError):
    """A problem with a collection request that the caller can fix."""


def read_collection_file(path: str) -> str:
    """Read a collection export; UTF-8 (with or without BOM), else Windows-1252."""
    full = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isfile(full):
        raise CollectionError(f"File not found: {full}. Pass an absolute path to a text or CSV file.")
    if os.path.getsize(full) > MAX_FILE_BYTES:
        raise CollectionError(f"{full} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    with open(full, "rb") as f:
        raw = f.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _csv_columns(line: str) -> Optional[Tuple[str, int, Optional[int]]]:
    """(delimiter, name column, quantity column) if the line is a CSV header row."""
    for delimiter in (",", ";", "\t"):
        if delimiter not in line:
            continue
        header = [h.strip().strip('"').lower() for h in next(csv.reader([line], delimiter=delimiter))]
        name_col = next((header.index(h) for h in _NAME_HEADERS if h in header), None)
        if name_col is None:
            continue
        qty_col = next((header.index(h) for h in _QTY_HEADERS if h in header), None)
        return delimiter, name_col, qty_col
    return None


def parse_collection(text: str) -> ParseResult:
    """Parse a collection export into merged {'quantity', 'name', 'lines'} entries."""
    lines = (text or "").splitlines()
    # Dragon Shield (and Excel) exports may start with a "sep=," hint.
    if lines and lines[0].strip().strip('"').lower().startswith("sep="):
        lines = lines[1:]
    first = next((line for line in lines if line.strip()), "")
    columns = _csv_columns(first)
    if columns is None:
        return parse_decklist_detailed("\n".join(lines))

    delimiter, name_col, qty_col = columns
    result = ParseResult()
    by_name: Dict[str, Dict] = {}
    body = "\n".join(lines[lines.index(first) + 1:])
    for row in csv.reader(io.StringIO(body), delimiter=delimiter):
        if not any(cell.strip() for cell in row):
            continue
        line = delimiter.join(row)
        name = row[name_col].strip() if name_col < len(row) else ""
        if not name:
            result.skipped.append({"line": line, "reason": "No card name in the name column"})
            continue
        quantity = 1
        if qty_col is not None and qty_col < len(row) and row[qty_col].strip():
            match = re.match(r"\s*(\d+)", row[qty_col])
            if not match:
                result.skipped.append({"line": line, "reason": "Quantity is not a number"})
                continue
            quantity = int(match.group(1))
        if quantity <= 0:
            continue
        key = name.casefold()
        if key in by_name:
            by_name[key]["quantity"] += quantity
            by_name[key]["lines"].append(line)
        else:
            by_name[key] = {"quantity": quantity, "name": name, "lines": [line]}
            result.cards.append(by_name[key])
    return result


class CollectionStore:
    """Named collections in their own small SQLite file (the card DB stays read-only)."""

    def __init__(self, path: str = COLLECTIONS_FILE):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS collections (
                name       TEXT PRIMARY KEY COLLATE NOCASE,
                source     TEXT,
                updated_at INTEGER NOT NULL   -- nanoseconds; also a change token
            );
            CREATE TABLE IF NOT EXISTS collection_cards (
                collection      TEXT NOT NULL COLLATE NOCASE,
                name_normalized TEXT NOT NULL,  -- card_index key
                quantity        INTEGER NOT NULL,
                PRIMARY KEY (collection, name_normalized)
            ) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS collection_unmatched (
                collection TEXT NOT NULL COLLATE NOCASE,
                name       TEXT NOT NULL,
                quantity   INTEGER,
                reason     TEXT,
                PRIMARY KEY (collection, name)
            ) WITHOUT ROWID;
        """)
        self.conn.commit()

    def get(self, name: str) -> Optional[Dict]:
        row = self.conn.execute("SELECT * FROM collections WHERE name = ?", ((name or "").strip(),)).fetchone()
        return dict(row) if row else None

    def require(self, name: str) -> Dict:
        found = self.get(name)
        if found:
            return found
        known = [c["name"] for c in self.list()]
        raise CollectionError(f"No collection named '{name}'. "
                              + (f"Available: {', '.join(known)}." if known
                                 else "Import one with import_collection first."))

    def list(self) -> List[Dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT c.name, c.source, c.updated_at, COUNT(cc.name_normalized) AS unique_cards, "
            "COALESCE(SUM(cc.quantity), 0) AS copies, "
            "(SELECT COUNT(*) FROM collection_unmatched u WHERE u.collection = c.name) AS unmatched "
            "FROM collections c LEFT JOIN collection_cards cc ON cc.collection = c.name "
            "GROUP BY c.name ORDER BY c.name")]

    def cards(self, name: str) -> Dict[str, int]:
        return {r[0]: r[1] for r in self.conn.execute(
            "SELECT name_normalized, quantity FROM collection_cards WHERE collection = ?", (name,))}

    def unmatched(self, name: str) -> List[Dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT name, quantity, reason FROM collection_unmatched WHERE collection = ? ORDER BY name",
            (name,))]

    def save(self, name: str, cards: Dict[str, int], unmatched: Sequence[Dict],
             source: str, mode: str = "replace") -> Dict:
        """Store resolved cards {card_index key: quantity}; mode 'add' merges into an existing one."""
        name = (name or "").strip()
        if not name:
            raise CollectionError("A collection needs a name.")
        if mode not in ("replace", "add"):
            raise CollectionError("mode must be 'replace' or 'add'.")
        existing = self.get(name)
        with self.conn:
            if existing:
                name = existing["name"]  # keep the original spelling
                if mode == "replace":
                    self.conn.execute("DELETE FROM collection_cards WHERE collection = ?", (name,))
                    self.conn.execute("DELETE FROM collection_unmatched WHERE collection = ?", (name,))
                else:
                    source = f"{existing['source']}; {source}"
            self.conn.execute("INSERT OR REPLACE INTO collections VALUES (?, ?, ?)",
                              (name, source, time.time_ns()))
            self.conn.executemany(
                "INSERT INTO collection_cards VALUES (?, ?, ?) ON CONFLICT(collection, name_normalized) "
                "DO UPDATE SET quantity = quantity + excluded.quantity",
                [(name, key, qty) for key, qty in cards.items()])
            self.conn.executemany(
                "INSERT INTO collection_unmatched VALUES (?, ?, ?, ?) ON CONFLICT(collection, name) "
                "DO UPDATE SET quantity = quantity + excluded.quantity",
                [(name, u["name"], u.get("quantity"), u.get("reason")) for u in unmatched])
        return self.get(name)

    def delete(self, name: str) -> str:
        name = self.require(name)["name"]
        with self.conn:
            for table, column in (("collection_cards", "collection"),
                                  ("collection_unmatched", "collection"), ("collections", "name")):
                self.conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (name,))
        return name
