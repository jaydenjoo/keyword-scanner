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
        with mock.patch("keyword_scanner.money_cli.HttpClient", return_value=client), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return main(["--config", str(self.config_path), *extra], today=TODAY)

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
        self.assertEqual(summary[2], ["habit", "1", "0", "1", "1", "아니오", "1", "", ""])
        self.assertEqual(len(summary), 3)  # 실패한 'broken' 은 빠짐

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
        self.assertEqual([row[0] for row in read_sheet(self.output)], ["키워드", "budget app", "habit"])
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
        self.assertEqual(summary[2][7:9], ["", ""])
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
        self.assertEqual([row[0] for row in read_sheet(self.output)], ["키워드", "budget app"])

    def test_output_option(self) -> None:
        other = self.dir / "other.xlsx"
        self.assertEqual(self.run_main(self.default_client(), "--output", str(other)), 0)
        self.assertTrue(other.exists())
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
