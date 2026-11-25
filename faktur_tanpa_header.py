"""Fallback konservatif tabel transaksi tanpa header OCR; hasil wajib review."""
import hashlib,json,re,statistics
from decimal import Decimal
from ekstrak_faktur_digital import FAKTUR,DETAIL,cek_hitung,dec


def struktur_tanpa_header(data):
    from ekstrak_faktur_ocr import PembacaOCR,norm,cy,ht
    r=PembacaOCR(data)
    # Bukan jalan pintas untuk dokumen dengan judul nonpenjualan yang terbaca.
    if any(re.search(r'(?i)\b(kwitansi|kuitansi|faktur pajak|bukti transfer|registrasi)\b',s['teks_asli']) and cy(s)<r.height*.25 for s in r.s): return None
    labels={k:[s for s in r.s if norm(s['teks_asli'])==k and cy(s)<r.height*.3] for k in ['nomor','tanggal','kepada']}
    total=[s for s in r.s if norm(s['teks_asli'])=='total' and s['bbox'][0]>r.w/2]
    if any(len(v)!=1 for v in labels.values()) or len(total)!=1: return None
    top=max(s['bbox'][3] for group in labels.values() for s in group)+r.h
    bottom=total[0]['bbox'][1]
    numeric=[s for s in r.s if top<cy(s)<bottom and re.fullmatch(r'-?\d[\d.,]*%?',s['teks_asli'].strip())]
    clusters=[]
    for s in sorted(numeric,key=lambda s:(s['bbox'][0]+s['bbox'][2])/2):
        x=(s['bbox'][0]+s['bbox'][2])/2
        g=next((g for g in clusters if abs(x-statistics.median((v['bbox'][0]+v['bbox'][2])/2 for v in g))<r.h*1.6),None)
        if g is None: clusters.append([s])
        else: g.append(s)
    columns=[g for g in clusters if len(g)>=8]
    money=[g for g in columns if sum('%' not in s['teks_asli'] for s in g)>=len(g)*.9]
    if len(money)<3: return None
    # Tiga kolom numerik paling kanan sebelum kolom persen: jumlah/harga/subtotal.
    q,p,sub=money[-3:]
    if not all(any(re.fullmatch(r'\d{1,3}(?:,\d{3})+\.\d{2}',s['teks_asli']) for s in g) for g in [p,sub]): return None
    xq=statistics.median(s['bbox'][0] for s in q)
    names=[s for s in r.s if top<cy(s)<bottom and s['bbox'][2]<xq and re.search(r'[A-Za-z]',s['teks_asli'])]
    groups=[]
    for s in sorted(names,key=lambda s:s['bbox'][0]):
        g=next((g for g in groups if abs(s['bbox'][0]-statistics.median(x['bbox'][0] for x in g))<r.h*1.6),None)
        if g is None: groups.append([s])
        else:g.append(s)
    groups=[g for g in groups if len(g)>=len(q)*.6]
    if not groups:return None
    names=max(groups,key=lambda g:statistics.median(s['bbox'][0] for s in g))
    return r,labels,total[0],q,p,sub,names,columns


def ekstrak_tanpa_header(data):
    from ekstrak_faktur_ocr import cy,ht,norm
    detected=struktur_tanpa_header(data)
    if not detected: raise ValueError('Struktur tabel tanpa header tidak cukup kuat; tidak memaksakan faktur')
    r,labels,total,q,prices,subs,names,columns=detected; r.locale='en_US'
    area=[0,0,r.w,total['bbox'][1]]; fields={}
    for key,label,kind in [('nomor_invoice','Nomor','text'),('tanggal_invoice','Tanggal','tanggal'),('nama_pelanggan','Kepada','text')]:
        # Kolom identitas kiri dibatasi sebelum area alamat kanan.
        fields[key]=r.label([label],[0,0,r.w*.48,total['bbox'][1]],kind)
    fields['total_akhir']=r.label(['Total'],[r.w/2,total['bbox'][1]-r.h/2,r.w,total['bbox'][3]+r.h/2],'angka')
    # Nama tanda tangan/footer hanya kandidat penerbit; tidak dianggap verifikasi.
    lower=[s for s in r.s if norm(s['teks_asli'].split(':')[0].replace(';','')) in ['saldo','dibayar','total'] and s['bbox'][0]>r.w/2]
    edge=max(s['bbox'][3] for s in lower)
    issuer=[s for s in r.s if s['bbox'][0]>r.w/2 and edge<cy(s)<edge+4*r.h and re.search('[A-Za-z]',s['teks_asli'])]
    fields['nama_penerbit']=r.nilai(issuer[0]['teks_asli'],issuer,'Kandidat penerbit pada tanda tangan kanan setelah saldo; perlu review') if len(issuer)==1 else r.bukti('',issuer,'Penerbit tidak tunggal','ambigu')
    def closest(group,anchor):
        candidates=sorted([s for s in group if abs(cy(s)-cy(anchor))<.7*max(ht(s),ht(anchor))],key=lambda s:abs(cy(s)-cy(anchor)))
        return candidates[:1] if len(candidates)==1 or len(candidates)>1 and abs(cy(candidates[1])-cy(anchor))-abs(cy(candidates[0])-cy(anchor))>r.h*.2 else []
    items=[]; used=set()
    for anchor in sorted(q,key=cy):
        cells={'jumlah':[anchor],'harga_satuan':closest(prices,anchor),'subtotal_item':closest(subs,anchor),'nama_barang':closest(names,anchor)}
        if not cells['nama_barang']: continue
        cols={}
        for key,ss in cells.items():
            cols[key]=r.nilai(ss[0]['teks_asli'] if ss else '',ss,'Klaster kolom dan tetangga vertikal unik; header OCR tidak tersedia','text' if key=='nama_barang' else 'angka')
            for s in ss:
                if s['id_segmen'] in used: raise ValueError('Segmen dipasangkan ke lebih dari satu item; perlu review')
                used.add(s['id_segmen'])
        for key in ['unit','diskon_item_persen','pajak_item_tercetak']:
            cols[key]=r.bukti(reason='header_atau_satuan_tidak_terbaca',method='Tidak mengarang unit/diskon/pajak dari kolom tanpa header OCR')
        row={k:v['nilai'] for k,v in cols.items()}
        checks=[cek_hitung('jumlah x harga vs subtotal (diagnostik sebelum potongan; bukan validasi pajak/diskon)',dec(row,'subtotal_item'),dec(row,'jumlah')*dec(row,'harga_satuan') if None not in [dec(row,'jumlah'),dec(row,'harga_satuan')] else None)]
        items.append({'nilai':row,'kolom':cols,'validasi':checks})
    if len(items)!=len(q): raise ValueError('Tidak semua baris jumlah memiliki pasangan nama tunggal; perlu review')
    identity=[data['sha256_sumber_sebelum'],fields['nomor_invoice']['nilai'],fields['tanggal_invoice']['nilai'],fields['nama_penerbit']['nilai']]
    fid='FKT_OCR_'+hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()[:24]
    note='Judul/header tidak terbaca OCR; pengenalan struktur faktur dan kolom merupakan inferensi yang perlu review. Catatan tangan bukan aturan pengganti label pajak/diskon. Ejaan dipertahankan.'
    if r.masalah: note+=' '+ '; '.join(r.masalah)
    row={k:fields.get(k,{}).get('nilai','') for k in FAKTUR}
    row.update(id_faktur=fid,folder_klien=data['inventaris']['folder_klien'],file_sumber=data['sumber_gambar'],halaman_sumber='1',status_validasi='perlu_review',catatan_validasi=note)
    for k in FAKTUR:
        if k not in fields: fields[k]=r.bukti(row[k],method='Metadata sistem' if row[k] else 'Label tidak ditemukan dalam OCR',reason='tidak_ditemukan')
    for i,item in enumerate(items,1):
        v={k:item['nilai'].get(k,'') for k in DETAIL}; v.update(id_faktur=fid,nomor_baris_item=str(i),halaman_sumber='1',status_validasi='perlu_review',catatan_validasi='Kolom tanpa header OCR; unit, diskon dan pajak belum dipastikan. Angka cacat tetap kosong.')
        for k in DETAIL:
            if k not in item['kolom']: item['kolom'][k]=r.bukti(v[k],method='Metadata urutan/relasi/validasi')
        item['nilai']=v
    vals=[dec(i['nilai'],'subtotal_item') for i in items]
    checks=[cek_hitung('jumlah subtotal vs total tercetak (diagnostik, bukan pengisian subtotal faktur)',dec(row,'total_akhir'),sum(vals,Decimal(0)) if vals and None not in vals else None)]
    return {'nilai':row,'kolom':fields,'item':items,'validasi':checks,'keterangan_tabel':[],
            'format_total':'tabel_tanpa_header_total_saja','bukti_struktur':r.bukti(segments=r.s,method='Seluruh segmen asli; termasuk angka, kolom persen tanpa label, footer dan catatan tangan; belum dipastikan'),
            'catatan_layout':note,'status_manusia':'belum_diverifikasi'}
