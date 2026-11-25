"""Regresi ekspor teks: nol di depan, ID panjang, dan isi mirip formula."""
import csv
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from web_lokal.app import OUTPUT
from web_lokal.downloads import excel_bytes


class ExcelTextTests(unittest.TestCase):
    def test_preserves_text_leading_zeros_and_formula_like_values(self):
        with tempfile.TemporaryDirectory(prefix='uji_xlsx_') as directory:
            path = Path(directory) / 'faktur.csv'
            rows = [['id_faktur', 'nomor_invoice', 'nomor_baris_item', 'catatan', 'nilai_kosong'],
                    ['000000001234567890123456', '0000123', '0001', '=1+1', ''],
                    ['001', '001/INV', '02', 'Baris pertama\nBaris kedua café', '']]
            with path.open('w', encoding='utf-8-sig', newline='') as handle:
                csv.writer(handle).writerows(rows)
            original = path.read_bytes()
            content = excel_bytes(path, 'Faktur')
            self.assertEqual(path.read_bytes(), original)
            self.assertTrue(zipfile.is_zipfile(io.BytesIO(content)))
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                self.assertIn('[Content_Types].xml', archive.namelist())
                self.assertNotIn(b'<f>', archive.read('xl/worksheets/sheet1.xml'))
            workbook = load_workbook(io.BytesIO(content), data_only=False)
            try:
                self.assertEqual(workbook.sheetnames, ['Faktur'])
                sheet = workbook.active
                actual = [[cell.value if cell.value is not None else '' for cell in row]
                          for row in sheet.iter_rows()]
                self.assertEqual(actual, rows)
                for row in sheet.iter_rows(min_row=2):
                    for cell in row:
                        if cell.value is not None:
                            self.assertEqual(cell.data_type, 's')
                        self.assertEqual(cell.number_format, '@')
            finally:
                workbook.close()

    def test_rejects_overlong_cell_instead_of_silently_truncating(self):
        with tempfile.TemporaryDirectory(prefix='uji_xlsx_') as directory:
            path = Path(directory) / 'faktur.csv'
            with path.open('w', encoding='utf-8-sig', newline='') as handle:
                csv.writer(handle).writerows([['nomor_invoice'], ['0' * 32768]])
            with self.assertRaisesRegex(ValueError, 'batas Excel'):
                excel_bytes(path, 'Faktur')


if __name__ == '__main__':
    unittest.main()
