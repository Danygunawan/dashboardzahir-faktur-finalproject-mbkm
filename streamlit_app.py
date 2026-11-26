"""Entrypoint Streamlit Community Cloud yang menjalankan mesin ekstraksi asli dengan UI web_lokal/static."""
import base64
import gc
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit
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
    InvalidUpload,
    JobStore,
    MAX_BODY,
    OUTPUT,
    STATIC,
    TABLES,
    TERMINAL,
    parse_upload,
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


def register_tornado_routes(store):
    """Pasang handler API langsung pada ComponentRequestHandler dan router Tornado Streamlit."""
    def handle_get(handler, api_path):
        try:
            if api_path == '/api/health':
                return handler._zahir_send(200, {'status': 'ok', 'tahap': 1})
            if api_path == '/api/data':
                return handler._zahir_send(200, collect_data(store.root))
            if api_path in {'/api/data/download/data_faktur.xlsx', '/api/data/download/data_faktur_csv.zip'}:
                data = collect_data(store.root)
                if api_path.endswith('.xlsx'):
                    return handler._zahir_send(200, data_excel_bytes(data), DOWNLOADS['hasil_faktur.xlsx'], 'data_faktur.xlsx')
                return handler._zahir_send(200, data_csv_zip(data), 'application/zip', 'data_faktur_csv.zip')
            match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(result|download/([^/]+)|source/(gambar|pdf)))?', api_path)
            if not match:
                raise FileNotFoundError('Halaman tidak ditemukan.')
            job_id, action, download, source = match.groups()
            if action is None:
                return handler._zahir_send(200, store.status(job_id))
            if action == 'result':
                return handler._zahir_send(200, store.result(job_id))
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
                return handler._zahir_send(200, body, DOWNLOADS[download], download)
            uploads = read_json(folder / 'manifest.json')['dokumen']
            entry = next((e for e in uploads if Path(e['path_relatif']).parts[0] == source), None)
            if entry is None:
                raise FileNotFoundError('Berkas sumber tidak tersedia.')
            file = Path(entry['path_sumber']).resolve()
            if not file.is_relative_to((folder / 'unggahan').resolve()):
                raise FileNotFoundError('Berkas sumber tidak tersedia.')
            mime = 'application/pdf' if source == 'pdf' else 'image/' + IMAGE_FORMATS[file.suffix.lower()].lower()
            return handler._zahir_send(200, file.read_bytes(), mime)
        except FileNotFoundError as exc:
            return handler._zahir_send(404, {'error': str(exc)})
        except InvalidUpload as exc:
            return handler._zahir_send(409, {'error': str(exc)})
        except Exception as exc:
            return handler._zahir_send(500, {'error': f'Gagal membaca sesi: {exc}'})

    def handle_post(handler, api_path):
        try:
            body = handler.request.body or b''
            if not 0 < len(body) <= MAX_BODY:
                return handler._zahir_send(413, {'error': 'Permintaan terlalu besar. Unggah setiap file secara terpisah (maksimal 25 MB per file).'})
            if handler.request.headers.get('X-Local-Upload') != '1':
                return handler._zahir_send(403, {'error': 'Unggah melalui halaman web lokal.'})
            if api_path not in {'/api/jobs', '/api/uploads'}:
                return handler._zahir_send(404, {'error': 'Endpoint tidak ditemukan.'})
            content_type = handler.request.headers.get('Content-Type', '')
            if api_path == '/api/uploads':
                files = parse_upload(content_type, body)
                return handler._zahir_send(201, store.stage(files))
            if content_type.split(';')[0] == 'application/json':
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
            return handler._zahir_send(202, {'id': job_id, 'status_url': f'/api/jobs/{job_id}'})
        except InvalidUpload as exc:
            return handler._zahir_send(400, {'error': str(exc)})
        except Exception as exc:
            return handler._zahir_send(500, {'error': f'Gagal menyimpan unggahan: {exc}'})

    def zahir_send(self, code, body, content_type='application/json; charset=utf-8', filename=None):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.set_status(code)
        self.set_header('Access-Control-Allow-Origin', '*')
        self.set_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.set_header('Access-Control-Allow-Headers', 'Content-Type, X-Local-Upload')
        self.set_header('Content-Type', content_type)
        self.set_header('Cache-Control', 'no-store')
        self.set_header('X-Content-Type-Options', 'nosniff')
        if filename:
            self.set_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.finish(body)

    try:
        from streamlit.web.server.component_request_handler import ComponentRequestHandler
        if not getattr(ComponentRequestHandler, '_zahir_patched', False):
            orig_get = ComponentRequestHandler.get
            orig_xsrf = getattr(ComponentRequestHandler, 'check_xsrf_cookie', None)

            def comp_check_xsrf(self):
                if 'zahir_api/' in (self.request.uri or ''):
                    return
                if orig_xsrf:
                    return orig_xsrf(self)

            def comp_options(self, path=''):
                if str(path).startswith('zahir_api/'):
                    self.set_status(204)
                    self.set_header('Access-Control-Allow-Origin', '*')
                    self.set_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                    self.set_header('Access-Control-Allow-Headers', 'Content-Type, X-Local-Upload')
                    self.finish()
                    return
                self.set_status(204)
                self.finish()

            def comp_get(self, path=''):
                if str(path).startswith('zahir_api/'):
                    return handle_get(self, '/api/' + str(path)[len('zahir_api/'):])
                return orig_get(self, path)

            def comp_post(self, path=''):
                if str(path).startswith('zahir_api/'):
                    return handle_post(self, '/api/' + str(path)[len('zahir_api/'):])
                self.set_status(405)
                self.finish()

            ComponentRequestHandler._zahir_send = zahir_send
            ComponentRequestHandler.check_xsrf_cookie = comp_check_xsrf
            ComponentRequestHandler.options = comp_options
            ComponentRequestHandler.get = comp_get
            ComponentRequestHandler.post = comp_post
            ComponentRequestHandler._zahir_patched = True
    except Exception:
        pass
    return True


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

  // Cegah SecurityError saat history.replaceState dipanggil di dalam iframe srcdoc
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

  let activePrefix = null;
  const origFetch = window.fetch.bind(window);

  async function callBackend(apiPath, options) {{
    const suffix = apiPath.startsWith('/api/') ? apiPath.slice(4) : apiPath;
    if (activePrefix) {{
      try {{
        const resp = await origFetch(activePrefix + suffix, options);
        const ct = resp.headers.get('content-type') || '';
        if (!ct.includes('text/html')) return resp;
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
        const resp = await origFetch(prefix + suffix, options);
        const ct = resp.headers.get('content-type') || '';
        if (!ct.includes('text/html')) {{
          activePrefix = prefix;
          return resp;
        }}
      }} catch (_) {{}}
    }}
    throw new Error('Tidak dapat menghubungi server ekstraksi backend.');
  }}

  window.fetch = async (input, options) => {{
    const url = typeof input === 'string' ? input : input.url;
    if (typeof url === 'string' && url.startsWith('/api/')) {{
      return callBackend(url, options);
    }}
    return origFetch(input, options);
  }};

  // Tangani klik tautan unduhan (<a download>) agar mengunduh lewat Blob dari backend
  document.addEventListener('click', async (event) => {{
    const link = event.target.closest('a');
    if (!link) return;
    const rawHref = link.getAttribute('href') || '';
    if (!rawHref.startsWith('/api/')) return;
    event.preventDefault();
    try {{
      const resp = await callBackend(rawHref, {{ method: 'GET' }});
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
    components.html(build_embedded_html(store), height=900, scrolling=True)


if __name__ == '__main__':
    main()
