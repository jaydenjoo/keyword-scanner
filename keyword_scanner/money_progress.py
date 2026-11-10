"""돈 검증 진행 기록: 키워드 1개 검사가 끝날 때마다 한 줄씩 적어, 끊겨도 다시 실행하면 이어서 한다.

형식(JSON Lines): 1줄째 = 헤더(설정 지문 + 검사한 날짜), 2줄째부터 = {"keyword": 키워드, "apps": [앱 원자료]}.
집계(유료 추정 수·최근 업데이트 수 등)는 저장하지 않고 끝날 때 오늘 기준으로 다시 계산한다.
다른 날 남긴 기록은 쓰지 않는다(며칠 지난 검색 결과를 모르는 사이에 섞지 않게).
쓰는 방식은 키워드 스캐너의 progress.py와 같다(헤더는 임시 파일에 쓴 뒤 바꿔치기, 줄마다 flush).
"""

import dataclasses
import hashlib
import json
import logging
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

from .config import MoneyConfig
from .money import MoneyApp

log = logging.getLogger(__name__)

VERSION = 1
_FIELDS = [field.name for field in dataclasses.fields(MoneyApp)]

Done = Mapping[str, Sequence[MoneyApp]]


def fingerprint(keywords: list[str], config: MoneyConfig) -> str:
    """검색 결과에 영향을 주는 값(키워드·국가·상위 앱 수·유료 단어·저장 형식)만 본다."""
    material = {
        "keywords": sorted(keywords),  # 입력 엑셀을 다시 정렬만 해도 이어서 하게
        "country": config.base.collect.country,
        "top_n": config.base.collect.top_n,
        "paid_words": list(config.paid_words),
        "fields": _FIELDS,  # 저장 형식이 바뀌면 옛 기록은 쓰지 않음
    }
    text = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _app_to_dict(app: MoneyApp) -> dict[str, object]:
    record = dataclasses.asdict(app)
    record["released"] = app.released.isoformat() if app.released else None
    record["updated"] = app.updated.isoformat() if app.updated else None
    return record


def _optional_date(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("날짜는 문자열이어야 함")
    return date.fromisoformat(value)  # 모양이 다르면 ValueError


def _app_from_dict(record: object) -> MoneyApp:
    if not isinstance(record, dict) or set(record) != set(_FIELDS):
        raise TypeError("앱 기록 형식이 잘못됨")
    words = record["paid_words"]
    if not isinstance(words, list) or not all(isinstance(w, str) for w in words):
        raise TypeError("유료 단어 형식이 잘못됨")
    return MoneyApp(
        track_id=_typed(record["track_id"], int),
        name=_typed(record["name"], str),
        seller=_typed(record["seller"], str),
        price=_finite(record["price"]),
        rating_count=_typed(record["rating_count"], int),
        average_rating=_finite(record["average_rating"]),
        released=_optional_date(record["released"]),
        updated=_optional_date(record["updated"]),
        paid_words=tuple(words),
    )


def _typed(value: object, kind: type | tuple[type, ...]) -> Any:
    """JSON에서 읽은 값의 형식 확인(외부 입력 경계). bool은 int로 보지 않는다."""
    if isinstance(value, bool) or not isinstance(value, kind):
        raise TypeError(f"값 형식이 잘못됨: {value!r}")
    return value


def _finite(value: object) -> float:
    """NaN·Infinity·너무 큰 수는 손상으로 본다(엑셀에 그대로 쓰면 파일이 깨짐)."""
    number = float(_typed(value, (int, float)))  # 수백 자리 정수면 OverflowError
    if not math.isfinite(number):
        raise ValueError(f"숫자가 아님: {value!r}")
    return number


def _line(keyword: str, apps: Sequence[MoneyApp]) -> str:
    # ensure_ascii(기본값): 여러 바이트 글자가 없어 쓰다 만 줄도 글자 중간에서 깨지지 않는다
    return json.dumps({"keyword": keyword, "apps": [_app_to_dict(a) for a in apps]}) + "\n"


def load(
    path: Path, expected_fingerprint: str, keywords: list[str], today: date
) -> dict[str, tuple[MoneyApp, ...]] | None:
    """이어서 할 수 있으면 키워드 → 앱 원자료를(keywords에 있는 것만), 아니면 None(처음부터)을 돌려준다."""
    if not path.is_file():
        return None
    # splitlines()는 U+2028 같은 문자에서도 줄을 자르므로 "\n"으로만 나눈다
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").split("\n") if line]
    try:
        header = json.loads(lines[0]) if lines else None
    except json.JSONDecodeError:
        header = None
    if not isinstance(header, dict) or header.get("version") != VERSION:
        log.warning("진행 기록을 읽을 수 없어 처음부터 합니다: %s", path)
        return None
    if header.get("fingerprint") != expected_fingerprint:
        log.warning("키워드·설정 또는 프로그램 저장 형식이 바뀌어 처음부터 합니다 (이전 진행 기록은 덮어씀): %s", path)
        return None
    if header.get("day") != today.isoformat():
        log.warning("다른 날(%s) 남긴 진행 기록이라 처음부터 다시 검사합니다: %s", header.get("day"), path)
        return None

    wanted = set(keywords)
    done: dict[str, tuple[MoneyApp, ...]] = {}
    for number, line in enumerate(lines[1:], start=2):
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or set(record) != {"keyword", "apps"}:
                raise TypeError("줄 형식이 잘못됨")
            keyword = _typed(record["keyword"], str)
            apps = record["apps"]
            if not isinstance(apps, list):
                raise TypeError("앱 목록 형식이 잘못됨")
            parsed = tuple(_app_from_dict(a) for a in apps)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError, OverflowError, RecursionError):
            # 강제 종료로 쓰다 만 줄(또는 손상된 줄). 이 줄부터는 다시 검사한다.
            log.warning("진행 기록 %d번째 줄이 잘려 있어 그 키워드부터 다시 검사합니다.", number)
            break
        if keyword in wanted:
            done[keyword] = parsed
    return done


class MoneyProgressWriter:
    def __init__(self, handle: TextIO) -> None:
        self._handle = handle

    def append(self, keyword: str, apps: Sequence[MoneyApp]) -> None:
        # flush: 프로그램이 갑자기 죽어도 이미 쓴 줄은 운영체제에 넘어가 남는다
        self._handle.write(_line(keyword, apps))
        self._handle.flush()

    def __enter__(self) -> "MoneyProgressWriter":
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._handle.close()


def open_writer(path: Path, fingerprint_value: str, today: date, done: Done) -> MoneyProgressWriter:
    """헤더와 이미 끝난 결과를 새로 쓴 뒤(잘린 줄 정리) 이어 쓰기용으로 연다.

    임시 파일에 다 쓴 뒤 바꿔치기해서, 이 사이에 끊겨도 기존 진행 기록이 깨지지 않는다.
    """
    header = {"version": VERSION, "fingerprint": fingerprint_value, "day": today.isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".progress.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(header) + "\n")
            for keyword, apps in done.items():
                handle.write(_line(keyword, apps))
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return MoneyProgressWriter(path.open("a", encoding="utf-8"))
