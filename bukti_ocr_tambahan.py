"""Pemakaian ulang bukti OCR crop/sel tersegel; tidak memuat model atau acuan AI."""
import copy
import math
import re
from decimal import Decimal
from pathlib import Path
from PIL import Image
from batch_common import read_json, digest, complete
from uji_ocr_gambar import CONFIG, buat_segmen
from normalisasi_angka import analisis_angka
from baca_ulang_angka import kandidat_angka


def require(ok, reason):
    if not ok:raise ValueError(reason)


def locale_sel(text, fallback):
    if re.fullmatch(r'-?\d{1,3}(?:\.\d{3}){2,}',text):return 'id_ID','Grup titik homogen tiga digit di sel ROI.'
    if re.fullmatch(r'-?\d{1,3}(?:,\d{3}){2,}',text):return 'en_US','Grup koma homogen tiga digit di sel ROI.'
    return fallback or 'en_US','Locale sel baseline; grammar nonstandar tetap meminta review.'


def literal_roi_tunggal(text,request):
    if request.get('pemicu_konflik_pemisah_tabel'):
        # Tidak mengonversi titik yang sama menjadi ribuan. Bukti pembacaan
        # baru harus benar-benar menyatakan koma; digit/tanda tetap diperiksa.
        return re.fullmatch(r'-?\d{1,3},\d{3}',text) is not None
    if not request.get('pemicu_pemisah_campuran_tiga_digit'):return True
    # Baca ulang tanda baca, bukan memilih antara dua skala token awal.
    # Tiga desimal/mixed yang sama tidak menjadi kandidat numerik pasti.
    return re.fullmatch(r'-?(?:\d+|\d{1,3}(?:,\d{3}){2,}|\d{1,3}(?:\.\d{3}){2,})',text) is not None

def kandidat(raw, request):
    r=raw.get('res',raw)
    if any(len(r.get(k,[]))!=1 for k in ('rec_texts','rec_scores','rec_polys')):return None
    text=r['rec_texts'][0].strip();score=r['rec_scores'][0]
    if not literal_roi_tunggal(text,request):return None
    if not math.isfinite(score) or score<.90 or not re.fullmatch(r'-?[\d.,]+',text):return None
    loc,why=locale_sel(text,request.get('locale_baseline'));a=analisis_angka(text,loc)
    if a['status'] in ('ambigu','rusak','kosong') or a['nilai']=='':return None
    baseline=request['teks_baseline']
    if request.get('geometri_sel',{}).get('jenis')=='uang_persen_tanpa_grid':
        from geometri_uang_persen import POLA
        ss=request.get('bukti_baseline',{}).get('segmen',[])
        if len(ss)!=1:return None
        joined=ss[0]['teks_asli'].strip();match=POLA.fullmatch(joined)
        if match is None or baseline not in ('',joined):return None
        # Pemisahan TEKS menjaga digit; batas ROI dibuktikan layout/piksel dan
        # direkonstruksi validasi_record. Bbox region lama tidak dibagi menjadi kata.
        baseline=match[1]
    without_token=(request['kolom']=='jumlah' or (request['kolom']=='total_akhir' and request.get('geometri_sel',{}).get('jenis')=='ringkasan')) and not baseline
    if without_token:
        if request['kolom']=='jumlah' and not re.fullmatch(r'-?\d+(?:[.,]\d{1,2})?',text):return None
        c={'teks':text,'nilai':a['nilai'],'skor':score,'poligon_crop':r['rec_polys'][0]}
    else:
        c=kandidat_angka(raw,baseline,loc)
        if c is None and a['status']=='normalisasi_terstruktur' and a['wajib_review']:
            if not re.fullmatch(r'-?\d{1,3}(?:[.,]\d{3})+\.\d{2}',text):return None
            if re.sub(r'\D','',text)!=re.sub(r'\D','',baseline):return None
            c={'teks':text,'nilai':a['nilai'],'skor':score,'poligon_crop':r['rec_polys'][0]}
        if c is None:return None
    if baseline and text.startswith('-')!=baseline.strip().startswith('-'):return None
    return {**c,'locale':loc,'analisis_normalisasi':a,'bukti_format_sel':why,
            'status':'interpretasi_terstruktur_perlu_review' if a['wajib_review'] else 'kandidat_raw_sel_bukan_verifikasi_manusia'}


def validasi_record(record, check_models=True):
    """Periksa segel, piksel sumber, raw dan transformasi, bukan skor saja."""
    r=record;rawpath=Path(r['raw_json'])
    require(complete(rawpath.parent) and read_json(rawpath.parent/'bukti.json')==r,'Segel/bukti ROI tidak lengkap')
    require(r['konfigurasi']==CONFIG,'Konfigurasi OCR ROI berbeda')
    actual=copy.deepcopy(CONFIG)
    for key,name in [('text_detection_model_dir',CONFIG['text_detection_model_name']),('text_recognition_model_dir',CONFIG['text_recognition_model_name'])]:
        actual[key]=str(Path.home()/'.paddlex/official_models'/name)
    require(r['konfigurasi_aktual']==actual,'Konfigurasi aktual/model lokal ROI berbeda')
    if check_models:
        weights=r['hash_model'];require(bool(weights),'Hash model tidak tersedia')
        for folder in (Path(actual['text_detection_model_dir']),Path(actual['text_recognition_model_dir'])):
            require(all(str(folder/n) in weights for n in ('inference.json','inference.pdiparams','inference.yml')),'Hash model belum lengkap')
        require(all(digest(p)==h for p,h in weights.items()),'Hash model ROI berbeda')
    require(r['satuan']=='piksel' and r['nomor_halaman']>=1,'Halaman/satuan ROI tidak valid')
    require(r['permintaan']=={k:r[k] for k in r['permintaan']},'Metadata permintaan ROI berbeda')
    require(digest(r['path_sumber'])==r['hash_sumber']==r['sha256_sumber_sesudah'],'Hash sumber ROI berbeda')
    if r.get('geometri_sel'):
        from roi_sel_ocr import validasi_permintaan
        validasi_permintaan(r['permintaan'])
    require(digest(r['crop_asli'])==r['sha256_crop_asli'] and digest(r['crop_ocr'])==r['sha256_crop_ocr'],'Hash crop ROI berbeda')
    with Image.open(r['path_sumber']) as im:
        require(getattr(im,'n_frames',1)==1 and im.getexif().get(274,1)==1,'Frame/rotasi sumber belum didukung')
        require(r['nomor_halaman']==1,'ROI sumber gambar satu frame hanya mendukung halaman 1; PDF/render memerlukan metadata tersendiri')
        require(list(im.size)==r['ukuran_sumber'],'Ukuran sumber berbeda')
        x0,y0,x1,y1=r['crop_bbox_sumber'];w,h=im.size
        require(all(isinstance(v,int) for v in (x0,y0,x1,y1)) and 0<=x0<x1<=w and 0<=y0<y1<=h,'ROI di luar sumber')
        crop=im.crop((x0,y0,x1,y1)).convert('RGB')
    with Image.open(r['crop_asli']) as im:require(im.size==crop.size and im.convert('RGB').tobytes()==crop.tobytes(),'Piksel crop berbeda dari sumber')
    scale=3 if r['varian']=='resize3x' else 1
    require(r['varian'] in ('asli','resize3x','otsu') and scale==r['skala'],'Varian/skala tidak didukung')
    expected=crop
    if scale==3:expected=crop.resize((crop.width*3,crop.height*3),Image.Resampling.LANCZOS)
    elif r['varian']=='otsu':
        import cv2,numpy as np
        t,a=cv2.threshold(np.asarray(crop.convert('L')),0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        require(t==r['otsu_threshold'],'Threshold Otsu berbeda');expected=Image.fromarray(a).convert('RGB')
    with Image.open(r['crop_ocr']) as im:require(im.size==expected.size and im.convert('RGB').tobytes()==expected.tobytes(),'Piksel varian/transformasi berbeda')
    require(r['transformasi_balikan']=={'x':'x_crop/skala+x0','y':'y_crop/skala+y0','skala':scale,'x0':x0,'y0':y0},'Transformasi balikan tidak cocok')
    raw=read_json(rawpath);res=raw.get('res',raw)
    require(Path(res['input_path']).resolve()==Path(r['crop_ocr']).resolve(),'Raw merujuk input lain')
    segments=buat_segmen(raw,Path(r['path_sumber']),expected.width,expected.height,.85)
    for s in segments:
        s['poligon_crop']=copy.deepcopy(s['poligon']);s['bbox_crop']=copy.deepcopy(s['bbox'])
        require(all(0<=x<=expected.width and 0<=y<=expected.height for x,y in s['poligon_crop']),'Polygon di luar crop')
        s['poligon']=[[x/scale+x0,y/scale+y0] for x,y in s['poligon']]
        s['bbox']=[min(x for x,y in s['poligon']),min(y for x,y in s['poligon']),max(x for x,y in s['poligon']),max(y for x,y in s['poligon'])]
    require(segments==r['segmen_sumber'],'Segmen/bbox balikan tidak sama dengan raw')
    if r['jenis']=='angka':
        c=kandidat(raw,r['permintaan']);saved=r['kandidat']
        require((c is None)==(saved is None),'Keputusan kandidat historis tidak cocok')
        if c:
            # Deskripsi aturan historis bukan nilai; periksa seluruh komponen substantif.
            require(all(saved.get(k)==c[k] for k in ('teks','nilai','skor','poligon_crop','locale','analisis_normalisasi','status')),'Kandidat tidak sama dengan pembacaan raw')
            require(saved.get('poligon_sumber')==[[x/scale+x0,y/scale+y0] for x,y in c['poligon_crop']],'Polygon kandidat tidak cocok')
    else:require(r['jenis']=='isi' and r['kandidat'] is None,'Jenis bukti tambahan tidak dikenali')
    return raw


def baca_indeks(path):
    """Indeks eksperimen lengkap; hanya metadata/raw, tanpa laporan/acuan/CSV."""
    p=Path(path);index=read_json(p)
    require(complete(p.parent) and index.get('selesai') and index.get('hasil'),'Indeks tambahan belum lengkap/checksum gagal')
    from batch_faktur import ocr_config
    version_path=p.parent.parent/'verifikasi_akhir.json'
    require(version_path.is_file() and complete(version_path.parent),'Metadata versi/segel eksperimen tidak tersedia')
    versions=read_json(version_path)['versi_paket']
    require(all(versions.get(k)==v for k,v in ocr_config()['paket'].items()),'Versi paket bukti tambahan berbeda')
    seen=set();models=None;result=[]
    for task in index['hasil']:
        req=task['permintaan'];key=(req['hash_sumber'],req['nomor_halaman'],req['jenis'],req['kolom'],req['item'])
        require(key not in seen,'Target bukti tambahan berulang');seen.add(key)
        require(task['varian'],'Bukti varian tidak tersedia')
        names=[]
        for record in task['varian']:
            require(record['permintaan']==req,'Varian berbeda permintaan')
            require(models is None or models==record['hash_model'],'Varian berbeda hash model')
            validasi_record(record,check_models=models is None);models=record['hash_model'];names.append(record['varian'])
        expected=['asli','resize3x','otsu'] if req.get('eksperimen_semua_varian') else ['asli','resize3x'][:len(names)]
        require(names==expected,'Urutan/kelengkapan varian berbeda')
        result.append(copy.deepcopy(task))
    require(sum(len(t['varian']) for t in result)==index['inferensi_dimulai'],'Jumlah inferensi/bukti tidak lengkap')
    return result


def pilih_varian(task, baseline_text, baseline_locale, candidate_validator=None):
    """Konsensus raw; tidak memilih menurut aritmetika, acuan atau skor tertinggi."""
    seen=set();candidates=[]
    for r in task['varian']:
        raw=read_json(r['raw_json']);res=raw.get('res',raw)
        req={**r['permintaan'],'teks_baseline':baseline_text,'locale_baseline':baseline_locale}
        if len(res['rec_texts'])==1:
            text=res['rec_texts'][0].strip()
            if re.fullmatch(r'-?[\d.,]+',text) and literal_roi_tunggal(text,req):
                loc,_=locale_sel(text,baseline_locale);a=analisis_angka(text,loc)
                if a['status'] not in ('ambigu','rusak','kosong') and a['nilai']!='':seen.add(Decimal(a['nilai']))
        c=kandidat(raw,req)
        if c:candidates.append((r,c))
    if len(seen)>1:return None,'Raw antarvarian bertentangan; sel tetap kosong.'
    if not candidates:return None,'Tidak ada kandidat yang memenuhi grammar/posisi/digit sel baseline.'
    if len({Decimal(c['nilai']) for _,c in candidates})!=1:return None,'Kandidat antarvarian bertentangan.'
    if candidate_validator is not None:
        accepted=[];held=[]
        for r,c in candidates:
            try:
                candidate_validator(c)
                accepted.append((r,c))
            except ValueError as error:
                held.append(r['varian']+': '+str(error))
        if not accepted:return None,'Dukungan format kandidat belum cukup: '+'; '.join(held)
        # Konflik seluruh raw sudah diperiksa SEBELUM filter dukungan format.
        return accepted[0],'Urutan varian dengan bukti format cukup; seluruh raw tidak bertentangan. Ditahan: '+'; '.join(held)
    return candidates[0],'Urutan asli, resize3x, Otsu; nominal tidak bertentangan.'


def usulan_crop_ruang_kosong(source):
    """Opsi geometris untuk ekor putih gambar panjang, bukan koordinat pelanggan."""
    import numpy as np
    with Image.open(source) as im:
        if getattr(im,'n_frames',1)!=1 or im.getexif().get(274,1)!=1:return None
        w,h=im.size
        if h<3*w:return None
        a=np.asarray(im.convert('L'));occupied=np.flatnonzero((a<235).mean(axis=1)>.003)
        if not len(occupied):return None
        end=min(h,int(occupied[-1])+1+max(32,round(w*.05)))
        if end>h*.65 or float((a[end:]>=235).mean())<.995:return None
        return {'bbox':[0,0,w,end],'ukuran_sumber':[w,h],'akhir_isi':int(occupied[-1])+1,
                'margin':max(32,round(w*.05)),'fraksi_putih_ekor':float((a[end:]>=235).mean()),
                'aturan':'h>=3*w, fraksi gelap baris>0.003 pada ambang235, margin5% lebar/min32, ekor>=35% tinggi dan putih>=99.5%; tidak menjalankan OCR.'}


def data_crop_isi(data, task):
    """Pilih hanya bukti crop lengkap yang membuang ekor putih; baseline tetap."""
    req=task['permintaan'];r=task['varian'][0];validasi_record(r)
    proposal=usulan_crop_ruang_kosong(req['path_sumber'])
    require(proposal is not None,'Crop fallback tidak dibuktikan oleh ruang kosong gambar panjang')
    x0,y0,x1,y1=req['crop_bbox_sumber'];w,h=req['ukuran_sumber']
    require(x0==y0==0 and x1==w and proposal['bbox'][3]<=y1<=h*.65,'Crop isi memotong isi atau bukan ekor putih')
    require(data['sha256_sumber_sebelum']==req['hash_sumber']==data['sha256_sumber_sesudah'],'Hash crop/baseline berbeda')
    require(req['nomor_halaman']==data.get('halaman_dokumen',1),'Halaman crop/baseline berbeda')
    with Image.open(req['path_sumber']) as im:
        import numpy as np
        require(float((np.asarray(im.convert('L'))[y1:]>=235).mean())>=.995,'Ekor yang dibuang tidak putih')
    require(list(data['ukuran_gambar'].values())==[w,h] or data['ukuran_gambar']=={'lebar':w,'tinggi':h},'Ukuran baseline/crop berbeda')
    new=copy.deepcopy(data);new['segmen']=copy.deepcopy(r['segmen_sumber'])
    new['bukti_crop_isi']=copy.deepcopy(r);new['usulan_crop_ruang_kosong']=proposal
    return new


def terapkan_sel(inv, data, tasks, candidate_validator=None):
    """Isi hanya sel kosong yang cocok dengan slot dan bukti parser baseline."""
    from ekstrak_faktur_ocr import PembacaOCR
    from nilai_review_faktur import pemetaan_sel, NUM_DOC, NUM_ITEM
    applied=[];issues=[]
    for task in tasks:
        req=task['permintaan']
        if req['jenis']!='angka' or req['hash_sumber']!=inv['hash_sumber'] or req['nomor_halaman']!=int(inv['nilai']['halaman_sumber']):continue
        try:
            for r in task['varian']:validasi_record(r)
            item=req['item'];col=req['kolom'];allowed=NUM_ITEM if item else NUM_DOC
            require(col in allowed,'Kolom ROI bukan kolom numerik skema')
            targets=[t for t in inv['item'] if t['nilai']['nomor_baris_item']==item] if item else [inv]
            require(len(targets)==1,'Tidak ada satu baris item pasangan ROI')
            obj=targets[0];old=copy.deepcopy(obj['kolom'][col])
            if obj['nilai'][col]!='':
                issues.append({'kolom':col,'item':item,'alasan':'Nilai terisi dipertahankan; bukti baru tidak menggantikannya.'});continue
            # Metadata sel/ID bukan jawaban: verifikasi kembali bukti lokasi/raw.
            require(req['bukti_baseline']['segmen']==old.get('segmen',[]),'Bukti sel parser berbeda dari segmen baseline permintaan')
            selected,reason=pilih_varian(task,old.get('teks_diambil',''),old.get('locale_pemakaian'),candidate_validator)
            if not selected:issues.append({'kolom':col,'item':item,'alasan':reason});continue
            r,c=selected;segs=[copy.deepcopy(s) for s in r['segmen_sumber'] if s['asal_hasil']=='pengenalan']
            require(len(segs)==1,'ROI bukan satu segmen angka')
            segs[0]['id_segmen']='ROI_'+req['key']+'_'+r['varian']
            labels=[{'id_segmen':s['id_segmen'],'teks_asli':s['teks_asli'],'bbox':s['bbox_asal'],
                     'poligon':s['poligon_asal'],'nomor_halaman':s['halaman'],'skor_keyakinan':s['skor'],'tingkat_segmen':s['tingkat_segmen']}
                    for s in old.get('segmen',[]) if re.search(r'[A-Za-z]',s['teks_asli'])]
            reader=PembacaOCR(data);reader.locale=c['locale']
            ev=reader.nilai(c['teks'],labels+segs,old['metode']+'; bukti OCR ROI sel kembali ke sumber',kind='angka')
            require(ev['nilai']==c['nilai'],'Hasil normalisasi bukti/reader berbeda')
            if old.get('judul_kolom'):ev['judul_kolom']=copy.deepcopy(old['judul_kolom'])
            ev['riwayat_baseline']=old;ev['bukti_ROI_tambahan']=copy.deepcopy(r)
            ev['bukti_format_sel']={'locale':c['locale'],'aturan':c['bukti_format_sel'],'teks_raw_sel':c['teks'],
                                    'status':c['status'],'ruang_lingkup':'Satu sel/kolom, bukan locale halaman.'}
            trial=copy.deepcopy(obj);trial['kolom'][col]=ev
            if item:require(pemetaan_sel(inv,trial,col),'ROI tidak cocok kolom/baris item')
            else:
                require(len(labels)==1,'Label ringkasan tidak tunggal')
                b=segs[0]['bbox'];l=labels[0]['bbox']
                require(b[0]>l[2] and abs((b[1]+b[3]-l[1]-l[3])/2)<=.6*max(b[3]-b[1],l[3]-l[1]),'ROI tidak sejajar label ringkasan')
            obj['kolom'][col]=ev;obj['nilai'][col]=ev['nilai']
            applied.append({'item':item,'kolom':col,'lama':'','baru':ev['nilai'],'raw_json':r['raw_json'],'alasan':reason})
        except (ValueError,KeyError,IndexError) as error:
            issues.append({'kolom':req.get('kolom'),'item':req.get('item'),'alasan':str(error)})
    # Subtotal turunan dihitung hanya ketika subtotal item lengkap setelah ROI.
    # Tidak pernah mengganti subtotal tercetak atau memilih hasil OCR dari rumus.
    if any(t['kolom']=='subtotal_item' and t['item'] for t in applied):
        ev=inv['kolom']['subtotal_faktur'];subs=[it['nilai']['subtotal_item'] for it in inv['item']]
        if subs and all(v!='' for v in subs) and (ev.get('alasan_kosong')=='komponen_subtotal_item_tidak_lengkap' or 'akumulasi subtotal_item' in ev.get('metode','')):
            original=copy.deepcopy(ev);v=format(sum((Decimal(s) for s in subs),Decimal(0)),'f')
            new=copy.deepcopy(original);new.update(nilai=v,alasan_kosong=None,
                metode='Fallback global subtotal_faktur dari akumulasi subtotal_item setelah bukti ROI',
                catatan_asal='Nilai turunan dari item lengkap; bukan subtotal tercetak.',riwayat_sebelum_ROI=original,
                komponen_turunan=[{'nomor_item':it['nilai']['nomor_baris_item'],'nilai':it['nilai']['subtotal_item'],'bukti':it['kolom']['subtotal_item']} for it in inv['item']])
            inv['kolom']['subtotal_faktur']=new;inv['nilai']['subtotal_faktur']=v
            applied.append({'item':'','kolom':'subtotal_faktur','lama':original['nilai'],'baru':v,'asal':'Akumulasi subtotal item lengkap, bukan tercetak.'})
    return applied,issues
