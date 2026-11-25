"""QA browser dengan worker fixture dan hasil lama; tidak menjalankan OCR."""
import argparse
import base64
import copy
import csv
import json
import shutil
import subprocess
import os
import threading
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit
from batch_common import read_json, atomic_json, seal, now, digest
from web_lokal.app import JobStore, LocalServer, Handler, OUTPUT
from test_web_lokal import image_bytes, pdf_bytes
from tests.support import check_bundle
from test_data_web_lokal import copy_saved_session, SOURCES
from web_lokal.data import collect_data, data_matrices
from unittest.mock import patch

class FixtureStore(JobStore):
    def __init__(self, root):
        super().__init__(root, recover=False)
        self.finish = threading.Event()
        self.submits = 0
        for index, source in enumerate(SOURCES):
            copy_saved_session(self.root, source, partial=index == 0)
        self.old_id = SOURCES[1]
        self.old_export = self.export_folder(self.folder(self.old_id))

    def _run(self, folder):
        # Ini fixture pengujian UI, bukan hasil ekstraksi baru.
        self.finish.clear(); self.submits += 1
        state = read_json(folder/'job.json')
        state.update(status='diproses', mulai=now(), pesan='Worker fixture pengujian; tanpa OCR.')
        atomic_json(folder/'job.json',state)
        entries = read_json(folder/'manifest.json')['dokumen']
        docs = [{**e,'status':'perlu_review' if i==0 else 'diproses','error':None} for i,e in enumerate(entries)]
        atomic_json(folder/'run/progres.json',{'dokumen':docs})
        self.finish.wait(40)
        export=folder/'run/ekspor/fixture';export.mkdir(parents=True)
        evidence=copy.deepcopy(read_json(self.old_export/'bukti_ekstraksi.json'))
        keep=evidence['faktur'][0]['nilai']['id_faktur']
        for name in ['faktur.csv','detail_faktur.csv','daftar_review.csv']:
            with (self.old_export/name).open(encoding='utf-8-sig',newline='') as handle:
                reader=csv.DictReader(handle);fields=reader.fieldnames;rows=[r for r in reader if r['id_faktur']==keep]
            with (export/name).open('w',encoding='utf-8-sig',newline='') as handle:
                writer=csv.DictWriter(handle,fieldnames=fields);writer.writeheader();writer.writerows(rows)
        evidence['faktur']=evidence['faktur'][:1]
        evidence['dokumen']=evidence['dokumen'][:1]
        evidence['review']=[r for r in evidence['review'] if r['id_faktur']==keep]
        atomic_json(export/'bukti_ekstraksi.json',evidence);seal(export)
        for d in docs[1:]: d.update(status='gagal',error={'pesan':'Kegagalan fixture yang disengaja; tanpa OCR.'})
        atomic_json(folder/'run/progres.json',{'dokumen':docs,'ekspor_terakhir':str(export.resolve())})
        state.update(status='sebagian_gagal' if len(docs)>1 else 'perlu_review',selesai=now())
        atomic_json(folder/'job.json',state)

    def close(self):
        self.finish.set();super().close()

class QAHandler(Handler):
    def do_GET(self):
        path=urlsplit(self.path).path
        if path=='/__qa/files':
            return self.reply(200,{'gambar':base64.b64encode(image_bytes()).decode(),'pdf':base64.b64encode(pdf_bytes()).decode()})
        if path=='/__qa/stats':
            store=self.server.store
            return self.reply(200,{'submits':store.submits,'staged':len(list((store.root/'persiapan').glob('*/upload.json')))})
        if path=='/__qa/finish':
            self.server.store.finish.set();return self.reply(200,{'ok':True})
        return super().do_GET()

    def log_message(self, *args):
        pass

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();report=args.output.resolve()
    store=FixtureStore(report/('runtime_browser_'+uuid4().hex[:8]))
    initial_data=collect_data(store.root)
    server=LocalServer(('127.0.0.1',0),store);server.RequestHandlerClass=QAHandler
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with (report/'browser.log').open('wb') as log:
            proc=subprocess.run(['node','uji_tampilan_web_lokal.mjs',f'http://127.0.0.1:{server.server_port}',str(report),store.old_id],stdout=log,stderr=subprocess.STDOUT,timeout=120)
        print((report/'browser.log').read_text(encoding='utf-8',errors='replace'))
        assert proc.returncode==0
        # Validasi isi unduhan yang benar-benar diklik pada browser (hasil fixture subset sesi lama).
        csv_zip=report/'browser_downloads/hasil_faktur_csv.zip'
        import zipfile,io
        with zipfile.ZipFile(csv_zip) as archive:
            expected={name.removesuffix('.csv'):list(csv.reader(io.StringIO(archive.read(name).decode('utf-8-sig')))) for name in archive.namelist()}
        check_bundle((report/'browser_downloads/hasil_faktur.xlsx').read_bytes(),expected)
        with zipfile.ZipFile(report/'browser_downloads/data_faktur_csv.zip') as archive:
            matrices=data_matrices(initial_data)
            for key in matrices:
                assert list(csv.reader(io.StringIO(archive.read(key+'.csv').decode('utf-8-sig'))))==matrices[key]
        check_bundle((report/'browser_downloads/data_faktur.xlsx').read_bytes(),data_matrices(initial_data))
        atomic_json(report/'uji_browser_isi_bundle.json',{'hasil':'lolos','worker':'fixture tanpa OCR','baris_faktur':len(expected['faktur'])-1,'baris_detail':len(expected['detail_faktur'])-1,'csv_dan_excel_identik':True,'submits_fixture':store.submits})
        restart_root=store.root
        restart_port=server.server_port
        expected_restart=collect_data(restart_root)
    finally:
        store.finish.set();server.shutdown();server.server_close();thread.join();store.close()
    # Proses browser dan server baru, tetap memakai direktori hasil yang sama.
    restarted=JobStore(restart_root, recover=False)
    server2=LocalServer(('127.0.0.1',restart_port),restarted)
    thread2=threading.Thread(target=server2.serve_forever,daemon=True);thread2.start()
    try:
        environment={**os.environ,'QA_RESTORE_ONLY':'1','QA_EXPECTED_FACTS':str(len(expected_restart['faktur']['baris'])),'QA_EXPECTED_DETAILS':str(len(expected_restart['detail_faktur']['baris']))}
        with patch.object(restarted.executor,'submit',side_effect=AssertionError('Restart tidak boleh memulai OCR')):
            with (report/'browser_restart.log').open('wb') as log:
                proc=subprocess.run(['node','uji_tampilan_web_lokal.mjs',f'http://127.0.0.1:{server2.server_port}/#data',str(report),'restart'],env=environment,stdout=log,stderr=subprocess.STDOUT,timeout=60)
            print((report/'browser_restart.log').read_text(encoding='utf-8',errors='replace'))
            assert proc.returncode==0
            assert collect_data(restart_root)==expected_restart
    finally:
        server2.shutdown();server2.server_close();thread2.join();restarted.close()

if __name__=='__main__':main()
