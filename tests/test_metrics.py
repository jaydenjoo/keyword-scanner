import unittest

from keyword_scanner.apple import App
from keyword_scanner.metrics import ScoringRules, compile_big_companies, compute_stats, sort_weakest_first
from tests.helpers import project_config


def app(track_id: int, name: str = "x", reviews: int = 0, price: float = 0.0,
        seller: str = "Indie Dev", artist: str = "Indie Dev") -> App:
    return App(track_id=track_id, name=name, seller=seller, artist=artist, price=price, rating_count=reviews)


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        config = project_config()
        self.rules = ScoringRules(config.criteria, config.weights, compile_big_companies(config.big_companies))

    def test_basic_counts(self) -> None:
        apps = [
            app(1, "Budget App Pro", reviews=50),
            app(2, "My BUDGET APP", reviews=5000, price=1.99),
            app(3, "Money Tracker", reviews=999, seller="Google LLC"),
            app(4, "Wallet", reviews=200000),
        ]
        stats = compute_stats("budget app", "budget", apps, {1: True, 3: False, 4: None}, self.rules)
        self.assertEqual(stats.result_count, 4)
        self.assertEqual(stats.title_match_count, 2)        # 대소문자 무시
        self.assertEqual(stats.median_reviews, 2999.5)      # (999 + 5000) / 2
        self.assertEqual(stats.max_reviews, 200000)
        self.assertEqual(stats.low_review_count, 2)         # 50, 999 (1,000 미만)
        self.assertEqual(stats.big_company_count, 1)
        self.assertEqual(stats.big_company_names, "Google LLC")
        self.assertEqual(stats.paid_or_iap_count, 2)        # 1번 인앱 + 2번 유료
        self.assertEqual(stats.iap_unknown_count, 1)        # 4번 확인 실패

    def test_big_company_word_boundary(self) -> None:
        apps = [app(1, seller="Pineapple Studio"), app(2, seller="Applebee's"), app(3, seller="Apple Inc.")]
        stats = compute_stats("x", "s", apps, {}, self.rules)
        self.assertEqual(stats.big_company_count, 1)
        self.assertEqual(stats.big_company_names, "Apple Inc.")

    def test_big_company_matched_by_artist(self) -> None:
        stats = compute_stats("x", "s", [app(1, seller="Acme", artist="Microsoft Corporation")], {}, self.rules)
        self.assertEqual(stats.big_company_count, 1)

    def test_no_results(self) -> None:
        stats = compute_stats("zzz", "s", [], {}, self.rules)
        self.assertEqual((stats.result_count, stats.median_reviews, stats.max_reviews, stats.score), (0, 0, 0, 0.0))
        self.assertFalse(stats.is_app_name)

    def test_app_name_only_when_title_is_the_same(self) -> None:
        def flag(keyword: str, *names: str) -> bool:
            apps = [app(i, name) for i, name in enumerate(names)]
            return compute_stats(keyword, "s", apps, {}, self.rules).is_app_name

        self.assertTrue(flag("budget vials", "PinPoint", "Budget Vials"))
        self.assertTrue(flag("budgo - budget on the go", "Budgo – Budget  on the go "))  # 대시·공백 차이 무시
        self.assertFalse(flag("budget planner", "Fleur - Budget Planner App"))  # 포함만 됨 → 일반 검색어
        for dash in "\u2012\u2013\u2014\u2212":
            self.assertTrue(flag("budgo - budget", f"Budgo {dash} Budget"), repr(dash))
        self.assertFalse(flag("budget", "Budget Pro"))  # 앞부분만 같음

    def test_score_range_and_direction(self) -> None:
        weak = [app(i, "other", reviews=10) for i in range(10)]
        strong = [app(i, "budget app", reviews=500000, price=4.99, seller="Google LLC") for i in range(10)]
        weak_score = compute_stats("budget app", "s", weak, {}, self.rules).score
        strong_score = compute_stats("budget app", "s", strong, {}, self.rules).score
        self.assertLess(weak_score, strong_score)
        self.assertEqual(strong_score, 100.0)
        self.assertGreaterEqual(weak_score, 0.0)

    def test_sort_weakest_first(self) -> None:
        a = compute_stats("a", "s", [app(1, reviews=100000)], {}, self.rules)
        b = compute_stats("b", "s", [app(1, reviews=10)], {}, self.rules)
        self.assertEqual([r.keyword for r in sort_weakest_first([a, b])], ["b", "a"])


if __name__ == "__main__":
    unittest.main()
