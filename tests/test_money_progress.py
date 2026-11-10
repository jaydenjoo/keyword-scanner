import dataclasses
import tempfile
import unittest
from datetime import date
from pathlib import Path

from keyword_scanner import money_progress
from keyword_scanner.config import load_money_config
from keyword_scanner.money import MoneyApp
from tests.helpers import PROJECT_CONFIG


def money_app(track_id: int, **changes: object) -> MoneyApp:
    base = MoneyApp(
        track_id=track_id, name=f"App {track_id}", seller="Maker", price=0.0, rating_count=10,
        average_rating=4.5, released=date(2021, 1, 2), updated=date(2026, 9, 1), paid_words=(),
    )
    return dataclasses.replace(base, **changes)


class MoneyProgressTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "money_check.progress.jsonl"
        self.config = load_money_config(PROJECT_CONFIG)
        self.keywords = ["a", "b", "c"]
        self.fp = money_progress.fingerprint(self.keywords, self.config)
        self.today = date(2026, 11, 10)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, done: dict[str, tuple[MoneyApp, ...]], *new: tuple[str, tuple[MoneyApp, ...]]) -> None:
        with money_progress.open_writer(self.path, self.fp, self.today, done) as writer:
            for keyword, apps in new:
                writer.append(keyword, apps)

    def load(self, today: date | None = None) -> dict[str, tuple[MoneyApp, ...]] | None:
        return money_progress.load(self.path, self.fp, self.keywords, today or self.today)

    def replace_line(self, number: int, text: str) -> None:
        lines = self.path.read_text(encoding="utf-8").split("\n")
        lines[number - 1] = text
        self.path.write_text("\n".join(lines), encoding="utf-8")

    def test_round_trip_keeps_every_field(self) -> None:
        tricky = money_app(
            7, name="정비 기록 🚗 Log", seller="Ünïcode Co.", price=2.99, rating_count=12345,
            average_rating=4.25, released=None, updated=date(2026, 10, 31), paid_words=("premium", "free trial"),
        )
        self.write({}, ("a", (tricky, money_app(8))), ("b", ()))
        self.assertEqual(self.load(), {"a": (tricky, money_app(8)), "b": ()})

    def test_reopen_keeps_done_and_appends(self) -> None:
        self.write({}, ("a", (money_app(1),)))
        done = self.load()
        self.assertIsNotNone(done)
        self.write(done or {}, ("b", (money_app(2),)))
        self.assertEqual(self.load(), {"a": (money_app(1),), "b": (money_app(2),)})

    def test_cut_off_or_broken_line_drops_it_and_everything_after(self) -> None:
        self.write({}, ("a", (money_app(1),)), ("b", (money_app(2),)))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"keyword": "c", "apps": [{"track_id": 3, "na')  # 강제 종료로 쓰다 만 줄
        self.assertEqual(set(self.load() or {}), {"a", "b"})

        lines = self.path.read_text(encoding="utf-8").split("\n")
        lines[1] = '{"keyword": "a", "apps": [{"track_id": 1}]}'  # 필드가 빠진 줄
        self.path.write_text("\n".join(lines), encoding="utf-8")
        self.assertEqual(self.load(), {})

        lines[1] = '{"keyword": "a", "apps": [{"track_id": 1, "name": "x", "seller": "", "price": 0, ' \
            '"rating_count": 1, "average_rating": 0, "released": "not a date", "updated": null, "paid_words": []}]}'
        self.path.write_text("\n".join(lines), encoding="utf-8")
        self.assertEqual(self.load(), {})

    def test_wrong_fingerprint_or_header_starts_over(self) -> None:
        self.assertIsNone(self.load())  # 파일 없음
        self.write({}, ("a", (money_app(1),)))
        self.assertIsNone(money_progress.load(self.path, "other", self.keywords, self.today))
        self.path.write_text("not json\n", encoding="utf-8")
        self.assertIsNone(self.load())
        self.path.write_text('{"version": 999, "fingerprint": "%s"}\n' % self.fp, encoding="utf-8")
        self.assertIsNone(self.load())

    def test_record_from_another_day_starts_over(self) -> None:
        self.write({}, ("a", (money_app(1),)))
        self.assertIsNone(self.load(date(2026, 11, 11)))  # 지난 검색 결과를 모르는 사이에 다시 쓰지 않음

    def test_strange_values_are_treated_as_broken_lines(self) -> None:
        good = (
            '{{"keyword": "a", "apps": [{{"track_id": 1, "name": "x", "seller": "", "price": {price}, '
            '"rating_count": {count}, "average_rating": 0.0, "released": null, "updated": null, "paid_words": []}}]}}'
        )
        self.write({}, ("b", (money_app(2),)))
        for price, count in [("NaN", "1"), ("Infinity", "1"), ("1" + "0" * 400, "1"), ("0.0", "true")]:
            with self.subTest(price=price[:10], count=count):
                self.replace_line(2, good.format(price=price, count=count))
                self.assertEqual(self.load(), {})
        self.replace_line(2, '{"keyword": "a", "apps": ' + "[" * 100_000 + "]" * 100_000 + "}")
        self.assertEqual(self.load(), {})

    def test_only_listed_keywords_and_any_key_order(self) -> None:
        self.write({}, ("zzz", (money_app(1),)), ("a", (money_app(2),)))
        self.assertEqual(self.load(), {"a": (money_app(2),)})  # 목록에 없는 키워드 줄은 버림
        reordered = '{"apps": [{"paid_words": [], "updated": null, "released": null, "average_rating": 4.5, ' \
            '"rating_count": 10, "price": 0.0, "seller": "Maker", "name": "App 3", "track_id": 3}], "keyword": "b"}'
        self.replace_line(2, reordered)
        self.assertEqual(self.load(), {"b": (money_app(3, released=None, updated=None),), "a": (money_app(2),)})

    def test_fingerprint_follows_search_settings_not_dates(self) -> None:
        same = money_progress.fingerprint(self.keywords, dataclasses.replace(self.config, recent_update_months=3))
        self.assertEqual(same, self.fp)
        fewer_words = dataclasses.replace(self.config, paid_words=self.config.paid_words[1:])
        self.assertNotEqual(money_progress.fingerprint(self.keywords, fewer_words), self.fp)
        self.assertNotEqual(money_progress.fingerprint(["a", "b"], self.config), self.fp)
        self.assertEqual(money_progress.fingerprint(["c", "a", "b"], self.config), self.fp)  # 순서만 바뀜


if __name__ == "__main__":
    unittest.main()
