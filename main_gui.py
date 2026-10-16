# main_gui.py
import importlib.util
import json
import os
import platform
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from api_client import fetch_bulk_data_url, download_bulk_json, fetch_card_data, lookup_card
from database import CardDatabase
from formatter import build_table_rows, format_deck_as_markdown, get_table_columns
from models import Card
from parser import parse_decklist_detailed
import search_index
import rules


MAX_PROGRESS_UPDATES = 200
TABLE_INSERT_BATCH_SIZE = 500
TREE_PREVIEW_LIMIT = 180


def _should_report_progress(current: int, total: int) -> bool:
    """Limit worker progress chatter while still keeping the UI informative."""
    if total <= 0 or current <= 1 or current >= total:
        return True
    step = max(1, (total + MAX_PROGRESS_UPDATES - 1) // MAX_PROGRESS_UPDATES)
    return current % step == 0


def _attach_available_meld_results(deck, db: CardDatabase, progress_queue: queue.Queue):
    """
    Attach a meld result only when every required meld component is in the deck.

    The result is attached to exactly one component via Card.meld_result_card.
    This keeps the user's deck count unchanged while giving the formatter enough
    information to add one synthetic result row.
    """
    deck_names = {card.name.casefold() for card in deck}
    handled_results = set()

    for card in deck:
        if not card.all_parts:
            continue

        meld_parts = [
            part.get("name")
            for part in card.all_parts
            if part.get("component") == "meld_part" and part.get("name")
        ]
        result_names = [
            part.get("name")
            for part in card.all_parts
            if part.get("component") == "meld_result" and part.get("name")
        ]

        if len(meld_parts) < 2 or not result_names:
            continue

        # Only a listed meld component should trigger automatic result handling.
        if card.name.casefold() not in {name.casefold() for name in meld_parts}:
            continue

        if not all(name.casefold() in deck_names for name in meld_parts):
            continue

        result_name = result_names[0]
        result_key = result_name.casefold()
        if result_key in handled_results or result_key in deck_names:
            handled_results.add(result_key)
            continue

        progress_queue.put(('progress', len(deck), len(deck), f"Fetching meld result: {result_name}"))

        result_json = db.get_card(result_name)
        if not result_json:
            result_json = fetch_card_data(result_name)
            if result_json:
                db.save_card(result_json)

        if result_json:
            card.meld_result_card = Card.from_scryfall_json(result_json, 1)
            handled_results.add(result_key)


def build_detailed_deck(decklist_text: str, progress_queue: queue.Queue, db: CardDatabase):
    """
    Worker: parse the decklist, resolve each card, and report the result.

    Sends ('done', deck, skipped) on success, where skipped is a list of
    {'line': ..., 'reason': ...} dicts for every line that did not make it
    into the deck. Sends ('error', message, skipped) if nothing could be used.
    """
    try:
        parsed = parse_decklist_detailed(decklist_text)
        skipped = list(parsed.skipped)
        card_queries = parsed.cards

        if not card_queries:
            progress_queue.put(('error', "Decklist is empty or could not be parsed.", skipped))
            return

        # Keyed by the resolved Scryfall name so that e.g. "Delver of Secrets"
        # and "Delver of Secrets // Insectile Aberration" become one entry.
        cards_by_name = {}
        total_cards = len(card_queries)

        for i, query in enumerate(card_queries):
            current = i + 1
            input_name = query['name']

            if _should_report_progress(current, total_cards):
                progress_queue.put(('progress', current, total_cards, f"Processing: {input_name}"))

            # 1. Try local DB.
            scryfall_json = db.get_card(input_name)
            reason = None

            # 2. Fall back to Scryfall and cache successful results.
            if not scryfall_json:
                scryfall_json, reason = lookup_card(input_name)
                if scryfall_json:
                    db.save_card(scryfall_json)

            if not scryfall_json:
                for line in query.get('lines') or [input_name]:
                    skipped.append({'line': line, 'reason': reason or "Card not found"})
                continue

            resolved_key = scryfall_json.get('name', input_name).casefold()
            existing = cards_by_name.get(resolved_key)
            if existing:
                existing.quantity += query['quantity']
            else:
                cards_by_name[resolved_key] = Card.from_scryfall_json(scryfall_json, query['quantity'])

        detailed_deck = list(cards_by_name.values())

        if not detailed_deck:
            progress_queue.put(('error', "No cards were found. Check the card names.", skipped))
            return

        # Meld results are resolved only after the whole deck is known.
        _attach_available_meld_results(detailed_deck, db, progress_queue)
        progress_queue.put(('done', detailed_deck, skipped))

    except Exception as exc:
        progress_queue.put(('error', f"Could not process the decklist:\n{exc}", []))


def update_database_logic(progress_queue: queue.Queue, db: CardDatabase):
    try:
        progress_queue.put(('status', "Fetching bulk data URL..."))
        url = fetch_bulk_data_url()

        if not url:
            progress_queue.put(('error', "Could not retrieve Scryfall Bulk Data URL."))
            return

        progress_queue.put(('status', "Downloading Database (approx 200-300MB)..."))

        def dl_callback(current, total):
            if total > 0:
                pct = int((current / total) * 100)
                progress_queue.put(('dl_progress', pct))
            else:
                mb = current / (1024 * 1024)
                progress_queue.put(('status', f"Downloading... {mb:.1f} MB"))

        data = download_bulk_json(url, progress_callback=dl_callback)

        if not data:
            progress_queue.put(('error', "Download failed or file was empty."))
            return

        progress_queue.put(('status', "Importing cards into Local DB (this may take a moment)..."))

        def import_callback(current, total):
            pct = int((current / total) * 100) if total > 0 else 100
            progress_queue.put(('db_progress', pct))

        db.bulk_import(data, progress_callback=import_callback)
        message = f"Successfully imported {len(data)} cards."

        # New: keep the Claude search index (tags + filters) in sync. Optional,
        # so a failure here never undoes the card import above.
        try:
            message += "\n" + _refresh_search_index(progress_queue, db)
        except Exception as exc:
            message += f"\n\nThe card search index could not be updated:\n{exc}"

        progress_queue.put(('done_db', message))

    except Exception as exc:
        progress_queue.put(('error', f"Could not update the local database:\n{exc}"))


def _refresh_search_index(progress_queue: queue.Queue, db: CardDatabase) -> str:
    """Download Scryfall oracle tags and rebuild the card search index."""
    def status(text):
        progress_queue.put(('status', text))

    def tag_progress(current, total):
        progress_queue.put(('db_progress', int(current * 100 / total) if total else 100))

    search_index.ensure_schema(db.conn)
    tag_count = search_index.update_tags_from_scryfall(db.conn, status=status, progress_callback=tag_progress)
    status("Building card search index...")
    indexed = search_index.rebuild_card_index(db.conn, progress_callback=tag_progress)
    return f"Search index: {indexed} cards, {tag_count} Scryfall oracle tags."


def update_tags_logic(progress_queue: queue.Queue, db: CardDatabase):
    try:
        has_cards = db.conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        if not has_cards:
            progress_queue.put(('error', "The local card database is empty.\n"
                                         "Click 'Update Local DB' first; it also installs the tags."))
            return
        progress_queue.put(('done_db', _refresh_search_index(progress_queue, db)))
    except Exception as exc:
        progress_queue.put(('error', f"Could not update the Scryfall tags:\n{exc}"))


def update_rules_logic(progress_queue: queue.Queue, db: CardDatabase, url: str = None, path: str = None):
    """Download (or import) the Comprehensive Rules. Never exposed to Claude."""
    try:
        def status(text):
            progress_queue.put(('status', text))

        if path:
            status("Importing rules file...")
            info = rules.import_file(db.conn, path)
        else:
            info = rules.update_from_url(db.conn, url or None, status=status)
        progress_queue.put(('done_db', f"Comprehensive Rules effective {info['rules_effective']} installed: "
                                       f"{info['rules_count']} rules, {info['glossary_count']} glossary terms."))
    except Exception as exc:
        progress_queue.put(('error', f"Could not update the Comprehensive Rules:\n{exc}"))


def claude_desktop_config_path() -> str:
    if platform.system() == "Windows":
        return os.path.join(os.getenv("APPDATA", "~"), "Claude", "claude_desktop_config.json")
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Claude/claude_desktop_config.json")
    return os.path.expanduser("~/.config/Claude/claude_desktop_config.json")


def claude_desktop_snippet() -> str:
    server = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")
    entry = {"mcpServers": {"mtg-cards": {"command": sys.executable, "args": [server]}}}
    return json.dumps(entry, indent=2)


class MtgDeckFormatterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("MTG Deck Formatter (Local DB Support)")
        self.root.geometry("1350x820")
        self.root.minsize(950, 650)

        self.db = CardDatabase()
        self.show_price_var = tk.BooleanVar(value=True)
        self.show_rarity_var = tk.BooleanVar(value=True)

        self.comm_queue = queue.Queue()
        self.worker_thread = None
        self.current_deck = []
        self.skipped_cards = []
        self.table_rows = []
        self._table_render_generation = 0
        self.create_widgets()

    def create_widgets(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Deck.Treeview", rowheight=26)
        style.configure("Deck.Treeview.Heading", font=("TkDefaultFont", 10, "bold"))

        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        input_frame = ttk.LabelFrame(main_frame, text="Paste Decklist Here", padding="10")
        input_frame.pack(fill=tk.X, pady=5)

        self.input_text = scrolledtext.ScrolledText(input_frame, wrap=tk.WORD, width=60, height=8)
        self.input_text.pack(fill=tk.X, expand=True)

        controls_frame = ttk.Frame(main_frame)
        controls_frame.pack(fill=tk.X, pady=5)

        self.process_button = ttk.Button(
            controls_frame,
            text="Process Decklist",
            command=self.start_processing_thread,
        )
        self.process_button.pack(side=tk.LEFT, padx=5)

        self.copy_button = ttk.Button(controls_frame, text="Copy Markdown", command=self.copy_to_clipboard)
        self.copy_button.pack(side=tk.LEFT, padx=5)

        self.clear_button = ttk.Button(controls_frame, text="Clear", command=self.clear_fields)
        self.clear_button.pack(side=tk.LEFT, padx=5)

        options_frame = ttk.Frame(controls_frame)
        options_frame.pack(side=tk.LEFT, padx=15)

        chk_price = ttk.Checkbutton(
            options_frame,
            text="Show Price",
            variable=self.show_price_var,
            command=self.refresh_output_table,
        )
        chk_price.pack(side=tk.LEFT, padx=5)

        chk_rarity = ttk.Checkbutton(
            options_frame,
            text="Show Rarity",
            variable=self.show_rarity_var,
            command=self.refresh_output_table,
        )
        chk_rarity.pack(side=tk.LEFT, padx=5)

        self.claude_button = ttk.Button(controls_frame, text="Claude Setup", command=self.show_claude_setup)
        self.claude_button.pack(side=tk.RIGHT, padx=5)

        self.rules_button = ttk.Button(controls_frame, text="Update Rules", command=self.show_rules_dialog)
        self.rules_button.pack(side=tk.RIGHT, padx=5)

        self.tags_button = ttk.Button(controls_frame, text="Update Tags", command=self.start_tags_update_thread)
        self.tags_button.pack(side=tk.RIGHT, padx=5)

        self.db_button = ttk.Button(controls_frame, text="Update Local DB", command=self.start_db_update_thread)
        self.db_button.pack(side=tk.RIGHT, padx=5)

        self.status_label = ttk.Label(controls_frame, text="")
        self.status_label.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=10)

        progress_frame = ttk.Frame(main_frame)
        progress_frame.pack(fill=tk.X, pady=5)
        self.progress_bar = ttk.Progressbar(progress_frame, mode='determinate', maximum=100, value=0)
        self.progress_bar.pack(fill=tk.X, expand=True)

        output_frame = ttk.LabelFrame(main_frame, text="Deck Table", padding="10")
        output_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        output_frame.rowconfigure(0, weight=4)
        output_frame.rowconfigure(2, weight=1)
        output_frame.columnconfigure(0, weight=1)

        tree_frame = ttk.Frame(output_frame)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)

        self.output_tree = ttk.Treeview(
            tree_frame,
            show="headings",
            style="Deck.Treeview",
            selectmode="browse",
        )
        self.output_tree.grid(row=0, column=0, sticky="nsew")
        self.output_tree.bind("<<TreeviewSelect>>", self._on_table_selection)

        y_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.output_tree.yview)
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.output_tree.xview)
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.output_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        # A Treeview uses one widget for the entire table rather than one widget
        # per cell. This avoids huge X11 widget/pixmap allocations on Linux.
        self.output_tree.tag_configure("meld_result", font=("TkDefaultFont", 10, "italic"))

        ttk.Label(output_frame, text="Selected Card Details", font=("TkDefaultFont", 10, "bold")).grid(
            row=1, column=0, sticky="w", pady=(8, 3)
        )
        self.detail_text = scrolledtext.ScrolledText(output_frame, wrap=tk.WORD, height=7, state=tk.DISABLED)
        self.detail_text.grid(row=2, column=0, sticky="nsew")

        self._configure_tree_columns()
        self._set_detail_text("Process a decklist to display the table here.")

    def _clear_comm_queue(self):
        while True:
            try:
                self.comm_queue.get_nowait()
            except queue.Empty:
                break

    def _configure_tree_columns(self):
        columns = get_table_columns(
            show_price=self.show_price_var.get(),
            show_rarity=self.show_rarity_var.get(),
        )
        column_keys = [key for key, _heading, _width in columns]
        self.output_tree.configure(columns=column_keys)

        for key, heading, width in columns:
            self.output_tree.heading(key, text=heading)
            anchor = tk.CENTER if key in {"quantity", "price"} else tk.W
            self.output_tree.column(
                key,
                width=width,
                minwidth=max(45, min(width, 100)),
                anchor=anchor,
                stretch=False,
            )

        return columns

    @staticmethod
    def _tree_cell_text(value):
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
        text = " | ".join(part.strip() for part in text.split("\n") if part.strip())
        if len(text) > TREE_PREVIEW_LIMIT:
            return text[:TREE_PREVIEW_LIMIT - 1].rstrip() + "…"
        return text

    def _set_detail_text(self, text):
        self.detail_text.config(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert("1.0", text)
        self.detail_text.config(state=tk.DISABLED)

    def _on_table_selection(self, _event=None):
        selection = self.output_tree.selection()
        if not selection:
            return

        item_id = selection[0]
        try:
            row_index = int(item_id.rsplit("-", 1)[1])
            row = self.table_rows[row_index]
        except (ValueError, IndexError):
            return

        details = []
        for key, heading, _width in get_table_columns(show_price=True, show_rarity=True):
            value = row.get(key, "")
            if value in (None, ""):
                value = "—"
            details.append(f"{heading}: {value}")

        self._set_detail_text("\n\n".join(details))

    def start_processing_thread(self):
        decklist = self.input_text.get("1.0", tk.END)
        if not decklist.strip():
            messagebox.showwarning("Input Required", "Please paste a decklist before processing.")
            return

        self._clear_comm_queue()
        self.process_button.config(state=tk.DISABLED)
        self.db_button.config(state=tk.DISABLED)
        self.clear_fields(output_only=True)
        self.progress_bar['value'] = 0

        self.worker_thread = threading.Thread(
            target=build_detailed_deck,
            args=(decklist, self.comm_queue, self.db),
            daemon=True,
        )
        self.worker_thread.start()
        self.root.after(25, self.check_queue)

    def start_db_update_thread(self):
        confirm = messagebox.askyesno(
            "Update Database",
            "This will download ~250MB of data from Scryfall.\nIt may take a minute or two.\nContinue?",
        )
        if not confirm:
            return

        self._clear_comm_queue()
        self.db_button.config(state=tk.DISABLED)
        self.tags_button.config(state=tk.DISABLED)
        self.process_button.config(state=tk.DISABLED)
        self.progress_bar['value'] = 0

        self.worker_thread = threading.Thread(
            target=update_database_logic,
            args=(self.comm_queue, self.db),
            daemon=True,
        )
        self.worker_thread.start()
        self.root.after(25, self.check_queue)

    def start_tags_update_thread(self):
        confirm = messagebox.askyesno(
            "Update Tags",
            "This downloads the Scryfall Tagger oracle tags (~6MB) and rebuilds the\n"
            "card search index Claude uses. Continue?",
        )
        if not confirm:
            return

        self._clear_comm_queue()
        self.db_button.config(state=tk.DISABLED)
        self.tags_button.config(state=tk.DISABLED)
        self.process_button.config(state=tk.DISABLED)
        self.progress_bar['value'] = 0

        self.worker_thread = threading.Thread(
            target=update_tags_logic,
            args=(self.comm_queue, self.db),
            daemon=True,
        )
        self.worker_thread.start()
        self.root.after(25, self.check_queue)

    def _start_worker(self, target, *args):
        self._clear_comm_queue()
        for button in (self.db_button, self.tags_button, self.rules_button, self.process_button):
            button.config(state=tk.DISABLED)
        self.progress_bar['value'] = 0
        self.worker_thread = threading.Thread(target=target, args=(self.comm_queue, self.db) + args, daemon=True)
        self.worker_thread.start()
        self.root.after(25, self.check_queue)

    def show_rules_dialog(self):
        """Download the latest Comprehensive Rules, a specific URL, or import a local .txt."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Comprehensive Rules")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        try:
            meta = search_index.get_meta(self.db.conn)
        except Exception:
            meta = {}
        current = (f"Installed: effective {meta['rules_effective']} ({meta.get('rules_count')} rules)"
                   if meta.get("rules_count") else "Installed: none")
        ttk.Label(frame, text=current).pack(anchor="w")
        ttk.Label(frame, text=("Leave the URL empty to find the newest .txt on magic.wizards.com/rules,\n"
                               "or paste a direct link to a MagicCompRules .txt file."),
                  justify=tk.LEFT).pack(anchor="w", pady=(10, 4))
        url_var = tk.StringVar()
        ttk.Entry(frame, textvariable=url_var, width=80).pack(fill=tk.X)

        def download():
            dialog.destroy()
            self._start_worker(update_rules_logic, url_var.get().strip() or None, None)

        def import_local():
            path = filedialog.askopenfilename(parent=dialog, title="Choose the Comprehensive Rules .txt",
                                              filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
            if path:
                dialog.destroy()
                self._start_worker(update_rules_logic, None, path)

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(row, text="Download", command=download).pack(side=tk.LEFT)
        ttk.Button(row, text="Import File...", command=import_local).pack(side=tk.LEFT, padx=6)
        ttk.Button(row, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT)

    def show_claude_setup(self):
        """Show how to connect Claude Desktop to the local card search (MCP)."""
        dialog = tk.Toplevel(self.root)
        dialog.title("Connect Claude Desktop")
        dialog.transient(self.root)
        dialog.geometry("760x560")

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=tk.BOTH, expand=True)

        try:
            meta = search_index.get_meta(self.db.conn)
        except Exception:
            meta = {}
        problems = []
        if getattr(sys, "frozen", False):
            problems.append("You are running the packaged app. Claude Desktop needs to run "
                            "mcp_server.py with Python from a source checkout of this project.")
        if importlib.util.find_spec("mcp") is None:
            problems.append(f"The 'mcp' package is not installed for this Python. Run:\n"
                            f"  \"{sys.executable}\" -m pip install mcp")
        if not meta.get("indexed_cards"):
            problems.append("The search index is empty: click 'Update Local DB' (or 'Update Tags').")
        elif not meta.get("tag_count"):
            problems.append("No Scryfall tags installed yet: click 'Update Tags'.")

        status = (f"Search index: {meta.get('indexed_cards', 0)} cards, "
                  f"{meta.get('tag_count', 0)} oracle tags.")
        ttk.Label(frame, text=status).pack(anchor="w")
        for problem in problems:
            ttk.Label(frame, text="⚠ " + problem, foreground="#b45309", wraplength=720,
                      justify=tk.LEFT).pack(anchor="w", pady=(6, 0))

        steps = (
            "1. Open Claude Desktop → Settings → Developer → Edit Config (or open the file below).\n"
            f"   {claude_desktop_config_path()}\n"
            "2. Merge this into the file (keep any other servers under \"mcpServers\").\n"
            "3. Restart Claude Desktop. The 'mtg-cards' tools appear in the tools menu.\n"
            "   Keep this app's database updated; Claude only reads it, never the web."
        )
        ttk.Label(frame, text=steps, justify=tk.LEFT).pack(anchor="w", pady=(12, 6))

        snippet = claude_desktop_snippet()
        box = scrolledtext.ScrolledText(frame, height=10, wrap=tk.NONE)
        box.insert("1.0", snippet)
        box.config(state=tk.DISABLED)
        box.pack(fill=tk.BOTH, expand=True)

        def copy_snippet():
            self.root.clipboard_clear()
            self.root.clipboard_append(snippet)
            copy_btn.config(text="Copied!")
            dialog.after(1500, lambda: copy_btn.config(text="Copy Config"))

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, pady=(8, 0))
        copy_btn = ttk.Button(row, text="Copy Config", command=copy_snippet)
        copy_btn.pack(side=tk.LEFT)
        ttk.Button(row, text="Close", command=dialog.destroy).pack(side=tk.RIGHT)

    def _display_transient_message(self, message):
        msg_type = message[0]

        if msg_type == 'progress':
            current, total, name = message[1], message[2], message[3]
            self.progress_bar['value'] = (current / total) * 100 if total else 0
            self.status_label.config(text=f"Processing ({current}/{total}): {name}")
        elif msg_type == 'status':
            self.status_label.config(text=message[1])
        elif msg_type == 'dl_progress':
            self.status_label.config(text=f"Downloading... {message[1]}%")
            self.progress_bar['value'] = message[1]
        elif msg_type == 'db_progress':
            self.status_label.config(text=f"Importing... {message[1]}%")
            self.progress_bar['value'] = message[1]

    def _handle_terminal_message(self, message):
        msg_type = message[0]

        if msg_type == 'done':
            self.current_deck = message[1]
            self.skipped_cards = message[2] if len(message) > 2 else []
            self.table_rows = build_table_rows(self.current_deck)
            self.reset_ui_state()
            self.refresh_output_table()
            if self.skipped_cards:
                self.show_skipped_dialog(self.skipped_cards)
        elif msg_type == 'done_db':
            self.reset_ui_state()
            self.status_label.config(text=message[1])
            messagebox.showinfo("Success", message[1])
        elif msg_type == 'error':
            self.reset_ui_state()
            self.status_label.config(text="Error occurred.")
            messagebox.showerror("Error", message[1])
            skipped = message[2] if len(message) > 2 else []
            if skipped:
                self.skipped_cards = skipped
                self.show_skipped_dialog(skipped)

    def _completion_status_text(self):
        text = f"Processing complete — {len(self.current_deck)} unique cards."
        if self.skipped_cards:
            count = len(self.skipped_cards)
            text += f" {count} line{'s' if count != 1 else ''} skipped."
        return text

    def show_skipped_dialog(self, skipped):
        """Show every skipped decklist line with the reason it was skipped."""
        dialog = tk.Toplevel(self.root)
        count = len(skipped)
        dialog.title(f"{count} card{'s' if count != 1 else ''} skipped")
        dialog.transient(self.root)
        dialog.geometry("640x360")
        dialog.minsize(420, 220)

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text=f"These {count} line{'s were' if count != 1 else ' was'} not added to the deck:",
            font=("TkDefaultFont", 10, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        report = "\n".join(f"{entry['line']}  —  {entry['reason']}" for entry in skipped)

        text_box = scrolledtext.ScrolledText(frame, wrap=tk.WORD, height=10)
        text_box.insert("1.0", report)
        text_box.config(state=tk.DISABLED)
        text_box.pack(fill=tk.BOTH, expand=True)

        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X, pady=(8, 0))

        copy_lines = "\n".join(entry['line'] for entry in skipped)

        def copy_skipped():
            self.root.clipboard_clear()
            self.root.clipboard_append(copy_lines)
            copy_btn.config(text="Copied!")
            dialog.after(1500, lambda: copy_btn.winfo_exists() and copy_btn.config(text="Copy Skipped Lines"))

        copy_btn = ttk.Button(button_row, text="Copy Skipped Lines", command=copy_skipped)
        copy_btn.pack(side=tk.LEFT)
        close_btn = ttk.Button(button_row, text="Close", command=dialog.destroy)
        close_btn.pack(side=tk.RIGHT)

        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        close_btn.focus_set()

        # Center over the main window.
        dialog.update_idletasks()
        width, height = 640, 360
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 3
        dialog.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def check_queue(self):
        """Drain pending worker messages and coalesce stale progress updates."""
        latest_transient = None
        terminal_message = None

        while True:
            try:
                message = self.comm_queue.get_nowait()
            except queue.Empty:
                break

            if message[0] in {'done', 'done_db', 'error'}:
                terminal_message = message
            else:
                latest_transient = message

        if terminal_message is not None:
            self._handle_terminal_message(terminal_message)
        elif latest_transient is not None:
            self._display_transient_message(latest_transient)

        worker_running = self.worker_thread is not None and self.worker_thread.is_alive()
        if worker_running or not self.comm_queue.empty():
            self.root.after(25, self.check_queue)

    def reset_ui_state(self):
        self.process_button.config(state=tk.NORMAL)
        self.db_button.config(state=tk.NORMAL)
        for name in ("tags_button", "rules_button"):
            if hasattr(self, name):
                getattr(self, name).config(state=tk.NORMAL)
        self.progress_bar['value'] = 100

    def refresh_output_table(self):
        if not hasattr(self, "output_tree"):
            return

        self._table_render_generation += 1
        generation = self._table_render_generation

        existing = self.output_tree.get_children()
        if existing:
            self.output_tree.delete(*existing)

        columns = self._configure_tree_columns()
        self._set_detail_text(
            "Select a row to see the complete card data."
            if self.table_rows
            else "Process a decklist to display the table here."
        )

        if not self.table_rows:
            return

        self.output_tree.xview_moveto(0)
        self.output_tree.yview_moveto(0)
        self._insert_table_rows(generation, columns, 0)

    def _insert_table_rows(self, generation, columns, start_index):
        """Insert Treeview rows in batches so large decks keep the UI responsive."""
        if generation != self._table_render_generation:
            return

        end_index = min(start_index + TABLE_INSERT_BATCH_SIZE, len(self.table_rows))
        for row_index in range(start_index, end_index):
            row = self.table_rows[row_index]
            values = [self._tree_cell_text(row.get(key, "")) for key, _heading, _width in columns]
            tags = ("meld_result",) if row.get("_row_kind") == "meld_result" else ()
            self.output_tree.insert(
                "",
                tk.END,
                iid=f"row-{row_index}",
                values=values,
                tags=tags,
            )

        if end_index < len(self.table_rows):
            self.status_label.config(text=f"Rendering table... {end_index}/{len(self.table_rows)} rows")
            self.root.after_idle(self._insert_table_rows, generation, columns, end_index)
        else:
            self.status_label.config(text=self._completion_status_text())

    def copy_to_clipboard(self):
        if not self.current_deck:
            return

        markdown = format_deck_as_markdown(
            self.current_deck,
            show_price=self.show_price_var.get(),
            show_rarity=self.show_rarity_var.get(),
        )
        self.root.clipboard_clear()
        self.root.clipboard_append(markdown)

        original_text = self.copy_button.cget("text")
        self.copy_button.config(text="Copied Markdown!")
        self.root.after(1500, lambda: self.copy_button.config(text=original_text))

    def clear_fields(self, output_only=False):
        if not output_only:
            self.input_text.delete("1.0", tk.END)

        self.current_deck = []
        self.skipped_cards = []
        self.table_rows = []
        if hasattr(self, "output_tree"):
            self.refresh_output_table()

        self.progress_bar['value'] = 0
        self.status_label.config(text="")

    def on_close(self):
        if self.db:
            self.db.close()
        self.root.destroy()


if __name__ == "__main__":
    app_root = tk.Tk()
    app = MtgDeckFormatterApp(app_root)
    app_root.protocol("WM_DELETE_WINDOW", app.on_close)
    app_root.mainloop()
