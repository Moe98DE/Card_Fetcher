# api_client.py
import json

import requests
import time
from typing import Dict, Optional, List

SCRYFALL_API_BASE = "https://api.scryfall.com"


def fetch_card_data(card_name: str) -> Optional[Dict]:
    """
    Fetches card data from the Scryfall API for a given card name.
    """
    time.sleep(0.1)  # Rate limit

    url = f"{SCRYFALL_API_BASE}/cards/named"
    params = {'fuzzy': card_name}

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
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


# --- NEW FUNCTIONS BELOW ---

def fetch_bulk_data_url() -> Optional[str]:
    """
    Queries Scryfall to get the download URL for 'Default Cards' (includes prices/images).
    """
    try:
        response = requests.get(f"{SCRYFALL_API_BASE}/bulk-data")
        response.raise_for_status()
        data = response.json()

        # We look for the "default_cards" type
        for item in data.get('data', []):
            if item.get('type') == 'default_cards':
                return item.get('download_uri')
    except Exception as e:
        print(f"Error fetching bulk data meta: {e}")
    return None


def download_bulk_json(download_url: str, progress_callback=None) -> Optional[List[Dict]]:
    try:
        with requests.get(download_url, stream=True) as r:
            r.raise_for_status()
            total_length = int(r.headers.get('content-length', 0))
            
            downloaded = 0
            chunks = []
            
            # Helper to throttle updates
            last_reported_percent = -1
            
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    
                    if progress_callback:
                        # Only callback if we have a total_length
                        if total_length > 0:
                            current_percent = int((downloaded / total_length) * 100)
                            # Only update if the integer percentage changed (0, 1, 2...)
                            if current_percent > last_reported_percent:
                                progress_callback(downloaded, total_length)
                                last_reported_percent = current_percent
                        else:
                            # If no total length, throttle by size (e.g., every 1MB)
                            # 1024 * 1024 = 1MB
                            if downloaded % (1024 * 1024) < 8192: 
                                progress_callback(downloaded, total_length)

            full_content = b"".join(chunks)
            return json.loads(full_content)
            
    except Exception as e:
        print(f"Error downloading bulk file: {e}")
        return None