"""Baseline OCR lokal satu gambar: hasil asli, koordinat piksel, dan pratinjau ber-ID."""

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path, PureWindowsPath
from uuid import uuid4


DOCS = [
    'https://www.paddlepaddle.org.cn/documentation/docs/install/pip/windows-pip_en.html',
    'https://www.paddleocr.ai/v3.3.2/en/version3.x/installation.html',
    'https://www.paddleocr.ai/v3.3.2/en/version3.x/pipeline_usage/OCR.html',
    'https://www.paddleocr.ai/v3.3.2/en/version3.x/algorithm/PP-OCRv5/PP-OCRv5_multi_languages.html',
]
CONFIG = {
    'device':'cpu', 'lang':'id', 'ocr_version':'PP-OCRv5',
    'text_detection_model_name':'PP-OCRv5_server_det',
    'text_recognition_model_name':'latin_PP-OCRv5_mobile_rec',
    'use_doc_orientation_classify':False, 'use_doc_unwarping':False,
    'use_textline_orientation':False, 'text_rec_score_thresh':0.0,
    'text_det_limit_side_len':1280, 'text_det_limit_type':'max',
    'text_det_thresh':0.3, 'text_det_box_thresh':0.6, 'text_det_unclip_ratio':1.5,
    'enable_mkldnn':False, 'cpu_threads':4,
}


def sha256(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle,'sha256').hexdigest()


def cocokkan_inventaris(image, inventory, source_root):
    with inventory.open(encoding='utf-8-sig',newline='') as handle:
        matches=[row for row in csv.DictReader(handle,strict=True) if row['nama_file']==image.name]
    if len(matches)!=1:
        raise ValueError(f'Nama lengkap {image.name}: {len(matches)} kecocokan. Kandidat: '+repr([r['path_relatif'] for r in matches]))
    row=matches[0]
    rel=PureWindowsPath(row['path_relatif'])
    if rel.is_absolute() or rel.drive or '..' in rel.parts:
        raise ValueError('Path relatif inventaris tidak aman.')
    expected=source_root.resolve().joinpath(*rel.parts).resolve()
    if not expected.is_relative_to(source_root.resolve()) or image.resolve()!=expected or not image.is_file():
        raise ValueError(f'Input tidak cocok dengan lokasi inventaris: {expected}')
    return row


def buat_segmen(raw, source, width, height, threshold):
    """Pertahankan tingkat baris/region model; jangan membuat koordinat kata semu."""
    res=raw.get('res',raw)
    texts,scores,polys=res['rec_texts'],res['rec_scores'],res['rec_polys']
    if not len(texts)==len(scores)==len(polys):
        raise ValueError('Jumlah rec_texts, rec_scores, dan rec_polys tidak konsisten.')
    detections=res['dt_polys']
    def key(poly):
        return tuple(sorted(tuple(float(v) for v in point) for point in poly))
    recognized={key(poly) for poly in polys}
    source_regions=[(text,float(score),poly,'pengenalan') for text,score,poly in zip(texts,scores,polys)]
    source_regions += [('',None,poly,'deteksi_tanpa_pengenalan') for poly in detections if key(poly) not in recognized]
    segments=[]
    for index,(text,score,poly,kind) in enumerate(source_regions,1):
        polygon=[[float(v) for v in point] for point in poly]
        if len(polygon)<3 or any(len(p)!=2 or not all(math.isfinite(v) for v in p) for p in polygon):
            raise ValueError(f'Poligon segmen {index} tidak valid; hasil mentah tetap disimpan.')
        xs,ys=[p[0] for p in polygon],[p[1] for p in polygon]
        box=[min(xs),min(ys),max(xs),max(ys)]
        inside=all(0<=x<=width and 0<=y<=height for x,y in polygon)
        flags=[]
        if not text.strip(): flags.append('teks_kosong')
        if score is None: flags.append('skor_tidak_tersedia')
        elif score<threshold: flags.append('skor_di_bawah_ambang_review')
        if not inside: flags.append('koordinat_di_luar_gambar')
        segments.append({'id_segmen':f'S{index:03d}', 'teks_asli':text, 'skor_keyakinan':score,
                         'poligon':polygon,'bbox':box,'tingkat_segmen':'baris',
                         'catatan_tingkat':'Region/baris hasil detektor; dapat berupa satu atau beberapa kata. Tidak dipecah menjadi bbox kata.',
                         'sumber_gambar':str(source),'nomor_halaman':1,'asal_hasil':kind,
                         'koordinat_dalam_gambar':inside,'perlu_diperiksa':flags})
    return segments


def pratinjau(image, segments, destination):
    from PIL import ImageDraw, ImageFont
    preview=image.copy().convert('RGB')
    draw=ImageDraw.Draw(preview)
    font=ImageFont.load_default(size=9)
    for seg in segments:
        color='#d7191c' if seg['perlu_diperiksa'] else '#007a3d'
        points=[tuple(p) for p in seg['poligon']]
        draw.line(points+[points[0]],fill=color,width=1)
        draw.rectangle(seg['bbox'],outline=color,width=1)
        x0,y0,_,_=seg['bbox']
        label=seg['id_segmen']
        tx=max(0,min(preview.width-30,x0));ty=max(0,y0-10)
        rect=draw.textbbox((tx,ty),label,font=font)
        draw.rectangle(rect,fill='white')
        draw.text((tx,ty),label,font=font,fill=color)
    preview.save(destination)


def evaluasi_teks(segments, reference):
    """Diagnostik pasca-OCR; acuan tidak pernah diteruskan ke model."""
    def normal(text): return re.sub(r'\s+',' ',text).strip().casefold()
    checks=[]
    for expected in reference['teks']:
        matched=[s for s in segments if normal(expected) in normal(s['teks_asli'])]
        nearest=max(segments,key=lambda s:SequenceMatcher(None,normal(expected),normal(s['teks_asli'])).ratio(),default=None)
        checks.append({'jenis':'teks','acuan':expected,'status':'ditemukan' if matched else 'tidak_ditemukan_persis',
                       'id_cocok':[s['id_segmen'] for s in matched],
                       'observasi_terdekat':{'id':nearest['id_segmen'],'teks_ocr_asli':nearest['teks_asli']} if nearest else None,
                       'aturan':'Hanya samakan kapital dan spasi untuk pencarian substring; bukan koreksi teks OCR.'})
    for expected in reference['angka']:
        hits=[]
        for seg in segments:
            for token in re.findall(r'(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\w.])',seg['teks_asli']):
                try:
                    if Decimal(token.replace(',',''))==Decimal(expected): hits.append(seg['id_segmen']);break
                except InvalidOperation: pass
        checks.append({'jenis':'angka','acuan':expected,'status':'token_ditemukan' if hits else 'token_tidak_ditemukan',
                       'id_cocok':hits,'aturan':'Pencarian token angka format koma ribuan/titik desimal dengan Decimal; tanpa memasangkan nilai ke kolom atau item.'})
    return {'asal_acuan':reference['asal'],'pemeriksaan':checks,
            'batasan':'Kecocokan substring/token adalah diagnostik; bukan bukti angka telah dipasangkan dengan kolom yang benar dan bukan akurasi seluruh arsip.'}


def laporan(run, data, diagnostics):
    segments=data['segmen']; bad=[s for s in segments if s['perlu_diperiksa']]
    lines=['# Uji OCR satu gambar — baseline asli','',f"Sumber: `{data['sumber_gambar']}`",'',
           f"Ukuran: {data['ukuran_gambar']['lebar']} × {data['ukuran_gambar']['tinggi']} piksel. Halaman virtual: 1.",'',
           f"Hasil aktual: {len(segments)} segmen tingkat baris/region, {sum(not s['teks_asli'].strip() for s in segments)} teks kosong, {len(bad)} segmen ditandai review.",
           f"Koordinat di luar gambar: {sum(not s['koordinat_dalam_gambar'] for s in segments)}. Tidak ada segmen berskor rendah yang dibuang.",'',
           '## Model, konfigurasi dan waktu','',f"Konfigurasi: `{json.dumps(data['konfigurasi'],ensure_ascii=False)}`",'',
           f"Versi: `{json.dumps(data['versi_paket_utama'])}`",'',f"Waktu (detik): `{json.dumps(data['waktu_detik'])}`",'',
           'Pemuatan model mencakup unduh bila belum ada cache; waktunya terpisah dari inferensi. Inferensi mencakup deteksi, crop internal region, pengenalan, serta pascaproses.','',
           '## Koordinat dan pratinjau','',
           'BBox [x0,y0,x1,y1] adalah persegi pembungkus poligon, dalam piksel gambar sumber; asal kiri atas. Pratinjau berukuran sama dengan sumber. Tidak ada preprocessing, rotasi, crop, atau resize aplikasi. Model dapat melakukan resize tensor/crop internal dan mengembalikan poligon ke koordinat input; ini bukan koordinat point PDF.',
           'Kotak/poligon hijau: tidak ditandai oleh pemeriksaan otomatis. Merah: perlu review. ID pada gambar cocok dengan JSON. Skor adalah keyakinan pengenalan model, bukan jaminan kebenaran; skor deteksi tidak tersedia per segmen pada keluaran pipeline ini.',
           f"Ambang review: skor < {data['ambang_review']}; ambang penyaringan pengenalan model 0.0. Ambang deteksi tetap berlaku, sehingga teks yang tidak dideteksi belum tentu muncul dalam hasil.",'',
           '[Pratinjau dengan ID](pratinjau_ocr.png) · [Hasil mentah](ocr_mentah.json) · [Teks dan koordinat](teks_koordinat_ocr.json)','',
           '## Segmen yang perlu diperiksa','', '| ID | Teks OCR asli | Skor | Alasan |','| --- | --- | ---: | --- |']
    for s in bad:
        text=s['teks_asli'].replace('|','\\|').replace('\n',' ')
        lines.append(f"| {s['id_segmen']} | {text} | {s['skor_keyakinan']} | {', '.join(s['perlu_diperiksa'])} |")
    if not bad: lines.append('| — | Tidak ada yang ditandai; tetap bukan verifikasi manusia | | |')
    lines+=['','## Evaluasi terpisah terhadap acuan visual','',
            'Acuan berikut berasal dari pengguna, bukan teks yang dihasilkan model. Observasi ditampilkan persis dari hasil OCR. Tidak dilakukan koreksi identitas, tanggal, nama, atau angka.','']
    if diagnostics:
        for check in diagnostics['pemeriksaan']:
            lines.append(f"- Acuan `{check['acuan']}`: {check['status']}; ID cocok {check['id_cocok']}.")
            if check.get('observasi_terdekat'):
                near=check['observasi_terdekat'];lines.append(f"  Observasi terdekat {near['id']}: `{near['teks_ocr_asli']}`.")
        lines+=['',diagnostics['batasan']]
    else: lines.append('Acuan evaluasi tidak diberikan.')
    lines+=['','## Keterbatasan dan integritas','',
            'Bagian terlewat tidak dapat dihitung lengkap tanpa transkripsi referensi seluruh gambar. Acuan yang tidak cocok dapat disebabkan salah baca, segmentasi, atau deteksi terlewat; periksa poligon dan gambar asli. Teks kecil di kop, catatan bawah, logo, dan nomor seri memerlukan pemeriksaan visual. Tidak dilakukan varian filter otomatis.',
            'Hasil JSON telah dibaca kembali; jumlah teks/skor/poligon serta batas koordinat diperiksa. Hasil mentah disimpan sebelum penataan skema dan evaluasi. Hash SHA-256 sumber sebelum/sesudah sama.',
            'Tidak membuat faktur.csv/detail_faktur.csv dari gambar, tidak mengubah inventaris/review/hasil PDF, dan tidak memproses seluruh arsip.','',
            '## Dokumentasi resmi','']
    lines += [f'- [Referensi {i+1}]({url})' for i,url in enumerate(DOCS)]
    (run/'laporan_ocr.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def jalankan(image_path,inventory,source_root,output_dir,acuan=None,threshold=.85):
    started=time.perf_counter()
    image_path=image_path.resolve();output_dir=output_dir.resolve()
    if not 0<=threshold<=1: raise ValueError('Ambang review harus 0..1.')
    row=cocokkan_inventaris(image_path,inventory,source_root)
    if output_dir.is_relative_to(source_root.resolve()): raise ValueError('Keluaran tidak boleh di dalam sumber.')
    run=output_dir/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid4().hex[:8])
    run.mkdir(parents=True,exist_ok=False)
    original_hash=sha256(image_path)
    try:
        # BOS adalah host model resmi Paddle; bukan layanan inferensi gambar.
        os.environ['PADDLE_PDX_MODEL_SOURCE']='BOS'
        t=time.perf_counter()
        from PIL import Image
        import paddle
        from paddleocr import PaddleOCR
        import_seconds=time.perf_counter()-t
        with Image.open(image_path) as source:
            orientation=source.getexif().get(274,1)
            if orientation!=1: raise ValueError(f'EXIF orientation {orientation} belum didukung baseline; jangan memetakan koordinat diam-diam.')
            image=source.convert('RGB')
        width,height=image.size
        t=time.perf_counter()
        ocr=PaddleOCR(**CONFIG)
        model_seconds=time.perf_counter()-t
        t=time.perf_counter()
        results=list(ocr.predict(input=str(image_path)))
        infer_seconds=time.perf_counter()-t
        if len(results)!=1: raise ValueError(f'Diharapkan satu hasil gambar, ditemukan {len(results)}.')
        # Simpan keluaran API asli terlebih dahulu tanpa penggantian teks/skor/poligon.
        results[0].save_to_json(str(run/'ocr_mentah.json'))
        raw=json.loads((run/'ocr_mentah.json').read_text(encoding='utf-8'))
        segments=buat_segmen(raw,image_path,width,height,threshold)
        res=raw.get('res',raw)
        versions={name:importlib.metadata.version(name) for name in ['paddleocr','paddlepaddle','paddlex','Pillow','numpy','opencv-contrib-python']}
        data={'versi_skema':1,'metode':'PaddleOCR deteksi dan pengenalan teks lokal pada CPU; baseline gambar asli',
              'sumber_gambar':str(image_path),'inventaris':row,'nomor_halaman':1,
              'ukuran_gambar':{'lebar':width,'tinggi':height},'satuan_koordinat':'piksel',
              'gambar_acuan_koordinat':str(image_path),'transformasi_aplikasi':{'jenis':'identitas','matriks':[[1,0,0],[0,1,0],[0,0,1]],'exif_orientation':orientation},
              'transformasi_internal_model':'Resize/padding detektor dan crop pengenal dikelola PaddleOCR; poligon hasil dipetakan kembali ke gambar input. Tidak membuat gambar preprocessing eksternal.',
              'konfigurasi':dict(CONFIG),'parameter_deteksi_aktual':res.get('text_det_params'),
              'pengaturan_model_aktual':res.get('model_settings'), 'sumber_model':'BOS resmi Paddle (PADDLE_PDX_MODEL_SOURCE=BOS)',
              'versi_paket_utama':versions,'semua_paket':{d.metadata['Name']:d.version for d in importlib.metadata.distributions()},
              'python':sys.version,'executable':sys.executable,'platform':platform.platform(),'perangkat_aktual':paddle.device.get_device(),
              'cuda_dikompilasi':paddle.is_compiled_with_cuda(),
              'sha256_sumber_sebelum':original_hash,'sha256_sumber_sesudah':sha256(image_path),
              'waktu_detik':{'import_paket':import_seconds,'muat_dan_unduh_model':model_seconds,'inferensi':infer_seconds},
              'ambang_review':threshold,'jumlah_deteksi':len(res['dt_polys']),'jumlah_pengenalan':len(res['rec_texts']),
              'segmen':segments,'catatan':'Segmen baris, bukan bbox kata. Teks OCR tidak dinormalisasi atau dikoreksi. Skor bukan verifikasi manusia.'}
        if data['sha256_sumber_sesudah']!=original_hash: raise ValueError('Hash gambar berubah selama proses.')
        pratinjau(image,segments,run/'pratinjau_ocr.png')
        diagnostics=evaluasi_teks(segments,json.loads(acuan.read_text(encoding='utf-8'))) if acuan else None
        data['evaluasi_terpisah']=diagnostics
        data['waktu_detik']['total_hingga_penulisan']=time.perf_counter()-started
        output=run/'teks_koordinat_ocr.json'
        output.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        if json.loads(output.read_text(encoding='utf-8'))!=data: raise ValueError('Verifikasi baca ulang JSON gagal.')
        with Image.open(run/'pratinjau_ocr.png') as check:
            if check.size!=(width,height): raise ValueError('Ukuran pratinjau berbeda dari acuan koordinat.')
        laporan(run,data,diagnostics)
        print(f'Hasil: {len(segments)} segmen; review: {sum(bool(s["perlu_diperiksa"]) for s in segments)}; inferensi: {infer_seconds:.3f} detik')
        print(f'Keluaran: {run}')
        return run
    except Exception as error:
        (run/'error_ocr.json').write_text(json.dumps({'jenis':type(error).__name__,'error':str(error),'traceback':traceback.format_exc()},ensure_ascii=False,indent=2),encoding='utf-8')
        (run/'laporan_ocr.md').write_text(f'# Uji OCR gagal\n\n{type(error).__name__}: {error}\n\nTidak membuat hasil OCR pengganti. Periksa error_ocr.json.\n',encoding='utf-8')
        raise


def main():
    root=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--inventory',type=Path,default=root/'outputs/inventory.csv')
    parser.add_argument('--source-root',type=Path,default=Path(r'C:\Data Klient Zahir'))
    parser.add_argument('--output-dir',type=Path,default=root/'outputs/uji_ocr_gambar')
    parser.add_argument('--acuan',type=Path)
    parser.add_argument('--ambang-review',type=float,default=.85)
    args=parser.parse_args()
    try: jalankan(args.input,args.inventory,args.source_root,args.output_dir,args.acuan,args.ambang_review)
    except Exception as error: parser.exit(1,f'OCR gagal: {type(error).__name__}: {error}\n')


if __name__=='__main__': main()
