import json
import unittest
from datetime import date

from keyword_scanner.cli import InputError
from keyword_scanner.money import (
    MoneyRules,
    compile_paid_words,
    months_ago,
    parse_money_search,
    select_pass_keywords,
    summarize,
)

PAID = compile_paid_words(("subscription", "pro", "in-app purchase"))


def item(track_id: object = 1, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "trackId": track_id,
        "trackName": "Budget",
        "sellerName": "Seller Inc",
        "price": 0.0,
        "userRatingCount": 500,
        "averageUserRating": 4.56789,
        "releaseDate": "2020-08-29T07:00:00Z",
        "currentVersionReleaseDate": "2026-09-02T02:17:21Z",
        "description": "Track spending.",
    }
    return {**base, **overrides}


def body(*items: dict[str, object]) -> bytes:
    return json.dumps({"resultCount": len(items), "results": list(items)}).encode("utf-8")


class ParseMoneySearchTest(unittest.TestCase):
    def test_reads_fields(self) -> None:
        [app] = parse_money_search(body(item()), PAID)
        self.assertEqual(app.track_id, 1)
        self.assertEqual(app.seller, "Seller Inc")
        self.assertEqual(app.rating_count, 500)
        self.assertEqual(app.average_rating, 4.57)
        self.assertEqual(app.released, date(2020, 8, 29))
        self.assertEqual(app.updated, date(2026, 9, 2))
        self.assertEqual(app.paid_words, ())
        self.assertFalse(app.is_paid_guess)

    def test_paid_words_whole_word_case_insensitive(self) -> None:
        text = "Our PRODUCT has a Subscription. Go Pro! Includes In-App Purchase options."
        [app] = parse_money_search(body(item(description=text)), PAID)
        self.assertEqual(app.paid_words, ("subscription", "pro", "in-app purchase"))
        self.assertTrue(app.is_paid_guess)

    def test_product_is_not_pro(self) -> None:
        [app] = parse_money_search(body(item(description="A great product for professionals")), PAID)
        self.assertEqual(app.paid_words, ())

    def test_price_alone_means_paid(self) -> None:
        [app] = parse_money_search(body(item(price=2.99)), PAID)
        self.assertTrue(app.is_paid_guess)

    def test_bad_values_become_empty(self) -> None:
        raw = item(description=None, releaseDate="soon", currentVersionReleaseDate=7, price="free",
                   averageUserRating=True)
        [app] = parse_money_search(body(raw), PAID)
        self.assertEqual((app.released, app.updated), (None, None))
        self.assertEqual((app.price, app.average_rating, app.paid_words), (0.0, 0.0, ()))

    def test_drops_items_without_track_id(self) -> None:
        apps = parse_money_search(body(item("x"), item(True), item(2)), PAID)
        self.assertEqual([a.track_id for a in apps], [2])

    def test_missing_or_bad_results_is_failure_not_zero_apps(self) -> None:
        for raw in (b'{"results": null}', b'{"errorMessage": "x"}', b'[1]'):
            with self.assertRaises(ValueError):
                parse_money_search(raw, PAID)
        self.assertEqual(parse_money_search(b'{"results": []}', PAID), [])

    def test_non_finite_numbers_become_zero(self) -> None:
        [app] = parse_money_search(b'{"results": [{"trackId": 1, "price": NaN, "averageUserRating": Infinity}]}', PAID)
        self.assertEqual((app.price, app.average_rating), (0.0, 0.0))

    def test_paid_words_across_line_breaks(self) -> None:
        [app] = parse_money_search(body(item(description="Start a free\ntrial or get In-App\u00a0Purchase")), PAID)
        self.assertEqual(app.paid_words, ("in-app purchase",))

    def test_unreadable_date_is_logged(self) -> None:
        with self.assertLogs("keyword_scanner.money", "WARNING"):
            parse_money_search(body(item(currentVersionReleaseDate="soon")), PAID)

    def test_not_json_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_money_search(b"<html>", PAID)


class MonthsAgoTest(unittest.TestCase):
    def test_clamps_to_month_end(self) -> None:
        self.assertEqual(months_ago(date(2026, 3, 31), 1), date(2026, 2, 28))
        self.assertEqual(months_ago(date(2026, 10, 3), 6), date(2026, 4, 3))
        self.assertEqual(months_ago(date(2026, 2, 15), 6), date(2025, 8, 15))
        self.assertEqual(months_ago(date(2028, 8, 31), 6), date(2028, 2, 29))


class SummarizeTest(unittest.TestCase):
    def rules(self, paid_min: int = 1) -> MoneyRules:
        return MoneyRules(low_review_threshold=1000, recent_since=date(2026, 4, 3), paid_competitor_min=paid_min)

    def test_counts_and_judgments(self) -> None:
        apps = parse_money_search(
            body(
                item(1, price=1.99, userRatingCount=5000, currentVersionReleaseDate="2026-04-03T00:00:00Z"),
                item(2, description="subscription", currentVersionReleaseDate="2026-04-02T23:59:59Z"),
                item(3, userRatingCount=999, currentVersionReleaseDate="bad"),
            ),
            PAID,
        )
        result = summarize("budget", apps, self.rules())
        self.assertEqual(result.keyword, "budget")
        self.assertEqual(result.paid_count, 2)
        self.assertEqual(result.recent_update_count, 1)  # 정확히 6개월 전은 포함, 하루 전·이상한 날짜는 제외
        self.assertEqual(result.low_review_count, 2)
        self.assertTrue(result.has_paying_competitor)
        self.assertEqual(result.live_competitor_count, 1)

    def test_no_paid_apps(self) -> None:
        apps = parse_money_search(body(item(1), item(2)), PAID)
        self.assertFalse(summarize("k", apps, self.rules()).has_paying_competitor)

    def test_paid_min_from_settings(self) -> None:
        apps = parse_money_search(body(item(1, price=1.0), item(2)), PAID)
        self.assertFalse(summarize("k", apps, self.rules(paid_min=2)).has_paying_competitor)

    def test_empty_result(self) -> None:
        result = summarize("k", [], self.rules())
        self.assertEqual((result.paid_count, result.live_competitor_count), (0, 0))
        self.assertFalse(result.has_paying_competitor)


class SelectPassKeywordsTest(unittest.TestCase):
    def test_picks_marked_rows(self) -> None:
        rows = [
            ["순위", "키워드", "경쟁 점수", " PASS "],
            ["1", "budget app", "10", "o"],
            ["2", "budget alarm", "11", "   "],
            ["3", "Budget  App", "12", "1"],
            ["4", "habit", "13"],
            ["5", "", "14", "o"],
            ["6", "money", "15", "x"],
        ]
        self.assertEqual(select_pass_keywords(rows, "키워드", "pass"), ["budget app", "money"])

    def test_unchecked_checkbox_and_error_cells_are_not_marked(self) -> None:
        rows = [["키워드", "pass"], ["a", "FALSE"], ["b", "#N/A"], ["c", "#DIV/0!"], ["d", "TRUE"], ["e", "0"]]
        self.assertEqual(select_pass_keywords(rows, "키워드", "pass"), ["d", "e"])

    def test_missing_pass_column(self) -> None:
        with self.assertRaisesRegex(InputError, "pass"):
            select_pass_keywords([["키워드"], ["a"]], "키워드", "pass")

    def test_missing_keyword_column(self) -> None:
        with self.assertRaisesRegex(InputError, "키워드"):
            select_pass_keywords([["pass"], ["o"]], "키워드", "pass")

    def test_nothing_marked(self) -> None:
        with self.assertRaisesRegex(InputError, "표시"):
            select_pass_keywords([["키워드", "pass"], ["a", ""]], "키워드", "pass")

    def test_empty_sheet(self) -> None:
        with self.assertRaises(InputError):
            select_pass_keywords([], "키워드", "pass")


if __name__ == "__main__":
    unittest.main()
