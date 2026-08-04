# api_client.py
import json
import os
import time
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
            headers=SCRYFALL_HEADERS,
            timeout=REQUEST_TIMEOUT,
            verify=SSL_CERT_FILE,
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
