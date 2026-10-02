"""스캔 진행 기록: 키워드 1개 분석이 끝날 때마다 한 줄씩 적어, 끊겨도 다시 실행하면 이어서 한다.

형식(JSON Lines): 1줄째 = 헤더(설정 지문 + 수집한 키워드 목록), 2줄째부터 = 키워드별 결과 1줄씩.
"""

import dataclasses
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType
from typing import TextIO

from .config import Config
from .metrics import KeywordStats

log = logging.getLogger(__name__)

VERSION = 1


@dataclasses.dataclass(frozen=True)
class Saved:
    keywords: dict[str, list[str]]
    rows: list[KeywordStats]


def progress_path(output: Path) -> Path:
    """keywords_result.xlsx → keywords_result.progress.jsonl"""
    return output.with_suffix(".progress.jsonl")


def fingerprint(seeds: list[str], config: Config) -> str:
    """결과에 영향을 주는 값(씨앗·수집·기준·가중치·대기업 목록)만 본다. 요청 간격·파일 경로는 제외."""
    material = {
        "seeds": seeds,
        "collect": dataclasses.asdict(config.collect),
        "criteria": dataclasses.asdict(config.criteria),
        "weights": dataclasses.asdict(config.weights),
        "big_companies": list(config.big_companies),
        "fields": list(KeywordStats.__dataclass_fields__),  # 저장 형식이 바뀌면 옛 기록은 쓰지 않음
    }
    text = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_keywords(value: object) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, list) and all(isinstance(s, str) for s in v)
        for k, v in value.items()
    )


def load(path: Path, expected_fingerprint: str) -> Saved | None:
    """이어서 할 수 있으면 저장된 키워드·결과를, 아니면 None(처음부터)을 돌려준다."""
    if not path.is_file():
        return None
    # 파일은 ASCII로만 쓰지만, 손상에 대비해 깨진 글자는 대체하고 줄 판정에서 걸러낸다.
    # splitlines()는 U+2028 같은 문자에서도 줄을 자르므로 "\n"으로만 나눈다.
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").split("\n") if line]
    try:
        header = json.loads(lines[0]) if lines else None
    except json.JSONDecodeError:
        header = None
    if not isinstance(header, dict) or header.get("version") != VERSION or not _valid_keywords(header.get("keywords")):
        log.warning("진행 기록을 읽을 수 없어 처음부터 합니다: %s", path)
        return None
    if header.get("fingerprint") != expected_fingerprint:
        log.warning("설정·씨앗 또는 프로그램 저장 형식이 바뀌어 처음부터 합니다 (이전 진행 기록은 덮어씀): %s", path)
        return None
    if not header["keywords"]:
        return None  # 지난번 자동완성 수집이 통째로 실패함 → 다시 수집

    keywords: dict[str, list[str]] = header["keywords"]
    rows: dict[str, KeywordStats] = {}
    for number, line in enumerate(lines[1:], start=2):
        try:
            stats = KeywordStats(**json.loads(line))
        except (json.JSONDecodeError, TypeError):
            # 강제 종료로 쓰다 만 줄. 이 줄부터는 다시 분석한다.
            log.warning("진행 기록 %d번째 줄이 잘려 있어 그 키워드부터 다시 분석합니다.", number)
            break
        if stats.keyword in keywords:
            rows[stats.keyword] = stats
    return Saved(keywords=keywords, rows=list(rows.values()))


def _stats_line(stats: KeywordStats) -> str:
    # ensure_ascii(기본값): 여러 바이트 글자가 없어 쓰다 만 줄도 글자 중간에서 깨지지 않는다
    return json.dumps(dataclasses.asdict(stats)) + "\n"


class ProgressWriter:
    def __init__(self, handle: TextIO) -> None:
        self._handle = handle

    def append(self, stats: KeywordStats) -> None:
        # flush: 프로그램이 갑자기 죽어도 이미 쓴 줄은 운영체제에 넘어가 남는다
        self._handle.write(_stats_line(stats))
        self._handle.flush()

    def __enter__(self) -> "ProgressWriter":
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._handle.close()


def open_writer(
    path: Path, fingerprint_value: str, keywords: dict[str, list[str]], rows: Iterable[KeywordStats]
) -> ProgressWriter:
    """헤더와 이미 끝난 결과를 새로 쓴 뒤(잘린 줄 정리) 이어 쓰기용으로 연다.

    임시 파일에 다 쓴 뒤 바꿔치기해서, 이 사이에 끊겨도 기존 진행 기록이 깨지지 않는다.
    """
    header = {"version": VERSION, "fingerprint": fingerprint_value, "keywords": keywords}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".progress.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(header) + "\n")
            for stats in rows:
                handle.write(_stats_line(stats))
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return ProgressWriter(path.open("a", encoding="utf-8"))


def remove(path: Path) -> None:
    path.unlink(missing_ok=True)
