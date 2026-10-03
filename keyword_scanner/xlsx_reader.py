"""외부 패키지 없이 .xlsx 시트 하나를 글자 표(list[list[str]])로 읽는다.

이 스캐너가 쓴 파일(inlineStr)과 Excel이 다시 저장한 파일(sharedStrings, 빈 셀 생략)을 모두 읽는다.
"""

import posixpath
import re
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_CELL_REF = re.compile(r"([A-Z]+)([0-9]+)$")
_DIGITS = re.compile(r"[0-9]+")
# 압축을 풀면 아주 커지는 파일로 메모리를 다 쓰지 않도록 XML 한 조각의 크기를 제한한다
MAX_PART_BYTES = 50 * 1024 * 1024
# Excel 한계(행 1,048,576 · 열 XFD=16,384). 이보다 큰 위치는 깨진 파일로 본다
MAX_ROWS = 1_048_576
MAX_COLUMNS = 16_384


class XlsxReadError(ValueError):
    """엑셀 파일이 없거나 읽을 수 없는 모양."""


def _read_xml(zf: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        info = zf.getinfo(name)
    except KeyError as error:
        raise XlsxReadError(f"엑셀 파일 안에 {name} 이(가) 없습니다.") from error
    if info.file_size > MAX_PART_BYTES:
        raise XlsxReadError(f"엑셀 파일 안의 {name} 이(가) 너무 큽니다.")
    return ElementTree.fromstring(zf.read(name))


def _text(element: ElementTree.Element) -> str:
    """<t> 글자를 이어 붙인다. 발음 표기(<rPh>) 안의 글자는 뺀다."""
    phonetic = {id(t) for ph in element.iter(f"{_MAIN}rPh") for t in ph.iter(f"{_MAIN}t")}
    return "".join(t.text or "" for t in element.iter(f"{_MAIN}t") if id(t) not in phonetic)


def _column_index(letters: str) -> int:
    """A → 0, Z → 25, AA → 26"""
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


def _sheet_path(zf: zipfile.ZipFile, index: int) -> str:
    workbook = _read_xml(zf, "xl/workbook.xml")
    sheets = workbook.findall(f"{_MAIN}sheets/{_MAIN}sheet")
    if not 0 <= index < len(sheets):
        raise XlsxReadError(f"엑셀 파일에 {index + 1}번째 시트가 없습니다.")
    rel_id = sheets[index].get(_REL_ID)
    rels = _read_xml(zf, "xl/_rels/workbook.xml.rels")
    target = next((r.get("Target") for r in rels.iter(_PKG_REL) if r.get("Id") == rel_id), None)
    if not target:
        raise XlsxReadError("엑셀 파일의 시트 연결 정보가 깨졌습니다.")
    # "/xl/worksheets/sheet1.xml"(절대) 또는 "worksheets/sheet1.xml"(xl/ 기준 상대)
    return target.lstrip("/") if target.startswith("/") else posixpath.normpath(f"xl/{target}")


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    return [_text(si) for si in _read_xml(zf, "xl/sharedStrings.xml").iter(f"{_MAIN}si")]


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> str:
    kind = cell.get("t", "n")
    if kind == "inlineStr":
        inline = cell.find(f"{_MAIN}is")
        return "" if inline is None else _text(inline)
    raw = cell.findtext(f"{_MAIN}v") or ""
    if kind == "s":
        if not _DIGITS.fullmatch(raw) or int(raw) >= len(shared):
            raise XlsxReadError(f"엑셀 셀 {cell.get('r')}의 공유 문자열 번호가 잘못됐습니다: {raw!r}")
        return shared[int(raw)]
    if kind == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return raw


def _place(row: list[str], position: int, value: str) -> None:
    if position >= MAX_COLUMNS:
        raise XlsxReadError(f"엑셀 셀 위치가 너무 큽니다: {position + 1}번째 열")
    row.extend([""] * (position + 1 - len(row)))
    row[position] = value


def read_sheet(path: Path, index: int = 0) -> list[list[str]]:
    """index번째(0부터) 시트를 행 목록으로. 비어 건너뛴 행·셀은 빈 행·빈 글자로 채워 위치를 지킨다."""
    if not path.is_file():
        raise XlsxReadError(f"엑셀 파일을 찾을 수 없습니다: {path}")
    try:
        with zipfile.ZipFile(path) as zf:
            shared = _shared_strings(zf)
            sheet = _read_xml(zf, _sheet_path(zf, index))
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError) as error:
        # 압축 데이터 손상·잘린 파일·암호 걸린 항목·지원 안 하는 압축 방식
        raise XlsxReadError(f"엑셀(.xlsx) 파일이 아니거나 깨졌습니다: {path}") from error
    except OSError as error:
        raise XlsxReadError(f"엑셀 파일을 열 수 없습니다 ({error.strerror or error}): {path}") from error
    except ElementTree.ParseError as error:
        raise XlsxReadError(f"엑셀 파일 내용이 깨졌습니다: {error}") from error

    rows: list[list[str]] = []
    for row_element in sheet.iter(f"{_MAIN}row"):
        row_number = row_element.get("r")
        if row_number and _DIGITS.fullmatch(row_number):
            if int(row_number) > MAX_ROWS:
                raise XlsxReadError(f"엑셀 행 번호가 너무 큽니다: {row_number}")
            rows.extend([] for _ in range(int(row_number) - 1 - len(rows)))
        row: list[str] = []
        for cell in row_element.iter(f"{_MAIN}c"):
            match = _CELL_REF.match(cell.get("r", ""))
            position = _column_index(match.group(1)) if match else len(row)
            _place(row, position, _cell_value(cell, shared))
        rows.append(row)
    return rows
