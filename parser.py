# parser.py
import re
from typing import List, Dict

def parse_decklist(decklist_text: str) -> List[Dict[str, str]]:
    """
    Parses a raw decklist string into a list of card queries.

    Handles formats like:
    - 1x Sol Ring
    - 4 Command Tower
    - Sol Ring
    - Sol Ring (SET) 123

    Returns a list of dictionaries, e.g., [{'quantity': '1', 'name': 'Sol Ring'}]
    """
    # This regex captures:
    # - Group 1 (Optional): A number, possibly followed by 'x' and whitespace.
    # - Group 2: The card name, which can contain any characters. We capture it lazily (.*?)
    # - A non-capturing group for the optional set/collector info, which we discard.
    card_pattern = re.compile(r"^\s*(\d+x?\s*)?(.*?)(?:\s\(.*\)\s*\d*)?$")
    
    parsed_cards = []
    lines = decklist_text.strip().split('\n')

    for line in lines:
        line = line.strip()
        if not line or line.startswith('//'): # Skip empty lines and comments
            continue

        match = card_pattern.match(line)
        if match:
            quantity_str = match.group(1)
            card_name = match.group(2).strip()

            if quantity_str:
                quantity = int(quantity_str.lower().replace('x', '').strip())
            else:
                quantity = 1
            
            parsed_cards.append({'quantity': quantity, 'name': card_name})
            
    return parsed_cards