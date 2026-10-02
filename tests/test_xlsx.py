import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from keyword_scanner.xlsx_writer import _column_letter, write_xlsx

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class XlsxWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "out.xlsx"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_column_letters(self) -> None:
        self.assertEqual([_column_letter(i) for i in (0, 25, 26, 27, 701)], ["A", "Z", "AA", "AB", "ZZ"])

    def test_writes_valid_workbook(self) -> None:
        write_xlsx(self.path, "keywords", ["키워드", "점수"], [["a & <b>", 1.5], ["bad\x01char", 7]])
        with zipfile.ZipFile(self.path) as zf:
            names = set(zf.namelist())
            self.assertTrue({"[Content_Types].xml", "xl/workbook.xml", "xl/worksheets/sheet1.xml"} <= names)
            for name in names:
                ElementTree.fromstring(zf.read(name))  # 모든 XML이 문법상 올바른지
            sheet = ElementTree.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        rows = sheet.findall(".//m:row", NS)
        self.assertEqual(len(rows), 3)
        texts = [t.text for t in sheet.findall(".//m:t", NS)]
        self.assertEqual(texts, ["키워드", "점수", "a & <b>", "badchar"])
        values = [v.text for v in sheet.findall(".//m:v", NS)]
        self.assertEqual(values, ["1.5", "7"])
        self.assertEqual(sheet.find(".//m:autoFilter", NS).get("ref"), "A1:B3")

    def test_no_temp_file_left(self) -> None:
        write_xlsx(self.path, "s", ["a"], [[1]])
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["out.xlsx"])


if __name__ == "__main__":
    unittest.main()
