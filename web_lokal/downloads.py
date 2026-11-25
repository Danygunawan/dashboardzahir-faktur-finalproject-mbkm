"""Ekspor tabel sesi ke XLSX tanpa mengubah CSV atau nilai hasil ekstraksi."""
import csv
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

TABLES = {'faktur': 'Faktur', 'detail_faktur': 'Detail Faktur'}


def fill_sheet(sheet, csv_path):
    with Path(csv_path).open(encoding='utf-8-sig', newline='') as handle:
        fill_values(sheet, csv.reader(handle))


def fill_values(sheet, values):
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    for row_index, row in enumerate(values, 1):
        for column, value in enumerate(row, 1):
            if len(value) > 32767:
                raise ValueError('Teks sel melampaui batas Excel; gunakan unduhan CSV untuk tabel ini.')
            cell = sheet.cell(row_index, column, value)
            # String eksplisit, termasuk angka, nol di depan, dan awalan '='.
            cell.data_type = 's'
            cell.number_format = '@'
            if row_index == 1:
                cell.font = Font(bold=True)
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    for column in range(1, sheet.max_column + 1):
        sheet.column_dimensions[get_column_letter(column)].width = 24


def build_excel(tables):
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.remove(workbook.active)
    try:
        for csv_path, title in tables:
            fill_sheet(workbook.create_sheet(title), csv_path)
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()
    finally:
        workbook.close()


def excel_bytes(csv_path, sheet_name):
    return build_excel([(csv_path, sheet_name)])


def matrix_excel_bytes(tables):
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.remove(workbook.active)
    try:
        for values, title in tables:
            fill_values(workbook.create_sheet(title), values)
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()
    finally:
        workbook.close()


def session_excel_bytes(export):
    return build_excel([(Path(export) / (name + '.csv'), title) for name, title in TABLES.items()])


def session_csv_zip(export):
    output = BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for name in TABLES:
            filename = name + '.csv'
            archive.writestr(filename, (Path(export) / filename).read_bytes())
    return output.getvalue()
