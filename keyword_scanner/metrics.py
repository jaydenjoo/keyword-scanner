"""키워드별 경쟁 지표와 경쟁 점수 계산 (네트워크 없음, 순수 계산)."""

import math
import re
import statistics
from dataclasses import dataclass

from .apple import App
from .config import CriteriaConfig, ScoreWeights


@dataclass(frozen=True)
class ScoringRules:
    criteria: CriteriaConfig
    weights: ScoreWeights
    big_company_patterns: tuple[re.Pattern[str], ...]


@dataclass(frozen=True)
class KeywordStats:
    keyword: str
    seeds: str
    result_count: int
    title_match_count: int
    median_reviews: float
    max_reviews: int
    low_review_count: int
    big_company_count: int
    big_company_names: str
    paid_or_iap_count: int
    iap_unknown_count: int
    score: float


def compile_big_companies(names: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """단어 단위로 맞춘다: "Apple"은 "Apple Inc."와 맞고 "Pineapple Studio"와는 안 맞는다."""
    return tuple(re.compile(rf"\b{re.escape(n)}\b", re.IGNORECASE) for n in names)


def _big_company_name(app: App, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in patterns:
        if pattern.search(app.seller) or pattern.search(app.artist):
            return app.seller or app.artist
    return None


def _review_level(count: float, scale_max: int) -> float:
    """리뷰 수를 0~1로 바꾼다. 로그 눈금이라 100→1,000 차이와 10,000→100,000 차이를 같게 본다."""
    return min(1.0, math.log10(count + 1) / math.log10(scale_max + 1))


def _score(stats: dict[str, float], n: int, rules: ScoringRules) -> float:
    if n == 0:
        return 0.0
    scale = rules.criteria.review_scale_max
    w = rules.weights
    parts = [
        (w.title_match, stats["title_match_count"] / n),
        (w.median_reviews, _review_level(stats["median_reviews"], scale)),
        (w.max_reviews, _review_level(stats["max_reviews"], scale)),
        (w.big_company, stats["big_company_count"] / n),
        (w.established_apps, 1 - stats["low_review_count"] / n),
        (w.monetized_apps, stats["paid_or_iap_count"] / n),
    ]
    total_weight = sum(weight for weight, _ in parts)
    return round(100 * sum(weight * value for weight, value in parts) / total_weight, 1)


def compute_stats(
    keyword: str, seeds: str, apps: list[App], iap: dict[int, bool | None], rules: ScoringRules
) -> KeywordStats:
    """iap: track_id → 인앱결제 여부. 확인 안 한 앱은 넣지 않는다(유료 여부만 반영)."""
    needle = keyword.casefold()
    reviews = [a.rating_count for a in apps]
    big_names = [
        name for a in apps if (name := _big_company_name(a, rules.big_company_patterns)) is not None
    ]
    stats = {
        "title_match_count": sum(needle in a.name.casefold() for a in apps),
        "median_reviews": statistics.median(reviews) if reviews else 0,
        "max_reviews": max(reviews, default=0),
        "low_review_count": sum(r < rules.criteria.low_review_threshold for r in reviews),
        "big_company_count": len(big_names),
        "paid_or_iap_count": sum(a.price > 0 or iap.get(a.track_id) is True for a in apps),
    }
    return KeywordStats(
        keyword=keyword,
        seeds=seeds,
        result_count=len(apps),
        title_match_count=stats["title_match_count"],
        median_reviews=stats["median_reviews"],
        max_reviews=stats["max_reviews"],
        low_review_count=stats["low_review_count"],
        big_company_count=stats["big_company_count"],
        big_company_names=", ".join(dict.fromkeys(big_names)),
        paid_or_iap_count=stats["paid_or_iap_count"],
        iap_unknown_count=sum(a.price == 0 and a.track_id in iap and iap[a.track_id] is None for a in apps),
        score=_score(stats, len(apps), rules),
    )


def sort_weakest_first(rows: list[KeywordStats]) -> list[KeywordStats]:
    """경쟁 점수 낮은 순. 같으면 리뷰 중간값 낮은 순, 그다음 키워드 가나다순."""
    return sorted(rows, key=lambda r: (r.score, r.median_reviews, r.keyword))
