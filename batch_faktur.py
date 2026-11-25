"""Batch berurutan dengan progres atomik, cache OCR, dan hasil dokumen immutable."""
import argparse
import copy
import importlib.metadata
import json
import os
import shutil
import time
import traceback
from pathlib import Path
from uuid import uuid4
from batch_common import atomic_json, complete, digest, export, fingerprint, lock_run, now, read_json, seal, review_queue
from uji_ocr_gambar import CONFIG, buat_segmen, pratinjau
from baca_pdf_digital import ekstrak_halaman, kategori_judul
from ekstrak_faktur_digital import ekstrak_dokumen
from ekstrak_faktur_ocr import ekstrak
ROOT=Path(__file__).resolve().parent


def processing_config():
    return {'versi':'batch-v14-konflik-pemisah','ekstraksi':{n:digest(ROOT/n) for n in ['ekstrak_faktur_ocr.py','ekstrak_faktur_digital.py','baca_pdf_digital.py','batch_common.py','batch_faktur.py','faktur_tanpa_header.py','baca_ulang_angka.py','normalisasi_angka.py','konteks_angka_total.py','konteks_format_uang.py','format_ringkasan.py','header_grid.py','label_region_invoice.py','identitas_header_native.py','bukti_ocr_tambahan.py','pemulihan_angka.py','profil_format_uang.py','roi_sel_ocr.py','geometri_uang_persen.py','geometri_band_baris.py','profil_uang_tabel.py','diskon_item_bersatuan.py','nilai_review_faktur.py','uji_ocr_gambar.py','geometri_tabel_pdf.py','cakupan_dokumen.py','validasi_ringkasan.py']},
            'fitur_umum':{'profil_uang':'profil-uang-v3-konteks-tabel','profil_tabel':'profil-uang-tabel-v1','konteks_uang_item':'konteks-uang-item-v2-kolom','identitas_header_native':'identitas-header-native-v1','identitas_fallback_guard':'identitas-fallback-v2','label_region_invoice':'label-region-invoice-v2','format_ringkasan':'format-ringkasan-v3','validasi_ringkasan':'validasi-ringkasan-v2','label_total':'layout-ringkasan-v3','terbilang':'total-terbilang-v3','cakupan':'cakupan-layout-v2','asal_subtotal':'asal-subtotal-v2','header':'header-grid-v3','PDF_label':'pdf-label-sel-v3','PDF_grid':'pdf-grid-native-v2','ROI_geometri':'roi-geometri-sel-v5-band','ROI_band':'band-piksel-baris-v1','ROI_region_uang_persen':'geometri-uang-persen-v1','ROI':'pemulihan-angka-v8-konflik-pemisah'},
            'digital_min_kata':20,'render_dpi':144,'ocr':CONFIG,
            'paket':{n:importlib.metadata.version(n) for n in ['paddleocr','paddlepaddle','paddlex','PyMuPDF']}}


def ocr_config():
    return {'model':CONFIG,'preprocessing':'identitas','paket':{n:importlib.metadata.version(n) for n in ['paddleocr','paddlepaddle','paddlex']}}


class OCRCache:
    def __init__(self,root):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True); self.model=None; self.load_seconds=0; self.stats=[]

    def key(self,h,page,dpi): return fingerprint({'hash_sumber':h,'halaman':page,'dpi_pdf':dpi,**ocr_config()})

    def valid_data(self,data):
        if data.get('konfigurasi')!=CONFIG: return False
        if data.get('satuan_koordinat')!='piksel': return False
        for s in data['segmen']:
            if not all(k in s for k in ['teks_asli','id_segmen','bbox','poligon','skor_keyakinan','tingkat_segmen','nomor_halaman']): return False
        return bool(data['segmen'])

    def previous(self,h):
        for p in sorted((ROOT/'outputs/uji_ocr_gambar').glob('*/teks_koordinat_ocr.json')):
            try:
                d=read_json(p)
                if d.get('sha256_sumber_sebelum')!=h or d.get('sha256_sumber_sesudah')!=h or not self.valid_data(d): continue
                if any(d.get('versi_paket_utama',{}).get(k)!=v for k,v in ocr_config()['paket'].items()): continue
                raw=read_json(p.parent/'ocr_mentah.json'); raw=raw.get('res',raw)
                rec=[s for s in d['segmen'] if s['asal_hasil']=='pengenalan']
                if [s['teks_asli'] for s in rec]!=raw['rec_texts'] or [s['poligon'] for s in rec]!=raw['rec_polys'] or [s['skor_keyakinan'] for s in rec]!=raw['rec_scores']: continue
                from PIL import Image
                with Image.open(p.parent/'pratinjau_ocr.png') as im:
                    if list(im.size)!=[d['ukuran_gambar']['lebar'],d['ukuran_gambar']['tinggi']]: continue
                    im.verify()
                if not (p.parent/'laporan_ocr.md').is_file(): continue
                return p,d
            except (OSError,ValueError,KeyError,TypeError): continue
        return None

    def obtain(self,path,h,page,meta,dpi=None):
        started=time.perf_counter(); key=self.key(h,page,dpi)
        for folder in sorted(self.root.glob(key+'_*')):
            if complete(folder):
                d=read_json(folder/'teks_koordinat_ocr.json')
                if self.valid_data(d):
                    self.stats.append({'halaman':page,'metode':'cache_ocr','detik':time.perf_counter()-started,'cache':str(folder)})
                    return d,folder
        folder=self.root/(key+'_'+uuid4().hex[:8]); folder.mkdir()
        previous=self.previous(h) if dpi is None and page==1 else None
        if previous:
            src,d=previous
            for name in ['ocr_mentah.json','teks_koordinat_ocr.json','pratinjau_ocr.png']:
                shutil.copyfile(src.parent/name,folder/name)
            atomic_json(folder/'asal_cache.json',{'input':str(src),'sha256':digest(src),'key':key,'config':ocr_config()})
            method='impor_cache_ocr_valid'
        else:
            # Tidak mengganti model atau mengunduh ulang; Paddle memakai cache model yang lengkap.
            if self.model is None:
                t=time.perf_counter(); os.environ['PADDLE_PDX_MODEL_SOURCE']='BOS'
                from paddleocr import PaddleOCR
                self.model=PaddleOCR(**CONFIG); self.load_seconds+=time.perf_counter()-t
            from PIL import Image
            with Image.open(path) as im:
                if im.getexif().get(274,1)!=1: raise ValueError('Rotasi EXIF belum didukung')
                image=im.convert('RGB'); w,hh=image.size
            t=time.perf_counter(); result=list(self.model.predict(input=str(path)))
            infer=time.perf_counter()-t
            if len(result)!=1: raise ValueError('Jumlah hasil OCR gambar tidak tunggal')
            result[0].save_to_json(str(folder/'ocr_mentah.json'))
            raw=read_json(folder/'ocr_mentah.json'); segments=buat_segmen(raw,path,w,hh,.85)
            d={'versi_skema':1,'sumber_gambar':str(path),'sha256_sumber_sebelum':h,'sha256_sumber_sesudah':h,
               'inventaris':meta,'nomor_halaman':1,'ukuran_gambar':{'lebar':w,'tinggi':hh},'satuan_koordinat':'piksel',
               'gambar_acuan_koordinat':str(path),'transformasi_aplikasi':{'jenis':'identitas'},'konfigurasi':CONFIG,
               'versi_paket_utama':ocr_config()['paket'],'segmen':segments,'waktu_detik':{'inferensi':infer},
               'sumber_dokumen':meta['path_sumber'],'halaman_dokumen':page,'render_dpi':dpi}
            atomic_json(folder/'teks_koordinat_ocr.json',d); pratinjau(image,segments,folder/'pratinjau_ocr.png')
            atomic_json(folder/'asal_cache.json',{'key':key,'config':ocr_config(),'sha256_input_render':digest(path),'inferensi_detik':infer})
            method='ocr_baru'
        seal(folder)
        self.stats.append({'halaman':page,'metode':method,'detik':time.perf_counter()-started,'cache':str(folder),'waktu_ocr_asal':d.get('waktu_detik')})
        return d,folder


def category_ocr(d):
    lines=[{'teks':s['teks_asli'],'bbox':s['bbox']} for s in d['segmen']]
    result=kategori_judul(lines,d['ukuran_gambar']['tinggi'])
    if result[0]=='perlu_diperiksa':
        from ekstrak_faktur_ocr import judul_typo_didukung
        typo=judul_typo_didukung(d)
        if typo:return 'faktur_penjualan','Judul typo spesifik didukung label identitas dan tabel; perlu review',[typo]
        from faktur_tanpa_header import struktur_tanpa_header
        if struktur_tanpa_header(d):
            return 'faktur_penjualan','Inferensi struktur nomor/tanggal/pelanggan, kolom barang-jumlah-harga-subtotal dan total; judul/header OCR hilang, wajib review',[]
    return result


def old_pdf(h):
    for p in sorted((ROOT/'outputs/uji_pdf_digital').glob('*/*/teks_koordinat.json')):
        try:
            d=read_json(p)
            if d.get('sha256_sumber_sebelum')==h==d.get('sha256_sumber_sesudah') and d.get('versi_pymupdf')==importlib.metadata.version('PyMuPDF'):
                pages=d['hasil_program']['halaman']
                if len(pages)==d['hasil_program']['jumlah_halaman'] and all('kata' in pg and 'baris' in pg for pg in pages): return d,p
        except (OSError,ValueError,KeyError): continue
    return None


def process_document(entry,h,folder,cache):
    import pymupdf
    source=Path(entry['path_sumber']); suffix=source.suffix.lower(); pages=[]; t=time.perf_counter(); begin=len(cache.stats)
    meta={k:entry.get(k,'') for k in ['folder_klien','nama_file','path_relatif','path_sumber']}
    if suffix=='.pdf':
        previous=old_pdf(h)
        with pymupdf.open(source) as pdf:
            if pdf.needs_pass: raise ValueError('PDF memerlukan kata sandi')
            for index,page in enumerate(pdf):
                num=index+1; words=page.get_text('words')
                suitable=len(words)>=20 and sum(any(c.isalpha() for c in w[4]) for w in words)>=5
                if suitable:
                    pg=copy.deepcopy(previous[0]['hasil_program']['halaman'][index]) if previous and len(previous[0]['hasil_program']['halaman'])==len(pdf) else ekstrak_halaman(page,source,folder,144)
                    if previous:
                        from geometri_tabel_pdf import perkaya_halaman
                        pg=perkaya_halaman(source,num,pg)
                    cat,reason,title=kategori_judul(pg['baris'],pg['ukuran_halaman_pt']['tinggi']); pg['kategori_sementara']=cat
                    pages.append({'halaman':num,'jenis':cat,'alasan':reason,'bukti_judul':title,'metode':'cache_teks_digital' if previous else 'teks_digital','data':pg,'cache_json':str(previous[1]) if previous else None})
                else:
                    image=folder/f'halaman_{num:03d}.png'; page.get_pixmap(dpi=144,alpha=False).save(image)
                    d,cf=cache.obtain(image,h,num,meta,144); cat,reason,title=category_ocr(d)
                    pages.append({'halaman':num,'jenis':cat,'alasan':reason,'bukti_judul':title,'metode':'ocr','data':d,'cache_json':str(cf/'teks_koordinat_ocr.json')})
    elif suffix in ['.jpg','.jpeg','.png','.bmp','.webp','.tif','.tiff']:
        from PIL import Image
        with Image.open(source) as im:
            if getattr(im,'n_frames',1)>1: raise ValueError('Gambar multi-frame belum didukung; perlu review, tidak memilih frame diam-diam')
        d,cf=cache.obtain(source,h,1,meta); cat,reason,title=category_ocr(d)
        pages.append({'halaman':1,'jenis':cat,'alasan':reason,'bukti_judul':title,'metode':'ocr','data':d,'cache_json':str(cf/'teks_koordinat_ocr.json')})
    else: raise ValueError('Ekstensi belum didukung: '+suffix)
    from cakupan_dokumen import observasi_cakupan
    for p in pages:
        data=p['data'];lines=data.get('segmen',data.get('baris',[]))
        height=data.get('ukuran_gambar',{}).get('tinggi',data.get('ukuran_halaman_pt',{}).get('tinggi'))
        p['cakupan_dokumen']=observasi_cakupan(lines,height)
        if p['cakupan_dokumen'] and p['jenis']!='faktur_penjualan':p['alasan']=p['cakupan_dokumen']['alasan']
    sales=[p for p in pages if p['jenis']=='faktur_penjualan']; invoices=[]; problems=[]
    continuation=any(__import__('re').search(r'(?i)\b(lanjutan|continued)\b',s.get('teks_asli',s.get('teks',''))) for p in pages for s in p['data'].get('segmen',p['data'].get('baris',[])))
    if len(sales)>1 or continuation:
        problems.append('Beberapa halaman faktur atau penanda lanjutan; belum didukung. Seluruh halaman disimpan tanpa penggabungan otomatis.')
    elif len(sales)==1:
        p=sales[0]
        try:
            if p['metode']!='ocr':
                d={'sha256_sumber_sebelum':h,'sha256_sumber_sesudah':h,'hasil_program':{**meta,'jumlah_halaman':len(pages),'halaman':[p['data']]}}
                invoices,log=ekstrak_dokumen(d)
                problems += [e.get('alasan','Review parser PDF') for e in log if e['status']=='perlu_review']
            else:
                d=copy.deepcopy(p['data']); d['sumber_gambar']=str(source); d['inventaris']=meta
                inv=ekstrak(d)
                # Jalur dokumen baru, bukan helper replay. ROI saat ini hanya
                # gambar asli satu frame; PDF/render mempertahankan jalur lama.
                if suffix!='.pdf' and getattr(cache,'pemulihan_angka',None) is not None:
                    inv['hash_sumber']=h
                    inv,_=cache.pemulihan_angka.lengkap(inv,d,source,cache_path=cf/'teks_koordinat_ocr.json')
                # Bbox tetap piksel render; identitas halaman asal dipertahankan terpisah.
                def page_refs(value):
                    if isinstance(value,dict):
                        for key,item in value.items():
                            if key in ['halaman','nomor_halaman']: value[key]=p['halaman']
                            elif key=='halaman_sumber' and not isinstance(item,dict): value[key]=str(p['halaman'])
                            elif key=='halaman_sumber' and isinstance(item,dict):
                                item['nilai']=str(p['halaman']); page_refs(item)
                            else: page_refs(item)
                    elif isinstance(value,list):
                        for item in value: page_refs(item)
                page_refs(inv); inv['halaman_dokumen']=p['halaman']; invoices=[inv]
        except (ValueError,KeyError,IndexError) as e:
            problems.append('Ekstraksi perlu review: '+str(e))
    for p in pages:
        p['status']='dilewati_kwitansi' if p['jenis']=='kwitansi' else ('perlu_review' if p['jenis']!='faktur_penjualan' else 'faktur')
        scope=p.get('cakupan_dokumen')
        if p['jenis']!='faktur_penjualan' and scope and scope['status']=='di_luar_cakupan_faktur_penjualan':
            p['status']='dilewati_di_luar_cakupan';problems.append(f'Halaman {p["halaman"]}: '+scope['alasan'])
        elif p['jenis']=='perlu_diperiksa': problems.append(f'Halaman {p["halaman"]}: judul tidak dikenali, termasuk kemungkinan surat/dokumen lain; tidak menambah transaksi')
    from cakupan_dokumen import terapkan_cakupan
    for inv in invoices:
        inv['hash_sumber']=h; inv['lokasi_sumber']=[str(source)]
        pg=next(p for p in sales if str(p['halaman'])==str(inv['nilai']['halaman_sumber']))
        d=pg['data'];lines=d.get('segmen',d.get('baris',[]));size=d.get('ukuran_gambar',d.get('ukuran_halaman_pt',{}))
        analysis=terapkan_cakupan(inv,lines,size['lebar'],size['tinggi'])
        pg['cakupan_dokumen']=analysis['observasi'];pg['analisis_cakupan']=analysis
    from format_ringkasan import tandai_asal_subtotal
    from batch_common import validate_invoice
    for inv in invoices:
        tandai_asal_subtotal(inv)
        inv['validasi']=validate_invoice(inv)
    critical=any(q['prioritas']<=4 for q in review_queue(invoices))
    if not critical and not problems:
        for inv in invoices:
            if any(p['metode']=='ocr' for p in sales):
                inv['nilai']['status_validasi']='lolos_otomatis_belum_verifikasi_manusia'
                inv['kolom']['status_validasi']['nilai']=inv['nilai']['status_validasi']
    result={'faktur':invoices,'halaman':pages,'kendala':problems,'waktu_detik':time.perf_counter()-t,'cache_ocr':cache.stats[begin:],
            'sumber':str(source),'hash_sumber':h,'status':'perlu_review' if problems or critical else 'selesai'}
    if digest(source)!=h: raise ValueError('Sumber berubah selama pemrosesan')
    atomic_json(folder/'hasil_dokumen.json',result); seal(folder)
    return result


def run_batch(manifest,run_dir=None,limit=4,cache_dir=None,processor=process_document,config=None,
              roi_mode='cache',bukti_tambahan=(),roi_max_sel=2,roi_max_inferensi=4):
    if limit<1: raise ValueError('Batas dokumen minimal 1')
    manifest=Path(manifest).resolve(); m=read_json(manifest); entries=m['dokumen']
    if not entries: raise ValueError('Manifest kosong')
    paths=[str(Path(e['path_sumber']).resolve()) for e in entries]
    if len(paths)!=len(set(paths)): raise ValueError('Manifest mengulang path yang sama')
    from pemulihan_angka import PemulihanAngka
    roi=PemulihanAngka(Path(cache_dir or ROOT/'outputs/cache_ocr')/'roi_angka_v1',roi_mode,bukti_tambahan,roi_max_sel,roi_max_inferensi)
    conf=copy.deepcopy(config or processing_config()); conf['pemulihan_angka']=roi.config(); conf_hash=fingerprint(conf)
    run_dir=Path(run_dir).resolve() if run_dir else ROOT/'outputs/batch'/(now().replace(':','').replace('+','_')+'_'+uuid4().hex[:8])
    if run_dir.is_relative_to(Path(r'C:\Data Klient Zahir')): raise ValueError('Run tidak boleh pada sumber')
    run_dir.mkdir(parents=True,exist_ok=True)
    cache=OCRCache(cache_dir or ROOT/'outputs/cache_ocr')
    cache.pemulihan_angka=roi
    roi.model_owner=cache
    with lock_run(run_dir):
        progress_path=run_dir/'progres.json'
        if progress_path.exists():
            progress=read_json(progress_path)
            if progress['hash_manifest']!=digest(manifest): raise ValueError('Manifest berubah; gunakan run baru')
        else:
            progress={'manifest':str(manifest),'hash_manifest':digest(manifest),'dibuat':now(),'dokumen':[{**e,'status':'menunggu','waktu_mulai':None,'waktu_selesai':None,'hash_sumber':None,'config_hash':None,'lokasi_hasil':None,'error':None} for e in entries]}
        progress['config_aktual']=conf; progress['config_hash_aktual']=conf_hash; atomic_json(progress_path,progress)
        known={}; processed=0; skipped=0
        total_doc=min(limit,len(progress['dokumen']))
        for index,entry in enumerate(progress['dokumen']):
            if index>=limit: break
            try:
                source=Path(entry['path_sumber']).resolve()
                if not source.is_file(): raise FileNotFoundError(str(source))
                h=digest(source)
                if entry.get('sha256_diharapkan') and h!=entry['sha256_diharapkan']: raise ValueError('Hash sumber berbeda dari manifest; tinjau perubahan dan buat manifest/run baru')
                result_path=entry.get('lokasi_hasil')
                valid=entry.get('hash_sumber')==h and entry.get('config_hash')==conf_hash and result_path and complete(Path(result_path).parent)
                if valid and entry['status'] in ['selesai','perlu_review']:
                    known[h]=entry; skipped+=1; continue
                print(f"[{index+1}/{total_doc}] Memproses: {entry['nama_file']} ... ",end="",flush=True)
                entry.update(status='diproses',hash_sumber=h,config_hash=conf_hash,versi_config=conf,waktu_mulai=now(),waktu_selesai=None,error=None)
                atomic_json(progress_path,progress)
                if h in known:
                    canonical=known[h]; entry.update(status=canonical['status'],lokasi_hasil=canonical['lokasi_hasil'],duplikat_hash_dari=canonical['path_sumber'],waktu_selesai=now())
                    print("Selesai.")
                else:
                    for old_folder in (run_dir/'dokumen').glob(f'{index+1:03d}_*'):
                        if not complete(old_folder):
                            shutil.rmtree(old_folder,ignore_errors=True)
                    folder=run_dir/'dokumen'/f'{index+1:03d}_{uuid4().hex[:8]}'; folder.mkdir(parents=True)
                    result=processor(entry,h,folder,cache)
                    if not complete(folder): raise ValueError('Hasil parsial: manifest complete tidak valid')
                    entry.update(status=result['status'],lokasi_hasil=str(folder/'hasil_dokumen.json'),waktu_selesai=now(),error=None)
                    known[h]=entry; processed+=1
                    print("Selesai.")
            except Exception as e:
                print(f"Gagal ({e}).")
                entry.update(status='gagal',waktu_selesai=now(),error={'jenis':type(e).__name__,'pesan':str(e),'traceback':traceback.format_exc()})
            atomic_json(progress_path,progress)
        invoices=[]; sources={}; statuses=[]
        for entry in progress['dokumen']:
            path=entry.get('lokasi_hasil'); h=entry.get('hash_sumber')
            if entry['status'] not in ['selesai','perlu_review'] or entry.get('config_hash')!=conf_hash or not path or not complete(Path(path).parent): continue
            # Verifikasi ulang hash saat penggabungan, termasuk hasil dari resume sebelumnya.
            if not Path(entry['path_sumber']).is_file() or digest(entry['path_sumber'])!=h: continue
            sources.setdefault(h,[]).append(entry['path_sumber'])
            if any(s['hash_sumber']==h for s in statuses): continue
            result=read_json(path); invoices.extend(copy.deepcopy(result['faktur'])); statuses.append({'hash_sumber':h,'sumber':entry['path_sumber'],'status':entry['status'],'lokasi_hasil':path,'halaman':[{k:p[k] for k in ['halaman','jenis','metode','status']} for p in result['halaman']],'kendala':result['kendala'],'waktu_detik':result['waktu_detik'],'cache_ocr':result['cache_ocr']})
        for inv in invoices: inv['lokasi_sumber']=sources[inv['hash_sumber']]
        target=run_dir/'ekspor'/uuid4().hex[:12]
        export(target,invoices,{'dokumen':statuses,'semua_lokasi_per_hash':sources,'config':conf,'sumber_otomatis':True})
        progress['ekspor_terakhir']=str(target); progress['terakhir_diperbarui']=now(); progress['statistik_panggilan']={'diproses_baru':processed,'resume_dilewati':skipped,'muat_model_detik':cache.load_seconds,'cache_ocr':cache.stats,
                'ROI':{'inferensi_baru':roi.inferences,'bukti_dipakai_ulang':roi.reused,'muat_model_detik':roi.load_seconds}}
        atomic_json(progress_path,progress)
    print('RUN:',run_dir); print('EKSPOR:',target); print(json.dumps(progress['statistik_panggilan']))
    return run_dir


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--manifest',required=True,type=Path); p.add_argument('--limit',type=int,default=4); p.add_argument('--run-dir',type=Path); p.add_argument('--cache-dir',type=Path)
    p.add_argument('--roi-mode',choices=['off','cache','auto'],default='cache',help='cache: tanpa OCR ROI baru (default); auto: asli lalu3x bila perlu, batas per dokumen')
    p.add_argument('--bukti-tambahan',action='append',type=Path,default=[],help='Indeks bukti ROI tersegel; dapat diulang, bukan CSV jawaban')
    p.add_argument('--roi-max-sel',type=int,default=2); p.add_argument('--roi-max-inferensi',type=int,default=4)
    a=p.parse_args(); run_batch(a.manifest,a.run_dir,a.limit,a.cache_dir,roi_mode=a.roi_mode,bukti_tambahan=a.bukti_tambahan,roi_max_sel=a.roi_max_sel,roi_max_inferensi=a.roi_max_inferensi)
