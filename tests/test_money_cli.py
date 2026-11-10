import io
import json
import logging
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from keyword_scanner.money_cli import main
from keyword_scanner.xlsx_reader import read_sheet
from keyword_scanner.xlsx_writer import write_xlsx
from tests.helpers import PROJECT_CONFIG

TODAY = date(2026, 10, 3)


def search_body(*apps: dict[str, object]) -> bytes:
    return json.dumps({"results": list(apps)}).encode("utf-8")


def app(track_id: int, price: float = 0.0, reviews: int = 10, updated: str = "2026-09-01T00:00:00Z",
        description: str = "") -> dict[str, object]:
    return {
        "trackId": track_id, "trackName": f"App {track_id}", "sellerName": "Maker", "price": price,
        "userRatingCount": reviews, "averageUserRating": 4.5, "releaseDate": "2021-01-02T00:00:00Z",
        "currentVersionReleaseDate": updated, "description": description,
    }


class FakeClient:
    """검색어 → 응답 본문(None = 재시도까지 실패)."""

    def __init__(self, bodies: dict[str, bytes | None]) -> None:
        self.bodies = bodies
        self.terms: list[str] = []
        self.interrupt_on: str | None = None

    def get(self, url: str, headers: dict[str, str] | None = None) -> bytes | None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertions(query)
        term = query["term"][0]
        if term == self.interrupt_on:
            raise KeyboardInterrupt
        self.terms.append(term)
        return self.bodies.get(term)

    @staticmethod
    def assertions(query: dict[str, list[str]]) -> None:
        if (query["country"], query["entity"], query["limit"]) != (["us"], ["software"], ["10"]):
            raise AssertionError(f"검색 조건이 다름: {query}")


class MoneyCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config_path = self.dir / "config.toml"
        self.config_path.write_text(PROJECT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        self.input = self.dir / "keywords_result.xlsx"
        self.output = self.dir / "money_check.xlsx"
        self.log_file = self.dir / "money_check.log"
        write_xlsx(
            self.input, "keywords", ["순위", "키워드", "pass"],
            [[1, "budget app", "o"], [2, "skip me", ""], [3, "broken", "o"], [4, "habit", "v"]],
        )
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

    def run_main(self, client: FakeClient, *extra: str) -> int:
        """화면 출력(INFO 이상)은 self.screen 에 남긴다. 로그 파일에는 WARNING 이상만 들어간다."""
        screen = io.StringIO()
        with mock.patch("keyword_scanner.money_cli.HttpClient", return_value=client), \
                redirect_stdout(screen), redirect_stderr(io.StringIO()):
            code = main(["--config", str(self.config_path), *extra], today=TODAY)
        self.screen = screen.getvalue()
        return code

    def default_client(self) -> FakeClient:
        return FakeClient({
            "budget app": search_body(
                app(1, price=2.99, reviews=5000),
                app(2, description="Start your free trial", updated="2025-01-01T00:00:00Z"),
            ),
            "broken": None,
            "habit": search_body(app(3)),
        })

    def test_writes_summary_and_details(self) -> None:
        client = self.default_client()
        self.assertEqual(self.run_main(client), 0)
        self.assertEqual(client.terms, ["budget app", "broken", "habit"])  # pass 없는 키워드는 요청 안 함

        summary = read_sheet(self.output)
        header = summary[0]
        self.assertEqual(header[:5], ["키워드", "조회된 앱 수", "유료 추정 앱 수", "최근 6개월 업데이트 앱 수",
                                      "리뷰 1,000개 미만 앱 수"])
        self.assertEqual(header[5:], ["돈 내는 경쟁자 있음", "살아 있는 경쟁자 수", "리뷰 불만 반복", "Reddit 불만"])
        self.assertEqual(summary[1], ["budget app", "2", "2", "1", "1", "예", "1", "", ""])
        self.assertEqual(summary[2], ["broken", "미검사", "", "", "", "", "", "", ""])  # 실패해도 줄은 남김
        self.assertEqual(summary[3], ["habit", "1", "0", "1", "1", "아니오", "1", "", ""])
        self.assertEqual(len(summary), 4)

        log_text = self.log_file.read_text(encoding="utf-8")
        self.assertIn("broken", log_text)

    def test_detail_sheet(self) -> None:
        self.run_main(self.default_client())
        details = read_sheet(self.output, 1)
        self.assertEqual(details[0], ["키워드", "순위", "앱 이름", "판매자", "가격", "평점 수", "평균 평점", "출시일",
                                      "최근 업데이트일", "유료 단어 포함", "찾은 유료 단어", "유료 추정"])
        self.assertEqual(details[1], ["budget app", "1", "App 1", "Maker", "2.99", "5000", "4.5", "2021-01-02",
                                      "2026-09-01", "아니오", "", "예"])
        self.assertEqual(details[2][9:], ["예", "free trial", "예"])
        self.assertEqual(len(details), 4)

    def test_bad_search_response_is_skipped_and_logged(self) -> None:
        client = self.default_client()
        client.bodies["broken"] = b'{"errorMessage": "Invalid value(s) for key(s)"}'
        self.assertEqual(self.run_main(client), 0)
        self.assertEqual([row[:2] for row in read_sheet(self.output)[1:]],
                         [["budget app", "2"], ["broken", "미검사"], ["habit", "1"]])
        self.assertIn("broken", self.log_file.read_text(encoding="utf-8"))

    def test_rerun_keeps_manual_notes_and_backs_up(self) -> None:
        self.assertEqual(self.run_main(self.default_client()), 0)
        rows = read_sheet(self.output)
        rows[1][7:9] = ["앱이 자주 멈춤", "r/budget 글 3개"]
        write_xlsx(self.output, "summary", rows[0], rows[1:])  # Jayden이 엑셀에서 메모를 적고 저장한 상태

        self.assertEqual(self.run_main(self.default_client()), 0)
        summary = read_sheet(self.output)
        self.assertEqual(summary[1][0], "budget app")
        self.assertEqual(summary[1][7:9], ["앱이 자주 멈춤", "r/budget 글 3개"])
        self.assertEqual(summary[3][7:9], ["", ""])
        backup = self.dir / "money_check.bak.xlsx"
        self.assertEqual(read_sheet(backup)[1][7], "앱이 자주 멈춤")

    def test_unreadable_old_output_is_backed_up_not_blocking(self) -> None:
        self.output.write_bytes(b"not an xlsx")
        self.assertEqual(self.run_main(self.default_client()), 0)
        self.assertEqual((self.dir / "money_check.bak.xlsx").read_bytes(), b"not an xlsx")
        self.assertEqual(read_sheet(self.output)[1][0], "budget app")

    def test_nothing_analyzed_writes_nothing(self) -> None:
        client = FakeClient({})
        self.assertEqual(self.run_main(client), 1)
        self.assertFalse(self.output.exists())

    def test_missing_input_or_pass_column(self) -> None:
        self.assertEqual(self.run_main(FakeClient({}), "--input", str(self.dir / "nope.xlsx")), 2)
        write_xlsx(self.input, "keywords", ["키워드"], [["a"]])
        self.assertEqual(self.run_main(FakeClient({})), 2)

    def test_interrupt_saves_partial(self) -> None:
        client = self.default_client()
        client.interrupt_on = "habit"
        self.assertEqual(self.run_main(client), 130)
        self.assertEqual([row[:2] for row in read_sheet(self.output)[1:]],
                         [["budget app", "2"], ["broken", "미검사"], ["habit", "미검사"]])

    @property
    def progress_file(self) -> Path:
        return self.dir / "money_check.progress.jsonl"

    def test_resume_after_interrupt_skips_done_keywords(self) -> None:
        first = self.default_client()
        first.interrupt_on = "habit"
        self.assertEqual(self.run_main(first), 130)
        self.assertTrue(self.progress_file.exists())

        second = self.default_client()
        self.assertEqual(self.run_main(second), 0)
        self.assertEqual(second.terms, ["broken", "habit"])  # 'budget app' 은 다시 요청하지 않음
        self.assertEqual([row[0] for row in read_sheet(self.output)], ["키워드", "budget app", "broken", "habit"])
        self.assertEqual(read_sheet(self.output)[1][:7], ["budget app", "2", "2", "1", "1", "예", "1"])
        self.assertIn("이어서 합니다: 키워드 3개 중 1개는 이미 완료", self.screen)

    def test_failed_keyword_is_retried_and_record_removed_when_all_done(self) -> None:
        self.assertEqual(self.run_main(self.default_client()), 0)
        self.assertTrue(self.progress_file.exists())  # 'broken' 이 실패해서 남겨 둠

        retry = self.default_client()
        retry.bodies["broken"] = search_body(app(9))
        self.assertEqual(self.run_main(retry), 0)
        self.assertEqual(retry.terms, ["broken"])
        self.assertEqual([row[0] for row in read_sheet(self.output)], ["키워드", "budget app", "broken", "habit"])
        self.assertFalse(self.progress_file.exists())

    def test_fresh_ignores_record(self) -> None:
        self.run_main(self.default_client())
        again = self.default_client()
        self.run_main(again, "--fresh")
        self.assertEqual(again.terms, ["budget app", "broken", "habit"])

    def test_changed_paid_words_start_over(self) -> None:
        self.run_main(self.default_client())
        text = self.config_path.read_text(encoding="utf-8")
        self.config_path.write_text(text.replace('"subscription", ', "", 1), encoding="utf-8")
        again = self.default_client()
        self.run_main(again)
        self.assertEqual(again.terms, ["budget app", "broken", "habit"])

    def write_notes(self, notes: dict[str, list[str]]) -> None:
        """Jayden이 summary 시트의 직접 입력 열(8·9번째)에 메모를 적고 저장한 상태를 만든다."""
        rows = read_sheet(self.output)
        for row in rows[1:]:
            row[7:9] = notes.get(row[0], row[7:9])
        rows += [[keyword, *[""] * 6, *values] for keyword, values in notes.items()
                 if keyword not in [row[0] for row in rows]]
        write_xlsx(self.output, "summary", rows[0], rows[1:])

    def test_notes_on_unchecked_keywords_survive_repeated_interrupts(self) -> None:
        self.assertEqual(self.run_main(self.default_client()), 0)  # 'broken' 실패
        self.write_notes({"broken": ["불만 메모", "레딧 메모"], "habit": ["습관 메모", ""]})

        for _ in range(2):  # 예전에는 두 번째 실행에서 'broken' 메모가 백업에서도 사라짐
            client = self.default_client()
            client.interrupt_on = "broken"
            self.assertEqual(self.run_main(client), 130)
            summary = read_sheet(self.output)
            self.assertEqual(summary[2], ["broken", "미검사", "", "", "", "", "", "불만 메모", "레딧 메모"])
            self.assertEqual([summary[3][0], *summary[3][7:9]], ["habit", "습관 메모", ""])

    def test_notes_of_keywords_no_longer_pass_are_logged(self) -> None:
        self.assertEqual(self.run_main(self.default_client()), 0)
        self.write_notes({"old idea": ["예전 메모", ""]})
        self.assertEqual(self.run_main(self.default_client()), 0)
        self.assertNotIn("old idea", [row[0] for row in read_sheet(self.output)])
        self.assertEqual(read_sheet(self.dir / "money_check.bak.xlsx")[-1][:1], ["old idea"])
        warning = [line for line in self.log_file.read_text(encoding="utf-8").splitlines() if "pass 목록에 없는" in line]
        self.assertEqual(len(warning), 1)
        self.assertIn("old idea", warning[0])
        self.assertNotIn("budget app", warning[0])  # pass 키워드는 경고 목록에 섞이지 않음

    def test_notes_match_keywords_ignoring_case_and_spaces(self) -> None:
        self.assertEqual(self.run_main(self.default_client()), 0)
        rows = read_sheet(self.output)
        rows[1][0], rows[1][7] = "Budget  App", "대소문자 다른 메모"
        write_xlsx(self.output, "summary", rows[0], rows[1:])
        self.assertEqual(self.run_main(self.default_client()), 0)
        self.assertEqual(read_sheet(self.output)[1][7], "대소문자 다른 메모")

    def test_nothing_checked_keeps_old_result_and_notes(self) -> None:
        self.assertEqual(self.run_main(self.default_client()), 0)
        self.write_notes({"budget app": ["지켜야 할 메모", ""]})
        before = self.output.read_bytes()
        self.assertEqual(self.run_main(FakeClient({}), "--fresh"), 1)  # 전부 실패
        interrupted = self.default_client()
        interrupted.interrupt_on = "budget app"
        self.assertEqual(self.run_main(interrupted, "--fresh"), 130)  # 첫 키워드에서 중단
        self.assertEqual(self.output.read_bytes(), before)

    def test_save_log_counts_checked_and_unchecked(self) -> None:
        self.assertEqual(self.run_main(self.default_client()), 0)
        self.assertIn("검사한 키워드 2개, 미검사 1개", self.screen)

    def test_notes_columns_missing_from_first_sheet_are_warned(self) -> None:
        self.assertEqual(self.run_main(self.default_client()), 0)
        rows = read_sheet(self.output)
        rows[0][8] = "Reddit"  # 직접 입력 열 머리글을 바꿈 → 그 열 메모는 옮길 수 없음
        write_xlsx(self.output, "summary", rows[0], rows[1:])
        self.assertEqual(self.run_main(self.default_client()), 0)
        self.assertIn("Reddit 불만", self.log_file.read_text(encoding="utf-8"))
        write_xlsx(self.output, "apps", ["순위", "앱 이름"], [["1", "x"]])  # summary 가 첫 시트가 아님
        self.assertEqual(self.run_main(self.default_client()), 0)
        self.assertIn("'키워드' 열이 없어", self.log_file.read_text(encoding="utf-8"))

    def test_output_option(self) -> None:
        other = self.dir / "other.xlsx"
        self.assertEqual(self.run_main(self.default_client(), "--output", str(other)), 0)
        self.assertTrue(other.exists())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
