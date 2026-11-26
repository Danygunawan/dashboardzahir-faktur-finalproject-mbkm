"""Entrypoint Streamlit Community Cloud yang menjalankan mesin ekstraksi asli dengan UI web_lokal/static."""
import base64
import gc
import json
import os
import re
import traceback
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st
import streamlit.components.v1 as components

import batch_faktur
from batch_common import now
from batch_faktur import OCRCache, run_batch
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


class CloudJobStore(JobStore):
    """JobStore yang menjalankan pipeline batch_faktur asli dengan model OCR ter-cache di memori."""

    def __init__(self, root=OUTPUT):
        super().__init__(root=root, recover=True)
        self._shared_ocr_model = None
        self._shared_load_seconds = 0.0

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

            original_writer = batch_faktur.atomic_json
            original_init = OCRCache.__init__
            store_ref = self
            holder = {'cache': None}

            def patched_init(cache_self, root_dir):
                original_init(cache_self, root_dir)
                if store_ref._shared_ocr_model is not None:
                    cache_self.model = store_ref._shared_ocr_model
                    cache_self.load_seconds = store_ref._shared_load_seconds
                holder['cache'] = cache_self

            batch_faktur.atomic_json = atomic_json
            OCRCache.__init__ = patched_init
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
    """Daftarkan endpoint API pada router utama Tornado sebelum ComponentRequestHandler."""
    try:
        from tornado.routing import PathMatches, Rule
        from tornado.web import Application, RequestHandler
    except Exception:
        return False

    class ZahirAPIHandler(RequestHandler):
        def check_xsrf_cookie(self):
            pass

        def set_default_headers(self):
            self.set_header('Access-Control-Allow-Origin', '*')
            self.set_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.set_header('Access-Control-Allow-Headers', 'Content-Type, X-Local-Upload')

        def options(self, *_args):
            self.set_status(204)
            self.finish()

        def _send(self, code, body, content_type='application/json; charset=utf-8', filename=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.set_status(code)
            self.set_header('Content-Type', content_type)
            self.set_header('Cache-Control', 'no-store')
            self.set_header('X-Content-Type-Options', 'nosniff')
            if filename:
                self.set_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.finish(body)

        def _normalized_path(self):
            raw_path = urlsplit(self.request.uri).path
            match = re.search(r'(?:/component/zahir_api|/media/zahir_api|/_stcore/zahir_api|/api)(/.*)$', raw_path)
            if match:
                return '/api' + match.group(1)
            return raw_path

        def get(self, *_args):
            path = self._normalized_path()
            try:
                if path == '/api/health':
                    return self._send(200, {'status': 'ok', 'tahap': 1})
                if path == '/api/data':
                    return self._send(200, collect_data(store.root))
                if path in {'/api/data/download/data_faktur.xlsx', '/api/data/download/data_faktur_csv.zip'}:
                    data = collect_data(store.root)
                    if path.endswith('.xlsx'):
                        return self._send(200, data_excel_bytes(data), DOWNLOADS['hasil_faktur.xlsx'], 'data_faktur.xlsx')
                    return self._send(200, data_csv_zip(data), 'application/zip', 'data_faktur_csv.zip')
                match = re.fullmatch(r'/api/jobs/([a-f0-9]{32})(?:/(result|download/([^/]+)|source/(gambar|pdf)))?', path)
                if not match:
                    raise FileNotFoundError('Halaman tidak ditemukan.')
                job_id, action, download, source = match.groups()
                if action is None:
                    return self._send(200, store.status(job_id))
                if action == 'result':
                    return self._send(200, store.result(job_id))
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
                    return self._send(200, body, DOWNLOADS[download], download)
                uploads = read_json(folder / 'manifest.json')['dokumen']
                entry = next((e for e in uploads if Path(e['path_relatif']).parts[0] == source), None)
                if entry is None:
                    raise FileNotFoundError('Berkas sumber tidak tersedia.')
                file = Path(entry['path_sumber']).resolve()
                if not file.is_relative_to((folder / 'unggahan').resolve()):
                    raise FileNotFoundError('Berkas sumber tidak tersedia.')
                mime = 'application/pdf' if source == 'pdf' else 'image/' + IMAGE_FORMATS[file.suffix.lower()].lower()
                return self._send(200, file.read_bytes(), mime)
            except FileNotFoundError as exc:
                return self._send(404, {'error': str(exc)})
            except InvalidUpload as exc:
                return self._send(409, {'error': str(exc)})
            except Exception as exc:
                return self._send(500, {'error': f'Gagal membaca sesi: {exc}'})

        def post(self, *_args):
            path = self._normalized_path()
            try:
                body = self.request.body or b''
                if not 0 < len(body) <= MAX_BODY:
                    return self._send(413, {'error': 'Permintaan terlalu besar. Unggah setiap file secara terpisah (maksimal 25 MB per file).'})
                if self.request.headers.get('X-Local-Upload') != '1':
                    return self._send(403, {'error': 'Unggah melalui halaman web lokal.'})
                if path not in {'/api/jobs', '/api/uploads'}:
                    return self._send(404, {'error': 'Endpoint tidak ditemukan.'})
                content_type = self.request.headers.get('Content-Type', '')
                if path == '/api/uploads':
                    files = parse_upload(content_type, body)
                    return self._send(201, store.stage(files))
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
                return self._send(202, {'id': job_id, 'status_url': f'/api/jobs/{job_id}'})
            except InvalidUpload as exc:
                return self._send(400, {'error': str(exc)})
            except Exception as exc:
                return self._send(500, {'error': f'Gagal menyimpan unggahan: {exc}'})

    apps = [obj for obj in gc.get_objects() if isinstance(obj, Application)]
    if not apps:
        return False
    pattern = r'^.*(?:/component/zahir_api|/media/zahir_api|/_stcore/zahir_api|/api)/(.*)$'
    for app in apps:
        if not getattr(app, '_zahir_routes_v2', False):
            rule = Rule(PathMatches(pattern), ZahirAPIHandler)
            if hasattr(app, 'wildcard_router') and hasattr(app.wildcard_router, 'rules'):
                app.wildcard_router.rules.insert(0, rule)
            if hasattr(app, 'default_router') and hasattr(app.default_router, 'rules'):
                for top_rule in app.default_router.rules:
                    target = getattr(top_rule, 'target', None)
                    if hasattr(target, 'rules'):
                        target.rules.insert(0, rule)
            app._zahir_routes_v2 = True
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
