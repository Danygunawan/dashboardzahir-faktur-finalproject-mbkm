"""Pengenalan format dari bukti label/posisi; tanpa memilih rumus dari angka."""
import re
import statistics


FORMAT_NOMINAL = 'diskon_nominal_sebelum_total_sebelum_pajak'
VERSI_FORMAT = 'format-ringkasan-v3'


def kenali_ringkasan_nominal(inv):
    """Kompatibilitas API; keputusan label/bbox bersama, bukan gap/Harga@."""
    from validasi_ringkasan import basis_diskon
    profile = basis_diskon(inv)
    return dict(profile, format_total=FORMAT_NOMINAL) if profile['basis']=='setelah_diskon' else None


def identitas_invoice_fallback(inv):
    """Nomor SJ yang disimpan pada nomor_invoice tetap identitas inferensi."""
    ev = inv.get('kolom', {}).get('nomor_invoice', {})
    method = ev.get('metode', '')
    if ev.get('provenance_identitas_header') and ev.get('deduplikasi_otomatis_diizinkan') is False:
        return {'label_asal':ev.get('label_asal','Nomor'),
                'status_identitas':ev['status_identitas'],'versi_guard':'identitas-fallback-v2',
                'metode_asal':method,'bukti_asal':ev,'deduplikasi_otomatis_diizinkan':False}
    if re.search(r'(?i)fallback.*nomor\s*sj', method):
        return {'label_asal': 'Nomor SJ', 'status_identitas': 'fallback_belum_dipastikan_sebagai_nomor_invoice',
                'metode_asal': method, 'bukti_asal': ev,
                'deduplikasi_otomatis_diizinkan': False}
    return None


def kenali_basis_sebelum_diskon(inv):
    """TSP mendahului diskon menurut label/area; tidak membaca nominal."""
    from validasi_ringkasan import basis_diskon
    profile = basis_diskon(inv)
    return dict(profile, format_total='basis_label_sebelum_diskon') if profile['basis']=='sebelum_diskon' else None


def _label_ocr(ev, pattern):
    """Satu label lengkap dan sel kanan; angka tidak memilih format."""
    ss=ev.get('segmen',[])
    found=[s for s in ss if re.fullmatch(pattern+r'\s*(?::.*)?',s.get('teks_asli','').strip(),re.I)]
    if len(found)!=1:return None
    s=found[0];b=s.get('bbox_asal');h=b[3]-b[1] if b else 0
    if h<=0 or ev.get('satuan_koordinat')!='piksel':return None
    inline=':' in s['teks_asli'] and bool(s['teks_asli'].split(':',1)[1].strip())
    values=[v for v in ss if v is not s and v.get('teks_asli','').strip(' :')]
    if not inline:
        if len(values)!=1:return None
        vb=values[0].get('bbox_asal')
        if not vb or vb[0]<b[2]-.3*h or abs((vb[1]+vb[3]-b[1]-b[3])/2)>.75*max(h,vb[3]-vb[1]):return None
    return {'teks':s['teks_asli'],'bbox':b,'halaman':s.get('halaman',ev.get('halaman')),'id_segmen':s.get('id_segmen')}


def _rantai_ocr(inv, specs):
    labels=[]
    for key,pattern in specs:
        label=_label_ocr(inv.get('kolom',{}).get(key,{}),pattern)
        if not label:return None
        labels.append({'kolom':key,**label})
    if len({s['halaman'] for s in labels})!=1 or labels[0]['halaman'] is None:return None
    h=statistics.median(s['bbox'][3]-s['bbox'][1] for s in labels)
    ys=[(s['bbox'][1]+s['bbox'][3])/2 for s in labels];xs=[s['bbox'][0] for s in labels]
    if any(not .6*h<=b-a<=4*h for a,b in zip(ys,ys[1:])) or max(xs)-min(xs)>3*h:return None
    return labels


def kenali_setelah_pajak(inv):
    """Susunan eksplisit setelah pajak/PPN, opsional PPh pemotongan.

    Tidak memakai nilai/rumus untuk memilih susunan. Cabang DPP/Grand Total
    yang sudah ada tidak diubah. Nominal positif PPh pada susunan ini
    adalah potongan sesudah pajak; tanda asli angka tetap digunakan.
    """
    if inv.get('nilai',{}).get('dpp_tercetak'):return None
    # PPH23 dan PPH 23 adalah label lengkap yang sama; fullmatch tetap
    # menolak PPH230/PPH23lain. Bbox/urutan/pasangan nominal tetap wajib.
    cols=inv.get('kolom',{});pph=r'pph(?:\s*23)?(?:\s*\([\d.,]+\s*%\))?'
    has_pph=bool(cols.get('pph_tercetak',{}).get('segmen'))
    specs=[('subtotal_faktur',r'sub\s*total')]
    ppn_label=_label_ocr(cols.get('pajak_faktur',{}),r'ppn\s+[\d.,]+\s*%')
    if ppn_label:
        specs += [('pajak_faktur',r'ppn\s+[\d.,]+\s*%'),('total_setelah_pajak',r'total\s+setelah\s+ppn')]
        basis='subtotal_faktur';kind='subtotal_ppn_setelah_ppn'
    else:
        specs += [('diskon_final_nominal',r'(?:discount|diskon)(?:\s+final)?(?:\s+[\d.,]+\s*%)?'),
                  ('total_sebelum_pajak',r'total\s+sebelum\s+pa[ji]ak'),('pajak_faktur',r'pajak'),
                  ('total_setelah_pajak',r'total\s+setelah\s+pajak')]
        basis='total_sebelum_pajak';kind='diskon_sebelum_pajak_setelah_pajak'
    if has_pph:specs.append(('pph_tercetak',pph))
    specs.append(('total_akhir',r'total'))
    labels=_rantai_ocr(inv,specs)
    if not labels:return None
    return {'format_total':kind,'basis_pajak':basis,'memiliki_pph':has_pph,'label':labels,
            'pemilihan_dari_kecocokan_angka':False,
            'rumus_pph':'total_setelah_pajak - pph_tercetak' if has_pph else 'PPh tidak menjadi komponen pada susunan tanpa label PPh',
            'batasan':'Inferensi struktur otomatis; bukan konfirmasi manusia. Tidak mengisi kolom yang tidak ditemukan.'}


def kenali_setelah_diskon(inv):
    specs=[('subtotal_faktur',r'total\s+sebelum\s+diskon'),
           ('diskon_final_nominal',r'diskon\s+akhir'),('total_akhir',r'total\s+setelah\s+diskon')]
    labels=_rantai_ocr(inv,specs)
    return {'format_total':'total_sebelum_diskon_diskon_akhir_total_setelah_diskon','label':labels,
            'pemilihan_dari_kecocokan_angka':False} if labels else None


def asal_subtotal(inv):
    """Kenali bukti turunan lama/baru tanpa menyebutnya nilai tercetak."""
    ev=inv.get('kolom',{}).get('subtotal_faktur',{});method=ev.get('metode','').casefold()
    if 'fallback' in method and 'akumulasi subtotal_item' in method:return 'penjumlahan_item'
    if 'fallback' in method and 'dari total_sebelum_pajak' in method:return 'salinan_total_sebelum_pajak'
    return 'label_tercetak' if ev.get('segmen') or ev.get('label') else 'belum_diketahui'


VERSI_ASAL_SUBTOTAL = 'asal-subtotal-v2'


def tandai_asal_subtotal(inv):
    """Perjelas asal nilai tanpa mengubah nominal atau menyimpulkan absensi.

    Salinan TSP bukan penjumlahan item, dan bukti label TSP tidak menjadi
    bukti SubTotal tercetak. Provenance lama tetap disimpan pada iterasi baru.
    """
    import copy
    ev=inv.get('kolom',{}).get('subtotal_faktur')
    if not ev or not ev.get('nilai'):return
    origin=asal_subtotal(inv)
    if origin not in ('salinan_total_sebelum_pajak','penjumlahan_item'):return
    previous=copy.deepcopy(ev)
    proof={'versi':VERSI_ASAL_SUBTOTAL,'jenis':origin,'subtotal_tercetak_terverifikasi':False,
           'keberadaan_subtotal_tercetak':'belum_dipastikan',
           'bukti_pencarian_label':copy.deepcopy(ev.get('bukti_label_awal',{}))}
    if origin=='salinan_total_sebelum_pajak':
        proof.update(kolom_asal='total_sebelum_pajak',
            nilai_asal=inv['nilai'].get('total_sebelum_pajak',''),
            bukti_asal=copy.deepcopy(inv['kolom'].get('total_sebelum_pajak',{})),
            bukan_penjumlahan_item=True)
        ev['catatan_asal']='Salinan/fallback dari total_sebelum_pajak; bukan penjumlahan item dan bukan pembacaan subtotal tercetak. Keberadaan subtotal tercetak belum dipastikan.'
    else:
        proof['komponen']=[{'nomor_item':it['nilai']['nomor_baris_item'],'nilai':it['nilai']['subtotal_item'],
                           'bukti':copy.deepcopy(it['kolom']['subtotal_item'])} for it in inv['item']]
        ev['catatan_asal']='Penjumlahan subtotal_item; bukan pembacaan subtotal tercetak. Keberadaan subtotal tercetak belum dipastikan.'
    if ev.get('asal_nilai',{}).get('versi')!=VERSI_ASAL_SUBTOTAL:
        ev['riwayat_provenance_sebelum_penjelasan']=previous
    ev['asal_nilai']=proof


def _label_pdf(ev, pattern):
    """Label/fragmen asli point PDF; tidak membuat segmen atau skor OCR."""
    matches=[s for s in ev.get('label',[]) if re.fullmatch(pattern+r'\s*:?',s.get('teks','').strip(),re.I)]
    if len(matches)!=1 or not ev.get('fragmen') or not ev.get('bbox'):return None
    label=matches[0];b=label['bbox'];v=ev['bbox'];h=max(b[3]-b[1],v[3]-v[1])
    if h<=0 or v[0]<b[2] or abs((v[1]+v[3]-b[1]-b[3])/2)>.75*h:return None
    return {'teks':label['teks'],'bbox':b,'halaman':ev.get('halaman'),'satuan':'point PDF',
            'fragmen_nilai':ev['fragmen'],'metode':'Label lengkap dan fragmen kata digital kanan sejajar'}


def kenali_rekap_diskon_ppn_pph(inv):
    """TSP -> Discount -> PPN -> PPh Ps.23 -> Total, dari label/posisi.

    TSP pada format ini mendahului pengurangan footer, bukan basis pajak
    setelah diskon. Tidak memilih cabang menurut kecocokan nilai/rumus.
    """
    if inv.get('nilai',{}).get('dpp_tercetak'):return None
    specs=[('total_sebelum_pajak',r'total\s+sebelum\s+pajak'),
           ('diskon_final_nominal',r'discount'),('pajak_faktur',r'ppn'),
           ('pph_tercetak',r'pph\s+ps\.?\s*23'),('total_akhir',r'total')]
    labels=[]
    for key,pat in specs:
        ev=inv.get('kolom',{}).get(key,{})
        label=_label_ocr(ev,pat) or _label_pdf(ev,pat)
        if not label:return None
        labels.append({'kolom':key,**label})
    if len({x['halaman'] for x in labels})!=1:return None
    if len({x.get('satuan','piksel') for x in labels})!=1:return None
    h=statistics.median(x['bbox'][3]-x['bbox'][1] for x in labels)
    ys=[(x['bbox'][1]+x['bbox'][3])/2 for x in labels]
    if any(not .6*h<=b-a<=4*h for a,b in zip(ys,ys[1:])):return None
    # Label panjang/pendek rata kanan pada format ini.
    right=[x['bbox'][2] for x in labels]
    if max(right)-min(right)>h:return None
    for key in ('diskon_final_nominal','pph_tercetak'):
        ev=inv['kolom'][key];ss=ev.get('segmen',[])
        texts=[s.get('teks_asli','') for s in ss]
        if ev.get('fragmen'):texts.append(' '.join(w['teks'] for w in sorted(ev['fragmen'],key=lambda w:w['bbox'][0])))
        if not any(re.fullmatch(r'(?i)\(\s*(?:Rp\.?|IDR)\s+[\d.,]+\s*\)',t.strip()) for t in texts):return None
    return {'format_total':'sebelum_pajak_sebelum_diskon_ppn_pph_bertanda','label':labels,
            'konvensi_diskon':'magnitude_pengurangan_kurung','konvensi_pph':'pengurangan_bertanda',
            'dasar':'Urutan label dan sel kanan, Discount/PPh kurung mata uang; tidak memilih dari rumus.',
            'rumus':'total_sebelum_pajak - magnitude_discount + pajak_faktur + pph_tercetak_bertanda',
            'subtotal_item':'Sudah setelah diskon item; tidak dikurangi footer lagi dari jumlah subtotal item.',
            'pemilihan_dari_kecocokan_angka':False,'status_manusia':'belum_diverifikasi'}
