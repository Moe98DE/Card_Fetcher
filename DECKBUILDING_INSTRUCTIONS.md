# Deckbuilding with the `mtg-cards` tools

You have the `mtg-cards` tools, which read my local Magic database. Use them for every card, price and rules fact. **Do not use the web.** If a tool says data is missing or outdated, tell me which button to press in MTG Deck Formatter (Update Local DB / Update Tags / Update Rules) instead of guessing.

## Tools

| Tool | Use it for |
| --- | --- |
| `database_status` | Check what's installed (cards, tags, rules version). Call once at the start. |
| `edhrec_recommendations` | EDHREC's recommended pool for a commander. Takes an optional `theme`, `bracket` or `budget`, plus filters. |
| `edhrec_average_deck` | EDHREC's average 99, as a baseline only. |
| `analyze_deck` | Role counts (ramp, draw, removal, wipes, …) and recurring themes of a list. |
| `find_tags` / `list_tags` | Turn a need ("sac outlet", "token doubler") into exact Scryfall tag slugs. Never guess slugs. |
| `card_tags` | See which tags a card has, to find more cards like it. |
| `search_cards` | Search all cards by tags, text, type, colour identity, mana value, price and popularity. |
| `get_cards` | Full rules text, stats and price of specific cards. |
| `rules_lookup` / `rules_search` / `rules_glossary` | Comprehensive Rules. Cite rule numbers when rules matter. |

## Workflow

1. **Survey.** Call `edhrec_recommendations` for the commander with no filters. Note its themes, average type split and curve. If I gave a theme, bracket or budget, call it again with those.
2. **Plan.** Decide the deck's game plan and role targets before picking cards. A typical starting point is ~36 lands, ~10 ramp, ~10 card advantage, ~8–10 removal (including 2–3 wipes), then synergy pieces and win conditions. Adjust to the commander.
3. **Fill from the EDHREC pool.** Use filters (`tags_any`, `types`, `price_max`, `mv_max`, `exclude`) to fill specific roles.
4. **Avoid a generic deck.** Don't just copy the top of the list:
   - `sort="inclusion"` is the consensus, so use it for the core.
   - `sort="synergy"` or `sort="lift"` finds cards specific to this commander.
   - `search_cards(sort="deep_cut", color_identity=…)` finds strong cards that EDHREC users rarely play.
   - Aim for a meaningful share of picks that are not in the EDHREC top inclusion list. Say which ones they are.
5. **Check.** Run `analyze_deck` on the draft and fix any role that falls short.
6. **Verify.** Call `get_cards` on anything you're unsure about before relying on its text.

## Constraints to respect

- Exactly 100 cards including the commander(s). Singleton, except basic lands and cards that say otherwise.
- Every card is Commander-legal and inside the commander's colour identity. Always pass `color_identity` to `search_cards`.
- **Budget:** respect it with `price_max` and the per-card prices. Prices are the cheapest paper printing in EUR from my last database update. Report the deck total.
- **Bracket:** if I name one, respect its Game Changer limits (`game_changers="exclude"`, or count them) and avoid things outside its spirit, such as early infinite combos in low brackets.

## Output

1. A short summary of the game plan and how the deck wins.
2. The decklist grouped by type, with role counts, the total price, and the number of Game Changers.
3. Notable non-generic picks, one line each on why.
4. If you're improving an existing deck: cuts and adds as pairs, with reasons.
5. The full list as a plain decklist (`1 Card Name` per line), so I can paste it into MTG Deck Formatter or Moxfield.

## This task

- Commander:
- Theme / strategy:
- Budget (EUR, total or per card):
- Bracket:
- Must include / must avoid:
- Current decklist (if upgrading):
