# MTG Deck Formatter

 [Screenshot](https://imgur.com/a/sZ3awTF)

A simple yet powerful desktop application for Windows, Mac, and Linux that takes a raw Magic: The Gathering decklist and formats it into a detailed, human-readable report with card data fetched live from the Scryfall API.

## Features

-   **Flexible Parsing**: Accepts various decklist formats (e.g., `1x Sol Ring`, `Sol Ring`, `4 Counterspell (SET) 123`).
-   **Detailed Card Information**: Fetches and displays mana cost, colors, card type, full oracle text, and P/T for creatures.
-   **Responsive GUI**: The app remains fully responsive while fetching card data in the background, with a progress bar to show its status.
-   **Simple Interface**: A clean and intuitive UI built with Python's native Tkinter library.
-   **Copy to Clipboard**: Easily copy the entire formatted output with a single click.
-   **Modular Codebase**: Built with separation of concerns, making the code easy to read, maintain, and expand.

## How to Use

To run this application, you will need Python 3 installed on your system.

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/your-username/mtg-deck-formatter.git
    cd mtg-deck-formatter
    ```

2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv venv
    ```
    Activate it:
    -   On Windows: `.\venv\Scripts\activate`
    -   On macOS/Linux: `source venv/bin/activate`

3.  **Install the required dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Run the application:**
    ```bash
    python main_gui.py
    ```

## Project Structure

The application is designed to be modular and scalable:

-   `main_gui.py`: The main entry point for the desktop application, handling all GUI logic, threading, and event handling.
-   `parser.py`: Contains the logic for parsing raw decklist text into a structured format.
-   `api_client.py`: Manages all communication with the external Scryfall API.
-   `models.py`: Defines the `Card` data structure, decoupling the app from the specific API response format.
-   `formatter.py`: Handles the presentation logic, turning structured `Card` data into a clean, formatted string.

## Dependencies

-   [requests](https://pypi.org/project/requests/): For making HTTP requests to the Scryfall API.
-   Python's built-in libraries: `tkinter`, `threading`, `queue`, `re`, `textwrap`.

## Acknowledgments

This application relies entirely on the fantastic and free [Scryfall API](https://scryfall.com/docs/api) for all Magic: The Gathering card data.

## License

This project is licensed under the MIT License - see the `LICENSE` file for details.