# api_client.py
import json
import os
import time
from typing import Dict, Optional, List

import certifi
import requests

SCRYFALL_API_BASE = "https://api.scryfall.com"
REQUEST_TIMEOUT = 30

# Default to certifi's CA bundle, but allow the user to override it from PowerShell:
#
#   $env:REQUESTS_CA_BUNDLE = "C:\path\to\combined-ca-bundle.pem"
#   .\MtgDeckFormatter-debug.exe
#
SSL_CERT_FILE = os.environ.get("REQUESTS_CA_BUNDLE", certifi.where())


def _get_request_kwargs() -> Dict:
    """
    Shared request settings for all API calls.

    Using a function makes it easy to print/debug or change settings later.
    """
    return {
        "timeout": REQUEST_TIMEOUT,
        "verify": SSL_CERT_FILE,
        "headers": {
            # Scryfall asks clients to identify themselves politely.
            # You can replace the URL/email with your own project info if you want.
            "User-Agent": "MtgDeckFormatter/1.0",
            "Accept": "application/json",
        },
    }


def _print_ssl_help(error: Exception) -> None:
    """
    Prints a helpful message when SSL certificate validation fails.

    This usually means something on the Windows machine/network is intercepting HTTPS,
    such as antivirus HTTPS scanning, a VPN, school/work proxy, Zscaler, Fortinet, etc.
    """
    print(f"SSL certificate verification failed: {error}")
    print(f"Current CA bundle: {SSL_CERT_FILE}")
    print("")
    print("This is usually not a file permissions issue.")
    print("It often means Windows/browser trusts a certificate that Python does not.")
    print("")
    print("Possible fix:")
    print("1. Export the trusted root/intermediate certificate from your browser or proxy.")
    print("2. Combine it with certifi's CA bundle.")
    print("3. Run the app with:")
    print(r'   $env:REQUESTS_CA_BUNDLE = "C:\path\to\combined-ca-bundle.pem"')
    print(r"   .\MtgDeckFormatter-debug.exe")


def fetch_card_data(card_name: str) -> Optional[Dict]:
    """
    Fetches card data from the Scryfall API for a given card name.
    """
    time.sleep(0.1)  # Rate limit

    url = f"{SCRYFALL_API_BASE}/cards/named"
    params = {"fuzzy": card_name}

    try:
        response = requests.get(
            url,
            params=params,
            **_get_request_kwargs()
        )
        response.raise_for_status()
        return response.json()

    except requests.exceptions.HTTPError as err:
        if err.response is not None and err.response.status_code == 404:
            print(f"Error: Card '{card_name}' not found.")
        else:
            print(f"HTTP Error fetching '{card_name}': {err}")
        return None

    except requests.exceptions.SSLError as err:
        print(f"SSL Error fetching '{card_name}'.")
        _print_ssl_help(err)
        return None

    except requests.exceptions.RequestException as err:
        print(f"Request Error fetching '{card_name}': {err}")
        return None

    except Exception as err:
        print(f"Unexpected error fetching '{card_name}': {err}")
        return None


def fetch_bulk_data_url() -> Optional[str]:
    """
    Queries Scryfall to get the download URL for 'Default Cards' (includes prices/images).
    """
    try:
        response = requests.get(
            f"{SCRYFALL_API_BASE}/bulk-data",
            **_get_request_kwargs()
        )
        response.raise_for_status()
        data = response.json()

        # We look for the "default_cards" type
        for item in data.get("data", []):
            if item.get("type") == "default_cards":
                return item.get("download_uri")

        print("Could not find 'default_cards' in Scryfall bulk data response.")
        return None

    except requests.exceptions.SSLError as err:
        print("SSL Error fetching bulk data meta.")
        _print_ssl_help(err)
        return None

    except requests.exceptions.RequestException as err:
        print(f"Request Error fetching bulk data meta: {err}")
        return None

    except Exception as err:
        print(f"Unexpected error fetching bulk data meta: {err}")
        return None


def download_bulk_json(download_url: str, progress_callback=None) -> Optional[List[Dict]]:
    """
    Downloads the Scryfall bulk JSON file and returns it as a list of dictionaries.
    """
    try:
        with requests.get(
            download_url,
            stream=True,
            **_get_request_kwargs()
        ) as response:
            response.raise_for_status()

            total_length = int(response.headers.get("content-length", 0))
            downloaded = 0
            chunks = []

            # Helper to throttle progress updates
            last_reported_percent = -1

            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue

                chunks.append(chunk)
                downloaded += len(chunk)

                if progress_callback:
                    if total_length > 0:
                        current_percent = int((downloaded / total_length) * 100)

                        # Only update if the integer percentage changed: 0, 1, 2...
                        if current_percent > last_reported_percent:
                            progress_callback(downloaded, total_length)
                            last_reported_percent = current_percent
                    else:
                        # If no total length, update roughly every 1MB
                        if downloaded % (1024 * 1024) < 8192:
                            progress_callback(downloaded, total_length)

            full_content = b"".join(chunks)
            return json.loads(full_content)

    except requests.exceptions.SSLError as err:
        print("SSL Error downloading bulk file.")
        _print_ssl_help(err)
        return None

    except requests.exceptions.RequestException as err:
        print(f"Request Error downloading bulk file: {err}")
        return None

    except json.JSONDecodeError as err:
        print(f"Error parsing downloaded bulk JSON: {err}")
        return None

    except Exception as err:
        print(f"Unexpected error downloading bulk file: {err}")
        return None
