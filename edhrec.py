# edhrec.py
"""
EDHREC commander recommendations, fetched once and cached locally.

EDHREC has no official API; its website loads each commander page as JSON from
json.edhrec.com. This module requests exactly the page for the commander (and
optional theme / power bracket / budget) you ask for, caches it in a small
SQLite file next to the card database, and parses it into plain Python data.
Repeat requests within the cache lifetime never touch the network.

Page structure on EDHREC:  /commanders/<slug>[/<theme> | /<bracket>][/<budget>]
(a theme and a bracket cannot be combined).
"""
import json
import os
import re
import sqlite3
import time
import unicodedata
from typing import Dict, List, Optional, Sequence

import requests

from database import DB_FILE

EDHREC_JSON = "https://json.edhrec.com/pages"
CACHE_FILE = os.path.join(os.path.dirname(DB_FILE), "edhrec_cache.db")
CACHE_TTL_SECONDS = 7 * 24 * 3600
REQUEST_TIMEOUT = 30
HEADERS = {"User-Agent": "Card_Fetcher/1.0 (personal deckbuilding tool)", "Accept": "application/json"}

BRACKETS = ("exhibition", "core", "upgraded", "optimized", "cedh")
BUDGETS = ("budget", "expensive")

# Lists on the page that are views across the whole pool rather than card types.
META_SECTIONS = {"newcards", "topcards", "highliftcards", "highsynergycards", "gamechangers"}


class EdhrecError(RuntimeError):
    """Something the caller can act on (unknown commander, bad theme, offline...)."""


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """EDHREC's URL form of a card name: front face only, ASCII, hyphenated."""
    front = (name or "").split("//")[0]
    ascii_name = unicodedata.normalize("NFKD", front).encode("ascii", "ignore").decode()
    ascii_name = re.sub(r"['’.,!?]", "", ascii_name.lower())
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")


def commander_slug(commanders: Sequence[str]) -> str:
    """One commander, or two partners (EDHREC joins them; redirects fix the order)."""
    parts = [slugify(c) for c in commanders if c and c.strip()]
    if not parts:
        raise EdhrecError("No commander given.")
    if len(parts) > 2:
        raise EdhrecError("At most two commanders (partners / background) are supported.")
    return "-".join(sorted(parts))


def page_path(commanders: Sequence[str], theme: Optional[str] = None,
              bracket: Optional[str] = None, budget: Optional[str] = None) -> str:
    if theme and bracket:
        raise EdhrecError("EDHREC pages combine a theme OR a power bracket with a budget, not both.")
    path = f"commanders/{commander_slug(commanders)}"
    if theme:
        path += f"/{slugify(theme)}"
    if bracket:
        b = bracket.strip().lower()
        if b not in BRACKETS:
            raise EdhrecError(f"bracket must be one of {', '.join(BRACKETS)}")
        path += f"/{b}"
    if budget:
        b = budget.strip().lower()
        if b not in BUDGETS:
            raise EdhrecError(f"budget must be one of {', '.join(BUDGETS)}")
        path += f"/{b}"
    return path


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

class EdhrecClient:
    def __init__(self, cache_file: str = CACHE_FILE, ttl: int = CACHE_TTL_SECONDS):
        self.ttl = ttl
        self.conn = sqlite3.connect(cache_file, check_same_thread=False, timeout=10)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS pages (path TEXT PRIMARY KEY, fetched_at INTEGER, body TEXT)"
        )
        self.conn.commit()

    def _cached(self, path: str, allow_stale: bool = False) -> Optional[Dict]:
        row = self.conn.execute("SELECT fetched_at, body FROM pages WHERE path = ?", (path,)).fetchone()
        if not row:
            return None
        if not allow_stale and time.time() - row[0] > self.ttl:
            return None
        data = json.loads(row[1])
        data["_fetched_at"] = row[0]
        return data

    def get(self, path: str, refresh: bool = False, _depth: int = 0) -> Dict:
        if not refresh:
            cached = self._cached(path)
            if cached is not None:
                return cached
        try:
            kwargs = {"timeout": REQUEST_TIMEOUT, "headers": HEADERS}
            ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE")
            if ca_bundle:
                kwargs["verify"] = ca_bundle
            resp = requests.get(f"{EDHREC_JSON}/{path}.json", **kwargs)
        except requests.exceptions.RequestException as err:
            stale = self._cached(path, allow_stale=True)
            if stale is not None:  # offline: an old page beats nothing
                stale["_stale"] = True
                return stale
            raise EdhrecError(f"Could not reach EDHREC: {err}") from err

        if resp.status_code in (403, 404):
            raise EdhrecError(f"EDHREC has no page '{path}'.")
        if resp.status_code != 200:
            raise EdhrecError(f"EDHREC returned HTTP {resp.status_code} for '{path}'.")
        try:
            data = resp.json()
        except ValueError as err:
            raise EdhrecError(f"EDHREC returned an unreadable page for '{path}'.") from err

        # Partner pairs in the "wrong" order answer with a redirect stub.
        if isinstance(data, dict) and data.get("redirect") and _depth < 2:
            target = data["redirect"].strip("/")
            return self.get(target, refresh=refresh, _depth=_depth + 1)

        now = int(time.time())
        self.conn.execute("INSERT OR REPLACE INTO pages (path, fetched_at, body) VALUES (?, ?, ?)",
                          (path, now, json.dumps(data)))
        self.conn.commit()
        data["_fetched_at"] = now
        return data

    # -- parsed views ---------------------------------------------------------

    def commander_page(self, commanders: Sequence[str], theme: Optional[str] = None,
                       bracket: Optional[str] = None, budget: Optional[str] = None,
                       refresh: bool = False) -> Dict:
        path = page_path(commanders, theme, bracket, budget)
        try:
            raw = self.get(path, refresh=refresh)
        except EdhrecError:
            if theme:
                # Most likely a theme name that isn't one of EDHREC's; say which ones exist.
                base = self.get(page_path(commanders), refresh=False)
                themes = ", ".join(t["slug"] for t in parse_page(base)["themes"][:25])
                raise EdhrecError(f"No EDHREC theme '{theme}' for this commander. "
                                  f"Available themes: {themes}")
            raise
        page = parse_page(raw)
        page["path"] = path
        return page

    def average_deck(self, commanders: Sequence[str], theme: Optional[str] = None,
                     bracket: Optional[str] = None, budget: Optional[str] = None,
                     refresh: bool = False) -> Dict:
        path = "average-decks/" + page_path(commanders, theme, bracket, budget).split("/", 1)[1]
        raw = self.get(path, refresh=refresh)
        deck = raw.get("deck") or {}
        sections = {}
        for section, entries in (deck.get("cards") or {}).items():
            sections[section] = [(e[0], e[1] if len(e) > 1 else 1) for e in entries if e]
        return {
            "path": path,
            "header": raw.get("header"),
            "commanders": deck.get("commander") or [],
            "sections": sections,
            "fetched_at": raw.get("_fetched_at"),
            "stale": raw.get("_stale", False),
        }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_page(raw: Dict) -> Dict:
    jd = (raw.get("container") or {}).get("json_dict") or {}
    commander = jd.get("card") or {}
    panels = raw.get("panels") or {}

    cards: Dict[str, Dict] = {}
    for cardlist in jd.get("cardlists") or []:
        section = cardlist.get("tag") or ""
        header = cardlist.get("header") or section
        for cv in cardlist.get("cardviews") or []:
            name = cv.get("name")
            if not name:
                continue
            entry = cards.get(name)
            if entry is None:
                potential = cv.get("potential_decks") or 0
                entry = cards[name] = {
                    "name": name,
                    "section": None,
                    "labels": [],
                    "num_decks": cv.get("num_decks"),
                    "potential_decks": potential,
                    "inclusion": (cv.get("num_decks") or 0) / potential if potential else None,
                    "synergy": cv.get("synergy"),
                    "lift": cv.get("lift"),
                    "trend": cv.get("trend_zscore"),
                }
            if section in META_SECTIONS:
                entry["labels"].append(header)
            elif entry["section"] is None:
                entry["section"] = header

    for entry in cards.values():
        entry["section"] = entry["section"] or (entry["labels"][0] if entry["labels"] else "Other")

    type_counts = {k: raw.get(k) for k in ("creature", "instant", "sorcery", "artifact", "enchantment",
                                            "planeswalker", "battle", "land", "basic", "nonbasic")
                   if raw.get(k) is not None}
    return {
        "header": raw.get("header"),
        "commander": commander.get("name"),
        "num_decks": commander.get("num_decks"),
        "rank": commander.get("rank"),
        "salt": commander.get("salt"),
        "themes": [{"slug": t.get("slug"), "name": t.get("value"), "decks": t.get("count")}
                   for t in panels.get("taglinks") or [] if t.get("slug")],
        "similar": raw.get("similar") or [],
        "type_counts": type_counts,
        "mana_curve": panels.get("mana_curve") or {},
        "budget_counts": raw.get("budget_counts") or {},
        "bracket_counts": raw.get("bracket_counts") or {},
        "cards": list(cards.values()),
        "fetched_at": raw.get("_fetched_at"),
        "stale": raw.get("_stale", False),
    }
