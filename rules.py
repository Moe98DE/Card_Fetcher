# rules.py
"""
Magic: The Gathering Comprehensive Rules: download, store and index locally.

Fetching is a manual, user-triggered step (GUI "Update Rules" button or
`python rules.py`). The MCP server never imports this module; Claude only gets
read access to the stored file and the parsed index (see card_search.py).

Stored data:
* <app data>/comprehensive_rules.txt  - the original text file, normalised to UTF-8
* tables in the card cache database:
    rules     (number, title/text, parent, chapter order)  + rules_fts
    glossary  (term, definition)
"""
import os
import re
import shutil
import sqlite3
import time
from typing import Callable, Dict, List, Optional, Tuple

import requests

import search_index as si
from database import DB_FILE

RULES_PAGE = "https://magic.wizards.com/en/rules"
RULES_FILE = os.path.join(os.path.dirname(DB_FILE), "comprehensive_rules.txt")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; Card_Fetcher/1.0)"}
TIMEOUT = 60

CHAPTER_RE = re.compile(r"^(\d)\. (.+)$")
SECTION_RE = re.compile(r"^(\d{3})\. (.+)$")
RULE_RE = re.compile(r"^(\d{3}\.\d+)\. (.+)$")
SUBRULE_RE = re.compile(r"^(\d{3}\.\d+[a-z])\.? (.+)$")


class RulesError(RuntimeError):
    pass


def _request_kwargs() -> Dict:
    kwargs = {"timeout": TIMEOUT, "headers": HEADERS}
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE")
    if ca_bundle:
        kwargs["verify"] = ca_bundle
    return kwargs


# ---------------------------------------------------------------------------
# Fetching (manual only)
# ---------------------------------------------------------------------------

def discover_latest_url() -> str:
    """Find the newest .txt Comprehensive Rules link on Wizards' rules page."""
    try:
        resp = requests.get(RULES_PAGE, **_request_kwargs())
        resp.raise_for_status()
    except requests.exceptions.RequestException as err:
        raise RulesError(f"Could not open {RULES_PAGE}: {err}") from err
    links = set(re.findall(r'https?://media\.wizards\.com/[^"\'<>]+?\.txt', resp.text))
    if not links:
        raise RulesError("No .txt rules link found on the Wizards rules page. "
                         "Paste the download URL manually.")
    # File names end in YYYYMMDD; pick the newest.
    return max(links, key=lambda u: re.findall(r"(\d{8})", u)[-1] if re.findall(r"(\d{8})", u) else "")


def download_rules_text(url: str) -> str:
    try:
        resp = requests.get(requests.utils.requote_uri(url.strip()), **_request_kwargs())
        resp.raise_for_status()
    except requests.exceptions.RequestException as err:
        raise RulesError(f"Download failed: {err}") from err
    return _decode(resp.content)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise RulesError("Could not decode the rules file.")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", "\n")
    lines = [("" if line.strip("\xa0 \t") == "" else line.rstrip()) for line in text.split("\n")]
    return "\n".join(lines).strip() + "\n"


def validate(text: str) -> None:
    if "Comprehensive Rules" not in text[:500] or "\nGlossary\n" not in text:
        raise RulesError("This doesn't look like the Magic Comprehensive Rules text file "
                         "(use the .txt version, not the PDF or DOCX).")


def effective_date(text: str) -> Optional[str]:
    m = re.search(r"effective as of ([A-Z][a-z]+ \d{1,2}, \d{4})", text[:1000])
    return m.group(1) if m else None


def parse(text: str) -> Tuple[List[Dict], List[Tuple[str, str]]]:
    """Return (rules, glossary). Rules keep document order; examples stay with their rule."""
    # The body starts at the second "1. Game Concepts" (the first is the table of contents).
    first = text.index("\n1. Game Concepts")
    body_start = text.index("\n1. Game Concepts", first + 1)
    glossary_start = text.rindex("\nGlossary\n")
    credits = text.rfind("\nCredits\n")
    body = text[body_start:glossary_start]
    glossary_text = text[glossary_start + len("\nGlossary\n"): credits if credits > glossary_start else None]

    rules: List[Dict] = []
    current = None
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        for kind, regex in (("chapter", CHAPTER_RE), ("section", SECTION_RE),
                            ("rule", RULE_RE), ("subrule", SUBRULE_RE)):
            m = regex.match(line)
            if m:
                number, rest = m.group(1), m.group(2)
                if kind == "chapter":
                    parent = None
                elif kind == "section":
                    parent = number[0]
                elif kind == "rule":
                    parent = number.split(".")[0]
                else:
                    parent = number[:-1]
                current = {"number": number, "kind": kind, "parent": parent, "text": rest}
                rules.append(current)
                break
        else:
            if current is not None:  # "Example: ..." and continuation lines
                current["text"] += "\n" + line

    glossary: List[Tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", glossary_text.strip()):
        parts = [p.strip() for p in block.split("\n") if p.strip()]
        if len(parts) >= 2:
            glossary.append((parts[0], "\n".join(parts[1:])))
    return rules, glossary


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    si.ensure_schema(conn)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rules (
            ord    INTEGER PRIMARY KEY,
            number TEXT UNIQUE,
            kind   TEXT,
            parent TEXT,
            text   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rules_parent ON rules(parent);
        CREATE TABLE IF NOT EXISTS glossary (
            term       TEXT PRIMARY KEY,
            definition TEXT
        );
    """)
    conn.commit()


def store(conn: sqlite3.Connection, text: str, source: str) -> Dict:
    text = normalize(text)
    validate(text)
    rules, glossary = parse(text)
    if len(rules) < 1000:
        raise RulesError(f"Only {len(rules)} rules parsed; the file format may have changed.")

    # Keep the previous file as a backup, then write the new one.
    if os.path.exists(RULES_FILE):
        shutil.copyfile(RULES_FILE, RULES_FILE + ".previous")
    with open(RULES_FILE, "w", encoding="utf-8") as fh:
        fh.write(text)

    ensure_schema(conn)
    cur = conn.cursor()
    cur.execute("BEGIN")
    cur.execute("DELETE FROM rules")
    cur.execute("DELETE FROM glossary")
    cur.executemany("INSERT OR REPLACE INTO rules (number, kind, parent, text) VALUES (?, ?, ?, ?)",
                    [(r["number"], r["kind"], r["parent"], r["text"]) for r in rules])
    cur.executemany("INSERT OR REPLACE INTO glossary (term, definition) VALUES (?, ?)", glossary)
    fts = "0"
    try:
        cur.execute("DROP TABLE IF EXISTS rules_fts")
        cur.execute("CREATE VIRTUAL TABLE rules_fts USING fts5(number UNINDEXED, kind UNINDEXED, text, "
                    "tokenize='porter unicode61')")
        cur.execute("INSERT INTO rules_fts (number, kind, text) SELECT number, kind, text FROM rules")
        cur.execute("INSERT INTO rules_fts (number, kind, text) "
                    "SELECT 'glossary:' || term, 'glossary', term || '\n' || definition FROM glossary")
        fts = "1"
    except sqlite3.OperationalError:
        pass
    info = {
        "rules_effective": effective_date(text) or "unknown",
        "rules_source": source,
        "rules_updated_at": int(time.time()),
        "rules_count": len(rules),
        "glossary_count": len(glossary),
        "rules_fts": fts,
        "rules_file": RULES_FILE,
    }
    for key, value in info.items():
        cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    return info


def update_from_url(conn: sqlite3.Connection, url: Optional[str] = None,
                    status: Callable[[str], None] = print) -> Dict:
    if not url:
        status("Looking up the latest Comprehensive Rules on magic.wizards.com...")
        url = discover_latest_url()
    status(f"Downloading {url.rsplit('/', 1)[-1]}...")
    text = download_rules_text(url)
    status("Indexing rules...")
    return store(conn, text, url)


def import_file(conn: sqlite3.Connection, path: str) -> Dict:
    with open(path, "rb") as fh:
        text = _decode(fh.read())
    return store(conn, text, os.path.abspath(path))


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Download or import the MTG Comprehensive Rules.")
    ap.add_argument("source", nargs="?", help="URL or local .txt file (default: auto-detect latest)")
    args = ap.parse_args()
    connection = si.connect()
    if args.source and os.path.exists(args.source):
        result = import_file(connection, args.source)
    else:
        result = update_from_url(connection, args.source)
    print(f"Comprehensive Rules effective {result['rules_effective']}: "
          f"{result['rules_count']} rules, {result['glossary_count']} glossary terms -> {result['rules_file']}")
