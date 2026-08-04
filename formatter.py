# formatter.py
import textwrap
from typing import List
from models import Card, CardFace

# ----- Tweakables -----
WIDTH = 72
WRAP_WIDTH = WIDTH - 4
SECT_RULE = "─" * WIDTH
CARD_SEPARATOR = "·" * (WIDTH - 8)
MELD_RULE = "─" * (WIDTH - 2)


LAYOUT_NAMES = {
    "adventure": "Adventure",
    "prepare": "Prepare",
    "split": "Split Card",
    "flip": "Flip Card",
    "transform": "Transforming Double-Faced Card",
    "modal_dfc": "Modal Double-Faced Card",
    "double_faced_token": "Double-Faced Token",
    "reversible_card": "Reversible Card",
}

LAYOUT_FACE_LABELS = {
    "adventure": ("Permanent", "Adventure Spell"),
    "prepare": ("Permanent", "Prepare Spell"),
    "split": ("Left Half", "Right Half"),
    "flip": ("Initial State", "Flipped State"),
    "transform": ("Front Face", "Transformed Face"),
    "modal_dfc": ("Castable Face A", "Castable Face B"),
    "double_faced_token": ("Front Face", "Back Face"),
    "reversible_card": ("Face A", "Face B"),
}


def _rule(char="─", width=WIDTH) -> str:
    return char * width


def _center(title: str, pad_char=" ") -> str:
    title = f" {title.strip()} "
    if len(title) >= WIDTH:
        return title[:WIDTH]
    side = (WIDTH - len(title)) // 2
    return f"{pad_char * side}{title}{pad_char * (WIDTH - len(title) - side)}"


def _wrap(text: str, initial_indent: str = "  ", subsequent_indent: str = "  ") -> str:
    wrapper = textwrap.TextWrapper(
        width=WRAP_WIDTH,
        initial_indent=initial_indent,
        subsequent_indent=subsequent_indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return wrapper.fill(text)


def _format_colors(colors: List[str]) -> str:
    return ", ".join(colors) if colors else "Colorless"


def _format_mana_cost(face: CardFace) -> str:
    return (face.mana_cost or "").strip()


def _align_right(left: str, right: str) -> str:
    """Place text at both edges when it fits; otherwise use two lines."""
    if not right:
        return left

    spaces = WIDTH - len(left) - len(right)
    if spaces >= 1:
        return f"{left}{' ' * spaces}{right}"

    return f"{left}\n  {right}"


def _layout_name(layout: str) -> str:
    if not layout:
        return "Unknown"
    return LAYOUT_NAMES.get(layout, layout.replace("_", " ").title())


def _face_role(card: Card, face_index: int) -> str:
    labels = LAYOUT_FACE_LABELS.get(card.layout)
    if labels and face_index < len(labels):
        return labels[face_index]
    return f"Component {face_index + 1}"


def _face_colors(face: CardFace) -> List[str]:
    return face.colors or face.color_indicator


def _format_oracle_text(oracle_text: str) -> List[str]:
    lines: List[str] = []
    if not oracle_text:
        return lines

    lines.append("Text:")
    bullet_prefixes = ("•", "-", "–")

    for raw_line in oracle_text.split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue

        if stripped.startswith(bullet_prefixes):
            bullet_wrap = textwrap.TextWrapper(
                width=WRAP_WIDTH,
                initial_indent="    " + stripped[0] + " ",
                subsequent_indent="      ",
                break_long_words=False,
                break_on_hyphens=False,
            )
            lines.append(bullet_wrap.fill(stripped[1:].lstrip()))
        else:
            lines.append(_wrap(stripped))

    return lines


def _format_face_details(face: CardFace, show_name: bool = False) -> List[str]:
    """Render all characteristics belonging to one Scryfall card face/part."""
    lines: List[str] = []

    if show_name:
        lines.append(face.name)

    mana_cost = _format_mana_cost(face)
    if mana_cost:
        lines.append(f"  Mana Cost: {mana_cost}")

    if face.type_line:
        lines.append(f"  Type: {face.type_line}")

    colors = _face_colors(face)
    if colors:
        lines.append(f"  Colors: {_format_colors(colors)}")

    if face.power is not None and face.toughness is not None:
        lines.append(f"  P/T: {face.power}/{face.toughness}")

    if face.loyalty is not None:
        lines.append(f"  Loyalty: {face.loyalty}")

    if face.defense is not None:
        lines.append(f"  Defense: {face.defense}")

    if face.oracle_text:
        if lines:
            lines.append("")
        lines.extend(_format_oracle_text(face.oracle_text))

   # if face.flavor_text:
      #  lines.append("")
      #  lines.append("Flavor:")
      #  lines.append(_wrap(face.flavor_text))

    return lines


def _should_display_colors(card: Card) -> bool:
    if not card.card_faces:
        return True
    first_face = card.card_faces[0]
    if not card.colors:
        return True
    if not first_face.mana_cost and card.colors:
        return True
    if first_face.oracle_text and "Devoid" in first_face.oracle_text:
        return True
    return False


def _is_meld_card(card: Card) -> bool:
    if not card.all_parts:
        return False

    meld_components = {"meld_part", "meld_result"}
    return any(part.get("component") in meld_components for part in card.all_parts)


def _format_meld_section(card: Card, show_price: bool, show_rarity: bool) -> List[str]:
    lines: List[str] = []
    lines.append("~ Meld Information ~")
    is_result = any(
        part.get("component") == "meld_result" and part.get("name") == card.name
        for part in card.all_parts
    )

    if is_result:
        parts = [
            part.get("name")
            for part in card.all_parts
            if part.get("component") == "meld_part"
        ]
        lines.append("  This is the result of melding:")
        for part_name in parts:
            lines.append(f"    • {part_name}")
    else:
        try:
            partner = next(
                part.get("name")
                for part in card.all_parts
                if part.get("component") == "meld_part" and part.get("name") != card.name
            )
            result_name = next(
                part.get("name")
                for part in card.all_parts
                if part.get("component") == "meld_result"
            )
            lines.append(f"  Melds with: {partner}")
            lines.append(f"  To become:  {result_name}")
        except StopIteration:
            lines.append("  Meld data is incomplete.")

    if getattr(card, "meld_result_card", None):
        lines.append("")
        lines.extend(
            _format_card_block(
                card.meld_result_card,
                is_sub_card=True,
                show_price=show_price,
                show_rarity=show_rarity,
            )
        )

    return lines


def _format_card_header(card: Card, show_price: bool, show_rarity: bool) -> List[str]:
    lines: List[str] = []
    first_face = card.card_faces[0] if card.card_faces else None
    if not first_face:
        return []

    qty_name = f"{card.quantity}x {card.name}"
    mana_cost = _format_mana_cost(first_face)
    lines.extend(_align_right(qty_name, mana_cost).splitlines())

    type_line_str = first_face.type_line or ""
    if show_rarity and card.rarity:
        type_line_str = f"{type_line_str} - {card.rarity.title()}"
    type_line = f"  {type_line_str}"

    right_side_info = []
    if _should_display_colors(card):
        right_side_info.append(f"Colors: {_format_colors(card.colors)}")
    if show_price and card.price_usd:
        right_side_info.append(f"Price: €{card.price_usd}")

    right_str = "   •   ".join(right_side_info)
    lines.extend(_align_right(type_line, right_str).splitlines())
    return lines


def _format_multiface_header(card: Card, show_price: bool, show_rarity: bool) -> List[str]:
    lines: List[str] = []
    qty_name = f"{card.quantity}x {card.name}"

    summary = []
    if show_rarity and card.rarity:
        summary.append(card.rarity.title())
    if show_price and card.price_usd:
        summary.append(f"Price: €{card.price_usd}")

    lines.extend(_align_right(qty_name, "   •   ".join(summary)).splitlines())
    lines.append(f"  Layout: {_layout_name(card.layout)}")
    return lines


def _format_multiface_card(card: Card, show_price: bool, show_rarity: bool) -> List[str]:
    lines = _format_multiface_header(card, show_price, show_rarity)

    for index, face in enumerate(card.card_faces):
        role = _face_role(card, index)
        lines.append("")
        lines.append(_center(f"{face.name} [{role}]", "-"))
        lines.extend(_format_face_details(face))

    return lines


def _format_card_block(
    card: Card,
    is_sub_card: bool = False,
    show_price: bool = True,
    show_rarity: bool = True,
) -> List[str]:
    out: List[str] = []

    if is_sub_card:
        out.append(MELD_RULE)

    if len(card.card_faces) > 1:
        out.extend(_format_multiface_card(card, show_price, show_rarity))
    else:
        out.extend(_format_card_header(card, show_price, show_rarity))
        if card.card_faces:
            face = card.card_faces[0]
            details = _format_face_details(face)
            # The normal header already contains mana cost and type line.
            details = [
                line
                for line in details
                if not line.startswith("  Mana Cost:") and not line.startswith("  Type:")
            ]
            out.extend(details)

    return out


def format_deck_as_text(deck: List[Card], show_price: bool = True, show_rarity: bool = True) -> str:
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

        output.extend(
            _format_card_block(
                card,
                show_price=show_price,
                show_rarity=show_rarity,
            )
        )

        if _is_meld_card(card):
            output.append("")
            output.extend(
                _format_meld_section(
                    card,
                    show_price=show_price,
                    show_rarity=show_rarity,
                )
            )

    while output and not output[-1].strip():
        output.pop()
    output.append("")
    return "\n".join(output)
