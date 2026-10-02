import io
import logging
import re
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from keyword_scanner.apple import App
from keyword_scanner.cli import InputError, analyze, build_table, collect_keywords, main, read_seeds
from keyword_scanner.metrics import ScoringRules, compile_big_companies
from tests.helpers import PROJECT_CONFIG, project_config


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

    def test_analyze_skips_done_keeping_numbering(self) -> None:
        store = FakeStore(hints={}, apps={k: [App(1, k, "s", "a", 1.0, 5)] for k in ("a", "b", "c")})
        with self.assertLogs("keyword_scanner", "INFO") as logs:
            rows = list(analyze(store, {"a": ["s"], "b": ["s"], "c": ["s"]}, self.config, self.rules, done={"b"}))
        self.assertEqual([r.keyword for r in rows], ["a", "c"])
        self.assertTrue(any("[3/3] c" in line for line in logs.output))


class InterruptingStore(FakeStore):
    """지정한 키워드를 분석하려는 순간 Ctrl+C가 눌린 것처럼 멈춘다."""

    def __init__(self, interrupt_on: str | None) -> None:
        super().__init__(
            hints={"budget a": ["budget app", "budget alarm", "budget art"]},
            apps={k: [App(1, k, "s", "a", 0.99, 5)] for k in ("budget app", "budget alarm", "budget art")},
        )
        self.interrupt_on = interrupt_on
        self.analyzed: list[str] = []
        self.hint_calls = 0

    def suggestions(self, term: str):
        self.hint_calls += 1
        return super().suggestions(term)

    def top_apps(self, keyword: str):
        if keyword == self.interrupt_on:
            raise KeyboardInterrupt
        self.analyzed.append(keyword)
        return super().top_apps(keyword)


class ResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config_path = self.dir / "config.toml"
        self.config_path.write_text(PROJECT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        (self.dir / "seeds.txt").write_text("budget\n", encoding="utf-8")
        self.output = self.dir / "keywords_result.xlsx"
        self.progress_file = self.dir / "keywords_result.progress.jsonl"
        self.saved_handlers = logging.getLogger().handlers[:]
        self.saved_level = logging.getLogger().level

    def tearDown(self) -> None:
        root = logging.getLogger()
        for handler in root.handlers:
            if handler not in self.saved_handlers:
                handler.close()
        root.handlers = self.saved_handlers
        root.setLevel(self.saved_level)
        self.tmp.cleanup()

    def run_main(self, store: InterruptingStore, *extra: str) -> int:
        with mock.patch("keyword_scanner.cli.AppleStore", return_value=store), redirect_stdout(io.StringIO()):
            return main(["--config", str(self.config_path), *extra])

    def test_interrupt_then_resume_only_remaining(self) -> None:
        first = InterruptingStore(interrupt_on="budget alarm")
        self.assertEqual(self.run_main(first), 130)
        self.assertEqual(first.analyzed, ["budget app"])
        self.assertTrue(self.progress_file.exists())

        second = InterruptingStore(interrupt_on=None)
        self.assertEqual(self.run_main(second), 0)
        self.assertEqual(second.hint_calls, 0)  # 자동완성을 다시 부르지 않음
        self.assertEqual(second.analyzed, ["budget alarm", "budget art"])
        self.assertEqual(self.excel_keywords(), {"budget app", "budget alarm", "budget art"})
        self.assertFalse(self.progress_file.exists())

    def test_failed_keyword_is_retried_on_resume(self) -> None:
        first = InterruptingStore(interrupt_on="budget art")
        first.apps["budget app"] = None  # 네트워크 실패로 건너뜀
        self.assertEqual(self.run_main(first), 130)
        second = InterruptingStore(interrupt_on=None)
        self.assertEqual(self.run_main(second), 0)
        self.assertEqual(second.analyzed, ["budget app", "budget art"])

    def test_no_keywords_collected_does_not_block_next_run(self) -> None:
        empty = InterruptingStore(interrupt_on=None)
        empty.hints = {}
        self.assertEqual(self.run_main(empty), 1)
        second = InterruptingStore(interrupt_on=None)
        self.assertEqual(self.run_main(second), 0)
        self.assertGreater(second.hint_calls, 0)

    def excel_keywords(self) -> set[str]:
        with zipfile.ZipFile(self.output) as zf:
            sheet = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
        return set(re.findall(r'<c r="B\d+" t="inlineStr"><is><t xml:space="preserve">([^<]*)</t>', sheet)) - {"키워드"}

    def test_fresh_starts_over(self) -> None:
        self.run_main(InterruptingStore(interrupt_on="budget alarm"))
        again = InterruptingStore(interrupt_on=None)
        self.assertEqual(self.run_main(again, "--fresh"), 0)
        self.assertEqual(again.analyzed, ["budget app", "budget alarm", "budget art"])

    def test_changed_settings_start_over(self) -> None:
        self.run_main(InterruptingStore(interrupt_on="budget alarm"))
        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(text.replace("title_match = 3", "title_match = 4"), encoding="utf-8")
        again = InterruptingStore(interrupt_on=None)
        with self.assertLogs("keyword_scanner.progress", "WARNING"):
            self.assertEqual(self.run_main(again), 0)
        self.assertEqual(again.analyzed, ["budget app", "budget alarm", "budget art"])

    def test_fresh_discards_old_progress_even_if_collection_is_interrupted(self) -> None:
        self.run_main(InterruptingStore(interrupt_on="budget alarm"))
        stopped = InterruptingStore(interrupt_on=None)
        stopped.suggestions = mock.Mock(side_effect=KeyboardInterrupt)
        self.run_main(stopped, "--fresh")
        self.assertFalse(self.progress_file.exists())


if __name__ == "__main__":
    unittest.main()
