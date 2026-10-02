import json
import plistlib
import unittest

from keyword_scanner.apple import AppleStore, parse_hints, parse_search, page_has_iap
from tests.helpers import project_config

HINTS_BODY = plistlib.dumps(
    {
        "title": "Suggestions",
        "hints": [
            {"term": "budget app", "url": "https://x"},
            {"term": "  budget planner ", "url": "https://x"},
            {"term": ""},
            {"url": "no term"},
        ],
    }
)

SEARCH_BODY = json.dumps(
    {
        "resultCount": 3,
        "results": [
            {"trackId": 1, "trackName": "Budget App", "sellerName": "A LLC", "artistName": "A",
             "price": 0.0, "userRatingCount": 1500},
            {"trackId": 2, "trackName": "Money", "sellerName": "B", "artistName": "B", "price": 2.99},
            {"trackName": "no id"},
        ],
    }
).encode()


class ParseTest(unittest.TestCase):
    def test_parse_hints(self) -> None:
        self.assertEqual(parse_hints(HINTS_BODY), ["budget app", "budget planner"])

    def test_parse_hints_empty(self) -> None:
        self.assertEqual(parse_hints(plistlib.dumps({"title": "Suggestions"})), [])

    def test_parse_search(self) -> None:
        apps = parse_search(SEARCH_BODY)
        self.assertEqual([a.track_id for a in apps], [1, 2])
        self.assertEqual(apps[0].rating_count, 1500)
        self.assertEqual(apps[1].rating_count, 0)  # 리뷰 수 없음 → 0
        self.assertEqual(apps[1].price, 2.99)

    def test_parse_search_invalid_json(self) -> None:
        with self.assertRaises(ValueError):
            parse_search(b"<html>")

    def test_page_has_iap(self) -> None:
        yes = '{"$kind":"Annotation","title":"Seller"},{"$kind":"Annotation","title":"In-App Purchases","summary":"Yes"}'
        no = '{"$kind":"Annotation","title":"Seller"},{"$kind":"Annotation","title":"Size"}'
        self.assertTrue(page_has_iap(yes))
        self.assertFalse(page_has_iap(no))
        self.assertIsNone(page_has_iap("<html>다른 모양</html>"))


class FakeClient:
    def __init__(self, bodies: dict[str, bytes | None]) -> None:
        self.bodies = bodies
        self.urls: list[str] = []
        self.headers: list = []

    def get(self, url: str, headers=None):
        self.urls.append(url)
        self.headers.append(headers)
        for key, body in self.bodies.items():
            if key in url:
                return body
        return None


class AppleStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.collect = project_config().collect

    def test_suggestions_uses_us_store_front(self) -> None:
        client = FakeClient({"MZSearchHints": HINTS_BODY})
        store = AppleStore(client, self.collect)
        self.assertEqual(store.suggestions("budget a"), ["budget app", "budget planner"])
        self.assertIn("term=budget+a", client.urls[0])
        self.assertEqual(client.headers[0], {"X-Apple-Store-Front": "143441-1,29"})

    def test_suggestions_failure_returns_none(self) -> None:
        store = AppleStore(FakeClient({}), self.collect)
        self.assertIsNone(store.suggestions("budget a"))

    def test_suggestions_bad_body_returns_none(self) -> None:
        store = AppleStore(FakeClient({"MZSearchHints": b"not plist"}), self.collect)
        with self.assertLogs("keyword_scanner.apple"):
            self.assertIsNone(store.suggestions("budget a"))

    def test_top_apps_query(self) -> None:
        client = FakeClient({"itunes.apple.com/search": SEARCH_BODY})
        apps = AppleStore(client, self.collect).top_apps("budget app")
        self.assertEqual(len(apps), 2)
        for part in ["country=us", "entity=software", "limit=10", "term=budget+app"]:
            self.assertIn(part, client.urls[0])

    def test_iap_checked_once_per_app(self) -> None:
        page = b'{"$kind":"Annotation","title":"In-App Purchases","summary":"Yes"}'
        client = FakeClient({"apps.apple.com/us/app/id7": page})
        store = AppleStore(client, self.collect)
        self.assertTrue(store.has_in_app_purchases(7))
        self.assertTrue(store.has_in_app_purchases(7))
        self.assertEqual(len(client.urls), 1)

    def test_iap_network_failure_is_unknown(self) -> None:
        store = AppleStore(FakeClient({}), self.collect)
        self.assertIsNone(store.has_in_app_purchases(7))

    def test_known_iap_excludes_unknown_and_preload_skips_requests(self) -> None:
        page = b'{"$kind":"Annotation","title":"Ratings"}'
        store = AppleStore(FakeClient({"apps.apple.com/us/app/id7": page}), self.collect)
        store.has_in_app_purchases(7)  # 없음
        store.has_in_app_purchases(8)  # 실패 → 다음 실행에서 다시 확인해야 하므로 저장 대상 아님
        self.assertEqual(store.known_in_app_purchases(), {7: False})

        client = FakeClient({})
        resumed = AppleStore(client, self.collect)
        resumed.preload_in_app_purchases({7: False, 9: True})
        self.assertFalse(resumed.has_in_app_purchases(7))
        self.assertTrue(resumed.has_in_app_purchases(9))
        self.assertEqual(client.urls, [])


if __name__ == "__main__":
    unittest.main()
