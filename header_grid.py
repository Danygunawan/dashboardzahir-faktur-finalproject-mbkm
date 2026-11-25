"""Fallback header rusak: piksel grid dan peran sel, tanpa alias/acuan pelanggan."""
import re
import statistics
from pathlib import Path


PROFILE = ['nama_barang', 'jumlah', 'unit', 'harga_satuan', 'diskon_item_persen', 'subtotal_item', 'pajak_item_tercetak']
VERSI = 'header-grid-v3'


def runs(values):
    groups = []
    for value in values:
        if groups and value <= groups[-1][-1] + 1:
            groups[-1].append(int(value))
        else:
            groups.append([int(value)])
    return [statistics.median(group) for group in groups]


def inferensi_header_grid(reader, title):
    """Tahan bila grid/header/peran baris tidak unik; teks header tidak diganti."""
    import numpy as np
    from PIL import Image
    from batch_common import digest
    if not re.fullmatch(r'(?i)faktur\s+pen[ji]ualan|sales\s+invoice|invoice',title.get('teks_asli','').strip()):
        raise ValueError('Grid memerlukan judul faktur yang didukung')
    source = Path(reader.data['sumber_gambar'])
    if source.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.webp'):
        raise ValueError('Grid fallback hanya gambar sumber satu frame')
    h = reader.data.get('sha256_sumber_sebelum')
    if not h or digest(source) != h or reader.data.get('sha256_sumber_sesudah') != h:
        raise ValueError('Hash gambar/grid tidak cocok')
    with Image.open(source) as im:
        if getattr(im, 'n_frames', 1) != 1 or im.getexif().get(274, 1) != 1 or im.size != (reader.w, reader.height):
            raise ValueError('Frame/orientasi/ukuran grid tidak cocok')
        dark = np.asarray(im.convert('L')) < 90
    center = lambda s: (s['bbox'][1] + s['bbox'][3]) / 2
    height = lambda s: s['bbox'][3] - s['bbox'][1]
    text = lambda s: re.sub(r'\s+', ' ', s['teks_asli'].strip()).casefold()
    def role(s):
        t=text(s).rstrip(':')
        if re.match(r'^nama\b|^nama\s*barang\b',t):return 'nama_barang'
        for k,labels in [('jumlah',('jumlah','qty')),('unit',('unit',)),
                         ('harga_satuan',('harga satuan','harga @','harga','harga@','haga')),
                         ('diskon_item_persen',('disc','disc.','disc.%','disc %','diskon','discount')),
                         ('subtotal_item',('sub total','subtotal')),('pajak_item_tercetak',('pajak','tax','tax%','tax %'))]:
            if t in labels:return k
    anchors = [s for s in reader.s if role(s) and s['bbox'][1] > title['bbox'][3]]
    identity = [s for s in reader.s if title['bbox'][3] < center(s) and re.search(r'(?i)\b(nomor\s*invoice|nomor\s*sj|tanggal)\b', s['teks_asli'])]
    if not identity:
        raise ValueError('Grid tanpa dukungan identitas faktur')
    horizontal = runs(np.flatnonzero(dark.mean(axis=1) > .85))
    candidates = []
    for anchor in anchors:
        band = sorted([s for s in reader.s if abs(center(s) - center(anchor)) <= .60 * max(height(s), height(anchor))], key=lambda s: s['bbox'][0])
        if not 7<=len(band)<=10 or role(band[0])!='nama_barang':
            continue
        above = [v for v in horizontal if 0 < anchor['bbox'][1] - v < 2 * reader.h]
        below = [v for v in horizontal if 0 < v - anchor['bbox'][3] < 2 * reader.h]
        if len(above) != 1 or len(below) != 1:
            continue
        top = below[0]
        bottoms = [v for v in horizontal if v > top + 4 * reader.h]
        if not bottoms:
            continue
        bottom = bottoms[0]
        vertical = runs(np.flatnonzero(dark[int(top) + 3:int(bottom) - 3].mean(axis=0) > .85))
        vertical = [v for v in vertical if reader.h < v < reader.w - reader.h]
        if len(vertical) != 6:
            continue
        bounds = [0] + vertical + [reader.w]
        header_cells=[[s for s in band if bounds[j]<(s['bbox'][0]+s['bbox'][2])/2<bounds[j+1]] for j in range(7)]
        if any(not cell for cell in header_cells) or band[0] not in header_cells[0]:
            continue
        if any(len({tuple(s['bbox']) for s in cell})!=len(cell) for cell in header_cells):continue
        if any(len(cell)>1 for j,cell in enumerate(header_cells) if j!=3):continue
        recognized=[(j,role(s)) for j,cell in enumerate(header_cells) for s in cell if role(s)]
        if any(PROFILE[j]!=k for j,k in recognized):continue
        # Grid7kolom dan >=2baris bersatuan tetap wajib. Anchor Harga yang
        # rusak boleh digantikan >=2peran header lain pada slot yang benar.
        enough_headers=any(k=='harga_satuan' for j,k in recognized) or len({k for j,k in recognized})>=2
        # Fragmen tambahan Harga boleh pecahan OCR yang bersebelahan dalam
        # satu sel grid; semua region asal tetap bukti, tidak membuat kata.
        representatives=[cell[0] for cell in header_cells]
        price_anchors=[s for s in header_cells[3] if role(s)=='harga_satuan']
        if price_anchors:representatives[3]=price_anchors[0]
        rows = []
        for s in sorted((s for s in reader.s if top < center(s) < bottom), key=center):
            group = next((g for g in rows if abs(center(s) - statistics.median(center(v) for v in g)) <= .6 * reader.h), None)
            if group is None:
                rows.append([s])
            else:
                group.append(s)
        support = []
        for group in rows:
            cells = [[s for s in group if bounds[j] <= (s['bbox'][0] + s['bbox'][2]) / 2 < bounds[j + 1]] for j in range(7)]
            if not cells[0] or any(len(cells[j]) != 1 for j in range(1, 7)):
                continue
            if not re.fullmatch(r'-?\d+(?:[.,]\d+)?', cells[1][0]['teks_asli'].strip()):
                continue
            if not re.search(r'[A-Za-z]', cells[2][0]['teks_asli']):
                continue
            if not all(re.fullmatch(r'-?[\d.,]+', cells[j][0]['teks_asli'].strip()) for j in (3, 5)):
                continue
            if not all(re.fullmatch(r'-?\d+(?:[.,]\d+)?\s*%', cells[j][0]['teks_asli'].strip()) for j in (4, 6)):
                continue
            support.append(group)
        alternative=False
        if len(support)==1:
            # Satu baris membutuhkan bukti tambahan yang tidak disyaratkan
            # profil multi-baris: dua label identitas, unit eksplisit, dua
            # nominal dengan >=2grup dan dua kolom persen eksplisit.
            group=support[0]
            cells=[[s for s in group if bounds[j]<=(s['bbox'][0]+s['bbox'][2])/2<bounds[j+1]] for j in range(7)]
            footer=[s for s in reader.s if center(s)>bottom and s['bbox'][0]>reader.w/2
                    and text(s).rstrip(':').strip() in ('discount final','total sebelum pajak','pajak','total')]
            footer_roles={text(s).rstrip(':').strip() for s in footer}
            identity_date=any(re.search(r'(?i)\btanggal\b',s['teks_asli']) for s in identity)
            identity_number=any(re.search(r'(?i)\bnomor\s*(?:invoice|sj)\b',s['teks_asli']) for s in identity)
            alternative=(identity_date and identity_number and len(footer_roles)>=3
                and {'total','total sebelum pajak'}.issubset(footer_roles) and role(header_cells[0][0])=='nama_barang'
                and re.fullmatch(r'(?i)paket|modul|unit|tahun|set|sesi|user|bulan|hari',cells[2][0]['teks_asli'].strip()) is not None
                and all(re.fullmatch(r'-?\d{1,3}(?:,\d{3}){2,}|-?\d{1,3}(?:\.\d{3}){2,}',cells[j][0]['teks_asli'].strip()) for j in (3,5))
                and all(s.get('skor_keyakinan',0)>=.85 for cell in cells[1:] for s in cell))
        if not ((len(support)>=2 and enough_headers) or alternative):
            continue
        key=tuple(tuple(s['bbox']) for s in representatives)
        if any(tuple(tuple(s['bbox']) for s in hh.values())==key for hh,pp in candidates):continue
        candidates.append((dict(zip(PROFILE, representatives)), {
            'versi':VERSI,
            'metode': 'Inferensi tujuh kolom dari grid sumber dan baris bersatuan; profil satu baris memerlukan dukungan alternatif eksplisit',
            'profil_satu_baris_alternatif':bool(alternative),
            'bukti_alternatif_identitas':reader.bukti(segments=identity,method='Label identitas asli'),
            'bukti_alternatif_footer':reader.bukti(segments=footer,method='Label ringkasan asli') if alternative else None,
            'anchor_peran_terbaca':[{'kolom':k,'slot':j} for j,k in recognized],
            'kolom_diinferensi': [k for k in PROFILE if k != 'harga_satuan'],
            'batas_kolom_grid': bounds,
            'grid_sumber': {'hash_sumber': h, 'path_sumber': str(source), 'satuan': 'piksel', 'baris_header': [above[0], top],
                            'bawah_tabel': bottom, 'garis_vertikal': vertical, 'ambang_gelap': 90, 'fraksi_garis_minimal': .85},
            'segmen_header': reader.bukti(segments=band, method='Header OCR asli rusak dipertahankan, peran diinferensi dari grid'),
            'fragmen_per_sel_header':{k:reader.bukti(segments=cell,method='Region asli dalam batas sel grid; bukan bbox kata buatan') for k,cell in zip(PROFILE,header_cells)},
            'baris_pendukung': [reader.bukti(segments=g, method='Posisi jumlah/unit/nominal/persen; bukan kecocokan hitungan') for g in support],
            'status': 'inferensi_struktur_perlu_review'
        }))
    if len(candidates) != 1:
        raise ValueError('Grid/header/peran baris tidak tunggal atau tidak cukup')
    return candidates[0]
