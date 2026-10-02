import dataclasses
import tempfile
import unittest
from pathlib import Path

from keyword_scanner import progress
from keyword_scanner.metrics import KeywordStats
from tests.helpers import project_config


def make_stats(keyword: str) -> KeywordStats:
    return KeywordStats(keyword, "budget", 10, 2, 150.5, 9000, 4, 1, "Intuit Inc.", 3, 0, 41.2, False)


KEYWORDS = {"budget app": ["budget"], "budget alarm": ["budget"]}


class FingerprintTest(unittest.TestCase):
    def test_changes_with_result_settings_only(self) -> None:
        config = project_config()
        base = progress.fingerprint(["budget"], config)
        self.assertEqual(base, progress.fingerprint(["budget"], config))
        self.assertNotEqual(base, progress.fingerprint(["habit"], config))
        heavier = dataclasses.replace(config, weights=dataclasses.replace(config.weights, title_match=9.0))
        self.assertNotEqual(base, progress.fingerprint(["budget"], heavier))
        slower = dataclasses.replace(config, request=dataclasses.replace(config.request, delay_seconds=5.0))
        self.assertEqual(base, progress.fingerprint(["budget"], slower))


class SaveLoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "result.progress.jsonl"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_path_next_to_output(self) -> None:
        self.assertEqual(
            progress.progress_path(Path("/x/keywords_result.xlsx")), Path("/x/keywords_result.progress.jsonl")
        )

    def test_round_trip_and_resume_append(self) -> None:
        with progress.open_writer(self.path, "fp1", KEYWORDS, []) as writer:
            writer.append(make_stats("budget app"))
        saved = progress.load(self.path, "fp1")
        self.assertEqual(saved.keywords, KEYWORDS)
        self.assertEqual(saved.rows, [make_stats("budget app")])

        with progress.open_writer(self.path, "fp1", saved.keywords, saved.rows) as writer:
            writer.append(make_stats("budget alarm"))
        self.assertEqual(
            progress.load(self.path, "fp1").rows, [make_stats("budget app"), make_stats("budget alarm")]
        )

    def test_iap_results_round_trip(self) -> None:
        with progress.open_writer(self.path, "fp1", KEYWORDS, []) as writer:
            writer.append(make_stats("budget app"), {1: True, 2: None})
            writer.append(make_stats("budget alarm"), {1: True, 3: False})
        saved = progress.load(self.path, "fp1")
        self.assertEqual(saved.iap, {1: True, 3: False})  # 확인 실패(None)는 저장 안 함
        self.assertEqual(len(saved.rows), 2)

        with progress.open_writer(self.path, "fp1", saved.keywords, saved.rows, saved.iap):
            pass
        self.assertEqual(progress.load(self.path, "fp1").iap, {1: True, 3: False})

    def test_broken_iap_line_stops_like_a_cut_line(self) -> None:
        broken_lines = [
            '{"iap": {"5": "yes"}}',  # 값이 true/false가 아님
            '{"iap": {"²": true}}',  # 숫자처럼 보이지만 int()가 못 읽는 글자
            '{"iap": {"١": true}}',  # 아라비아 숫자 1 → 다른 앱 번호로 바뀌면 안 됨
        ]
        for broken in broken_lines:
            with self.subTest(broken=broken):
                with progress.open_writer(self.path, "fp1", KEYWORDS, []) as writer:
                    writer.append(make_stats("budget app"), {1: True})
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(broken + "\n")
                    handle.write(progress._stats_line(make_stats("budget alarm")))
                with self.assertLogs("keyword_scanner.progress", "WARNING"):
                    saved = progress.load(self.path, "fp1")
                self.assertEqual((saved.iap, [r.keyword for r in saved.rows]), ({1: True}, ["budget app"]))

    def test_iap_kept_when_following_stats_line_is_cut(self) -> None:
        with progress.open_writer(self.path, "fp1", KEYWORDS, []) as writer:
            writer.append(make_stats("budget app"), {1: True})
            writer.append(make_stats("budget alarm"), {2: False})
        text = self.path.read_text(encoding="utf-8")
        self.path.write_text(text[: len(text) - 20], encoding="utf-8")  # 마지막 결과 줄만 잘림
        with self.assertLogs("keyword_scanner.progress", "WARNING"):
            saved = progress.load(self.path, "fp1")
        self.assertEqual((saved.iap, [r.keyword for r in saved.rows]), ({1: True, 2: False}, ["budget app"]))

        with progress.open_writer(self.path, "fp1", saved.keywords, saved.rows, saved.iap) as writer:
            writer.append(make_stats("budget alarm"), {1: True, 2: False})  # 이미 적은 결과는 다시 안 적음
        self.assertEqual(self.path.read_text(encoding="utf-8").count('"iap"'), 1)
        self.assertEqual(progress.load(self.path, "fp1").iap, {1: True, 2: False})

    def test_file_without_iap_lines_still_loads(self) -> None:
        with progress.open_writer(self.path, "fp1", KEYWORDS, []) as writer:
            writer.append(make_stats("budget app"))
        self.assertEqual(progress.load(self.path, "fp1").iap, {})

    def test_missing_or_other_fingerprint_or_broken_header(self) -> None:
        self.assertIsNone(progress.load(self.path, "fp1"))
        with progress.open_writer(self.path, "fp1", KEYWORDS, []):
            pass
        with self.assertLogs("keyword_scanner.progress", "WARNING"):
            self.assertIsNone(progress.load(self.path, "fp2"))
        self.path.write_text("not json\n", encoding="utf-8")
        with self.assertLogs("keyword_scanner.progress", "WARNING"):
            self.assertIsNone(progress.load(self.path, "fp1"))

    def test_cut_last_line_is_dropped_and_rewritten(self) -> None:
        with progress.open_writer(self.path, "fp1", KEYWORDS, []) as writer:
            writer.append(make_stats("budget app"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"keyword": "budget al')  # 강제 종료로 쓰다 만 줄
        with self.assertLogs("keyword_scanner.progress", "WARNING"):
            saved = progress.load(self.path, "fp1")
        self.assertEqual(saved.rows, [make_stats("budget app")])

        with progress.open_writer(self.path, "fp1", saved.keywords, saved.rows) as writer:
            writer.append(make_stats("budget alarm"))
        self.assertEqual([r.keyword for r in progress.load(self.path, "fp1").rows], ["budget app", "budget alarm"])

    def test_unusual_characters_survive_and_file_is_ascii(self) -> None:
        odd = {"café budget": ["budget"]}  # U+2028: splitlines()가 줄로 잘라 버리는 문자
        with progress.open_writer(self.path, "fp1", odd, []) as writer:
            writer.append(make_stats("café budget"))
        self.path.read_bytes().decode("ascii")  # 여러 바이트 문자가 없어야 중간에 잘려도 안전
        saved = progress.load(self.path, "fp1")
        self.assertEqual(saved.keywords, odd)
        self.assertEqual([r.keyword for r in saved.rows], ["café budget"])

    def test_empty_keyword_list_is_not_resumed(self) -> None:
        with progress.open_writer(self.path, "fp1", {}, []):
            pass
        self.assertIsNone(progress.load(self.path, "fp1"))

    def test_remove(self) -> None:
        with progress.open_writer(self.path, "fp1", KEYWORDS, []):
            pass
        progress.remove(self.path)
        self.assertFalse(self.path.exists())
        progress.remove(self.path)  # 없어도 오류 없음


if __name__ == "__main__":
    unittest.main()
