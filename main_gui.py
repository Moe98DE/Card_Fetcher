# main_gui.py
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import queue

from parser import parse_decklist
from api_client import fetch_card_data, fetch_bulk_data_url, download_bulk_json  # Updated import
from models import Card
from formatter import format_deck_as_text
from database import CardDatabase  # New import


# --- Updated Worker Function ---
def build_detailed_deck(decklist_text: str, progress_queue: queue.Queue, db: CardDatabase, show_price: bool,
                        show_rarity: bool):
    card_queries = parse_decklist(decklist_text)
    if not card_queries:
        progress_queue.put(('done', "Decklist is empty or could not be parsed."))
        return

    detailed_deck = []
    processed_card_names = set()
    total_cards = len(card_queries)

    for i, query in enumerate(card_queries):
        input_name = query['name']

        if input_name.lower() in {name.lower() for name in processed_card_names}:
            progress_queue.put(('progress', i + 1, total_cards, f"Skipping {input_name} (already handled)"))
            continue

        progress_queue.put(('progress', i + 1, total_cards, f"Processing: {input_name}"))

        # 1. Try DB
        scryfall_json = db.get_card(input_name)

        # 2. Try API
        if not scryfall_json:
            scryfall_json = fetch_card_data(input_name)
            if scryfall_json:
                db.save_card(scryfall_json)

        if scryfall_json:
            card_object = Card.from_scryfall_json(scryfall_json, query['quantity'])

            # Meld Logic (Identical to previous logic)
            if card_object.all_parts:
                is_part = any(part['component'] == 'meld_part' and part['name'] == card_object.name for part in
                              card_object.all_parts)
                if is_part:
                    try:
                        result_name = next(p['name'] for p in card_object.all_parts if p['component'] == 'meld_result')
                        result_json = db.get_card(result_name)
                        if not result_json:
                            result_json = fetch_card_data(result_name)
                            if result_json: db.save_card(result_json)

                        if result_json:
                            card_object.meld_result_card = Card.from_scryfall_json(result_json, 1)
                    except StopIteration:
                        pass

            detailed_deck.append(card_object)
            processed_card_names.add(card_object.name)

    if not detailed_deck:
        progress_queue.put(('done', "No cards were found. Check the card names."))
        return

    # --- UPDATED: Pass flags to formatter ---
    final_output = format_deck_as_text(detailed_deck, show_price=show_price, show_rarity=show_rarity)
    progress_queue.put(('done', final_output))


def update_database_logic(progress_queue: queue.Queue, db: CardDatabase):
    progress_queue.put(('status', "Fetching bulk data URL..."))
    url = fetch_bulk_data_url()

    if not url:
        progress_queue.put(('error', "Could not retrieve Scryfall Bulk Data URL."))
        return

    progress_queue.put(('status', "Downloading Database (approx 200-300MB)..."))

    # --- FIX START: Safe Division Logic ---
    def dl_callback(current, total):
        if total > 0:
            pct = int((current / total) * 100)
            progress_queue.put(('dl_progress', pct))
        else:
            # If we don't know the total size, show MB downloaded in the status text
            # We use a special -1 signal or just update status directly
            mb = current / (1024 * 1024)
            progress_queue.put(('status', f"Downloading... {mb:.1f} MB"))
            # We keep the bar at 0 or pulsing mode (optional), here we just leave it

    data = download_bulk_json(url, progress_callback=dl_callback)

    if not data:
        progress_queue.put(('error', "Download failed or file was empty."))
        return

    progress_queue.put(('status', "Importing cards into Local DB (this may take a moment)..."))

    def import_callback(current, total):
        # Total here comes from len(list), so it should always be safe,
        # but good practice to protect it too.
        if total > 0:
            pct = int((current / total) * 100)
        else:
            pct = 100
        progress_queue.put(('db_progress', pct))

    db.bulk_import(data, progress_callback=import_callback)

    progress_queue.put(('done_db', f"Successfully imported {len(data)} cards."))

class MtgDeckFormatterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("MTG Deck Formatter (Local DB Support)")
        self.root.geometry("900x700")

        self.db = CardDatabase()

        # --- NEW: Boolean Variables for Checkboxes ---
        self.show_price_var = tk.BooleanVar(value=True)
        self.show_rarity_var = tk.BooleanVar(value=True)

        self.comm_queue = queue.Queue()
        self.create_widgets()

    def create_widgets(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")

        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        input_frame = ttk.LabelFrame(main_frame, text="Paste Decklist Here", padding="10")
        input_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.input_text = scrolledtext.ScrolledText(input_frame, wrap=tk.WORD, width=60, height=10)
        self.input_text.pack(fill=tk.BOTH, expand=True)

        controls_frame = ttk.Frame(main_frame)
        controls_frame.pack(fill=tk.X, pady=5)

        self.process_button = ttk.Button(controls_frame, text="Process Decklist",
                                             command=self.start_processing_thread)
        self.process_button.pack(side=tk.LEFT, padx=5)

        self.copy_button = ttk.Button(controls_frame, text="Copy Output", command=self.copy_to_clipboard)
        self.copy_button.pack(side=tk.LEFT, padx=5)

        self.clear_button = ttk.Button(controls_frame, text="Clear", command=self.clear_fields)
        self.clear_button.pack(side=tk.LEFT, padx=5)

            # --- NEW: Checkboxes for Options ---
            # We put them in a small frame to keep them tidy
        options_frame = ttk.Frame(controls_frame)
        options_frame.pack(side=tk.LEFT, padx=15)

        chk_price = ttk.Checkbutton(options_frame, text="Show Price", variable=self.show_price_var)
        chk_price.pack(side=tk.LEFT, padx=5)

        chk_rarity = ttk.Checkbutton(options_frame, text="Show Rarity", variable=self.show_rarity_var)
        chk_rarity.pack(side=tk.LEFT, padx=5)

        self.db_button = ttk.Button(controls_frame, text="Update Local DB", command=self.start_db_update_thread)
        self.db_button.pack(side=tk.RIGHT, padx=5)

        self.status_label = ttk.Label(controls_frame, text="")
        self.status_label.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=10)

        progress_frame = ttk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=5)
        self.progress_bar = ttk.Progressbar(progress_frame, mode='determinate', maximum=100, value=0)
        self.progress_bar.pack(fill=tk.X, expand=True)

        output_frame = ttk.LabelFrame(main_frame, text="Detailed Output", padding="10")
        output_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        self.output_text = scrolledtext.ScrolledText(
            output_frame, wrap=tk.WORD, state=tk.DISABLED, font=("Consolas", 10)
        )
        self.output_text.pack(fill=tk.BOTH, expand=True)

    def start_processing_thread(self):
        decklist = self.input_text.get("1.0", tk.END)
        if not decklist.strip():
            messagebox.showwarning("Input Required", "Please paste a decklist before processing.")
            return

            # --- NEW: Capture values of checkboxes ---
        show_price = self.show_price_var.get()
        show_rarity = self.show_rarity_var.get()

        self.process_button.config(state=tk.DISABLED)
        self.db_button.config(state=tk.DISABLED)
        self.clear_fields(output_only=True)
        self.progress_bar['value'] = 0

        self.worker_thread = threading.Thread(
            target=build_detailed_deck,
            args=(decklist, self.comm_queue, self.db, show_price, show_rarity)  # Pass them here
        )
        self.worker_thread.start()
        self.root.after(100, self.check_queue)

    def start_db_update_thread(self):
        confirm = messagebox.askyesno(
            "Update Database",
            "This will download ~250MB of data from Scryfall.\nIt may take a minute or two.\nContinue?"
        )
        if not confirm: return

        self.db_button.config(state=tk.DISABLED)
        self.process_button.config(state=tk.DISABLED)
        self.progress_bar['value'] = 0

        self.worker_thread = threading.Thread(
            target=update_database_logic,
            args=(self.comm_queue, self.db)
        )
        self.worker_thread.start()
        self.root.after(100, self.check_queue)

    def check_queue(self):
        try:
            message = self.comm_queue.get(block=False)
            msg_type = message[0]

            # --- Deck Processing Messages ---
            if msg_type == 'progress':
                current, total, name = message[1], message[2], message[3]
                self.progress_bar['value'] = (current / total) * 100
                self.status_label.config(text=f"Processing ({current}/{total}): {name}...")
                self.root.after(100, self.check_queue)
            elif msg_type == 'done':
                result = message[1]
                self.reset_ui_state()
                self.status_label.config(text="Processing complete!")
                self.update_output(result)

            # --- DB Update Messages ---
            elif msg_type == 'status':
                self.status_label.config(text=message[1])
                self.root.after(100, self.check_queue)
            elif msg_type == 'dl_progress':
                self.status_label.config(text=f"Downloading... {message[1]}%")
                self.progress_bar['value'] = message[1]
                self.root.after(100, self.check_queue)
            elif msg_type == 'db_progress':
                self.status_label.config(text=f"Importing... {message[1]}%")
                self.progress_bar['value'] = message[1]
                self.root.after(100, self.check_queue)
            elif msg_type == 'done_db':
                self.reset_ui_state()
                self.status_label.config(text=message[1])
                messagebox.showinfo("Success", message[1])
            elif msg_type == 'error':
                self.reset_ui_state()
                self.status_label.config(text="Error occurred.")
                messagebox.showerror("Error", message[1])

        except queue.Empty:
            self.root.after(100, self.check_queue)

    def reset_ui_state(self):
        self.process_button.config(state=tk.NORMAL)
        self.db_button.config(state=tk.NORMAL)
        self.progress_bar['value'] = 100

    def update_output(self, text):
        self.output_text.config(state=tk.NORMAL)
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert(tk.END, text)
        self.output_text.config(state=tk.DISABLED)

    def copy_to_clipboard(self):
        output_content = self.output_text.get("1.0", tk.END)
        if output_content.strip():
            self.root.clipboard_clear()
            self.root.clipboard_append(output_content)
            original_text = self.copy_button.cget("text")
            self.copy_button.config(text="Copied!")
            self.root.after(1500, lambda: self.copy_button.config(text=original_text))

    def clear_fields(self, output_only=False):
        if not output_only:
            self.input_text.delete("1.0", tk.END)
        self.update_output("")
        self.progress_bar['value'] = 0
        self.status_label.config(text="")

    def on_close(self):
        # Close DB connection properly
        if self.db:
            self.db.close()
        self.root.destroy()


if __name__ == "__main__":
    app_root = tk.Tk()
    app = MtgDeckFormatterApp(app_root)
    app_root.protocol("WM_DELETE_WINDOW", app.on_close)
    app_root.mainloop()