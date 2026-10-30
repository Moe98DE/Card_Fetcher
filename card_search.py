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
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from pydantic import ValidationError

import search_index as si
from formatter import format_deck_as_markdown
from models import Card
from card_filters import CardFilters, color_clause, compile_filters, card_color_mask, numeric_stat

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


BASIC_LANDS = {"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"}
CARD_TYPES = ("Creature", "Instant", "Sorcery", "Artifact", "Enchantment", "Planeswalker", "Battle", "Land")


class SearchError(ValueError):
    """A problem with the query that the caller (or model) can fix."""


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

class CardSearch:
    def __init__(self, conn: Optional[sqlite3.Connection] = None,
                 collections_file: Optional[str] = None):
        self.conn = conn or si.connect(read_only=True)
        self._collections_file = collections_file
        self._collection_store = None
        # Loaded collections: name -> {"token", "cards": {key: qty}, "tag_counts"}.
        self._scopes: Dict[str, Dict] = {}
        self._tags_cache: Optional[List[Dict]] = None
        self._names_cache: Optional[Dict[str, str]] = None
        self._edhrec_client = None
        self._formats_cache = None
        self.conn.create_function("card_color_mask", 1, card_color_mask, deterministic=True)
        self.conn.create_function("numeric_stat", 1, numeric_stat, deterministic=True)

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

    def find_tags(self, query: str, limit: int = 20, collection: Optional[str] = None) -> List[Dict]:
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
        found = [t for _, t in scored[:max(1, min(limit, MAX_LIMIT))]]
        if collection:
            counts = self._collection_tag_counts(collection)
            found = [{**t, "owned_count": counts.get(t["key"], 0)} for t in found]
        return found

    def list_tags(self, parent: Optional[str] = None, prefix: Optional[str] = None,
                  min_cards: int = 0, offset: int = 0, limit: int = 100,
                  collection: Optional[str] = None) -> Dict:
        """Browse the tag catalogue: roots, children of a tag, or slugs by prefix.
        With a collection, only tags it has cards for, counted and ordered by those cards."""
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
        if collection:
            counts = self._collection_tag_counts(collection)
            tags = [{**t, "owned_count": counts.get(t["key"], 0)} for t in tags]
            tags = [t for t in tags if t["owned_count"] >= max(1, min_cards)]
            tags.sort(key=lambda t: (-t["owned_count"], t["slug"]))
        else:
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

    def _card_predicates(
        self, *, tags_all=(), tags_any=(), tags_none=(), text=None,
        types=(), types_none=(), color_identity=None, mv_min=None, mv_max=None,
        price_max=None, rarity=(), exclude=(), commander_legal=True,
        game_changers="include", filters=None, collection=None,
    ):
        """One predicate builder for local search and EDHREC candidate pools."""
        self.require_index(need_tags=bool(tags_all or tags_any or tags_none))
        try:
            filters = CardFilters.model_validate(filters or {})
            if filters.color_identity_match != "subset" and color_identity is None:
                raise ValueError("filters.color_identity_match requires color_identity.")
            if filters.price_known is False and price_max is not None:
                raise ValueError("price_known=false cannot be combined with price_max.")
            for label, low, high in (("mana value", mv_min, mv_max),
                                     ("EUR price", filters.price_min, price_max)):
                _validate_bounds(label, low, high)
            if filters.legalities:
                if self._formats_cache is None:
                    self._formats_cache = {r[0] for r in self.conn.execute(
                        "SELECT DISTINCT f.key FROM cards d, json_each(d.data, '$.legalities') f")}
                unknown = set(filters.legalities) - self._formats_cache
                if unknown:
                    raise ValueError("Unknown format(s): " + ", ".join(sorted(unknown))
                                     + ". Available in this database: " + ", ".join(sorted(self._formats_cache)))
            extra, extra_params, needs_data = compile_filters(filters)
            # Validate identity even when no JSON filters are requested.
            if color_identity is not None:
                color_clause("c.ci_mask", color_identity, filters.color_identity_match)
        except (ValidationError, ValueError) as err:
            raise SearchError(str(err)) from err
        if game_changers not in ("include", "exclude", "only"):
            raise SearchError("game_changers must be 'include', 'exclude' or 'only'")
        where = ["c.is_card = 1"]
        params: List = []
        notes: List[str] = []

        if collection:
            clause, values = self._collection_clause(collection)
            where.append(clause)
            params += values

        if commander_legal and "commander" not in filters.legalities:
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
            clause, values = color_clause("c.ci_mask", color_identity, filters.color_identity_match)
            where.append(clause)
            params.extend(values)

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

        where.extend(extra)
        params.extend(extra_params)
        if filters.price_min is not None and price_max is None:
            notes.append("Cards without a known EUR price are excluded when price_min is set.")
        source = "card_index c" + (" JOIN cards d ON d.name_normalized = c.name_normalized" if needs_data else "")
        return where, params, notes, source

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
        filters: Optional[CardFilters] = None,
        sort_reverse: bool = False,
        collection: Optional[str] = None,
    ) -> Dict:
        if sort not in SORTS:
            raise SearchError(f"sort must be one of {', '.join(SORTS)}")
        where, params, notes, source = self._card_predicates(
            tags_all=tags_all, tags_any=tags_any, tags_none=tags_none, text=text,
            types=types, types_none=types_none, color_identity=color_identity,
            mv_min=mv_min, mv_max=mv_max, price_max=price_max, rarity=rarity,
            exclude=exclude, commander_legal=commander_legal, game_changers=game_changers, filters=filters,
            collection=collection,
        )
        _validate_bounds("EDHREC rank", min_edhrec_rank, max_edhrec_rank, minimum=1)

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
            "mana_value": "c.mana_value IS NULL, c.mana_value, c.edhrec_rank IS NULL, c.edhrec_rank",
            "name": "c.name",
            "random": "random()",
        }[sort]

        if sort_reverse:
            if sort not in ("price", "mana_value", "name"):
                raise SearchError("sort_reverse is supported for price, mana_value and name. Use least_popular to reverse popularity.")
            order = {
                "price": "c.price_eur IS NULL, c.price_eur DESC, c.name",
                "mana_value": "c.mana_value IS NULL, c.mana_value DESC, c.name",
                "name": "c.name DESC",
            }[sort]
        # A name tie-break keeps pages stable when ranks, prices or mana values tie.
        if sort in ("price", "mana_value") and not sort_reverse:
            order += ", c.name"
        clause = " AND ".join(where)
        total = self.conn.execute(f"SELECT COUNT(*) FROM {source} WHERE {clause}", params).fetchone()[0]
        limit = max(1, min(int(limit), MAX_LIMIT))
        rows = self.conn.execute(
            f"SELECT c.* FROM {source} WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [limit, max(0, int(offset))],
        ).fetchall()
        rows = [dict(r) for r in rows]
        if collection:
            self._add_owned(rows, collection)
        return {"total": total, "offset": max(0, int(offset)), "rows": rows, "notes": notes}

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

    def analyze_deck(self, names: Sequence[str] = (), extra_tags: Sequence[str] = (), top: int = 25,
                     collection: Optional[str] = None) -> Dict:
        """Count role tags in a decklist (or a whole collection) and list its most common functional tags."""
        self.require_index(need_tags=True)
        if collection:
            # A collection can hold thousands of cards: match through its temp table.
            members, keys_params = self._collection_clause(collection, column="name_normalized")
            unknown = []
            count = len(self._collection(collection)["cards"])
        else:
            resolved = self.resolve_names(names)
            keys = sorted({k for k in resolved.values() if k})
            unknown = [n for n, k in resolved.items() if not k]
            members = "name_normalized IN (%s)" % ",".join("?" * len(keys))
            keys_params, count = keys, len(keys)
        if not count:
            raise SearchError("None of the given card names were found in the local database.")

        oracle_ids = (f"SELECT oracle_id FROM card_index WHERE {members} AND oracle_id IS NOT NULL")

        def cards_with(tag_key):
            return [r[0] for r in self.conn.execute(
                f"SELECT c.name FROM card_index c JOIN tag_cards tc ON tc.oracle_id = c.oracle_id "
                f"WHERE tc.tag_key = ? AND c.{members} ORDER BY c.name",
                [tag_key] + keys_params)]

        roles = []
        wanted = [(s, label) for s, label in CORE_ROLES] + [(t, t) for t in extra_tags]
        for slug, label in wanted:
            t = self.tag(slug)
            if t:
                roles.append({"slug": t["slug"], "label": label, "cards": cards_with(t["key"])})

        common = self.conn.execute(
            f"SELECT t.slug, COUNT(*) AS n FROM tag_cards tc JOIN tags t ON t.tag_key = tc.tag_key "
            f"WHERE tc.direct = 1 AND tc.oracle_id IN ({oracle_ids}) GROUP BY t.slug "
            f"HAVING n > 1 ORDER BY n DESC, t.slug",
            keys_params,
        ).fetchall()
        common = [r for r in common if not NOISE_TAG.search(r[0])][:top]

        identity = 0
        for r in self.conn.execute(f"SELECT ci_mask FROM card_index WHERE {members}", keys_params):
            identity |= r[0] or 0

        return {
            "card_count": count, "unknown": unknown, "roles": roles,
            "common_tags": [{"slug": r[0], "count": r[1]} for r in common],
            "color_identity": "".join(c for c, b in si.COLOR_BITS.items() if identity & b) or "C",
        }


    # -- EDHREC ---------------------------------------------------------------

    def _edhrec(self):
        if self._edhrec_client is None:
            from edhrec import EdhrecClient
            self._edhrec_client = EdhrecClient()
        return self._edhrec_client

    def resolve_commander(self, name: str) -> Dict:
        """Exact card name, single face, or a loose name like 'korvold' -> canonical name."""
        name = (name or "").strip()
        if not name:
            raise SearchError("No commander name given.")
        key = self.resolve_names([name]).get(name)
        if key:
            row = self.conn.execute("SELECT name FROM card_index WHERE name_normalized = ?", (key,)).fetchone()
            return {"name": row[0], "interpreted": False}
        words = [w for w in re.findall(r"[a-z0-9']+", name.lower()) if len(w) > 1]
        if words:
            where = " AND ".join("name_normalized LIKE ?" for _ in words)
            row = self.conn.execute(
                f"SELECT name FROM card_index WHERE is_card = 1 AND {where} AND "
                f"(type_line LIKE '%Legendary%' OR oracle_text LIKE '%can be your commander%') "
                f"ORDER BY edhrec_rank IS NULL, edhrec_rank LIMIT 1",
                [f"%{w}%" for w in words],
            ).fetchone()
            if row:
                return {"name": row[0], "interpreted": True}
        sugg = self.suggest_names(name)
        raise SearchError(f"Commander '{name}' not found locally."
                          + (f" Did you mean: {', '.join(sugg)}?" if sugg else ""))

    def edhrec_recommendations(
        self,
        commander: str,
        partner: Optional[str] = None,
        theme: Optional[str] = None,
        bracket: Optional[str] = None,
        budget: Optional[str] = None,
        sections: Sequence[str] = (),
        types: Sequence[str] = (),
        types_none: Sequence[str] = (),
        tags_all: Sequence[str] = (),
        tags_any: Sequence[str] = (),
        tags_none: Sequence[str] = (),
        price_max: Optional[float] = None,
        mv_min: Optional[float] = None,
        mv_max: Optional[float] = None,
        exclude: Sequence[str] = (),
        min_inclusion: Optional[float] = None,
        min_synergy: Optional[float] = None,
        include_lands: bool = True,
        game_changers: str = "include",
        sort: str = "inclusion",
        limit: int = 40,
        offset: int = 0,
        refresh: bool = False,
        filters: Optional[CardFilters] = None,
        color_identity: Optional[str] = None,
        commander_legal: bool = True,
        sort_reverse: bool = False,
        all_pages: bool = False,
        collection: Optional[str] = None,
    ) -> Dict:
        """EDHREC's recommended pool for a commander, joined with local card data and filtered."""
        from edhrec import EdhrecError

        sorts = ("inclusion", "synergy", "lift", "trend", "price", "mana_value", "name")
        if sort not in sorts:
            raise SearchError(f"sort must be one of {', '.join(sorts)}")

        where, params, notes, source = self._card_predicates(
            tags_all=tags_all, tags_any=tags_any, tags_none=tags_none,
            types=types, types_none=types_none, color_identity=color_identity,
            mv_min=mv_min, mv_max=mv_max, price_max=price_max, exclude=exclude,
            commander_legal=commander_legal, game_changers=game_changers, filters=filters,
            collection=collection,
        )
        commanders = []
        for raw in [commander, partner]:
            if raw:
                res = self.resolve_commander(raw)
                if res["interpreted"]:
                    notes.append(f"Interpreted '{raw}' as {res['name']}.")
                commanders.append(res["name"])
        try:
            page = self._edhrec().commander_page(commanders, theme=theme, bracket=bracket,
                                                 budget=budget, refresh=refresh)
        except EdhrecError as err:
            raise SearchError(str(err)) from err
        if page.get("stale"):
            notes.append("EDHREC was unreachable; showing an older cached copy.")

        # Join with the local index.
        names = [c["name"] for c in page["cards"]]
        resolved = self.resolve_names(names)
        keys = sorted({k for k in resolved.values() if k})
        local = {}
        # Batch candidate IDs to stay below older SQLite parameter limits.
        for start in range(0, len(keys), 400):
            batch = keys[start:start + 400]
            ph = ",".join("?" * len(batch))
            clause = " AND ".join(where + [f"c.name_normalized IN ({ph})"])
            for r in self.conn.execute(f"SELECT c.* FROM {source} WHERE {clause}", params + batch):
                local[r["name_normalized"]] = dict(r)
        unmatched = [n for n, k in resolved.items() if not k]

        commander_keys = {k for k in self.resolve_names(commanders).values() if k}
        sec_filter = [x.lower() for x in sections or []]

        rows = []
        for card in page["cards"]:
            key = resolved.get(card["name"])
            info = local.get(key) if key else None
            if info is None or key in commander_keys:
                continue
            tl = (info["type_line"] or "").lower()
            if "basic" in tl and "land" in tl:
                continue
            if not include_lands and "land" in tl.split("//")[0]:
                continue
            labels = [card["section"]] + card["labels"]
            if sec_filter and not any(f in l.lower() for f in sec_filter for l in labels):
                continue
            incl = (card["inclusion"] or 0) * 100
            if min_inclusion is not None and incl < min_inclusion:
                continue
            syn = (card["synergy"] or 0) * 100
            if min_synergy is not None and syn < min_synergy:
                continue
            rows.append({**info, "section": card["section"], "labels": card["labels"],
                         "inclusion": incl, "synergy": syn, "lift": card["lift"],
                         "trend": card["trend"], "num_decks": card["num_decks"]})

        keyf = {
            "inclusion": lambda r: -r["inclusion"],
            "synergy": lambda r: -r["synergy"],
            "lift": lambda r: -(r["lift"] or 0),
            "trend": lambda r: -(r["trend"] or 0),
            "price": lambda r: (r["price_eur"] is None, r["price_eur"] or 0),
            "mana_value": lambda r: (r["mana_value"] or 0, -r["inclusion"]),
            "name": lambda r: r["name"],
        }[sort]
        # Keep unknown prices/mana values last in either direction.
        rows.sort(key=lambda r: r["name"])
        rows.sort(key=keyf, reverse=sort_reverse)
        if sort in ("price", "mana_value"):
            column = "price_eur" if sort == "price" else "mana_value"
            rows.sort(key=lambda r: r[column] is None)
        if unmatched:
            notes.append(f"{len(unmatched)} EDHREC card(s) not in the local database "
                         f"(update the local DB): " + ", ".join(unmatched[:8]))
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        # MCP callers can request the complete filtered pool in one response.
        # Keep pagination as the default for non-MCP callers.
        selected = rows if all_pages else rows[offset:offset + limit]
        if collection:
            self._add_owned(selected, collection)
        return {"page": page, "commanders": commanders, "total": len(rows),
                "offset": 0 if all_pages else offset, "rows": selected, "notes": notes}

    def edhrec_average_deck(self, commander: str, partner: Optional[str] = None,
                            theme: Optional[str] = None, bracket: Optional[str] = None,
                            budget: Optional[str] = None, refresh: bool = False,
                            collection: Optional[str] = None) -> Dict:
        from edhrec import EdhrecError

        owned = self._collection(collection)["cards"] if collection else None

        commanders, notes = [], []
        for raw in [commander, partner]:
            if raw:
                res = self.resolve_commander(raw)
                if res["interpreted"]:
                    notes.append(f"Interpreted '{raw}' as {res['name']}.")
                commanders.append(res["name"])
        try:
            deck = self._edhrec().average_deck(commanders, theme=theme, bracket=bracket,
                                               budget=budget, refresh=refresh)
        except EdhrecError as err:
            raise SearchError(str(err)) from err

        total_price, unpriced, count = 0.0, [], 0
        priced_sections = {}
        for section, entries in deck["sections"].items():
            out = []
            for name, qty in entries:
                key = self.resolve_names([name]).get(name)
                row = key and self.conn.execute(
                    "SELECT price_eur, mana_value FROM card_index WHERE name_normalized = ?", (key,)).fetchone()
                price = row[0] if row else None
                tl_basic = section.lower() == "land" and name in ("Plains", "Island", "Swamp", "Mountain",
                                                                    "Forest", "Wastes")
                if price is not None:
                    total_price += price * qty
                elif not tl_basic:
                    unpriced.append(name)
                count += qty
                entry = {"name": name, "qty": qty, "price_eur": price}
                if owned is not None:
                    entry["owned"] = owned.get(key, 0) if key else 0
                out.append(entry)
            priced_sections[section] = out
        deck.update(sections=priced_sections, total_price=total_price, unpriced=unpriced,
                    card_count=count, notes=notes)
        if owned is not None:
            entries = [e for cards in priced_sections.values() for e in cards
                       if e["name"] not in BASIC_LANDS]
            missing = [e for e in entries if e["owned"] < e["qty"]]
            deck["collection"] = {
                "name": self._collection(collection)["name"],
                "owned": sum(min(e["owned"], e["qty"]) for e in entries),
                "needed": sum(e["qty"] for e in entries),
                "missing": [e["name"] for e in missing],
                "missing_price": sum((e["price_eur"] or 0) * (e["qty"] - e["owned"]) for e in missing),
            }
        return deck

    # -- collections ----------------------------------------------------------

    def collections(self):
        """The collection store (created next to the card database on first use)."""
        if self._collection_store is None:
            from collection import CollectionStore, COLLECTIONS_FILE
            self._collection_store = CollectionStore(self._collections_file or COLLECTIONS_FILE)
        return self._collection_store

    def _collection(self, name: str) -> Dict:
        """Load a collection into this connection's temp table, refreshing it when it changed."""
        from collection import CollectionError
        try:
            info = self.collections().require(name)
        except CollectionError as err:
            raise SearchError(str(err)) from err
        scope = self._scopes.get(info["name"])
        if scope is None or scope["token"] != info["updated_at"]:
            cards = self.collections().cards(info["name"])
            # TEMP tables work on read-only connections and avoid SQLite's
            # parameter limit for collections with thousands of cards.
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS collection_scope ("
                              "collection TEXT NOT NULL, name_normalized TEXT NOT NULL, "
                              "PRIMARY KEY (collection, name_normalized)) WITHOUT ROWID")
            with self.conn:
                self.conn.execute("DELETE FROM temp.collection_scope WHERE collection = ?", (info["name"],))
                self.conn.executemany("INSERT INTO temp.collection_scope VALUES (?, ?)",
                                      [(info["name"], k) for k in cards])
            scope = {"token": info["updated_at"], "cards": cards, "tag_counts": None}
            self._scopes[info["name"]] = scope
        return {"name": info["name"], **scope}

    def _collection_clause(self, name: str, column: str = "c.name_normalized"):
        canonical = self._collection(name)["name"]
        return (f"{column} IN (SELECT name_normalized FROM temp.collection_scope "
                f"WHERE collection = ?)", [canonical])

    def _add_owned(self, rows: List[Dict], name: str) -> None:
        cards = self._collection(name)["cards"]
        for r in rows:
            r["owned"] = cards.get(r["name_normalized"], 0)

    def _collection_tag_counts(self, name: str) -> Dict[int, int]:
        """Distinct cards of a collection per tag (child tags included, like total_count)."""
        self.require_index(need_tags=True)
        scope = self._collection(name)
        if scope["tag_counts"] is None:
            scope["tag_counts"] = dict(self.conn.execute(
                "SELECT tc.tag_key, COUNT(DISTINCT c.oracle_id) FROM temp.collection_scope s "
                "JOIN card_index c ON c.name_normalized = s.name_normalized AND c.is_card = 1 "
                "JOIN tag_cards tc ON tc.oracle_id = c.oracle_id "
                "WHERE s.collection = ? GROUP BY tc.tag_key", (scope["name"],)).fetchall())
            self._scopes[scope["name"]]["tag_counts"] = scope["tag_counts"]
        return scope["tag_counts"]

    def import_collection(self, name: str, text: Optional[str] = None, path: Optional[str] = None,
                          mode: str = "replace") -> Dict:
        """Parse a collection export, resolve names locally and store it under a name."""
        from collection import CollectionError, parse_collection, read_collection_file
        self.require_index()
        if bool(text and text.strip()) == bool(path and path.strip()):
            raise SearchError("Give exactly one of path (a text/CSV file) or text (the pasted list).")
        try:
            source = os.path.abspath(os.path.expanduser(path)) if path else "pasted text"
            parsed = parse_collection(read_collection_file(path) if path else text)
            if not parsed.cards:
                raise CollectionError("No card names found. Expected lines like '2 Sol Ring' "
                                      "or a CSV export with a Name column.")
            resolved = self.resolve_names([c["name"] for c in parsed.cards])
            cards: Dict[str, int] = {}
            unmatched = [{"name": s["line"], "quantity": None, "reason": s["reason"]}
                         for s in parsed.skipped]
            for entry in parsed.cards:
                key = resolved.get(entry["name"])
                if key:
                    cards[key] = cards.get(key, 0) + entry["quantity"]
                else:
                    unmatched.append({"name": entry["name"], "quantity": entry["quantity"],
                                      "reason": "not in the local card database"})
            info = self.collections().save(name, cards, unmatched, source, mode=mode)
        except CollectionError as err:
            raise SearchError(str(err)) from err
        return {**self.collection_info(info["name"]), "mode": mode,
                "imported_unique": len(cards), "imported_copies": sum(cards.values()),
                "imported_unmatched": unmatched}

    def list_collections(self) -> List[Dict]:
        return self.collections().list()

    def delete_collection(self, name: str) -> str:
        from collection import CollectionError
        try:
            deleted = self.collections().delete(name)
        except CollectionError as err:
            raise SearchError(str(err)) from err
        if self._scopes.pop(deleted, None) is not None:
            with self.conn:
                self.conn.execute("DELETE FROM temp.collection_scope WHERE collection = ?", (deleted,))
        return deleted

    def collection_info(self, name: str) -> Dict:
        """Size, value, colour identity / card type spread and unmatched lines of a collection."""
        scope = self._collection(name)
        info = self.collections().get(scope["name"])
        cards = scope["cards"]
        rows = self.conn.execute(
            "SELECT c.name_normalized, c.color_identity, c.type_line, c.price_eur, c.commander_legal "
            "FROM temp.collection_scope s JOIN card_index c ON c.name_normalized = s.name_normalized "
            "WHERE s.collection = ?", (scope["name"],)).fetchall()
        by_identity: Dict[str, int] = {}
        by_type: Dict[str, int] = {}
        value, unpriced, commanders = 0.0, 0, 0
        for r in rows:
            identity = r["color_identity"] or "C"
            by_identity[identity] = by_identity.get(identity, 0) + 1
            front = (r["type_line"] or "").split("//")[0].lower()
            for card_type in CARD_TYPES:
                if card_type.lower() in front:
                    by_type[card_type] = by_type.get(card_type, 0) + 1
            if "legendary" in front and "creature" in front and r["commander_legal"]:
                commanders += 1
            if r["price_eur"] is None:
                unpriced += 1
            else:
                value += r["price_eur"] * cards.get(r["name_normalized"], 1)
        order = {c: i for i, c in enumerate("WUBRG")}
        return {
            "name": scope["name"], "source": info["source"], "updated_at": info["updated_at"],
            "unique_cards": len(cards), "copies": sum(cards.values()),
            "value_eur": value, "unpriced": unpriced, "legendary_creatures": commanders,
            "by_identity": sorted(by_identity.items(),
                                  key=lambda x: (x[0] != "C", len(x[0]), [order.get(c, 9) for c in x[0]])),
            "by_type": [(t, by_type[t]) for t in CARD_TYPES if t in by_type],
            "unmatched": self.collections().unmatched(scope["name"]),
        }


    # -- Comprehensive Rules (read-only; downloading lives in rules.py) -------

    def rules_status(self) -> Dict:
        meta = si.get_meta(self.conn)
        return {k: meta.get(k) for k in ("rules_effective", "rules_count", "glossary_count",
                                         "rules_updated_at", "rules_file")}

    def _require_rules(self) -> None:
        try:
            n = self.conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        except sqlite3.OperationalError:
            n = 0
        if not n:
            raise SearchError("The Comprehensive Rules are not installed. Open MTG Deck Formatter "
                              "and click 'Update Rules'.")

    def rules_lookup(self, ref: str, max_children: int = 200) -> Dict:
        """A rule with all its subrules, a section's rule list, a chapter's sections,
        or a rule found by its title (e.g. 'Trample', 'Ward')."""
        self._require_rules()
        ref = (ref or "").strip().rstrip(".")
        row = self.conn.execute("SELECT number, kind, parent, text FROM rules WHERE number = ?",
                                (ref.lower() if re.match(r"^\d", ref) else ref,)).fetchone()
        if row is None and not re.match(r"^\d", ref):
            # By title: keyword/section names are the whole text of rules like '702.19. Trample'.
            row = self.conn.execute(
                "SELECT number, kind, parent, text FROM rules WHERE kind IN ('chapter','section','rule') "
                "AND lower(text) = lower(?) ORDER BY ord LIMIT 1", (ref,)).fetchone()
        if row is None:
            raise SearchError(f"No rule '{ref}'. Use rules_search to find the right rule number.")

        entry = dict(row)
        children = [dict(r) for r in self.conn.execute(
            "SELECT number, kind, text FROM rules WHERE parent = ? ORDER BY ord", (entry["number"],))]
        # Subrules of a rule are shown in full; a section/chapter lists its children.
        full_children = entry["kind"] in ("rule", "subrule")
        breadcrumb = []
        parent = entry["parent"]
        while parent:
            p = self.conn.execute("SELECT number, text, parent FROM rules WHERE number = ?", (parent,)).fetchone()
            if not p:
                break
            breadcrumb.insert(0, f"{p[0]}. {p[1].splitlines()[0]}")
            parent = p[2]
        return {"entry": entry, "children": children[:max_children], "more": max(0, len(children) - max_children),
                "full_children": full_children, "breadcrumb": breadcrumb}

    def rules_search(self, query: str, limit: int = 15, include_glossary: bool = True) -> List[Dict]:
        self._require_rules()
        limit = max(1, min(limit, 50))
        if si.get_meta(self.conn).get("rules_fts") == "1":
            where = "" if include_glossary else "AND kind != 'glossary'"
            sql = (f"SELECT number, kind, text FROM rules_fts WHERE rules_fts MATCH ? {where} "
                   f"ORDER BY bm25(rules_fts) LIMIT ?")
            rows = self.conn.execute(sql, (_fts_query(query), limit)).fetchall()
            if not rows:
                # Nothing contains every term: rank rules containing any of them instead.
                terms = [t for t in re.findall(r'"[^"]+"|[\w\']+', query) if t.upper() not in ("OR", "AND")]
                loose = " OR ".join('"' + re.sub(r'["]', "", t) + '"' for t in terms)
                if loose:
                    rows = self.conn.execute(sql, (loose, limit)).fetchall()
        else:
            words = re.findall(r"\w+", query.lower())
            cond = " AND ".join("lower(text) LIKE ?" for _ in words) or "1"
            rows = self.conn.execute(f"SELECT number, kind, text FROM rules WHERE {cond} ORDER BY ord LIMIT ?",
                                     [f"%{w}%" for w in words] + [limit]).fetchall()
        return [{"number": r[0], "kind": r[1], "text": r[2]} for r in rows]

    def rules_glossary(self, term: str, limit: int = 5) -> List[Dict]:
        self._require_rules()
        t = (term or "").strip()
        rows = self.conn.execute("SELECT term, definition FROM glossary WHERE lower(term) = lower(?)",
                                 (t,)).fetchall()
        if not rows:
            rows = self.conn.execute(
                "SELECT term, definition FROM glossary WHERE lower(term) LIKE lower(?) ORDER BY length(term) LIMIT ?",
                (f"%{t}%", limit)).fetchall()
        if not rows:
            terms = [r[0] for r in self.conn.execute("SELECT term FROM glossary")]
            close = difflib.get_close_matches(t, terms, n=limit, cutoff=0.6)
            rows = [self.conn.execute("SELECT term, definition FROM glossary WHERE term = ?", (c,)).fetchone()
                    for c in close]
        return [{"term": r[0], "definition": r[1]} for r in rows if r]

    def rules_text(self) -> str:
        path = self.rules_status().get("rules_file") or si.RULES_TEXT_FILE
        if not path or not os.path.exists(path):
            raise SearchError("The Comprehensive Rules file is not installed. Click 'Update Rules' in the app.")
        with open(path, encoding="utf-8") as fh:
            return fh.read()


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


def _validate_bounds(label, low, high, minimum=0):
    for value in (low, high):
        if value is not None and (not math.isfinite(value) or value < minimum):
            raise SearchError(f"{label} bounds must be finite and at least {minimum}.")
    if low is not None and high is not None and low > high:
        raise SearchError(f"Minimum {label} must be less than or equal to maximum {label}.")


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
