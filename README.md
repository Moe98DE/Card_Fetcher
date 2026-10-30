# MTG Deck Formatter

 [Screenshot](https://imgur.com/a/sZ3awTF)

A simple yet powerful desktop application for Windows, Mac, and Linux that takes a raw Magic: The Gathering decklist and formats it into a detailed, human-readable report with card data fetched live from the Scryfall API.

## Features

-   **Flexible Parsing**: Accepts various decklist formats (e.g., `1x Sol Ring`, `Sol Ring`, `1 High Tide (FEM) 18b`, `1 Loki, God of Mischief (PMEI) 2026-14 *F*`) and exports from MTG Arena, Moxfield, Archidekt and MTGO. Duplicate entries are merged.
-   **Skipped Card Report**: Any line that couldn't be read or found on Scryfall is listed in a dialog after processing, together with the reason.
-   **Euro Pricing**: Shows the cheapest regular (nonfoil) paper EUR price across all printings.
-   **Detailed Card Information**: Fetches and displays mana cost, colors, card type, full oracle text, and P/T for creatures.
-   **Responsive GUI**: The app remains fully responsive while fetching card data in the background, with a progress bar to show its status.
-   **Simple Interface**: A clean and intuitive UI built with Python's native Tkinter library.
-   **Copy to Clipboard**: Easily copy the entire formatted output with a single click.
-   **Modular Codebase**: Built with separation of concerns, making the code easy to read, maintain, and expand.

## How to Use

To run this application, you will need Python 3 installed on your system.

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/Moe98DE/Card_Fetcher.git
    cd Card_Fetcher
    ```

2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv venv
    ```
    Activate it:
    -   On Windows: `.\venv\Scripts\activate`
    -   On macOS/Linux: `source venv/bin/activate`

3.  **Install the required dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Run the application:**
    ```bash
    python main_gui.py
    ```

## OpenAI Codex and Claude Integration (Card Search for AI Deckbuilding)

The app exposes the same MCP tools to OpenAI Codex, Claude Desktop, and Claude
Code over **stdio**. Card and rules searches use only your local Scryfall data;
EDHREC tools can fetch public recommendations and cache them locally. No OpenAI
or Anthropic API key is needed by this server. Both clients can use it at once.

What each client gets:

-   **Scryfall Tagger oracle tags**: ~4,500 community function tags
    (`sacrifice-outlet`, `token-doubler`, `sweeper`, ...). Broad tags include
    all their narrower child tags, so `removal` really means all removal.
-   **`find_tags` / `list_tags`**: turn a need ("board wipe", "graveyard hate")
    into exact tag slugs, or browse the whole tag catalogue.
-   **`search_cards`**: filter by tags, rules text, type, colour identity, mana
    value, EUR budget, rarity, Game Changer status, and exclude the current list.
    Sort by `popular` (EDHREC staples first) or `deep_cut` (hides the most-played
    EDHREC cards and shows the best of the rest) plus price, mana value or random.
-   **`analyze_deck`**: counts ramp, draw, removal, wipes, tutors etc. in a list
    and shows its recurring themes, so the assistant can see what is missing.
-   **`card_tags`** and **`get_cards`**: the tags of a card you like, and full card
    details in this app's Markdown table format.
-   **`edhrec_recommendations`**: give it just a commander (loose names like
    "korvold" work, partners too) and it fetches EDHREC's recommended pool for it,
    optionally for a theme (`treasure`), a power bracket (`exhibition` to `cedh`)
    or the budget/expensive pools. Every card is enriched with local mana cost,
    type, price and rules text, and the pool can be filtered with the same tags,
    budget, curve and type filters as `search_cards`. Sort by `inclusion`
    (consensus), `synergy` / `lift` (commander-specific, less generic picks),
    `trend` or `price`. It also reports the commander's themes, similar
    commanders, average type split and mana curve.
    By default, `all_pages=true` returns every matching recommendation (no pagination)
    and `full_text=true` includes complete, untruncated Oracle rules text.
    Set either flag to `false` to restore paginated results or shortened text.
    The response lists available theme and bracket filters; use `bracket="upgraded"`
    (or `exhibition`, `core`, `optimized`, `cedh`) similarly to `theme="tokens"`.
    EDHREC does not support combining a theme with a bracket.
-   **`edhrec_average_deck`**: EDHREC's average 99 for a commander, with a local
    price total, as a baseline to improve on.

-   **`rules_lookup` / `rules_search` / `rules_glossary`**: read access to the
    Magic Comprehensive Rules stored by the app: a rule with all subrules by number
    or keyword name (`702.19`, `Ward`, `903`), a search over rules and glossary,
    and glossary definitions. The full text is also exposed as the MCP resource
    `rules://comprehensive-rules`.

-   **Collections (`import_collection`, `list_collections`, `collection_info`,
    `delete_collection`)**: import the cards you own from a text or CSV file and
    pass `collection="<name>"` to `search_cards`, `edhrec_recommendations`,
    `analyze_deck`, `find_tags` and `list_tags` to work only with owned cards.
    See [Searching your collection](#searching-your-collection).

The rules are only ever downloaded by you: click **Update Rules** (leave the URL
empty to fetch the newest `.txt` from magic.wizards.com/rules, paste a direct
link, or import a local file), or run `python rules.py [url-or-file]`. The file
is stored as `comprehensive_rules.txt` next to the card database (the previous
version is kept as `.previous`). The MCP server has no tool to fetch rules.

EDHREC has no official API; the tool requests only the page for the commander
you name, the same JSON the EDHREC website loads, and caches it for 7 days in
`edhrec_cache.db` next to the card database. Repeat requests are offline, and if
EDHREC is unreachable an older cached copy is used. The server performs these requests; the client does not need a browser tool.

### Searching your collection

Ask the assistant to import your collection, e.g. *"import my collection from
C:\Users\me\Downloads\moxfield_haves.csv as 'binder'"*. `import_collection`
reads the file itself (give an absolute path) or accepts a pasted list:

-   Decklist-style text: `4 Sol Ring`, `1x Sol Ring (C21) 263 *F*`, and the
    Arena, MTGO, Moxfield and Archidekt text exports the GUI already reads.
-   CSV exports with a header row containing a `Name` (or `Card Name`) column and
    optionally `Count` / `Quantity` (Moxfield, Deckbox, ManaBox, Dragon Shield ...).

Rows for the same card (different printings, foils) are merged and their
quantities added. Names that are not in the local database are listed with
spelling suggestions. `mode="add"` merges another file into an existing
collection; the default `replace` overwrites it. Collections are stored by name
in `collections.db` next to the card database, so they survive restarts; the
card database itself stays read-only.

Then add `collection="binder"` to the usual tools. Every other argument keeps
its meaning, so tags, text search and `filters` work on owned cards:

| Goal | Call |
| --- | --- |
| Ramp in Golgari colours I own | `search_cards(collection="binder", tags_any=["ramp"], color_identity="BG")` |
| Owned cards with specific wording | `search_cards(collection="binder", text='"enters the battlefield" token')` or `filters={"oracle_contains": "+1/+1 counter"}` |
| Which tags / themes my collection covers | `list_tags(collection="binder")`, `find_tags("sacrifice outlet", collection="binder")`, `analyze_deck(collection="binder")` |
| EDHREC picks for a commander that I own | `edhrec_recommendations("Korvold", collection="binder")` |
| What I'd need to buy for the average deck | `edhrec_average_deck("Korvold", collection="binder")` |

Results include an **Own** column with the number of copies. `collection_info`
summarises size, value, colour identity and card type spread, and possible
commanders.

### Composing detailed card queries

Use the existing `search_cards` tool for local discovery and
`edhrec_recommendations` for a commander's recommendation pool. Both accept the
same optional **`filters` object** for additional refinements. There are no
separate tools for each filter combination. Existing arguments such as
`tags_all`/`tags_any`/`tags_none`, `types`/`types_none`, `color_identity`,
`mv_min`/`mv_max`, `price_max`, and `exclude` keep their meanings and defaults.
`rarity` and EDHREC-rank bounds remain available on `search_cards`.

All supplied criteria are combined with **AND**; fields named `any` and
`layouts` accept alternatives within that field. Filters run before sorting,
counting, and pagination. Omit a criterion to leave that property unrestricted.

| Inside `filters` | Meaning |
| --- | --- |
| `color_identity_match` | Refines the existing `color_identity`: `subset` (default, within a commander's identity), `exact`, `contains` (every requested color), or `overlap` (at least one requested color). |
| `colors`, `colors_match` | Actual card colors, distinct from identity. WUBRG letters or `C`; same matching modes, default `exact`. Uses the card's top-level colors, or the union of its faces if those are absent. |
| `legalities` | Format-to-status map, e.g. `{"modern":"legal","vintage":"restricted"}`. All entries must match. Statuses: `legal`, `not_legal`, `restricted`, `banned`. An absent format is unknown and does not match `not_legal`. |
| `price_min` | Inclusive EUR minimum, combined with existing `price_max`. |
| `price_known` | `true`: priced cards only; `false`: unpriced only; omitted: either. Unpriced cards never pass price bounds. |
| `name_contains`, `oracle_contains` | Literal, case-insensitive substring in name only or rules text only. Rules text includes all faces; `%` and `_` are literal characters. Existing `text` remains full-text search across name/type/rules. |
| `types_any` | At least one type-line substring, combined with existing `types` and `types_none`. |
| `keywords_all`, `keywords_any`, `keywords_none` | Exact Scryfall keyword names, case-insensitive, e.g. `Flying`, `Ward`. These are distinct from functional Tagger slugs. |
| `layouts` | Any listed Scryfall layout, e.g. `normal`, `transform`, `modal_dfc`, `adventure`. |
| `stats` | Map of `power`, `toughness`, `loyalty`, or `defense` to inclusive `min`/`max` ranges. All ranges must match one face. Missing or variable stats (`*`, `1+*`, `X`) never pass numeric bounds. |

Both tools default to `commander_legal=true`. An explicit
`filters.legalities.commander` takes precedence, so asking for banned Commander
cards works without also changing the boolean. For a different format without
the Commander restriction, set `commander_legal=false`. Unknown formats produce
an error listing the formats recorded in your local database. `overlap` requires
at least one color; use `exact` with `C` for colorless cards.

Both tools accept `sort_reverse=true` to reverse the default sort direction.
On `search_cards`, this applies to `price`, `mana_value`, and `name`; use the
existing `least_popular` sort to reverse popularity. EDHREC supports reversing
any of its sorts (e.g. lowest inclusion first). Unknown prices and mana values
stay last. Deterministic sorts use card names to break ties for stable pages;
`random` remains a fresh sample on each call.

Example `search_cards` arguments for affordable ramp inside a Golgari identity:

```json
{
  "tags_all": ["ramp"],
  "color_identity": "BG",
  "mv_max": 2,
  "price_max": 3,
  "filters": {"price_min": 0.25},
  "sort": "price"
}
```

Example for blue Modern-legal flyers with at least four power:

```json
{
  "commander_legal": false,
  "types": ["creature"],
  "filters": {
    "colors": "U",
    "colors_match": "contains",
    "legalities": {"modern": "legal"},
    "keywords_all": ["Flying"],
    "stats": {"power": {"min": 4}}
  }
}
```

Example `edhrec_recommendations` arguments combining the existing pool selection
with the shared card filters:

```json
{
  "commander": "Korvold, Fae-Cursed King",
  "theme": "sacrifice",
  "price_max": 5,
  "mv_max": 3,
  "filters": {"types_any": ["creature", "artifact"], "price_min": 0.25},
  "sort": "synergy"
}
```

These refinements read existing cached JSON; **no database migration, rebuild,
or new download is required**. Restart the MCP client/server to discover the
updated schemas. Install updated `requirements.txt` when running from source.
The local cache contains one stored Scryfall record per card name, plus the
cheapest regular paper EUR price across printings. It is not an index of every
printing. Legality and prices reflect the last local database update. EDHREC
still fetches requested pages and caches them for seven days; these changes do
not turn it into an offline copy of all of EDHREC. Raw Scryfall query syntax,
arbitrary SQL, printing/set searches, and additional currencies are not parsed.

### Setup

**Packaged app:** each build also contains `MtgCardsMCP` (`MtgCardsMCP.exe` on
Windows), a small console program that serves the tools. Keep it in the same
folder as the app (on macOS, next to `MtgDeckFormatter.app`). Packaged
windowed apps can't serve MCP over stdio directly, so each client launches
this program instead. It reads the same local database as the app.

**From source:** `pip install mcp` in the environment you run the app with.

Then, either way:

1.  In the app, click **Update Local DB** (this also downloads the Scryfall oracle
    tags, ~6MB) or, if your card database is already current, just **Update Tags**.
    Optionally click **Update Rules**.
2.  Choose **Codex Setup** or **Claude Setup** and follow the client instructions
    below. Both buttons generate absolute paths for this installation, for either
    the packaged server or your current Python environment. They only copy text;
    they do not overwrite either client's settings.

### OpenAI Codex (app, CLI, and IDE extension)

Click **Codex Setup** and merge the generated TOML into `~/.codex/config.toml`
(or `$CODEX_HOME/config.toml` when set). You can instead use a trusted project's
`.codex/config.toml`. Replace any existing `[mcp_servers.mtg-cards]` table rather
than duplicating it, and keep your other settings.

Example for a source installation (replace paths with your own):

```toml
[mcp_servers.mtg-cards]
command = "/absolute/path/Card_Fetcher/venv/bin/python"
args = ["/absolute/path/Card_Fetcher/mcp_server.py"]
startup_timeout_sec = 30
tool_timeout_sec = 120
```

On Windows use the virtual environment's `Scripts/python.exe`. TOML paths can
use forward slashes, such as `C:/Card_Fetcher/venv/Scripts/python.exe`; the setup
button also handles escaped backslashes. For packaged builds, `command` is the
absolute path to `MtgCardsMCP.exe` (Windows) or `MtgCardsMCP` (macOS/Linux), and
`args = []`. The longer timeouts allow packaged startup and EDHREC requests.

Alternatively, register a source installation with the Codex CLI:

```sh
codex mcp add mtg-cards -- /absolute/path/venv/bin/python /absolute/path/Card_Fetcher/mcp_server.py
codex mcp list
```

Restart Codex after changing configuration. Ask it to call `database_status`,
then `search_cards` with `text="Sol Ring"`. In the CLI, `/mcp` shows the connection.
A missing database does not prevent connection: the tools explain how to install
it. See the [official Codex MCP documentation](https://developers.openai.com/codex/mcp/).

### Claude Desktop and Claude Code

The existing **Claude Setup** workflow is unchanged: merge its JSON snippet into
Claude Desktop's `claude_desktop_config.json` (Settings → Developer → Edit Config),
keeping other entries under `mcpServers`, then restart Claude Desktop.

Claude Code can use the same command/args entry in a project `.mcp.json`:

```json
{
  "mcpServers": {
    "mtg-cards": {
      "command": "/absolute/path/venv/bin/python",
      "args": ["/absolute/path/Card_Fetcher/mcp_server.py"]
    }
  }
}
```

### Troubleshooting

On macOS, if Claude Desktop can't start `MtgCardsMCP` because it was downloaded
from the internet, run `xattr -d com.apple.quarantine /path/to/MtgCardsMCP` once.

If HTTPS requests fail because something on your network intercepts TLS (see
the SSL notes in `api_client.py`), add
`"env": {"REQUESTS_CA_BUNDLE": "C:\\path\\to\\combined-ca-bundle.pem"}` to the
`mtg-cards` entry in the Claude Desktop config. For Codex, add:

```toml
[mcp_servers.mtg-cards.env]
REQUESTS_CA_BUNDLE = "C:/path/to/combined-ca-bundle.pem"
```

Use the console Python executable, not `pythonw.exe`. The source interpreter must
have `requirements.txt` installed. No working directory setting is required.
To select a different database, append `"--db", "/absolute/path/scryfall_cache.db"`
to the server's `args` in either client. Each client starts its own process; both
read the same card database. Only EDHREC tools access the network, and their MCP
annotations advertise this. All diagnostics go to stderr, leaving stdout for MCP.

Headless refresh is also possible with `python search_index.py`.

New modules: `search_index.py` (tag import and search index), `card_search.py`
(search engine, usable without MCP), `edhrec.py` (EDHREC fetch, parse and cache),
`rules.py` (Comprehensive Rules download and indexing; GUI/CLI only) and
`mcp_server.py` (the tools).

## Project Structure

The application is designed to be modular and scalable:

-   `main_gui.py`: The main entry point for the desktop application, handling all GUI logic, threading, and event handling.
-   `parser.py`: Contains the logic for parsing raw decklist text into a structured format.
-   `api_client.py`: Manages all communication with the external Scryfall API.
-   `models.py`: Defines the `Card` data structure, decoupling the app from the specific API response format.
-   `formatter.py`: Handles the presentation logic, turning structured `Card` data into a clean, formatted string.
-   `collection.py`: Parses collection exports (text and CSV) and stores named collections for the MCP tools.

## Dependencies

-   [requests](https://pypi.org/project/requests/): For making HTTP requests to the Scryfall API.
-   Python's built-in libraries: `tkinter`, `threading`, `queue`, `re`, `textwrap`.

## Acknowledgments

This application relies entirely on the fantastic and free [Scryfall API](https://scryfall.com/docs/api) for all Magic: The Gathering card data.

## License

This project is licensed under the MIT License - see the `LICENSE` file for details.
## MCP compatibility tests

Run the offline tests with Python 3.11+ after installing `requirements.txt`:

```sh
python -m unittest discover -s tests -v
```

Python 3.10 also needs `pip install tomli` for these tests. They exercise JSON/TOML
configuration, stdio initialization with Codex and Claude client identities,
tool discovery, search, validation, resources, and missing-database recovery.
They also exercise composed filters, all color/legality modes, unknown prices,
multi-face stats, filtered pagination, and the shared EDHREC filtering path.
They use temporary read-only fixture databases and mocked EDHREC pools; no
Scryfall or EDHREC data is downloaded.
Set `MCP_SERVER_BINARY` to a built server's absolute path to test the packaged
executable instead. CI checks both supported SDK major versions and all three OSes.
