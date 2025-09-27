# formatter.py
import textwrap
from typing import List
from models import Card

# ----- Tweakables -----
WIDTH = 72                  # overall report width
LEFT_COL = 50               # width for the left side (name, fields)
WRAP_WIDTH = WIDTH - 4      # paragraph width inside sections (with indent)
SECT_RULE = "─" * WIDTH     # main section rule
CARD_RULE = "─" * WIDTH     # per-card rule
MELD_RULE = "─" * (WIDTH - 2)

def _rule(char="─", width=WIDTH) -> str:
    return char * width

def _center(title: str, pad_char=" ") -> str:
    title = f" {title.strip()} "
    if len(title) >= WIDTH:
        return title[:WIDTH]
    side = (WIDTH - len(title)) // 2
    return f"{pad_char * side}{title}{pad_char * (WIDTH - len(title) - side)}"

def _wrap(text: str, initial_indent: str = "  ", subsequent_indent: str = "  ") -> str:
    wrapper = textwrap.TextWrapper(width=WRAP_WIDTH,
                                   initial_indent=initial_indent,
                                   subsequent_indent=subsequent_indent,
                                   break_long_words=False,
                                   break_on_hyphens=False)
    return wrapper.fill(text)

def _safe(val, default="") -> str:
    return default if val is None else str(val)

def _format_colors(colors: List[str]) -> str:
    return ", ".join(colors) if colors else "Colorless"

def _format_mana_cost(face) -> str:
    # mana_cost may be None or empty; show nothing rather than "()"
    mc = (face.mana_cost or "").strip()
    return mc

def _face_title(i: int, faces: List) -> str:
    if len(faces) <= 1:
        return ""
    return "Front Face" if i == 0 else "Back Face"

def _format_face(face, faces_len: int, face_index: int) -> List[str]:
    lines: List[str] = []
    title = _face_title(face_index, [None] * faces_len)
    if face.name:
        label = face.name if not title else f"{face.name} ({title})"
        lines.append(f"// --- {label} --- //")

    if face.type_line:
        lines.append(f"Type: {face.type_line}")

    if face.power is not None and face.toughness is not None:
        lines.append(f"P/T: {face.power}/{face.toughness}")
    if face.loyalty is not None:
        lines.append(f"Loyalty: {face.loyalty}")

    if face.oracle_text:
        lines.append("")  # spacing
        lines.append("Text:")

        bullet_prefixes = ("•", "-", "–")
        # break into lines; Scryfall often uses bullets inline or one per line
        for raw_line in face.oracle_text.split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.startswith(bullet_prefixes):
                # hanging indent for bullets
                bullet_wrap = textwrap.TextWrapper(
                    width=WRAP_WIDTH,
                    initial_indent="    " + stripped[0] + " ",
                    subsequent_indent="      ",
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                # remove the first bullet character before wrapping
                lines.append(bullet_wrap.fill(stripped[1:].lstrip()))
            else:
                # normal wrapped text
                lines.append(_wrap(stripped))
    return lines


# replace _format_meld_box with this
def _format_meld_section(card: Card) -> List[str]:
    lines: List[str] = []
    lines.append("~ Meld Information ~")

    # Is this the meld RESULT?
    is_result = any(p["component"] == "meld_result" and p["name"] == card.name
                    for p in card.all_parts)

    if is_result:
        parts = [p["name"] for p in card.all_parts if p["component"] == "meld_part"]
        lines.append("  This is the result of melding:")
        for p in parts:
            lines.append(f"    • {p}")
    else:
        # This is a meld PART
        try:
            partner = next(p["name"] for p in card.all_parts
                           if p["component"] == "meld_part" and p["name"] != card.name)
            result_name = next(p["name"] for p in card.all_parts
                               if p["component"] == "meld_result")
            lines.append(f"  Melds with: {partner}")
            lines.append(f"  To become:  {result_name}")
        except StopIteration:
            lines.append("  Meld data is incomplete.")

    # If we have the full result card, show it exactly once (no extra header here).
    if getattr(card, "meld_result_card", None):
        lines.append("")  # spacing
        lines.extend(_format_card_details(card.meld_result_card, is_sub_card=True))

    return lines

def _format_card_header(card: Card) -> str:
    """
    LEFT:  '{qty}x {name}'
    RIGHT: mana cost for first face (or blank)
    Next row (right aligned): Colors: <...>
    """
    qty_name = f"{card.quantity}x {card.name}"
    first_face = card.card_faces[0] if card.card_faces else None
    mana_cost = _format_mana_cost(first_face) if first_face else ""
    left = qty_name[:LEFT_COL]
    right = f"{mana_cost}".rjust(WIDTH - LEFT_COL)
    return f"{left}{right}"

def _format_card_meta_right(colors: List[str]) -> str:
    color_str = _format_colors(colors)
    return f"  Colors: {color_str}"

def _format_card_details(card: Card, is_sub_card: bool = False) -> List[str]:
    """Helper to format a single Card object into a list of lines."""
    out: List[str] = []

    # Sub-card header (for meld result display inside the box)
    if is_sub_card:
        out.append(_center(card.name))
        out.append(CARD_RULE)

    # Colors line (right-aligned on the card block)
    out.append(_format_card_meta_right(card.colors))

    # Per-face details
    for i, face in enumerate(card.card_faces):
        out.extend(_format_face(face, faces_len=len(card.card_faces), face_index=i))

    return out

def format_deck_as_text(deck: List[Card]) -> str:
    """Formats a list of Card objects into a clean, aligned report."""
    output: List[str] = []
    total_cards = sum(card.quantity for card in deck)

    # Deck header
    output.append(SECT_RULE)
    output.append(_center("MTG DECKLIST REPORT"))
    output.append(_center(f"Total Cards: {total_cards}   •   Unique Cards: {len(deck)}"))
    output.append(SECT_RULE)
    output.append("")

    for idx, card in enumerate(deck, start=1):
        # Card header
        output.append(_format_card_header(card))
        output.append(CARD_RULE)

        # Main card details
        output.extend(_format_card_details(card))

        # Meld box (if present)
        if card.all_parts:
            output.append("")  # spacing
            output.extend(_format_meld_section(card))

        # Card footer spacing
        output.append("")
        output.append(SECT_RULE)
        output.append("")

    # Trim trailing newlines
    while output and output[-1].strip() == "":
        output.pop()

    return "\n".join(output)
