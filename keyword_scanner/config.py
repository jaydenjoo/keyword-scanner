"""config.toml 읽기 + 검증. 잘못된 값은 실행 전에 바로 알려준다."""

import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """설정 파일이 없거나 값이 잘못됨."""


@dataclass(frozen=True)
class RequestConfig:
    delay_seconds: float
    max_retries: int
    timeout_seconds: float
    user_agent: str


@dataclass(frozen=True)
class CollectConfig:
    country: str
    store_front: str
    suffix_letters: str
    suffix_separator: str
    top_n: int


@dataclass(frozen=True)
class CriteriaConfig:
    low_review_threshold: int
    check_in_app_purchases: bool
    review_scale_max: int


@dataclass(frozen=True)
class ScoreWeights:
    title_match: float
    median_reviews: float
    max_reviews: float
    big_company: float
    established_apps: float
    monetized_apps: float


@dataclass(frozen=True)
class Config:
    seeds_file: Path
    output_file: Path
    log_file: Path
    request: RequestConfig
    collect: CollectConfig
    criteria: CriteriaConfig
    weights: ScoreWeights
    big_companies: tuple[str, ...]


def _section(data: dict, name: str) -> dict:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] 섹션이 없습니다.")
    return value


def _get(section: dict, section_name: str, key: str, kind: type | tuple[type, ...]):
    if key not in section:
        raise ConfigError(f"[{section_name}] {key} 값이 없습니다.")
    value = section[key]
    # bool은 int의 하위 타입이라 숫자 칸에 true/false가 들어오는 것을 따로 막는다
    if isinstance(value, bool) and bool not in (kind if isinstance(kind, tuple) else (kind,)):
        raise ConfigError(f"[{section_name}] {key} 값의 형식이 잘못됐습니다: {value!r}")
    if not isinstance(value, kind):
        raise ConfigError(f"[{section_name}] {key} 값의 형식이 잘못됐습니다: {value!r}")
    return value


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ConfigError(message)


def load_config(path: Path) -> Config:
    """config.toml을 읽어 검증된 Config를 돌려준다. 상대 경로는 config 파일 기준."""
    if not path.is_file():
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"설정 파일 문법 오류: {error}") from error

    base = path.resolve().parent
    files = _section(data, "files")
    req = _section(data, "request")
    col = _section(data, "collect")
    cri = _section(data, "criteria")
    wts = _section(data, "score_weights")
    big = _section(data, "big_companies")
    number = (int, float)

    request = RequestConfig(
        delay_seconds=float(_get(req, "request", "delay_seconds", number)),
        max_retries=_get(req, "request", "max_retries", int),
        timeout_seconds=float(_get(req, "request", "timeout_seconds", number)),
        user_agent=_get(req, "request", "user_agent", str),
    )
    _require(request.delay_seconds >= 0, "[request] delay_seconds는 0 이상이어야 합니다.")
    _require(request.max_retries >= 0, "[request] max_retries는 0 이상이어야 합니다.")
    _require(request.timeout_seconds > 0, "[request] timeout_seconds는 0보다 커야 합니다.")

    collect = CollectConfig(
        country=_get(col, "collect", "country", str),
        store_front=_get(col, "collect", "store_front", str),
        suffix_letters=_get(col, "collect", "suffix_letters", str),
        suffix_separator=_get(col, "collect", "suffix_separator", str),
        top_n=_get(col, "collect", "top_n", int),
    )
    _require(len(collect.country) == 2, "[collect] country는 두 글자 국가 코드여야 합니다.")
    _require(bool(collect.store_front.strip()), "[collect] store_front가 비어 있습니다.")
    _require(bool(collect.suffix_letters), "[collect] suffix_letters가 비어 있습니다.")
    _require(1 <= collect.top_n <= 200, "[collect] top_n은 1~200 사이여야 합니다.")

    criteria = CriteriaConfig(
        low_review_threshold=_get(cri, "criteria", "low_review_threshold", int),
        check_in_app_purchases=_get(cri, "criteria", "check_in_app_purchases", bool),
        review_scale_max=_get(cri, "criteria", "review_scale_max", int),
    )
    _require(criteria.low_review_threshold > 0, "[criteria] low_review_threshold는 0보다 커야 합니다.")
    _require(criteria.review_scale_max > 0, "[criteria] review_scale_max는 0보다 커야 합니다.")

    weights = ScoreWeights(
        **{
            key: float(_get(wts, "score_weights", key, number))
            for key in ScoreWeights.__dataclass_fields__
        }
    )
    weight_values = list(vars(weights).values())
    _require(all(w >= 0 for w in weight_values), "[score_weights] 가중치는 0 이상이어야 합니다.")
    _require(sum(weight_values) > 0, "[score_weights] 가중치가 모두 0입니다.")

    names = _get(big, "big_companies", "names", list)
    _require(all(isinstance(n, str) and n.strip() for n in names), "[big_companies] names에 빈 값이 있습니다.")

    return Config(
        seeds_file=base / _get(files, "files", "seeds", str),
        output_file=base / _get(files, "files", "output", str),
        log_file=base / _get(files, "files", "log", str),
        request=request,
        collect=collect,
        criteria=criteria,
        weights=weights,
        big_companies=tuple(n.strip() for n in names),
    )
