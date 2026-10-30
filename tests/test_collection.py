"""Collection import, storage and collection-scoped searches over the read-only card DB."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import search_index as si
from card_search import CardSearch, SearchError
from collection import parse_collection
from test_card_filters import build_fixture, fixture_cards


class ParseTests(unittest.TestCase):
    def test_decklist_lines_merge_names_and_quantities(self):
        res = parse_collection("Deck\n2 Guild Drake\n1x Blue Scout (ABC) 12 *F*\nguild drake\n")
        self.assertEqual([(c["name"], c["quantity"]) for c in res.cards],
                         [("Guild Drake", 3), ("Blue Scout", 1)])

    def test_csv_exports_with_quotes_and_separator_hint(self):
        moxfield = ('"Count","Tradelist Count","Name","Edition"\n'
                    '"2","0","Loki, God of Mischief","PMEI"\n"1","0","Loki, God of Mischief","SLD"\n'
                    '"x","0","Bad Row","SLD"\n\n')
        res = parse_collection(moxfield)
        self.assertEqual([(c["name"], c["quantity"]) for c in res.cards], [("Loki, God of Mischief", 3)])
        self.assertEqual(res.skipped[0]["reason"], "Quantity is not a number")
        dragon_shield = "sep=;\nCard Name;Quantity;Set\nSol Ring;4;C21\n"
        self.assertEqual([(c["name"], c["quantity"]) for c in parse_collection(dragon_shield).cards],
                         [("Sol Ring", 4)])
        # No quantity column: one copy per row.
        self.assertEqual(parse_collection("Name\tSet\nSol Ring\tC21\n").cards[0]["quantity"], 1)

    def test_names_with_commas_are_not_mistaken_for_csv(self):
        res = parse_collection("1 Loki, God of Mischief\nSol Ring")
        self.assertEqual([c["name"] for c in res.cards], ["Loki, God of Mischief", "Sol Ring"])


class CollectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "cards.db"
        conn = si.connect(str(self.path))
        build_fixture(conn)
        conn.close()
        self.original = self.path.read_bytes()
        self.engine = CardSearch(si.connect(str(self.path), read_only=True),
                                 collections_file=str(self.dir / "collections.db"))
        self.addCleanup(lambda: self.engine.collections().conn.close())
        self.addCleanup(self.engine.conn.close)
        self.engine.import_collection(
            "Binder", text="2 Guild Drake\n1x Blue Scout (ABC) 12 *F*\nColorless Engine\n"
                           "Day\nNonexistent Card\n1 guild drake\nTest Commander")

    def tearDown(self):
        self.assertEqual(self.path.read_bytes(), self.original)

    def names(self, **query):
        return {r["name"] for r in self.engine.search_cards(**query)["rows"]}

    def test_import_reports_matches_unmatched_and_spread(self):
        info = self.engine.collection_info("binder")  # names are case-insensitive
        self.assertEqual(info["name"], "Binder")
        self.assertEqual((info["unique_cards"], info["copies"]), (5, 7))
        self.assertEqual([u["name"] for u in info["unmatched"]], ["Nonexistent Card"])
        self.assertIn(("UB", 1), info["by_identity"])
        self.assertEqual(info["legendary_creatures"], 1)
        self.assertAlmostEqual(info["value_eur"], 3 * 2 + 0.5 + 10 + 2 + 2)

    def test_search_only_returns_owned_cards_with_quantities(self):
        rows = self.engine.search_cards(collection="Binder", sort="name")["rows"]
        self.assertEqual([(r["name"], r["owned"]) for r in rows], [
            ("Blue Scout", 1), ("Colorless Engine", 1), ("Day // Night", 1),
            ("Guild Drake", 3), ("Test Commander", 1)])
        self.assertNotIn("owned", self.engine.search_cards()["rows"][0])

    def test_tags_text_and_wording_work_inside_a_collection(self):
        self.assertEqual(self.names(collection="Binder", tags_any=["ramp"]), {"Blue Scout", "Colorless Engine"})
        self.assertEqual(self.names(collection="Binder", tags_all=["evasion"]), {"Guild Drake"})
        self.assertEqual(self.names(collection="Binder", text='"destroy target" artifact'), {"Day // Night"})
        self.assertEqual(self.names(collection="Binder", filters={"oracle_contains": "{C}{C}"}),
                         {"Colorless Engine"})
        self.assertEqual(self.names(collection="Binder", color_identity="U", exclude=["Blue Scout"]),
                         {"Colorless Engine", "Test Commander"})
        # Same query without a collection reaches cards the user does not own.
        self.assertIn("Unpriced Drake", self.names(text="flying", limit=100))
        self.assertNotIn("Unpriced Drake", self.names(collection="Binder", text="flying"))

    def test_tag_counts_and_analysis_for_a_collection(self):
        evasion = self.engine.find_tags("evasion", collection="Binder")[0]
        self.assertEqual((evasion["slug"], evasion["owned_count"]), ("evasion", 1))
        listed = self.engine.list_tags(collection="Binder")["tags"]
        self.assertEqual([(t["slug"], t["owned_count"]) for t in listed], [("ramp", 2), ("evasion", 1)])
        analysis = self.engine.analyze_deck(collection="Binder")
        self.assertEqual(analysis["card_count"], 5)
        self.assertEqual(analysis["roles"][0]["cards"], ["Blue Scout", "Colorless Engine"])

    def test_replace_add_delete_and_refresh_after_reimport(self):
        self.assertEqual(len(self.names(collection="Binder")), 5)
        csv_file = self.dir / "export.csv"
        csv_file.write_bytes("﻿Count,Name\n2,Siege\n".encode("utf-8"))
        self.engine.import_collection("binder", path=str(csv_file), mode="add")
        self.assertEqual(len(self.names(collection="Binder")), 6)
        self.engine.import_collection("Binder", text="Mind Sage")
        self.assertEqual(self.names(collection="Binder"), {"Mind Sage"})
        self.assertEqual(self.engine.collection_info("Binder")["unmatched"], [])
        self.assertEqual(self.engine.delete_collection("BINDER"), "Binder")
        self.assertEqual(self.engine.list_collections(), [])
        with self.assertRaisesRegex(SearchError, "No collection named"):
            self.names(collection="Binder")

    def test_import_errors_are_actionable(self):
        for kwargs in ({}, {"text": "Sol Ring", "path": "x.txt"}, {"path": str(self.dir / "missing.txt")},
                       {"text": "Deck\n\n"}, {"text": "Sol Ring", "mode": "merge"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SearchError):
                self.engine.import_collection("Other", **kwargs)
        with self.assertRaisesRegex(SearchError, "Available: Binder"):
            self.engine.search_cards(collection="Other")

    def test_edhrec_recommendations_and_average_deck_use_the_collection(self):
        pool = [dict(name=c["name"], section="Creatures", labels=[], inclusion=.5, synergy=.1,
                     lift=2, trend=0, num_decks=50) for c in fixture_cards()]
        self.engine._edhrec_client = SimpleNamespace(
            commander_page=Mock(return_value={"cards": pool}),
            average_deck=Mock(return_value={"sections": {"Creatures": [("Guild Drake", 1), ("Mind Sage", 1)],
                                                         "Land": [("Island", 10)]}}))
        rec = self.engine.edhrec_recommendations("Test Commander", collection="Binder", tags_any=["ramp"],
                                                 all_pages=True)
        self.assertEqual({(r["name"], r["owned"]) for r in rec["rows"]},
                         {("Blue Scout", 1), ("Colorless Engine", 1)})
        deck = self.engine.edhrec_average_deck("Test Commander", collection="Binder")
        self.assertEqual(deck["collection"]["owned"], 1)
        self.assertEqual(deck["collection"]["missing"], ["Mind Sage"])
        self.assertAlmostEqual(deck["collection"]["missing_price"], 2.0)

    def test_mcp_tools_format_collection_results(self):
        import mcp_server
        with patch.object(mcp_server, "engine", return_value=self.engine):
            out = mcp_server.import_collection("Spare", text="Siege\nNot A Card")
            self.assertIn("Imported 'Spare': 1 unique cards", out)
            self.assertIn("Not A Card", out)
            out = mcp_server.search_cards(collection="Binder", tags_any=["ramp"])
            self.assertIn("in collection 'Binder'", out)
            self.assertIn("| Own |", out)
            self.assertIn("in collection", mcp_server.find_tags("ramp", collection="Binder"))
            self.assertIn("Ramp (`ramp`): 2", mcp_server.analyze_deck(collection="Binder"))
            self.assertIn("either names", mcp_server.analyze_deck())
            self.assertIn("**Binder**", mcp_server.list_collections())
            self.assertIn("Nonexistent Card", mcp_server.collection_info("Binder"))
            self.assertIn("Deleted collection 'Spare'", mcp_server.delete_collection("spare"))
            self.assertTrue(mcp_server.search_cards(collection="Spare").startswith("Error: No collection"))


if __name__ == "__main__":
    unittest.main()
