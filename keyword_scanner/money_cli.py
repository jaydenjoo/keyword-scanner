"""돈 검증 실행 흐름: pass 키워드 읽기 → 키워드별 상위 앱 검색 → 유료·업데이트·리뷰 집계 → 엑셀 저장."""

import argparse
import logging
import shutil
import sys
from datetime import date
from pathlib import Path

from . import money_progress, progress
from .apple import search_url
from .cli import DEFAULT_CONFIG, InputError, _setup_logging
from .config import ConfigError, MoneyConfig, load_money_config
from .http_client import HttpClient
from .money import (
    KeywordMoney,
    MoneyApp,
    MoneyRules,
    PaidPatterns,
    compile_paid_words,
    months_ago,
    parse_money_search,
    select_pass_keywords,
    summarize,
)
from .xlsx_reader import XlsxReadError, read_sheet
from .xlsx_writer import Cell, Sheet, write_workbook

log = logging.getLogger("keyword_scanner.money")

MANUAL_COLUMNS = ["리뷰 불만 반복", "Reddit 불만"]  # 사람이 직접 채우는 열 (빈칸으로 만듦)
UNCHECKED = "미검사"  # 중단·실패로 이번에 검사하지 못한 키워드 (줄을 남겨 메모를 지킨다)


def _yes_no(value: bool) -> str:
    return "예" if value else "아니오"


def _day(value: date | None) -> str:
    return value.isoformat() if value else ""


def _key(keyword: str) -> str:
    return " ".join(keyword.split()).casefold()


def load_manual_notes(path: Path) -> dict[str, list[str]]:
    """지난 결과 파일의 summary 시트에서 키워드 → 직접 입력 열 값. 파일이 없거나 못 읽으면 빈 결과."""
    if not path.is_file():
        return {}
    try:
        rows = read_sheet(path)
    except XlsxReadError as error:
        log.warning("지난 결과의 직접 입력 열을 읽지 못함 (%s) — 백업 파일에만 남습니다.", error)
        return {}
    header = [" ".join(cell.split()) for cell in rows[0]] if rows else []
    if "키워드" not in header:
        log.warning("지난 결과의 첫 시트에 '키워드' 열이 없어 직접 입력 열을 옮기지 못함 — 백업 파일에만 남습니다.")
        return {}
    keyword_index = header.index("키워드")
    missing = [name for name in MANUAL_COLUMNS if name not in header]
    if missing:
        log.warning("지난 결과에 직접 입력 열 %s 이(가) 없어 그 열의 메모는 백업 파일에만 남습니다.", ", ".join(missing))
    indexes = [header.index(name) if name in header else None for name in MANUAL_COLUMNS]
    notes: dict[str, list[str]] = {}
    for row in rows[1:]:
        values = [row[i] if i is not None and i < len(row) else "" for i in indexes]
        keyword = row[keyword_index] if keyword_index < len(row) else ""
        if keyword.strip() and any(v.strip() for v in values):
            notes[_key(keyword)] = values
    return notes


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}.bak{path.suffix}")


def back_up(path: Path) -> None:
    """덮어쓰기 전에 지난 결과를 <이름>.bak.xlsx 로 복사해 둔다(직접 적은 메모 보호)."""
    if not path.is_file():
        return
    backup = _backup_path(path)
    shutil.copy2(path, backup)
    log.info("지난 결과를 백업함: %s", backup)


def check_keyword(
    client: HttpClient, config: MoneyConfig, keyword: str, paid: PaidPatterns, rules: MoneyRules
) -> KeywordMoney | None:
    """검색 실패(재시도까지)·응답 해석 실패면 로그를 남기고 None — 그 키워드는 건너뛴다."""
    body = client.get(search_url(config.base.collect, keyword))
    if body is None:
        log.error("건너뜀 (검색 실패): %s", keyword)
        return None
    try:
        apps = parse_money_search(body, paid)[: config.base.collect.top_n]
    except ValueError as error:
        log.error("건너뜀 (검색 응답 해석 실패 %s): %s", error, keyword)
        return None
    return summarize(keyword, apps, rules)


def build_sheets(
    keywords: list[str], results: list[KeywordMoney], config: MoneyConfig, notes: dict[str, list[str]] | None = None
) -> list[Sheet]:
    """summary 는 pass 키워드마다 한 줄(입력 순서). 결과가 없는 키워드는 '미검사' 줄.
    notes: 키워드 → 직접 입력 열 값(지난 결과에서 옮겨 담음). 없으면 빈칸."""
    notes = notes or {}
    blank = [""] * len(MANUAL_COLUMNS)
    by_keyword = {r.keyword: r for r in results}
    threshold = config.base.criteria.low_review_threshold
    summary_headers = [
        "키워드", "조회된 앱 수", "유료 추정 앱 수", f"최근 {config.recent_update_months}개월 업데이트 앱 수",
        f"리뷰 {threshold:,}개 미만 앱 수", "돈 내는 경쟁자 있음", "살아 있는 경쟁자 수", *MANUAL_COLUMNS,
    ]
    summary: list[list[Cell]] = []
    for keyword in keywords:
        r = by_keyword.get(keyword)
        metrics: list[Cell] = [UNCHECKED, "", "", "", "", ""] if r is None else [
            len(r.apps), r.paid_count, r.recent_update_count, r.low_review_count,
            _yes_no(r.has_paying_competitor), r.live_competitor_count,
        ]
        summary.append([keyword, *metrics, *notes.get(_key(keyword), blank)])
    detail_headers = [
        "키워드", "순위", "앱 이름", "판매자", "가격", "평점 수", "평균 평점", "출시일", "최근 업데이트일",
        "유료 단어 포함", "찾은 유료 단어", "유료 추정",
    ]
    details: list[list[Cell]] = [
        [
            r.keyword, rank, a.name, a.seller, a.price, a.rating_count, a.average_rating, _day(a.released),
            _day(a.updated), _yes_no(bool(a.paid_words)), ", ".join(a.paid_words), _yes_no(a.is_paid_guess),
        ]
        for r in results
        for rank, a in enumerate(r.apps, start=1)
    ]
    return [Sheet("summary", summary_headers, summary), Sheet("apps", detail_headers, details)]


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="pass 표시한 키워드의 '돈 내는 경쟁자' 검증")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="설정 파일 (기본: config.toml)")
    parser.add_argument("--input", type=Path, help="입력 엑셀 (기본: 설정의 money_check.input)")
    parser.add_argument("--output", type=Path, help="결과 엑셀 (기본: 설정의 money_check.output)")
    parser.add_argument("--fresh", action="store_true", help="진행 기록을 무시하고 처음부터 (기본: 끊긴 곳부터 이어서)")
    return parser.parse_args(argv)


def _load_done(
    progress_file: Path, fingerprint: str, keywords: list[str], today: date, fresh: bool
) -> dict[str, tuple[MoneyApp, ...]]:
    """이어서 할 키워드 → 앱 원자료. --fresh 이거나 기록을 못 쓰면 빈 결과."""
    if fresh:
        progress.remove(progress_file)  # 검사 중에 끊겨도 다음 실행이 버린 기록으로 이어서 하지 않게
        return {}
    done = money_progress.load(progress_file, fingerprint, keywords, today) or {}
    if done:
        log.info("이어서 합니다: 키워드 %d개 중 %d개는 이미 완료 (처음부터 하려면 --fresh)", len(keywords), len(done))
    return done


def main(argv: list[str] | None = None, today: date | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_money_config(args.config)
        input_file = args.input or config.input_file
        keywords = select_pass_keywords(read_sheet(input_file), config.keyword_column, config.pass_column)
    except (ConfigError, InputError, XlsxReadError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2
    output = args.output or config.output_file
    _setup_logging(config.log_file)

    day = today or date.today()  # '최근 업데이트' 기준일, 진행 기록의 날짜 (기존과 같은 컴퓨터 날짜)
    client = HttpClient(config.base.request)
    paid = compile_paid_words(config.paid_words)
    rules = MoneyRules(
        low_review_threshold=config.base.criteria.low_review_threshold,
        recent_since=months_ago(day, config.recent_update_months),
        paid_competitor_min=config.paid_competitor_min,
    )
    progress_file = progress.progress_path(output)
    fingerprint = money_progress.fingerprint(keywords, config)
    found = _load_done(progress_file, fingerprint, keywords, day, args.fresh)
    todo = [keyword for keyword in keywords if keyword not in found]
    seconds = round(len(todo) * config.base.request.delay_seconds)
    log.info("pass 키워드 %d개 검사 시작 (약 %d분 %d초)", len(todo), seconds // 60, seconds % 60)
    skipped = 0
    interrupted = False
    try:
        with money_progress.open_writer(progress_file, fingerprint, day, found) as writer:
            for index, keyword in enumerate(todo, start=1):
                result = check_keyword(client, config, keyword, paid, rules)
                if result is None:
                    skipped += 1
                    continue
                # 이번 엑셀에 먼저 담고, 한 줄씩 남겨야 Ctrl+C·터미널 닫힘 뒤에도 다시 실행할 때 이어서 한다
                found[keyword] = result.apps
                writer.append(keyword, result.apps)
                log.info("[%d/%d] %s — 유료 추정 %d개, 살아 있는 경쟁자 %d개",
                         index, len(todo), keyword, result.paid_count, result.live_competitor_count)
    except KeyboardInterrupt:
        log.warning("사용자가 중단함 — 지금까지 결과만 저장합니다. 다시 실행하면 이어서 합니다.")
        interrupted = True

    # 이어서 한 결과도 '최근 업데이트' 기준은 오늘 날짜로 다시 계산한다. 순서는 입력 엑셀 순서.
    results = [summarize(keyword, list(found[keyword]), rules) for keyword in keywords if keyword in found]
    if not results:
        log.error("검사된 키워드가 없어 엑셀을 만들지 않았습니다. 로그 확인: %s", config.log_file)
        return 130 if interrupted else 1
    notes = load_manual_notes(output)
    listed = {_key(keyword) for keyword in keywords}
    dropped = [keyword for keyword in notes if keyword not in listed]
    if dropped:
        log.warning("pass 목록에 없는 키워드 %d개의 직접 입력 메모는 %s 에만 남습니다: %s",
                    len(dropped), _backup_path(output).name, ", ".join(dropped))
    back_up(output)
    write_workbook(output, build_sheets(keywords, results, config, notes))
    log.info("저장 완료: %s (검사한 키워드 %d개, 미검사 %d개)", output, len(results), len(keywords) - len(results))
    if interrupted:
        return 130
    if skipped:
        log.info("실패한 키워드 %d개는 다시 실행하면 그것만 다시 검사합니다 (처음부터 하려면 --fresh)", skipped)
    else:
        progress.remove(progress_file)
    return 0
