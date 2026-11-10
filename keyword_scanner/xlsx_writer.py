"""외부 패키지 없이 단순한 .xlsx(시트 1개 이상, 굵은 머리글, 틀 고정, 필터)를 만든다.

xlsx는 XML 파일 몇 개를 zip으로 묶은 형식이라 표준 라이브러리만으로 쓸 수 있다.
"""

import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

Cell = str | int | float

# 엑셀 XML에 들어갈 수 없는 제어 문자 (탭·줄바꿈 제외)
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheets}
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""
_SHEET_TYPE = (
    '<Override PartName="/xl/worksheets/sheet{n}.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
)

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
{sheets}
<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
_SHEET_REL = (
    '<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet{n}.xml"/>'
)

# 스타일 0 = 기본, 1 = 굵게(머리글)
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
</styleSheet>"""


def _column_letter(index: int) -> str:
    """0 → A, 25 → Z, 26 → AA"""
    letters = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _cell_xml(ref: str, value: Cell, style: int) -> str:
    style_attr = f' s="{style}"' if style else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{style_attr}><v>{value}</v></c>'
    text = escape(_ILLEGAL_XML.sub("", str(value)))
    return f'<c r="{ref}" t="inlineStr"{style_attr}><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(headers: list[str], rows: list[list[Cell]], widths: list[int]) -> str:
    cols = "".join(
        f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)
    )
    lines = []
    for r, row in enumerate([headers, *rows], start=1):
        style = 1 if r == 1 else 0
        cells = "".join(_cell_xml(f"{_column_letter(c)}{r}", v, style) for c, v in enumerate(row))
        lines.append(f'<row r="{r}">{cells}</row>')
    last = f"{_column_letter(len(headers) - 1)}{len(rows) + 1}"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"<cols>{cols}</cols><sheetData>{''.join(lines)}</sheetData>"
        f'<autoFilter ref="A1:{last}"/></worksheet>'
    )


@dataclass(frozen=True)
class Sheet:
    name: str
    headers: list[str]
    rows: list[list[Cell]]


def _workbook_xml(sheet_names: list[str]) -> str:
    names = [escape(name, {'"': "&quot;"}) for name in sheet_names]
    sheets = "".join(
        f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>' for n, name in enumerate(names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets></workbook>"
    )


def _widths(headers: list[str], rows: list[list[Cell]]) -> list[int]:
    return [
        min(60, max(8, *(len(str(row[i])) + 2 for row in [headers, *rows])))
        for i in range(len(headers))
    ]


def write_xlsx(path: Path, sheet_name: str, headers: list[str], rows: list[list[Cell]]) -> None:
    write_workbook(path, [Sheet(sheet_name, headers, rows)])


def write_workbook(path: Path, sheets: list[Sheet]) -> None:
    """임시 파일에 다 쓴 뒤 바꿔치기해서, 중간에 실패해도 기존 결과 파일이 깨지지 않게 한다."""
    if not sheets:
        raise ValueError("시트가 하나 이상 있어야 합니다.")
    numbers = range(1, len(sheets) + 1)
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES.format(sheets="\n".join(_SHEET_TYPE.format(n=n) for n in numbers)),
        "_rels/.rels": _ROOT_RELS,
        "xl/workbook.xml": _workbook_xml([sheet.name for sheet in sheets]),
        "xl/_rels/workbook.xml.rels": _WORKBOOK_RELS.format(sheets="\n".join(_SHEET_REL.format(n=n) for n in numbers)),
        "xl/styles.xml": _STYLES,
        **{
            f"xl/worksheets/sheet{n}.xml": _sheet_xml(sheet.headers, sheet.rows, _widths(sheet.headers, sheet.rows))
            for n, sheet in zip(numbers, sheets)
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(suffix=".xlsx.tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle, zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in parts.items():
                zf.writestr(name, content.encode("utf-8"))
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
