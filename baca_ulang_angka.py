"""OCR lokal ROI angka bermasalah; hasil baseline dan koordinat asal tetap utuh."""
import argparse,copy,math,os,re,time
from pathlib import Path
from batch_common import read_json,atomic_json,digest,seal
from ekstrak_faktur_digital import angka
from normalisasi_angka import analisis_angka
from uji_ocr_gambar import CONFIG


def kandidat_angka(raw,old,locale):
    r=raw.get('res',raw); valid=[]
    for text,score,poly in zip(r.get('rec_texts',[]),r.get('rec_scores',[]),r.get('rec_polys',[])):
        try:
            if analisis_angka(text,locale)['wajib_review']:continue
            value=angka(text,locale)
        except ValueError: continue
        tail=re.search(r'[.,](\d{2})\s*%?$',old)
        scale_ok=not tail or value.as_tuple().exponent==-2
        if (score>=.90 and re.sub(r'\D','',text)==re.sub(r'\D','',old) and scale_ok and
            text.strip().startswith('-')==old.strip().startswith('-') and ('%' in text)==('%' in old)):
            valid.append({'teks':text,'nilai':format(value,'f'),'skor':score,'poligon_crop':poly})
    # Tidak memilih nominal dari beberapa deteksi, atau memakai hitungan sebagai acuan.
    return valid[0] if len(valid)==1 and len(r.get('rec_texts',[]))==1 else None


def kandidat_sel_persen(raw, record):
    """Baca ulang satu sel persen, termasuk digit hilang; bukan koreksi rumus.

    Jalur ini terpisah dari kandidat_angka yang tetap mewajibkan digit sama.
    Sel/transformasi ditetapkan dari posisi tabel sebelum melihat jawaban.
    """
    r=raw.get('res',raw)
    if record.get('mode')!='sel_persen' or record.get('kolom')!='diskon_item_persen':return None
    if record.get('skala') not in (1,3):return None
    if any(len(r.get(k,[]))!=1 for k in ('rec_texts','rec_scores','rec_polys','dt_polys')):return None
    text=r['rec_texts'][0].strip();score=r['rec_scores'][0]
    if not re.fullmatch(r'(?:0|[1-9]\d{0,2})(?:[.,]\d{1,4})?\s*%',text) or not .90<=score<=1:return None
    try:value=angka(text,record['locale'])
    except ValueError:return None
    if not 0<=value<=100:return None
    poly=r['rec_polys'][0];det=r['dt_polys'][0]
    if poly!=det or len(poly)!=4:return None
    box=record['crop_bbox_sumber'];scale=record['skala'];cell=record['bbox_sel_sumber']
    size=record['ukuran_sumber']
    if not (0<=box[0]<box[2]<=size[0] and 0<=box[1]<box[3]<=size[1]):return None
    if not (0<=cell[0]<cell[2]<=size[0] and 0<=cell[1]<cell[3]<=size[1]):return None
    if any(not (0<=x<=(box[2]-box[0])*scale and 0<=y<=(box[3]-box[1])*scale) for x,y in poly):return None
    source=[[float(x)/scale+box[0],float(y)/scale+box[1]] for x,y in poly]
    if any(not (cell[0]<=x<=cell[2] and cell[1]<=y<=cell[3]) for x,y in source):return None
    bbox=[min(x for x,y in source),min(y for x,y in source),max(x for x,y in source),max(y for x,y in source)]
    if bbox[0]>=bbox[2] or bbox[1]>=bbox[3]:return None
    return {'teks':text,'nilai':format(value,'f'),'skor':score,'poligon_crop':poly,
            'poligon_sumber':source,'bbox_sumber':bbox}


def run(requests_path,out):
    out=Path(out); out.mkdir(parents=True,exist_ok=False)
    requests=read_json(requests_path); os.environ['PADDLE_PDX_MODEL_SOURCE']='BOS'
    from PIL import Image
    from paddleocr import PaddleOCR
    t=time.perf_counter(); model=PaddleOCR(**CONFIG); load=time.perf_counter()-t; records=[]
    for n,req in enumerate(requests,1):
        assert digest(req['path_sumber'])==req['hash_sumber'],'Hash sumber berubah'
        folder=out/f'{n:03d}'; folder.mkdir(); b=req['segmen']['bbox']
        with Image.open(req['path_sumber']) as image:
            box=[max(0,math.floor(b[0])-3),max(0,math.floor(b[1])-3),min(image.width,math.ceil(b[2])+3),min(image.height,math.ceil(b[3])+3)]
            crop=image.crop(box).convert('RGB'); crop.save(folder/'crop_asli.png')
            # Satu varian pembesaran untuk huruf/angka kecil; tidak mengubah pemisah.
            scale=3; processed=crop.resize((crop.width*scale,crop.height*scale),Image.Resampling.LANCZOS); processed.save(folder/'crop_ocr.png')
        t=time.perf_counter(); predictions=list(model.predict(input=str(folder/'crop_ocr.png'))); elapsed=time.perf_counter()-t
        if len(predictions)!=1: raise ValueError('Hasil ROI tidak tunggal')
        predictions[0].save_to_json(str(folder/'ocr_mentah.json')); raw=read_json(folder/'ocr_mentah.json'); candidate=kandidat_angka(raw,req['segmen']['teks_asli'],req['locale'])
        if candidate:
            candidate['poligon_sumber']=[[x/scale+box[0],y/scale+box[1]] for x,y in candidate['poligon_crop']]
        record={**req,'crop_bbox_sumber':box,'skala':scale,'satuan':'piksel','transformasi_balikan':'x_sumber=x_crop/3+x0; y_sumber=y_crop/3+y0',
                'metode':'OCR ulang lokal satu ROI, bukan koreksi tanda baca atau hitung balik; digit harus tetap, satu segmen angka valid, skor >= 0.90',
                'konfigurasi':CONFIG,'kandidat':candidate,'status':'kandidat_ocr_bukan_verifikasi_manusia' if candidate else 'belum_berhasil',
                'inferensi_detik':elapsed,'raw_json':str((folder/'ocr_mentah.json').resolve()),'crop_asli':str((folder/'crop_asli.png').resolve()),'crop_ocr':str((folder/'crop_ocr.png').resolve()),'sha256_sumber_sesudah':digest(req['path_sumber'])}
        records.append(record); atomic_json(folder/'bukti.json',record); seal(folder)
        atomic_json(out/'hasil.json',{'muat_model_detik':load,'hasil':records,'selesai':len(records)==len(requests)})
        print(n,req['kolom'],req['segmen']['teks_asli'],'=>',candidate['teks'] if candidate else 'kosong',flush=True)
    seal(out)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--requests',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args();run(a.requests,a.output_dir)
