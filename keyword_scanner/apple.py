"""애플 자동완성·검색 API·앱 상세 페이지 호출과 응답 해석."""

import json
import logging
import plistlib
import re
import urllib.parse
from dataclasses import dataclass

from .config import CollectConfig
from .http_client import HttpClient

log = logging.getLogger(__name__)

HINTS_URL = "https://search.itunes.apple.com/WebObjects/MZSearchHints.woa/wa/hints"
SEARCH_URL = "https://itunes.apple.com/search"
APP_PAGE_URL = "https://apps.apple.com/{country}/app/id{track_id}"

# 앱 상세 페이지의 정보 표(Annotation). 인앱결제가 있으면 "In-App Purchases" 항목이 생긴다.
_ANNOTATION_MARK = '"$kind":"Annotation"'
_IAP_MARK = re.compile(r'"\$kind":"Annotation","title":"In-App Purchases"')


@dataclass(frozen=True)
class App:
    track_id: int
    name: str
    seller: str
    artist: str
    price: float
    rating_count: int


def parse_hints(body: bytes) -> list[str]:
    """자동완성 응답(plist XML)에서 추천 검색어 목록을 꺼낸다."""
    data = plistlib.loads(body)
    hints = data.get("hints", []) if isinstance(data, dict) else []
    terms = (h.get("term") for h in hints if isinstance(h, dict))
    return [t.strip() for t in terms if isinstance(t, str) and t.strip()]


def _to_number(value: object, kind: type) -> int | float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return kind(value)
    return kind(0)


def parse_search(body: bytes) -> list[App]:
    """검색 API(JSON) 응답에서 앱 목록을 꺼낸다. 필수값(trackId) 없는 항목은 버린다."""
    data = json.loads(body)
    results = data.get("results", []) if isinstance(data, dict) else []
    apps = []
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("trackId"), int):
            continue
        apps.append(
            App(
                track_id=item["trackId"],
                name=str(item.get("trackName", "")),
                seller=str(item.get("sellerName", "")),
                artist=str(item.get("artistName", "")),
                price=_to_number(item.get("price"), float),
                rating_count=_to_number(item.get("userRatingCount"), int),
            )
        )
    return apps


def page_has_iap(html: str) -> bool | None:
    """True=인앱결제 있음, False=없음, None=페이지 모양이 달라 판단 불가."""
    if _IAP_MARK.search(html):
        return True
    if _ANNOTATION_MARK in html:
        return False
    return None


class AppleStore:
    def __init__(self, client: HttpClient, config: CollectConfig) -> None:
        self._client = client
        self._config = config
        self._iap_cache: dict[int, bool | None] = {}

    def suggestions(self, term: str) -> list[str] | None:
        query = urllib.parse.urlencode({"clientApplication": "Software", "term": term})
        body = self._client.get(
            f"{HINTS_URL}?{query}", headers={"X-Apple-Store-Front": self._config.store_front}
        )
        if body is None:
            return None
        try:
            return parse_hints(body)
        except (plistlib.InvalidFileException, ValueError) as error:
            log.error("자동완성 응답 해석 실패 (%s): %s", term, error)
            return None

    def top_apps(self, keyword: str) -> list[App] | None:
        query = urllib.parse.urlencode(
            {
                "term": keyword,
                "country": self._config.country,
                "entity": "software",
                "limit": self._config.top_n,
            }
        )
        body = self._client.get(f"{SEARCH_URL}?{query}")
        if body is None:
            return None
        try:
            return parse_search(body)[: self._config.top_n]
        except ValueError as error:
            log.error("검색 응답 해석 실패 (%s): %s", keyword, error)
            return None

    def has_in_app_purchases(self, track_id: int) -> bool | None:
        """같은 앱은 한 번만 확인한다(여러 키워드에 반복 등장하므로)."""
        if track_id in self._iap_cache:
            return self._iap_cache[track_id]
        url = APP_PAGE_URL.format(country=self._config.country, track_id=track_id)
        body = self._client.get(url)
        result = None if body is None else page_has_iap(body.decode("utf-8", errors="replace"))
        if body is not None and result is None:
            log.error("인앱결제 판단 불가 (페이지 모양 변경?): %s", url)
        self._iap_cache[track_id] = result
        return result
