"""Additional, composable card filters shared by the existing MCP search tools.

The SQL reads the original Scryfall JSON only when necessary. Existing databases
remain usable in read-only mode without a migration or another bulk download.
"""
import json
import math
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import search_index as si

ColorMatch = Literal["subset", "exact", "contains", "overlap"]
Legality = Literal["legal", "not_legal", "restricted", "banned"]
Stat = Literal["power", "toughness", "loyalty", "defense"]


class NumericRange(BaseModel):
    """Inclusive numeric bounds; use equal min/max for an exact value."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min: Optional[float] = None
    max: Optional[float] = None

    @model_validator(mode="after")
    def valid_range(self):
        if self.min is None and self.max is None:
            raise ValueError("Provide min and/or max for a stat range.")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("Range min must be less than or equal to max.")
        return self


class CardFilters(BaseModel):
    """Optional refinements. All supplied fields are ANDed with existing filters."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    color_identity_match: ColorMatch = Field(default="subset", description=(
        "How to match the top-level color_identity: subset = within commander's colors, "
        "exact = same colors, contains = includes every requested color, overlap = any requested color. "
        "Requires color_identity when changed from subset. C means colorless; overlap with C is invalid."))
    colors: Optional[str] = Field(default=None, description=(
        "Actual card colors as WUBRG letters or C, distinct from Commander identity. "
        "Uses top-level colors when available, otherwise the union of all faces' colors."))
    colors_match: ColorMatch = Field(default="exact", description="Match mode for colors; same modes as color_identity_match.")
    legalities: Dict[str, Legality] = Field(default_factory=dict, description=(
        "Format to status, e.g. {'modern':'legal','pauper':'legal'} or {'commander':'banned'}. "
        "All must match. Explicit commander status overrides commander_legal's default. "
        "For other formats alone, set commander_legal=false to remove the Commander restriction."))
    price_min: Optional[float] = Field(default=None, ge=0, description="Minimum cheapest regular paper EUR price; combines with price_max.")
    price_known: Optional[bool] = Field(default=None, description=(
        "true = known EUR price, false = missing EUR price, omitted = either. "
        "Missing prices never satisfy price_min or price_max."))
    name_contains: Optional[str] = Field(default=None, description="Literal, case-insensitive substring in card name only.")
    oracle_contains: Optional[str] = Field(default=None, description="Literal, case-insensitive substring in rules text only; all faces searched.")
    types_any: List[str] = Field(default_factory=list, description="At least one of these type-line substrings, in addition to types/types_none.")
    keywords_all: List[str] = Field(default_factory=list, description="All these exact Scryfall keywords, case-insensitive, e.g. ['Flying','Vigilance'].")
    keywords_any: List[str] = Field(default_factory=list, description="At least one of these exact Scryfall keywords.")
    keywords_none: List[str] = Field(default_factory=list, description="None of these exact Scryfall keywords.")
    layouts: List[str] = Field(default_factory=list, description="Any of these Scryfall layouts, e.g. ['normal','transform','modal_dfc','adventure'].")
    stats: Dict[Stat, NumericRange] = Field(default_factory=dict, description=(
        "Numeric power/toughness/starting loyalty/defense ranges, e.g. {'power':{'min':4}}. "
        "All stat ranges must match the same face. Missing or variable values (*, 1+*, X) are excluded."))

    @field_validator("colors")
    @classmethod
    def valid_colors(cls, value):
        if value is not None:
            return normalize_colors(value)
        return value

    @field_validator("name_contains", "oracle_contains")
    @classmethod
    def nonempty_text(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Text filters cannot be empty.")
        return value

    @field_validator("types_any", "keywords_all", "keywords_any", "keywords_none", "layouts")
    @classmethod
    def normalize_list(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("Filter lists cannot contain empty strings.")
        return list(dict.fromkeys(value.strip().lower() for value in values))

    @field_validator("legalities")
    @classmethod
    def normalize_formats(cls, values):
        result = {}
        for key, value in values.items():
            normalized = key.strip().lower()
            if not normalized or not all(c.isascii() and (c.isalnum() or c == "_") for c in normalized):
                raise ValueError("Format names must use letters, numbers or underscores, e.g. commander or historicbrawl.")
            if normalized in result and result[normalized] != value:
                raise ValueError(f"Conflicting legality statuses for {normalized}.")
            result[normalized] = value
        return result

    @model_validator(mode="after")
    def valid_options(self):
        if self.colors is None and self.colors_match != "exact":
            raise ValueError("colors_match requires colors.")
        if self.colors == "C" and self.colors_match == "overlap":
            raise ValueError("overlap requires at least one color; use exact for colorless.")
        if self.price_known is False and self.price_min is not None:
            raise ValueError("price_known=false cannot be combined with price_min.")
        return self


def normalize_colors(value: str) -> str:
    cleaned = value.strip().upper()
    if cleaned in ("", "C", "COLORLESS", "COLOURLESS"):
        return "C"
    letters = set(cleaned.replace(",", "").replace(" ", "").replace("{", "").replace("}", ""))
    if letters - set("WUBRG"):
        raise ValueError("Colors must use W, U, B, R, G (or C for colorless).")
    return "".join(c for c in "WUBRG" if c in letters)


def numeric_stat(value):
    """SQLite function: variable characteristics are unknown, never zero."""
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def card_color_mask(data: str) -> int:
    card = json.loads(data)
    colors = card.get("colors")
    if colors is None:
        colors = [color for face in card.get("card_faces") or [] for color in face.get("colors") or []]
    return si.ci_mask(colors)


def color_clause(expression: str, colors: str, mode: ColorMatch):
    mask = si.ci_mask(normalize_colors(colors).replace("C", ""))
    if mode == "subset":
        return f"({expression} & ?) = 0", [31 & ~mask]
    if mode == "exact":
        return f"{expression} = ?", [mask]
    if mode == "contains":
        return f"({expression} & ?) = ?", [mask, mask]
    if not mask:
        raise ValueError("overlap requires at least one color; use exact for colorless.")
    return f"({expression} & ?) != 0", [mask]


def compile_filters(filters: CardFilters):
    """Return SQL predicates, bound values, and whether cached JSON is needed.

    c is card_index; d is the original cards table. User input is always bound.
    """
    where, params = [], []
    needs_data = bool(filters.colors is not None or filters.legalities or filters.keywords_all
                      or filters.keywords_any or filters.keywords_none or filters.stats)

    if filters.colors is not None:
        clause, values = color_clause("card_color_mask(d.data)", filters.colors, filters.colors_match)
        where.append(clause)
        params.extend(values)
    for format_name, status in filters.legalities.items():
        where.append("json_extract(d.data, ?) = ?")
        params.extend([f"$.legalities.{format_name}", status])
    if filters.price_min is not None:
        where.append("c.price_eur >= ?")
        params.append(filters.price_min)
    if filters.price_known is not None:
        where.append("c.price_eur IS " + ("NOT NULL" if filters.price_known else "NULL"))
    for field, value in (("name", filters.name_contains), ("oracle_text", filters.oracle_contains)):
        if value is not None:
            where.append(f"instr(lower(c.{field}), lower(?)) > 0")
            params.append(value)
    if filters.types_any:
        where.append("(" + " OR ".join("instr(lower(c.type_line), ?) > 0" for _ in filters.types_any) + ")")
        params.extend(filters.types_any)
    for keyword in filters.keywords_all:
        where.append("EXISTS (SELECT 1 FROM json_each(d.data, '$.keywords') k WHERE lower(k.value) = ?)")
        params.append(keyword)
    for keywords, negate in ((filters.keywords_any, False), (filters.keywords_none, True)):
        if keywords:
            where.append(("NOT " if negate else "") + "EXISTS (SELECT 1 FROM json_each(d.data, '$.keywords') k "
                         "WHERE lower(k.value) IN (" + ",".join("?" for _ in keywords) + "))")
            params.extend(keywords)
    if filters.layouts:
        where.append("c.layout IN (" + ",".join("?" for _ in filters.layouts) + ")")
        params.extend(filters.layouts)
    if filters.stats:
        face_predicates = []
        for stat, bounds in filters.stats.items():
            for operator, value in ((">=", bounds.min), ("<=", bounds.max)):
                if value is not None:
                    face_predicates.append(f"numeric_stat(json_extract(face.value, ?)) {operator} ?")
                    params.extend([f"$.{stat}", value])
        where.append("EXISTS (SELECT 1 FROM json_each(CASE WHEN json_array_length(d.data, '$.card_faces') > 0 "
                     "THEN json_extract(d.data, '$.card_faces') ELSE json_array(json(d.data)) END) face WHERE "
                     + " AND ".join(face_predicates) + ")")
    return where, params, needs_data
