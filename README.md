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

## Claude Desktop Integration (Card Search for AI Deckbuilding)

The app can expose your local card database to Claude Desktop as a set of
read-only search tools (via MCP). Claude can then look for cards a deck is
missing **without any web access**, using only your local Scryfall data.

What Claude gets:

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
    and shows its recurring themes, so Claude can see what is missing.
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
-   **`edhrec_average_deck`**: EDHREC's average 99 for a commander, with a local
    price total, as a baseline to improve on.

-   **`rules_lookup` / `rules_search` / `rules_glossary`**: read access to the
    Magic Comprehensive Rules stored by the app: a rule with all subrules by number
    or keyword name (`702.19`, `Ward`, `903`), a search over rules and glossary,
    and glossary definitions. The full text is also exposed as the MCP resource
    `rules://comprehensive-rules`.

The rules are only ever downloaded by you: click **Update Rules** (leave the URL
empty to fetch the newest `.txt` from magic.wizards.com/rules, paste a direct
link, or import a local file), or run `python rules.py [url-or-file]`. The file
is stored as `comprehensive_rules.txt` next to the card database (the previous
version is kept as `.previous`). Claude has no tool to fetch rules.

EDHREC has no official API; the tool requests only the page for the commander
you name, the same JSON the EDHREC website loads, and caches it for 7 days in
`edhrec_cache.db` next to the card database. Repeat requests are offline, and if
EDHREC is unreachable an older cached copy is used. Claude itself never browses.

### Setup

**Packaged app:** each build also contains `MtgCardsMCP` (`MtgCardsMCP.exe` on
Windows), a small console program that serves the tools. Keep it in the same
folder as the app (on macOS, next to `MtgDeckFormatter.app`). Packaged
windowed apps can't talk to Claude Desktop directly, so Claude Desktop launches
this program instead. It reads the same local database as the app.

**From source:** `pip install mcp` in the environment you run the app with.

Then, either way:

1.  In the app, click **Update Local DB** (this also downloads the Scryfall oracle
    tags, ~6MB) or, if your card database is already current, just **Update Tags**.
    Optionally click **Update Rules**.
2.  Click **Claude Setup**, copy the config snippet into Claude Desktop's
    `claude_desktop_config.json` (Settings → Developer → Edit Config), and restart
    Claude Desktop. The `mtg-cards` tools then show up in the tools menu.

On macOS, if Claude Desktop can't start `MtgCardsMCP` because it was downloaded
from the internet, run `xattr -d com.apple.quarantine /path/to/MtgCardsMCP` once.

If HTTPS requests fail because something on your network intercepts TLS (see
the SSL notes in `api_client.py`), add
`"env": {"REQUESTS_CA_BUNDLE": "C:\\path\\to\\combined-ca-bundle.pem"}` to the
`mtg-cards` entry in the Claude Desktop config.

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

## Dependencies

-   [requests](https://pypi.org/project/requests/): For making HTTP requests to the Scryfall API.
-   Python's built-in libraries: `tkinter`, `threading`, `queue`, `re`, `textwrap`.

## Acknowledgments

This application relies entirely on the fantastic and free [Scryfall API](https://scryfall.com/docs/api) for all Magic: The Gathering card data.

## License

This project is licensed under the MIT License - see the `LICENSE` file for details.