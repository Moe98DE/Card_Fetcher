# formatter.py
import textwrap
from typing import Dict, List, Tuple

from models import Card, CardFace

# ----- Tweakables for the legacy plain-text formatter -----
WIDTH = 72
WRAP_WIDTH = WIDTH - 4
SECT_RULE = "─" * WIDTH
CARD_SEPARATOR = "·" * (WIDTH - 8)


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

# key, heading, preferred pixel width
TABLE_COLUMN_SPECS: Tuple[Tuple[str, str, int], ...] = (
    ("quantity", "Qty", 55),
    ("card", "Card", 210),
    ("mana", "Mana", 130),
    ("type", "Type", 260),
    ("stats", "Stats", 110),
    ("text", "Rules Text", 430),
    ("rarity", "Rarity", 100),
    ("price", "Price", 100),
    ("notes", "Notes", 220),
)


def get_table_columns(show_price: bool = True, show_rarity: bool = True) -> List[Tuple[str, str, int]]:
    """Return the visible table columns for the current display options."""
    columns = []
    for spec in TABLE_COLUMN_SPECS:
        key = spec[0]
        if key == "price" and not show_price:
            continue
        if key == "rarity" and not show_rarity:
            continue
        columns.append(spec)
    return columns


def _format_colors(colors: List[str]) -> str:
    return ", ".join(colors) if colors else "Colorless"


def _format_mana_cost(face: CardFace) -> str:
    return (face.mana_cost or "").strip()


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


def _meld_parts(card: Card) -> List[str]:
    if not card.all_parts:
        return []
    return [
        part.get("name")
        for part in card.all_parts
        if part.get("component") == "meld_part" and part.get("name")
    ]


def _is_meld_part(card: Card) -> bool:
    card_name = card.name.casefold()
    return any(name.casefold() == card_name for name in _meld_parts(card))


def _is_meld_result(card: Card) -> bool:
    if not card.all_parts:
        return False
    card_name = card.name.casefold()
    return any(
        part.get("component") == "meld_result"
        and part.get("name", "").casefold() == card_name
        for part in card.all_parts
    )


def _meld_partner(card: Card) -> str:
    card_name = card.name.casefold()
    for name in _meld_parts(card):
        if name.casefold() != card_name:
            return name
    return ""


def _meld_result_note(card: Card) -> str:
    parts = _meld_parts(card)
    if not parts:
        return ""
    if len(parts) == 1:
        return f"Melded from {parts[0]}"
    return f"Melded from {' and '.join(parts)}"


def _face_stats(face: CardFace) -> str:
    stats: List[str] = []
    if face.power is not None and face.toughness is not None:
        stats.append(f"P/T {face.power}/{face.toughness}")
    if face.loyalty is not None:
        stats.append(f"Loyalty {face.loyalty}")
    if face.defense is not None:
        stats.append(f"Defense {face.defense}")
    return ", ".join(stats)


def _join_face_values(card: Card, value_getter) -> str:
    """Join face-specific values while keeping multi-face cards understandable."""
    values: List[str] = []
    multiface = len(card.card_faces) > 1

    for face in card.card_faces:
        value = value_getter(face)
        if not value:
            continue
        if multiface:
            values.append(f"{face.name}: {value}")
        else:
            values.append(value)

    return "\n".join(values)


def _card_to_table_row(card: Card, quantity: str = None, row_kind: str = "card") -> Dict[str, str]:
    if quantity is None:
        quantity = str(card.quantity)

    if card.card_faces:
        mana = _join_face_values(card, lambda face: _format_mana_cost(face))
        type_line = _join_face_values(card, lambda face: face.type_line or "")
        stats = _join_face_values(card, _face_stats)
        rules_text = _join_face_values(card, lambda face: face.oracle_text or "")
    else:
        mana = ""
        type_line = ""
        stats = ""
        rules_text = ""

    if _is_meld_result(card):
        note = _meld_result_note(card)
        row_kind = "meld_result"
    elif _is_meld_part(card):
        partner = _meld_partner(card)
        note = f"Melds with {partner}" if partner else "Meld card"
    else:
        note = ""

    return {
        "quantity": quantity,
        "card": card.name,
        "mana": mana or "—",
        "type": type_line or "—",
        "stats": stats or "—",
        "text": rules_text or "—",
        "rarity": card.rarity.title() if card.rarity else "—",
        "price": f"€{card.price_usd}" if card.price_usd else "—",
        "notes": note,
        "_row_kind": row_kind,
    }


def build_table_rows(deck: List[Card]) -> List[Dict[str, str]]:
    """
    Build the rows shared by the GUI table and Markdown export.

    User-entered cards are always shown first. A meld result is added once, after
    the listed cards, only when main_gui has attached it after confirming that
    all required meld parts are present in the deck.
    """
    rows = [_card_to_table_row(card) for card in deck]

    seen_results = {
        card.name.casefold()
        for card in deck
        if _is_meld_result(card)
    }

    for card in deck:
        result = getattr(card, "meld_result_card", None)
        if not result:
            continue

        result_key = result.name.casefold()
        if result_key in seen_results:
            continue

        rows.append(_card_to_table_row(result, quantity="—", row_kind="meld_result"))
        seen_results.add(result_key)

    return rows


def _markdown_cell(value: str) -> str:
    """Escape a value for use inside a Markdown table cell."""
    text = str(value or "")
    text = text.replace("|", "\\|")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.replace("\n", "<br>")


def format_deck_as_markdown(
    deck: List[Card],
    show_price: bool = True,
    show_rarity: bool = True,
) -> str:
    """Format the displayed deck as a portable Markdown table."""
    columns = get_table_columns(show_price=show_price, show_rarity=show_rarity)
    rows = build_table_rows(deck)

    total_cards = sum(card.quantity for card in deck)
    unique_cards = len(deck)

    output = [
        "## MTG Decklist Report",
        "",
        f"**Total Cards:** {total_cards}  ·  **Unique Cards:** {unique_cards}",
        "",
        "| " + " | ".join(title for _, title, _ in columns) + " |",
    ]

    alignments = []
    for key, _, _ in columns:
        if key in {"quantity", "price"}:
            alignments.append("---:")
        else:
            alignments.append("---")
    output.append("| " + " | ".join(alignments) + " |")

    for row in rows:
        output.append(
            "| " + " | ".join(_markdown_cell(row[key]) for key, _, _ in columns) + " |"
        )

    return "\n".join(output) + "\n"


# ---------------------------------------------------------------------------
# Legacy plain-text formatter
# ---------------------------------------------------------------------------
# main.py still imports format_deck_as_text, so it remains available. The GUI
# now uses the table helpers above.


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


def _align_right(left: str, right: str) -> str:
    if not right:
        return left

    spaces = WIDTH - len(left) - len(right)
    if spaces >= 1:
        return f"{left}{' ' * spaces}{right}"
    return f"{left}\n  {right}"


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


def _format_face_details(face: CardFace) -> List[str]:
    lines: List[str] = []

    mana_cost = _format_mana_cost(face)
    if mana_cost:
        lines.append(f"  Mana Cost: {mana_cost}")
    if face.type_line:
        lines.append(f"  Type: {face.type_line}")

    colors = _face_colors(face)
    if colors:
        lines.append(f"  Colors: {_format_colors(colors)}")

    stats = _face_stats(face)
    if stats:
        lines.append(f"  {stats}")

    if face.oracle_text:
        if lines:
            lines.append("")
        lines.extend(_format_oracle_text(face.oracle_text))

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


def _format_card_block(card: Card, show_price: bool, show_rarity: bool) -> List[str]:
    out: List[str] = []

    if len(card.card_faces) > 1:
        summary = []
        if show_rarity and card.rarity:
            summary.append(card.rarity.title())
        if show_price and card.price_usd:
            summary.append(f"Price: €{card.price_usd}")

        out.extend(_align_right(f"{card.quantity}x {card.name}", "   •   ".join(summary)).splitlines())
        out.append(f"  Layout: {_layout_name(card.layout)}")
        for index, face in enumerate(card.card_faces):
            out.append("")
            out.append(_center(f"{face.name} [{_face_role(card, index)}]", "-"))
            out.extend(_format_face_details(face))
    elif card.card_faces:
        face = card.card_faces[0]
        out.extend(_align_right(f"{card.quantity}x {card.name}", _format_mana_cost(face)).splitlines())

        type_line = face.type_line or ""
        if show_rarity and card.rarity:
            type_line = f"{type_line} - {card.rarity.title()}"

        right_side = []
        if _should_display_colors(card):
            right_side.append(f"Colors: {_format_colors(card.colors)}")
        if show_price and card.price_usd:
            right_side.append(f"Price: €{card.price_usd}")

        out.extend(_align_right(f"  {type_line}", "   •   ".join(right_side)).splitlines())

        details = _format_face_details(face)
        details = [
            line for line in details
            if not line.startswith("  Mana Cost:") and not line.startswith("  Type:")
        ]
        out.extend(details)

    if _is_meld_part(card):
        partner = _meld_partner(card)
        if partner:
            out.append("")
            out.append(f"  Melds with {partner}")
    elif _is_meld_result(card):
        note = _meld_result_note(card)
        if note:
            out.append("")
            out.append(f"  {note}")

    return out


def format_deck_as_text(deck: List[Card], show_price: bool = True, show_rarity: bool = True) -> str:
    output: List[str] = []
    total_cards = sum(card.quantity for card in deck)
    output.append(SECT_RULE)
    output.append(_center("MTG DECKLIST REPORT"))
    output.append(_center(f"Total Cards: {total_cards}   •   Unique Cards: {len(deck)}"))
    output.append(SECT_RULE)
    output.append("")

    display_cards = list(deck)
    seen_results = {card.name.casefold() for card in deck if _is_meld_result(card)}
    for card in deck:
        result = getattr(card, "meld_result_card", None)
        if result and result.name.casefold() not in seen_results:
            display_cards.append(result)
            seen_results.add(result.name.casefold())

    for idx, card in enumerate(display_cards, start=1):
        if idx > 1:
            output.append("")
            output.append(_center(CARD_SEPARATOR))
            output.append("")
        output.extend(_format_card_block(card, show_price=show_price, show_rarity=show_rarity))

    while output and not output[-1].strip():
        output.pop()
    output.append("")
    return "\n".join(output)
