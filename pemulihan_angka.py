"""Fallback angka umum setelah pemetaan: konteks kolom, cache ROI, OCR terbatas.

Tidak membaca CSV jawaban/acuan AI. Default cache-only; mode auto diaktifkan
eksplisit oleh CLI. Hanya gambar satu frame berorientasi asli, sel uang kosong
yang terpetakan dan mempunyai digit baseline. Tidak OCR .000 yang sudah jelas
terbaca tetapi skala belum pasti. Cache penuh dan bukti historis tidak ditimpa.
"""
import copy
import math
import os
import re
import time
from pathlib import Path
from uuid import uuid4
from PIL import Image
from batch_common import atomic_json, complete, digest, fingerprint, read_json, seal, lock_run
from bukti_ocr_tambahan import baca_indeks, validasi_record, kandidat, pilih_varian, terapkan_sel
from profil_format_uang import kumpulkan, profil, keputusan, konteks_roi
from uji_ocr_gambar import CONFIG, buat_segmen

VERSI = 'pemulihan-angka-v8-konflik-pemisah'
# Anggaran persisten bukan versi parser. Tetap membaca/menulis ledger v1:
# upgrade pemicu tidak memberi jatah baru atau mengulang percobaan gagal.
VERSI_LEDGER = 'pemulihan-angka-v1'


def alasan_pemicu_roi(text, locale):
    """Hanya digit uang dengan spasi hilang/pemisah rusak; bukan penentu nilai."""
    from normalisasi_angka import analisis_angka
    text = text.strip()
    if not re.fullmatch(r'-?\d[\d., ]*\d', text):
        raise ValueError('ROI memerlukan region digit uang, bukan label/persen/teks campuran')
    if ' ' in text:
        return 'Pemisah OCR hilang/spasi pada sel uang terpetakan'
    a = analisis_angka(text, locale)
    if a['nilai'] != '' or a['status'] != 'rusak':
        raise ValueError('ROI tidak mengulang token valid atau locale ambigu (.000); perlu bukti konteks')
    if re.search(r'[.,]', text):
        return 'Susunan pemisah OCR rusak pada sel uang terpetakan'
    raise ValueError('Tidak ada pola pemisah rusak yang layak dibaca ulang')


def metadata_model():
    actual = copy.deepcopy(CONFIG); weights = {}
    for key, name in [('text_detection_model_dir', CONFIG['text_detection_model_name']),
                      ('text_recognition_model_dir', CONFIG['text_recognition_model_name'])]:
        folder = Path.home()/'.paddlex/official_models'/name
        actual[key] = str(folder)
        for filename in ('inference.json', 'inference.pdiparams', 'inference.yml'):
            path = folder/filename
            if not path.is_file():
                raise ValueError('Model lokal belum lengkap; fallback tidak mengunduh: '+str(path))
            weights[str(path)] = digest(path)
    return actual, weights


def request_roi(cell, inv, data, source, cache_path=None):
    """Region asli dipertahankan, tidak membuat bbox kata dari region OCR."""
    quantity=cell['kolom']=='jumlah' and not cell['bukti'].get('segmen')
    summary=cell['kolom']=='total_akhir' and cell['item']==''
    from geometri_uang_persen import region_campuran
    joined=region_campuran(cell)
    if cell['nilai_lama'] != '' or not cell['header_valid'] or (not cell['pemetaan_valid'] and not quantity and not joined):
        raise ValueError('ROI memerlukan sel uang kosong dengan header/pemetaan sah')
    text = cell['bukti']['segmen'][0]['teks_asli'].strip() if joined else cell['teks'].strip()
    repair3=bool(cell['kolom'] in ('harga_satuan','subtotal_item') and cell['pemetaan_valid'] and
        len(cell['bukti'].get('segmen',[]))==1 and re.fullmatch(r'-?\d{1,3}(?:,\d{3})+\.\d{3}',text))
    # Membaca pemisah yang bertentangan antar sel uang terpetakan satu baris
    # tidak berarti menyamakan locale atau menyalin nilai kolom tetangga.
    conflicting=[]
    if (cell['kolom'] in ('harga_satuan','subtotal_item') and cell['pemetaan_valid']
        and len(cell['bukti'].get('segmen',[]))==1 and re.fullmatch(r'-?\d{1,3}\.\d{3}',text)):
        for other in kumpulkan(inv):
            if (other['item']==cell['item'] and other['kolom']!=cell['kolom'] and other['header_valid']
                and other['pemetaan_valid'] and other['nilai_lama']!='' and len(other['bukti'].get('segmen',[]))==1
                and re.fullmatch(r'-?\d{1,3},\d{3}',other['teks'].strip())):
                conflicting.append(copy.deepcopy(other['bukti']))
    from roi_sel_ocr import dukungan_pemisah_campuran,proposal
    mixed=not quantity and dukungan_pemisah_campuran(cell)
    if not quantity and not mixed and not summary and not joined and not repair3 and not conflicting:alasan_pemicu_roi(text, cell['bukti'].get('locale_pemakaian'))
    evidence = cell['bukti']; segments = evidence.get('segmen', [])
    if len(segments) != 1 and not quantity and not summary:
        raise ValueError('ROI otomatis memerlukan satu region sel; tidak membagi region label/nilai')
    with Image.open(source) as im:
        if getattr(im, 'n_frames', 1) != 1 or im.getexif().get(274, 1) != 1:
            raise ValueError('ROI otomatis belum mendukung frame/rotasi')
        w, h = im.size
    if data.get('ukuran_gambar') != {'lebar': w, 'tinggi': h} or data.get('render_dpi'):
        raise ValueError('Koordinat ROI harus piksel gambar asli, bukan render PDF/resize')
    geometry=None
    if joined:
        from geometri_uang_persen import proposal as proposal_region
        roi,geometry=proposal_region(cell,inv,data,source,cache_path)
    elif summary:
        from roi_sel_ocr import proposal_ringkasan
        roi,geometry=proposal_ringkasan(cell,inv,data,source,cache_path)
    elif quantity or mixed:
        roi,geometry=proposal(cell,inv,data,source,cache_path)
    else:
        b = segments[0]['bbox_asal']
        roi = [max(0, math.floor(b[0])-6), max(0, math.floor(b[1])-4),
               min(w, math.ceil(b[2])+6), min(h, math.ceil(b[3])+4)]
        for s in data['segmen']:
            if s['id_segmen'] == segments[0]['id_segmen'] or not re.search(r'\d',s['teks_asli']):continue
            bb=s['bbox']
            if min(bb[2],roi[2])>max(bb[0],roi[0]) and min(bb[3],roi[3])>max(bb[1],roi[1]):
                if not cache_path:raise ValueError('Margin ROI menyentuh region digit lain; perlu pemetaan/crop manual')
                roi,geometry=proposal(cell,inv,data,source,cache_path)
                break
    sh = inv['hash_sumber']
    if digest(source) != sh or data['sha256_sumber_sebelum'] != sh or data['sha256_sumber_sesudah'] != sh:
        raise ValueError('Hash gambar/teks baseline berbeda')
    req={'key': fingerprint({'hash': sh, 'roi': roi, 'kolom': cell['kolom'], 'item': cell['item']})[:24],
            'jenis': 'angka', 'path_sumber': str(Path(source).resolve()), 'hash_sumber': sh,
            'ukuran_sumber': [w,h], 'nomor_halaman': 1, 'crop_bbox_sumber': roi,
            'kolom': cell['kolom'], 'item': cell['item'], 'teks_baseline': text,
            'locale_baseline': evidence.get('locale_pemakaian'), 'bukti_baseline': copy.deepcopy(evidence)}
    if geometry:
        req['geometri_sel']=geometry
        req['jenis_nilai']='jumlah' if quantity else ('uang_ringkasan' if summary else 'uang')
        req['alasan_pemicu']='Total utama tanpa token dengan kolom/band terbukti' if summary else ('Token jumlah hilang dengan grid/band sah' if quantity else ('Pemisah campuran dengan anchor kolom independen' if mixed else 'Margin disesuaikan tanpa memotong region target'))
        if joined:req['alasan_pemicu']='Region uang/persen menyatu; sel harga dibuktikan header, dua baris terpisah dan koridor piksel tanpa grid'
    if repair3:
        req['pemicu_pemisah_campuran_tiga_digit']=True
        req['alasan_pemicu']='Pemisah campuran dengan ekor3digit: OCR hanya menambah bukti literal; kandidat tiga desimal tetap ambigu, bukan otomatis ribuan.'
    if conflicting:
        req['pemicu_konflik_pemisah_tabel']=True
        req['bukti_konflik_pemisah']=conflicting
        req['alasan_pemicu']='Titik tunggal ambigu berhadapan dengan koma pada sel uang lain satu baris; OCR membaca literal, bukan memilih skala dari nilai tetangga.'
    return req


class PemulihanAngka:
    def __init__(self, root, mode='cache', indices=(), max_cells=2, max_inferences=4):
        if mode not in ('off', 'cache', 'auto') or not 1 <= max_cells <= 8 or not 1 <= max_inferences <= 16:
            raise ValueError('Mode/batas ROI tidak valid')
        self.root = Path(root); self.mode = mode; self.indices = list(indices)
        self.max_cells = max_cells; self.max_inferences = max_inferences
        self.historical = None; self.model = None; self.models = None
        self.model_owner = None
        self.inferences = 0; self.load_seconds = 0.; self.reused = 0

    def config(self):
        return {'versi': VERSI, 'mode': self.mode, 'maks_sel_per_dokumen': self.max_cells,
                'maks_inferensi_per_dokumen': self.max_inferences,
                'varian': ['asli', 'resize3x_jika_belum_cukup'],
                'indeks': [{'path':str(Path(p).resolve()), 'sha256':digest(p)} for p in self.indices]}

    def historical_tasks(self):
        if self.historical is None:
            self.historical = [t for p in self.indices for t in baca_indeks(p)]
        return self.historical

    def task(self, req, budget, candidate_validator=None):
        matches = [t for t in self.historical_tasks() if all(t['permintaan'][k] == req[k]
                   for k in ('hash_sumber', 'nomor_halaman', 'kolom', 'item'))]
        if matches:
            for t in matches:
                r = t['permintaan']
                if r['bukti_baseline']['segmen'] != req['bukti_baseline']['segmen'] or r['crop_bbox_sumber'] != req['crop_bbox_sumber']:
                    raise ValueError('Bukti historis tidak cocok geometri/region baseline saat ini')
            values = set()
            for t in matches:
                selected, reason = pilih_varian(t, req['teks_baseline'], req['locale_baseline'])
                if selected: values.add(selected[1]['nilai'])
                else: raise ValueError(reason)
            if len(values) != 1:
                raise ValueError('Indeks historis bertentangan; tidak memilih indeks/score tertinggi')
            self.reused += 1
            return copy.deepcopy(matches[0])
        records = []
        for variant in ('asli','resize3x'):
            record = self.inference(req, variant, budget)
            if record is None:
                break
            records.append(record)
            # Hanya eskalasi jika raw belum memenuhi guard, bukan karena jawaban acuan.
            if record['kandidat'] is not None:
                try:
                    if candidate_validator is not None:candidate_validator(record['kandidat'])
                    break
                except ValueError:
                    # Pembacaan asli ada tetapi dukungan formatter belum cukup.
                    # Coba satu3x, bukan memilih nominal dari skor/rumus/acuan.
                    pass
        return {'permintaan': req, 'varian': records} if records else None

    def inference(self, req, variant, budget):
        self.root.mkdir(parents=True, exist_ok=True)
        with lock_run(self.root):
            return self._inference_locked(req,variant,budget)

    def _inference_locked(self, req, variant, budget):
        from batch_faktur import ocr_config
        if self.models is None: self.models = metadata_model()
        actual, weights = self.models
        key = fingerprint({'sumber':req['hash_sumber'], 'ROI':req['crop_bbox_sumber'],
                           'permintaan':req, 'varian':variant, 'ocr':ocr_config(), 'hash_model':weights})
        if self.root.exists():
            found = []
            for f in sorted(self.root.glob(key+'_*')):
                if complete(f):
                    r = read_json(f/'bukti.json'); validasi_record(r)
                    meta=read_json(f/'progres.json')
                    if meta.get('status')!='selesai' or meta.get('paket')!=ocr_config()['paket']:
                        raise ValueError('Versi/kelengkapan cache ROI berbeda')
                    if r['permintaan'] != req or r['varian'] != variant or r['hash_model'] != weights:
                        raise ValueError('Cache ROI berbeda dari kunci/konfigurasi')
                    found.append(r)
            if found:
                if len({fingerprint(read_json(r['raw_json'])) for r in found}) != 1:
                    raise ValueError('Beberapa cache ROI berbeda; kandidat ditahan')
                self.reused += 1; return found[0]
        if self.mode != 'auto' or budget['inferensi'] >= self.max_inferences:
            return None
        ledger_path=self.root/'progres'/(
            fingerprint({'sumber':req['hash_sumber'],'ocr':ocr_config(),'hash_model':weights,'versi':VERSI_LEDGER})+'.json')
        ledger=read_json(ledger_path) if ledger_path.exists() else {'percobaan':[]}
        attempts=ledger['percobaan']
        if any(a['key']==req['key'] and a['varian']==variant for a in attempts):
            raise ValueError('Percobaan ROI sebelumnya parsial/gagal; tidak mengulang otomatis tanpa bukti lengkap')
        if len(attempts)>=self.max_inferences:
            raise ValueError('Batas inferensi persisten sumber tercapai, termasuk percobaan parsial')
        keys={a['key'] for a in attempts}
        if req['key'] not in keys and len(keys)>=self.max_cells:
            raise ValueError('Batas sel persisten sumber tercapai')
        self.root.mkdir(parents=True, exist_ok=True)
        folder = self.root/(key+'_'+uuid4().hex[:8]); folder.mkdir()
        with Image.open(req['path_sumber']) as im:
            if digest(req['path_sumber']) != req['hash_sumber']:
                raise ValueError('Sumber ROI berubah')
            crop = im.crop(req['crop_bbox_sumber']).convert('RGB')
        crop.save(folder/'crop_asli.png')
        scale = 3 if variant == 'resize3x' else 1
        image = crop if scale == 1 else crop.resize((crop.width*3,crop.height*3),Image.Resampling.LANCZOS)
        image.save(folder/'crop_ocr.png')
        attempts.append({'key':req['key'],'varian':variant,'folder':str(folder),'status':'dimulai'})
        atomic_json(ledger_path,ledger)
        if self.model is None:
            if self.model_owner is not None and self.model_owner.model is not None:
                self.model=self.model_owner.model
            else:
                os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
                from paddleocr import PaddleOCR
                t = time.perf_counter(); self.model = PaddleOCR(**actual); self.load_seconds += time.perf_counter()-t
                if self.model_owner is not None:self.model_owner.model=self.model
        budget['inferensi'] += 1; self.inferences += 1
        atomic_json(folder/'progres.json', {'status':'inferensi_dimulai','permintaan':req,
                    'varian':variant,'versi':VERSI,'paket':ocr_config()['paket']})
        t = time.perf_counter(); pred = list(self.model.predict(input=str(folder/'crop_ocr.png')))
        elapsed = time.perf_counter()-t
        if len(pred) != 1: raise ValueError('ROI tidak menghasilkan satu keluaran')
        rawpath = folder/'ocr_mentah.json'; pred[0].save_to_json(str(rawpath)); raw = read_json(rawpath)
        segments = buat_segmen(raw,Path(req['path_sumber']),image.width,image.height,.85)
        x0,y0,_,_ = req['crop_bbox_sumber']
        for s in segments:
            s['poligon_crop'] = copy.deepcopy(s['poligon']); s['bbox_crop'] = copy.deepcopy(s['bbox'])
            s['poligon'] = [[x/scale+x0,y/scale+y0] for x,y in s['poligon']]
            s['bbox'] = [min(x for x,y in s['poligon']),min(y for x,y in s['poligon']),max(x for x,y in s['poligon']),max(y for x,y in s['poligon'])]
        c = kandidat(raw,req)
        if c: c['poligon_sumber'] = [[x/scale+x0,y/scale+y0] for x,y in c['poligon_crop']]
        r = {**req,'permintaan':req,'varian':variant,'skala':scale,'otsu_threshold':None,
             'satuan':'piksel','transformasi_balikan':{'x':'x_crop/skala+x0','y':'y_crop/skala+y0','skala':scale,'x0':x0,'y0':y0},
             'konfigurasi':CONFIG,'konfigurasi_aktual':actual,'hash_model':weights,'kandidat':c,
             'inferensi_detik':elapsed,'raw_json':str(rawpath.resolve()),'segmen_sumber':segments,
             'crop_asli':str((folder/'crop_asli.png').resolve()),'crop_ocr':str((folder/'crop_ocr.png').resolve()),
             'sha256_crop_asli':digest(folder/'crop_asli.png'),'sha256_crop_ocr':digest(folder/'crop_ocr.png'),
             'sha256_sumber_sesudah':digest(req['path_sumber'])}
        atomic_json(folder/'bukti.json',r)
        atomic_json(folder/'progres.json',{'status':'selesai','versi':VERSI,'paket':ocr_config()['paket'],
                                          'inferensi_detik':elapsed,'permintaan':req,'varian':variant})
        seal(folder); validasi_record(r)
        attempts[-1]['status']='selesai';atomic_json(ledger_path,ledger)
        return r

    def lengkap(self, inv, data, source, cache_path=None):
        """Tidak memodifikasi inv input; sel terisi/struktur/ID menjadi guard."""
        new = copy.deepcopy(inv); events = []; budget = {'sel':0,'inferensi':0}
        if self.mode == 'off': return new, events
        from roi_sel_ocr import kumpulkan_jumlah,kumpulkan_total
        cells = kumpulkan(new); prof = profil(cells)
        candidates=cells+kumpulkan_jumlah(new)+kumpulkan_total(new)
        events.append({'tahap':'profil_format','profil':prof})
        for cell in candidates:
            if cell['nilai_lama'] != '': continue
            decision = keputusan(cell,prof) if cell['kolom'] not in ('jumlah','total_akhir') else {'nilai':'','alasan':'Sel tanpa token hanya melalui geometri dan OCR sel; bukan default.'}
            events.append({'tahap':'interpretasi_format','item':cell['item'],'kolom':cell['kolom'],'keputusan':decision})
            if decision['nilai'] != '':
                obj = next(it for it in new['item'] if it['nilai']['nomor_baris_item'] == cell['item'])
                ev = copy.deepcopy(obj['kolom'][cell['kolom']]); ev['riwayat_baseline'] = copy.deepcopy(ev)
                ev.update(nilai=decision['nilai'],alasan_kosong=None,profil_format_uang=decision,
                          analisis_normalisasi=decision['analisis_normalisasi'],locale_pemakaian=decision['locale'],
                          metode=ev['metode']+'; konteks format satu kolom uang')
                ev['analisis_normalisasi']['wajib_review'] = True
                obj['kolom'][cell['kolom']] = ev; obj['nilai'][cell['kolom']] = ev['nilai']
                continue
            try:
                req = request_roi(cell,new,data,source,cache_path)
                if budget['sel'] >= self.max_cells:
                    raise ValueError('Batas sel per dokumen tercapai; sisa tetap kosong')
                budget['sel'] += 1
                validator = lambda c: konteks_roi(cell,cells,c)
                task = self.task(req,budget,validator)
                if task is None:
                    raise ValueError('Bukti ROI belum tersedia/mode cache atau batas inferensi; tidak mengarang nilai')
                selected, reason = pilih_varian(task,cell['teks'],cell['bukti'].get('locale_pemakaian'),validator)
                if not selected: raise ValueError(reason)
                context = konteks_roi(cell,cells,selected[1])
                applied, issues = terapkan_sel(new,data,[task],validator)
                for a in applied:
                    if a['item']:
                        obj = next(it for it in new['item'] if it['nilai']['nomor_baris_item'] == a['item'])
                        obj['kolom'][a['kolom']]['bukti_format_independen'] = context
                events.append({'tahap':'ROI','item':cell['item'],'kolom':cell['kolom'],'diterapkan':applied,'kendala':issues,'konteks':context,
                               'varian_diperiksa':[{'varian':r['varian'],'raw_json':r['raw_json'],'kandidat':r['kandidat']} for r in task['varian']]})
            except (ValueError,KeyError,IndexError,OSError,RuntimeError,TypeError) as error:
                events.append({'tahap':'ROI_ditahan','item':cell['item'],'kolom':cell['kolom'],'alasan':str(error)})
        # Jangan membiarkan penjumlahan turunan mengubah angka lama yang terisi.
        for old_obj,new_obj in [(inv,new), *zip(inv['item'],new['item'])]:
            for col,v in old_obj['nilai'].items():
                if v != '' and new_obj['nilai'][col] != v:
                    new_obj['nilai'][col] = v; new_obj['kolom'][col] = copy.deepcopy(old_obj['kolom'][col])
                    events.append({'tahap':'perubahan_terisi_diblokir','kolom':col,'nilai_dipertahankan':v})
        from batch_common import validate_invoice
        new['validasi'] = validate_invoice(new)
        for i,it in enumerate(new['item'],1):
            it['validasi']=[c for c in new['validasi'] if c.get('item')==i]
        new['pemulihan_angka'] = {'konfigurasi':self.config(),'kejadian':events,'budget_dokumen':budget}
        return new,events
