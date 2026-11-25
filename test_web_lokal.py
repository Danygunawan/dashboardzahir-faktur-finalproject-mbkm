"""Kontrak unggahan/progres/error adaptor web; tidak menjalankan batch produksi."""
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pymupdf
from PIL import Image

from batch_common import atomic_json, read_json
from web_lokal.app import InvalidUpload, JobStore, LocalServer, OUTPUT, parse_upload
from web_lokal.worker import execute
from web_lokal.io import atomic_json as web_atomic_json


def multipart(files):
    boundary = 'faktur-boundary-test-987'
    chunks = []
    for field, filename, payload in files:
        chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode('utf-8'), payload, b'\r\n'])
    chunks.append(f'--{boundary}--\r\n'.encode())
    return f'multipart/form-data; boundary={boundary}', b''.join(chunks)


def image_bytes(format='PNG', **options):
    stream = io.BytesIO()
    Image.new('RGB', (100, 80), 'white').save(stream, format=format, **options)
    return stream.getvalue()


def pdf_bytes(encrypted=False):
    with pymupdf.open() as pdf:
        pdf.new_page()
        if encrypted:
            return pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='secret')
        return pdf.tobytes()


class UploadTests(unittest.TestCase):
    def parse(self, files):
        return parse_upload(*multipart(files))

    def test_pair_unicode_filename_and_byte_preservation(self):
        files = [('gambar', 'faktur café.png', image_bytes()), ('gambar', 'faktur2.png', image_bytes())]
        self.assertEqual(self.parse(files), files)

    def test_empty_mixed_and_wrong_type_rejected(self):
        for files in [[], [('gambar', 'x.png', b'')],
                      [('gambar', 'x.png', image_bytes()), ('pdf', 'x.pdf', pdf_bytes())],
                      [('pdf', 'x.png', image_bytes())], [('lain', 'x.pdf', pdf_bytes())]]:
            with self.subTest(files=[f[:2] for f in files]), self.assertRaises(InvalidUpload):
                self.parse(files)

    def test_corrupt_mislabeled_and_password_files_rejected(self):
        for field, name, body in [('pdf', 'a.pdf', b'not a pdf'), ('gambar', 'a.png', b'corrupt'),
                                  ('gambar', 'a.jpg', image_bytes()), ('pdf', 'locked.pdf', pdf_bytes(True))]:
            with self.subTest(name=name), self.assertRaises(InvalidUpload):
                self.parse([(field, name, body)])

    def test_multiframe_and_exif_rotation_rejected(self):
        stream = io.BytesIO()
        Image.new('RGB', (80, 80)).save(stream, format='TIFF', save_all=True,
                                      append_images=[Image.new('RGB', (80, 80), 'red')])
        exif = Image.Exif()
        exif[274] = 6
        for name, payload in [('frames.tif', stream.getvalue()), ('rotated.jpg', image_bytes('JPEG', exif=exif))]:
            with self.subTest(name=name), self.assertRaises(InvalidUpload):
                self.parse([('gambar', name, payload)])

    def test_file_size_and_pdf_page_limit(self):
        with patch('web_lokal.app.MAX_FILE', 2), self.assertRaises(InvalidUpload):
            self.parse([('gambar', 'large.png', image_bytes())])
        with pymupdf.open() as pdf:
            for _ in range(21):
                pdf.new_page()
            with self.assertRaises(InvalidUpload):
                self.parse([('pdf', 'large.pdf', pdf.tobytes())])

    def test_path_is_reduced_to_filename(self):
        parsed = self.parse([('gambar', '../sample.png', image_bytes())])
        self.assertEqual(parsed[0][1], 'sample.png')

    def test_windows_polling_does_not_block_atomic_progress_write(self):
        with tempfile.TemporaryDirectory(prefix='uji_io_') as directory:
            path = Path(directory) / 'progres.json'
            atomic_json(path, {'status': 'diproses'})
            import os
            replace = os.replace
            attempts = []

            def replace_busy_once(source, destination):
                attempts.append(source)
                if len(attempts) == 1:
                    raise PermissionError('reader Windows aktif')
                replace(source, destination)

            with patch('web_lokal.io.os.replace', side_effect=replace_busy_once):
                web_atomic_json(path, {'status': 'selesai'})
            self.assertEqual(len(attempts), 2)
            self.assertEqual(read_json(path)['status'], 'selesai')
            self.assertEqual(list(Path(directory).glob('.tmp_*')), [])

    def test_long_windows_output_path_uses_short_temporary_name(self):
        with tempfile.TemporaryDirectory(prefix='uji_path_') as directory:
            base = Path(directory).resolve()
            folder = base / ('d' * max(1, 200 - len(str(base)) - 1))
            path = folder / 'teks_koordinat_ocr.json'
            web_atomic_json(path, {'status': 'selesai'})
            self.assertEqual(read_json(path)['status'], 'selesai')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='uji_unit_')
        self.store = JobStore(self.tmp.name)
        self.submit = patch.object(self.store.executor, 'submit').start()
        self.server = LocalServer(('127.0.0.1', 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()
        patch.stopall()
        self.tmp.cleanup()

    def request(self, path, files=None, headers=None):
        data = None
        defaults = {}
        if files is not None:
            content_type, data = multipart(files)
            defaults = {'Content-Type': content_type, 'X-Local-Upload': '1', 'Origin': self.base}
        defaults.update(headers or {})
        try:
            with urlopen(Request(self.base + path, data=data, headers=defaults), timeout=10) as response:
                return response.status, response.read(), response.headers
        except HTTPError as exc:
            return exc.code, exc.read(), exc.headers

    def test_assets_and_upload_only_own_manifest(self):
        for asset in ['/', '/style.css', '/app.js', '/api/health']:
            self.assertEqual(self.request(asset)[0], 200)
        pdf_payload = pdf_bytes()
        code, body, _ = self.request('/api/jobs', [('pdf', 'x.pdf', pdf_payload), ('pdf', 'x2.pdf', pdf_payload)])
        self.assertEqual(code, 202)
        job_id = json.loads(body)['id']
        folder = self.store.folder(job_id)
        manifest = read_json(folder / 'manifest.json')
        self.assertEqual(len(manifest['dokumen']), 2)
        self.assertTrue(all(Path(d['path_sumber']).is_relative_to(folder) for d in manifest['dokumen']))
        self.submit.assert_called_once()
        self.assertEqual(self.request(f'/api/jobs/{job_id}/result')[0], 409)
        self.assertEqual(self.request(f'/api/jobs/{job_id}/source/pdf')[1], pdf_payload)

    def test_host_origin_invalid_and_download_traversal(self):
        files = [('gambar', 'x.png', image_bytes())]
        self.assertEqual(self.request('/api/jobs', files, {'Origin': 'https://outside.example'})[0], 403)
        self.assertEqual(self.request('/api/jobs', files, {'X-Local-Upload': ''})[0], 403)
        self.assertEqual(self.request('/api/health', headers={'Host': 'outside.example'})[0], 403)
        self.assertEqual(self.request('/api/jobs', [('pdf', 'broken.pdf', b'broken')])[0], 400)
        _, body, _ = self.request('/api/jobs', files)
        job_id = json.loads(body)['id']
        for suffix in ['/download/worker.log', '/download/../../manifest.json', '/source/lain']:
            self.assertEqual(self.request(f'/api/jobs/{job_id}{suffix}')[0], 404)
        self.assertEqual(self.request('/api/jobs/' + 'f' * 32)[0], 404)

    def test_restart_marks_interrupted_without_resubmitting(self):
        _, body, _ = self.request('/api/jobs', [('gambar', 'x.png', image_bytes())])
        job_id = json.loads(body)['id']
        restored = JobStore(self.tmp.name)
        try:
            self.assertEqual(restored.status(job_id)['status'], 'terputus')
            self.assertTrue(restored.status(job_id)['terminal'])
        finally:
            restored.close()

    def test_worker_reports_caught_document_failure_and_does_not_use_production(self):
        _, body, _ = self.request('/api/jobs', [('gambar', 'x.png', image_bytes())])
        folder = self.store.folder(json.loads(body)['id'])

        def failed_batch(manifest, **kwargs):
            self.assertEqual(manifest, folder / 'manifest.json')
            self.assertEqual(kwargs['limit'], 1)
            self.assertEqual(kwargs['cache_dir'], folder / 'cache_ocr')
            atomic_json(folder / 'run/progres.json', {'dokumen': [{'status': 'gagal'}]})

        with patch('web_lokal.worker.run_batch', side_effect=failed_batch):
            self.assertEqual(execute(folder), 1)
        self.assertEqual(read_json(folder / 'job.json')['status'], 'gagal')

    def test_worker_startup_exception_is_visible(self):
        _, body, _ = self.request('/api/jobs', [('gambar', 'x.png', image_bytes())])
        folder = self.store.folder(json.loads(body)['id'])
        with patch('web_lokal.worker.run_batch', side_effect=RuntimeError('dependency unavailable')):
            self.assertEqual(execute(folder), 1)
        self.assertIn('dependency unavailable', self.store.status(folder.name)['pesan'])


if __name__ == '__main__':
    unittest.main()
