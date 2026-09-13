# database.py
import sqlite3
import json
import os
import platform
from decimal import Decimal, InvalidOperation


CHEAPEST_EUR_PRICE_KEY = "_cheapest_normal_paper_eur"


def get_app_data_path():
    """
    Returns a cross-platform path to store the database.
    Windows: %APPDATA%\\MtgDeckFormatter\\
    macOS: ~/Library/Application Support/MtgDeckFormatter/
    Linux: ~/.local/share/MtgDeckFormatter/
    """
    app_name = "MtgDeckFormatter"

    if platform.system() == "Windows":
        base_path = os.getenv('APPDATA')
    elif platform.system() == "Darwin":  # macOS
        base_path = os.path.expanduser("~/Library/Application Support")
    else:  # Linux/Unix
        base_path = os.path.expanduser("~/.local/share")

    # Fallback for Linux or weird environments
    if not base_path:
        base_path = os.path.expanduser("~")

    # Construct full path
    full_path = os.path.join(base_path, app_name)

    # Ensure the directory exists
    try:
        os.makedirs(full_path, exist_ok=True)
    except OSError:
        # Absolute fallback if we can't write to system folders: use a temp folder
        import tempfile
        full_path = os.path.join(tempfile.gettempdir(), app_name)
        os.makedirs(full_path, exist_ok=True)

    return os.path.join(full_path, "scryfall_cache.db")


# Use the dynamic path instead of a relative string
DB_FILE = get_app_data_path()


def _normal_paper_eur_price(card_data: dict):
    """Return a comparable regular/nonfoil EUR paper price, or None."""
    games = card_data.get("games") or []
    if "paper" not in games:
        return None

    if card_data.get("digital") or card_data.get("oversized"):
        return None

    price = (card_data.get("prices") or {}).get("eur")
    if price in (None, ""):
        return None

    try:
        return Decimal(str(price)), str(price)
    except (InvalidOperation, ValueError, TypeError):
        return None


class CardDatabase:
    def __init__(self):
        # We also need to ensure the directory exists before connecting,
        # though get_app_data_path handles creation, it's safe to call.
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.create_table()

    def create_table(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cards (
                name_normalized TEXT PRIMARY KEY,
                data TEXT
            )
        ''')
        self.conn.commit()

    def get_card(self, card_name: str):
        """Returns the Dict (JSON) of the card if found, else None."""
        cursor = self.conn.cursor()
        # Simple normalization: lowercase and stripped
        key = card_name.lower().strip()
        cursor.execute("SELECT data FROM cards WHERE name_normalized = ?", (key,))
        row = cursor.fetchone()

        if row:
            card_data = json.loads(row[0])

            # Cache rows written by older versions contain only the price for
            # whichever printing happened to be stored.  Treat them as stale so
            # the caller refetches/re-saves the card with cheapest-print pricing.
            if CHEAPEST_EUR_PRICE_KEY not in card_data:
                return None

            return card_data
        return None

    def save_card(self, card_data: dict):
        """Saves a single card's JSON to the database."""
        if not card_data or 'name' not in card_data:
            return

        key = card_data['name'].lower().strip()
        json_str = json.dumps(card_data)

        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO cards (name_normalized, data)
            VALUES (?, ?)
        ''', (key, json_str))
        self.conn.commit()

    def bulk_import(self, cards_list, progress_callback=None):
        """
        Import Scryfall bulk data while calculating one price per card name:
        the cheapest regular/nonfoil paper copy with an EUR price.

        The database still stores one representative Scryfall object per card
        name, but every stored object receives the minimum eligible EUR price
        found across all of that card's printings.
        """
        cursor = self.conn.cursor()
        total = len(cards_list)

        # First pass: find the cheapest eligible EUR price for every card name.
        cheapest_by_name = {}
        for card in cards_list:
            name = card.get('name')
            if not name:
                continue

            price_info = _normal_paper_eur_price(card)
            if price_info is None:
                continue

            price_value, price_text = price_info
            key = name.lower().strip()
            current = cheapest_by_name.get(key)

            if current is None or price_value < current[0]:
                cheapest_by_name[key] = (price_value, price_text)

        # We use a transaction for speed.
        cursor.execute("BEGIN TRANSACTION")

        for i, card in enumerate(cards_list):
            if 'name' in card:
                key = card['name'].lower().strip()

                # Copy before adding our internal field so the downloaded list
                # remains unchanged for any other caller.
                stored_card = dict(card)
                cheapest = cheapest_by_name.get(key)
                stored_card[CHEAPEST_EUR_PRICE_KEY] = cheapest[1] if cheapest else None

                json_str = json.dumps(stored_card)
                cursor.execute(
                    "INSERT OR REPLACE INTO cards (name_normalized, data) VALUES (?, ?)",
                    (key, json_str)
                )

            if progress_callback and i % 1000 == 0:
                progress_callback(i, total)

        self.conn.commit()

        if progress_callback:
            progress_callback(total, total)

    def close(self):
        self.conn.close()
