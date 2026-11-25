"""HTTP lokal dengan antrean ekstraksi dan progres asli dari run_batch()."""
import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit
from uuid import uuid4

from batch_common import digest, lock_run, now
from web_lokal.io import atomic_json, read_json
from web_lokal.downloads import TABLES, excel_bytes, session_excel_bytes, session_csv_zip
from web_lokal.data import collect_data, data_excel_bytes, data_csv_zip

ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent / 'static'
OUTPUT = ROOT / 'outputs/web_lokal'
MAX_FILE = 25 * 1024 * 1024
MAX_BODY = 2 * MAX_FILE + 1024 * 1024
LIMITS = {'gambar': 60, 'pdf': 10}
IMAGE_FORMATS = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.bmp': 'BMP',
                 '.webp': 'WEBP', '.tif': 'TIFF', '.tiff': 'TIFF'}
TERMINAL = {'selesai', 'perlu_review', 'gagal', 'sebagian_gagal', 'terputus'}
DOWNLOADS = {'faktur.csv': 'text/csv; charset=utf-8',
             'hasil_faktur.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
             'hasil_faktur_csv.zip': 'application/zip',
             'detail_faktur.csv': 'text/csv; charset=utf-8',
             'faktur.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
             'detail_faktur.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
             'daftar_review.csv': 'text/csv; charset=utf-8',
             'bukti_ekstraksi.json': 'application/json; charset=utf-8'}
PUBLIC_DOWNLOADS = ('hasil_faktur.xlsx', 'hasil_faktur_csv.zip')


class InvalidUpload(ValueError):
    pass


def validate_selection(kinds):
    if not kinds:
        raise InvalidUpload('Pilih sedikitnya satu dokumen.')
    if len(set(kinds)) != 1 or kinds[0] not in LIMITS:
        raise InvalidUpload('Gambar dan PDF tidak boleh dicampur dalam satu sesi.')
    kind = kinds[0]
    if len(kinds) > LIMITS[kind]:
        raise InvalidUpload(f'Maksimal {LIMITS[kind]} {"gambar" if kind == "gambar" else "PDF"} per sesi.')
    return kind


def parse_upload(content_type, body):
    if not content_type.lower().startswith('multipart/form-data'):
        raise InvalidUpload('Gunakan formulir unggah gambar/PDF.')
    message = BytesParser(policy=policy.default).parsebytes(
        ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
    if not message.is_multipart() or message.defects:
        raise InvalidUpload('Formulir unggahan tidak valid.')
    files = []
    for part in message.iter_parts():
        field = part.get_param('name', header='content-disposition')
        filename = part.get_filename()
        if field not in LIMITS or not filename or part.defects:
            raise InvalidUpload('Pilih dokumen gambar atau PDF yang valid.')
        # Browser dapat mengirim C:\\fakepath; hanya nama terakhir dipertahankan.
        filename = PureWindowsPath(filename).name
        if (not filename or len(filename) > 180 or filename in {'.', '..'}
                or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in filename)
                or filename.endswith((' ', '.')) or PureWindowsPath(filename).is_reserved()):
            raise InvalidUpload('Nama file tidak valid; ganti nama lalu unggah kembali.')
        payload = part.get_payload(decode=True)
        if not payload or len(payload) > MAX_FILE:
            raise InvalidUpload('Setiap file harus berisi data dan berukuran maksimal 25 MB.')
        suffix = Path(filename).suffix.lower()
        if (field == 'pdf' and suffix != '.pdf') or (field == 'gambar' and suffix not in IMAGE_FORMATS):
            raise InvalidUpload('Jenis file tidak sesuai: gunakan gambar yang didukung atau PDF.')
        files.append((field, filename, payload))
    validate_selection([f[0] for f in files])
    for _, filename, payload in files:
        try:
            validate_content(payload, Path(filename).suffix.lower())
        except InvalidUpload as exc:
            raise InvalidUpload(filename + ': ' + str(exc)) from exc
    return files


def validate_content(payload, suffix):
    try:
        if suffix == '.pdf':
            import pymupdf
            with pymupdf.open(stream=payload, filetype='pdf') as pdf:
                if pdf.needs_pass:
                    raise InvalidUpload('PDF dilindungi kata sandi; gunakan salinan tanpa kata sandi.')
                if not 1 <= len(pdf) <= 20:
                    raise InvalidUpload('Tahap pertama menerima PDF dengan 1–20 halaman.')
        else:
            from PIL import Image
            with Image.open(io.BytesIO(payload)) as image:
                if image.format != IMAGE_FORMATS[suffix]:
                    raise InvalidUpload('Isi gambar berbeda dengan ekstensi file.')
                if getattr(image, 'n_frames', 1) != 1:
                    raise InvalidUpload('Gambar multi-frame belum didukung; gunakan satu gambar per file.')
                if image.getexif().get(274, 1) != 1:
                    raise InvalidUpload('Rotasi EXIF belum didukung; simpan gambar dengan orientasi tegak.')
            # PNG getexif() dapat memuat piksel; verify() memerlukan objek baru.
            with Image.open(io.BytesIO(payload)) as image:
                image.verify()
    except InvalidUpload:
        raise
    except Exception as exc:
        raise InvalidUpload('File rusak atau tidak dapat dibaca sebagai gambar/PDF.') from exc


class JobStore:
    def __init__(self, root=OUTPUT, recover=True):
        self.root = Path(root).resolve()
        (self.root / 'jobs').mkdir(parents=True, exist_ok=True)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ekstraksi')
        # Tidak menjalankan ulang job ketika server dinyalakan kembali.
        for path in (self.root / 'jobs').glob('*/job.json') if recover else ():
            state = read_json(path)
            if state['status'] not in TERMINAL:
                state.update(status='terputus', pesan='Server sebelumnya berhenti. Unggah kembali untuk sesi baru.')
                atomic_json(path, state)

    def folder(self, job_id):
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise FileNotFoundError('Sesi tidak ditemukan.')
        folder = self.root / 'jobs' / job_id
        if not (folder / 'job.json').is_file():
            raise FileNotFoundError('Sesi tidak ditemukan.')
        return folder

    def stage(self, files):
        if len(files) != 1:
            raise InvalidUpload('Unggah satu file pada setiap permintaan persiapan.')
        kind, name, payload = files[0]
        token = uuid4().hex
        folder = self.root / 'persiapan' / token
        folder.mkdir(parents=True)
        source = folder / ('input' + Path(name).suffix.lower())
        source.write_bytes(payload)
        metadata = {'id': token, 'jenis': kind, 'nama_file': name, 'ukuran_byte': len(payload),
                    'sha256': digest(source), 'file': source.name}
        atomic_json(folder / 'upload.json', metadata)
        return metadata

    def create_staged(self, request):
        tokens = request.get('unggahan', [])
        kind = request.get('jenis')
        if not isinstance(tokens, list) or kind not in LIMITS:
            raise InvalidUpload('Jenis sesi atau daftar unggahan tidak valid.')
        validate_selection([kind] * len(tokens))
        if any(not isinstance(token, str) for token in tokens):
            raise InvalidUpload('Referensi unggahan tidak valid.')
        if len(set(tokens)) != len(tokens):
            raise InvalidUpload('Referensi unggahan berulang.')
        files = []
        for token in tokens:
            if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{32}', token):
                raise InvalidUpload('Referensi unggahan tidak valid.')
            folder = self.root / 'persiapan' / token
            if not (folder / 'upload.json').is_file():
                raise InvalidUpload('Unggahan persiapan tidak ditemukan. Pilih file kembali.')
            metadata = read_json(folder / 'upload.json')
            source = (folder / metadata['file']).resolve()
            if not source.is_relative_to(folder.resolve()) or digest(source) != metadata['sha256']:
                raise InvalidUpload('Unggahan berubah; pilih file kembali.')
            files.append((metadata['jenis'], metadata['nama_file'], source))
        validate_selection([f[0] for f in files])
        if files[0][0] != kind:
            raise InvalidUpload('Jenis file berbeda dengan jenis sesi.')
        return self.create(files, request.get('format', 'xlsx'))

    def create(self, files, result_format='xlsx'):
        kind = validate_selection([f[0] for f in files])
        if result_format not in {'xlsx', 'csv'}:
            raise InvalidUpload('Format hasil harus Excel (.xlsx) atau CSV.')
        job_id = uuid4().hex
        folder = self.root / 'jobs' / job_id
        folder.mkdir()
        entries = []
        uploads = []
        for index, (field, name, payload) in enumerate(files, 1):
            path = folder / 'unggahan' / field / f'{index:03d}' / name
            if len(str(path)) > 240:
                path = path.with_name('input' + Path(name).suffix.lower())
            path.parent.mkdir(parents=True)
            if isinstance(payload, Path):
                shutil.copyfile(payload, path)
            else:
                with path.open('xb') as handle:
                    handle.write(payload)
            entries.append({'path_sumber': str(path), 'folder_klien': 'unggahan_web',
                            'nama_file': name, 'path_relatif': f'{field}/{index:03d}/{name}',
                            'sha256_diharapkan': digest(path)})
            uploads.append({'jenis': field, 'nama_file': name, 'ukuran_byte': path.stat().st_size})
        atomic_json(folder / 'manifest.json', {'asal': 'unggahan_web_lokal_tahap_1', 'dokumen': entries})
        atomic_json(folder / 'job.json', {'id': job_id, 'status': 'menunggu', 'dibuat': now(),
                                        'pesan': 'Menunggu giliran ekstraksi.', 'unggahan': uploads,
                                        'jenis': kind, 'format': result_format})
        self.executor.submit(self._run, folder)
        return job_id

    def _run(self, folder):
        try:
            with (folder / 'worker.log').open('xb') as log:
                proc = subprocess.run([sys.executable, '-u', '-m', 'web_lokal.worker', str(folder)],
                                      cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                      creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0,
                                      start_new_session=sys.platform != 'win32')
            state = read_json(folder / 'job.json')
            if state['status'] not in TERMINAL:
                state.update(status='gagal', pesan=f'Proses ekstraksi berhenti (kode {proc.returncode}). Lihat worker.log.')
                atomic_json(folder / 'job.json', state)
        except Exception as exc:
            state = read_json(folder / 'job.json')
            state.update(status='gagal', pesan=f'Proses ekstraksi tidak dapat dijalankan: {exc}')
            atomic_json(folder / 'job.json', state)

    def status(self, job_id):
        folder = self.folder(job_id)
        state = read_json(folder / 'job.json')
        progress_path = folder / 'run/progres.json'
        if progress_path.is_file():
            progress = read_json(progress_path)
            docs = [{k: d.get(k) for k in ('nama_file', 'status', 'waktu_mulai', 'waktu_selesai')}
                    | {'error': (d.get('error') or {}).get('pesan')} for d in progress['dokumen']]
        else:
            docs = [{'nama_file': u['nama_file'], 'status': 'menunggu'} for u in state['unggahan']]
        state['dokumen'] = docs
        state['jumlah_selesai'] = sum(d['status'] in TERMINAL for d in docs)
        state['jumlah_dokumen'] = len(docs)
        state['terminal'] = state['status'] in TERMINAL
        state.setdefault('format', 'xlsx')
        if state['status'] == 'diproses':
            current = next((d for d in docs if d['status'] == 'diproses'), None)
            if current:
                state['pesan'] = f"Memproses {current['nama_file']}. Progres diperbarui per dokumen; OCR dapat memerlukan waktu."
            elif state['jumlah_selesai'] == len(docs):
                state['pesan'] = 'Menyusun CSV dan catatan review.'
        state['hasil_tersedia'] = state['terminal'] and self.export_folder(folder) is not None
        return state

    @staticmethod
    def export_folder(folder):
        path = folder / 'run/progres.json'
        if not path.is_file():
            return None
        value = read_json(path).get('ekspor_terakhir')
        if not value:
            return None
        export = Path(value).resolve()
        if not export.is_relative_to((folder / 'run/ekspor').resolve()):
            raise ValueError('Lokasi hasil di luar sesi.')
        return export if (export / 'complete.json').is_file() else None

    def result(self, job_id):
        folder = self.folder(job_id)
        if read_json(folder / 'job.json')['status'] not in TERMINAL:
            raise InvalidUpload('Ekstraksi masih berjalan.')
        export = self.export_folder(folder)
        if export is None:
            raise FileNotFoundError('Hasil belum tersedia. Periksa status ekstraksi.')
        data = {}
        for key, name in [('faktur', 'faktur.csv'), ('detail_faktur', 'detail_faktur.csv'),
                          ('review', 'daftar_review.csv')]:
            with (export / name).open(encoding='utf-8-sig', newline='') as handle:
                reader = csv.DictReader(handle)
                data[key] = {'kolom': reader.fieldnames, 'baris': list(reader)}
        evidence = read_json(export / 'bukti_ekstraksi.json')
        data['dokumen'] = evidence['dokumen']
        data['unduhan'] = {name: f'/api/jobs/{job_id}/download/{name}' for name in PUBLIC_DOWNLOADS}
        return data

    def close(self):
        self.executor.shutdown(wait=True)


class Handler(BaseHTTPRequestHandler):
    server_version = 'FakturLokal/1'

    def reply(self, code, body, content_type='application/json; charset=utf-8', filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; frame-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def allowed_host(self):
        if os.environ.get('PORT') or getattr(self.server, 'server_address', ('', 0))[0] == '0.0.0.0':
            return bool(self.headers.get('Host'))
        return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',
                                           f'localhost:{self.server.server_port}'}

    def do_GET(self):
        if not self.allowed_host():
            return self.reply(403, {'error': 'Gunakan alamat localhost yang ditampilkan server.'})
        path = urlsplit(self.path).path
        try:
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/theme.js': ('theme.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8'),
                      '/zahir-one.png': ('zahir-one.png', 'image/png'),
                      '/InterVariable.woff2': ('InterVariable.woff2', 'font/woff2')}
            if path in assets:
                name, mime = assets[path]
                return self.reply(200, (STATIC / name).read_bytes(), mime)
            if path == '/api/health':
                return self.reply(200, {'status': 'ok', 'tahap': 1})
            if path == '/api/data':
                return self.reply(200, collect_data(self.server.store.root))
            if path in {'/api/data/download/data_faktur.xlsx', '/api/data/download/data_faktur_csv.zip'}:
                data = collect_data(self.server.store.root)
                if path.endswith('.xlsx'):
                    return self.reply(200, data_excel_bytes(data), DOWNLOADS['hasil_faktur.xlsx'], 'data_faktur.xlsx')
                return self.reply(200, data_csv_zip(data), 'application/zip', 'data_faktur_csv.zip')
            match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(result|download/([^/]+)|source/(gambar|pdf)))?', path)
            if not match:
                raise FileNotFoundError('Halaman tidak ditemukan.')
            job_id, action, download, source = match.groups()
            store = self.server.store
            if action is None:
                return self.reply(200, store.status(job_id))
            if action == 'result':
                return self.reply(200, store.result(job_id))
            folder = store.folder(job_id)
            if download:
                if download not in DOWNLOADS or not store.status(job_id)['terminal']:
                    raise FileNotFoundError('Unduhan tidak tersedia.')
                export = store.export_folder(folder)
                if export is None:
                    raise FileNotFoundError('Unduhan tidak tersedia.')
                if download == 'hasil_faktur.xlsx':
                    body = session_excel_bytes(export)
                elif download == 'hasil_faktur_csv.zip':
                    body = session_csv_zip(export)
                elif download.endswith('.xlsx'):
                    table = download.removesuffix('.xlsx')
                    body = excel_bytes(export / (table + '.csv'), TABLES[table])
                else:
                    body = (export / download).read_bytes()
                return self.reply(200, body, DOWNLOADS[download], download)
            uploads = read_json(folder / 'manifest.json')['dokumen']
            entry = next((e for e in uploads if Path(e['path_relatif']).parts[0] == source), None)
            if entry is None:
                raise FileNotFoundError('Berkas sumber tidak tersedia.')
            file = Path(entry['path_sumber']).resolve()
            if not file.is_relative_to((folder / 'unggahan').resolve()):
                raise FileNotFoundError('Berkas sumber tidak tersedia.')
            mime = 'application/pdf' if source == 'pdf' else 'image/' + IMAGE_FORMATS[file.suffix.lower()].lower()
            return self.reply(200, file.read_bytes(), mime)
        except FileNotFoundError as exc:
            self.reply(404, {'error': str(exc)})
        except InvalidUpload as exc:
            self.reply(409, {'error': str(exc)})
        except ConnectionError:
            pass
        except Exception as exc:
            self.log_error('Kesalahan membaca sesi: %s', exc)
            self.reply(500, {'error': 'Gagal membaca sesi; lihat terminal server.'})

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                return self.reply(413, {'error': 'Permintaan terlalu besar. Unggah setiap file secara terpisah (maksimal 25 MB per file).'})
            self.connection.settimeout(60)
            body = self.rfile.read(length)
            if len(body) != length:
                raise InvalidUpload('Unggahan tidak lengkap; coba kembali.')
            # Habiskan body terbatas sebelum respons penolakan: socket Windows
            # dapat mengirim reset jika ditutup dengan body yang belum dibaca.
            if not self.allowed_host() or self.headers.get('X-Local-Upload') != '1':
                return self.reply(403, {'error': 'Unggah melalui halaman web lokal.'})
            origin = self.headers.get('Origin')
            host = self.headers.get('Host', '')
            if origin and origin not in (f'http://{host}', f'https://{host}'):
                if not (os.environ.get('PORT') and (origin.endswith('.hf.space') or origin == 'https://huggingface.co' or origin.endswith('.koyeb.app') or origin.endswith('.onrender.com'))):
                    return self.reply(403, {'error': 'Asal permintaan tidak diizinkan.'})
            if self.path not in {'/api/jobs', '/api/uploads'}:
                return self.reply(404, {'error': 'Endpoint tidak ditemukan.'})
            if self.path == '/api/uploads':
                files = parse_upload(self.headers.get('Content-Type', ''), body)
                return self.reply(201, self.server.store.stage(files))
            if self.headers.get('Content-Type', '').split(';')[0] == 'application/json':
                request = json.loads(body)
                if not isinstance(request, dict):
                    raise InvalidUpload('Permintaan sesi tidak valid.')
                job_id = self.server.store.create_staged(request)
            else:
                files = parse_upload(self.headers.get('Content-Type', ''), body)
                job_id = self.server.store.create(files)
            self.reply(202, {'id': job_id, 'status_url': f'/api/jobs/{job_id}'})
        except (ValueError, TimeoutError) as exc:
            self.reply(400, {'error': str(exc)})
        except ConnectionError:
            pass
        except Exception as exc:
            self.log_error('Gagal membuat sesi: %s', exc)
            self.reply(500, {'error': 'Gagal membuat sesi. Periksa ruang disk dan terminal server.'})


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        self.store = store
        super().__init__(address, Handler)


def serve(port, host=None):
    if host is None:
        host = os.environ.get('HOST', '0.0.0.0' if os.environ.get('PORT') else '127.0.0.1')
    store = JobStore()
    try:
        server = LocalServer((host, port), store)
    except OSError:
        store.close()
        raise SystemExit(f'Port {port} tidak tersedia. Coba: python web_faktur.py --port {port + 1}')
    print(f'Web faktur: http://{host}:{server.server_port}', flush=True)
    print(f'Hasil sesi: {OUTPUT}\nCtrl+C untuk berhenti; tunggu pekerjaan aktif selesai.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nMenghentikan server; menunggu ekstraksi yang sudah diterima.', flush=True)
    finally:
        server.server_close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_port = int(os.environ.get('PORT', 8765))
    default_host = os.environ.get('HOST', '0.0.0.0' if os.environ.get('PORT') else '127.0.0.1')
    parser.add_argument('--port', type=int, default=default_port)
    parser.add_argument('--host', type=str, default=default_host)
    args = parser.parse_args()
    # Mencegah dua server menandai sesi aktif milik proses lain sebagai terputus.
    try:
        with lock_run(OUTPUT):
            serve(args.port, args.host)
    except (OSError, BlockingIOError) as exc:
        raise SystemExit(f'Server lokal tidak dapat dimulai (mungkin sudah aktif): {exc}')
