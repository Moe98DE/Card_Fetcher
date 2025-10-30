# formatter.py
import textwrap
from typing import List, Optional
from models import Card

# ----- Tweakables -----
WIDTH = 72
WRAP_WIDTH = WIDTH - 4
SECT_RULE = "─" * WIDTH
CARD_SEPARATOR = "·" * (WIDTH - 8)
MELD_RULE = "─" * (WIDTH - 2)


# ... (all other helper functions like _rule, _center, etc. remain the same) ...

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
    mc = (face.mana_cost or "").strip()
    return mc


def _face_title(i: int, faces: List) -> str:
    if len(faces) <= 1:
        return ""
    return "Front Face" if i == 0 else "Back Face"


def _should_display_colors(card: Card) -> bool:
    if not card.card_faces: return True
    first_face = card.card_faces[0]
    if not card.colors: return True
    if not first_face.mana_cost and card.colors: return True
    if first_face.oracle_text and "Devoid" in first_face.oracle_text: return True
    return False


# ----- NEW HELPER FUNCTION TO FIX THE BUG -----
def _is_meld_card(card: Card) -> bool:
    """
    Checks if a card is part of a meld mechanic specifically.
    The `all_parts` field is used for transform, adventures, etc., so we must
    check the component type.
    """
    if not card.all_parts:
        return False

    meld_components = {"meld_part", "meld_result"}
    return any(part.get("component") in meld_components for part in card.all_parts)


# -----------------------------------------------

def _format_face(face, faces_len: int, face_index: int) -> List[str]:
    lines: List[str] = []
    if faces_len > 1:
        title = _face_title(face_index, [None] * faces_len)
        label = face.name if not title else f"{face.name} ({title})"
        lines.append("")
        lines.append(_center(label, "-"))
    if face.power is not None and face.toughness is not None:
        lines.append(f"  P/T: {face.power}/{face.toughness}")
    if face.loyalty is not None:
        lines.append(f"  Loyalty: {face.loyalty}")
    if face.oracle_text:
        if not (face.power is not None or face.loyalty is not None):
            lines.append("")
        lines.append("Text:")
        bullet_prefixes = ("•", "-", "–")
        for raw_line in face.oracle_text.split("\n"):
            stripped = raw_line.strip()
            if not stripped: continue
            if stripped.startswith(bullet_prefixes):
                bullet_wrap = textwrap.TextWrapper(
                    width=WRAP_WIDTH, initial_indent="    " + stripped[0] + " ",
                    subsequent_indent="      ", break_long_words=False, break_on_hyphens=False
                )
                lines.append(bullet_wrap.fill(stripped[1:].lstrip()))
            else:
                lines.append(_wrap(stripped))
    return lines


def _format_meld_section(card: Card) -> List[str]:
    lines: List[str] = []
    lines.append("~ Meld Information ~")
    is_result = any(p["component"] == "meld_result" and p["name"] == card.name for p in card.all_parts)
    if is_result:
        parts = [p["name"] for p in card.all_parts if p["component"] == "meld_part"]
        lines.append("  This is the result of melding:")
        for p in parts: lines.append(f"    • {p}")
    else:
        try:
            partner = next(
                p["name"] for p in card.all_parts if p["component"] == "meld_part" and p["name"] != card.name)
            result_name = next(p["name"] for p in card.all_parts if p["component"] == "meld_result")
            lines.append(f"  Melds with: {partner}")
            lines.append(f"  To become:  {result_name}")
        except StopIteration:
            lines.append("  Meld data is incomplete.")
    if getattr(card, "meld_result_card", None):
        lines.append("")
        lines.extend(_format_card_block(card.meld_result_card, is_sub_card=True))
    return lines


def _format_card_header(card: Card) -> List[str]:
    lines: List[str] = []
    first_face = card.card_faces[0] if card.card_faces else None
    if not first_face: return []
    qty_name = f"{card.quantity}x {card.name}"
    mana_cost = _format_mana_cost(first_face)
    lines.append(f"{qty_name}{mana_cost.rjust(WIDTH - len(qty_name))}")
    type_line = f"  {first_face.type_line or ''}"
    if _should_display_colors(card):
        color_str = f"Colors: {_format_colors(card.colors)}"
        lines.append(f"{type_line}{color_str.rjust(WIDTH - len(type_line))}")
    else:
        lines.append(type_line)
    return lines


def _format_card_block(card: Card, is_sub_card: bool = False) -> List[str]:
    out: List[str] = []
    if is_sub_card:
        out.append(MELD_RULE)
        out.extend(_format_card_header(card))
    else:
        out.extend(_format_card_header(card))
    for i, face in enumerate(card.card_faces):
        out.extend(_format_face(face, faces_len=len(card.card_faces), face_index=i))
    return out


def format_deck_as_text(deck: List[Card]) -> str:
    output: List[str] = []
    total_cards = sum(card.quantity for card in deck)
    output.append(SECT_RULE)
    output.append(_center("MTG DECKLIST REPORT"))
    output.append(_center(f"Total Cards: {total_cards}   •   Unique Cards: {len(deck)}"))
    output.append(SECT_RULE)
    output.append("")
    for idx, card in enumerate(deck, start=1):
        if idx > 1:
            output.append("")
            output.append(_center(CARD_SEPARATOR))
            output.append("")

        output.extend(_format_card_block(card))

        # ----- FIX APPLIED HERE -----
        # Now we check specifically for meld components.
        if _is_meld_card(card):
            output.append("")
            output.extend(_format_meld_section(card))
        # ----------------------------

    while output and not output[-1].strip():
        output.pop()
    output.append("")
    return "\n".join(output)