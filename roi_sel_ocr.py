"""ROI sel dari grid fisik dan band baris; tidak membuat bbox kata baseline."""
import copy
import math
import re
import statistics
from pathlib import Path
from PIL import Image
from batch_common import complete,digest,read_json

VERSI='roi-geometri-sel-v1'
VERSI_MODUL='roi-geometri-sel-v5-band'

def cache_geometri_cocok(cp,data):
    # Inventaris lokasi diperkaya saat runtime; isi OCR, ukuran, config dan
    # hash gambar tetap identik dengan cache tersegel.
    if not complete(cp.parent):return False
    original=read_json(cp)
    return {k:v for k,v in original.items() if k!='inventaris'}=={k:v for k,v in data.items() if k!='inventaris'}


def kumpulkan_total(inv):
    ev=inv['kolom'].get('total_akhir',{})
    primary=ev.get('pemilihan_label_total',{}).get('label_utama',{}).get('segmen',[])
    if inv['nilai'].get('total_akhir')!='' or len(primary)!=1:return []
    if any(re.search(r'\d',s['teks_asli']) for s in ev.get('segmen',[])):return []
    return [{'kolom':'total_akhir','item':'','nilai_lama':'','teks':'',
             'header_valid':True,'pemetaan_valid':True,'bukti':ev}]


def proposal_ringkasan(cell,inv,data,source,cache_path):
    """Sel tanpa token dari label utama, dua anchor kolom, dan band tetangga."""
    import numpy as np
    if not cache_path:raise ValueError('Ringkasan memerlukan cache baseline tersegel')
    cp=Path(cache_path).resolve()
    if not cache_geometri_cocok(cp,data):raise ValueError('Cache ringkasan tidak cocok')
    if digest(source)!=inv['hash_sumber'] or data['sha256_sumber_sebelum']!=inv['hash_sumber'] or data['sha256_sumber_sesudah']!=inv['hash_sumber']:
        raise ValueError('Hash ringkasan berbeda')
    options=kumpulkan_total(inv)
    if len(options)!=1 or options[0]['bukti']!=cell['bukti']:raise ValueError('Bukan Total utama tunggal tanpa token')
    label=cell['bukti']['pemilihan_label_total']['label_utama']['segmen'][0]
    lb=label['bbox_asal'];lc=(lb[1]+lb[3])/2;hh=lb[3]-lb[1]
    area=inv['struktur']['area_total']
    known=r'(?i)(?:total|grand total|sub total|subtotal|total sebelum pajak|pajak|ppn|pph(?:\s*23)?|discount final(?:\s+[\d.,]+\s*%)?|diskon final|dibayar|saldo)\s*:?' 
    labels=[s for s in data['segmen'] if abs(s['bbox'][0]-lb[0])<=hh
            and area[1]<=s['bbox'][1]<area[3] and re.fullmatch(known,s['teks_asli'].strip())]
    centers=sorted({(s['bbox'][1]+s['bbox'][3])/2 for s in labels})
    above=[y for y in centers if y<lc];below=[y for y in centers if y>lc]
    if not above:raise ValueError('Band Total membutuhkan label tetangga atas')
    alternative=None
    y0=math.ceil((max(above)+lc)/2)+1
    y1=math.floor((min(below)+lc)/2) if below else None
    anchors=[s for s in data['segmen'] if area[1]<=s['bbox'][1]<area[3]
             and s['bbox'][0]>max(v['bbox'][2] for v in labels)+hh
             and re.fullmatch(r'-?[\d.,]+',s['teks_asli'].strip())]
    if len(anchors)<2 or max(s['bbox'][2] for s in anchors)-min(s['bbox'][2] for s in anchors)>.5*hh:
        raise ValueError('Kolom nominal ringkasan tidak terbukti oleh dua anchor sejajar')
    from PIL import Image
    with Image.open(source) as im:
        if getattr(im,'n_frames',1)!=1 or im.getexif().get(274,1)!=1:raise ValueError('Frame/orientasi ringkasan tidak didukung')
        w,h=im.size
        if data['ukuran_gambar']!={'lebar':w,'tinggi':h} or data.get('render_dpi'):raise ValueError('Ringkasan harus piksel sumber asli')
        if not below:
            # Baris terakhir membutuhkan batas fisik independen dan baris
            # piksel angka yang terisolasi, bukan sekadar mengabaikan guard.
            strides=[b-a for a,b in zip(centers,centers[1:])]
            if len(strides)<2 or not all(.6*hh<=v<=1.5*hh for v in strides) or max(strides)-min(strides)>.5*hh:
                raise ValueError('Ritme baris ringkasan terakhir tidak terbukti')
            gray=np.asarray(im.convert('L'));x0=max(0,math.floor(min(s['bbox'][0] for s in anchors)-.4*hh))
            x1=min(w,math.ceil(max(s['bbox'][2] for s in anchors)+.4*hh))
            line_x0=math.floor(lb[0]);line_x1=w-5
            candidates=[y for y in range(math.ceil(lb[3]+hh),min(h,math.floor(lb[3]+16*hh)))
                        if (gray[y,line_x0:line_x1]<150).mean()>=.85]
            if not candidates:raise ValueError('Batas fisik bawah ringkasan terakhir tidak ditemukan')
            rule_y=min(candidates);bottom=min(rule_y-math.ceil(hh),math.floor(lc+1.25*statistics.median(strides)))
            if bottom<=y0:raise ValueError('Band akhir terlalu sempit')
            # Anchor pendek (misalnya nol) tidak membuktikan lebar nominal
            # target. Tahan jika terdapat tinta digit di bagian lain area
            # nominal, agar crop tidak mengambil ekor suatu bilangan saja.
            value_left=math.ceil(max(v['bbox'][2] for v in labels)+hh)
            if (gray[y0:bottom,value_left:x0]<150).any() or (gray[y0:bottom,x1:w-1]<150).any():
                raise ValueError('Tinta nominal di luar batas anchor; seluruh digit target belum terbukti')
            ink_rows=(gray[y0:bottom,x0:x1]<150).any(axis=1)
            groups=[]
            for row in np.flatnonzero(ink_rows):
                if groups and row==groups[-1][1]:groups[-1][1]=int(row)+1
                else:groups.append([int(row),int(row)+1])
            if len(groups)!=1:raise ValueError('Piksel baris terakhir tidak tunggal; kemungkinan digit tetangga')
            start,end=groups[0]
            if start<2 or end>len(ink_rows)-2 or end-start<3:
                raise ValueError('Digit baris terakhir terpotong/tidak memiliki margin kosong')
            mid=y0+(start+end)/2
            if abs(mid-lc)>.65*hh:raise ValueError('Piksel nominal tidak sejajar label Total terakhir')
            y1=y0+end+2
            alternative={'jenis':'garis_fisik_dan_band_piksel_terisolasi','garis_bawah_y':rule_y,
                'rentang_x_garis':[line_x0,line_x1],'ambang_cakupan_garis':.85,
                'stride_label':strides,'batas_jendela_y':[y0,bottom],'run_piksel_y':[y0+start,y0+end],
                'aturan':'Dua anchor kolom, ritme >=3label, garis fisik independen, satu baris tinta dengan margin putih; guard digit tetangga tetap.'}
        roi=[max(0,math.floor(min(s['bbox'][0] for s in anchors)-.4*hh)),y0,
             min(w,math.ceil(max(s['bbox'][2] for s in anchors)+.4*hh)),y1]
        if not (0<=roi[0]<roi[2]<=w and 0<=y0<y1<=h):raise ValueError('Sel ringkasan di luar gambar')
        for s in data['segmen']:
            bb=s['bbox']
            if re.search(r'\d',s['teks_asli']) and min(bb[2],roi[2])>max(bb[0],roi[0]) and min(bb[3],roi[3])>max(bb[1],roi[1]):
                raise ValueError('Sel ringkasan menyentuh token digit lain; tidak meminjam Dibayar/Saldo')
        ink=np.asarray(im.convert('L'))[y0:y1,roi[0]:roi[2]]<150
        if len(np.flatnonzero(ink.any(axis=1)))<3:raise ValueError('Tidak cukup piksel angka; tidak default nol')
        if ink[0].any() or ink[-1].any() or ink[:,0].any() or ink[:,-1].any():
            raise ValueError('Piksel target menyentuh tepi sel ringkasan; digit utuh belum terbukti')
    proof={'versi':VERSI_MODUL if alternative else 'roi-geometri-sel-v2','jenis':'ringkasan','cache_json':str(cp),'sha256_cache':digest(cp),
        'kolom':'total_akhir','item':'','label_asli':label,'label_band':labels,'anchor_kolom':anchors,
        'bbox_sel_sumber':roi,'bukan_bbox_kata_baseline':True,'piksel_gelap_crop':int(ink.sum()),
        'aturan':'Posisi kolom dari dua anchor, band dari label tetangga; teks/nominal anchor bukan jawaban.'}
    if alternative:proof['batas_bawah_alternatif']=alternative
    return roi,proof


def kumpulkan_jumlah(inv):
    cells=[]
    for it in inv['item']:
        ev=it['kolom']['jumlah']
        if it['nilai']['jumlah']!='' or ev.get('segmen'):continue
        headers=ev.get('judul_kolom',{}).get('segmen',[])
        hp=inv.get('struktur',{}).get('bukti_header',{})
        supported=len(headers)==1 and (re.fullmatch(r'(?i)jumlah|qty',headers[0]['teks_asli'].strip()) or hp.get('segmen_header'))
        cells.append({'kolom':'jumlah','item':it['nilai']['nomor_baris_item'],'nilai_lama':'','teks':'',
                      'header_valid':bool(supported),'pemetaan_valid':False,'bukti':ev})
    return cells


def dukungan_pemisah_campuran(cell):
    """Pemicu OCR saja, bukan izin menafsirkan skala token campuran."""
    from normalisasi_angka import analisis_angka
    text=cell['teks'].strip();ev=cell['bukti'];ctx=ev.get('bukti_locale_kolom',{})
    if not re.fullmatch(r'-?\d{1,3}(?:[.,]\d{3}){2,}',text) or len(set(re.findall('[.,]',text)))<2:return False
    if ctx.get('kolom')!=cell['kolom'] or ctx.get('locale') not in ('en_US','id_ID'):return False
    ss=ctx.get('bukti',{}).get('segmen',[])
    return bool(ss) and all(s.get('skor',0)>=.85 and
        re.fullmatch(r'-?\d{1,3}(?:,\d{3}){2,}' if ctx['locale']=='en_US' else r'-?\d{1,3}(?:\.\d{3}){2,}',s['teks_asli']) and
        analisis_angka(s['teks_asli'],ctx['locale'])['status']=='langsung' for s in ss)


def proposal(cell,inv,data,source,cache_path):
    """Sel aktual dari garis, header dan row anchors; bbox kata tetap kosong."""
    import numpy as np
    from header_grid import runs
    if not cache_path:raise ValueError('Geometri sel memerlukan rujukan cache baseline tersegel')
    cp=Path(cache_path).resolve()
    if not cache_geometri_cocok(cp,data):raise ValueError('Cache geometri tidak lengkap/cocok')
    from uji_ocr_gambar import CONFIG
    if data.get('konfigurasi')!=CONFIG or data.get('render_dpi'):raise ValueError('Geometri bukan piksel gambar asli/config cocok')
    if digest(source)!=inv['hash_sumber']:
        raise ValueError('Hash baseline geometri berbeda')
    if not data['sha256_sumber_sebelum']==data['sha256_sumber_sesudah']==inv['hash_sumber']:raise ValueError('Hash cache geometri berbeda')
    it=next(t for t in inv['item'] if t['nilai']['nomor_baris_item']==cell['item'])
    if it['nilai'][cell['kolom']]!='':raise ValueError('Geometri tidak mengganti sel terisi')
    headers={k:v['judul_kolom']['segmen'][0] for k,v in it['kolom'].items() if len(v.get('judul_kolom',{}).get('segmen',[]))==1}
    ordered=sorted(headers,key=lambda k:sum(headers[k]['bbox_asal'][::2])/2)
    if len(ordered) not in (6,7) or any(k not in ordered for k in ('nama_barang','jumlah','unit','harga_satuan','subtotal_item')):
        raise ValueError('Peran kolom geometri belum cukup')
    hs=statistics.median(s['bbox_asal'][3]-s['bbox_asal'][1] for s in headers.values())
    top=math.ceil(max(s['bbox_asal'][3] for s in headers.values())+hs)
    bottom=math.floor(inv['struktur']['area_total'][1])
    with Image.open(source) as im:
        if getattr(im,'n_frames',1)!=1 or im.getexif().get(274,1)!=1:raise ValueError('Frame/orientasi geometri belum didukung')
        w,h=im.size
        if data['ukuran_gambar']!={'lebar':w,'tinggi':h} or bottom-top<4*hs:raise ValueError('Ukuran/tinggi tabel belum cukup')
        a=np.asarray(im.convert('L'));lines=runs(np.flatnonzero((a[top:bottom]<90).mean(axis=0)>.85))
    centers=[(headers[k]['bbox_asal'][0]+headers[k]['bbox_asal'][2])/2 for k in ordered]
    boundaries=[]
    for left,right in zip(centers,centers[1:]):
        candidates=[x for x in lines if left<x<right]
        if len(candidates)!=1:raise ValueError('Batas grid di antara header tidak tunggal')
        boundaries.append(candidates[0])
    edges=[max([x for x in lines if x<centers[0]],default=0),*boundaries,min([x for x in lines if x>centers[-1]],default=w)]
    j=ordered.index(cell['kolom']);xmin,xmax=edges[j:j+2]
    row=[]
    for key in ('unit','harga_satuan','subtotal_item'):
        ss=it['kolom'][key].get('segmen',[])
        if key==cell['kolom'] or len(ss)!=1:continue
        if key=='unit' and not re.search('[A-Za-z]',ss[0]['teks_asli']):continue
        if key!='unit' and not re.fullmatch(r'-?[\d., ]+',ss[0]['teks_asli']):continue
        row.append({'kolom':key,'segmen':copy.deepcopy(ss[0])})
    if len(row)<2 or not any(x['kolom']=='unit' for x in row):raise ValueError('Band baris perlu unit dan angka pada kolom lain')
    yy=[(x['segmen']['bbox_asal'][1]+x['segmen']['bbox_asal'][3])/2 for x in row]
    hh=statistics.median(x['segmen']['bbox_asal'][3]-x['segmen']['bbox_asal'][1] for x in row)
    if max(yy)-min(yy)>.6*hh:raise ValueError('Anchor baris tidak sejajar')
    ss=cell['bukti'].get('segmen',[])
    if cell['kolom']=='jumlah' and not ss:
        box=[xmin+1,min(x['segmen']['bbox_asal'][1] for x in row),xmax-1,max(x['segmen']['bbox_asal'][3] for x in row)]
        roi=[math.ceil(xmin)+1,max(0,math.floor(box[1])-2),math.floor(xmax)-1,min(h,math.ceil(box[3])+2)]
    elif len(ss)==1:
        box=ss[0]['bbox_asal'];roi=[max(math.ceil(xmin)+1,math.floor(box[0])-6),max(0,math.floor(box[1])-4),
                                   min(math.floor(xmax)-1,math.ceil(box[2])+6),min(h,math.ceil(box[3])+4)]
    else:raise ValueError('Satu region target atau jumlah tanpa token saja yang didukung')
    target_ids={s['id_segmen'] for s in ss}
    obstacles=[]
    for s in data['segmen']:
        if s['id_segmen'] in target_ids or not re.search(r'\d',s['teks_asli']):continue
        bb=s['bbox']
        if min(bb[2],roi[2])<=max(bb[0],roi[0]):continue
        if bb[3]<=box[1]:roi[1]=max(roi[1],math.ceil(bb[3])+1)
        elif bb[1]>=box[3]:roi[3]=min(roi[3],math.floor(bb[1])-1)
        if min(bb[2],roi[2])>max(bb[0],roi[0]) and min(bb[3],roi[3])>max(bb[1],roi[1]):
            obstacles.append(s['id_segmen'])
    pixel_proof=None
    cut=bool(ss and not (roi[0]<=box[0]<box[2]<=roi[2] and roi[1]<=box[1]<box[3]<=roi[3]))
    if obstacles or cut:
        if not ss or cell['kolom'] not in ('harga_satuan','subtotal_item'):
            raise ValueError('Sel geometri tetap menyentuh digit tetangga: '+str(obstacles))
        from geometri_band_baris import band
        yy0,yy1,pixel_proof=band(cell,inv,a,roi[0],roi[2]);roi[1]=yy0;roi[3]=yy1
        for s in data['segmen']:
            if s['id_segmen'] in target_ids or not re.search(r'\d',s['teks_asli']):continue
            bb=s['bbox']
            if min(bb[2],roi[2])>max(bb[0],roi[0]) and min(bb[3],roi[3])>max(bb[1],roi[1]):
                raise ValueError('Band piksel masih menyentuh bbox digit tetangga')
        if not (roi[0]<=box[0]<box[2]<=roi[2]):raise ValueError('Band piksel tidak boleh memotong rentang horizontal digit')
    if not (0<=roi[0]<roi[2]<=w and 0<=roi[1]<roi[3]<=h):raise ValueError('Sel kosong/di luar gambar')
    ink=a[roi[1]:roi[3],roi[0]:roi[2]]<150
    if len(np.flatnonzero(ink.any(axis=1)))<3:raise ValueError('Sel tidak memiliki dukungan piksel untuk dibaca; tidak default nilai')
    proof={'versi':VERSI,'kolom':cell['kolom'],'item':cell['item'],'cache_json':str(cp),'sha256_cache':digest(cp),
           'batas_kolom_grid':edges,'urutan_kolom':ordered,'band_deteksi_grid':[top,bottom],
           'ambang_gelap':90,'fraksi_garis_minimum':.85,'anchor_baris':row,
           'judul_kolom_asal':headers[cell['kolom']],'bbox_sel_sumber':roi,
           'bbox_region_baseline':ss[0]['bbox_asal'] if ss else None,
           'bukan_bbox_kata_baseline':not bool(ss),'piksel_gelap_crop':int(ink.sum())}
    if pixel_proof:proof['bukti_band_piksel']=pixel_proof
    return roi,proof


def validasi_permintaan(req):
    """Rekonstruksi geometri dari cache, bukan percaya metadata crop saja."""
    proof=req.get('geometri_sel')
    if not proof:return
    cp=Path(proof['cache_json'])
    if digest(cp)!=proof['sha256_cache'] or not complete(cp.parent):raise ValueError('Hash/segel cache geometri berubah')
    data=read_json(cp)
    from ekstrak_faktur_ocr import ekstrak
    inv=ekstrak(data);inv['hash_sumber']=req['hash_sumber']
    if proof.get('jenis')=='uang_persen_tanpa_grid':
        from geometri_uang_persen import proposal as proposal_region
        it=next(i for i in inv['item'] if i['nilai']['nomor_baris_item']==req['item'])
        cell={'kolom':req['kolom'],'item':req['item'],'bukti':it['kolom'][req['kolom']]}
        roi,actual=proposal_region(cell,inv,data,req['path_sumber'],cp)
        if roi!=req['crop_bbox_sumber'] or actual!=proof:raise ValueError('Rekonstruksi region uang/persen berbeda')
        return
    if proof.get('jenis')=='ringkasan':
        cell=kumpulkan_total(inv)
        if len(cell)!=1:raise ValueError('Total utama tidak tersedia untuk rekonstruksi')
        roi,actual=proposal_ringkasan(cell[0],inv,data,req['path_sumber'],cp)
        if roi!=req['crop_bbox_sumber'] or actual!=proof:raise ValueError('Rekonstruksi ringkasan berbeda')
        return
    it=next(i for i in inv['item'] if i['nilai']['nomor_baris_item']==req['item'])
    ev=it['kolom'][req['kolom']]
    if ev.get('segmen',[])!=req['bukti_baseline'].get('segmen',[]):raise ValueError('Target geometri berbeda dari baseline parser')
    cell={'kolom':req['kolom'],'item':req['item'],'bukti':ev}
    roi,actual=proposal(cell,inv,data,req['path_sumber'],cp)
    if roi!=req['crop_bbox_sumber'] or actual!=proof:raise ValueError('Rekonstruksi grid/band/ROI tidak cocok')
