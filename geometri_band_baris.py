"""Memisahkan band piksel saat bbox OCR baris bertetangga saling menimpa."""
import math,statistics,re,copy
VERSI='band-piksel-baris-v1'

def band(cell,inv,gray,x0,x1):
    import numpy as np
    rows=[]
    for it in inv['item']:
        keys=('unit','subtotal_item' if cell['kolom']=='harga_satuan' else 'harga_satuan')
        ss=[it['kolom'][k].get('segmen',[]) for k in keys]
        if any(len(s)!=1 for s in ss):continue
        u,n=(s[0] for s in ss)
        if not re.search('[A-Za-z]',u['teks_asli']) or not re.fullmatch(r'-?[\d., ]+',n['teks_asli']):continue
        boxes=[u['bbox_asal'],n['bbox_asal']];h=statistics.median(b[3]-b[1] for b in boxes)
        centers=[(b[1]+b[3])/2 for b in boxes]
        if max(centers)-min(centers)>.6*h:continue
        rows.append({'item':it['nilai']['nomor_baris_item'],'y':statistics.mean(centers),'h':h,'segmen':[copy.deepcopy(u),copy.deepcopy(n)]})
    rows.sort(key=lambda r:r['y']);j=next((i for i,r in enumerate(rows) if r['item']==cell['item']),-1)
    if not 0<j<len(rows)-1:raise ValueError('Band piksel perlu baris target serta tetangga atas/bawah dengan anchor independen')
    prev,target,nxt=rows[j-1:j+2];h=target['h']
    if not (h*.8<target['y']-prev['y']<h*3 and h*.8<nxt['y']-target['y']<h*3):raise ValueError('Jarak baris belum terisolasi')
    top=math.ceil((prev['y']+target['y'])/2);bottom=math.floor((target['y']+nxt['y'])/2)
    ink=gray[top:bottom,x0:x1]<150;active=np.flatnonzero(ink.any(axis=1));runs=[]
    for y in active:
        y=int(y)
        if runs and y==runs[-1][1]:runs[-1][1]=y+1
        else:runs.append([y,y+1])
    if len(runs)!=1:raise ValueError('Band piksel target bukan satu run digit')
    a,b=runs[0]
    if a<2 or b>bottom-top-2 or b-a<3:raise ValueError('Run digit belum memiliki dua baris margin putih')
    y0,y1=top+a-2,top+b+2
    if abs((y0+y1)/2-target['y'])>.6*h:raise ValueError('Run piksel tidak sejajar anchor target')
    crop=gray[y0:y1,x0:x1]<150
    if crop[:,0].any() or crop[:,-1].any():raise ValueError('Digit menyentuh tepi horizontal ROI')
    return y0,y1,{'versi':VERSI,'anchor_baris':rows[j-1:j+2],'jendela_y':[top,bottom],
        'run_tinta_y':[top+a,top+b],'ambang_gelap':150,'margin_putih':2,
        'bukan_bbox_kata':True,'alasan':'BBox OCR bertetangga boleh menimpa ruang putih; ROI dibentuk run piksel tunggal dengan anchor unit/uang serta batas midpoint baris, bukan memotong digit menurut bbox.'}
