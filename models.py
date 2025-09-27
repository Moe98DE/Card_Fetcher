# models.py
from dataclasses import dataclass, field
from typing import List, Dict, Optional

@dataclass
class CardFace:
    """Represents a single face of a Magic card."""
    name: str
    mana_cost: str
    type_line: str
    oracle_text: str
    power: Optional[str]
    toughness: Optional[str]
    loyalty: Optional[str]  # <-- NEW ATTRIBUTE
    image_url: str

@dataclass
class Card:
    """A structured representation of a Magic card, supporting multiple faces."""
    name: str
    colors: List[str]
    quantity: int
    card_faces: List[CardFace] = field(default_factory=list)
    all_parts: Optional[List[Dict]] = None  # <-- NEW: To store related card info (for Meld)
    meld_result_card: Optional['Card'] = None # <-- NEW: To hold the full Card object of the meld result

    @classmethod
    def from_scryfall_json(cls, scryfall_data: Dict, quantity: int) -> 'Card':
        """
        Factory method to create a Card object from Scryfall API JSON.
        Handles both single-faced and multi-faced cards.
        """
        faces = []
        
        if 'card_faces' in scryfall_data and len(scryfall_data['card_faces']) > 1:
            for face_data in scryfall_data['card_faces']:
                face = CardFace(
                    name=face_data.get('name', 'N/A'),
                    mana_cost=face_data.get('mana_cost', ''),
                    type_line=face_data.get('type_line', 'N/A'),
                    oracle_text=face_data.get('oracle_text', 'N/A'),
                    power=face_data.get('power', None),
                    toughness=face_data.get('toughness', None),
                    loyalty=face_data.get('loyalty', None),  # <-- NEW: Get loyalty for each face
                    image_url=face_data.get('image_uris', {}).get('normal', '')
                )
                faces.append(face)
        else:
            card_info = scryfall_data['card_faces'][0] if 'card_faces' in scryfall_data else scryfall_data
            
            single_face = CardFace(
                name=scryfall_data.get('name', 'N/A'),
                mana_cost=card_info.get('mana_cost', ''),
                type_line=scryfall_data.get('type_line', 'N/A'),
                oracle_text=card_info.get('oracle_text', 'N/A'),
                power=scryfall_data.get('power', None),
                toughness=scryfall_data.get('toughness', None),
                loyalty=scryfall_data.get('loyalty', None),  # <-- NEW: Get loyalty for single-faced cards
                image_url=card_info.get('image_uris', {}).get('normal', '')
            )
            faces.append(single_face)
            
        return cls(
            name=scryfall_data.get('name', 'N/A'),
            colors=scryfall_data.get('colors', []),
            quantity=quantity,
            card_faces=faces,
            all_parts=scryfall_data.get('all_parts', None) # <-- NEW: Populate the all_parts field
        )