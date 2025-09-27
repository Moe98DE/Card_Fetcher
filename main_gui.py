# main_gui.py
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import queue

from parser import parse_decklist
from api_client import fetch_card_data
from models import Card
from formatter import format_deck_as_text

def build_detailed_deck(decklist_text: str, progress_queue: queue.Queue):
    """
    This function contains the core logic. It now correctly handles lists
    with multiple meld parts by processing each one individually.
    """
    card_queries = parse_decklist(decklist_text)
    if not card_queries:
        progress_queue.put(('done', "Decklist is empty or could not be parsed."))
        return

    detailed_deck = []
    processed_card_names = set()
    total_cards = len(card_queries)
    
    for i, query in enumerate(card_queries):
        card_name = query['name']
        
        # This check is now only for exact duplicates in the input list, which is fine.
        if card_name.lower() in {name.lower() for name in processed_card_names}:
            progress_queue.put(('progress', i + 1, total_cards, f"Skipping {card_name} (already handled)"))
            continue

        progress_queue.put(('progress', i + 1, total_cards, card_name))
        
        scryfall_json = fetch_card_data(card_name)
        
        if scryfall_json:
            card_object = Card.from_scryfall_json(scryfall_json, query['quantity'])
            
            if card_object.all_parts:
                is_part = any(part['component'] == 'meld_part' and part['name'] == card_object.name for part in card_object.all_parts)
                if is_part:
                    try:
                        result_name = next(p['name'] for p in card_object.all_parts if p['component'] == 'meld_result')
                        result_json = fetch_card_data(result_name)
                        if result_json:
                            result_card_object = Card.from_scryfall_json(result_json, 1)
                            card_object.meld_result_card = result_card_object
                    except StopIteration:
                        pass

            detailed_deck.append(card_object)
            
            # --- THE FIX ---
            # We ONLY add the card we just processed. We no longer add its partners.
            processed_card_names.add(card_object.name)
            # The aggressive loop that added all parts has been removed.

    if not detailed_deck:
        progress_queue.put(('done', "No cards were found. Check the card names."))
        return
    
    final_output = format_deck_as_text(detailed_deck)
    progress_queue.put(('done', final_output))


class MtgDeckFormatterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("MTG Deck Formatter")
        self.root.geometry("900x700")

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

        self.process_button = ttk.Button(controls_frame, text="Process Decklist", command=self.start_processing_thread)
        self.process_button.pack(side=tk.LEFT, padx=5)

        self.copy_button = ttk.Button(controls_frame, text="Copy Output", command=self.copy_to_clipboard)
        self.copy_button.pack(side=tk.LEFT, padx=5)

        # NEW: Clear button
        self.clear_button = ttk.Button(controls_frame, text="Clear", command=self.clear_fields)
        self.clear_button.pack(side=tk.LEFT, padx=5)
        
        # This Label will show the current status (e.g., "Fetching Sol Ring...")
        self.status_label = ttk.Label(controls_frame, text="")
        self.status_label.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=10)

        # Progress Bar Frame (to contain the bar)
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

        self.process_button.config(state=tk.DISABLED)
        self.clear_fields(output_only=True) # Clear previous results
        self.progress_bar['value'] = 0

        self.worker_thread = threading.Thread(
            target=build_detailed_deck,
            args=(decklist, self.comm_queue)
        )
        self.worker_thread.start()
        self.root.after(100, self.check_queue)

    def check_queue(self):
        try:
            message = self.comm_queue.get(block=False)
            msg_type = message[0]
            
            if msg_type == 'progress':
                current, total, name = message[1], message[2], message[3]
                self.progress_bar['value'] = (current / total) * 100
                self.status_label.config(text=f"Fetching ({current}/{total}): {name}...")
                self.root.after(100, self.check_queue) # Continue checking
            elif msg_type == 'done':
                result = message[1]
                self.process_button.config(state=tk.NORMAL)
                self.progress_bar['value'] = 100
                self.status_label.config(text="Processing complete!")
                self.update_output(result)
        except queue.Empty:
            self.root.after(100, self.check_queue) # Continue checking

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

if __name__ == "__main__":
    app_root = tk.Tk()
    app = MtgDeckFormatterApp(app_root)
    app_root.mainloop()