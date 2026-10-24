"""Offline query semantics, including old-schema/read-only databases and EDHREC."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import search_index as si
from card_search import CardSearch, SearchError


def fixture_cards():
    def card(name, **values):
        base = dict(name=name, oracle_id=name.lower(), layout="normal", cmc=3,
                    mana_cost="{2}{U}", type_line="Creature — Bird", oracle_text="Flying",
                    color_identity=["U"], colors=["U"], keywords=["Flying"],
                    power="4", toughness="3", prices={"eur": "2.00"}, rarity="rare",
                    legalities={"commander": "legal", "modern": "legal", "vintage": "legal"},
                    edhrec_rank=100, games=["paper"])
        base.update(values)
        return base

    return [
        card("Guild Drake", color_identity=["U", "B"], keywords=["Flying", "Ward"]),
        card("Blue Scout", cmc=1, power="1", toughness="1", prices={"eur": "0.50"},
             keywords=["Flying", "Vigilance"]),
        card("Colorless Engine", color_identity=[], colors=[], type_line="Artifact", keywords=[],
             oracle_text="{T}: Add {C}{C}.", prices={"eur": "10.00"}, power=None, toughness=None),
        card("Devoid Horror", color_identity=["U", "B"], colors=[], keywords=["Devoid"],
             oracle_text="Devoid", power="5", toughness="4", cmc=4, prices={"eur": "1.00"}),
        card("Day // Night", color_identity=["W", "R"], colors=None, layout="transform", keywords=[],
             type_line="Creature — Human // Creature — Horror", oracle_text=None,
             card_faces=[dict(name="Day", colors=["W"], power="1", toughness="8", oracle_text="Draw a card."),
                         dict(name="Night", colors=["R"], power="8", toughness="1", oracle_text="Destroy target artifact.")]),
        card("Variable Beast", color_identity=["G"], colors=["G"], power="*", toughness="1+*",
             prices={"eur": None}, keywords=[]),
        card("Unpriced Drake", prices={}, power="0", toughness="0"),
        card("Banned Idol", color_identity=[], colors=[], type_line="Artifact", keywords=[],
             legalities={"commander": "banned", "modern": "banned", "vintage": "restricted"}),
        card("Absent Format", legalities={"commander": "legal"}),
        card("Not Modern", legalities={"commander": "legal", "modern": "not_legal"}),
        card("Negative Beast", power="-1", toughness="2"),
        card("Mind Sage", type_line="Legendary Planeswalker — Sage", power=None, toughness=None, loyalty="3"),
        card("Siege", type_line="Battle — Siege", power=None, toughness=None, defense="5"),
        card("100%_Real", oracle_text="Draw 100%_of a card's text."),
        card("Test Token", layout="token"),
        card("Test Commander", type_line="Legendary Creature — Bird"),
    ]


def build_fixture(conn):
    conn.execute("CREATE TABLE cards (name_normalized TEXT PRIMARY KEY, data TEXT)")
    conn.executemany("INSERT INTO cards VALUES (?, ?)",
                     [(c["name"].lower(), json.dumps(c)) for c in fixture_cards()])
    conn.commit()
    si.rebuild_card_index(conn)
    si.import_oracle_tags(conn, [
        dict(id="ramp", slug="ramp", taggings=[{"oracle_id": "colorless engine"}, {"oracle_id": "blue scout"}]),
        dict(id="evasion", slug="evasion", child_ids=["flying"]),
        dict(id="flying", slug="flying", parent_ids=["evasion"], taggings=[{"oracle_id": "guild drake"}]),
    ])


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "cards.db"
        conn = si.connect(str(self.path))
        build_fixture(conn)
        conn.close()
        self.original = self.path.read_bytes()
        # Every test exercises an actual read-only connection to the old schema.
        self.engine = CardSearch(si.connect(str(self.path), read_only=True))
        self.addCleanup(self.engine.conn.close)

    def tearDown(self):
        self.assertEqual(self.path.read_bytes(), self.original)

    def names(self, **query):
        return {r["name"] for r in self.engine.search_cards(**query)["rows"]}

    def test_identity_modes_and_colorless(self):
        self.assertEqual(self.names(color_identity="UB", filters={"color_identity_match": "exact"}),
                         {"Guild Drake", "Devoid Horror"})
        self.assertIn("Colorless Engine", self.names(color_identity="UB"))
        self.assertNotIn("Colorless Engine", self.names(color_identity="U", filters={"color_identity_match": "contains"}))
        self.assertEqual(self.names(color_identity="R", filters={"color_identity_match": "overlap"}), {"Day // Night"})
        self.assertEqual(self.names(color_identity="C"), {"Colorless Engine"})
        self.assertEqual(self.names(color_identity="{B}, {U}", filters={"color_identity_match": "exact"}),
                         {"Guild Drake", "Devoid Horror"})

    def test_actual_colors_are_distinct_from_identity(self):
        self.assertEqual(self.names(filters={"colors": "C"}), {"Colorless Engine", "Devoid Horror"})
        self.assertEqual(self.names(filters={"colors": "RW"}), {"Day // Night"})
        self.assertEqual(self.names(filters={"colors": "W", "colors_match": "overlap"}), {"Day // Night"})
        self.assertIn("Colorless Engine", self.names(filters={"colors": "U", "colors_match": "subset"}))
        self.assertNotIn("Day // Night", self.names(filters={"colors": "U", "colors_match": "contains"}))

    def test_legality_statuses_and_default_override(self):
        self.assertNotIn("Banned Idol", self.names())
        self.assertEqual(self.names(filters={"legalities": {"commander": "banned"}}), {"Banned Idol"})
        self.assertEqual(self.names(commander_legal=False, filters={"legalities": {"vintage": "restricted"}}), {"Banned Idol"})
        self.assertEqual(self.names(commander_legal=False, filters={"legalities": {"modern": "banned", "vintage": "restricted"}}), {"Banned Idol"})
        self.assertNotIn("Absent Format", self.names(filters={"legalities": {"modern": "not_legal"}}))
        self.assertEqual(self.names(filters={"legalities": {"modern": "not_legal"}}), {"Not Modern"})
        self.assertNotIn("Absent Format", self.names(filters={"legalities": {"modern": "legal"}}))
        self.assertEqual(self.names(filters={"legalities": {" Commander ": "banned"}}), {"Banned Idol"})

    def test_price_ranges_include_boundaries_and_exclude_missing(self):
        self.assertIn("Guild Drake", self.names(price_max=2, filters={"price_min": 2}))
        self.assertNotIn("Blue Scout", self.names(price_max=2, filters={"price_min": 1}))
        self.assertNotIn("Unpriced Drake", self.names(price_max=100))
        self.assertEqual(self.names(filters={"price_known": False}), {"Variable Beast", "Unpriced Drake"})
        self.assertNotIn("Variable Beast", self.names(filters={"price_known": True}))
        self.assertNotIn("Variable Beast", self.names(filters={"price_min": 0}))

    def test_field_text_is_literal_and_multiface(self):
        self.assertEqual(self.names(filters={"name_contains": "100%_"}), {"100%_Real"})
        self.assertEqual(self.names(filters={"oracle_contains": "100%_"}), {"100%_Real"})
        self.assertEqual(self.names(filters={"oracle_contains": "DESTROY TARGET"}), {"Day // Night"})
        self.assertEqual(self.names(filters={"name_contains": "' OR 1=1 --"}), set())
        self.assertNotIn("Guild Drake", self.names(filters={"oracle_contains": "Guild"}))

    def test_exact_keyword_sets(self):
        self.assertEqual(self.names(filters={"keywords_all": ["flying", " WARD "]}), {"Guild Drake"})
        self.assertEqual(self.names(filters={"keywords_any": ["ward", "vigilance"], "keywords_none": ["ward"]}), {"Blue Scout"})
        self.assertEqual(self.names(filters={"keywords_any": ["fly"]}), set())
        self.assertIn("Colorless Engine", self.names(filters={"keywords_none": ["flying"]}))

    def test_types_any_and_layouts_compose(self):
        self.assertEqual(self.names(types_none=["creature"], filters={"types_any": ["artifact", "battle"]}), {"Colorless Engine", "Siege"})
        self.assertEqual(self.names(filters={"layouts": ["TRANSFORM", "adventure"]}), {"Day // Night"})
        self.assertEqual(self.names(filters={"layouts": ["token"]}), set())

    def test_stats_exclude_missing_and_variable_values(self):
        result = self.names(filters={"stats": {"power": {"min": 0, "max": 0}}})
        self.assertEqual(result, {"Unpriced Drake"})
        self.assertEqual(self.names(filters={"stats": {"power": {"min": -1, "max": -1}}}), {"Negative Beast"})
        self.assertNotIn("Variable Beast", self.names(filters={"stats": {"power": {"max": 100}}}))
        self.assertEqual(self.names(filters={"stats": {"loyalty": {"min": 3, "max": 3}}}), {"Mind Sage"})
        self.assertEqual(self.names(filters={"stats": {"defense": {"min": 5}}}), {"Siege"})

    def test_stats_must_match_same_face(self):
        self.assertEqual(self.names(filters={"stats": {"power": {"min": 8}}}), {"Day // Night"})
        self.assertEqual(self.names(filters={"stats": {"power": {"min": 8}, "toughness": {"min": 8}}}), set())
        self.assertEqual(self.names(filters={"stats": {"power": {"min": 8}, "toughness": {"max": 1}}}), {"Day // Night"})

    def test_existing_tags_and_filters_compose_with_refinements(self):
        self.assertEqual(self.names(tags_all=["evasion"], color_identity="UB", price_max=2, mv_max=3,
                                    filters={"keywords_all": ["ward"], "price_min": 1}), {"Guild Drake"})
        self.assertEqual(self.names(tags_any=["ramp", "flying"], tags_none=["flying"],
                                    filters={"stats": {"power": {"min": 1}}}), {"Blue Scout"})
        self.assertEqual(self.names(tags_all=["ramp"], exclude=["Blue Scout"], filters={"colors": "U"}), set())

    def test_filtered_totals_and_stable_pagination(self):
        query = dict(sort="price", filters={"price_min": 1}, limit=2)
        pages = [self.engine.search_cards(offset=n, **query) for n in range(0, 12, 2)]
        full = self.engine.search_cards(sort="price", filters={"price_min": 1}, limit=100)
        self.assertEqual([r["name"] for p in pages for r in p["rows"]], [r["name"] for r in full["rows"]])
        self.assertTrue(all(p["total"] == full["total"] for p in pages))
        high_first = self.engine.search_cards(sort="price", sort_reverse=True)["rows"]
        self.assertEqual(high_first[0]["name"], "Colorless Engine")
        self.assertIsNone(high_first[-1]["price_eur"])

    def test_invalid_queries_fail_with_actionable_errors(self):
        queries = [
            {"filters": {"price_mni": 1}}, {"filters": {"price_min": -1}},
            {"filters": {"colors": "blue"}}, {"filters": {"colors": "C", "colors_match": "overlap"}},
            {"filters": {"colors_match": "contains"}}, {"filters": {"color_identity_match": "exact"}},
            {"color_identity": "C", "filters": {"color_identity_match": "overlap"}},
            {"filters": {"legalities": {"modren": "legal"}}},
            {"filters": {"legalities": {"modern": "allowed"}}},
            {"filters": {"legalities": {"Modern": "legal", "modern": "banned"}}},
            {"price_max": 2, "filters": {"price_known": False}},
            {"price_max": 2, "filters": {"price_min": 3}},
            {"mv_min": 3, "mv_max": 2}, {"mv_max": float("inf")},
            {"filters": {"keywords_any": [""]}}, {"filters": {"oracle_contains": " "}},
            {"filters": {"stats": {"power": {}}}},
            {"filters": {"stats": {"power": {"min": 3, "max": 2}}}},
            {"filters": {"stats": {"power": {"min": float("nan")}}}},
            {"sort_reverse": True, "sort": "popular"},
        ]
        for query in queries:
            with self.subTest(query=query), self.assertRaises(SearchError):
                self.engine.search_cards(**query)
        with self.assertRaisesRegex(SearchError, "Available in this database:.*modern"):
            self.engine.search_cards(filters={"legalities": {"modren": "legal"}})

    def edhrec_client(self):
        pool = [dict(name=c["name"], section="Creatures", labels=[], inclusion=.5, synergy=.1,
                     lift=2, trend=0, num_decks=50) for c in fixture_cards()]
        client = SimpleNamespace(commander_page=Mock(return_value={"cards": pool}))
        self.engine._edhrec_client = client
        return client

    def test_edhrec_uses_same_filters_before_pagination(self):
        self.edhrec_client()
        query = dict(tags_all=["evasion"], price_max=2, color_identity="UB", mv_max=3,
                     filters={"price_min": 1, "keywords_all": ["ward"], "legalities": {"modern": "legal"}})
        local = self.names(**query)
        rec = self.engine.edhrec_recommendations("Test Commander", limit=1, **query)
        self.assertEqual({r["name"] for r in rec["rows"]}, local)
        self.assertEqual(rec["total"], 1)
        self.assertEqual(self.engine.edhrec_recommendations("Test Commander", offset=1, **query)["rows"], [])

    def test_edhrec_all_pages_ignores_pagination_and_passes_bracket(self):
        client = self.edhrec_client()
        paged = self.engine.edhrec_recommendations("Test Commander", limit=2)
        all_rows = self.engine.edhrec_recommendations(
            "Test Commander", bracket="upgraded", all_pages=True, limit=1, offset=10)
        self.assertGreater(paged["total"], 2)
        self.assertEqual(len(paged["rows"]), 2)
        self.assertEqual(all_rows["total"], paged["total"])
        self.assertEqual(len(all_rows["rows"]), paged["total"])
        self.assertEqual(all_rows["offset"], 0)
        self.assertEqual([r["name"] for r in all_rows["rows"][:2]],
                         [r["name"] for r in paged["rows"]])
        client.commander_page.assert_called_with(
            ["Test Commander"], theme=None, bracket="upgraded",
            budget=None, refresh=False)

    def test_mcp_edhrec_defaults_to_full_text_and_all_pages(self):
        from unittest.mock import patch
        import mcp_server

        long_rules = "Pay {2}: " + ("Draw a card, then discard a card. " * 12)
        sample = {
            "name": "Example Card", "game_changer": False, "section": "Creatures",
            "inclusion": 50, "synergy": 5, "lift": 2.0,
            "mana_cost": "{2}{U}", "type_line": "Creature — Wizard",
            "price_eur": 1.0, "oracle_text": long_rules,
        }
        page = {
            "header": "Test Commander", "num_decks": 100, "fetched_at": None,
            "themes": [{"slug": "tokens", "decks": 20}],
            "bracket_counts": {"upgraded": 42}, "similar": [],
            "type_counts": {}, "mana_curve": {},
        }
        with patch.object(mcp_server, "engine") as engine:
            engine.return_value.edhrec_recommendations.return_value = {
                "page": page, "rows": [sample], "total": 1, "offset": 0, "notes": [],
            }
            result = mcp_server.edhrec_recommendations("Test Commander", bracket="upgraded")
            self.assertIn(long_rules, result)
            self.assertNotIn("Brackets:", result)
            forwarded = engine.return_value.edhrec_recommendations.call_args.kwargs
            self.assertTrue(forwarded["all_pages"])
            self.assertEqual(forwarded["bracket"], "upgraded")
            short = mcp_server.edhrec_recommendations("Test Commander", full_text=False)
            self.assertNotIn(long_rules, short)
            self.assertIn("…", short)
            self.assertIn("Brackets:", short)
            self.assertIn("`upgraded` (3; 42 decks)", short)
            mcp_server.edhrec_recommendations("Test Commander", all_pages=False)
            self.assertFalse(engine.return_value.edhrec_recommendations.call_args.kwargs["all_pages"])

    def test_edhrec_rejects_bad_filters_before_network_access(self):
        client = self.edhrec_client()
        with self.assertRaises(SearchError):
            self.engine.edhrec_recommendations("Test Commander", price_max=1, filters={"price_min": 2})
        client.commander_page.assert_not_called()

    def test_edhrec_legality_and_reverse_price_order(self):
        self.edhrec_client()
        result = self.engine.edhrec_recommendations("Test Commander", filters={"legalities": {"commander": "banned"}})
        self.assertEqual([r["name"] for r in result["rows"]], ["Banned Idol"])
        result = self.engine.edhrec_recommendations("Test Commander", sort="price", sort_reverse=True)
        self.assertEqual(result["rows"][0]["name"], "Colorless Engine")
        self.assertIsNone(result["rows"][-1]["price_eur"])


if __name__ == "__main__":
    unittest.main()
