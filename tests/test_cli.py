import tempfile
import unittest
from pathlib import Path

from keyword_scanner.apple import App
from keyword_scanner.cli import InputError, analyze, build_table, collect_keywords, read_seeds
from keyword_scanner.metrics import ScoringRules, compile_big_companies
from tests.helpers import project_config


class FakeStore:
    def __init__(self, hints: dict[str, list[str] | None], apps: dict[str, list[App] | None]) -> None:
        self.hints = hints
        self.apps = apps
        self.iap_checked: list[int] = []

    def suggestions(self, term: str):
        return self.hints.get(term, [])

    def top_apps(self, keyword: str):
        return self.apps.get(keyword)

    def has_in_app_purchases(self, track_id: int):
        self.iap_checked.append(track_id)
        return True


class ReadSeedsTest(unittest.TestCase):
    def test_reads_and_cleans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seeds.txt"
            path.write_text("﻿# 주석\nbudget\n\n  Budget \nhabit   tracker\n", encoding="utf-8")
            self.assertEqual(read_seeds(path), ["budget", "habit tracker"])

    def test_missing_or_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seeds.txt"
            with self.assertRaises(InputError):
                read_seeds(path)
            path.write_text("# only comment\n", encoding="utf-8")
            with self.assertRaises(InputError):
                read_seeds(path)


class FlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = project_config()
        self.rules = ScoringRules(
            self.config.criteria, self.config.weights, compile_big_companies(self.config.big_companies)
        )

    def test_collect_appends_letters_and_dedupes(self) -> None:
        store = FakeStore(
            hints={"budget a": ["Budget App", "budget alarm"], "budget b": ["budget app"], "budget c": None},
            apps={},
        )
        keywords = collect_keywords(store, ["budget"], self.config.collect)
        self.assertEqual(keywords, {"budget app": ["budget"], "budget alarm": ["budget"]})

    def test_analyze_skips_failed_and_checks_iap_only_for_free(self) -> None:
        store = FakeStore(
            hints={},
            apps={"ok": [App(1, "ok", "s", "a", 0.0, 10), App(2, "x", "s", "a", 1.99, 10)], "fail": None},
        )
        rows = list(analyze(store, {"ok": ["s"], "fail": ["s"]}, self.config, self.rules))
        self.assertEqual([r.keyword for r in rows], ["ok"])
        self.assertEqual(store.iap_checked, [1])
        self.assertEqual(rows[0].paid_or_iap_count, 2)

    def test_build_table_sorted_with_threshold_header(self) -> None:
        store = FakeStore(hints={}, apps={"hard": [App(1, "hard", "s", "a", 0.0, 90000)],
                                          "easy": [App(2, "x", "s", "a", 0.0, 5)]})
        rows = list(analyze(store, {"hard": ["s"], "easy": ["s"]}, self.config, self.rules))
        headers, table = build_table(rows, self.config)
        self.assertIn("리뷰 1,000개 미만 앱 수", headers)
        self.assertEqual([row[1] for row in table], ["easy", "hard"])
        self.assertEqual([row[0] for row in table], [1, 2])
        self.assertEqual(len(headers), len(table[0]))


if __name__ == "__main__":
    unittest.main()
