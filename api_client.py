# api_client.py
import requests
import time
from typing import Dict, Optional

SCRYFALL_API_BASE = "https://api.scryfall.com"

def fetch_card_data(card_name: str) -> Optional[Dict]:
    """
    Fetches card data from the Scryfall API for a given card name.

    Uses the 'fuzzy' search to accommodate minor typos or variations.
    Returns the JSON response as a dictionary, or None if the card is not found.
    """
    # Scryfall API good practice: wait 50-100ms between requests.
    time.sleep(0.1) 
    
    url = f"{SCRYFALL_API_BASE}/cards/named"
    params = {'fuzzy': card_name}
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()  # Raises an HTTPError for bad responses (4xx or 5xx)
        return response.json()
    except requests.exceptions.HTTPError as err:
        if err.response.status_code == 404:
            print(f"Error: Card '{card_name}' not found.")
        else:
            print(f"HTTP Error fetching '{card_name}': {err}")
        return None
    except requests.exceptions.RequestException as err:
        print(f"Request Error fetching '{card_name}': {err}")
        return None