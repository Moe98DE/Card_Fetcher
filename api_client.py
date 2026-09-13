# api_client.py
import json
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional, List
import gzip
import tempfile

import certifi
import requests

SCRYFALL_API_BASE = "https://api.scryfall.com"
REQUEST_TIMEOUT = 30

SCRYFALL_HEADERS = {
    "User-Agent":
        "Card_Fetcher/1.0 "
    ,
    "Accept": "application/json",
}

# Internal marker added to cached Scryfall objects.  Its presence also lets the
# database distinguish newly-priced rows from older cache rows created before
# cheapest-print pricing was implemented.
CHEAPEST_EUR_PRICE_KEY = "_cheapest_normal_paper_eur"

# Default to certifi's CA bundle, but allow the user to override it from PowerShell:
#
#   $env:REQUESTS_CA_BUNDLE = "C:\path\to\combined-ca-bundle.pem"
#   .\MtgDeckFormatter-debug.exe
#
SSL_CERT_FILE = os.environ.get("REQUESTS_CA_BUNDLE", certifi.where())


def _get_request_kwargs() -> Dict:
    return {
        "timeout": REQUEST_TIMEOUT,
        "verify": SSL_CERT_FILE,
        "headers": SCRYFALL_HEADERS.copy(),
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


def _normal_paper_eur_price(card_data: Dict) -> Optional[str]:
    """
    Return this printing's regular (nonfoil) EUR price when it represents a
    physical paper card.

    Scryfall's ``prices.eur`` field is specifically the regular/nonfoil EUR
    price.  Foil and etched prices live in separate fields, so they are not
    considered here.
    """
    games = card_data.get("games") or []
    if "paper" not in games:
        return None

    if card_data.get("digital") or card_data.get("oversized"):
        return None

    price = (card_data.get("prices") or {}).get("eur")
    if price in (None, ""):
        return None

    try:
        Decimal(str(price))
    except (InvalidOperation, ValueError, TypeError):
        return None

    return str(price)


def _fetch_cheapest_normal_paper_eur(card_data: Dict) -> Optional[str]:
    """Find the cheapest regular paper EUR price across all printings."""
    prints_url = card_data.get("prints_search_uri")

    # If Scryfall ever omits the prints URI, the resolved printing is still a
    # useful fallback rather than losing a valid price entirely.
    if not prints_url:
        return _normal_paper_eur_price(card_data)

    cheapest_value: Optional[Decimal] = None
    cheapest_text: Optional[str] = None
    next_url = prints_url

    try:
        while next_url:
            time.sleep(0.1)  # Respect Scryfall's request guidance.
            response = requests.get(next_url, **_get_request_kwargs())
            response.raise_for_status()
            page = response.json()

            for printing in page.get("data", []):
                price_text = _normal_paper_eur_price(printing)
                if price_text is None:
                    continue

                try:
                    price_value = Decimal(price_text)
                except (InvalidOperation, ValueError, TypeError):
                    continue

                if cheapest_value is None or price_value < cheapest_value:
                    cheapest_value = price_value
                    cheapest_text = price_text

            next_url = page.get("next_page") if page.get("has_more") else None
    except (requests.exceptions.RequestException, ValueError, TypeError) as err:
        # Card lookup itself succeeded.  Do not discard the whole card just
        # because the secondary print-history request failed; fall back to the
        # resolved printing's regular EUR price instead.
        print(f"Could not check all printings for '{card_data.get('name', 'Unknown')}': {err}")
        return _normal_paper_eur_price(card_data)

    return cheapest_text


def fetch_card_data(card_name: str) -> Optional[Dict]:
    """
    Fetch card data from Scryfall and attach the cheapest regular paper EUR
    price across all printings of that card.
    """
    time.sleep(0.1)  # Rate limit

    url = f"{SCRYFALL_API_BASE}/cards/named"
    params = {"fuzzy": card_name}

    try:
        response = requests.get(
            url,
            params=params,
            **_get_request_kwargs(),
        )
        response.raise_for_status()
        card_data = response.json()

        # Store the computed value on the same object that gets cached.  A
        # value of None is intentional and means no eligible EUR price exists.
        card_data[CHEAPEST_EUR_PRICE_KEY] = _fetch_cheapest_normal_paper_eur(card_data)
        return card_data

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
    Gets the Default Cards bulk download URL from Scryfall.

    Scryfall currently provides gzip-compressed JSON Lines using
    jsonl_download_uri. download_uri is retained as a fallback for
    older API responses.
    """
    try:
        response = requests.get(
            f"{SCRYFALL_API_BASE}/bulk-data",
            **_get_request_kwargs(),
        )
        response.raise_for_status()
        data = response.json()

        for item in data.get("data", []):
            if item.get("type") != "default_cards":
                continue

            download_url = (
                item.get("jsonl_download_uri")
                or item.get("download_uri")
            )

            if not download_url:
                print(
                    "The default_cards entry contained neither "
                    "jsonl_download_uri nor download_uri."
                )

            return download_url

        print("Could not find 'default_cards' in Scryfall bulk data response.")
        return None

    except requests.exceptions.SSLError as err:
        print("SSL Error fetching bulk data metadata.")
        _print_ssl_help(err)
        return None

    except requests.exceptions.HTTPError as err:
        response = err.response
        body = response.text[:1000] if response is not None else ""
        print(f"HTTP Error fetching bulk data metadata: {err}")
        if body:
            print(f"Scryfall response: {body}")
        return None

    except requests.exceptions.RequestException as err:
        print(f"Request Error fetching bulk data metadata: {err}")
        return None

    except (ValueError, TypeError) as err:
        print(f"Invalid bulk data metadata response: {err}")
        return None


def download_bulk_json(
    download_url: str,
    progress_callback=None,
) -> Optional[List[Dict]]:
    """
    Downloads Scryfall bulk data.

    Supports:
    - Current gzip-compressed JSON Lines files (.jsonl.gz)
    - Legacy JSON-array downloads
    """
    temp_path = None

    try:
        with requests.get(
            download_url,
            stream=True,
            **_get_request_kwargs(),
        ) as response:
            response.raise_for_status()

            total_length = int(response.headers.get("content-length", 0))
            downloaded = 0
            last_reported_percent = -1

            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=".scryfall-bulk",
                delete=False,
            ) as temp_file:
                temp_path = temp_file.name

                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue

                    temp_file.write(chunk)
                    downloaded += len(chunk)

                    if not progress_callback:
                        continue

                    if total_length > 0:
                        current_percent = int(
                            downloaded * 100 / total_length
                        )

                        if current_percent > last_reported_percent:
                            progress_callback(downloaded, total_length)
                            last_reported_percent = current_percent
                    else:
                        progress_callback(downloaded, 0)

        # Detect gzip using the file signature rather than relying only
        # on the filename or HTTP headers.
        with open(temp_path, "rb") as downloaded_file:
            is_gzip = downloaded_file.read(2) == b"\x1f\x8b"

        if is_gzip:
            cards = []

            with gzip.open(
                temp_path,
                mode="rt",
                encoding="utf-8",
            ) as jsonl_file:
                for line_number, line in enumerate(jsonl_file, start=1):
                    line = line.strip()

                    if not line:
                        continue

                    try:
                        card = json.loads(line)
                    except json.JSONDecodeError as err:
                        raise ValueError(
                            f"Invalid JSON on bulk-data line "
                            f"{line_number}: {err}"
                        ) from err

                    if isinstance(card, dict):
                        cards.append(card)

            return cards

        # Compatibility with the previous uncompressed JSON-array format.
        with open(temp_path, mode="r", encoding="utf-8") as json_file:
            data = json.load(json_file)

        if not isinstance(data, list):
            raise ValueError(
                "Expected the legacy Scryfall bulk file to contain "
                "a JSON array."
            )

        return data

    except requests.exceptions.SSLError as err:
        print("SSL Error downloading bulk file.")
        _print_ssl_help(err)
        return None

    except requests.exceptions.RequestException as err:
        print(f"Request Error downloading bulk file: {err}")
        return None

    except (OSError, ValueError, json.JSONDecodeError) as err:
        print(f"Error processing downloaded bulk data: {err}")
        return None

    except Exception as err:
        print(f"Unexpected error downloading bulk file: {err}")
        return None

    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
