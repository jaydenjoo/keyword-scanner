"""실행 흐름: 씨앗 읽기 → 자동완성 수집 → 키워드별 상위 앱 분석 → 엑셀 저장."""

import argparse
import logging
import sys
from collections.abc import Iterator
from pathlib import Path

from .apple import AppleStore
from .config import CollectConfig, Config, ConfigError, load_config
from .http_client import HttpClient
from .metrics import KeywordStats, ScoringRules, compile_big_companies, compute_stats, sort_weakest_first
from .xlsx_writer import Cell, write_xlsx

log = logging.getLogger("keyword_scanner")

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.toml"


class InputError(ValueError):
    """씨앗 파일 문제."""


def read_seeds(path: Path) -> list[str]:
    """빈 줄·# 주석은 무시, 대소문자만 다른 중복은 하나로."""
    if not path.is_file():
        raise InputError(f"씨앗 파일을 찾을 수 없습니다: {path}")
    seeds: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        word = " ".join(line.split())
        if word and not word.startswith("#"):
            seeds.setdefault(word.casefold(), word)
    if not seeds:
        raise InputError(f"씨앗 파일에 단어가 없습니다: {path}")
    return list(seeds.values())


def collect_keywords(store: AppleStore, seeds: list[str], collect: CollectConfig) -> dict[str, list[str]]:
    """키워드(소문자) → 그 키워드를 만든 씨앗 목록. 수집 순서 유지."""
    keywords: dict[str, list[str]] = {}
    for seed in seeds:
        for letter in collect.suffix_letters:
            term = f"{seed}{collect.suffix_separator}{letter}"
            hints = store.suggestions(term)
            if hints is None:
                continue
            for hint in hints:
                origins = keywords.setdefault(hint.casefold(), [])
                if seed not in origins:
                    origins.append(seed)
        log.info("씨앗 '%s' 수집 완료 — 누적 키워드 %d개", seed, len(keywords))
    return keywords


def analyze(
    store: AppleStore, keywords: dict[str, list[str]], config: Config, rules: ScoringRules
) -> Iterator[KeywordStats]:
    total = len(keywords)
    for index, (keyword, seeds) in enumerate(keywords.items(), start=1):
        apps = store.top_apps(keyword)
        if apps is None:
            continue
        iap: dict[int, bool | None] = {}
        if config.criteria.check_in_app_purchases:
            # 유료 앱은 이미 '유료' 로 세므로 무료 앱만 상세 페이지 확인
            iap = {a.track_id: store.has_in_app_purchases(a.track_id) for a in apps if a.price == 0}
        stats = compute_stats(keyword, ", ".join(seeds), apps, iap, rules)
        log.info("[%d/%d] %s — 경쟁 점수 %.1f", index, total, keyword, stats.score)
        yield stats


def build_table(rows: list[KeywordStats], config: Config) -> tuple[list[str], list[list[Cell]]]:
    threshold = config.criteria.low_review_threshold
    monetized = "유료·인앱결제 앱 수" if config.criteria.check_in_app_purchases else "유료 앱 수(인앱 미확인)"
    headers = [
        "순위", "키워드", "경쟁 점수(낮을수록 약함)", "씨앗", "조회된 앱 수", "제목 포함 앱 수",
        "리뷰 중간값", "리뷰 최대값", f"리뷰 {threshold:,}개 미만 앱 수", "대기업 여부",
        "대기업 앱 수", "대기업 판매자", monetized, "인앱 확인 실패 수",
    ]
    table: list[list[Cell]] = [
        [
            rank, r.keyword, r.score, r.seeds, r.result_count, r.title_match_count,
            r.median_reviews, r.max_reviews, r.low_review_count,
            "예" if r.big_company_count else "아니오", r.big_company_count, r.big_company_names,
            r.paid_or_iap_count, r.iap_unknown_count,
        ]
        for rank, r in enumerate(sort_weakest_first(rows), start=1)
    ]
    return headers, table


def _setup_logging(log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [file_handler, console]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="미국 앱스토어 키워드 경쟁도 스캐너")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="설정 파일 (기본: config.toml)")
    parser.add_argument("--seeds", type=Path, help="씨앗 파일 (기본: 설정의 files.seeds)")
    parser.add_argument("--output", type=Path, help="결과 엑셀 (기본: 설정의 files.output)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
        seeds = read_seeds(args.seeds or config.seeds_file)
    except (ConfigError, InputError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2
    output = args.output or config.output_file
    _setup_logging(config.log_file)

    store = AppleStore(HttpClient(config.request), config.collect)
    rules = ScoringRules(config.criteria, config.weights, compile_big_companies(config.big_companies))
    rows: list[KeywordStats] = []
    interrupted = False
    try:
        log.info("씨앗 %d개로 자동완성 수집 시작", len(seeds))
        keywords = collect_keywords(store, seeds, config.collect)
        per_keyword = 1 + (config.collect.top_n if config.criteria.check_in_app_purchases else 0)
        minutes = len(keywords) * per_keyword * config.request.delay_seconds / 60
        log.info("키워드 %d개 분석 시작 (최대 약 %.0f분, 같은 앱은 재확인 안 해서 보통 더 짧음)", len(keywords), minutes)
        # 한 줄씩 담아야 중간에 Ctrl+C로 멈춰도 그때까지 결과가 남는다
        for stats in analyze(store, keywords, config, rules):
            rows.append(stats)
    except KeyboardInterrupt:
        interrupted = True
        log.warning("사용자가 중단함 — 지금까지 결과만 저장합니다.")

    if not rows:
        log.error("분석된 키워드가 없어 엑셀을 만들지 않았습니다. 로그 확인: %s", config.log_file)
        return 1
    headers, table = build_table(rows, config)
    write_xlsx(output, "keywords", headers, table)
    log.info("저장 완료: %s (키워드 %d개)", output, len(rows))
    return 130 if interrupted else 0
