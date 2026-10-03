# parser.py
import re
from dataclasses import dataclass, field
from typing import Dict, List

# "4 Sol Ring", "4x Sol Ring", "4X Sol Ring", "4 x Sol Ring"
_QUANTITY_RE = re.compile(r"^(\d+)\s*[xX]?\s+(.*)$|^(\d+)[xX](\S.*)$")

# Trailing decorations added by deck builders, stripped repeatedly from the end:
#   *F*  *E*  *CMDR*          Moxfield foil / etched / commander markers
#   [Ramp]  ^Have,#37d67a^     Archidekt categories and labels
#   #tag                      tags
#   (FEM) 18b / (PMEI) 2026-14 / (SLD) 123★ / (CMM)
#                             set code with an optional collector number of any shape
_TRAILING_PATTERNS = (
    re.compile(r"\s+\*[A-Za-z]+\*$"),
    re.compile(r"\s+\[[^\]]*\]$"),
    re.compile(r"\s+\^[^^]*\^$"),
    re.compile(r"\s+#\S+$"),
    re.compile(r"(?:^|\s+)\([A-Za-z0-9]{2,6}\)(?:\s+\S+)?$"),
)

# Lines that only introduce a section of the list.
_SECTION_HEADERS = {
    "about", "deck", "main", "maindeck", "main deck", "mainboard",
    "sideboard", "side", "commander", "commanders", "companion",
    "maybeboard", "maybe", "considering", "tokens", "attractions", "stickers",
}
_HEADER_RE = re.compile(r"^([A-Za-z ]+?)\s*(?:\(\d+\))?\s*:?$")

# MTGO exports prefix sideboard lines with "SB:".
_SB_PREFIX_RE = re.compile(r"^SB:\s*", re.IGNORECASE)


@dataclass
class ParseResult:
    cards: List[Dict] = field(default_factory=list)
    # Each entry: {'line': original text, 'reason': why it was skipped}
    skipped: List[Dict] = field(default_factory=list)


def _strip_decorations(name: str) -> str:
    changed = True
    while changed:
        changed = False
        for pattern in _TRAILING_PATTERNS:
            new_name = pattern.sub("", name)
            if new_name != name:
                name = new_name.rstrip()
                changed = True
    return name.strip()


def parse_decklist_detailed(decklist_text: str) -> ParseResult:
    """
    Parse a raw decklist into card queries and report lines that could not be read.

    Handles formats like:
    - 1x Sol Ring / 4 Command Tower / Sol Ring
    - 1 High Tide (FEM) 18b
    - 1 Loki, God of Mischief (PMEI) 2026-14 *F*
    - SB: 2 Pyroblast
    - Section headers such as "Deck", "Sideboard", "Commander (1)"

    Duplicate names (e.g. the same card in two sections) are merged by adding
    their quantities. Each card dict looks like
    {'quantity': 2, 'name': 'Sol Ring', 'lines': ['1 Sol Ring', '1x Sol Ring (C21) 263']}.
    """
    result = ParseResult()
    by_name: Dict[str, Dict] = {}
    previous_header = None

    for raw_line in decklist_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue

        header_match = _HEADER_RE.match(line)
        if header_match and header_match.group(1).strip().lower() in _SECTION_HEADERS:
            previous_header = header_match.group(1).strip().lower()
            continue

        # MTG Arena puts "Name <deck name>" right after the "About" header.
        if previous_header == "about" and line.lower().startswith("name "):
            continue
        previous_header = None

        line_body = _SB_PREFIX_RE.sub("", line)

        quantity = 1
        match = _QUANTITY_RE.match(line_body)
        if match:
            if match.group(1) is not None:
                quantity, name_part = int(match.group(1)), match.group(2)
            else:
                quantity, name_part = int(match.group(3)), match.group(4)
        else:
            name_part = line_body

        name = _strip_decorations(name_part)

        if not name:
            result.skipped.append({"line": line, "reason": "No card name found on this line"})
            continue
        if quantity <= 0:
            result.skipped.append({"line": line, "reason": "Quantity is zero"})
            continue

        key = name.casefold()
        if key in by_name:
            by_name[key]["quantity"] += quantity
            by_name[key]["lines"].append(line)
        else:
            entry = {"quantity": quantity, "name": name, "lines": [line]}
            by_name[key] = entry
            result.cards.append(entry)

    return result


def parse_decklist(decklist_text: str) -> List[Dict]:
    """Backwards-compatible helper that returns only the parsed card queries."""
    return parse_decklist_detailed(decklist_text).cards
