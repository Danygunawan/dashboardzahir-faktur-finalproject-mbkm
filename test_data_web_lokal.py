"""Gabungan lintas sesi, benturan ID, ekspor dan restart; tanpa OCR."""
import csv
import io
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from openpyxl import load_workbook

from batch_common import atomic_json, digest, read_json, seal
from web_lokal.app import JobStore, LocalServer, OUTPUT
from web_lokal.data import collect_data, data_matrices, data_excel_bytes, data_csv_zip
from tests.support import make_session, SOURCES

def copy_saved_session(root, source_id, partial=False):
    """Nama helper lama dipertahankan; isinya kini selalu sintetis."""
    return make_session(root, source_id, partial)


def assert_relations(test, data):
    parents = {r['kunci_faktur']: (r['id_sesi'], r['id_faktur']) for r in data['faktur']['baris']}
    test.assertEqual(len(parents), len(data['faktur']['baris']))
    for row in data['detail_faktur']['baris']:
        test.assertEqual(parents[row['kunci_faktur']], (row['id_sesi'], row['id_faktur']))


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='uji_data_')
        self.root = Path(self.tmp.name)
        for index, source in enumerate(SOURCES):
            copy_saved_session(self.root, source, partial=index == 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_saved_sessions_partial_latest_only_and_no_duplicates(self):
        data = collect_data(self.root)
        self.assertEqual(len(data['faktur']['baris']), 3)
        self.assertEqual(len(data['detail_faktur']['baris']), 7)
        self.assertEqual(data['kendala'], [])
        assert_relations(self, data)
        # Sebuah ekspor lama yang tidak ditunjuk tidak menambah baris.
        target = self.root / 'jobs' / SOURCES[0] / 'run/ekspor'
        shutil.copytree(target / 'synthetic', target / 'old_snapshot')
        self.assertEqual(collect_data(self.root), data)
        self.assertEqual(collect_data(self.root), data)

    def test_same_invoice_id_across_synthetic_sessions_keeps_separate_parents(self):
        data = collect_data(self.root)
        self.assertGreaterEqual(len(data['sesi']), 2)
        repeated = [key for key, count in Counter(r['id_faktur'] for r in data['faktur']['baris']).items() if count > 1]
        self.assertTrue(repeated, 'Fixture mencakup ID faktur sama pada dua sesi.')
        assert_relations(self, data)
        for source_id in data['sesi']:
            export = Path(read_json(self.root/'jobs'/source_id/'run/progres.json')['ekspor_terakhir'])
            with (export/'detail_faktur.csv').open(encoding='utf-8-sig', newline='') as handle:
                expected = list(csv.DictReader(handle))
            actual = [{k:v for k,v in row.items() if k not in {'id_sesi','kunci_faktur'}}
                      for row in data['detail_faktur']['baris'] if row['id_sesi'] == source_id]
            self.assertEqual(actual, expected)

    def test_corrupt_export_and_external_pointer_are_excluded(self):
        job = self.root/'jobs'/SOURCES[0]
        export = Path(read_json(job/'run/progres.json')['ekspor_terakhir'])
        with (export/'faktur.csv').open('ab') as handle:
            handle.write(b'corrupt')
        data = collect_data(self.root)
        self.assertEqual(len(data['faktur']['baris']), 2)
        self.assertEqual(len(data['kendala']), 1)
        progress = read_json(job/'run/progres.json')
        progress['ekspor_terakhir'] = str(self.root / 'outside-export')
        atomic_json(job/'run/progres.json', progress)
        self.assertEqual(len(collect_data(self.root)['faktur']['baris']), 2)

    def test_orphan_detail_is_rejected_even_if_sealed(self):
        job = self.root/'jobs'/SOURCES[0]
        export = Path(read_json(job/'run/progres.json')['ekspor_terakhir'])
        with (export/'detail_faktur.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader=csv.DictReader(handle); fields=reader.fieldnames; rows=list(reader)
        rows[0]['id_faktur']='induk-tidak-ada'
        with (export/'detail_faktur.csv').open('w',encoding='utf-8-sig',newline='') as handle:
            writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(rows)
        seal(export)
        data=collect_data(self.root)
        self.assertEqual(len(data['faktur']['baris']),2)
        self.assertIn('induk',data['kendala'][0]['pesan'])

    def test_bundles_match_all_rows_and_have_text_identity(self):
        data = collect_data(self.root)
        expected = data_matrices(data)
        workbook = load_workbook(io.BytesIO(data_excel_bytes(data)))
        try:
            self.assertEqual(workbook.sheetnames, ['Faktur','Detail Faktur'])
            for key, sheet in zip(['faktur','detail_faktur'],workbook):
                values=[[c.value if c.value is not None else '' for c in row] for row in sheet.iter_rows()]
                self.assertEqual(values, expected[key])
                self.assertTrue(all(c.data_type=='s' for row in sheet.iter_rows(min_row=2) for c in row if c.value is not None))
        finally:
            workbook.close()
        with zipfile.ZipFile(io.BytesIO(data_csv_zip(data))) as archive:
            self.assertEqual(archive.namelist(), ['faktur.csv','detail_faktur.csv'])
            for key in expected:
                self.assertEqual(list(csv.reader(io.StringIO(archive.read(key+'.csv').decode('utf-8-sig')))), expected[key])

    def test_http_restart_refresh_and_download_do_not_mutate_or_trigger_worker(self):
        before={str(p):digest(p) for p in self.root.rglob('*') if p.is_file()}
        snapshots=[]
        for _ in range(2):
            store=JobStore(self.root)
            server=LocalServer(('127.0.0.1',0),store)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with patch.object(store.executor,'submit',side_effect=AssertionError('OCR dilarang')):
                    base=f'http://127.0.0.1:{server.server_port}'
                    with urlopen(base+'/api/data') as response:data=json.load(response)
                    snapshots.append(data)
                    for name in ['data_faktur.xlsx','data_faktur_csv.zip']:
                        with urlopen(base+'/api/data/download/'+name) as response:
                            self.assertTrue(response.read())
                            self.assertEqual(response.headers['Content-Disposition'],f'attachment; filename="{name}"')
                    with urlopen(base+'/api/data') as response:self.assertEqual(json.load(response),data)
            finally:
                server.shutdown();server.server_close();thread.join();store.close()
        self.assertEqual(snapshots[0],snapshots[1])
        self.assertEqual(len(snapshots[1]['faktur']['baris']),3)
        self.assertEqual(len(snapshots[1]['detail_faktur']['baris']),7)
        self.assertEqual(before,{str(p):digest(p) for p in self.root.rglob('*') if p.is_file()})


if __name__=='__main__':
    unittest.main()
