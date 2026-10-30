# mcp_server.py
"""
MCP stdio server for OpenAI Codex, Claude Desktop and other MCP clients.
Card searches use the local Scryfall cache; EDHREC tools can fetch recommendations.

It exposes read-only tools over the same SQLite database the GUI uses:
tag discovery (Scryfall Tagger oracle tags), filtered card search with EDHREC
popularity / deep-cut ordering, per-card tags, deck role analysis, and full
card details in the app's standard Markdown table.

Run by Claude Desktop via claude_desktop_config.json, e.g.:

    "mtg-cards": {
      "command": "/path/to/venv/python",
      "args": ["/path/to/Card_Fetcher/mcp_server.py"]
    }

The GUI's "Codex Setup" and "Claude Setup" buttons generate client-specific
configuration for the same server. No client-specific protocol mode is needed.
"""
import argparse
import os
import sys
from typing import Annotated, List, Literal, Optional

from pydantic import Field

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

try:
    from mcp.types import ToolAnnotations
    READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
    EDHREC_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
    # Collections are the only data this server writes (to collections.db).
    COLLECTION_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
    COLLECTION_DELETE = ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                        idempotentHint=True, openWorldHint=False)
except ImportError:  # very old SDKs
    READ_ONLY = EDHREC_READ_ONLY = COLLECTION_WRITE = COLLECTION_DELETE = None

import search_index as si
from card_search import CardSearch, SearchError, DEFAULT_DEEP_CUT_RANK, MAX_LIMIT
from card_filters import CardFilters

INSTRUCTIONS = f"""\
Magic: The Gathering card search over the user's local Scryfall database
(Commander-focused). Card and rules searches are offline. EDHREC tools may
fetch public recommendations and cache them locally. Prefer these tools for
card and rules questions.

Starting from a commander: edhrec_recommendations returns EDHREC's pool of
recommended cards for it (inclusion % = share of that commander's decks
running the card, synergy = how much more than in other decks of those
colours, lift = how unusually strongly it pairs with this commander). It
accepts the same tag / budget / curve / type filters as search_cards, plus an
EDHREC theme, power bracket or budget tier. By default it returns the complete
filtered pool with untruncated Oracle text (all_pages=true, full_text=true).
Pick from this pool according to
the task (theme, budget, bracket) rather than copying the top of the list:
sort="lift" or "synergy" surfaces commander-specific picks; sort="inclusion"
is the generic consensus. edhrec_average_deck gives a baseline 99. Combine
with search_cards(sort="deep_cut") for cards EDHREC users rarely play.

Suggested workflow when improving a deck:
1. analyze_deck with the current list to see colour identity, role counts
   (ramp, draw, removal, wipes, ...) and the deck's recurring themes.
2. find_tags to turn a need ("sac outlet", "token doubler", "graveyard hate")
   into exact Scryfall Tagger slugs. Never guess slugs; use find_tags/list_tags.
   card_tags on a key card shows the tags of cards you already like.
3. search_cards with those tags plus color_identity (the commander's identity),
   exclude (the current decklist), and budget/curve filters.
   sort="popular" = EDHREC staples first; sort="deep_cut" hides the
   {DEFAULT_DEEP_CUT_RANK} most-played cards and shows the best of the rest,
   which is how you find non-generic picks. Text search (text=...) works when
   no tag fits.
4. get_cards for the full rules text of finalists before recommending them.
Rules questions: the Magic Comprehensive Rules are stored locally. Use
rules_lookup for a rule number or keyword name (e.g. '702.19' or 'Ward'),
rules_search to find which rules cover something, and rules_glossary for
terms. Cite rule numbers. The full text is also the resource
rules://comprehensive-rules. You cannot download rules; if they are missing or
outdated, tell the user to click 'Update Rules' in MTG Deck Formatter.

Queries: combine the existing top-level tag, text, type, color_identity, mv_min,
mv_max and price_max arguments. Optional filters adds exact/contains/overlap
identity matching, actual card colors, per-format legalities, price_min and
price_known, field-specific literal text, types_any, exact keywords, layouts,
and numeric face stats. All filters are ANDed. Use filters.legalities for
legal/banned/restricted/not_legal in any format recorded in the cache; an explicit
Commander status overrides commander_legal. For non-Commander searches, pass
commander_legal=false. Unknown prices fail price bounds; missing/variable stats
fail numeric ranges. Color matching uses WUBRG letters or C. No Scryfall query
language is parsed; use the named arguments. The same filters object works in
edhrec_recommendations.

Collections (cards the user owns): import_collection reads a text or CSV
export from a file path (preferred for large collections) or pasted text and
stores it under a name. Pass collection="<name>" to search_cards,
edhrec_recommendations, analyze_deck, find_tags and list_tags to work only
with owned cards; every other argument (tags, text, filters, sorts) works the
same, and results show how many copies are owned. Examples: "ramp in BG I
own" = search_cards(collection=..., tags_any=["ramp"], color_identity="BG");
specific wording = text='"enters the battlefield" token' (word/phrase search
over name, type and rules text) or filters.oracle_contains="+1/+1 counter"
(literal rules-text substring). edhrec_recommendations(collection=...) gives
EDHREC's picks for a commander that the user already owns;
edhrec_average_deck(collection=...) shows what is missing and its cost.
collection_info gives an overview; list_collections names the stored ones.

Prices are the cheapest regular paper EUR price across printings, from the
last database update. EDHREC pages are cached locally for 7 days.
"""

mcp = _Server("mtg-cards", instructions=INSTRUCTIONS)
_search: Optional[CardSearch] = None


def engine() -> CardSearch:
    global _search
    if _search is None:
        if not os.path.exists(_DB_FILE):
            raise SearchError("No local card database yet. Open MTG Deck Formatter and click "
                              "'Update Local DB'.")
        _search = CardSearch(si.connect(_DB_FILE, read_only=True),
                             collections_file=os.path.join(os.path.dirname(os.path.abspath(_DB_FILE)),
                                                           "collections.db"))
    return _search


def _tool(**kw):
    if READ_ONLY is not None:
        kw.setdefault("annotations", READ_ONLY)
    else:
        kw.pop("annotations", None)
    return mcp.tool(**kw)


def _guard(fn):
    """Turn query mistakes into readable messages the model can act on."""
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except SearchError as err:
            return f"Error: {err}"
        except Exception as err:  # e.g. database missing or locked
            return f"Error: {type(err).__name__}: {err}"
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    wrapper.__annotations__ = fn.__annotations__
    wrapper.__wrapped__ = fn
    return wrapper


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _cell(text, limit: Optional[int] = None) -> str:
    s = " / ".join(line.strip() for line in str(text or "").splitlines() if line.strip())
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s.replace("|", "\\|")


def _price(value) -> str:
    return f"€{value:.2f}" if value is not None else "—"


def _tag_lines(tags) -> str:
    lines = []
    for t in tags:
        desc = _cell(t["description"], 140)
        owned = f"; {t['owned_count']} in collection" if "owned_count" in t else ""
        lines.append(f"- `{t['slug']}` ({t['total_count']} cards{owned})" + (f": {desc}" if desc else ""))
        if t["children"]:
            kids = ", ".join(t["children"][:12]) + (" …" if len(t["children"]) > 12 else "")
            lines.append(f"  - narrower: {kids}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

CollectionArg = Annotated[Optional[str], Field(description=(
    "Name of an imported collection (see list_collections). Only cards in it are considered."))]

@_tool()
@_guard
def database_status() -> str:
    """Show whether the local card database, search index and Scryfall tags are installed."""
    st = engine().status()
    import datetime as dt

    def when(ts):
        return dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M") if ts else "never"

    rs = engine().rules_status()
    rules_line = (f"Comprehensive Rules: effective {rs['rules_effective']}, {rs['rules_count']} rules, "
                  f"{rs['glossary_count']} glossary terms (installed {when(rs['rules_updated_at'])})"
                  if rs.get("rules_count") else "Comprehensive Rules: not installed")
    return (f"Indexed cards: {st['indexed_cards']} (index built {when(st['index_built_at'])})\n"
            f"Oracle tags: {st['tag_count']} (updated {when(st['tags_updated_at'])})\n"
            f"Full-text search: {'yes' if st['full_text'] else 'no (LIKE fallback)'}\n"
            f"{rules_line}")


@_tool()
@_guard
def find_tags(
    query: Annotated[str, Field(description="What the card should do, in plain words, e.g. "
                                            "'sacrifice outlet', 'board wipe', 'copy tokens', 'land recursion'.")],
    limit: Annotated[int, Field(ge=1, le=50)] = 15,
    collection: CollectionArg = None,
) -> str:
    """Find Scryfall Tagger oracle tags (functional card tags) matching a need.

    Returns tag slugs to use in search_cards, with card counts, descriptions and
    narrower child tags. Broad tags include all their child tags' cards.
    With collection, also shows how many of the collection's cards have each tag."""
    tags = engine().find_tags(query, limit=limit, collection=collection)
    if not tags:
        return f"No tags matched '{query}'. Try other words, list_tags, or search_cards(text=...)."
    return f"Tags matching '{query}':\n" + _tag_lines(tags)


@_tool()
@_guard
def list_tags(
    parent: Annotated[Optional[str], Field(description="Show this tag's details and its child tags.")] = None,
    prefix: Annotated[Optional[str], Field(description="Only tags whose slug starts with this, e.g. 'sacrifice-'.")] = None,
    min_cards: Annotated[int, Field(ge=0, description="Hide tags with fewer cards.")] = 0,
    offset: Annotated[int, Field(ge=0)] = 0,
    limit: Annotated[int, Field(ge=1, le=300)] = 100,
    collection: Annotated[Optional[str], Field(description=(
        "Only tags that cards in this imported collection have, ordered by how many it has; "
        "min_cards then counts the collection's cards."))] = None,
) -> str:
    """Browse the full catalogue of Scryfall oracle tags (~4,500).

    With no arguments, lists top-level tags by size. Use `parent` to drill into a
    tag's children, or `prefix` to see a family of related slugs."""
    res = engine().list_tags(parent=parent, prefix=prefix, min_cards=min_cards, offset=offset,
                             limit=limit, collection=collection)
    out = []
    if res["parent"]:
        p = res["parent"]
        out.append(f"`{p['slug']}`: {p['description'] or '(no description)'}")
        out.append(f"{p['total_count']} cards incl. children ({p['direct_count']} tagged directly). "
                   f"Parents: {', '.join(p['parents']) or 'none'}. Aliases: {', '.join(p['aliases']) or 'none'}.")
        out.append("Children:" if res["tags"] else "No child tags.")
    shown = len(res["tags"])
    if res["tags"]:
        out.append(_tag_lines(res["tags"]))
    if res["total"] > res["offset"] + shown:
        out.append(f"\nShowing {res['offset'] + 1}-{res['offset'] + shown} of {res['total']}; "
                   f"use offset={res['offset'] + shown} for more.")
    return "\n".join(out) or "No tags found."


@_tool()
@_guard
def search_cards(
    tags_all: Annotated[Optional[List[str]], Field(description="Card must have ALL of these tag slugs.")] = None,
    tags_any: Annotated[Optional[List[str]], Field(description="Card must have AT LEAST ONE of these tags.")] = None,
    tags_none: Annotated[Optional[List[str]], Field(description="Card must have NONE of these tags.")] = None,
    text: Annotated[Optional[str], Field(description=(
        "Full-text search over name, type line and rules text. Words are ANDed; use "
        "\"quoted phrases\", OR between words, and -word to exclude."))] = None,
    types: Annotated[Optional[List[str]], Field(description="Type-line words that must all appear, e.g. ['creature'] or ['legendary','artifact'].")] = None,
    types_none: Annotated[Optional[List[str]], Field(description="Type-line words to exclude, e.g. ['land'].")] = None,
    color_identity: Annotated[Optional[str], Field(description=(
        "Commander colour identity as WUBRG letters, e.g. 'BG' or 'WUR'; 'C' for colourless. "
        "By default only cards inside this identity are returned; filters.color_identity_match changes the match mode."))] = None,
    mv_min: Annotated[Optional[float], Field(description="Minimum mana value.")] = None,
    mv_max: Annotated[Optional[float], Field(description="Maximum mana value.")] = None,
    price_max: Annotated[Optional[float], Field(description="Maximum price in EUR (cheapest paper printing).")] = None,
    rarity: Annotated[Optional[List[str]], Field(description="e.g. ['common','uncommon'].")] = None,
    exclude: Annotated[Optional[List[str]], Field(description="Card names to leave out, normally the current decklist.")] = None,
    commander_legal: Annotated[bool, Field(description="Only Commander-legal cards.")] = True,
    game_changers: Annotated[Literal["include", "exclude", "only"], Field(
        description="Filter on the official Commander 'Game Changer' list (relevant to power brackets).")] = "include",
    sort: Annotated[Literal["popular", "deep_cut", "least_popular", "price", "mana_value", "name", "random"], Field(
        description=("popular: EDHREC rank, most-played first. deep_cut: hides the most-played EDHREC "
                     f"cards (top {DEFAULT_DEEP_CUT_RANK} unless min_edhrec_rank is set) and orders the rest "
                     "by popularity - good for non-generic picks. least_popular: most obscure first. "
                     "price: cheapest first. mana_value: lowest first. random: a varied sample."))] = "popular",
    min_edhrec_rank: Annotated[Optional[int], Field(description="Exclude cards ranked better (lower number) than this on EDHREC.")] = None,
    max_edhrec_rank: Annotated[Optional[int], Field(description="Only cards ranked this well or better on EDHREC.")] = None,
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT)] = 25,
    offset: Annotated[int, Field(ge=0)] = 0,
    filters: Annotated[Optional[CardFilters], Field(description=(
        "Additional refinements; combine with the existing top-level filters. All supplied criteria are ANDed."))] = None,
    sort_reverse: Annotated[bool, Field(description="Reverse the default order for price, mana_value or name; unknown values stay last.")] = False,
    collection: CollectionArg = None,
) -> str:
    """Search local cards by tags, text, identity, mana value, budget and popularity.

    Use filters for colors, format legality, keywords, layouts and numeric stats.
    Use collection to search only cards the user owns.

    Returns a compact table (rules text truncated). Use get_cards for full details."""
    res = engine().search_cards(
        tags_all=tags_all or [], tags_any=tags_any or [], tags_none=tags_none or [],
        text=text, types=types or [], types_none=types_none or [], color_identity=color_identity,
        mv_min=mv_min, mv_max=mv_max, price_max=price_max, rarity=rarity or [], exclude=exclude or [],
        commander_legal=commander_legal, game_changers=game_changers, sort=sort,
        min_edhrec_rank=min_edhrec_rank, max_edhrec_rank=max_edhrec_rank, limit=limit, offset=offset,
        filters=filters, sort_reverse=sort_reverse, collection=collection,
    )
    rows = res["rows"]
    if not rows:
        if res["total"]:
            return f"{res['total']} cards match, but offset={offset} is beyond the last result. Use offset=0 to start again."
        msg = "No cards matched" + (f" in collection '{collection}'." if collection else ".")
        if res["notes"]:
            msg += " " + " ".join(res["notes"])
        return msg + " Loosen filters, try broader/parent tags, or a different sort."

    scope = f" in collection '{collection}'" if collection else ""
    lines = [f"{res['total']} cards match{scope}; showing {offset + 1}-{offset + len(rows)} (sort: {sort}{'; reversed' if sort_reverse else ''}).", "",
             "| Card | Mana | Type | CI | EDHREC | Price |" + (" Own |" if collection else "") + " Rules text |",
             "| --- | --- | --- | --- | ---: | ---: |" + (" ---: |" if collection else "") + " --- |"]
    for r in rows:
        name = r["name"] + (" (GC)" if r["game_changer"] else "")
        lines.append("| " + " | ".join([
            _cell(name), _cell(r["mana_cost"]) or "—", _cell(r["type_line"]), r["color_identity"] or "C",
            str(r["edhrec_rank"]) if r["edhrec_rank"] else "—", _price(r["price_eur"]),
            *([str(r["owned"])] if collection else []),
            _cell(r["oracle_text"], 220),
        ]) + " |")
    if res["total"] > offset + len(rows):
        lines.append(f"\nMore results: offset={offset + len(rows)}.")
    if any(r["game_changer"] for r in rows):
        lines.append("(GC) = on the Commander Game Changers list.")
    for note in res["notes"]:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


@_tool()
@_guard
def get_cards(
    names: Annotated[List[str], Field(description="Exact card names (a single face name is fine).")],
    show_price: bool = True,
) -> str:
    """Full card details (all faces, complete rules text, stats, rarity, price) as a Markdown table,
    from the local database only."""
    res = engine().get_cards_markdown(names, show_price=show_price)
    out = res["markdown"]
    if res["missing"]:
        hints = []
        for name in res["missing"]:
            sug = engine().suggest_names(name)
            hints.append(f"- {name}" + (f" (similar: {', '.join(sug)})" if sug else ""))
        out += "\nNot found in the local database:\n" + "\n".join(hints)
    return out or "No cards found."


@_tool()
@_guard
def card_tags(
    names: Annotated[List[str], Field(description="Card names whose tags you want.")],
) -> str:
    """List the Scryfall oracle tags directly applied to each card (most specific first).

    Useful to find what makes a card you like tick, then search for more cards with those tags."""
    res = engine().card_tags(names)
    out = []
    for name, tags in res.items():
        if tags is None:
            out.append(f"**{name}**: not found in the local database")
        elif not tags:
            out.append(f"**{name}**: no tags")
        else:
            out.append(f"**{name}**: " + ", ".join(f"`{t['slug']}` ({t['total_count']})" for t in tags))
    return "\n".join(out)


@_tool()
@_guard
def analyze_deck(
    names: Annotated[Optional[List[str]], Field(description="The decklist's card names (quantities not needed).")] = None,
    extra_tags: Annotated[Optional[List[str]], Field(description="Additional tag slugs to count, e.g. the deck's theme.")] = None,
    collection: Annotated[Optional[str], Field(description=(
        "Analyse a whole imported collection instead of names: role counts and its most common themes."))] = None,
) -> str:
    """Profile a decklist (or a collection): colour identity, how many cards fill each core Commander role
    (ramp, card advantage, removal, board wipes, counterspells, tutors, protection, recursion),
    and the most frequent functional tags. Use it to spot what the deck is missing."""
    if bool(names) == bool(collection):
        return "Error: give either names (a decklist) or collection, not both."
    res = engine().analyze_deck(names or [], extra_tags=extra_tags or [], collection=collection)
    out = [f"Cards recognised: {res['card_count']} · Colour identity: {res['color_identity']}", "", "**Roles**"]
    for role in res["roles"]:
        listed = ", ".join(role["cards"][:15]) + (" …" if len(role["cards"]) > 15 else "")
        out.append(f"- {role['label']} (`{role['slug']}`): {len(role['cards'])}" + (f" — {listed}" if listed else ""))
    if res["common_tags"]:
        out += ["", "**Most frequent tags**", ", ".join(f"`{t['slug']}` ×{t['count']}" for t in res["common_tags"])]
    if res["unknown"]:
        out += ["", "Not found locally: " + ", ".join(res["unknown"])]
    return "\n".join(out)


def _pct(v) -> str:
    return f"{v:.0f}%" if v is not None else "—"


def _signed_pct(v) -> str:
    return f"{v:+.0f}%" if v is not None else "—"


def _fetched(ts) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d") if ts else "?"


@_tool(annotations=EDHREC_READ_ONLY)
@_guard
def edhrec_recommendations(
    commander: Annotated[str, Field(description="Commander name; loose names like 'korvold' are resolved locally.")],
    partner: Annotated[Optional[str], Field(description="Second commander (partner / background / friends forever).")] = None,
    theme: Annotated[Optional[str], Field(description=(
        "An EDHREC theme slug for this commander, e.g. 'sacrifice', 'treasure', 'tokens'. "
        "The first call (no theme) lists the available themes."))] = None,
    bracket: Annotated[Optional[Literal["exhibition", "core", "upgraded", "optimized", "cedh"]], Field(
        description=("Restrict to a Commander bracket: exhibition (1), core (2), upgraded (3), "
                     "optimized (4), or cedh (5). As with themes, the first call lists available brackets. "
                     "Cannot be combined with theme."))] = None,
    budget: Annotated[Optional[Literal["budget", "expensive"]], Field(
        description="EDHREC's budget or expensive deck pool.")] = None,
    sections: Annotated[Optional[List[str]], Field(description=(
        "Only these EDHREC lists, e.g. ['Creatures','Instants'], ['High Synergy Cards'], ['Utility Lands'], ['New Cards']."))] = None,
    types: Annotated[Optional[List[str]], Field(description="Type-line words that must appear, e.g. ['creature'].")] = None,
    types_none: Annotated[Optional[List[str]], Field(description="Type-line words to exclude.")] = None,
    tags_all: Annotated[Optional[List[str]], Field(description="Scryfall oracle tag slugs the card must all have.")] = None,
    tags_any: Annotated[Optional[List[str]], Field(description="Card must have at least one of these tags.")] = None,
    tags_none: Annotated[Optional[List[str]], Field(description="Card must have none of these tags.")] = None,
    price_max: Annotated[Optional[float], Field(description="Maximum EUR price (local cheapest paper printing).")] = None,
    mv_min: Optional[float] = None,
    mv_max: Optional[float] = None,
    exclude: Annotated[Optional[List[str]], Field(description="Card names to leave out, usually the current decklist.")] = None,
    min_inclusion: Annotated[Optional[float], Field(description="Minimum % of this commander's decks that play the card (0-100).")] = None,
    min_synergy: Annotated[Optional[float], Field(description="Minimum EDHREC synergy in percentage points (can be negative).")] = None,
    include_lands: bool = True,
    game_changers: Literal["include", "exclude", "only"] = "include",
    sort: Annotated[Literal["inclusion", "synergy", "lift", "trend", "price", "mana_value", "name"], Field(
        description=("inclusion: most-played with this commander. synergy: played much more with this commander "
                     "than in other decks of its colours. lift: unusually strong commander-specific pairings "
                     "(good for non-generic picks). trend: rising recently. price: cheapest first."))] = "inclusion",
    limit: Annotated[int, Field(ge=1, le=MAX_LIMIT, description="Page size when all_pages=false.")] = 40,
    offset: Annotated[int, Field(ge=0, description="Starting position when all_pages=false.")] = 0,
    full_text: Annotated[bool, Field(description="Return complete, untruncated Oracle rules text (default true).")] = True,
    all_pages: Annotated[bool, Field(description="Return all matching recommendations, ignoring limit and offset (default true).")] = True,
    refresh: Annotated[bool, Field(description="Re-download from EDHREC instead of using the 7-day local cache.")] = False,
    filters: Annotated[Optional[CardFilters], Field(description="Same additional card refinements as search_cards; applied before ranking and pagination.")] = None,
    color_identity: Annotated[Optional[str], Field(description="Optional WUBRG identity or C; subset by default, refined by filters.color_identity_match.")] = None,
    commander_legal: Annotated[bool, Field(description="Only Commander-legal cards unless filters.legalities supplies an explicit Commander status.")] = True,
    sort_reverse: Annotated[bool, Field(description="Reverse this sort's default order (e.g. price: highest first; inclusion: lowest first).")] = False,
    collection: Annotated[Optional[str], Field(description=(
        "Only recommendations that are in this imported collection (cards the user owns)."))] = None,
) -> str:
    """EDHREC's recommended cards for a commander (optionally a theme, power bracket or budget pool),
    enriched with local mana cost, type, price and rules text, and filterable by Scryfall tags,
    budget and curve. By default returns every matching card with complete rules text.
    Also reports available themes, power brackets, similar commanders and average deck shape."""
    res = engine().edhrec_recommendations(
        commander, partner=partner, theme=theme, bracket=bracket, budget=budget, sections=sections or [],
        types=types or [], types_none=types_none or [], tags_all=tags_all or [], tags_any=tags_any or [],
        tags_none=tags_none or [], price_max=price_max, mv_min=mv_min, mv_max=mv_max, exclude=exclude or [],
        min_inclusion=min_inclusion, min_synergy=min_synergy, include_lands=include_lands,
        game_changers=game_changers, sort=sort, limit=limit, offset=offset, refresh=refresh,
        filters=filters, color_identity=color_identity, commander_legal=commander_legal,
        sort_reverse=sort_reverse, all_pages=all_pages, collection=collection,
    )
    page, rows = res["page"], res["rows"]
    offset = res["offset"]
    out = [f"**{page['header']}** · {page['num_decks'] or '?'} decks on EDHREC · data from {_fetched(page['fetched_at'])}"]
    if offset == 0:
        if page["themes"] and not theme:
            out.append("Themes: " + ", ".join(f"`{t['slug']}` ({t['decks']})" for t in page["themes"][:20]))
        if not theme and not bracket:
            counts = page.get("bracket_counts") or {}
            options = [("exhibition", 1), ("core", 2), ("upgraded", 3),
                       ("optimized", 4), ("cedh", 5)]
            listed = []
            for slug, number in options:
                count = (counts.get(slug, counts.get(str(number)))
                         if isinstance(counts, dict) else None)
                if isinstance(count, dict):
                    count = count.get("count")
                listed.append(f"`{slug}` ({number}" +
                              (f"; {count} decks" if isinstance(count, int) else "") + ")")
            out.append("Brackets: " + ", ".join(listed))
        if page["similar"]:
            out.append("Similar commanders: " + ", ".join(page["similar"][:6]))
        tc = page["type_counts"]
        if tc:
            out.append("Average deck: " + ", ".join(f"{v} {k}" for k, v in tc.items() if v))
        if page["mana_curve"]:
            out.append("Average curve (nonland, by MV): " +
                       ", ".join(f"{k}:{v}" for k, v in sorted(page["mana_curve"].items(), key=lambda x: float(x[0]))))
    out.append("")
    if not rows:
        if res["total"]:
            out.append(f"{res['total']} recommended cards match, but offset={offset} is beyond the last result. Use offset=0 to start again.")
        else:
            out.append("No recommended cards match these filters"
                       + (f" in collection '{collection}'." if collection else "."))
    else:
        scope = f" in collection '{collection}'" if collection else ""
        out.append(f"{res['total']} recommended cards match{scope}; showing {offset + 1}-{offset + len(rows)} (sort: {sort}{'; reversed' if sort_reverse else ''}).")
        out += ["", "| Card | EDHREC list | Incl. | Synergy | Lift | Mana | Type | Price |" + (" Own |" if collection else "") + " Rules text |",
                "| --- | --- | ---: | ---: | ---: | --- | --- | ---: |" + (" ---: |" if collection else "") + " --- |"]
        for r in rows:
            name = r["name"] + (" (GC)" if r["game_changer"] else "")
            out.append("| " + " | ".join([
                _cell(name), _cell(r["section"]), _pct(r["inclusion"]), _signed_pct(r["synergy"]),
                f"{r['lift']:.1f}" if r["lift"] else "—", _cell(r["mana_cost"]) or "—",
                _cell(r["type_line"]), _price(r["price_eur"]),
                *([str(r["owned"])] if collection else []),
                _cell(r["oracle_text"]) if full_text else _cell(r["oracle_text"], 150),
            ]) + " |")
        if res["total"] > offset + len(rows):
            out.append(f"\nMore: offset={offset + len(rows)}.")
    for note in res["notes"]:
        out.append(f"Note: {note}")
    return "\n".join(out)


@_tool(annotations=EDHREC_READ_ONLY)
@_guard
def edhrec_average_deck(
    commander: Annotated[str, Field(description="Commander name; loose names are resolved locally.")],
    partner: Optional[str] = None,
    theme: Optional[str] = None,
    bracket: Optional[Literal["exhibition", "core", "upgraded", "optimized", "cedh"]] = None,
    budget: Optional[Literal["budget", "expensive"]] = None,
    refresh: bool = False,
    collection: Annotated[Optional[str], Field(description=(
        "Compare with this imported collection: owned count, missing cards and their cost."))] = None,
) -> str:
    """EDHREC's statistically average 99 for a commander (optionally theme / bracket / budget),
    with local prices. A baseline to improve on, not a finished deck."""
    deck = engine().edhrec_average_deck(commander, partner=partner, theme=theme, bracket=bracket,
                                        budget=budget, refresh=refresh, collection=collection)
    out = [f"**{deck['header']}** · {deck['card_count']} cards · ≈€{deck['total_price']:.2f} "
           f"(local prices) · data from {_fetched(deck['fetched_at'])}"]
    owned = deck.get("collection")
    if owned:
        out.append(f"Collection '{owned['name']}': own {owned['owned']}/{owned['needed']} non-basic cards; "
                   f"missing {len(owned['missing'])} cost ≈€{owned['missing_price']:.2f}.")
    for section, cards in deck["sections"].items():
        out.append(f"\n**{section}** ({sum(c['qty'] for c in cards)})")
        out.append(", ".join((f"{c['qty']}x " if c["qty"] > 1 else "") + c["name"] for c in cards))
    if owned and owned["missing"]:
        out.append("\nMissing from collection: " + ", ".join(owned["missing"]))
    if deck["unpriced"]:
        out.append("\nNo local price: " + ", ".join(deck["unpriced"]))
    for note in deck["notes"]:
        out.append(f"Note: {note}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------

def _collection_summary(info) -> List[str]:
    out = [f"**{info['name']}** · {info['unique_cards']} unique cards, {info['copies']} copies · "
           f"≈€{info['value_eur']:.2f} (local prices"
           + (f"; {info['unpriced']} cards unpriced" if info["unpriced"] else "") + ")",
           f"Source: {info['source']}"]
    if info["by_identity"]:
        out.append("By colour identity: " + ", ".join(f"{ci} {n}" for ci, n in info["by_identity"]))
    if info["by_type"]:
        out.append("By card type: " + ", ".join(f"{t} {n}" for t, n in info["by_type"]))
    out.append(f"Legendary creatures (possible commanders): {info['legendary_creatures']}")
    return out


def _unmatched_lines(unmatched, limit: int) -> List[str]:
    if not unmatched:
        return []
    out = [f"\nNot imported ({len(unmatched)}):"]
    for u in unmatched[:limit]:
        hint = ""
        if u["reason"] == "not in the local card database":
            sug = engine().suggest_names(u["name"])
            hint = f" (similar: {', '.join(sug)})" if sug else ""
        out.append(f"- {u['name']}" + (f" ×{u['quantity']}" if u["quantity"] else "")
                   + f": {u['reason']}{hint}")
    if len(unmatched) > limit:
        out.append(f"... {len(unmatched) - limit} more (collection_info with show_unmatched for all)")
    return out


@_tool(annotations=COLLECTION_WRITE)
@_guard
def import_collection(
    name: Annotated[str, Field(description="Name to store the collection under, e.g. 'binder' or 'main'.")],
    path: Annotated[Optional[str], Field(description=(
        "Absolute path to a text or CSV export on this computer. Preferred for large collections."))] = None,
    text: Annotated[Optional[str], Field(description="The card list itself, one card per line, e.g. '4 Sol Ring'.")] = None,
    mode: Annotated[Literal["replace", "add"], Field(description=(
        "replace: the collection becomes exactly this list. add: merge into an existing collection, adding quantities."))] = "replace",
) -> str:
    """Import cards the user owns from a file or pasted text and store them under a name.

    Reads decklist-style lines ('4 Sol Ring', '1 Sol Ring (C21) 263 *F*', Arena/MTGO/Moxfield/Archidekt
    text) and CSV exports with a Name column (Moxfield, Deckbox, ManaBox, Dragon Shield, ...).
    Afterwards pass collection=<name> to search_cards, edhrec_recommendations, analyze_deck,
    find_tags and list_tags to work only with owned cards."""
    res = engine().import_collection(name, text=text, path=path, mode=mode)
    verb = "Added to" if mode == "add" else "Imported"
    out = [f"{verb} '{res['name']}': {res['imported_unique']} unique cards ({res['imported_copies']} copies)"
           + (f", {len(res['imported_unmatched'])} lines not imported." if res["imported_unmatched"] else "."), ""]
    out += _collection_summary(res)
    out += _unmatched_lines(res["imported_unmatched"], 15)
    out.append(f"\nUse collection=\"{res['name']}\" in search_cards and the other tools.")
    return "\n".join(out)


@_tool()
@_guard
def list_collections() -> str:
    """List the imported collections with their size."""
    rows = engine().list_collections()
    if not rows:
        return "No collections imported yet. Use import_collection with a file path or pasted list."
    return "\n".join(f"- **{r['name']}**: {r['unique_cards']} unique cards, {r['copies']} copies"
                     + (f", {r['unmatched']} lines not imported" if r["unmatched"] else "")
                     + f" (source: {r['source']})" for r in rows)


@_tool()
@_guard
def collection_info(
    name: Annotated[str, Field(description="Collection name.")],
    show_unmatched: Annotated[bool, Field(description="List every line that was not imported.")] = False,
) -> str:
    """Overview of an imported collection: size, value, colour identity and card type spread,
    possible commanders, and lines that could not be imported."""
    info = engine().collection_info(name)
    out = _collection_summary(info)
    out += _unmatched_lines(info["unmatched"], len(info["unmatched"]) if show_unmatched else 10)
    out.append("\nFor roles and themes use analyze_deck(collection=...); for tags list_tags(collection=...).")
    return "\n".join(out)


@_tool(annotations=COLLECTION_DELETE)
@_guard
def delete_collection(
    name: Annotated[str, Field(description="Collection to delete.")],
) -> str:
    """Delete a stored collection (the original file is not touched)."""
    return f"Deleted collection '{engine().delete_collection(name)}'."


# ---------------------------------------------------------------------------
# Comprehensive Rules (read-only)
# ---------------------------------------------------------------------------

def _rule_line(number: str, kind: str, text: str) -> str:
    sep = "." if kind in ("chapter", "section", "rule") else ""
    return f"{number}{sep} {text}"


@_tool()
@_guard
def rules_lookup(
    rule: Annotated[str, Field(description=(
        "A rule number ('702.19', '704.5k', '903'), a chapter ('7'), or the name of a keyword / "
        "section ('Trample', 'Ward', 'Commander').") )],
) -> str:
    """Read the Magic Comprehensive Rules: a rule with all its subrules and examples, or the
    list of rules in a section / sections in a chapter."""
    res = engine().rules_lookup(rule)
    e = res["entry"]
    out = []
    if res["breadcrumb"]:
        out.append(" › ".join(res["breadcrumb"]))
    out.append(_rule_line(e["number"], e["kind"], e["text"]))
    if res["children"]:
        out.append("")
        for c in res["children"]:
            text = c["text"] if res["full_children"] or c["kind"] == "section" \
                else c["text"].splitlines()[0][:160]
            out.append(_rule_line(c["number"], c["kind"], text))
        if not res["full_children"]:
            out.append("\n(Use rules_lookup on a number above for its full text.)")
    if res["more"]:
        out.append(f"... {res['more']} more")
    return "\n".join(out)


@_tool()
@_guard
def rules_search(
    query: Annotated[str, Field(description=(
        "Key terms to find in the Comprehensive Rules, e.g. 'commander tax', 'deathtouch trample', "
        "'\"state-based actions\" legend'. Few specific words work best."))],
    limit: Annotated[int, Field(ge=1, le=50)] = 12,
    include_glossary: bool = True,
) -> str:
    """Search the Magic Comprehensive Rules and glossary; returns matching rule numbers with text."""
    hits = engine().rules_search(query, limit=limit, include_glossary=include_glossary)
    if not hits:
        return f"No rules matched '{query}'. Try other key terms or rules_glossary."
    out = []
    for h in hits:
        if h["kind"] == "glossary":
            term, _, definition = h["text"].partition("\n")
            out.append(f"- Glossary · **{term}**: {_cell(definition, 300)}")
        else:
            out.append(f"- **{h['number']}** {_cell(h['text'], 300)}")
    return "\n".join(out) + "\n\nUse rules_lookup with a number for the full rule and its subrules."


@_tool()
@_guard
def rules_glossary(
    term: Annotated[str, Field(description="A game term, e.g. 'Mana Value', 'Legend Rule', 'Commander Tax'.")],
) -> str:
    """Look a term up in the Comprehensive Rules glossary."""
    hits = engine().rules_glossary(term)
    if not hits:
        return f"'{term}' is not in the glossary. Try rules_search."
    return "\n\n".join(f"**{h['term']}**\n{h['definition']}" for h in hits)


@mcp.resource("rules://comprehensive-rules", name="Magic Comprehensive Rules",
              description="The full Magic: The Gathering Comprehensive Rules text stored by MTG Deck Formatter.",
              mime_type="text/plain")
def comprehensive_rules_text() -> str:
    try:
        return engine().rules_text()
    except SearchError as err:
        return str(err)


# ---------------------------------------------------------------------------

_DB_FILE = si.DB_FILE


def main(argv=None):
    global _DB_FILE
    ap = argparse.ArgumentParser(description="MTG card search MCP server (stdio).")
    ap.add_argument("--db", help="path to scryfall_cache.db (defaults to the app's database)")
    args = ap.parse_args(argv)
    if args.db:
        _DB_FILE = args.db
    print(f"mtg-cards MCP server using {_DB_FILE}", file=sys.stderr)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
