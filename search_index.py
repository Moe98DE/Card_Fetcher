# search_index.py
"""
Searchable index on top of the existing local card cache.

The original ``cards`` table stores one Scryfall JSON blob per card name, which
is ideal for "look these names up" but cannot answer "find cards that do X".
This module adds, in the same SQLite file and without touching the original
tables:

* ``card_index``  - one row per card with filterable columns (colour identity,
                     mana value, price, EDHREC rank, legality, ...).
* ``card_fts``    - full-text index over name, type line and rules text.
* ``tags``        - every Scryfall Tagger *oracle* tag (function tags such as
                     ``sacrifice-outlet`` or ``ramp``), with descriptions,
                     aliases and the parent/child hierarchy.
* ``tag_cards``   - which cards carry which tag. The hierarchy is flattened
                     here: Scryfall's bulk file only lists *direct* taggings,
                     so a parent like ``removal`` has no cards of its own. We
                     store every descendant's cards under the parent as well.

Everything is built from local data; nothing in here talks to the network.
"""
import json
import sqlite3
import time
from typing import Callable, Dict, Iterable, List, Optional, Set

from database import DB_FILE, CHEAPEST_EUR_PRICE_KEY

COLOR_BITS = {"W": 1, "U": 2, "B": 4, "R": 8, "G": 16}

# Layouts that are never real, deck-buildable cards.
NON_CARD_LAYOUTS = {
    "token", "double_faced_token", "emblem", "art_series",
    "planar", "scheme", "vanguard",
}

ProgressCallback = Optional[Callable[[int, int], None]]

# Where rules.py stores the Comprehensive Rules text (readers only need the path).
import os as _os
RULES_TEXT_FILE = _os.path.join(_os.path.dirname(DB_FILE), "comprehensive_rules.txt")


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

def connect(db_file: str = DB_FILE, read_only: bool = False) -> sqlite3.Connection:
    """Open the shared cache database. Read-only mode never takes write locks."""
    if read_only:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, check_same_thread=False)
    else:
        conn = sqlite3.connect(db_file, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS card_index (
            name_normalized TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            oracle_id       TEXT,
            mana_cost       TEXT,
            mana_value      REAL,
            type_line       TEXT,
            oracle_text     TEXT,
            color_identity  TEXT,     -- e.g. "BG" in WUBRG order, "" = colourless
            ci_mask         INTEGER,  -- W=1 U=2 B=4 R=8 G=16
            rarity          TEXT,
            price_eur       REAL,
            edhrec_rank     INTEGER,
            commander_legal INTEGER,
            game_changer    INTEGER,
            layout          TEXT,
            is_card         INTEGER   -- 0 for tokens, art series, emblems ...
        );
        CREATE INDEX IF NOT EXISTS idx_card_index_oracle ON card_index(oracle_id);
        CREATE INDEX IF NOT EXISTS idx_card_index_rank   ON card_index(edhrec_rank);

        CREATE TABLE IF NOT EXISTS tags (
            tag_key      INTEGER PRIMARY KEY,
            id           TEXT UNIQUE,
            slug         TEXT UNIQUE,
            label        TEXT,
            description  TEXT,
            aliases      TEXT,   -- JSON list
            parent_slugs TEXT,   -- JSON list
            child_slugs  TEXT,   -- JSON list
            direct_count INTEGER,
            total_count  INTEGER -- including all descendant tags
        );

        CREATE TABLE IF NOT EXISTS tag_aliases (
            alias   TEXT PRIMARY KEY,
            tag_key INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tag_cards (
            tag_key   INTEGER NOT NULL,
            oracle_id TEXT NOT NULL,
            direct    INTEGER NOT NULL,  -- 1 = tagged directly, 0 = via a child tag
            PRIMARY KEY (tag_key, oracle_id)
        ) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS idx_tag_cards_oracle ON tag_cards(oracle_id);
    """)
    conn.commit()


def fts_available(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM meta WHERE key = 'fts'").fetchone()
    return bool(row and row[0] == "1")


def get_meta(conn: sqlite3.Connection) -> Dict[str, str]:
    try:
        return {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.OperationalError:
        return {}


def _set_meta(cur: sqlite3.Cursor, **values) -> None:
    for key, value in values.items():
        cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


# ---------------------------------------------------------------------------
# Card index
# ---------------------------------------------------------------------------

def _ci_string(colors: Iterable[str]) -> str:
    present = set(colors or [])
    return "".join(c for c in "WUBRG" if c in present)


def ci_mask(colors: Iterable[str]) -> int:
    mask = 0
    for c in colors or []:
        mask |= COLOR_BITS.get(c.upper(), 0)
    return mask


def _faces(card: Dict) -> List[Dict]:
    return card.get("card_faces") or []


def _joined(card: Dict, key: str, sep: str) -> str:
    top = card.get(key)
    if top:
        return top
    values = [f.get(key) for f in _faces(card) if f.get(key)]
    return sep.join(values)


def _price(card: Dict) -> Optional[float]:
    raw = card.get(CHEAPEST_EUR_PRICE_KEY)
    if raw in (None, "") and CHEAPEST_EUR_PRICE_KEY not in card:
        raw = (card.get("prices") or {}).get("eur")
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def card_index_row(card: Dict) -> Optional[tuple]:
    name = card.get("name")
    if not name:
        return None

    oracle_id = card.get("oracle_id")
    if not oracle_id:  # e.g. reversible_card keeps oracle_id on the faces
        oracle_id = next((f.get("oracle_id") for f in _faces(card) if f.get("oracle_id")), None)

    legality = (card.get("legalities") or {}).get("commander")
    layout = card.get("layout", "normal")
    ci = card.get("color_identity") or []

    return (
        name.lower().strip(),
        name,
        oracle_id,
        _joined(card, "mana_cost", " // "),
        card.get("cmc"),
        _joined(card, "type_line", " // "),
        _joined(card, "oracle_text", "\n//\n"),
        _ci_string(ci),
        ci_mask(ci),
        card.get("rarity"),
        _price(card),
        card.get("edhrec_rank"),
        1 if legality == "legal" else 0,
        1 if card.get("game_changer") else 0,
        layout,
        0 if layout in NON_CARD_LAYOUTS else 1,
    )


def rebuild_card_index(conn: sqlite3.Connection, progress_callback: ProgressCallback = None) -> int:
    """(Re)build card_index and card_fts from the existing ``cards`` table."""
    ensure_schema(conn)
    cur = conn.cursor()

    total = cur.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    cur.execute("BEGIN")
    cur.execute("DELETE FROM card_index")

    rows = []
    for i, (data,) in enumerate(conn.execute("SELECT data FROM cards")):
        try:
            row = card_index_row(json.loads(data))
        except (TypeError, ValueError):
            row = None
        if row:
            rows.append(row)
        if len(rows) >= 5000:
            cur.executemany("INSERT OR REPLACE INTO card_index VALUES (%s)" % ",".join("?" * 16), rows)
            rows.clear()
        if progress_callback and i % 2000 == 0:
            progress_callback(i, total)
    if rows:
        cur.executemany("INSERT OR REPLACE INTO card_index VALUES (%s)" % ",".join("?" * 16), rows)

    # Full-text search over name / type / rules text. Some SQLite builds lack
    # FTS5; search then falls back to LIKE, which is slower but works.
    fts = "0"
    try:
        cur.execute("DROP TABLE IF EXISTS card_fts")
        cur.execute(
            "CREATE VIRTUAL TABLE card_fts USING fts5("
            "name_normalized UNINDEXED, name, type_line, oracle_text, tokenize='porter unicode61')"
        )
        cur.execute(
            "INSERT INTO card_fts (name_normalized, name, type_line, oracle_text) "
            "SELECT name_normalized, name, type_line, oracle_text FROM card_index"
        )
        fts = "1"
    except sqlite3.OperationalError:
        pass

    indexed = cur.execute("SELECT COUNT(*) FROM card_index").fetchone()[0]
    _set_meta(cur, fts=fts, index_built_at=int(time.time()), indexed_cards=indexed)
    conn.commit()

    if progress_callback:
        progress_callback(total, total)
    return indexed


# ---------------------------------------------------------------------------
# Oracle tags
# ---------------------------------------------------------------------------

def import_oracle_tags(
    conn: sqlite3.Connection,
    tags_list: List[Dict],
    progress_callback: ProgressCallback = None,
) -> int:
    """Replace the stored oracle tags with the contents of Scryfall's oracle_tags bulk file."""
    ensure_schema(conn)
    by_id = {t["id"]: t for t in tags_list if t.get("id") and t.get("slug")}
    slug_of = {tid: t["slug"] for tid, t in by_id.items()}

    direct: Dict[str, Set[str]] = {
        tid: {tg["oracle_id"] for tg in (t.get("taggings") or []) if tg.get("oracle_id")}
        for tid, t in by_id.items()
    }

    # Flatten the hierarchy: a tag's cards = its own + all descendants' cards.
    flattened: Dict[str, Set[str]] = {}

    def collect(tid: str, visiting: Set[str]) -> Set[str]:
        if tid in flattened:
            return flattened[tid]
        if tid in visiting:  # defensive: the community data could contain a cycle
            return direct.get(tid, set())
        visiting.add(tid)
        result = set(direct.get(tid, set()))
        for child in by_id[tid].get("child_ids") or []:
            if child in by_id:
                result |= collect(child, visiting)
        visiting.discard(tid)
        flattened[tid] = result
        return result

    import sys
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, 10000))
    try:
        for tid in by_id:
            collect(tid, set())
    finally:
        sys.setrecursionlimit(old_limit)

    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.execute("DELETE FROM tag_cards")
    cur.execute("DELETE FROM tag_aliases")
    cur.execute("DELETE FROM tags")

    total = len(by_id)
    for i, (tid, tag) in enumerate(by_id.items()):
        slug = tag["slug"]
        aliases = [a for a in (tag.get("aliases") or []) if a and a != slug]
        cur.execute(
            "INSERT INTO tags (id, slug, label, description, aliases, parent_slugs, child_slugs, "
            "direct_count, total_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tid, slug, tag.get("label") or slug, tag.get("description"),
                json.dumps(aliases),
                json.dumps(sorted(slug_of[p] for p in tag.get("parent_ids") or [] if p in slug_of)),
                json.dumps(sorted(slug_of[c] for c in tag.get("child_ids") or [] if c in slug_of)),
                len(direct[tid]), len(flattened[tid]),
            ),
        )
        key = cur.lastrowid
        for alias in aliases:
            cur.execute("INSERT OR IGNORE INTO tag_aliases (alias, tag_key) VALUES (?, ?)", (alias, key))
        own = direct[tid]
        cur.executemany(
            "INSERT INTO tag_cards (tag_key, oracle_id, direct) VALUES (?, ?, ?)",
            ((key, oid, 1 if oid in own else 0) for oid in flattened[tid]),
        )
        if progress_callback and i % 200 == 0:
            progress_callback(i, total)

    _set_meta(cur, tags_updated_at=int(time.time()), tag_count=total)
    conn.commit()
    if progress_callback:
        progress_callback(total, total)
    return total


def update_tags_from_scryfall(conn: sqlite3.Connection, status: Callable[[str], None] = print,
                              progress_callback: ProgressCallback = None) -> int:
    """Download the oracle_tags bulk file and import it. The only networked step here."""
    from api_client import fetch_bulk_data_url, download_bulk_json

    status("Fetching Scryfall tag data URL...")
    url = fetch_bulk_data_url("oracle_tags")
    if not url:
        raise RuntimeError("Could not retrieve the Scryfall oracle tags download URL.")
    status("Downloading Scryfall oracle tags (~6 MB)...")
    tags = download_bulk_json(url)
    if not tags:
        raise RuntimeError("Tag download failed or the file was empty.")
    status(f"Importing {len(tags)} oracle tags...")
    return import_oracle_tags(conn, tags, progress_callback)


if __name__ == "__main__":
    # Headless maintenance: `python search_index.py` refreshes tags and the index.
    import argparse

    ap = argparse.ArgumentParser(description="Build the local tag/search index.")
    ap.add_argument("--skip-tags", action="store_true", help="only rebuild the card index")
    args = ap.parse_args()

    connection = connect()
    ensure_schema(connection)
    if not args.skip_tags:
        print(f"Imported {update_tags_from_scryfall(connection)} tags.")
    print(f"Indexed {rebuild_card_index(connection)} cards.")
