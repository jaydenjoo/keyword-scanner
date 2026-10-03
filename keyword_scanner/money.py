"""돈 검증: 검색 응답 해석, 키워드별 유료·업데이트·리뷰 집계 (네트워크 없음, 순수 계산)."""

import calendar
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime

from .apple import _to_number
from .cli import InputError
from .metrics import compile_big_companies

log = logging.getLogger(__name__)

PaidPatterns = tuple[tuple[str, re.Pattern[str]], ...]

# 체크하지 않은 체크박스(FALSE)·엑셀 오류값(#N/A, #DIV/0! 등)은 pass 표시로 보지 않는다
_NOT_MARKED = re.compile(r"false|#n/a|#[a-z0-9/_]+[!?]", re.IGNORECASE)


@dataclass(frozen=True)
class MoneyApp:
    track_id: int
    name: str
    seller: str
    price: float
    rating_count: int
    average_rating: float
    released: date | None
    updated: date | None
    paid_words: tuple[str, ...]  # 설명문에서 찾은 유료 단어 (설정 순서)

    @property
    def is_paid_guess(self) -> bool:
        return self.price > 0 or bool(self.paid_words)


@dataclass(frozen=True)
class MoneyRules:
    low_review_threshold: int
    recent_since: date  # 이 날 이후(포함) 업데이트했으면 '최근 업데이트'
    paid_competitor_min: int


@dataclass(frozen=True)
class KeywordMoney:
    keyword: str
    apps: tuple[MoneyApp, ...]
    paid_count: int
    recent_update_count: int
    low_review_count: int
    has_paying_competitor: bool
    live_competitor_count: int


def compile_paid_words(words: tuple[str, ...]) -> PaidPatterns:
    """단어 단위로 맞춘다(대기업 이름과 같은 규칙): "pro"는 "Go Pro"와 맞고 "product"와는 안 맞는다."""
    return tuple(zip(words, compile_big_companies(words)))


def _date(value: object) -> date | None:
    """"2026-09-02T02:17:21Z" → 2026-09-02. 없거나 모양이 다르면 None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _checked_date(value: object, field: str, track_id: int) -> date | None:
    parsed = _date(value)
    if parsed is None and value not in (None, ""):
        # 날짜를 못 읽으면 '업데이트 안 함'으로 세어져 경쟁이 약해 보이므로 기록해 둔다
        log.warning("날짜를 읽을 수 없음 (앱 %d, %s): %r", track_id, field, value)
    return parsed


def _finite(value: object, kind: type) -> int | float:
    number = _to_number(value, kind)
    return number if math.isfinite(number) else kind(0)


def parse_money_search(body: bytes, paid_patterns: PaidPatterns) -> list[MoneyApp]:
    """검색 API(JSON) 응답에서 앱 목록을 꺼낸다. 필수값(trackId) 없는 항목은 버린다."""
    data = json.loads(body)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        # 오류 응답을 '앱 0개'로 세면 경쟁이 없는 것처럼 보이므로 실패로 처리한다
        raise ValueError("검색 응답에 results 목록이 없습니다")
    apps = []
    for item in results:
        if not isinstance(item, dict):
            continue
        track_id = item.get("trackId")
        if not isinstance(track_id, int) or isinstance(track_id, bool):
            continue
        description = item.get("description")
        # 줄바꿈·특수 공백이 단어 사이에 있어도 "free trial" 같은 구를 찾도록 공백을 한 칸으로
        text = " ".join(description.split()) if isinstance(description, str) else ""
        apps.append(
            MoneyApp(
                track_id=track_id,
                name=str(item.get("trackName", "")),
                seller=str(item.get("sellerName", "")),
                price=_finite(item.get("price"), float),
                rating_count=_finite(item.get("userRatingCount"), int),
                average_rating=round(_finite(item.get("averageUserRating"), float), 2),
                released=_checked_date(item.get("releaseDate"), "출시일", track_id),
                updated=_checked_date(item.get("currentVersionReleaseDate"), "최근 업데이트일", track_id),
                paid_words=tuple(word for word, pattern in paid_patterns if pattern.search(text)),
            )
        )
    return apps


def months_ago(today: date, months: int) -> date:
    """달력 기준 n개월 전. 그 달에 같은 날이 없으면 말일 (3월 31일의 1개월 전 → 2월 28일)."""
    year, month_index = divmod(today.year * 12 + today.month - 1 - months, 12)
    month = month_index + 1
    return date(year, month, min(today.day, calendar.monthrange(year, month)[1]))


def summarize(keyword: str, apps: list[MoneyApp], rules: MoneyRules) -> KeywordMoney:
    paid = sum(a.is_paid_guess for a in apps)
    recent = sum(a.updated is not None and a.updated >= rules.recent_since for a in apps)
    return KeywordMoney(
        keyword=keyword,
        apps=tuple(apps),
        paid_count=paid,
        recent_update_count=recent,
        low_review_count=sum(a.rating_count < rules.low_review_threshold for a in apps),
        has_paying_competitor=paid >= rules.paid_competitor_min,
        live_competitor_count=recent,
    )


def _normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def _column(header: list[str], name: str) -> int:
    wanted = _normalize(name)
    for index, cell in enumerate(header):
        if _normalize(cell) == wanted:
            return index
    raise InputError(
        f"입력 엑셀 첫 줄에 '{name}' 열이 없습니다. "
        f"엑셀 맨 끝에 머리글 '{name}' 열을 만들고, 검사할 키워드 칸에 아무 글자나 적은 뒤 저장하세요."
    )


def select_pass_keywords(rows: list[list[str]], keyword_column: str, pass_column: str) -> list[str]:
    """pass 칸에 무언가 적힌 키워드만, 입력 순서대로. 대소문자·띄어쓰기만 다른 중복은 하나로.

    빈칸·공백만·FALSE(체크 안 한 체크박스)·엑셀 오류값은 표시 안 한 것으로 본다."""
    if not rows:
        raise InputError("입력 엑셀 첫 시트가 비어 있습니다.")
    keyword_index = _column(rows[0], keyword_column)
    pass_index = _column(rows[0], pass_column)
    selected: dict[str, str] = {}
    for row in rows[1:]:
        cell = row[pass_index] if pass_index < len(row) else ""
        keyword = " ".join(row[keyword_index].split()) if keyword_index < len(row) else ""
        if cell.strip() and not _NOT_MARKED.fullmatch(cell.strip()) and keyword:
            selected.setdefault(keyword.casefold(), keyword)
    if not selected:
        raise InputError(f"'{pass_column}' 열에 표시된 키워드가 없습니다. 검사할 키워드 칸에 아무 글자나 적어 주세요.")
    return list(selected.values())
