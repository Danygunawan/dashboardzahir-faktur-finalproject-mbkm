"""ROI harga/Disc yang menyatu: header, dua baris pembanding dan celah piksel.

Tidak membagi bbox region menjadi bbox kata. ROI adalah area sel yang dibuktikan
oleh layout; koordinat teks target hanya berasal dari inferensi ROI berikutnya.
"""
import copy,math,re,statistics
from pathlib import Path
from PIL import Image
from batch_common import complete,digest,read_json

VERSI='geometri-uang-persen-v1'
JENIS='uang_persen_tanpa_grid'
POLA=re.compile(r'(-?\d[\d.,]*)(?:\s+)(\d+(?:[.,]\d+)?\s*%)')

def region_campuran(cell):
    ss=cell.get('bukti',{}).get('segmen',[])
    return (cell.get('kolom')=='harga_satuan' and len(ss)==1 and
            POLA.fullmatch(ss[0]['teks_asli'].strip()) is not None)

def proposal(cell,inv,data,source,cache_path):
    import numpy as np
    from uji_ocr_gambar import CONFIG
    def require(ok,msg):
        if not ok:raise ValueError(msg)
    require(region_campuran(cell),'Bukan satu region harga dan persen yang menyatu')
    require(cache_path is not None,'Geometri tanpa grid memerlukan cache tersegel')
    cp=Path(cache_path).resolve()
    # Jalur batch memperkaya inventaris lokasi saat runtime. Metadata lokasi itu
    # bukan keluaran OCR/geometri. Seluruh isi lain, termasuk segmen/config/ukuran
    # dan hash gambar, tetap harus sama dengan cache tersegel.
    cached=read_json(cp)
    require(complete(cp.parent) and {k:v for k,v in cached.items() if k!='inventaris'}==
            {k:v for k,v in data.items() if k!='inventaris'},'Cache geometri tanpa grid tidak lengkap/cocok')
    require(data.get('konfigurasi')==CONFIG and not data.get('render_dpi'),'Geometri harus piksel gambar asli/config sama')
    require(digest(source)==inv['hash_sumber']==data['sha256_sumber_sebelum']==data['sha256_sumber_sesudah'],'Hash geometri berbeda')
    it=next(i for i in inv['item'] if i['nilai']['nomor_baris_item']==cell['item'])
    require(it['nilai']['harga_satuan']=='','Geometri tidak mengganti harga terisi')
    headers={k:v['judul_kolom']['segmen'][0] for k,v in it['kolom'].items() if len(v.get('judul_kolom',{}).get('segmen',[]))==1}
    roles=('unit','harga_satuan','diskon_item_persen','subtotal_item')
    require(all(k in headers for k in roles),'Header satuan/harga/Disc/subtotal tidak lengkap')
    require(re.fullmatch(r'(?i)harga\s*(?:satuan|@)',headers['harga_satuan']['teks_asli'].strip()) is not None,'Peran header harga belum jelas')
    require(re.fullmatch(r'(?i)disc\.?|discount|diskon',headers['diskon_item_persen']['teks_asli'].strip()) is not None,'Peran header Disc belum jelas')
    boxes=[headers[k]['bbox_asal'] for k in roles]
    require(all(a[2]<b[0] for a,b in zip(boxes,boxes[1:])),'Header bersebelahan tidak terpisah/urut')
    hh=statistics.median(b[3]-b[1] for b in boxes)
    require(max((b[1]+b[3])/2 for b in boxes)-min((b[1]+b[3])/2 for b in boxes)<=.6*hh,'Header tidak satu band')
    anchors=[]
    for other in inv['item']:
        if other is it:continue
        evs={k:other['kolom'][k].get('segmen',[]) for k in roles[:3]}
        if any(len(v)!=1 for v in evs.values()):continue
        u,m,p=(evs[k][0] for k in roles[:3])
        if not re.fullmatch(r'-?\d[\d.,]*',m['teks_asli']) or not re.fullmatch(r'\d+(?:[.,]\d+)?\s*%',p['teks_asli']):continue
        if not re.search('[A-Za-z]',u['teks_asli']):continue
        bs=[s['bbox_asal'] for s in (u,m,p)];height=statistics.median(b[3]-b[1] for b in bs)
        if not all(a[2]<b[0] for a,b in zip(bs,bs[1:])):continue
        if max((b[1]+b[3])/2 for b in bs)-min((b[1]+b[3])/2 for b in bs)>.6*height:continue
        anchors.append({'item':other['nilai']['nomor_baris_item'],'unit':copy.deepcopy(u),'uang':copy.deepcopy(m),'persen':copy.deepcopy(p)})
    require(len({a['item'] for a in anchors})>=2,'Perlu dua baris pembanding uang/persen terpisah')
    yy=sorted((a['uang']['bbox_asal'][1]+a['uang']['bbox_asal'][3])/2 for a in anchors)
    require(all(b-a>hh for a,b in zip(yy,yy[1:])),'Baris pembanding tidak independen secara vertikal')
    # Interseksi celah dari header DAN seluruh anchor. Tidak memakai midpoint pusat kata.
    left_gap=[max(boxes[0][2],*(a['unit']['bbox_asal'][2] for a in anchors)),
              min(boxes[1][0],*(a['uang']['bbox_asal'][0] for a in anchors))]
    right_gap=[max(boxes[1][2],*(a['uang']['bbox_asal'][2] for a in anchors)),
               min(boxes[2][0],*(a['persen']['bbox_asal'][0] for a in anchors))]
    require(left_gap[1]-left_gap[0]>=4 and right_gap[1]-right_gap[0]>=4,'Celah kolom independen terlalu sempit/bertentangan')
    x0=math.ceil(sum(left_gap)/2);x1=math.floor(sum(right_gap)/2)
    ss=cell['bukti']['segmen'];merged=ss[0]['bbox_asal']
    row=[]
    for k in ('jumlah','unit','subtotal_item'):
        seg=it['kolom'][k].get('segmen',[])
        if len(seg)==1:row.append({'kolom':k,'segmen':copy.deepcopy(seg[0])})
    require(len(row)>=2 and any(a['kolom']=='unit' for a in row) and any(a['kolom']=='subtotal_item' for a in row),'Band target perlu unit dan subtotal independen')
    bb=[a['segmen']['bbox_asal'] for a in row]+[merged]
    height=statistics.median(b[3]-b[1] for b in bb)
    require(max((b[1]+b[3])/2 for b in bb)-min((b[1]+b[3])/2 for b in bb)<=.6*height,'Band target tidak sejajar')
    require(x0<merged[0]<x1<merged[2] and merged[1]>max(b[3] for b in boxes),'Region tidak melintasi batas harga/Disc pada area tabel')
    y0=math.floor(min(b[1] for b in bb))-2;y1=math.ceil(max(b[3] for b in bb))+2
    with Image.open(source) as im:
        require(getattr(im,'n_frames',1)==1 and im.getexif().get(274,1)==1,'Frame/orientasi tidak didukung')
        w,h=im.size;require(data['ukuran_gambar']=={'lebar':w,'tinggi':h},'Ukuran cache berbeda')
        require(0<=x0<x1<=w and 0<=y0<y1<=h,'Sel di luar gambar')
        ink=np.asarray(im.convert('L'))<150
    for s in data['segmen']:
        if s['id_segmen']==ss[0]['id_segmen'] or not re.search(r'\d',s['teks_asli']):continue
        b=s['bbox'];require(not(min(b[2],x1)>max(b[0],x0) and min(b[3],y1)>max(b[1],y0)),'ROI memuat digit tetangga di luar region target')
    for x in (x0,x1):require(not ink[y0:y1,x-1:x+2].any(),'Batas kolom tidak memiliki koridor putih; kemungkinan digit terpotong')
    crop=ink[y0:y1,x0:x1]
    require(not crop[:2].any() and not crop[-2:].any(),'Digit target menyentuh batas baris')
    require(np.count_nonzero(crop.any(axis=1))>=3 and np.count_nonzero(crop.any(axis=0))>=3,'Tidak cukup tinta harga dalam sel')
    require(ink[y0:y1,x1+2:math.ceil(merged[2])].any(),'Tidak ada dukungan piksel bagian persen di sebelah kanan')
    roi=[x0,y0,x1,y1]
    proof={'versi':VERSI,'jenis':JENIS,'cache_json':str(cp),'sha256_cache':digest(cp),
        'kolom':cell['kolom'],'item':cell['item'],'anchor_baris_pembanding':anchors,'anchor_baris_target':row,
        'header':{k:headers[k] for k in roles},'celah_kiri':left_gap,'celah_kanan':right_gap,
        'bbox_sel_sumber':roi,'bbox_region_baseline':merged,'ambang_piksel':150,
        'piksel_harga':int(crop.sum()),'bukan_bbox_kata':True,
        'aturan':'Dua baris uang/persen asli terpisah, header terurut, interseksi celah, band target independen, koridor putih dan guard seluruh digit tetangga. Tidak menyatakan bbox substring sebagai deteksi kata.'}
    return roi,proof
