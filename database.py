# database.py
import sqlite3
import json
import os

DB_FILE = "scryfall_cache.db"


class CardDatabase:
    def __init__(self):
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.create_table()

    def create_table(self):
        cursor = self.conn.cursor()
        # We store the normalized name (lowercase) for lookups, and the full JSON blob
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS cards
                       (
                           name_normalized
                           TEXT
                           PRIMARY
                           KEY,
                           data
                           TEXT
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
            return json.loads(row[0])
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
        Takes a list of card dictionaries (from Scryfall Bulk Data)
        and inserts them all into the DB.
        """
        cursor = self.conn.cursor()
        total = len(cards_list)

        # We use a transaction for speed
        cursor.execute("BEGIN TRANSACTION")

        for i, card in enumerate(cards_list):
            if 'name' in card:
                key = card['name'].lower().strip()
                json_str = json.dumps(card)
                cursor.execute(
                    "INSERT OR REPLACE INTO cards (name_normalized, data) VALUES (?, ?)",
                    (key, json_str)
                )

            if progress_callback and i % 1000 == 0:
                progress_callback(i, total)

        self.conn.commit()

    def close(self):
        self.conn.close()