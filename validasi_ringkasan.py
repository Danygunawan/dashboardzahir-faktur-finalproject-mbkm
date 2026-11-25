"""Keputusan struktur dan pemeriksaan ringkasan bersama; tidak mengisi nilai.

Label/bbox memilih rumus sebelum aritmetika. Observasi sumber opsional tetap
hash-spesifik, terpisah dari aturan produksi dan tidak menetapkan relasi final.
"""
import copy
import re
import statistics
from decimal import Decimal

VERSI = 'validasi-ringkasan-v2'
CONFIG = {'versi': VERSI, 'toleransi': '0.01', 'basis_dari_hitung': False,
          'gap_atau_harga_at_menetapkan_basis': False,
          'relasi_kandidat_menetapkan_transaksi': False}
PATTERNS = {
    'subtotal_faktur': r'sub\s*total',
    'diskon_final_nominal': r'(?:discount|diskon)\s*final(?:\s+[\d.,]+\s*%)?',
    'total_sebelum_pajak': r'total\s+sebelum\s+pa[ji]ak',
    'pajak_faktur': r'pajak', 'total_akhir': r'(?:grand\s+)?total',
}


def observasi_sah(inv, observation=None):
    obs = observation if observation is not None else inv.get('observasi_validasi', {})
    if not obs:
        return {}
    # Hash hanya mengikat bukti, tidak pernah memilih nominal atau rumus.
    if (obs.get('jenis') != 'observasi_visual_AI' or
        obs.get('hash_sumber') != inv.get('hash_sumber') or
        obs.get('sumber') != inv['nilai'].get('file_sumber') or
        obs.get('konfirmasi_manusia') is not False or not obs.get('rujukan_bukti')):
        return {}
    return obs


def label_ringkasan(inv, key):
    from format_ringkasan import _label_ocr, _label_pdf
    ev = inv.get('kolom', {}).get(key, {})
    pat = PATTERNS[key]
    label = _label_ocr(ev, pat) or _label_pdf(ev, pat)
    if not label:
        return None
    unit = ev.get('satuan_koordinat', label.get('satuan', 'point PDF'))
    box = label['bbox']
    area = inv.get('struktur', {}).get('area_total')
    if area and unit == 'piksel':
        if not (area[0] <= (box[0]+box[2])/2 <= area[2] and area[1] <= (box[1]+box[3])/2 <= area[3]):
            return None
    return {'kolom': key, **label, 'satuan_koordinat': unit,
            'area_ringkasan': area, 'bukti_pasangan_label_nilai': copy.deepcopy(ev)}


def basis_diskon(inv):
    """Dua urutan berbeda; nominal tidak dibaca untuk memilih basis.

    Rantai rapat nominal warisan tetap dikenali. Tanpa SubTotal independen,
    rantai Discount->TSP yang renggang ditahan: Harga@/gap tidak menyelesaikan
    kontradiksi template lama. SubTotal turunan tidak menjadi anchor cetak.
    """
    from format_ringkasan import asal_subtotal
    labels = {k: label_ringkasan(inv, k) for k in PATTERNS}
    result = {'versi': VERSI, 'basis': 'belum_dipastikan',
              'label': [v for v in labels.values() if v],
              'pemilihan_dari_kecocokan_angka': False,
              'alasan': 'Label lengkap, pasangan nominal, area atau urutan belum cukup.'}
    keys = ['diskon_final_nominal', 'total_sebelum_pajak', 'pajak_faktur', 'total_akhir']
    if not all(labels[k] for k in keys):
        return result
    ls = [labels[k] for k in keys]
    if len({(x['halaman'], x['satuan_koordinat']) for x in ls}) != 1 or ls[0]['halaman'] is None:
        return result
    h = statistics.median(x['bbox'][3]-x['bbox'][1] for x in ls)
    xs = [x['bbox'][0] for x in ls]; rights = [x['bbox'][2] for x in ls]
    if h <= 0 or (max(xs)-min(xs) > 3*h and max(rights)-min(rights) > h):
        return result
    cy = lambda x: (x['bbox'][1]+x['bbox'][3])/2
    ordered = sorted(ls, key=cy)
    gaps = [cy(b)-cy(a) for a,b in zip(ordered, ordered[1:])]
    if any(g < .45*h or g > 6*h for g in gaps):
        return result
    d,t,p,f = [cy(labels[k]) for k in keys]
    sub = labels['subtotal_faktur'] if asal_subtotal(inv) == 'label_tercetak' else None
    if sub and ((sub['halaman'], sub['satuan_koordinat']) != (ls[0]['halaman'], ls[0]['satuan_koordinat']) or cy(sub) >= min(d,t)):
        result['alasan'] = 'Anchor SubTotal bertentangan dengan urutan ringkasan.'
        return result
    if t < d < p < f:
        result.update(basis='sebelum_diskon', alasan='TSP mendahului Discount Final, Pajak dan Total utama dalam area ringkasan yang sama.')
    elif d < t < p < f:
        # Profil nominal rapat lama tidak menyatakan tarif pada label.
        # Tarif eksplisit tanpa SubTotal independen masih dapat berasal dari
        # template TSP sebelum diskon: jangan memutuskannya lewat gap saja.
        nominal_rapat = '%' not in labels['diskon_final_nominal']['teks'] and all(g <= 2*h for g in gaps)
        if sub or nominal_rapat:
            result.update(basis='setelah_diskon', alasan=('SubTotal tercetak -> Discount Final -> TSP -> Pajak -> Total.' if sub else
                'Rantai label/nominal rapat Discount Final -> TSP -> Pajak -> Total; tanpa klaim SubTotal tercetak.'))
        else:
            result['alasan'] = 'Discount di atas TSP tanpa anchor SubTotal tercetak dan tanpa profil nominal rapat yang cukup; gap/Harga@ tidak menentukan basis.'
    return result


def metadata_satuan(ev):
    """Satuan eksplisit dari teks sel/header saja; bukan dari nilai atau rumus."""
    head = ev.get('judul_kolom', {})
    ss = ev.get('segmen', []) + head.get('segmen', [])
    texts = [s.get('teks_asli', '') for s in ss] + [s.get('teks', '') for s in ev.get('label', [])]
    texts += [ev.get('teks_diambil', '')]
    native = ev.get('teks_asli', [])
    texts += native if isinstance(native,list) else [native]
    texts += [s.get('teks','') for s in ev.get('fragmen',[])]
    percent = any('%' in t for t in texts)
    rupiah = any(re.search(r'(?i)\b(?:rp\.?|idr|rupiah)\b', t) for t in texts)
    unit = 'persen' if percent and not rupiah else 'rupiah' if rupiah and not percent else 'belum_dipastikan'
    return {'versi': VERSI, 'satuan': unit, 'eksplisit': unit != 'belum_dipastikan',
            'konflik_simbol': percent and rupiah, 'bukti_header': copy.deepcopy(head or ev.get('label', [])),
            'bukti_sel': copy.deepcopy(ev.get('segmen', ev.get('fragmen', []))),
            'alasan': 'Simbol/header sumber; tidak memilih satuan dari nominal, skor atau kecocokan hitung.',
            'status_manusia': 'belum_diverifikasi'}


def siapkan_metadata(inv, observation=None):
    obs = observasi_sah(inv, observation)
    meta = {'versi': VERSI, 'basis_diskon': basis_diskon(inv),
            'observasi_sumber': copy.deepcopy(obs),
            'cakupan_subtotal': obs.get('cakupan_subtotal', 'belum_dipastikan_lokal_atau_dokumen'),
            'hubungan_halaman': 'kandidat_belum_dipastikan' if obs.get('cakupan_ringkasan') == 'kandidat_lintas_halaman' else 'belum_ditetapkan',
            'keberadaan_kolom': {k: 'tidak_tercetak_menurut_observasi_AI' for k in obs.get('tidak_tercetak', [])}}
    inv['metadata_validasi_ringkasan'] = meta
    for it in inv.get('item', []):
        for key in ('pajak_item_tercetak','diskon_item_persen'):
            ev = it.get('kolom', {}).get(key)
            if ev is not None:
                ev['metadata_satuan'] = metadata_satuan({k:v for k,v in ev.items() if k != 'metadata_satuan'})
    return meta


def batas_cakupan(check, inv, keys, observation=None):
    """Simpan hasil hitung bahkan ketika cakupan lokal/kelompok belum pasti."""
    obs = observasi_sah(inv, observation)
    if obs.get('cakupan_ringkasan') == 'kandidat_lintas_halaman':
        check['batas_cakupan_ringkasan'] = 'Blok ringkasan ini; bukan penetapan kelompok final atau transaksi unik.'
    if check.get('peran_pembanding') == 'item_lokal_terhadap_ringkasan' and obs.get('cakupan_subtotal', obs.get('cakupan_ringkasan')) == 'kandidat_lintas_halaman':
        check.update(status_aritmetika=check['status'], status='cakupan_belum_dipastikan',
                     alasan='Subtotal lokal dan ringkasan kandidat lintas halaman belum setara cakupannya; selisih bukan bukti kesalahan digit.',
                     hubungan_final=False, observasi_cakupan=copy.deepcopy(obs))
    return check


def pemeriksaan_subtotal(inv, observation=None):
    from ekstrak_faktur_digital import cek_hitung, dec
    from format_ringkasan import asal_subtotal
    origin = asal_subtotal(inv)
    if origin == 'penjumlahan_item':
        return {'pemeriksaan': 'jumlah subtotal item = subtotal turunan', 'status': 'tidak_berlaku',
                'alasan': 'Komponen yang sama membentuk subtotal turunan; bukan pembandingan angka cetak independen.'}
    subs = [dec(i['nilai'], 'subtotal_item') for i in inv['item']]
    c = cek_hitung('jumlah subtotal item', dec(inv['nilai'], 'subtotal_faktur'), sum(subs, Decimal(0)) if subs and None not in subs else None)
    c['peran_pembanding'] = 'item_lokal_terhadap_ringkasan'
    obs = observasi_sah(inv, observation)
    if not inv['nilai'].get('subtotal_faktur') and 'subtotal_faktur' in obs.get('tidak_tercetak', []):
        c.update(status='tidak_berlaku', alasan='Subtotal ringkasan tidak tercetak menurut observasi AI; komponen item tetap diperiksa sendiri.')
    return batas_cakupan(c, inv, ['subtotal_faktur'], observation)


def pemeriksaan_satuan_pajak(inv):
    checks = []
    for n,it in enumerate(inv.get('item', []),1):
        value = it['nilai'].get('pajak_item_tercetak', '')
        if value == '':
            continue
        unit = metadata_satuan(it.get('kolom', {}).get('pajak_item_tercetak', {}))
        if unit['satuan'] == 'belum_dipastikan' and Decimal(value) != 0:
            checks.append({'pemeriksaan': 'satuan pajak item', 'status': 'satuan_belum_dipastikan',
                           'item': n, 'nilai_dipertahankan': value, 'bukti': unit,
                           'alasan': 'Digit terbaca; header/sel belum memastikan persen atau Rupiah.'})
    return checks


def pemeriksaan_satuan_diskon(inv):
    """Satuan yang tidak tercetak tidak diselesaikan lewat kecocokan rumus."""
    checks=[]
    for n,it in enumerate(inv.get('item',[]),1):
        value=it['nilai'].get('diskon_item_persen','')
        if value=='' or Decimal(value)==0:
            continue
        ev=it.get('kolom',{}).get('diskon_item_persen',{})
        unit=metadata_satuan(ev)
        if unit['satuan']!='persen':
            checks.append({'pemeriksaan':'satuan diskon item','status':'satuan_belum_dipastikan',
                'item':n,'nilai_dipertahankan':value,'bukti':unit,
                'alasan':'Digit Disc terbaca tetapi satuan persen tidak eksplisit pada sel/header; kecocokan hitung bukan konfirmasi satuan.',
                'pemilihan_satuan_dari_hitung':False})
    return checks


def tandai_hipotesis_diskon(checks, inv):
    uncertain={c['item'] for c in pemeriksaan_satuan_diskon(inv)}
    for c in checks:
        if c.get('item') in uncertain and c.get('pemeriksaan')=='subtotal item':
            c['cakupan_hitung']='Hipotesis diskon persen dari kolom sistem; bukan penetapan satuan cetak.'
            c['satuan_diskon_belum_dipastikan']=True
    return checks


def pemeriksaan_ringkasan(inv, observation=None):
    """Hanya format biasa Discount/TSP; profil DPP/PPN/PPh khusus tetap terpisah."""
    from ekstrak_faktur_digital import cek_hitung, dec
    from format_ringkasan import asal_subtotal
    row = inv['nilai']; obs = observasi_sah(inv, observation); profile = basis_diskon(inv); checks = []
    def calc(name, actual, keys, fn):
        vals = [dec(row,k) for k in keys]
        c = cek_hitung(name, dec(row,actual), fn(*vals) if None not in vals else None)
        c.update(item='', kolom_aktual=actual, komponen=keys,
                 komponen_kosong=[k for k in dict.fromkeys([actual]+keys) if row.get(k,'') == ''],
                 versi=VERSI, bukti_basis=profile)
        unavailable = [k for k in c['komponen_kosong'] if k in obs.get('tidak_tercetak', [])]
        if unavailable:
            c.update(status='tidak_berlaku', alasan='Rumus ini memerlukan kolom yang tidak tercetak menurut observasi AI; pemeriksaan lain tetap berlaku.', kolom_tidak_tercetak=unavailable)
        pph = dec(row, 'pph_tercetak')
        if actual == 'total_akhir' and pph is not None and pph != 0:
            c.update(status_aritmetika=c['status'], status='tidak_dapat_diperiksa',
                     akar_masalah='komponen_pph_belum_dipetakan',
                     alasan='Profil biasa belum memastikan peran PPh nonnol; tanda dan angka tetap, tidak diasumsikan nol.')
        checks.append(batas_cakupan(c, inv, [actual]+keys, observation))
    basis = profile['basis']
    if basis == 'belum_dipastikan':
        # Kedua hipotesis disimpan untuk diagnosis, tidak dipakai memilih
        # basis atau meluluskan nilai. Konflik nyata tidak hilang saat hold.
        t,d,p = [dec(row,k) for k in ('total_sebelum_pajak','diskon_final_nominal','pajak_faktur')]
        hypotheses = [cek_hitung('hipotesis TSP setelah diskon',dec(row,'total_akhir'),t+p if t is not None and p is not None else None),
                      cek_hitung('hipotesis TSP sebelum diskon',dec(row,'total_akhir'),t-d+p if None not in (t,d,p) else None)]
        checks.append({'pemeriksaan': 'basis diskon ringkasan', 'status': 'tidak_dapat_diperiksa',
                       'akar_masalah': 'basis_diskon_belum_dipastikan', 'bukti_basis': profile,
                       'alasan': profile['alasan'], 'versi': VERSI,
                       'hipotesis_aritmetika_bukan_pemilih_basis': hypotheses,
                       'konflik_di_kedua_basis': all(c['status']=='selisih' for c in hypotheses)})
        return checks
    if basis == 'setelah_diskon':
        calc('total akhir = sebelum pajak + pajak (diskon sudah diterapkan)', 'total_akhir', ['total_sebelum_pajak','pajak_faktur'], lambda t,p:t+p)
        if asal_subtotal(inv) == 'label_tercetak':
            calc('total sebelum pajak = subtotal - diskon nominal', 'total_sebelum_pajak', ['subtotal_faktur','diskon_final_nominal'], lambda s,d:s-d)
        pct_basis = 'subtotal_faktur'
    else:
        calc('total akhir = basis sebelum diskon - diskon nominal + pajak', 'total_akhir', ['total_sebelum_pajak','diskon_final_nominal','pajak_faktur'], lambda t,d,p:t-d+p)
        pct_basis = 'total_sebelum_pajak'
    pct_ev = inv.get('kolom', {}).get('diskon_final_persen', {})
    pct_text = ' '.join(s.get('teks_asli', '') for s in pct_ev.get('segmen', [])) + pct_ev.get('teks_diambil', '')
    if row.get('diskon_final_persen') and '%' in pct_text:
        calc('diskon nominal dari basis label persen tercetak', 'diskon_final_nominal', [pct_basis,'diskon_final_persen'], lambda b,p:b*p/100 if 0 <= p <= 100 else None)
        c = checks[-1]; p = dec(row, 'diskon_final_persen'); b = dec(row,pct_basis); d = dec(row,'diskon_final_nominal')
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*%', pct_text)
        if m and b is not None and d is not None and p is not None and b != 0:
            places = len(re.split(r'[.,]', m[1])[1]) if re.search(r'[.,]',m[1]) else 0
            half = Decimal(1).scaleb(-places)/2
            c['diagnosis_pembulatan_persen_tampilan'] = {'kompatibel': abs(d-b*p/100) <= abs(b)*half/100,
                'digit_desimal_tampilan': places, 'setengah_interval_persen': str(half),
                'tidak_mengubah_status_selisih_toleransi_atau_angka': True}
    return checks
