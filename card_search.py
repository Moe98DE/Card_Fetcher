# card_search.py
"""
Offline card search over the local index built by ``search_index.py``.

This is the engine behind the Claude Desktop (MCP) tools, but it is plain
Python with no MCP dependency, so the GUI or other scripts can use it too.
Every function only reads the local SQLite cache; nothing touches the network.
"""
import difflib
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import search_index as si
from formatter import format_deck_as_markdown
from models import Card

# Cards ranked better than this on EDHREC count as "staples" for deep cuts.
DEFAULT_DEEP_CUT_RANK = 3000
MAX_LIMIT = 100

SORTS = ("popular", "deep_cut", "least_popular", "price", "mana_value", "name", "random")

# Common deckbuilding words that are not Tagger slugs.
TAG_SYNONYMS = {
    "board wipe": "sweeper", "boardwipe": "sweeper", "wrath": "sweeper",
    "mass removal": "sweeper", "wipe": "sweeper",
    "card draw": "draw", "reanimation": "reanimate", "reanimator": "reanimate",
    "sac outlet": "sacrifice-outlet", "sac": "sacrifice", "gy": "graveyard",
    "counter": "counterspell", "counters spell": "counterspell",
    "rock": "mana-rock", "dork": "mana-dork",
    "wincon": "win", "finisher": "win",
    "aristocrats": "sacrifice-outlet death-trigger drain", "blink": "flicker",
    "token maker": "token generator", "token makers": "token generator",
    "stax": "hate tax", "voltron": "equipment aura",
}

# Trivia / bookkeeping tags that say nothing about how a deck plays.
NOISE_TAG = re.compile(r"(^cycle-|name|achievement|^activated-ability$|^triggered-ability$|"
                       r"^static-ability$|^reprint|^art-|^flavor|^printed-|^fun-)")

# Roles a Commander deck usually wants, used by analyze_deck.
CORE_ROLES = [
    ("ramp", "Ramp"),
    ("card-advantage", "Card advantage"),
    ("draw", "Draw"),
    ("spot-removal", "Spot removal"),
    ("sweeper", "Board wipes"),
    ("counterspell", "Counterspells"),
    ("tutor", "Tutors (incl. land tutors)"),
    ("protection", "Protection"),
    ("recursion", "Recursion"),
]


class SearchError(ValueError):
    """A problem with the query that the caller (or model) can fix."""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

class CardSearch:
    def __init__(self, conn: Optional[sqlite3.Connection] = None):
        self.conn = conn or si.connect(read_only=True)
        self._tags_cache: Optional[List[Dict]] = None
        self._names_cache: Optional[Dict[str, str]] = None

    # -- status -----------------------------------------------------------

    def status(self) -> Dict:
        meta = si.get_meta(self.conn)
        return {
            "indexed_cards": int(meta.get("indexed_cards", 0)),
            "tag_count": int(meta.get("tag_count", 0)),
            "index_built_at": meta.get("index_built_at"),
            "tags_updated_at": meta.get("tags_updated_at"),
            "full_text": meta.get("fts") == "1",
        }

    def require_index(self, need_tags: bool = False) -> None:
        st = self.status()
        if not st["indexed_cards"]:
            raise SearchError(
                "The local search index is empty. Open MTG Deck Formatter and click "
                "'Update Local DB' (or 'Update Tags' if cards are already downloaded)."
            )
        if need_tags and not st["tag_count"]:
            raise SearchError(
                "No Scryfall tags are installed yet. Open MTG Deck Formatter and click 'Update Tags'."
            )

    # -- names ------------------------------------------------------------

    def resolve_names(self, names: Sequence[str]) -> Dict[str, Optional[str]]:
        """Map user-supplied names (incl. single faces) to name_normalized keys."""
        out = {}
        for raw in names:
            key = (raw or "").strip().lower()
            if not key:
                continue
            row = self.conn.execute(
                "SELECT name_normalized FROM card_index WHERE name_normalized = ?", (key,)
            ).fetchone()
            if not row:
                try:
                    row = self.conn.execute(
                        "SELECT a.name_normalized FROM aliases a "
                        "JOIN card_index c ON c.name_normalized = a.name_normalized "
                        "WHERE a.alias = ? AND c.is_card = 1", (key,)
                    ).fetchone()
                except sqlite3.OperationalError:
                    row = None
            if not row:
                # Face of a multi-part card whose alias was claimed by an art
                # series card in databases built by older app versions.
                row = self.conn.execute(
                    "SELECT name_normalized FROM card_index WHERE is_card = 1 AND "
                    "(name_normalized LIKE ? OR name_normalized LIKE ?) "
                    "ORDER BY edhrec_rank IS NULL, edhrec_rank LIMIT 1",
                    (key.replace("%", "") + " // %", "% // " + key.replace("%", "")),
                ).fetchone()
            out[raw] = row[0] if row else None
        return out

    def suggest_names(self, name: str, limit: int = 3) -> List[str]:
        words = [w for w in re.findall(r"[a-z0-9']+", name.lower()) if len(w) > 2]
        rows = []
        if words:
            where = " AND ".join("name_normalized LIKE ?" for _ in words)
            rows = self.conn.execute(
                f"SELECT name FROM card_index WHERE is_card = 1 AND {where} "
                f"ORDER BY edhrec_rank IS NULL, edhrec_rank LIMIT ?",
                [f"%{w}%" for w in words] + [limit],
            ).fetchall()
        if rows:
            return [r[0] for r in rows]
        # Typos ("Sol Rnig"): fall back to fuzzy matching over all card names.
        if self._names_cache is None:
            self._names_cache = {r[0].lower(): r[0] for r in self.conn.execute(
                "SELECT name FROM card_index WHERE is_card = 1")}
        close = difflib.get_close_matches(name.lower().strip(), self._names_cache.keys(), n=limit, cutoff=0.75)
        return [self._names_cache[c] for c in close]

    # -- tags -------------------------------------------------------------

    def _tags(self) -> List[Dict]:
        if self._tags_cache is None:
            rows = self.conn.execute(
                "SELECT tag_key, slug, description, aliases, parent_slugs, child_slugs, "
                "direct_count, total_count FROM tags"
            ).fetchall()
            self._tags_cache = [
                {
                    "key": r["tag_key"], "slug": r["slug"], "description": r["description"] or "",
                    "aliases": json.loads(r["aliases"] or "[]"),
                    "parents": json.loads(r["parent_slugs"] or "[]"),
                    "children": json.loads(r["child_slugs"] or "[]"),
                    "direct_count": r["direct_count"], "total_count": r["total_count"],
                }
                for r in rows
            ]
        return self._tags_cache

    def tag(self, name: str) -> Optional[Dict]:
        """Look a tag up by slug or alias (case/space insensitive)."""
        slug = _slugify(name)
        for candidate in (slug, _slugify(TAG_SYNONYMS.get(slug.replace("-", " "), ""))):
            if not candidate:
                continue
            for t in self._tags():
                if t["slug"] == candidate or candidate in t["aliases"]:
                    return t
        return None

    def _require_tags(self, names: Sequence[str]) -> List[Dict]:
        found, missing = [], []
        for n in names or []:
            t = self.tag(n)
            (found.append(t) if t else missing.append(n))
        if missing:
            hints = []
            for m in missing:
                guesses = [t["slug"] for t in self.find_tags(m, limit=5)]
                hints.append(f"'{m}' (did you mean: {', '.join(guesses) or 'no close match'})")
            raise SearchError("Unknown tag(s): " + "; ".join(hints) +
                              ". Use find_tags to discover valid tag slugs.")
        return found

    def find_tags(self, query: str, limit: int = 20) -> List[Dict]:
        """Rank tags by how well slug, aliases and description match a free-text query."""
        self.require_index(need_tags=True)
        q = re.sub(r"[-_]+", " ", query.strip().lower())
        if not q:
            return []
        if q in TAG_SYNONYMS:
            q = f"{q} {TAG_SYNONYMS[q]}"
        words = {_stem(w) for w in re.findall(r"[a-z0-9]+", q.replace("-", " ")) if w}
        target_slug = _slugify(query)

        scored = []
        for t in self._tags():
            slug_words = {_stem(w) for w in t["slug"].split("-")}
            alias_words = {_stem(w) for a in t["aliases"] for w in a.split("-")}
            desc_words = {_stem(w) for w in re.findall(r"[a-z0-9]+", t["description"].lower())}

            score = 0.0
            if t["slug"] == target_slug or target_slug in t["aliases"]:
                score += 100
            in_slug = len(words & (slug_words | alias_words))
            score += 12 * in_slug
            if words and words <= slug_words | alias_words:
                score += 25 - len(slug_words)  # full match; shorter slugs are more general
            score += 4 * len(words & desc_words)
            if score <= 0:
                continue
            score += math.log10(1 + t["total_count"])  # tie-break toward useful tags
            scored.append((score, t))

        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:max(1, min(limit, MAX_LIMIT))]]

    def list_tags(self, parent: Optional[str] = None, prefix: Optional[str] = None,
                  min_cards: int = 0, offset: int = 0, limit: int = 100) -> Dict:
        """Browse the tag catalogue: roots, children of a tag, or slugs by prefix."""
        self.require_index(need_tags=True)
        header = None
        tags = self._tags()
        if parent:
            p = self._require_tags([parent])[0]
            header = p
            wanted = set(p["children"])
            tags = [t for t in tags if t["slug"] in wanted]
        elif prefix:
            pre = _slugify(prefix)
            tags = [t for t in tags if t["slug"].startswith(pre)]
        else:
            tags = [t for t in tags if not t["parents"]]
        tags = [t for t in tags if t["total_count"] >= min_cards]
        tags.sort(key=lambda t: (-t["total_count"], t["slug"]))
        limit = max(1, min(limit, 500))
        return {"parent": header, "total": len(tags), "offset": offset,
                "tags": tags[offset:offset + limit]}

    def card_tags(self, names: Sequence[str]) -> Dict[str, Optional[List[Dict]]]:
        """Direct tags for each card (most specific tags first)."""
        self.require_index(need_tags=True)
        out = {}
        for raw, key in self.resolve_names(names).items():
            if not key:
                out[raw] = None
                continue
            rows = self.conn.execute(
                "SELECT t.slug, t.total_count FROM card_index c "
                "JOIN tag_cards tc ON tc.oracle_id = c.oracle_id AND tc.direct = 1 "
                "JOIN tags t ON t.tag_key = tc.tag_key "
                "WHERE c.name_normalized = ? ORDER BY t.total_count",
                (key,),
            ).fetchall()
            name = self.conn.execute("SELECT name FROM card_index WHERE name_normalized = ?",
                                     (key,)).fetchone()[0]
            out[name if name.lower() == raw.strip().lower() else f"{raw} → {name}"] = \
                [{"slug": r[0], "total_count": r[1]} for r in rows]
        return out

    # -- search -----------------------------------------------------------

    def search_cards(
        self,
        tags_all: Sequence[str] = (),
        tags_any: Sequence[str] = (),
        tags_none: Sequence[str] = (),
        text: Optional[str] = None,
        types: Sequence[str] = (),
        types_none: Sequence[str] = (),
        color_identity: Optional[str] = None,
        mv_min: Optional[float] = None,
        mv_max: Optional[float] = None,
        price_max: Optional[float] = None,
        rarity: Sequence[str] = (),
        exclude: Sequence[str] = (),
        commander_legal: bool = True,
        game_changers: str = "include",
        sort: str = "popular",
        min_edhrec_rank: Optional[int] = None,
        max_edhrec_rank: Optional[int] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> Dict:
        self.require_index(need_tags=bool(tags_all or tags_any or tags_none))
        if sort not in SORTS:
            raise SearchError(f"sort must be one of {', '.join(SORTS)}")
        if game_changers not in ("include", "exclude", "only"):
            raise SearchError("game_changers must be 'include', 'exclude' or 'only'")

        where = ["c.is_card = 1"]
        params: List = []
        notes: List[str] = []

        if commander_legal:
            where.append("c.commander_legal = 1")

        for t in self._require_tags(tags_all):
            where.append("c.oracle_id IN (SELECT oracle_id FROM tag_cards WHERE tag_key = ?)")
            params.append(t["key"])
        if tags_any:
            keys = [t["key"] for t in self._require_tags(tags_any)]
            where.append("c.oracle_id IN (SELECT oracle_id FROM tag_cards WHERE tag_key IN (%s))"
                         % ",".join("?" * len(keys)))
            params += keys
        if tags_none:
            keys = [t["key"] for t in self._require_tags(tags_none)]
            where.append("c.oracle_id NOT IN (SELECT oracle_id FROM tag_cards WHERE tag_key IN (%s))"
                         % ",".join("?" * len(keys)))
            params += keys

        if text and text.strip():
            if si.fts_available(self.conn):
                where.append("c.name_normalized IN (SELECT name_normalized FROM card_fts "
                             "WHERE card_fts MATCH ?)")
                params.append(_fts_query(text))
            else:
                for word in re.findall(r"\w+", text.lower()):
                    where.append("lower(c.oracle_text) LIKE ?")
                    params.append(f"%{word}%")

        for ty in types or []:
            where.append("lower(c.type_line) LIKE ?")
            params.append(f"%{ty.strip().lower()}%")
        for ty in types_none or []:
            where.append("lower(c.type_line) NOT LIKE ?")
            params.append(f"%{ty.strip().lower()}%")

        if color_identity is not None:
            allowed = _parse_identity(color_identity)
            where.append("(c.ci_mask & ?) = 0")
            params.append(31 & ~allowed)

        if mv_min is not None:
            where.append("c.mana_value >= ?"); params.append(mv_min)
        if mv_max is not None:
            where.append("c.mana_value <= ?"); params.append(mv_max)
        if price_max is not None:
            where.append("c.price_eur IS NOT NULL AND c.price_eur <= ?"); params.append(price_max)
            notes.append("Cards without a known EUR price are excluded when price_max is set.")
        if rarity:
            rs = [r.strip().lower() for r in rarity]
            where.append("c.rarity IN (%s)" % ",".join("?" * len(rs))); params += rs

        if game_changers == "exclude":
            where.append("c.game_changer = 0")
        elif game_changers == "only":
            where.append("c.game_changer = 1")

        if exclude:
            resolved = self.resolve_names(exclude)
            keys = [k for k in resolved.values() if k]
            unknown = [n for n, k in resolved.items() if not k]
            if keys:
                where.append("c.name_normalized NOT IN (%s)" % ",".join("?" * len(keys)))
                params += keys
            if unknown:
                notes.append("Could not resolve exclude names: " + ", ".join(unknown[:10]))

        if sort == "deep_cut" and min_edhrec_rank is None:
            min_edhrec_rank = DEFAULT_DEEP_CUT_RANK
            notes.append(f"deep_cut hides the {DEFAULT_DEEP_CUT_RANK} most-played EDHREC cards "
                         f"(override with min_edhrec_rank).")
        if min_edhrec_rank is not None:
            # Unranked cards are as obscure as it gets, so they pass this filter.
            where.append("(c.edhrec_rank IS NULL OR c.edhrec_rank >= ?)"); params.append(min_edhrec_rank)
        if max_edhrec_rank is not None:
            where.append("c.edhrec_rank IS NOT NULL AND c.edhrec_rank <= ?"); params.append(max_edhrec_rank)

        order = {
            # Popular / deep_cut: best-ranked first; deep_cut is "best of the rest".
            "popular": "c.edhrec_rank IS NULL, c.edhrec_rank, c.name",
            "deep_cut": "c.edhrec_rank IS NULL, c.edhrec_rank, c.name",
            "least_popular": "c.edhrec_rank IS NULL, c.edhrec_rank DESC, c.name",
            "price": "c.price_eur IS NULL, c.price_eur, c.edhrec_rank IS NULL, c.edhrec_rank",
            "mana_value": "c.mana_value, c.edhrec_rank IS NULL, c.edhrec_rank",
            "name": "c.name",
            "random": "random()",
        }[sort]

        clause = " AND ".join(where)
        total = self.conn.execute(f"SELECT COUNT(*) FROM card_index c WHERE {clause}", params).fetchone()[0]
        limit = max(1, min(int(limit), MAX_LIMIT))
        rows = self.conn.execute(
            f"SELECT c.* FROM card_index c WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, max(0, int(offset))],
        ).fetchall()
        return {"total": total, "offset": offset, "rows": [dict(r) for r in rows], "notes": notes}

    # -- full card details --------------------------------------------------

    def get_cards_markdown(self, names: Sequence[str], show_price=True, show_rarity=True) -> Dict:
        """Full rules text in the app's standard Markdown table, from the local cache only."""
        deck, missing = [], []
        seen = set()
        for raw in names:
            name = (raw or "").strip()
            if not name:
                continue
            key = self.resolve_names([name]).get(name)
            row = key and self.conn.execute(
                "SELECT data FROM cards WHERE name_normalized = ?", (key,)).fetchone()
            data = json.loads(row[0]) if row else None
            if data is None:
                missing.append(name)
                continue
            if data["name"].lower() in seen:
                continue
            seen.add(data["name"].lower())
            deck.append(Card.from_scryfall_json(data, 1))
        md = format_deck_as_markdown(deck, show_price=show_price, show_rarity=show_rarity) if deck else ""
        return {"markdown": md, "missing": missing}

    # -- deck analysis ------------------------------------------------------

    def analyze_deck(self, names: Sequence[str], extra_tags: Sequence[str] = (), top: int = 25) -> Dict:
        """Count role tags in a decklist and list its most common functional tags."""
        self.require_index(need_tags=True)
        resolved = self.resolve_names(names)
        keys = sorted({k for k in resolved.values() if k})
        unknown = [n for n, k in resolved.items() if not k]
        if not keys:
            raise SearchError("None of the given card names were found in the local database.")

        ph = ",".join("?" * len(keys))
        oracle_ids = [r[0] for r in self.conn.execute(
            f"SELECT oracle_id FROM card_index WHERE name_normalized IN ({ph}) AND oracle_id IS NOT NULL",
            keys)]
        oph = ",".join("?" * len(oracle_ids)) or "NULL"

        def cards_with(tag_key):
            return [r[0] for r in self.conn.execute(
                f"SELECT c.name FROM card_index c JOIN tag_cards tc ON tc.oracle_id = c.oracle_id "
                f"WHERE tc.tag_key = ? AND c.name_normalized IN ({ph}) ORDER BY c.name",
                [tag_key] + keys)]

        roles = []
        wanted = [(s, label) for s, label in CORE_ROLES] + [(t, t) for t in extra_tags]
        for slug, label in wanted:
            t = self.tag(slug)
            if t:
                roles.append({"slug": t["slug"], "label": label, "cards": cards_with(t["key"])})

        common = self.conn.execute(
            f"SELECT t.slug, COUNT(*) AS n FROM tag_cards tc JOIN tags t ON t.tag_key = tc.tag_key "
            f"WHERE tc.direct = 1 AND tc.oracle_id IN ({oph}) GROUP BY t.slug "
            f"HAVING n > 1 ORDER BY n DESC, t.slug",
            oracle_ids,
        ).fetchall()
        common = [r for r in common if not NOISE_TAG.search(r[0])][:top]

        identity = 0
        for r in self.conn.execute(f"SELECT ci_mask FROM card_index WHERE name_normalized IN ({ph})", keys):
            identity |= r[0] or 0

        return {
            "card_count": len(keys), "unknown": unknown, "roles": roles,
            "common_tags": [{"slug": r[0], "count": r[1]} for r in common],
            "color_identity": "".join(c for c, b in si.COLOR_BITS.items() if identity & b) or "C",
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def _stem(word: str) -> str:
    """Very small English stemmer, enough to match 'tokens'~'token', 'sacrificing'~'sacrifice'."""
    w = word.lower()
    for suffix in ("ing", "ers", "es", "er", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def _parse_identity(value: str) -> int:
    v = (value or "").strip().upper()
    if v in ("", "C", "COLORLESS", "COLOURLESS"):
        return 0
    letters = set(v.replace(",", "").replace(" ", "").replace("{", "").replace("}", ""))
    bad = letters - set("WUBRG")
    if bad:
        raise SearchError(f"color_identity must use the letters W, U, B, R, G (or 'C'); got '{value}'")
    return si.ci_mask(letters)


def _fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: words are ANDed, "quoted phrases" kept,
    -word excludes, and 'OR' between words is honoured."""
    tokens = re.findall(r'-?"[^"]+"|\S+', text)
    parts, negatives = [], []
    for tok in tokens:
        if tok.upper() == "OR" and parts:
            parts.append("OR")
            continue
        neg = tok.startswith("-")
        tok = tok.lstrip("-")
        phrase = tok.strip('"')
        clean = re.sub(r'[^\w\s\']', " ", phrase).strip()
        if not clean:
            continue
        quoted = '"' + clean.replace('"', "") + '"'
        (negatives if neg else parts).append(quoted)
    while parts and parts[-1] == "OR":
        parts.pop()
    query = " ".join(parts) if parts else '""'
    for n in negatives:
        query = f"({query}) NOT {n}" if parts else query
    return query
