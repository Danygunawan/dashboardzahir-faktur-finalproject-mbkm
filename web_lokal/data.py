"""Gabungan baca-saja dari ekspor terbaru sesi web, tanpa impor batch produksi."""
import csv
import io
import re
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from batch_common import complete, read_json
from ekstrak_faktur_digital import FAKTUR, DETAIL
from web_lokal.downloads import matrix_excel_bytes

FINISHED = {'selesai', 'perlu_review', 'sebagian_gagal', 'gagal', 'terputus'}
META = ['id_sesi', 'kunci_faktur']


def read_table(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields, rows = reader.fieldnames, list(reader)
    if not fields or len(fields) != len(set(fields)) or 'id_faktur' not in fields:
        raise ValueError('Header tabel hasil tidak valid.')
    if any(k in fields for k in META) or any(None in row or any(v is None for v in row.values()) for row in rows):
        raise ValueError('Isi tabel hasil tidak lengkap atau memiliki kolom metadata yang bertabrakan.')
    return fields, rows


def collect_data(root):
    root = Path(root).resolve()
    fields = {'faktur': list(FAKTUR), 'detail_faktur': list(DETAIL)}
    rows = {'faktur': [], 'detail_faktur': []}
    included, problems = [], []
    for folder in sorted((root / 'jobs').glob('*')):
        if not folder.is_dir() or not re.fullmatch('[a-f0-9]{32}', folder.name):
            continue
        if not folder.resolve().is_relative_to(root):
            continue
        if not (folder / 'job.json').is_file():
            continue
        try:
            state = read_json(folder / 'job.json')
            if state.get('id') != folder.name:
                raise ValueError('ID sesi tidak cocok dengan folder.')
            if state.get('status') not in FINISHED:
                continue
            progress_path = folder / 'run/progres.json'
            if not progress_path.is_file():
                continue
            latest = read_json(progress_path).get('ekspor_terakhir')
            if not latest:
                continue
            export = Path(latest).resolve()
            if not export.is_relative_to(folder.resolve()) or not export.is_relative_to((folder / 'run/ekspor').resolve()) or not complete(export):
                raise ValueError('Ekspor belum lengkap atau verifikasi berkas gagal.')
            tables = {name: read_table(export / (name + '.csv')) for name in rows}
            invoices = [r['id_faktur'] for r in tables['faktur'][1]]
            if any(not value for value in invoices) or len(invoices) != len(set(invoices)):
                raise ValueError('ID faktur kosong atau berulang di dalam sesi.')
            parent_ids = set(invoices)
            if any(r['id_faktur'] not in parent_ids for r in tables['detail_faktur'][1]):
                raise ValueError('Detail tanpa faktur induk di sesi asal.')
            for name, (columns, records) in tables.items():
                fields[name] += [c for c in columns if c not in fields[name]]
                rows[name].extend({**record, 'id_sesi': folder.name,
                                   'kunci_faktur': folder.name + '::' + record['id_faktur']}
                                  for record in records)
            included.append(folder.name)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problems.append({'id_sesi': folder.name, 'pesan': str(exc)})
    result = {'sesi': included, 'kendala': problems}
    for name in rows:
        columns = ['id_sesi'] + fields[name] + ['kunci_faktur']
        result[name] = {'kolom': columns, 'baris': [{c: row.get(c, '') for c in columns} for row in rows[name]]}
    result['unduhan'] = {'xlsx': '/api/data/download/data_faktur.xlsx', 'csv': '/api/data/download/data_faktur_csv.zip'}
    return result


def data_matrices(data):
    return {key: [data[key]['kolom']] + [[row[c] for c in data[key]['kolom']] for row in data[key]['baris']]
            for key in ['faktur', 'detail_faktur']}


def data_excel_bytes(data):
    values = data_matrices(data)
    return matrix_excel_bytes([(values['faktur'], 'Faktur'), (values['detail_faktur'], 'Detail Faktur')])


def data_csv_zip(data):
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
        for key, values in data_matrices(data).items():
            text = io.StringIO(newline='')
            csv.writer(text).writerows(values)
            archive.writestr(key + '.csv', text.getvalue().encode('utf-8-sig'))
    return output.getvalue()
