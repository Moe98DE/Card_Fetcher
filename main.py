# main.py
from parser import parse_decklist_detailed
from api_client import lookup_card
from models import Card
from formatter import format_deck_as_text

# Example decklist with various formats
SAMPLE_DECKLIST = """
1x Sol Ring (EOE) 245
1 High Tide (FEM) 18b
1 Loki, God of Mischief (PMEI) 2026-14 *F*
3x Command Tower (TDC) 203
Lightning Bolt
6 Mountain

// This is a comment, it should be ignored
4 Counterspell
"""

def process_decklist(decklist_text: str):
    """
    The main workflow function.
    Parses a decklist, fetches data for each card, and prints the formatted result.
    """
    print("Parsing decklist...")
    parsed = parse_decklist_detailed(decklist_text)
    card_queries = parsed.cards
    skipped = list(parsed.skipped)

    if not card_queries:
        print("Decklist is empty or could not be parsed.")
        return

    print(f"Found {len(card_queries)} unique cards. Fetching data from Scryfall...")

    detailed_deck = []
    for query in card_queries:
        card_name = query['name']
        quantity = query['quantity']

        print(f"  Fetching: {card_name}")
        scryfall_json, reason = lookup_card(card_name)

        if scryfall_json:
            card_object = Card.from_scryfall_json(scryfall_json, quantity)
            detailed_deck.append(card_object)
        else:
            for line in query['lines']:
                skipped.append({'line': line, 'reason': reason or "Card not found"})

    print("\n--- Detailed Decklist ---\n")
    formatted_output = format_deck_as_text(detailed_deck)
    print(formatted_output)

    if skipped:
        print(f"\n--- Skipped {len(skipped)} line(s) ---")
        for entry in skipped:
            print(f"  {entry['line']}  —  {entry['reason']}")


if __name__ == "__main__":
    # You can replace SAMPLE_DECKLIST with input() to make it interactive
    # user_input = input("Paste your decklist here:\n")
    process_decklist(SAMPLE_DECKLIST)