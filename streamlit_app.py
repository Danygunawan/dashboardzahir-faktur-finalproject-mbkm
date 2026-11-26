"""Entrypoint Streamlit Community Cloud yang menampilkan UI web_lokal/static secara utuh."""
import base64
import csv
import gc
import importlib.util
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import streamlit as st
import streamlit.components.v1 as components

from baca_pdf_digital import ekstrak_halaman, kategori_judul
from batch_common import digest, now, seal
from ekstrak_faktur_digital import DETAIL, FAKTUR, ekstrak_dokumen
from tests.support import SOURCES, make_session
from web_lokal.app import (
    DOWNLOADS,
    IMAGE_FORMATS,
    InvalidUpload,
    JobStore,
    MAX_BODY,
    OUTPUT,
    PUBLIC_DOWNLOADS,
    STATIC,
    TABLES,
    parse_upload,
)
from web_lokal.data import collect_data, data_csv_zip, data_excel_bytes
from web_lokal.downloads import excel_bytes, session_csv_zip, session_excel_bytes
from web_lokal.io import atomic_json, read_json


class CloudJobStore(JobStore):
    """JobStore yang mendukung lingkungan cloud ringan tanpa mengubah mesin ekstraksi asli."""

    def __init__(self, root=OUTPUT):
        super().__init__(root=root, recover=True)
        existing = list((self.root / 'jobs').glob('*/job.json'))
        if not existing:
            for index, session_id in enumerate(SOURCES):
                if not (self.root / 'jobs' / session_id).exists():
                    make_session(self.root, session_id, partial=index == 0)

    def _run(self, folder):
        if importlib.util.find_spec('paddleocr') is not None:
            return super()._run(folder)
        self._run_cloud_lightweight(folder)

    def _run_cloud_lightweight(self, folder):
        import pymupdf

        state_path = folder / 'job.json'
        state = read_json(state_path)
        try:
            state.update(status='diproses', mulai=now(), pesan='Menyiapkan mesin ekstraksi.')
            atomic_json(state_path, state)
            manifest = read_json(folder / 'manifest.json')
            entries = manifest.get('dokumen', [])
            progress_docs = [
                {'nama_file': e['nama_file'], 'status': 'menunggu', 'waktu_mulai': None, 'waktu_selesai': None, 'error': None}
                for e in entries
            ]
            atomic_json(folder / 'run/progres.json', {'dokumen': progress_docs})

            invoices, details, evidence, reviews, doc_summaries = [], [], [], [], []
            for idx, entry in enumerate(entries):
                progress_docs[idx].update(status='diproses', waktu_mulai=now())
                atomic_json(folder / 'run/progres.json', {'dokumen': progress_docs})

                source = Path(entry['path_sumber'])
                h = digest(source)
                suffix = source.suffix.lower()
                extracted_any = False

                if suffix == '.pdf':
                    with pymupdf.open(source) as pdf:
                        for page_idx, page in enumerate(pdf):
                            words = page.get_text('words')
                            if len(words) >= 20 and sum(any(c.isalpha() for c in w[4]) for w in words) >= 5:
                                pg = ekstrak_halaman(page, source, folder / 'run', 144)
                                cat, _, _ = kategori_judul(pg['baris'], pg['ukuran_halaman_pt']['tinggi'])
                                pg['kategori_sementara'] = cat
                                payload = {
                                    'sha256_sumber_sebelum': h,
                                    'sha256_sumber_sesudah': h,
                                    'hasil_program': {
                                        'folder_klien': entry.get('folder_klien', 'unggahan_web'),
                                        'nama_file': entry['nama_file'],
                                        'path_relatif': entry['path_relatif'],
                                        'path_sumber': str(source),
                                        'jumlah_halaman': len(pdf),
                                        'halaman': [pg],
                                    },
                                }
                                inv_list, _ = ekstrak_dokumen(payload)
                                for inv in inv_list:
                                    extracted_any = True
                                    row = dict.fromkeys(FAKTUR, '')
                                    row.update({k: str(v) if v is not None else '' for k, v in inv['nilai'].items() if k in row})
                                    if not row.get('id_faktur'):
                                        row['id_faktur'] = f'FKT_PDF_{h[:12]}_{page_idx + 1}'
                                    row['file_sumber'] = entry['nama_file']
                                    invoices.append(row)
                                    items_ev = []
                                    for item_idx, item in enumerate(inv.get('item', []), 1):
                                        d_row = dict.fromkeys(DETAIL, '')
                                        d_row.update({k: str(v) if v is not None else '' for k, v in item['nilai'].items() if k in d_row})
                                        d_row['id_faktur'] = row['id_faktur']
                                        if not d_row.get('nomor_baris_item'):
                                            d_row['nomor_baris_item'] = str(item_idx)
                                        details.append(d_row)
                                        items_ev.append({'nilai': d_row, 'kolom': {}})
                                    evidence.append({'nilai': row, 'item': items_ev, 'kolom': {}, 'hash_sumber': h, 'validasi': []})

                if not extracted_any:
                    inv_id = f'FKT_CLD_{h[:12]}_{idx + 1:02d}'
                    row = dict.fromkeys(FAKTUR, '')
                    row.update(
                        id_faktur=inv_id,
                        nomor_invoice=f'AUTO-{h[:6].upper()}',
                        tanggal_invoice=now()[:10],
                        nama_penerbit='PT. ZAHIR INTERNASIONAL',
                        nama_pelanggan=Path(entry['nama_file']).stem,
                        subtotal_faktur='0',
                        total_sebelum_pajak='0',
                        diskon_final_persen='0',
                        diskon_final_nominal='0',
                        pajak_faktur='0',
                        total_akhir='0',
                        folder_klien='unggahan_web',
                        file_sumber=entry['nama_file'],
                        halaman_sumber='1',
                    )
                    if 'status_validasi' in row:
                        row['status_validasi'] = 'perlu_review'
                    if 'catatan_validasi' in row:
                        row['catatan_validasi'] = 'Diproses pada mode cloud ringan; verifikasi nilai OCR penuh diperlukan.'
                    invoices.append(row)
                    d_row = dict.fromkeys(DETAIL, '')
                    d_row.update(
                        id_faktur=inv_id,
                        nomor_baris_item='1',
                        nama_barang=f"Dokumen {entry['nama_file']}",
                        jumlah='1',
                        unit='Dokumen',
                        harga_satuan='0',
                        diskon_item_persen='0',
                        subtotal_item='0',
                        halaman_sumber='1',
                    )
                    if 'status_validasi' in d_row:
                        d_row['status_validasi'] = 'perlu_review'
                    details.append(d_row)
                    evidence.append({'nilai': row, 'item': [{'nilai': d_row, 'kolom': {}}], 'kolom': {}, 'hash_sumber': h, 'validasi': []})
                    reviews.append({
                        'prioritas': 3,
                        'id_faktur': inv_id,
                        'nomor_item': '',
                        'jenis': 'cloud_review',
                        'kolom': 'nomor_invoice',
                        'pesan': f"{entry['nama_file']}: Diproses tanpa engine PaddleOCR penuh di container cloud.",
                    })

                progress_docs[idx].update(status='perlu_review', waktu_selesai=now())
                doc_summaries.append({'sumber': entry['nama_file'], 'status': 'perlu_review', 'halaman': [], 'kendala': []})
                atomic_json(folder / 'run/progres.json', {'dokumen': progress_docs})

            export = folder / 'run/ekspor/cloud_1'
            export.mkdir(parents=True, exist_ok=True)
            review_fields = ['prioritas', 'id_faktur', 'nomor_item', 'jenis', 'kolom', 'pesan']
            for filename, fields, rows in [
                ('faktur.csv', FAKTUR, invoices),
                ('detail_faktur.csv', DETAIL, details),
                ('daftar_review.csv', review_fields, reviews),
            ]:
                with (export / filename).open('w', encoding='utf-8-sig', newline='') as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(rows)

            atomic_json(
                export / 'bukti_ekstraksi.json',
                {'asal': 'cloud_web_lokal', 'faktur': evidence, 'review': reviews, 'dokumen': doc_summaries},
            )
            seal(export)
            atomic_json(
                folder / 'run/progres.json',
                {'dokumen': progress_docs, 'ekspor_terakhir': str(export.resolve())},
            )
            state.update(
                status='perlu_review' if reviews else 'selesai',
                selesai=now(),
                pesan='Ekstraksi selesai; periksa hasil dan catatan review.',
            )
            atomic_json(state_path, state)
        except Exception as exc:
            state.update(status='gagal', selesai=now(), pesan=f'{type(exc).__name__}: {exc}')
            atomic_json(state_path, state)


def register_tornado_routes(store):
    """Daftarkan endpoint API web_lokal langsung pada server Tornado milik Streamlit."""
    try:
        from tornado.routing import PathMatches, Rule
        from tornado.web import Application, RequestHandler
    except Exception:
        return False

    class ZahirAPIHandler(RequestHandler):
        def check_xsrf_cookie(self):
            pass

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
            for prefix in ('/_stcore/zahir_api', '/component/zahir_api'):
                if raw_path.startswith(prefix):
                    return '/api' + raw_path[len(prefix):]
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
    pattern = r'^(?:/_stcore/zahir_api|/component/zahir_api|/api)/(.*)$'
    for app in apps:
        if not getattr(app, '_zahir_routes_installed', False):
            rule = Rule(PathMatches(pattern), ZahirAPIHandler)
            if hasattr(app, 'wildcard_router'):
                app.wildcard_router.rules.insert(0, rule)
            if hasattr(app, 'default_router'):
                app.default_router.rules.insert(0, rule)
            app._zahir_routes_installed = True
    return True


@st.cache_resource
def get_runtime():
    store = CloudJobStore(OUTPUT)
    register_tornado_routes(store)
    return store


def build_embedded_html(store):
    """Bangun HTML mandiri dari web_lokal/static tanpa mengubah berkas sumber."""
    html = (STATIC / 'index.html').read_text(encoding='utf-8')
    css = (STATIC / 'style.css').read_text(encoding='utf-8')
    theme_js = (STATIC / 'theme.js').read_text(encoding='utf-8')
    app_js = (STATIC / 'app.js').read_text(encoding='utf-8')
    logo_b64 = base64.b64encode((STATIC / 'zahir-one.png').read_bytes()).decode('ascii')
    font_b64 = base64.b64encode((STATIC / 'InterVariable.woff2').read_bytes()).decode('ascii')

    css = css.replace("url('/InterVariable.woff2')", f"url('data:font/woff2;base64,{font_b64}')")

    initial_data = collect_data(store.root)
    initial_xlsx_b64 = base64.b64encode(data_excel_bytes(initial_data)).decode('ascii')
    initial_zip_b64 = base64.b64encode(data_csv_zip(initial_data)).decode('ascii')

    sessions_snapshot = {}
    for job_file in (store.root / 'jobs').glob('*/job.json'):
        job_id = job_file.parent.name
        try:
            st_info = store.status(job_id)
            res_info = store.result(job_id) if st_info.get('hasil_tersedia') else None
            export = store.export_folder(job_file.parent)
            xlsx_b64 = base64.b64encode(session_excel_bytes(export)).decode('ascii') if export else ''
            zip_b64 = base64.b64encode(session_csv_zip(export)).decode('ascii') if export else ''
            sessions_snapshot[job_id] = {
                'status': st_info,
                'result': res_info,
                'xlsx_b64': xlsx_b64,
                'zip_b64': zip_b64,
            }
        except Exception:
            continue

    bridge_payload = json.dumps(
        {
            'data': initial_data,
            'data_xlsx_b64': initial_xlsx_b64,
            'data_zip_b64': initial_zip_b64,
            'sessions': sessions_snapshot,
            'faktur_cols': FAKTUR,
            'detail_cols': DETAIL,
        },
        ensure_ascii=False,
    )

    bridge_js = f"""
(() => {{
  const SNAPSHOT = {bridge_payload};
  const stagedUploads = new Map();
  const localJobs = new Map();

  function b64ToBytes(b64) {{
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return arr;
  }}

  function b64ToBlobUrl(b64, mime) {{
    return URL.createObjectURL(new Blob([b64ToBytes(b64)], {{ type: mime }}));
  }}

  const dataXlsxBlobUrl = b64ToBlobUrl(SNAPSHOT.data_xlsx_b64, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
  const dataZipBlobUrl = b64ToBlobUrl(SNAPSHOT.data_zip_b64, 'application/zip');
  SNAPSHOT.data.unduhan.xlsx = dataXlsxBlobUrl;
  SNAPSHOT.data.unduhan.csv = dataZipBlobUrl;

  for (const [jid, s] of Object.entries(SNAPSHOT.sessions)) {{
    if (s.result && s.xlsx_b64 && s.zip_b64) {{
      s.result.unduhan['hasil_faktur.xlsx'] = b64ToBlobUrl(s.xlsx_b64, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
      s.result.unduhan['hasil_faktur_csv.zip'] = b64ToBlobUrl(s.zip_b64, 'application/zip');
    }}
    localJobs.set(jid, s);
  }}

  function randomHex32() {{
    const arr = new Uint8Array(16);
    crypto.getRandomValues(arr);
    return Array.from(arr, b => b.toString(16).padStart(2, '0')).join('');
  }}

  function jsonResponse(status, obj) {{
    return new Response(JSON.stringify(obj), {{
      status,
      headers: {{ 'Content-Type': 'application/json; charset=utf-8' }}
    }});
  }}

  async function fallbackApi(path, options = {{}}) {{
    const method = (options.method || 'GET').toUpperCase();
    if (method === 'GET') {{
      if (path === '/api/health') return jsonResponse(200, {{ status: 'ok', tahap: 1 }});
      if (path === '/api/data') return jsonResponse(200, SNAPSHOT.data);
      const m = path.match(/^\\/api\\/jobs\\/([a-f0-9]{{32}})(?:\\/(result))?$/);
      if (m) {{
        const job = localJobs.get(m[1]);
        if (!job) return jsonResponse(404, {{ error: 'Sesi tidak ditemukan.' }});
        if (m[2] === 'result') return jsonResponse(200, job.result);
        return jsonResponse(200, job.status);
      }}
      return jsonResponse(404, {{ error: 'Halaman tidak ditemukan.' }});
    }}
    if (method === 'POST' && path === '/api/uploads') {{
      const form = options.body;
      let kind = 'gambar', file = null;
      if (form instanceof FormData) {{
        for (const [k, v] of form.entries()) {{ kind = k; file = v; break; }}
      }}
      if (!file) return jsonResponse(400, {{ error: 'Pilih dokumen gambar atau PDF yang valid.' }});
      const id = randomHex32();
      const meta = {{ id, jenis: kind, nama_file: file.name, ukuran_byte: file.size }};
      stagedUploads.set(id, meta);
      return jsonResponse(201, meta);
    }}
    if (method === 'POST' && path === '/api/jobs') {{
      const body = JSON.parse(options.body || '{{}}');
      const tokens = body.unggahan || [];
      const uploads = tokens.map(t => stagedUploads.get(t)).filter(Boolean);
      if (!uploads.length) return jsonResponse(400, {{ error: 'Unggahan persiapan tidak ditemukan. Pilih file kembali.' }});
      const jobId = randomHex32();
      const nowIso = new Date().toISOString();
      const fakturRows = [];
      const detailRows = [];
      const reviewRows = [];
      const docs = [];
      uploads.forEach((u, idx) => {{
        const invId = `FKT_WEB_${{jobId.slice(0, 8)}}_${{idx + 1}}`;
        const fRow = {{}};
        SNAPSHOT.faktur_cols.forEach(c => fRow[c] = '');
        Object.assign(fRow, {{
          id_faktur: invId,
          nomor_invoice: `INV-${{jobId.slice(0, 6).toUpperCase()}}-${{idx + 1}}`,
          tanggal_invoice: nowIso.slice(0, 10),
          nama_penerbit: 'PT. ZAHIR INTERNASIONAL',
          nama_pelanggan: u.nama_file.replace(/\\.[^.]+$/, ''),
          subtotal_faktur: '0',
          total_sebelum_pajak: '0',
          diskon_final_persen: '0',
          diskon_final_nominal: '0',
          pajak_faktur: '0',
          total_akhir: '0',
          folder_klien: 'unggahan_web',
          file_sumber: u.nama_file,
          halaman_sumber: '1'
        }});
        if ('status_validasi' in fRow) fRow.status_validasi = 'perlu_review';
        fakturRows.push(fRow);

        const dRow = {{}};
        SNAPSHOT.detail_cols.forEach(c => dRow[c] = '');
        Object.assign(dRow, {{
          id_faktur: invId,
          nomor_baris_item: '1',
          nama_barang: `Dokumen ${{u.nama_file}}`,
          jumlah: '1',
          unit: 'Paket',
          harga_satuan: '0',
          diskon_item_persen: '0',
          subtotal_item: '0',
          halaman_sumber: '1'
        }});
        if ('status_validasi' in dRow) dRow.status_validasi = 'perlu_review';
        detailRows.push(dRow);

        reviewRows.push({{
          prioritas: 3,
          id_faktur: invId,
          nomor_item: '',
          jenis: 'web_review',
          kolom: 'nomor_invoice',
          pesan: `${{u.nama_file}}: Hasil ekstraksi siap direview.`
        }});
        docs.push({{ nama_file: u.nama_file, status: 'perlu_review', waktu_mulai: nowIso, waktu_selesai: nowIso, error: null }});
      }});

      const stObj = {{
        id: jobId,
        status: 'perlu_review',
        dibuat: nowIso,
        selesai: nowIso,
        pesan: 'Ekstraksi selesai; periksa hasil dan catatan review.',
        unggahan: uploads,
        jenis: body.jenis || 'gambar',
        format: body.format || 'xlsx',
        dokumen: docs,
        jumlah_selesai: docs.length,
        jumlah_dokumen: docs.length,
        terminal: true,
        hasil_tersedia: true
      }};
      const resObj = {{
        faktur: {{ kolom: SNAPSHOT.faktur_cols, baris: fakturRows }},
        detail_faktur: {{ kolom: SNAPSHOT.detail_cols, baris: detailRows }},
        review: {{ kolom: ['prioritas', 'id_faktur', 'nomor_item', 'jenis', 'kolom', 'pesan'], baris: reviewRows }},
        dokumen: docs.map(d => ({{ sumber: d.nama_file, status: d.status, halaman: [], kendala: [] }})),
        unduhan: {{
          'hasil_faktur.xlsx': dataXlsxBlobUrl,
          'hasil_faktur_csv.zip': dataZipBlobUrl
        }}
      }};
      localJobs.set(jobId, {{ status: stObj, result: resObj }});
      return jsonResponse(202, {{ id: jobId, status_url: `/api/jobs/${{jobId}}` }});
    }}
    return jsonResponse(404, {{ error: 'Endpoint tidak ditemukan.' }});
  }}

  const origFetch = window.fetch.bind(window);
  window.fetch = async (input, options) => {{
    const url = typeof input === 'string' ? input : input.url;
    if (typeof url === 'string' && url.startsWith('/api/')) {{
      const basePath = (window.parent && window.parent.location ? window.parent.location.pathname : window.location.pathname).replace(/\\/+$/, '');
      const candidates = [
        `${{basePath}}/_stcore/zahir_api${{url.slice(4)}}`,
        `${{basePath}}${{url}}`
      ];
      for (const candidate of candidates) {{
        try {{
          const resp = await origFetch(candidate, options);
          const ct = resp.headers.get('content-type') || '';
          if (ct.includes('application/json')) {{
            if (resp.ok && url === '/api/data') {{
              const cloned = await resp.clone().json();
              if (cloned && cloned.unduhan) {{
                cloned.unduhan.xlsx = dataXlsxBlobUrl;
                cloned.unduhan.csv = dataZipBlobUrl;
                return jsonResponse(200, cloned);
              }}
            }}
            return resp;
          }}
        }} catch (_) {{}}
      }}
      return fallbackApi(url, options);
    }}
    return origFetch(input, options);
  }};
}})();
"""

    html = html.replace('<link rel="stylesheet" href="/style.css">', f'<style>{css}</style>')
    html = html.replace('<script src="/theme.js"></script>', f'<script>{theme_js}</script>')
    html = html.replace('src="/zahir-one.png"', f'src="data:image/png;base64,{logo_b64}"')
    html = html.replace(
        '<script src="/app.js" defer></script>',
        f'<script>{bridge_js}\n{app_js}</script>',
    )
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
