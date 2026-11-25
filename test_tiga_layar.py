"""Batas sesi dan persiapan unggahan; seluruh worker dimock, tanpa OCR massal."""
import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import load_workbook

from batch_common import read_json
import test_web_lokal as legacy
from test_web_lokal import image_bytes, pdf_bytes
from web_lokal.app import InvalidUpload, OUTPUT, parse_upload
from web_lokal.downloads import session_excel_bytes, session_csv_zip
from test_web_lokal import multipart


class SelectionLimits(unittest.TestCase):
    def test_60_images_accepted_61_rejected(self):
        image = image_bytes()
        files = [('gambar', f'{i}.png', image) for i in range(60)]
        self.assertEqual(len(parse_upload(*multipart(files))), 60)
        with self.assertRaisesRegex(InvalidUpload, '60 gambar'):
            parse_upload(*multipart(files + [('gambar', '61.png', image)]))

    def test_10_pdfs_accepted_11_rejected(self):
        pdf = pdf_bytes()
        files = [('pdf', f'{i}.pdf', pdf) for i in range(10)]
        self.assertEqual(len(parse_upload(*multipart(files))), 10)
        with self.assertRaisesRegex(InvalidUpload, '10 PDF'):
            parse_upload(*multipart(files + [('pdf', '11.pdf', pdf)]))


class StagedHTTP(unittest.TestCase):
    setUp = legacy.HTTPTests.setUp
    tearDown = legacy.HTTPTests.tearDown
    request = legacy.HTTPTests.request

    def json_post(self, value):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        try:
            with urlopen(Request(self.base + '/api/jobs', data=json.dumps(value).encode(),
                         headers={'Content-Type': 'application/json', 'X-Local-Upload': '1', 'Origin': self.base})) as response:
                return response.status, json.loads(response.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_stage_does_not_start_worker_and_format_is_retained(self):
        code, body, _ = self.request('/api/uploads', [('gambar', 'same.png', image_bytes())])
        self.assertEqual(code, 201)
        first = json.loads(body)
        self.submit.assert_not_called()
        self.assertEqual(list((self.store.root / 'jobs').iterdir()), [])
        _, body, _ = self.request('/api/uploads', [('gambar', 'same.png', image_bytes())])
        second = json.loads(body)
        code, response = self.json_post({'jenis': 'gambar', 'format': 'csv', 'unggahan': [first['id'], second['id']]})
        self.assertEqual(code, 202)
        self.submit.assert_called_once()
        job = self.store.folder(response['id'])
        state = read_json(job / 'job.json')
        self.assertEqual(state['format'], 'csv')
        entries = read_json(job / 'manifest.json')['dokumen']
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0]['path_sumber'], entries[1]['path_sumber'])
        self.assertTrue(all(Path(e['path_sumber']).read_bytes() == image_bytes() for e in entries))

    def test_staged_mix_and_forged_session_type_rejected(self):
        _, im, _ = self.request('/api/uploads', [('gambar', 'x.png', image_bytes())])
        _, pdf, _ = self.request('/api/uploads', [('pdf', 'x.pdf', pdf_bytes())])
        image_id, pdf_id = json.loads(im)['id'], json.loads(pdf)['id']
        code, response = self.json_post({'jenis': 'gambar', 'unggahan': [image_id, pdf_id]})
        self.assertEqual(code, 400)
        self.assertIn('dicampur', response['error'])
        self.assertEqual(self.json_post({'jenis': 'pdf', 'unggahan': [image_id]})[0], 400)
        self.submit.assert_not_called()

    def test_http_count_boundaries_without_worker(self):
        for kind, count, suffix, payload in [('gambar', 60, 'png', image_bytes()), ('pdf', 10, 'pdf', pdf_bytes())]:
            with self.subTest(kind=kind):
                self.submit.reset_mock()
                files = [(kind, f'{i}.{suffix}', payload) for i in range(count)]
                self.assertEqual(self.request('/api/jobs', files)[0], 202)
                self.submit.assert_called_once()
                self.submit.reset_mock()
                self.assertEqual(self.request('/api/jobs', files + [(kind, f'last.{suffix}', payload)])[0], 400)
                self.submit.assert_not_called()
                # Start endpoint also rejects oversized ticket lists before resolution.
                self.assertEqual(self.json_post({'jenis': kind, 'unggahan': ['f'*32]*(count+1)})[0], 400)

    def test_invalid_stage_and_invalid_format_never_start_worker(self):
        self.assertEqual(self.request('/api/uploads', [('pdf', 'broken.pdf', b'broken')])[0], 400)
        self.assertEqual(self.request('/api/uploads', [('gambar', 'a.png', image_bytes()), ('gambar', 'b.png', image_bytes())])[0], 400)
        _, body, _ = self.request('/api/uploads', [('gambar', 'a.png', image_bytes())])
        token = json.loads(body)['id']
        self.assertEqual(self.json_post({'jenis': 'gambar', 'format': 'other', 'unggahan': [token]})[0], 400)
        self.submit.assert_not_called()

    def test_start_accepts_full_valid_ticket_counts(self):
        for kind, count, name, payload in [('gambar', 60, 'same.png', image_bytes()), ('pdf', 10, 'same.pdf', pdf_bytes())]:
            with self.subTest(kind=kind):
                self.submit.reset_mock()
                tokens = [self.store.stage([(kind, name, payload)])['id'] for _ in range(count)]
                code, response = self.json_post({'jenis': kind, 'format': 'xlsx', 'unggahan': tokens})
                self.assertEqual(code, 202)
                manifest = read_json(self.store.folder(response['id']) / 'manifest.json')
                self.assertEqual(len(manifest['dokumen']), count)
                self.assertEqual(len({d['path_sumber'] for d in manifest['dokumen']}), count)
                self.submit.assert_called_once()


class BundledDownloads(unittest.TestCase):
    def test_workbook_and_zip_preserve_blanks_zero_and_identity(self):
        with tempfile.TemporaryDirectory(prefix='uji_bundle_') as folder:
            export = Path(folder)
            tables = {'faktur': [['id_faktur', 'nomor_invoice', 'total', 'kosong'], ['00001', '000123', '0', '']],
                      'detail_faktur': [['id_faktur', 'nomor_baris_item', 'jumlah'], ['00001', '0001', '0']]}
            for name, rows in tables.items():
                with (export / (name + '.csv')).open('w', encoding='utf-8-sig', newline='') as handle:
                    csv.writer(handle).writerows(rows)
            xlsx = session_excel_bytes(export)
            workbook = load_workbook(io.BytesIO(xlsx))
            try:
                self.assertEqual(workbook.sheetnames, ['Faktur', 'Detail Faktur'])
                for (name, expected), sheet in zip(tables.items(), workbook):
                    values = [[c.value if c.value is not None else '' for c in row] for row in sheet.iter_rows()]
                    self.assertEqual(values, expected)
                    self.assertTrue(all(c.data_type == 's' for row in sheet.iter_rows(min_row=2) for c in row if c.value is not None))
            finally:
                workbook.close()
            with zipfile.ZipFile(io.BytesIO(session_csv_zip(export))) as archive:
                self.assertEqual(archive.namelist(), ['faktur.csv', 'detail_faktur.csv'])
                for name in archive.namelist():
                    self.assertEqual(archive.read(name), (export / name).read_bytes())
