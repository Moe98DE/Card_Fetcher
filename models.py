# models.py
from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class CardFace:
    """Represents one independently described part or face of a Magic card."""
    name: str
    mana_cost: str
    type_line: str
    oracle_text: str
    power: Optional[str]
    toughness: Optional[str]
    loyalty: Optional[str]
    image_url: str
    colors: List[str] = field(default_factory=list)
    color_indicator: List[str] = field(default_factory=list)
    #flavor_text: Optional[str] = None
    defense: Optional[str] = None


@dataclass
class Card:
    """A structured representation of a Magic card, supporting multiple parts and faces."""
    name: str
    colors: List[str]
    quantity: int
    price_usd: Optional[str] = None
    rarity: Optional[str] = None
    layout: str = "normal"
    card_faces: List[CardFace] = field(default_factory=list)
    all_parts: Optional[List[Dict]] = None
    meld_result_card: Optional['Card'] = None

    @classmethod
    def from_scryfall_json(cls, scryfall_data: Dict, quantity: int) -> 'Card':
        """
        Factory method to create a Card object from Scryfall API JSON.

        Scryfall uses ``card_faces`` for more than physical front/back cards. It
        also represents Adventure, Prepare, split, transform, modal DFC, and
        other multi-part layouts. Each part therefore keeps its own complete
        characteristics and the top-level ``layout`` controls presentation.
        """
        faces = []
        raw_faces = scryfall_data.get('card_faces') or []

        if len(raw_faces) > 1:
            for face_data in raw_faces:
                face = CardFace(
                    name=face_data.get('name', 'N/A'),
                    mana_cost=face_data.get('mana_cost', ''),
                    type_line=face_data.get('type_line', 'N/A'),
                    oracle_text=face_data.get('oracle_text', 'N/A'),
                    power=face_data.get('power'),
                    toughness=face_data.get('toughness'),
                    loyalty=face_data.get('loyalty'),
                    image_url=face_data.get('image_uris', {}).get('normal', ''),
                    colors=face_data.get('colors', []),
                    color_indicator=face_data.get('color_indicator', []),
                    #flavor_text=face_data.get('flavor_text'),
                    defense=face_data.get('defense'),
                )
                faces.append(face)
        else:
            card_info = raw_faces[0] if raw_faces else scryfall_data

            single_face = CardFace(
                name=card_info.get('name', scryfall_data.get('name', 'N/A')),
                mana_cost=card_info.get('mana_cost', ''),
                type_line=card_info.get('type_line', scryfall_data.get('type_line', 'N/A')),
                oracle_text=card_info.get('oracle_text', scryfall_data.get('oracle_text', 'N/A')),
                power=card_info.get('power', scryfall_data.get('power')),
                toughness=card_info.get('toughness', scryfall_data.get('toughness')),
                loyalty=card_info.get('loyalty', scryfall_data.get('loyalty')),
                image_url=card_info.get('image_uris', scryfall_data.get('image_uris', {})).get('normal', ''),
                colors=card_info.get('colors', scryfall_data.get('colors', [])),
                color_indicator=card_info.get('color_indicator', scryfall_data.get('color_indicator', [])),
                #flavor_text=card_info.get('flavor_text', scryfall_data.get('flavor_text')),
                defense=card_info.get('defense', scryfall_data.get('defense')),
            )
            faces.append(single_face)

        return cls(
            name=scryfall_data.get('name', 'N/A'),
            colors=scryfall_data.get('colors', []),
            quantity=quantity,
            price_usd=scryfall_data.get('prices', {}).get('eur'),
            rarity=scryfall_data.get('rarity'),
            layout=scryfall_data.get('layout', 'normal'),
            card_faces=faces,
            all_parts=scryfall_data.get('all_parts'),
        )
