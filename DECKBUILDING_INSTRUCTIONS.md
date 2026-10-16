# Guide: using the `mtg-cards` tools

The user gives you a commander and a Markdown table of the cards they physically own (the collection). It has full card details, so you never need tools to look up owned cards. The `mtg-cards` tools read a local card database, Scryfall function tags, EDHREC data and the Comprehensive Rules. Never use the web.

## Scenario 1: collection only

Build only from the collection. **Don't use the search or EDHREC tools.** The only exception is a rules interaction you're unsure about: check it with `rules_lookup` / `rules_search`.

## Scenario 2: collection + budget

Owned cards cost €0. The budget is spent only on cards from outside the collection. Price every bought card with the tools (cheapest paper printing in EUR), and keep a running total that never exceeds the budget.

1. **Build from the collection first.** Draft the best deck you can using owned cards only.
2. **Find the holes.** Run `analyze_deck` on the draft and compare it with what the commander needs. Look for:
   - weak roles (ramp, draw, removal, wipes, protection, recursion);
   - curve problems;
   - missing pieces for the theme;
   - the specific problems the user described in a deck that didn't work.

   Write the holes down in priority order before searching.
3. **Turn each hole into a search.** Use `find_tags` to get exact tag slugs for the hole (never guess them). Then search two ways:
   - `edhrec_recommendations(commander, tags_any=[...], price_max=..., exclude=<collection>)` finds proven cards for this commander that the user doesn't own.
   - `search_cards(tags_any=[...], color_identity=<commander identity>, price_max=..., exclude=<collection + deck>)` finds options beyond EDHREC.
4. **Fit the budget to the search strategy.**
   - **Low budget (e.g. €10):** popular cards are often expensive or already owned. Prefer cheap cards that fill the exact hole. Useful options are `search_cards(sort="deep_cut")`, `edhrec_recommendations(sort="lift" or "synergy")` and `sort="price"`. A €0.20 card that does precisely the missing job beats a famous staple.
   - **Larger budget:** `sort="inclusion"` / `"popular"` for proven upgrades, mixed with lift/deep-cut picks so the deck doesn't turn generic.
   - **Spend on the biggest holes first.** Each purchase should replace the weakest card in the draft, so name the cut for every add.
5. **Verify before committing.** Call `get_cards` on bought cards whose text matters. Run `analyze_deck` again on the final list.

## Tool notes

- `edhrec_recommendations` with no filters also returns the commander's EDHREC themes, average type split and mana curve. That's a useful sanity check for the draft. Pass `theme=` when the user names one EDHREC lists.
- `card_tags` on a card that works well in the deck shows its tags. Searching those tags finds similar cards.
- Always pass `color_identity` to `search_cards`. Results are Commander-legal by default.
- (GC) marks Game Changers. Count them if the user cares about brackets.
- If a tool reports missing or outdated data, tell the user to press Update Local DB / Update Tags / Update Rules in MTG Deck Formatter. Don't work around it.
