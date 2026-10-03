import tempfile
import unittest
import zipfile
from pathlib import Path

from keyword_scanner.xlsx_reader import XlsxReadError, read_sheet
from keyword_scanner.xlsx_writer import Sheet, write_workbook, write_xlsx

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

# Excel이 다시 저장한 모양: 공유 문자열(sharedStrings), 빈 셀 생략, 서식 있는 글자(r/t), 숫자·bool
EXCEL_WORKBOOK = f"""<?xml version="1.0"?>
<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>
<sheet name="keywords" sheetId="1" r:id="rId3"/><sheet name="other" sheetId="2" r:id="rId4"/>
</sheets></workbook>"""
EXCEL_RELS = """<?xml version="1.0"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId4" Type="x" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="x" Target="/xl/worksheets/sheet1.xml"/>
</Relationships>"""
EXCEL_SHARED = f"""<?xml version="1.0"?>
<sst xmlns="{MAIN}"><si><t>키워드</t></si><si><t>pass</t></si>
<si><r><t>budget</t></r><r><t xml:space="preserve"> app</t></r><rPh><t>ignored</t></rPh></si></sst>"""
EXCEL_SHEET = f"""<?xml version="1.0"?>
<worksheet xmlns="{MAIN}"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="s"><v>1</v></c></row>
<row r="3"><c r="A3" t="s"><v>2</v></c><c r="C3"><v>1</v></c></row>
<row r="4"><c r="A4" t="inlineStr"><is><t>habit</t></is></c><c r="B4" t="b"><v>1</v></c></row>
</sheetData></worksheet>"""


class ReadSheetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "in.xlsx"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_round_trip_with_writer(self) -> None:
        write_xlsx(self.path, "keywords", ["키워드", "점수"], [["a & <b>", 1.5], ["c", 7]])
        self.assertEqual(read_sheet(self.path), [["키워드", "점수"], ["a & <b>", "1.5"], ["c", "7"]])

    def test_reads_first_of_many_sheets(self) -> None:
        write_workbook(self.path, [Sheet("one", ["a"], [[1]]), Sheet("two", ["b"], [[2]])])
        self.assertEqual(read_sheet(self.path), [["a"], ["1"]])
        self.assertEqual(read_sheet(self.path, 1), [["b"], ["2"]])
        with self.assertRaises(XlsxReadError):
            read_sheet(self.path, 2)

    def test_excel_saved_file(self) -> None:
        with zipfile.ZipFile(self.path, "w") as zf:
            zf.writestr("xl/workbook.xml", EXCEL_WORKBOOK)
            zf.writestr("xl/_rels/workbook.xml.rels", EXCEL_RELS)
            zf.writestr("xl/sharedStrings.xml", EXCEL_SHARED)
            zf.writestr("xl/worksheets/sheet1.xml", EXCEL_SHEET)
            zf.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{MAIN}"><sheetData/></worksheet>')
        self.assertEqual(
            read_sheet(self.path),
            [["키워드", "", "pass"], [], ["budget app", "", "1"], ["habit", "TRUE"]],
        )

    def test_cells_without_position_and_other_kinds(self) -> None:
        sheet = f"""<worksheet xmlns="{MAIN}"><sheetData>
<row><c t="inlineStr"><is><t>a</t></is></c><c><v>2048</v></c><c r="D1" t="str"><v>formula text</v></c></row>
<row><c r="B2" t="e"><v>#N/A</v></c><c t="b"><v>0</v></c><c r="E2"><f>A1</f></c></row>
</sheetData></worksheet>"""
        with zipfile.ZipFile(self.path, "w") as zf:
            zf.writestr("xl/workbook.xml", EXCEL_WORKBOOK)
            zf.writestr("xl/_rels/workbook.xml.rels", EXCEL_RELS)
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        self.assertEqual(
            read_sheet(self.path),
            [["a", "2048", "", "formula text"], ["", "#N/A", "FALSE", "", ""]],
        )

    def test_corrupt_compressed_data(self) -> None:
        write_xlsx(self.path, "keywords", ["a"], [["b"]])
        data = bytearray(self.path.read_bytes())
        start = data.find(b"xl/worksheets/sheet1.xml") + len("xl/worksheets/sheet1.xml")
        data[start + 5:start + 40] = b"\xff" * 35  # 시트 압축 데이터 한가운데를 망가뜨림
        self.path.write_bytes(bytes(data))
        with self.assertRaises(XlsxReadError):
            read_sheet(self.path)

    def test_missing_or_broken_file(self) -> None:
        with self.assertRaisesRegex(XlsxReadError, "찾을 수 없습니다"):
            read_sheet(self.path)
        self.path.write_bytes(b"not a zip")
        with self.assertRaisesRegex(XlsxReadError, "엑셀"):
            read_sheet(self.path)

    def test_zip_without_workbook(self) -> None:
        with zipfile.ZipFile(self.path, "w") as zf:
            zf.writestr("hello.txt", "x")
        with self.assertRaises(XlsxReadError):
            read_sheet(self.path)

    def test_rejects_positions_beyond_excel_limits(self) -> None:
        for broken in ('<row r="99999999">', '<c r="ZZZZ4" t="inlineStr">'):
            original = '<row r="4">' if broken.startswith("<row") else '<c r="A4" t="inlineStr">'
            with zipfile.ZipFile(self.path, "w") as zf:
                zf.writestr("xl/workbook.xml", EXCEL_WORKBOOK)
                zf.writestr("xl/_rels/workbook.xml.rels", EXCEL_RELS)
                zf.writestr("xl/sharedStrings.xml", EXCEL_SHARED)
                zf.writestr("xl/worksheets/sheet1.xml", EXCEL_SHEET.replace(original, broken))
            with self.assertRaisesRegex(XlsxReadError, "너무 큽니다"):
                read_sheet(self.path)

    def test_bad_shared_string_index(self) -> None:
        for bad in ("<v>99</v>", "<v>\u00b2</v>"):
            self._check_bad_shared_string(EXCEL_SHEET.replace("<v>2</v>", bad))

    def _check_bad_shared_string(self, sheet: str) -> None:
        with zipfile.ZipFile(self.path, "w") as zf:
            zf.writestr("xl/workbook.xml", EXCEL_WORKBOOK)
            zf.writestr("xl/_rels/workbook.xml.rels", EXCEL_RELS)
            zf.writestr("xl/sharedStrings.xml", EXCEL_SHARED)
            zf.writestr("xl/worksheets/sheet1.xml", sheet)
        with self.assertRaises(XlsxReadError):
            read_sheet(self.path)


if __name__ == "__main__":
    unittest.main()
