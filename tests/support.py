"""Sesi sintetis deterministik. Tidak membaca outputs, arsip, atau model lokal."""
import csv
import io
from pathlib import Path

from batch_common import atomic_json, seal
from ekstrak_faktur_digital import FAKTUR, DETAIL

SOURCES = ['1' * 32, '2' * 32]


def make_session(root, session_id, partial=False):
    if session_id not in SOURCES:
        raise ValueError('Gunakan ID fixture sintetis yang tersedia.')
    folder = Path(root) / 'jobs' / session_id
    export = folder / 'run/ekspor/synthetic'
    export.mkdir(parents=True)
    count = 1 if session_id == SOURCES[0] else 2
    invoices, details, evidence, review, documents, uploads = [], [], [], [], [], []
    for index in range(count):
        invoice_id = f'{index + 1:05d}'
        row = dict.fromkeys(FAKTUR, '')
        row.update(id_faktur=invoice_id, nomor_invoice=f'{index + 123:06d}',
                   tanggal_invoice='2026-01-01', nama_penerbit='PT DEMO SINTETIS',
                   nama_pelanggan='PELANGGAN UJI FIKTIF', total_akhir='0',
                   subtotal_faktur='0', status_validasi='perlu_review',
                   file_sumber=f'synthetic_{index + 1}.pdf', catatan_validasi='DATA SINTETIS; bukan faktur pelanggan.')
        invoices.append(row)
        items = []
        for number in range(3 if index == 0 else 1):
            detail = dict.fromkeys(DETAIL, '')
            detail.update(id_faktur=invoice_id, nomor_baris_item=f'{number + 1:04d}',
                          nama_barang=f'ITEM UJI SINTETIS {number + 1}', jumlah='0', unit='unit',
                          harga_satuan='0', subtotal_item='0', diskon_item_persen='0', status_validasi='perlu_review')
            details.append(detail)
            items.append({'nilai': detail, 'kolom': {}})
        evidence.append({'nilai': row, 'item': items, 'kolom': {}, 'hash_sumber': '0' * 64, 'validasi': []})
        review.append({'prioritas': 3, 'id_faktur': invoice_id, 'nomor_item': '', 'jenis': 'fixture',
                       'kolom': 'nomor_invoice', 'pesan': 'Data sintetis untuk pengujian; tanpa OCR.'})
        document = {'nama_file': row['file_sumber'], 'status': 'perlu_review', 'error': None}
        documents.append(document)
        uploads.append({'jenis': 'pdf', 'nama_file': row['file_sumber'], 'ukuran_byte': 1})
    if partial:
        documents.append({'nama_file': 'synthetic_failed.pdf', 'status': 'gagal', 'error': {'pesan': 'Kegagalan sintetis yang disengaja.'}})
    for filename, fields, rows in [('faktur.csv', FAKTUR, invoices), ('detail_faktur.csv', DETAIL, details),
                                   ('daftar_review.csv', list(review[0]), review)]:
        with (export / filename).open('x', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)
    atomic_json(export / 'bukti_ekstraksi.json', {'asal': 'fixture_sintetis_tanpa_ocr', 'faktur': evidence, 'review': review,
        'dokumen': [{'sumber': item['nama_file'], 'status': item['status'], 'halaman': [], 'kendala': []}
                    for item in documents if item['status'] != 'gagal']})
    seal(export)
    atomic_json(folder / 'job.json', {'id': session_id, 'status': 'sebagian_gagal' if partial else 'perlu_review',
        'jenis': 'pdf', 'format': 'xlsx', 'unggahan': uploads, 'dibuat': '2026-01-01T00:00:00+00:00',
        'pesan': 'Fixture sintetis tersimpan; tidak menjalankan OCR.'})
    atomic_json(folder / 'run/progres.json', {'dokumen': documents, 'ekspor_terakhir': str(export.resolve())})
    return folder, export


def check_bundle(content, expected):
    from openpyxl import load_workbook
    workbook = load_workbook(io.BytesIO(content), data_only=False)
    try:
        assert workbook.sheetnames == ['Faktur', 'Detail Faktur']
        for key, sheet in zip(['faktur', 'detail_faktur'], workbook):
            actual = [[cell.value if cell.value is not None else '' for cell in row] for row in sheet.iter_rows()]
            assert actual == expected[key]
            for row in sheet.iter_rows(min_row=2):
                for cell in row:
                    if cell.value is not None:
                        assert cell.data_type == 's'
                    assert cell.number_format == '@'
    finally:
        workbook.close()
