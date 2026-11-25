"""Cakupan berdasarkan judul/label dan layout, bukan kata produk atau prefix."""
import copy
import re
VERSI='cakupan-layout-v2'
NEGATIVE={'surat penawaran','sales quotation','quotation','penawaran','proforma invoice','faktur pajak'}
SALES={'faktur penjualan','sales invoice','invoice'}
CUE=re.compile(r'\b(?:quotation|penawaran|proforma\s+invoice)\b',re.I)
LABEL=re.compile(r'^(?:pembayaran|perihal|hal|keterangan(?:\s+transaksi)?|catatan(?:\s+transaksi)?)\s*:?\s*$',re.I)
INLINE=re.compile(r'^(?:pembayaran|perihal|hal|keterangan(?:\s+transaksi)?|catatan(?:\s+transaksi)?)\s*:\s*',re.I)

def text(s):return re.sub(r'\s+',' ',s.get('teks_asli',s.get('teks','')).strip()).casefold()
def box(s):return s.get('bbox_asal',s.get('bbox'))
def inside(b,a):return bool(b and a and a[0]<=b[0] and a[1]<=(b[1]+b[3])/2<=a[3] and b[2]<=a[2])

def layout_dari_faktur(inv,lebar,tinggi):
    """Area dan bbox asli parser; tidak menciptakan bbox substring."""
    area=inv.get('area',{});st=inv.get('struktur',{});table=area.get('tabel')
    if not table and st.get('area_pelanggan') and st.get('area_total'):
        table=[0,st['area_pelanggan'][3],lebar,st['area_total'][1]]
    edges=area.get('batas_kolom_x',st.get('batas_kolom_piksel',[]))
    valid=bool(table and 0<=table[1]<table[3]<=tinggi and len(edges)>=6 and all(a<b for a,b in zip(edges,edges[1:])))
    products=[]
    for it in inv.get('item',[]):
        ev=it.get('kolom',{}).get('nama_barang',{});refs=ev.get('segmen',ev.get('fragmen',[]))
        if isinstance(refs,list):products.append({'nomor_item':it['nilai'].get('nomor_baris_item'),'nama_terbaca':it['nilai'].get('nama_barang',''),'bukti_asal':copy.deepcopy(refs)})
    return {'valid':valid,'tabel':table,'batas_kolom':edges,'produk':products,
        'identitas':[0,0,lebar,table[1]] if valid else None,'footer':[0,table[3],lebar,tinggi] if valid else None,
        'satuan':'mengikuti bukti asli: piksel atau point PDF; tidak saling dipertukarkan'}

def analisis_cakupan(lines,tinggi=None,layout=None):
    """Frasa memicu pencarian konteks; bukan keputusan jenis dokumen."""
    layout=layout or {};valid=bool(layout.get('valid'));table=layout.get('tabel') if valid else None
    titles=[];negative=[];transaction=[];uncertain=[];ignored=[];roles=[]
    labels=[s for s in lines if LABEL.fullmatch(text(s)) and box(s)]
    def product(line):
        b=box(line);t=text(line)
        if not valid or not inside(b,table):return None
        for p in layout.get('produk',[]):
            if not t or t not in re.sub(r'\s+',' ',p['nama_terbaca']).casefold():continue
            boxes=[box(ref) for ref in p['bukti_asal'] if box(ref)]
            # Union kata native adalah bbox gabungan bukti, bukan bbox kata
            # buatan dari satu region OCR. Referensi setiap kata tetap utuh.
            if boxes:boxes.append([min(b[0] for b in boxes),min(b[1] for b in boxes),max(b[2] for b in boxes),max(b[3] for b in boxes)])
            for rb in boxes:
                if not rb:continue
                overlap=max(0,min(b[2],rb[2])-max(b[0],rb[0]))*max(0,min(b[3],rb[3])-max(b[1],rb[1]))
                if overlap>=.8*(b[2]-b[0])*(b[3]-b[1]):return p
        return None
    for line in lines:
        t=text(line);b=box(line)
        header=bool(b and tinggi and b[1]<=.35*tinggi and (not table or b[3]<=table[1]))
        if header and t in SALES:titles.append(line)
        tax=bool(re.search(r'\b(?:kelengkapan\s+data\s+e[- ]?faktur|kode\s+dan\s+nomor\s+seri\s+faktur\s+pajak)\b',t))
        sq=bool(re.match(r'^nomor\s+sq\s*:',t))
        if not (CUE.search(t) or tax or t=='faktur pajak' or sq):continue
        p=product(line)
        if p:
            ignored.append({'bukti':line,'peran':'nama_barang','nomor_item':p['nomor_item'],'alasan':'Pemetaan item/bbox tabel membuktikan nama produk; bukan pernyataan jenis dokumen.'});continue
        if header and (t in NEGATIVE or tax):
            negative.append(line);roles.append({'bukti':line,'peran':'judul_atau_perihal_nonfaktur'});continue
        meta=bool(b and (inside(b,layout.get('identitas')) if valid else header));anchors=[]
        if meta:
            for label in labels:
                lb=box(label);hh=max(b[3]-b[1],lb[3]-lb[1])
                if abs((b[1]+b[3]-lb[1]-lb[3])/2)<=.6*hh and lb[2]<=b[0] and b[0]-lb[2]<=12*hh:anchors.append(label)
        inline=bool(INLINE.match(t))
        if meta and (inline or len(anchors)==1) and CUE.search(t):
            transaction.append(line);roles.append({'bukti':line,'peran':'pernyataan_transaksi','label_asal':[line] if inline else anchors,'aturan':'Label inline atau satu label kiri sejajar, di atas batas tabel.'})
        else:
            uncertain.append(line);roles.append({'bukti':line,'peran':'footer_keterangan' if inside(b,layout.get('footer')) else 'konteks_belum_cukup','alasan':'Kata terkait penawaran tanpa bukti judul/pernyataan transaksi cukup; jenis belum dipastikan.'})
    active=[]
    for s in negative+transaction+uncertain:
        if s not in active:active.append(s)
    observation=None
    if active:
        outside=bool(negative) and not titles
        observation={'versi':VERSI,'status':'di_luar_cakupan_faktur_penjualan' if outside else 'transaksi_final_belum_dipastikan','bukti':active,'bukti_judul_faktur':titles,
            'konflik_judul_isi':bool(titles and (negative or transaction)),'peran_bukti':roles,'layout_cukup':valid,'prefix_nomor_bukan_dasar':True,'status_manusia':'belum_diverifikasi',
            'alasan':'Judul/perihal nonfaktur eksplisit pada area atas; bukan transaksi penjualan final.' if outside else ('Judul faktur dan pernyataan transaksi penawaran bertentangan; record sementara dipertahankan.' if titles and (negative or transaction) else 'Petunjuk keterangan/footer atau layout belum cukup; cakupan tetap belum dipastikan, bukan bukti dokumen penawaran.')}
    return {'versi':VERSI,'observasi':observation,'petunjuk_produk_diabaikan':ignored,'peran_bukti':roles,'layout_cukup':valid,'status_transaksi_final':'belum_dipastikan','status_manusia':'belum_diverifikasi','batas':'Tidak adanya bukti penawaran atau judul faktur tidak membuktikan transaksi final.'}

def observasi_cakupan(lines,tinggi=None,layout=None):return analisis_cakupan(lines,tinggi,layout)['observasi']

def terapkan_cakupan(inv,lines,lebar,tinggi):
    analysis=analisis_cakupan(lines,tinggi,layout_dari_faktur(inv,lebar,tinggi))
    inv['analisis_cakupan']=analysis;inv['cakupan_dokumen']=analysis['observasi']
    if analysis['observasi']:inv.setdefault('status_record','sementara_cakupan_belum_dipastikan')
    return analysis
