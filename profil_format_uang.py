"""Profil format uang berbasis peran/geometri, bukan locale seluruh dokumen.

Modul aditif: nilai produksi terisi tidak ditafsirkan ulang. Keluarga layout
tidak dengan sendirinya membuktikan skala .000. Grammar tetap milik
normalisasi_angka, termasuk penolakan grup rusak.
"""
import copy
import re
from decimal import Decimal
from normalisasi_angka import analisis_angka

VERSI = 'profil-uang-v3-konteks-tabel'
UANG = ('harga_satuan', 'subtotal_item')


def kumpulkan(inv):
    from nilai_review_faktur import pemetaan_sel
    cells = []
    for it in inv['item']:
        for col in UANG:
            ev = it['kolom'][col]
            headers = ev.get('judul_kolom', {}).get('segmen', [])
            pattern = r'harga\s*(?:satuan|@)' if col == 'harga_satuan' else r'sub\s*total'
            header_ok = len(headers) == 1 and bool(re.fullmatch(pattern, headers[0]['teks_asli'].strip(), re.I))
            cells.append({'kolom': col, 'item': it['nilai']['nomor_baris_item'],
                          'nilai_lama': it['nilai'][col], 'teks': ev.get('teks_diambil', ''),
                          'pemetaan_valid': pemetaan_sel(inv, it, col),
                          'header_valid': header_ok, 'bukti': ev})
    return cells


def anchor(cell):
    """Hanya literal uang nonnol yang menyatakan formatter tanpa dua skala."""
    if cell['kolom'] not in UANG or not cell.get('header_valid') or not cell.get('pemetaan_valid'):
        return None
    raw = cell['teks'].strip()
    segs = cell.get('bukti', {}).get('segmen', [])
    if not segs or any(s.get('skor') is None or s['skor'] < .85 for s in segs):
        return None
    patterns = {
        'en_US': r'-?(?:\d{1,3}(?:,\d{3}){2,}|(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2})',
        'id_ID': r'-?(?:\d{1,3}(?:\.\d{3}){2,}|(?:\d+|\d{1,3}(?:\.\d{3})+),\d{2})'}
    for loc, pattern in patterns.items():
        if not re.fullmatch(pattern, raw):
            continue
        a = analisis_angka(raw, loc)
        if a['status'] == 'langsung' and a['nilai'] != '' and Decimal(a['nilai']) != 0:
            return {'locale': loc, 'presisi': 2 if re.search(r'[.,]\d{2}$', raw) else 'integer',
                    'kolom': cell['kolom'], 'item': cell['item'], 'teks': raw,
                    'segmen': copy.deepcopy(segs)}
    return None


def profil(cells):
    """Keluarga dikenali dari header/slot, bukan pelanggan atau dimensi absolut."""
    roles = {c['kolom'] for c in cells if c['kolom'] in UANG and c.get('header_valid') and c.get('pemetaan_valid')}
    names={s['teks_asli'].strip().casefold() for c in cells if c['kolom']=='harga_satuan'
           for s in c.get('bukti',{}).get('judul_kolom',{}).get('segmen',[])}
    family='tabel_harga_at_subtotal' if 'harga @' in names else 'tabel_harga_satuan_subtotal'
    anchors=[a for c in cells if (a := anchor(c))]
    from profil_uang_tabel import bangun
    return {'versi': VERSI, 'keluarga': family if roles == set(UANG) else 'belum_didukung',
            'header_harga_asli':sorted(names),
            'anchor_uang': anchors,'profil_tabel':bangun(cells,anchors),
            'batas': 'Formatter berlaku per kolom; area uang lain, jumlah, persen dan footer tidak menentukan skala. '
                     'Kesamaan grid/header lintas dokumen bukan deklarasi jumlah desimal.'}


def keputusan(cell, profile):
    result = {'nilai': '', 'versi': VERSI, 'status': 'ditahan', 'wajib_review': True,
              'anchor': [], 'alasan': '', 'keluarga': profile['keluarga']}
    if cell.get('nilai_lama', '') != '':
        return {**result, 'nilai': cell['nilai_lama'], 'status': 'nilai_lama_dipertahankan',
                'alasan': 'Bukan pengganti normalizer produksi; tidak mengosongkan atau mengubah nilai terisi.'}
    if profile['keluarga'] == 'belum_didukung' or not cell.get('pemetaan_valid') or not cell.get('header_valid'):
        return {**result, 'alasan': 'Header/peran/geometri sel belum cukup.'}
    from profil_uang_tabel import nilai as nilai_tabel
    table_value=nilai_tabel(cell,profile.get('profil_tabel',{}))
    if table_value:return {**result,**table_value}
    anchors = [a for a in profile['anchor_uang'] if a['kolom'] == cell['kolom'] and a['item'] != cell['item']]
    result['anchor'] = anchors
    if len({a['locale'] for a in anchors}) != 1:
        return {**result, 'alasan': 'Anchor uang satu kolom tidak ada atau bertentangan; tidak menyalin locale footer/kolom sebelah.'}
    loc = anchors[0]['locale']; a = analisis_angka(cell['teks'].strip(), loc)
    result['analisis_normalisasi'] = a; result['locale'] = loc
    raw=cell['teks'].strip()
    spaced=bool(re.fullmatch(r'-?\d{1,3}(?: \d{3})* \d{5}',raw) or
                re.fullmatch(r'-?\d{1,3}(?: \d{3})+ \d{2}',raw))
    precise=[v for v in anchors if v['presisi']==2]
    segs=cell.get('bukti',{}).get('segmen',[])
    if (spaced and len({v['item'] for v in precise})>=2 and len({v['teks'] for v in precise})>=2
        and len(segs)==1 and segs[0].get('skor') is not None and segs[0]['skor']>=.85):
        # Angka hanya dari digit target. Tidak menyentuh token dengan tanda baca
        # (.000 tetap ambigu) atau memilih skala dari Total/rumus/jumlah/persen.
        digits=''.join(raw.lstrip('-').split(' '))
        canonical=('-' if raw.startswith('-') else '')+digits[:-2]+('.' if loc=='en_US' else ',')+digits[-2:]
        parsed=analisis_angka(canonical,loc)
        if parsed['status']=='langsung' and parsed['nilai']!='':
            parsed.update(teks_asli=cell['teks'],status='normalisasi_terstruktur',wajib_review=True,
                          alasan='Pemisah hilang menjadi spasi; presisi dua desimal didukung >=2 literal uang berbeda pada kolom yang sama. Bukan literal standar/verifikasi manusia.')
            return {**result,'nilai':parsed['nilai'],'status':'presisi_kolom_uang_spasi_terbatas',
                'analisis_normalisasi':parsed,'analisis_tanpa_profil':a,'input_grammar':canonical,
                'bukti_presisi_kolom':{'presisi':2,'anchor':precise,'minimum_anchor':2,
                    'batas':'Target satu region, pemetaan/header sah, pola digit-spasi terbatas; bukan footer/kolom lain/rumus. Tanda dan digit utuh.'},
                'alasan':'Formatter .dd dari sedikitnya dua literal nonnol berbeda dalam kolom yang sama; target hanya pola grup3+akhir2 atau grup3+grup5. Normalizer global tetap.'}
    if a['nilai'] == '' or a['status'] != 'langsung':
        return {**result, 'alasan': 'Grammar rusak/ambigu/nonstandar memerlukan bukti lain, bukan perbaikan pemisah bebas.'}
    return {**result, 'nilai': a['nilai'], 'status': 'konteks_kolom_uang',
            'alasan': 'Pemisah/precision dari uang literal lain pada kolom yang sama; bukan nominal anchor atau rumus.'}


def konteks_roi(cell, cells, candidate):
    """Bukti formatter untuk interpretasi ROI terstruktur; tanpa jumlah/Disc/Tax."""
    if not candidate.get('analisis_normalisasi', {}).get('wajib_review'):
        return {'versi': VERSI, 'aturan': 'Grammar literal sel ROI; guard digit/tanda/geometri tetap berlaku.'}
    # Bentuk nonstandar yang sudah diterima grammar lama saja. Tidak .000 tunggal.
    if not re.fullmatch(r'-?\d{1,3}(?:[.,]\d{3})+\.\d{2}', candidate['teks']):
        raise ValueError('ROI nonstandar bukan grup tiga digit + dua desimal yang didukung')
    anchors = [a for c in cells if (a := anchor(c)) and a['presisi'] == 2 and
               (c['item'], c['kolom']) != (cell['item'], cell['kolom'])]
    # Format uang area boleh membantu ROI yang SUDAH menyatakan dua desimal;
    # tidak dipakai menafsirkan .000 tunggal atau nominal dari sel lain.
    if anchors and {a['locale'] for a in anchors} != {'en_US'}:
        raise ValueError('Formatter dua desimal uang independen bertentangan')
    if not anchors:
        # Beberapa cache kehilangan koma di SEMUA kolom uang. Dua sel lain
        # boleh membuktikan presisi .dd, dengan status nonstandar tetap.
        # Ini bukan anchor locale untuk token .000 tunggal pada keputusan().
        for c in cells:
            if (c['item'],c['kolom']) == (cell['item'],cell['kolom']):continue
            if not c['header_valid'] or not c['pemetaan_valid']:continue
            raw=c['teks'].strip()
            if not re.fullmatch(r'-?\d{1,3}(?:\.\d{3})+\.\d{2}',raw):continue
            a=analisis_angka(raw,'en_US')
            if a['status']!='normalisasi_terstruktur' or a['nilai']=='':continue
            segs=c['bukti'].get('segmen',[])
            if not segs or any(s.get('skor') is None or s['skor']<.85 for s in segs):continue
            anchors.append({'locale':'en_US','presisi':2,'kolom':c['kolom'],'item':c['item'],
                            'teks':raw,'segmen':copy.deepcopy(segs),'status':'struktur_nonstandar_perlu_review'})
        if len(anchors)<2:
            raise ValueError('Formatter dua desimal uang independen tidak tersedia; perlu >=2 anchor struktural bila tanpa literal')
    return {'versi': VERSI, 'aturan': 'Raw ROI grup3+akhir .dd; anchor uang literal .dd independen pada tabel. '
            'Tidak memilih nominal/skala dari rumus/jumlah/persen/footer. Interpretasi nonstandar tetap review.',
            'anchor': anchors, 'locale': 'en_US', 'wajib_review': True}
