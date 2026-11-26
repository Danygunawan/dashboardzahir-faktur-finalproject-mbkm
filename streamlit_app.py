"""Entrypoint Streamlit Community Cloud yang menjalankan mesin ekstraksi asli dengan UI web_lokal/static."""
import base64
import gc
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import threading
import time
import traceback
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import streamlit as st
import streamlit.components.v1 as components

import batch_faktur
from batch_common import complete, digest, now, seal
from batch_faktur import OCRCache, ocr_config, run_batch
from uji_ocr_gambar import CONFIG, buat_segmen, pratinjau
from web_lokal.app import (
    DOWNLOADS,
    IMAGE_FORMATS,
    LIMITS,
    InvalidUpload,
    JobStore,
    MAX_BODY,
    MAX_FILE,
    OUTPUT,
    STATIC,
    TABLES,
    TERMINAL,
    parse_upload,
    validate_content,
    validate_selection,
)
from web_lokal.data import collect_data, data_csv_zip, data_excel_bytes
from web_lokal.downloads import excel_bytes, session_csv_zip, session_excel_bytes
from web_lokal.io import atomic_json, read_json

_FALLBACK_VERSIONS = {
    'paddleocr': '3.3.2',
    'paddlepaddle': '3.3.0',
    'paddlex': '3.3.13',
}
_orig_version = importlib.metadata.version


def _safe_pkg_version(name):
    try:
        return _orig_version(name)
    except importlib.metadata.PackageNotFoundError:
        if name in _FALLBACK_VERSIONS:
            return _FALLBACK_VERSIONS[name]
        raise


importlib.metadata.version = _safe_pkg_version

_CHUNK_LOCK = threading.Lock()
_UPLOAD_BUFFERS = {}


def _stage_single_file(store, kind, filename, payload):
    """Validasi dan simpan 1 file unggahan persis seperti parse_upload + store.stage."""
    if kind not in LIMITS or not filename:
        raise InvalidUpload('Pilih dokumen gambar atau PDF yang valid.')
    filename = PureWindowsPath(filename).name
    if (
        not filename
        or len(filename) > 180
        or filename in {'.', '..'}
        or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in filename)
        or filename.endswith((' ', '.'))
        or PureWindowsPath(filename).is_reserved()
    ):
        raise InvalidUpload('Nama file tidak valid; ganti nama lalu unggah kembali.')
    if not payload or len(payload) > MAX_FILE:
        raise InvalidUpload('Setiap file harus berisi data dan berukuran maksimal 25 MB.')
    suffix = Path(filename).suffix.lower()
    if (kind == 'pdf' and suffix != '.pdf') or (kind == 'gambar' and suffix not in IMAGE_FORMATS):
        raise InvalidUpload('Jenis file tidak sesuai: gunakan gambar yang didukung atau PDF.')
    validate_selection([kind])
    try:
        validate_content(payload, suffix)
    except InvalidUpload as exc:
        raise InvalidUpload(filename + ': ' + str(exc)) from exc
    return store.stage([(kind, filename, payload)])


class CloudJobStore(JobStore):
    """JobStore yang menjalankan pipeline batch_faktur asli dengan dukungan PaddleOCR maupun RapidOCR ONNX."""

    def __init__(self, root=OUTPUT):
        super().__init__(root=root, recover=True)
        self._shared_ocr_model = None
        self._shared_rapid_model = None
        self._shared_load_seconds = 0.0

    def _obtain_with_rapidocr(self, cache_self, path, h, page, meta, dpi=None):
        started = time.perf_counter()
        key = cache_self.key(h, page, dpi)
        for folder in sorted(cache_self.root.glob(key + '_*')):
            if complete(folder):
                d = read_json(folder / 'teks_koordinat_ocr.json')
                if cache_self.valid_data(d):
                    cache_self.stats.append({
                        'halaman': page,
                        'metode': 'cache_ocr',
                        'detik': time.perf_counter() - started,
                        'cache': str(folder),
                    })
                    return d, folder

        folder = cache_self.root / (key + '_' + uuid4().hex[:8])
        folder.mkdir()
        previous = cache_self.previous(h) if dpi is None and page == 1 else None
        if previous:
            src, d = previous
            for name in ['ocr_mentah.json', 'teks_koordinat_ocr.json', 'pratinjau_ocr.png']:
                shutil.copyfile(src.parent / name, folder / name)
            atomic_json(folder / 'asal_cache.json', {'input': str(src), 'sha256': digest(src), 'key': key, 'config': ocr_config()})
            method = 'impor_cache_ocr_valid'
        else:
            if self._shared_rapid_model is None:
                t = time.perf_counter()
                try:
                    from rapidocr import RapidOCR
                except ImportError:
                    from rapidocr_onnxruntime import RapidOCR
                self._shared_rapid_model = RapidOCR()
                self._shared_load_seconds += time.perf_counter() - t
            cache_self.load_seconds = self._shared_load_seconds

            from PIL import Image
            with Image.open(path) as im:
                if im.getexif().get(274, 1) != 1:
                    raise ValueError('Rotasi EXIF belum didukung')
                image = im.convert('RGB')
                w, hh = image.size

            t = time.perf_counter()
            out = self._shared_rapid_model(str(path))
            infer = time.perf_counter() - t

            texts, scores, polys = [], [], []
            if hasattr(out, 'boxes') and out.boxes is not None:
                raw_boxes = out.boxes.tolist() if hasattr(out.boxes, 'tolist') else list(out.boxes)
                raw_txts = list(out.txts) if out.txts is not None else []
                raw_scores = list(out.scores) if out.scores is not None else []
                for box, txt, sc in zip(raw_boxes, raw_txts, raw_scores):
                    polys.append([[float(pt[0]), float(pt[1])] for pt in box])
                    texts.append(str(txt))
                    scores.append(float(sc))
            elif isinstance(out, tuple) and out[0]:
                for item in out[0]:
                    box, txt, sc = item[0], item[1], item[2]
                    polys.append([[float(pt[0]), float(pt[1])] for pt in box])
                    texts.append(str(txt))
                    scores.append(float(sc))

            raw = {
                'rec_texts': texts,
                'rec_scores': scores,
                'rec_polys': polys,
                'dt_polys': polys,
            }
            atomic_json(folder / 'ocr_mentah.json', raw)
            segments = buat_segmen(raw, path, w, hh, 0.85)
            d = {
                'versi_skema': 1,
                'sumber_gambar': str(path),
                'sha256_sumber_sebelum': h,
                'sha256_sumber_sesudah': h,
                'inventaris': meta,
                'nomor_halaman': 1,
                'ukuran_gambar': {'lebar': w, 'tinggi': hh},
                'satuan_koordinat': 'piksel',
                'gambar_acuan_koordinat': str(path),
                'transformasi_aplikasi': {'jenis': 'identitas'},
                'konfigurasi': CONFIG,
                'versi_paket_utama': ocr_config()['paket'],
                'segmen': segments,
                'waktu_detik': {'inferensi': infer},
                'sumber_dokumen': meta['path_sumber'],
                'halaman_dokumen': page,
                'render_dpi': dpi,
            }
            atomic_json(folder / 'teks_koordinat_ocr.json', d)
            pratinjau(image, segments, folder / 'pratinjau_ocr.png')
            atomic_json(
                folder / 'asal_cache.json',
                {'key': key, 'config': ocr_config(), 'sha256_input_render': digest(path), 'inferensi_detik': infer},
            )
            method = 'ocr_baru'

        seal(folder)
        cache_self.stats.append({
            'halaman': page,
            'metode': method,
            'detik': time.perf_counter() - started,
            'cache': str(folder),
            'waktu_ocr_asal': d.get('waktu_detik'),
        })
        return d, folder

    def _run(self, folder):
        folder = Path(folder).resolve()
        state_path = folder / 'job.json'
        state = read_json(state_path)
        try:
            state.update(status='diproses', mulai=now(), pesan='Menyiapkan mesin ekstraksi.')
            atomic_json(state_path, state)
            manifest = read_json(folder / 'manifest.json')

            os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', 'True')
            os.environ.setdefault('PADDLE_PDX_MODEL_SOURCE', 'BOS')

            has_paddle = importlib.util.find_spec('paddleocr') is not None
            original_writer = batch_faktur.atomic_json
            original_init = OCRCache.__init__
            original_obtain = OCRCache.obtain
            store_ref = self
            holder = {'cache': None}

            def patched_init(cache_self, root_dir):
                original_init(cache_self, root_dir)
                if store_ref._shared_ocr_model is not None:
                    cache_self.model = store_ref._shared_ocr_model
                    cache_self.load_seconds = store_ref._shared_load_seconds
                holder['cache'] = cache_self

            def patched_obtain(cache_self, path, h, page, meta, dpi=None):
                if has_paddle:
                    return original_obtain(cache_self, path, h, page, meta, dpi)
                return store_ref._obtain_with_rapidocr(cache_self, path, h, page, meta, dpi)

            batch_faktur.atomic_json = atomic_json
            OCRCache.__init__ = patched_init
            OCRCache.obtain = patched_obtain
            try:
                run_batch(
                    folder / 'manifest.json',
                    run_dir=folder / 'run',
                    limit=len(manifest['dokumen']),
                    cache_dir=self.root / 'cache_ocr',
                    roi_mode='cache',
                )
            finally:
                if holder['cache'] is not None and holder['cache'].model is not None:
                    store_ref._shared_ocr_model = holder['cache'].model
                    store_ref._shared_load_seconds = holder['cache'].load_seconds
                OCRCache.__init__ = original_init
                OCRCache.obtain = original_obtain
                batch_faktur.atomic_json = original_writer

            progress = read_json(folder / 'run/progres.json')
            statuses = [d['status'] for d in progress['dokumen']]
            failures = statuses.count('gagal')
            if failures == len(statuses):
                status = 'gagal'
            elif failures:
                status = 'sebagian_gagal'
            elif 'perlu_review' in statuses:
                status = 'perlu_review'
            else:
                status = 'selesai'
            state.update(status=status, selesai=now(), pesan='Ekstraksi selesai; periksa hasil dan catatan review.')
            atomic_json(state_path, state)
        except Exception as exc:
            traceback.print_exc()
            state = read_json(state_path)
            if state.get('status') not in TERMINAL:
                state.update(status='gagal', selesai=now(), pesan=f'{type(exc).__name__}: {exc}')
                atomic_json(state_path, state)


def _execute_get(store, api_path, query_string=''):
    """Jalankan logika GET API web_lokal (termasuk upload_chunk dan create_job via GET)."""
    try:
        if api_path == '/api/health':
            return 200, {'status': 'ok', 'tahap': 1, 'v': 3}, 'application/json; charset=utf-8', None

        if api_path == '/api/upload_chunk':
            params = {k: v[0] for k, v in parse_qs(query_string, keep_blank_values=True).items()}
            uid = params.get('uid', '')
            if not re.fullmatch(r'[a-f0-9]{16,64}', uid):
                raise InvalidUpload('ID unggahan tidak valid.')
            action = params.get('action', 'chunk')
            if action == 'chunk':
                idx = int(params.get('idx', '0'))
                chunk_b64 = params.get('d', '')
                with _CHUNK_LOCK:
                    buf = _UPLOAD_BUFFERS.setdefault(uid, {})
                    buf[idx] = chunk_b64
                return 200, {'ok': True, 'idx': idx}, 'application/json; charset=utf-8', None
            if action == 'commit':
                total = int(params.get('total', '0'))
                kind = params.get('kind', 'gambar')
                name = params.get('name', '')
                with _CHUNK_LOCK:
                    buf = _UPLOAD_BUFFERS.pop(uid, {})
                if len(buf) != total or any(i not in buf for i in range(total)):
                    raise InvalidUpload('Unggahan tidak lengkap; coba pilih file kembali.')
                full_b64 = ''.join(buf[i] for i in range(total))
                payload = base64.b64decode(full_b64)
                meta = _stage_single_file(store, kind, name, payload)
                return 201, meta, 'application/json; charset=utf-8', None
            raise InvalidUpload('Aksi unggahan tidak dikenali.')

        if api_path == '/api/create_job':
            params = {k: v[0] for k, v in parse_qs(query_string, keep_blank_values=True).items()}
            raw_json = params.get('payload', '')
            try:
                payload = json.loads(raw_json)
            except Exception as exc:
                raise InvalidUpload('Permintaan konversi tidak valid.') from exc
            if not isinstance(payload, dict):
                raise InvalidUpload('Permintaan konversi tidak valid.')
            job_id = store.create_staged(payload)
            return 202, {'id': job_id, 'status_url': f'/api/jobs/{job_id}'}, 'application/json; charset=utf-8', None

        if api_path == '/api/data':
            return 200, collect_data(store.root), 'application/json; charset=utf-8', None
        if api_path in {'/api/data/download/data_faktur.xlsx', '/api/data/download/data_faktur_csv.zip'}:
            data = collect_data(store.root)
            if api_path.endswith('.xlsx'):
                return 200, data_excel_bytes(data), DOWNLOADS['hasil_faktur.xlsx'], 'data_faktur.xlsx'
            return 200, data_csv_zip(data), 'application/zip', 'data_faktur_csv.zip'
        match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(result|download/([^/]+)|source/(gambar|pdf)))?', api_path)
        if not match:
            raise FileNotFoundError('Halaman tidak ditemukan.')
        job_id, action, download, source = match.groups()
        if action is None:
            return 200, store.status(job_id), 'application/json; charset=utf-8', None
        if action == 'result':
            return 200, store.result(job_id), 'application/json; charset=utf-8', None
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
            return 200, body, DOWNLOADS[download], download
        uploads = read_json(folder / 'manifest.json')['dokumen']
        entry = next((e for e in uploads if Path(e['path_relatif']).parts[0] == source), None)
        if entry is None:
            raise FileNotFoundError('Berkas sumber tidak tersedia.')
        file = Path(entry['path_sumber']).resolve()
        if not file.is_relative_to((folder / 'unggahan').resolve()):
            raise FileNotFoundError('Berkas sumber tidak tersedia.')
        mime = 'application/pdf' if source == 'pdf' else 'image/' + IMAGE_FORMATS[file.suffix.lower()].lower()
        return 200, file.read_bytes(), mime, None
    except InvalidUpload as exc:
        code = 409 if api_path.endswith('/result') else 400
        return code, {'error': str(exc)}, 'application/json; charset=utf-8', None
    except FileNotFoundError as exc:
        return 404, {'error': str(exc)}, 'application/json; charset=utf-8', None
    except Exception as exc:
        return 500, {'error': f'Gagal membaca sesi: {exc}'}, 'application/json; charset=utf-8', None


def _execute_post(store, api_path, body, headers):
    """Jalankan logika POST API web_lokal dan kembalikan (status_code, body, content_type, filename)."""
    try:
        body = body or b''
        if not 0 < len(body) <= MAX_BODY:
            return 413, {'error': 'Permintaan terlalu besar. Unggah setiap file secara terpisah (maksimal 25 MB per file).'}, 'application/json; charset=utf-8', None
        x_local = headers.get('X-Local-Upload') or headers.get('x-local-upload')
        if x_local != '1':
            return 403, {'error': 'Unggah melalui halaman web lokal.'}, 'application/json; charset=utf-8', None
        if api_path not in {'/api/jobs', '/api/uploads'}:
            return 404, {'error': 'Endpoint tidak ditemukan.'}, 'application/json; charset=utf-8', None
        content_type = headers.get('Content-Type') or headers.get('content-type') or ''
        if api_path == '/api/uploads':
            files = parse_upload(content_type, body)
            return 201, store.stage(files), 'application/json; charset=utf-8', None
        if content_type.split(';')[0].strip().lower() == 'application/json':
            try:
                payload = json.loads(body.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise InvalidUpload('Permintaan konversi tidak valid.') from exc
            if not isinstance(payload, dict):
                raise InvalidUpload('Permintaan konversi tidak valid.')
            job_id = store.create_staged(payload)
        else:
            files = parse_upload(content_type, body)
            job_id = store.create(files)
        return 202, {'id': job_id, 'status_url': f'/api/jobs/{job_id}'}, 'application/json; charset=utf-8', None
    except InvalidUpload as exc:
        return 400, {'error': str(exc)}, 'application/json; charset=utf-8', None
    except Exception as exc:
        return 500, {'error': f'Gagal menyimpan unggahan: {exc}'}, 'application/json; charset=utf-8', None


def register_tornado_routes(store):
    """Pasang route API pada Starlette (Streamlit >= 1.60) maupun Tornado (Streamlit lama)."""
    registered = False

    try:
        import asyncio
        from starlette.applications import Starlette
        from starlette.responses import Response
        from starlette.routing import Route

        def _make_starlette_response(code, body, content_type='application/json; charset=utf-8', filename=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            headers = {
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, POST, PUT, OPTIONS, HEAD',
                'Access-Control-Allow-Headers': 'Content-Type, X-Local-Upload',
                'Access-Control-Expose-Headers': 'X-Zahir-Api, Content-Disposition',
                'X-Zahir-Api': '1',
                'Cache-Control': 'no-store',
                'X-Content-Type-Options': 'nosniff',
            }
            if filename:
                headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            return Response(content=body, status_code=code, media_type=content_type, headers=headers)

        async def zahir_starlette_endpoint(request):
            try:
                if request.method == 'OPTIONS':
                    return _make_starlette_response(204, b'', 'text/plain')
                subpath = request.path_params.get('api_path', '').lstrip('/')
                api_path = '/api/' + subpath
                query_str = request.url.query or ''
                if request.method in {'GET', 'HEAD'}:
                    code, body, ct, fname = await asyncio.to_thread(_execute_get, store, api_path, query_str)
                    return _make_starlette_response(code, body, ct, fname)
                if request.method in {'POST', 'PUT'}:
                    raw_body = await request.body()
                    code, body, ct, fname = await asyncio.to_thread(_execute_post, store, api_path, raw_body, request.headers)
                    return _make_starlette_response(code, body, ct, fname)
                return _make_starlette_response(405, {'error': 'Method not allowed'})
            except Exception as exc:
                return _make_starlette_response(500, {'error': f'Server error: {exc}'})

        starlette_apps = [obj for obj in gc.get_objects() if isinstance(obj, Starlette)]
        for app in starlette_apps:
            app.router.routes[:] = [
                r for r in app.router.routes
                if not getattr(r, '_is_zahir_route', False) and 'zahir_api' not in getattr(r, 'path', '')
            ]
            candidate_prefixes = {
                '/_stcore/upload_file/zahir_api',
                '/component/zahir_api',
                '/media/zahir_api',
                '/_stcore/zahir_api',
                '/api',
            }
            for existing_route in list(app.router.routes):
                rpath = getattr(existing_route, 'path', '')
                if '/{' in rpath:
                    base_prefix = rpath.split('/{', 1)[0].rstrip('/')
                    if base_prefix:
                        candidate_prefixes.add(f'{base_prefix}/zahir_api')
            for prefix in sorted(candidate_prefixes):
                new_route = Route(
                    f'{prefix}/{{api_path:path}}',
                    zahir_starlette_endpoint,
                    methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS', 'HEAD'],
                )
                new_route._is_zahir_route = True
                app.router.routes.insert(0, new_route)
            registered = True
    except Exception:
        pass

    return registered


@st.cache_resource
def get_runtime():
    store = CloudJobStore(OUTPUT)
    register_tornado_routes(store)
    return store


def detect_server_origin():
    """Ambil origin server publik dari header request Streamlit bila tersedia."""
    try:
        headers = st.context.headers
        if headers:
            host = headers.get('X-Forwarded-Host') or headers.get('Host') or headers.get('host') or ''
            proto = headers.get('X-Forwarded-Proto') or ('http' if host.startswith(('localhost', '127.0.0.1')) else 'https')
            if host:
                return f'{proto}://{host}'
    except Exception:
        pass
    return ''


def build_embedded_html(store):
    """Bangun HTML mandiri dari web_lokal/static tanpa mengubah berkas sumber."""
    register_tornado_routes(store)
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    css = (STATIC / 'style.css').read_text(encoding='utf-8')
    theme_js = (STATIC / 'theme.js').read_text(encoding='utf-8')
    app_js = (STATIC / 'app.js').read_text(encoding='utf-8')
    logo_b64 = base64.b64encode((STATIC / 'zahir-one.png').read_bytes()).decode('ascii')
    font_b64 = base64.b64encode((STATIC / 'InterVariable.woff2').read_bytes()).decode('ascii')

    css = css.replace("url('/InterVariable.woff2')", f"url('data:font/woff2;base64,{font_b64}')")
    server_origin_json = json.dumps(detect_server_origin())

    bridge_js = f"""
(() => {{
  const SERVER_ORIGIN = {server_origin_json};

  try {{
    const origReplace = history.replaceState.bind(history);
    history.replaceState = function(...args) {{
      try {{ return origReplace(...args); }} catch (_) {{}}
    }};
    const origPush = history.pushState.bind(history);
    history.pushState = function(...args) {{
      try {{ return origPush(...args); }} catch (_) {{}}
    }};
  }} catch (_) {{}}

  function resolveOrigins() {{
    const origins = [];
    if (SERVER_ORIGIN) origins.push(SERVER_ORIGIN.replace(/\\/+$/, ''));
    try {{
      if (window.location.ancestorOrigins && window.location.ancestorOrigins.length > 0) {{
        for (let i = 0; i < window.location.ancestorOrigins.length; i++) {{
          const ao = window.location.ancestorOrigins[i];
          if (ao && ao !== 'null' && !origins.includes(ao)) origins.push(ao.replace(/\\/+$/, ''));
        }}
      }}
    }} catch (_) {{}}
    try {{
      if (window.parent && window.parent.location && window.parent.location.origin && window.parent.location.origin !== 'null') {{
        const po = window.parent.location.origin.replace(/\\/+$/, '');
        if (!origins.includes(po)) origins.push(po);
      }}
    }} catch (_) {{}}
    if (document.referrer) {{
      try {{
        const ref = new URL(document.referrer).origin.replace(/\\/+$/, '');
        if (ref && ref !== 'null' && !origins.includes(ref)) origins.push(ref);
      }} catch (_) {{}}
    }}
    if (window.location.origin && window.location.origin !== 'null' && !window.location.href.startsWith('about:')) {{
      const lo = window.location.origin.replace(/\\/+$/, '');
      if (!origins.includes(lo)) origins.push(lo);
    }}
    return origins;
  }}

  function randomHex32() {{
    const arr = new Uint8Array(16);
    crypto.getRandomValues(arr);
    return Array.from(arr, b => b.toString(16).padStart(2, '0')).join('');
  }}

  function arrayBufferToBase64(buffer) {{
    const bytes = new Uint8Array(buffer);
    let binary = '';
    const step = 0x8000;
    for (let i = 0; i < bytes.length; i += step) {{
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + step));
    }}
    return btoa(binary);
  }}

  let activeGetPrefix = null;
  const origFetch = window.fetch.bind(window);

  async function callGetBackend(suffixWithQuery) {{
    if (activeGetPrefix) {{
      try {{
        const resp = await origFetch(activeGetPrefix + suffixWithQuery, {{ method: 'GET' }});
        const ct = resp.headers.get('content-type') || '';
        const isZahir = resp.headers.get('x-zahir-api') === '1';
        if (isZahir || ct.includes('application/json') || ct.includes('application/zip') || ct.includes('spreadsheetml')) {{
          return resp;
        }}
      }} catch (_) {{}}
    }}
    const origins = resolveOrigins();
    const prefixes = [];
    for (const origin of origins) {{
      prefixes.push(`${{origin}}/~/+/component/zahir_api`);
      prefixes.push(`${{origin}}/component/zahir_api`);
      prefixes.push(`${{origin}}/~/+/media/zahir_api`);
      prefixes.push(`${{origin}}/media/zahir_api`);
      prefixes.push(`${{origin}}/api`);
    }}
    for (const prefix of prefixes) {{
      try {{
        const resp = await origFetch(prefix + suffixWithQuery, {{ method: 'GET' }});
        const ct = resp.headers.get('content-type') || '';
        const isZahir = resp.headers.get('x-zahir-api') === '1';
        if (isZahir || ct.includes('application/json') || ct.includes('application/zip') || ct.includes('spreadsheetml')) {{
          activeGetPrefix = prefix;
          return resp;
        }}
      }} catch (_) {{}}
    }}
    throw new Error('Tidak dapat menghubungi server ekstraksi backend.');
  }}

  async function uploadViaGetChunks(formData) {{
    let kind = 'gambar', file = null;
    for (const [k, v] of formData.entries()) {{
      kind = k;
      file = v;
      break;
    }}
    if (!file) throw new Error('Pilih dokumen gambar atau PDF yang valid.');
    const buf = await file.arrayBuffer();
    const b64 = arrayBufferToBase64(buf);
    const chunkSize = 6000;
    const chunks = [];
    for (let i = 0; i < b64.length; i += chunkSize) {{
      chunks.push(b64.slice(i, i + chunkSize));
    }}
    if (chunks.length === 0) chunks.push('');
    const uid = randomHex32();
    const batchSize = 6;
    for (let i = 0; i < chunks.length; i += batchSize) {{
      const slice = chunks.slice(i, i + batchSize);
      await Promise.all(slice.map((c, offset) => {{
        const idx = i + offset;
        const q = `/upload_chunk?action=chunk&uid=${{uid}}&idx=${{idx}}&d=${{encodeURIComponent(c)}}`;
        return callGetBackend(q);
      }}));
    }}
    const commitQ = `/upload_chunk?action=commit&uid=${{uid}}&total=${{chunks.length}}&kind=${{encodeURIComponent(kind)}}&name=${{encodeURIComponent(file.name)}}`;
    return callGetBackend(commitQ);
  }}

  window.fetch = async (input, options = {{}}) => {{
    const url = typeof input === 'string' ? input : input.url;
    if (typeof url === 'string' && url.startsWith('/api/')) {{
      const method = (options.method || 'GET').toUpperCase();
      const suffix = url.slice(4);
      if (method === 'POST' && url === '/api/uploads' && options.body instanceof FormData) {{
        return uploadViaGetChunks(options.body);
      }}
      if (method === 'POST' && url === '/api/jobs') {{
        const payloadStr = typeof options.body === 'string' ? options.body : JSON.stringify(options.body || {{}});
        return callGetBackend(`/create_job?payload=${{encodeURIComponent(payloadStr)}}`);
      }}
      return callGetBackend(suffix);
    }}
    return origFetch(input, options);
  }};

  document.addEventListener('click', async (event) => {{
    const link = event.target.closest('a');
    if (!link) return;
    const rawHref = link.getAttribute('href') || '';
    if (!rawHref.startsWith('/api/')) return;
    event.preventDefault();
    try {{
      const resp = await callGetBackend(rawHref.slice(4));
      if (!resp.ok) throw new Error(`Gagal mengunduh (${{resp.status}})`);
      const blob = await resp.blob();
      const blobUrl = URL.createObjectURL(blob);
      const temp = document.createElement('a');
      temp.href = blobUrl;
      temp.download = link.getAttribute('download') || rawHref.split('/').pop() || 'hasil_faktur.xlsx';
      document.body.appendChild(temp);
      temp.click();
      temp.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 15000);
    }} catch (err) {{
      const errEl = document.getElementById('app-error');
      if (errEl) {{
        errEl.textContent = err.message || 'Gagal mengunduh berkas.';
        errEl.hidden = false;
      }}
    }}
  }});
}})();
"""

    html = html.replace('<link rel="stylesheet" href="/style.css">', f'<style>{css}</style>')
    html = html.replace('<script src="/theme.js"></script>', f'<script>{theme_js}</script>')
    html = html.replace('src="/zahir-one.png"', f'src="data:image/png;base64,{logo_b64}"')
    html = html.replace('<script src="/app.js" defer></script>', '')
    html = html.replace('</body>', f'<script>{bridge_js}\n{app_js}</script>\n</body>')
    return html


def main():
    st.set_page_config(
        page_title='Ekstraksi Faktur | Zahir ONE',
        layout='wide',
        initial_sidebar_state='collapsed',
    )
    st.markdown(
        """
        <style>
        #MainMenu, header[data-testid="stHeader"], footer, div[data-testid="stToolbar"],
        div[data-testid="stDecoration"], div[data-testid="stStatusWidget"] {
            display: none !important;
        }
        .stApp, .main, .block-container, div[data-testid="stAppViewContainer"],
        section[data-testid="stMain"], div[data-testid="stMainBlockContainer"] {
            padding: 0 !important;
            margin: 0 !important;
            max-width: 100% !important;
            width: 100vw !important;
            height: 100vh !important;
            overflow: hidden !important;
        }
        iframe {
            position: fixed !important;
            top: 0 !important;
            left: 0 !important;
            width: 100vw !important;
            height: 100vh !important;
            border: none !important;
            margin: 0 !important;
            padding: 0 !important;
            z-index: 999999 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    store = get_runtime()
    register_tornado_routes(store)
    components.html(build_embedded_html(store), height=900, scrolling=True)


if __name__ == '__main__':
    main()
