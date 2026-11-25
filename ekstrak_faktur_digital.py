"""Pilot ekstraksi dari JSON koordinat; tidak membuka PDF, menjalankan OCR, atau membaca acuan dalam parser."""

import argparse
import csv
import hashlib
import json
import re
import statistics
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from normalisasi_angka import analisis_angka, parse_angka

FAKTUR = ('id_faktur nomor_invoice tanggal_invoice nama_penerbit nama_pelanggan contact_person telp email '
          'npwp_pelanggan subtotal_faktur total_sebelum_pajak diskon_final_persen diskon_final_nominal '
          'pajak_faktur total_akhir folder_klien file_sumber status_validasi catatan_validasi '
          'dpp_tercetak dpp_lain_tercetak ppn_persen_tercetak total_setelah_pajak label_pph pph_tercetak '
          'catatan_termin_tercetak halaman_sumber').split()
DETAIL = ('id_faktur nomor_baris_item nama_barang jumlah unit harga_satuan diskon_item_persen subtotal_item '
          'pajak_item_tercetak status_validasi catatan_validasi halaman_sumber').split()
TOLERANSI = Decimal('0.01')
VERSI_LABEL_PDF = 'pdf-label-sel-v3'
HEADER = {'nama_barang': ['nama barang / pesanan', 'nama barang', 'nama barang /pesanan', 'nama barang/pesanan', 'nama barangifesanan', 'nama barangi pesanan', 'nama produk'],
          'jumlah': ['jumlah', 'qty'],
          'unit': ['unit', 'satuan'], 'harga_satuan': ['harga @', 'harga satuan'],
          'diskon_item_persen': ['disc.', 'disc', 'disc %', 'disc.%', 'disc. %', 'diskon %', 'discount %', 'diskon'],
          'subtotal_item': ['sub total', 'subtotal'], 'pajak_item_tercetak': ['pajak']}


def norm(text):
    s = re.sub(r'(?i)nama\s+barang\s*[/i!|1]\s*[pf]esanan', 'nama barang / pesanan', text.strip().rstrip(':').strip())
    s = re.sub(r'(?i)nama\s+barangi[pf]esanan', 'nama barang / pesanan', s)
    return re.sub(r'\s+', ' ', s).casefold()


def cakupan_dari_teks(lines):
    """Indikasi penawaran/SQ menahan transaksi final, tanpa memblokir layout.

    Judul INVOICE/PAID bukan keputusan cakupan. Bukti lengkap tetap disimpan;
    fungsi tidak menggunakan pelanggan, filename atau nominal.
    """
    from cakupan_dokumen import observasi_cakupan
    return observasi_cakupan(lines)


def cy(item):
    return (item['bbox'][1] + item['bbox'][3]) / 2


def tinggi(item):
    return item['bbox'][3] - item['bbox'][1]


def bbox(items):
    return [min(w['bbox'][0] for w in items), min(w['bbox'][1] for w in items),
            max(w['bbox'][2] for w in items), max(w['bbox'][3] for w in items)] if items else None


def teks(items):
    return ' '.join(w['teks'] for w in sorted(items, key=lambda w: w['bbox'][0])).strip()


def angka(raw, locale='en_US'):
    """Grammar eksplisit; lihat analisis_angka untuk kandidat dan alasan review."""
    return parse_angka(raw, locale)


def tanggal(raw):
    bulan = dict(zip(('january february march april may june july august september october november december').split(), range(1, 13)))
    bulan.update(dict(zip(('januari februari maret april mei juni juli agustus september oktober november desember').split(), range(1, 13))))
    bulan.update({'nopember': 11, 'auqust': 8, 'auqustus': 8})
    bulan.update(dict(zip('jan feb mar apr may jun jul aug sep oct nov dec'.split(),range(1,13))))
    val = raw.strip()
    weekday=re.match(r'(?i)^(monday|tuesday|wednesday|thursday|friday|saturday|sunday),\s*',val)
    if weekday:val=val[weekday.end():]
    # Pisahkan huruf bulan dan angka tahun/hari yang menempel tanpa spasi (mis. '03 November2016' -> '03 November 2016')
    val = re.sub(r'([A-Za-z])(\d{4})$', r'\1 \2', val)
    val = re.sub(r'^(\d{1,2})([A-Za-z])', r'\1 \2', val)
    match = re.fullmatch(r'(\d{1,2})[\s-]+([A-Za-z]+)[\s-]+(\d{4})', val)
    if match and match[2].casefold() in bulan:
        result=date(int(match[3]), bulan[match[2].casefold()], int(match[1]))
        if weekday and result.weekday()!='monday tuesday wednesday thursday friday saturday sunday'.split().index(weekday[1].casefold()):raise ValueError('Nama hari bertentangan dengan tanggal tercetak')
        return result.isoformat()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', val):
        return date.fromisoformat(val).isoformat()
    m = re.fullmatch(r'(\d{2})/(\d{2})/(\d{4})', val)
    if m and int(m[1]) > 12:
        return date(int(m[3]), int(m[2]), int(m[1])).isoformat()
    raise ValueError('Format tanggal tidak dikenali atau ambigu; tidak menebak urutan hari/bulan')


class PembacaHalaman:
    def __init__(self, page, sumber):
        self.page, self.sumber = page, sumber
        self.words = page['kata']
        self.lines = [line for line in page['baris'] if line['teks'].strip()]
        self.h = statistics.median(tinggi(w) for w in self.words) if self.words else 10
        self.masalah = []
        self.fields = {}
        self.locale = None

    def evidence(self, items, method, value='', reason=None, labels=None, rule='Pertahankan teks; rapikan spasi antar kata saja.'):
        return {'nilai': value, 'alasan_kosong': reason if value == '' else None,
                'teks_asli': [w['teks'] for w in items], 'halaman': self.page['nomor_halaman'],
                'path_sumber': self.sumber, 'bbox': bbox(items),
                'fragmen': [{'teks': w['teks'], 'bbox': w['bbox']} for w in items],
                'label': [{'teks': w['teks'], 'bbox': w['bbox']} for w in (labels or [])],
                'metode': method, 'aturan_normalisasi': rule}

    def simpan(self, name, items, method, kind='text', reason=None, labels=None):
        raw = teks(items)
        numeric_raw, accounting = raw, None
        value, rule = raw, 'Pertahankan sebagai teks; rapikan spasi antar kata. Nol awal tidak dihapus.'
        if not raw:
            value, reason = '', reason or 'tidak_ditemukan'
        elif kind in ('money', 'percent'):
            # Token mata uang adalah label satuan, bukan digit/pemisah angka.
            # Whitelist satuan eksplisit, bukan pembersihan huruf bebas.
            numeric_raw = re.sub(r'(?:^(?:Rp\.IDR|IDR|Rp\.?)\s+|\s+(?:IDR|Rp\.?)$)', '', raw, flags=re.I)
            accounting = re.fullmatch(r'\(\s*(?:Rp\.?|IDR)\s+([\d.,]+)\s*\)', raw, re.I)
            if accounting:numeric_raw = '-'+accounting[1]
            rule = f'Decimal; format {self.locale}; hapus pemisah ribuan dan tanda persen, desimal menjadi titik; tidak mengganti nilai tercetak.'
            try:
                value = format(angka(numeric_raw, self.locale), 'f')
            except ValueError:
                value, reason = '', 'ambigu'
                self.masalah.append(f'{name}: format angka ambigu ({raw}).')
        elif kind == 'date':
            rule = 'Nama bulan Inggris/Indonesia atau dd/mm/yyyy hanya bila hari>12; menjadi YYYY-MM-DD tanpa menebak dua tanggal yang mungkin.'
            try:
                value = tanggal(raw.lstrip(':').strip())
                if raw.startswith(':'):rule += ' Kolon pemisah label dibuang; fragmen asli tetap.'
            except ValueError:
                value, reason = '', 'ambigu'
                self.masalah.append(f'{name}: tanggal tidak dapat dinormalisasi ({raw}).')
        if name=='nama_pelanggan' and kind=='text' and value.startswith(':'):
            value=value[1:].strip();rule += ' Kolon awal pemisah label-nilai dibuang.'
        ev = self.evidence(items, method, value, reason, labels, rule)
        if kind in ('money','percent'):
            ev['analisis_normalisasi']=analisis_angka(numeric_raw,self.locale)
            if numeric_raw != raw:
                ev['teks_diambil']=numeric_raw
                ev['aturan_pemisahan']=('Kurung akuntansi bermata uang menjadi tanda negatif; teks/fragmen asli tetap.' if accounting else
                                        'Prefix/suffix penuh IDR/Rp/Rp./Rp.IDR eksplisit; kata dan bbox asli tetap disimpan, grammar digit tidak diubah.')
                ev['satuan_mata_uang']={'satuan':'rupiah','versi':VERSI_LABEL_PDF,
                    'token_asli':[{'teks':w['teks'],'bbox':w['bbox']} for w in items if re.fullmatch(r'(?i)Rp\.IDR|IDR|Rp\.?',w['teks'])],
                    'teks_numerik':numeric_raw,'aturan':'Hanya token satuan eksplisit di ujung nilai; digit/tanda/pemisah diproses grammar semula.'}
                if accounting:
                    ev['teks_angka_bertanda']=numeric_raw
                    ev['teks_diambil']=raw
            if ev['analisis_normalisasi']['wajib_review'] and raw:
                self.masalah.append(f'{name}: interpretasi pemisah membutuhkan bukti tambahan; kandidat bukan nilai terverifikasi.')
        self.fields[name] = ev
        return value

    def cari_label(self, aliases, area):
        x0,y0,x1,y1 = area
        return [line for line in self.lines if x0 <= line['bbox'][0] < x1 and y0 <= cy(line) < y1
                and not (norm(line['teks'])=='npwp' and any(norm(prev['teks'])=='alamat'
                    and abs(prev['bbox'][0]-line['bbox'][0])<=.5*self.h
                    and 0<cy(line)-cy(prev)<=1.5*self.h for prev in self.lines))
                and norm(line['teks'].split(':', 1)[0]) in [norm(a) for a in aliases]]

    def fragmen_label(self,line):
        """BBox label berasal dari kata PDF sebelum kolon, bukan bbox substring tebakan."""
        if ':' not in line['teks']:return line
        words=sorted([w for w in self.words if line['bbox'][0]-.1<=w['bbox'][0] and w['bbox'][2]<=line['bbox'][2]+.1
            and abs(cy(w)-cy(line))<=.5*max(tinggi(w),tinggi(line))],key=lambda w:w['bbox'][0])
        colon=next((n for n,w in enumerate(words) if ':' in w['teks']),None)
        if colon is None:return line
        prefix=words[:colon+1]
        return {**line,'teks':line['teks'].split(':',1)[0].strip()+':','bbox':bbox(prefix),
            'baris_asli':line,'kata_label_asli':prefix,'metode_fragmentasi':'Kata PDF asli sampai kolon; nominal inline tidak masuk bbox label.'}

    def nilai_label(self, name, aliases, area, kind='text'):
        labels = self.cari_label(aliases, area)
        if len(labels) != 1:
            if labels:
                self.masalah.append(f'{name}: beberapa label cocok dalam area yang sama.')
            return self.simpan(name, [], 'Label lengkap dalam area', reason='ambigu' if labels else 'tidak_ditemukan', labels=labels)
        label = labels[0]
        # Potong label pada kolon; gunakan kata agar email inline tidak ikut menjadi bbox label.
        lw = sorted([w for w in self.words if abs(cy(w)-cy(label)) <= max(tinggi(w),tinggi(label))*.5
                     and label['bbox'][0]-.1 <= w['bbox'][0] and w['bbox'][2] <= label['bbox'][2]+.1], key=lambda w:w['bbox'][0])
        colon = next((i for i,w in enumerate(lw) if ':' in w['teks']), None)
        edge = lw[colon]['bbox'][2] if colon is not None else label['bbox'][2]
        items = [w for w in self.words if edge+.1 < w['bbox'][0] and w['bbox'][2] <= area[2]
                 and abs(cy(w)-cy(label)) <= .5*max(tinggi(w),tinggi(label))]
        identity = name in ('nomor_invoice','tanggal_invoice','nama_pelanggan','contact_person','telp','email','npwp_pelanggan')
        stops=[]
        if identity:
            registry={'nomor invoice','nomor','nomor sj','nomor so','tanggal','kepada','nama perusahaan',
                      'contact person','up','telp','telp.','telp / fax','telp/fax','telepon','email','npwp','no npwp','pembayaran','alamat npwp'}
            stops=[s for s in self.lines if s is not label and norm(s['teks'].split(':',1)[0]) in registry
                   and s['bbox'][0]>edge and abs(cy(s)-cy(label))<=.5*max(tinggi(s),tinggi(label))]
            # Dua label dapat berada dalam satu baris PDF. Cari urutan kata
            # native yang diakhiri kolon, jangan menghapus nama label dari nilai.
            band=sorted([w for w in self.words if w['bbox'][0]>edge and
                abs(cy(w)-cy(label))<=.5*max(tinggi(w),tinggi(label))],key=lambda w:w['bbox'][0])
            for start in range(len(band)):
                for count in range(1,6):
                    part=band[start:start+count]
                    if len(part)!=count or norm(teks(part).rstrip(':')) not in registry:continue
                    after=start+count
                    colon_end=part[-1]['teks'].endswith(':') or (after<len(band) and band[after]['teks']==':')
                    if colon_end:stops.append({'teks':teks(part),'bbox':bbox(part),'asal':'kata native label dengan kolon'})
            right=min([s['bbox'][0] for s in stops]+[area[2]])
            items=[w for w in items if w['bbox'][2]<right]
            # Kolon delimiter adalah kata native sendiri, bukan bagian identitas.
            items=sorted(items,key=lambda w:w['bbox'][0])
            if items and items[0]['teks']==':':items=items[1:]
        value=self.simpan(name, items, 'Label lengkap + kata di kanan pada tinggi sejajar dalam area', kind,
                           reason='tidak_ditemukan' if not items else None, labels=labels)
        if identity:
            self.fields[name]['bukti_batas_label']={'versi':VERSI_LABEL_PDF,'batas_kanan':right,
                'label_penghalang':[{'teks':s['teks'],'bbox':s['bbox']} for s in stops],
                'aturan':'Nilai berhenti sebelum label kolom tetangga pada band yang sama; semua bbox dari kata/baris native.'}
        return value


def kelompok_baris(words):
    """Klaster geometris dengan toleransi relatif tinggi teks; abaikan ID blok/baris."""
    groups = []
    for word in sorted(words, key=lambda w:(cy(w), w['bbox'][0])):
        target = next((g for g in reversed(groups) if abs(cy(word)-statistics.median(cy(w) for w in g))
                       <= .60*statistics.median([tinggi(word)]+[tinggi(w) for w in g])), None)
        if target is None:
            groups.append([word])
        else:
            target.append(word)
    return [sorted(g, key=lambda w:w['bbox'][0]) for g in groups]


def cek_hitung(name, actual, expected, tolerance=TOLERANSI, **extra):
    if actual is None or expected is None:
        return {'pemeriksaan': name, 'status': 'tidak_dapat_diperiksa', 'aktual': None,
                'harapan_hitung': None, 'selisih': None, 'toleransi': str(tolerance), **extra}
    delta = actual-expected
    return {'pemeriksaan': name, 'status': 'lolos_otomatis' if abs(delta)<=tolerance else 'selisih',
            'aktual': str(actual), 'harapan_hitung': str(expected), 'selisih': str(delta),
            'toleransi': str(tolerance), **extra}


def dec(row, key):
    return Decimal(row[key]) if row.get(key, '') != '' else None


def ekstrak_item(reader, headers, bawah):
    ordered = sorted(headers.items(), key=lambda kv:kv[1]['bbox'][0])
    bounds = [0] + [(a[1]['bbox'][2]+b[1]['bbox'][0])/2 for a,b in zip(ordered,ordered[1:])] + [reader.page['ukuran_halaman_pt']['lebar']]
    # Judul nama barang pendek tidak selalu menutupi lebar isi kolom.
    # Sisakan kolom nama sampai sebelum sisi kiri judul Jumlah. Ini mencegah
    # kata nama barang masuk sel angka hanya karena midpoint dua judul.
    if [key for key,_ in ordered[:2]]==['nama_barang','jumlah']:
        bounds[1]=max(bounds[1],min(headers['jumlah']['bbox'][0]-.5*reader.h,bounds[2]))
    line_boundaries=[]
    header_end=max(h['bbox'][3] for h in headers.values())
    qi=[k for k,_ in ordered].index('jumlah')
    quantity_words=[w for w in reader.words if header_end<cy(w)<bawah and bounds[qi]<=(w['bbox'][0]+w['bbox'][2])/2<bounds[qi+1] and re.fullmatch(r'-?\d+(?:[.,]\d+)?',w['teks'])]
    # Angka pada paragraf footer bukan jangkar tinggi baris tabel. Syaratkan
    # nama, harga dan region subtotal pada y jumlah yang sama.
    roles={k:n for n,(k,_) in enumerate(ordered)}
    def row_role(q,key):
        n=roles[key]
        return [w for w in reader.words if abs(cy(w)-cy(q))<=.6*reader.h and bounds[n]<=(w['bbox'][0]+w['bbox'][2])/2<bounds[n+1]]
    quantity_words=[q for q in quantity_words if row_role(q,'nama_barang') and row_role(q,'harga_satuan') and row_role(q,'subtotal_item')]
    required_bottom=max((cy(w)+reader.h for w in quantity_words),default=bawah-reader.h)
    for n,((_,left),(_,right)) in enumerate(zip(ordered,ordered[1:]),1):
        candidates=[line for line in reader.page.get('garis_tabel_pt',[]) if
                    left['bbox'][2] < line['x'] < right['bbox'][0] and
                    line['y0'] <= min(min((w['bbox'][1] for w in quantity_words),default=header_end),header_end+.5*reader.h)
                    and line['y1'] >= required_bottom]
        if len(candidates)==1:
            bounds[n]=candidates[0]['x'];line_boundaries.append({'batas':n,**candidates[0]})
    start = max(h['bbox'][3] for h in headers.values())
    groups = kelompok_baris([w for w in reader.words if start < cy(w) < bawah])
    # Harga dapat dicetak di bawah token IDR dalam sel yang sama. Sambungkan
    # hanya baris tanpa nama/jumlah, satu nilai tepat di slot mata uang.
    joined=[]
    for group in groups:
        if joined:
            prev=joined[-1]
            cell=lambda g,key:[w for w in g if bounds[[k for k,_ in ordered].index(key)] <= (w['bbox'][0]+w['bbox'][2])/2 < bounds[[k for k,_ in ordered].index(key)+1]]
            # Nominal Disc dapat turun baris bersama uraian nama barang.
            disc=cell(group,'diskon_item_persen');previous_disc=cell(prev,'diskon_item_persen')
            other=[w for w in group if w not in cell(group,'nama_barang') and w not in disc]
            if (not cell(group,'jumlah') and len(disc)==1 and not other and
                re.fullmatch(r'-?\d[\d.,]*',disc[0]['teks']) and len(previous_disc)==1 and
                re.fullmatch(r'(?i)Rp\.?|IDR',previous_disc[0]['teks']) and cell(prev,'jumlah') and
                0<cy(disc[0])-cy(previous_disc[0])<=1.5*reader.h):
                prev.extend(disc)
                if cell(group,'nama_barang'):joined.append(cell(group,'nama_barang'))
                continue
            if (not cell(group,'nama_barang') and not cell(group,'jumlah') and
                cell(prev,'nama_barang') and cell(prev,'jumlah') and
                0 < cy(group[0])-max(cy(w) for w in prev) <= 1.5*reader.h and
                all(any(re.fullmatch(r'(?i)IDR|Rp\.?',p['teks']) and
                            bounds[n] <= (p['bbox'][0]+p['bbox'][2])/2 < bounds[n+1]
                            for p in prev) and
                        re.fullmatch(r'-?\d[\d.,]*',w['teks'])
                    for w in group for n in range(len(ordered))
                    if bounds[n] <= (w['bbox'][0]+w['bbox'][2])/2 < bounds[n+1])):
                prev.extend(group);continue
        joined.append(group)
    groups=joined
    items, tambahan, checks = [], [], []
    for group in groups:
        cells = {name:[w for w in group if bounds[i] <= (w['bbox'][0]+w['bbox'][2])/2 < bounds[i+1]] for i,(name,_) in enumerate(ordered)}
        name = teks(cells['nama_barang'])
        numeric = all(cells[k] for k in ['jumlah','harga_satuan','subtotal_item'])
        metadata = bool(re.match(r'(?i)^(no\.?\s*seri|serial|expired|tanggal\s+expired)\b', name))
        if numeric and name and not metadata:
            saved_fields = reader.fields
            reader.fields = {}
            row = {k:'' for k in DETAIL}
            for key, cell in cells.items():
                row[key] = reader.simpan(key, cell, 'Klaster y + batas kolom dari judul tabel',
                                         'text' if key in ['nama_barang','unit'] else 'money',
                                         labels=[headers[key]])
                reader.fields[key]['bukti_batas_kolom']={'batas_x_pt':bounds,'garis_sumber':line_boundaries,
                    'satuan':'point PDF','metode':'Garis vertikal unik di antara header jika tersedia; selainnya batas dari header.'}
            tax_percent=None
            if 'pajak_item_tercetak' in cells and '%' in teks(cells['pajak_item_tercetak']):
                tax_percent=reader.fields['pajak_item_tercetak']
                tax_percent.update(satuan_tercetak='persen',bukan_nominal_rupiah=True)
                row['pajak_item_tercetak']=''
                reader.fields['pajak_item_tercetak']=reader.evidence([], 'Kolom Tax mencetak persen, bukan nominal Rupiah; lihat tax_persen_tercetak',
                    reason='satuan_persen_bukan_nominal',labels=[headers['pajak_item_tercetak']])
            if 'pajak_item_tercetak' not in cells:
                reader.fields['pajak_item_tercetak']=reader.evidence([], 'Judul kolom pajak item tidak ditemukan; tidak diasumsikan nol',reason='kolom_tidak_ditemukan')
            nominal_discount=None
            disc_cell=cells['diskon_item_persen']
            if any(re.fullmatch(r'(?i)Rp\.?|IDR',w['teks']) for w in disc_cell) and not any('%' in w['teks'] for w in disc_cell):
                nominal_discount=reader.fields['diskon_item_persen']
                nominal_discount.update(satuan_tercetak='rupiah',basis_diskon='potongan_nominal_per_baris',
                    konvensi='Magnitude potongan tercetak; tanda numerik sumber tetap, tidak dihitung dari persen.')
                row['diskon_item_persen']=''
                reader.fields['diskon_item_persen']=reader.evidence([], 'Disc mencetak Rp; persen tidak tercetak, nominal disimpan pada bukti item',reason='satuan_nominal_bukan_persen',labels=[headers['diskon_item_persen']])
            raw_discount = row['diskon_item_persen']
            q, price, sub = dec(row,'jumlah'),dec(row,'harga_satuan'),dec(row,'subtotal_item')
            discount = dec(row,'diskon_item_persen')
            inferred = False
            if nominal_discount is None and '%' not in headers['diskon_item_persen']['teks'] and discount is not None:
                percent_ok = q is not None and price is not None and sub is not None and 0 <= discount <= 100 and abs(q*price*(1-discount/100)-sub)<=TOLERANSI
                nominal_ok = q is not None and price is not None and sub is not None and abs(q*price-discount-sub)<=TOLERANSI
                reader.fields['diskon_item_persen']['uji_satuan_diskon'] = [
                    cek_hitung('hipotesis persen',sub,q*price*(1-discount/100) if None not in (q,price) else None),
                    cek_hitung('hipotesis nominal potongan per baris',sub,q*price-discount if None not in (q,price) else None)]
                if percent_ok and not nominal_ok:
                    inferred = True
                    reader.fields['diskon_item_persen']['metode'] += '; inferensi persen: cocok rumus persen, tidak cocok potongan nominal per baris'
                    reader.masalah.append(f'Item {len(items)+1}: Disc. tanpa tanda %, interpretasi persen merupakan inferensi aritmetika; perlu review manusia.')
                elif 0 <= discount <= 100:
                    # Diskon tanpa simbol % antara 0.00 sampai 100.00 otomatis ditetapkan sebagai persen
                    if discount == 0:
                        reader.fields['diskon_item_persen']['interpretasi_catatan'] = (
                            'Diskon nol; satuan tidak menentukan hasil subtotal (persen 0% = nominal 0). '
                            'Nilai dipertahankan sebagai informasi; tetap perlu review jika satuan diperlukan.'
                        )
                        reader.masalah.append(
                            f'Item {len(items)+1}: Disc. tanpa tanda %, nilai nol; satuan tidak menentukan subtotal.'
                        )
                    else:
                        reader.fields['diskon_item_persen']['interpretasi_catatan'] = (
                            'Diskon tanpa simbol % ditetapkan otomatis ke persen (rentang 0..100).'
                        )
                else:
                    row['diskon_item_persen'] = ''
                    reader.fields['diskon_item_persen'].update(nilai='', alasan_kosong='ambigu')
                    reader.masalah.append(f'Item {len(items)+1}: satuan diskon tidak dapat dipastikan ({raw_discount}).')
            discount = dec(row,'diskon_item_persen')
            expected = (q*price-Decimal(nominal_discount['nilai']) if None not in (q,price) and nominal_discount['nilai']!='' else None) if nominal_discount is not None else (q*price*(1-discount/100) if None not in (q,price,discount) and 0<=discount<=100 else None)
            check = cek_hitung('jumlah * harga - diskon nominal Rupiah = subtotal item' if nominal_discount is not None else 'jumlah * harga * (1 - diskon/100) = subtotal item', sub, expected,
                               nomor_baris_item=len(items)+1, persen_hasil_inferensi=inferred)
            if check['status'] != 'lolos_otomatis':
                reader.masalah.append(f'Item {len(items)+1}: pemeriksaan subtotal {check["status"]}.')
            row['nomor_baris_item'] = str(len(items)+1)
            row['halaman_sumber'] = str(reader.page['nomor_halaman'])
            row['status_validasi'] = 'perlu_review' if inferred or check['status']!='lolos_otomatis' else 'lolos_otomatis_belum_verifikasi_manusia'
            row['catatan_validasi'] = 'Persen diskon diinferensi dari aritmetika; belum diverifikasi manusia.' if inferred else 'Belum diverifikasi manusia.'
            if check['status']!='lolos_otomatis': row['catatan_validasi'] += ' Subtotal: '+check['status']
            items.append({'nilai':row, 'kolom':reader.fields, 'bbox_baris':bbox(group), 'validasi':[check]})
            if nominal_discount is not None:items[-1]['diskon_item_nominal_tercetak']=nominal_discount
            if tax_percent:items[-1]['tax_persen_tercetak']=tax_percent
            reader.fields = saved_fields
            checks.append(check)
        else:
            # Posisi hanya menghasilkan kandidat, bukan hubungan semantik.
            gap_y = cy(group[0])-items[-1]['bbox_baris'][3] if items else None
            indentasi = group[0]['bbox'][0]-items[-1]['bbox_baris'][0] if items else None
            dekat = bool(items) and indentasi > reader.h and 0 <= gap_y < 2.2*reader.h
            alasan = ('Kandidat berasal dari indentasi dan kedekatan posisi saja; cakupan terhadap satu item '
                      'atau seluruh faktur/paket belum dipastikan dan memerlukan review manusia.' if dekat else
                      'Tidak ada kandidat dengan kedekatan posisi yang memadai; hubungan semantik belum dipastikan dan memerlukan review manusia.')
            tambahan.append({**reader.evidence(group,'Keterangan tabel; kandidat geometris tanpa verifikasi hubungan semantik'),
                             'teks':teks(group), 'nomor_baris_item_terkait':None,
                             'calon_item_terdekat':len(items) if dekat else None,
                             'status':'perlu_review', 'status_hubungan':'belum_dipastikan',
                             'bukti_posisi':{'jarak_y_pt':gap_y,'indentasi_pt':indentasi,
                                             'tinggi_teks_acuan_pt':reader.h,'batas_jarak_y_pt':2.2*reader.h},
                             'alasan':alasan})
            reader.masalah.append(f'Hubungan keterangan belum_dipastikan: {teks(group)}. {alasan}')
    return items, tambahan, checks, bounds


def ekstrak_halaman(data, page):
    doc=data['hasil_program']
    r=PembacaHalaman(page, doc['path_sumber'])
    titles=[line for line in r.lines if norm(line['teks']) in ['faktur penjualan','invoice','sales invoice']]
    headers={}
    table_titles=[line for line in r.lines if norm(line['teks']) in HEADER['nama_barang']]
    for name, aliases in HEADER.items():
        if name=='pajak_item_tercetak':aliases=aliases+['tax','tax %','tax%']
        candidates=[line for line in r.lines if norm(line['teks']) in [norm(a) for a in aliases] and ':' not in line['teks']
                    and len(table_titles)==1 and abs(cy(line)-cy(table_titles[0]))<=r.h]
        if len(candidates)==1: headers[name]=candidates[0]
    required_headers=set(HEADER)-{'pajak_item_tercetak'}
    if len(titles)!=1 or not required_headers<=set(headers):
        return None, {'status':'perlu_review','alasan':'Judul tunggal atau kolom tabel lengkap tidak dikenali; multi-faktur/lanjutan belum didukung.', 'bukti_baris':r.lines}
    header_y=cy(headers['nama_barang'])
    if any(abs(cy(h)-header_y)>r.h for h in headers.values()):
        return None, {'status':'perlu_review','alasan':'Judul kolom tidak sejajar.', 'bukti_baris':r.lines}
    width=page['ukuran_halaman_pt']['lebar']
    summary=r.cari_label(['sub total','subtotal'],[width/2,header_y+r.h,width,page['ukuran_halaman_pt']['tinggi']])
    if not summary:
        summary=r.cari_label(['total sebelum pajak','total akhir','grand total','total'],[width/2,header_y+r.h,width,page['ukuran_halaman_pt']['tinggi']])
        summary=[r.fragmen_label(s) for s in summary]
        # Beberapa label ringkasan adalah satu blok vertikal; batas tabel
        # memakai label paling atas, dengan urutan dan alignment yang jelas.
        summary=sorted(summary,key=lambda s:s['bbox'][1])
        if len(summary)>1:
            if (all(abs(s['bbox'][2]-summary[0]['bbox'][2])<=2*r.h for s in summary) and
                all(cy(b)-cy(a)>r.h for a,b in zip(summary,summary[1:]))):
                summary=summary[:1]
    if len(summary)!=1:
        return None, {'status':'perlu_review','alasan':'Batas tabel/total tidak tunggal; kemungkinan halaman lanjutan.', 'bukti_baris':r.lines}
    money=[w for w in r.words if cy(w)>header_y and re.fullmatch(r'\d{1,3}(?:,\d{3})+\.\d{2}',w['teks'])]
    money_id=[w for w in r.words if cy(w)>header_y and re.fullmatch(r'\d{1,3}(?:\.\d{3})+,\d{2}',w['teks'])]
    r.locale='en_US' if money and not money_id else ('id_ID' if money_id and not money else None)
    if r.locale is None and not money and not money_id:
        anchors={loc:[w for w in r.words if cy(w)>header_y and re.fullmatch(pat,w['teks'])]
                 for loc,pat in [('id_ID',r'-?\d{1,3}(?:\.\d{3}){2,}'),('en_US',r'-?\d{1,3}(?:,\d{3}){2,}')]}
        present=[loc for loc,ws in anchors.items() if ws]
        if len(present)==1:r.locale=present[0]
    if r.locale is None: r.masalah.append('Pola pemisah angka halaman tidak dapat ditentukan secara tunggal.')
    title=titles[0]
    invoice_labels=r.cari_label(['nomor invoice'],[0,title['bbox'][3],width,headers['nama_barang']['bbox'][1]])
    split=invoice_labels[0]['bbox'][0]-r.h*.5 if len(invoice_labels)==1 else width/2
    left=[0,title['bbox'][3],split,headers['nama_barang']['bbox'][1]]
    right=[split,left[1],width,left[3]]
    identity_area=[0,title['bbox'][3],width,headers['nama_barang']['bbox'][1]]
    totals=[width/2,summary[0]['bbox'][1]-r.h/2,width,page['ukuran_halaman_pt']['tinggi']]
    row={k:'' for k in FAKTUR}
    for name,aliases,area,kind in [
        ('nama_pelanggan',['kepada','nama perusahaan'],left,'text'),('contact_person',['contact person'],left,'text'),
        ('telp',['telp','telp.','telepon'],left,'text'),('email',['email'],left,'text'),
        ('npwp_pelanggan',['no npwp','npwp'],right,'text'),('nomor_invoice',['nomor invoice'],right,'text'),
        ('tanggal_invoice',['tanggal'],right,'date'),('subtotal_faktur',['sub total','subtotal'],totals,'money'),
        ('total_sebelum_pajak',['total sebelum pajak'],totals,'money'),('pajak_faktur',['pajak','ppn'],totals,'money'),
        ('total_akhir',['total'],totals,'money'),('pph_tercetak',['pph','pph23','pph 23','pph ps. 23','pph ps 23'],totals,'money')]:
        if name in ('nama_pelanggan','contact_person','telp','email','npwp_pelanggan','nomor_invoice','tanggal_invoice'):
            area=identity_area
        row[name]=r.nilai_label(name,aliases,area,kind)
    if not row['nomor_invoice'] and not r.fields['nomor_invoice'].get('label'):
        # Alias lengkap SJ saja; SO tidak pernah menjadi fallback invoice.
        sj_aliases=['nomor sj','no. sj','no sj','nomor s.j','no. surat jalan','nomor surat jalan']
        sj_labels=r.cari_label(sj_aliases,identity_area)
        if len(sj_labels)==1:
            original=r.fields['nomor_invoice']
            sj=r.nilai_label('nomor_invoice',sj_aliases,identity_area,'text')
            if sj:
                row['nomor_invoice']=sj
                r.fields['nomor_invoice'].update(metode='Fallback nomor_invoice dari label Nomor SJ; kata native PDF berbatas label',
                    label_asal='Nomor SJ',status_fallback='belum_dipastikan_sebagai_nomor_invoice',
                    deduplikasi_otomatis_diizinkan=False,bukti_pencarian_invoice_awal=original)
    if not row['nomor_invoice'] and norm(title['teks'])=='invoice':
        identity_area=[0,title['bbox'][3],width,headers['nama_barang']['bbox'][1]]
        numbered=r.cari_label(['nomor'],identity_area)
        if len(numbered)==1:
            row['nomor_invoice']=r.nilai_label('nomor_invoice',['nomor'],identity_area,'text')
            r.fields['nomor_invoice']['label_asal']='Nomor pada dokumen berjudul Invoice; tetap teks termasuk digit panjang.'
        if not row['tanggal_invoice']:
            dates=[s for s in r.lines if identity_area[1]<cy(s)<identity_area[3] and re.fullmatch(r'\d{2}/\d{2}/\d{4}(?:\s+\d{1,2}:\d{2}\s+[AP]M)?',s['teks'].strip())]
            if len(dates)==1 and int(dates[0]['teks'][:2])>12:
                value=datetime.strptime(dates[0]['teks'][:10],'%d/%m/%Y').date().isoformat()
                row['tanggal_invoice']=r.simpan('tanggal_invoice',dates,'Tanggal literal dd/mm/yyyy pada area identitas; hari>12 menolak interpretasi mm/dd',kind='text')
                row['tanggal_invoice']=r.fields['tanggal_invoice']['nilai']=value
                r.fields['tanggal_invoice']['aturan_normalisasi']='Tanggal sebelum jam diparsing dd/mm/yyyy; tidak menebak urutan jika hari dan bulan <=12.'
        if not row['nama_pelanggan']:
            address=[s for s in r.lines if identity_area[1]<cy(s)<identity_area[3] and re.fullmatch(r'(?i)Kepada\s+Yth[,.]?',s['teks'].strip())]
            if len(address)==1:
                after=[s for s in r.lines if s['bbox'][0]<width/2 and 0<s['bbox'][1]-address[0]['bbox'][3]<1.5*r.h]
                if after:
                    nearest=min(s['bbox'][1] for s in after)
                    first=[s for s in after if abs(s['bbox'][1]-nearest)<.5*r.h]
                    if len(first)==1:row['nama_pelanggan']=r.simpan('nama_pelanggan',first,'Baris tunggal terdekat di bawah Kepada Yth pada area identitas',labels=address)
    # Jalankan setelah fallback lama agar nomor header baru tidak menutup
    # pembacaan tanggal/identitas yang sudah didukung pada area bawah judul.
    if norm(title['teks'])=='invoice' and (not row['nomor_invoice'] or not row['tanggal_invoice']):
        from identitas_header_native import identitas_sebelum_judul
        proposals=identitas_sebelum_judul(r,title)
        for key,ev in proposals.items():
            if not row[key]:row[key]=ev['nilai'];r.fields[key]=ev
    publishers=[line for line in r.lines if line['bbox'][3]<title['bbox'][1] and re.match(r'(?i)^(PT\.?|CV\.?|UD\.?)\s*\w',line['teks'])]
    publisher_method='Identitas badan usaha di area kop penerbit; varian spasi OCR/PDF digabung, teks terpanjang dipilih'
    if not publishers:
        total_labels=r.cari_label(['total','grand total'],totals)
        if len(total_labels)==1:
            publishers=[line for line in r.lines if line['bbox'][0]>=width/2 and line['bbox'][1]>total_labels[0]['bbox'][3]
                        and re.match(r'(?i)^(PT\.?|CV\.?|UD\.?)\s+\w',line['teks'])]
            publisher_method='Badan usaha tunggal pada tanda tangan/footer kanan di bawah label Total; bukan logo atau pelanggan'
    if len(publishers)==1:
        row['nama_penerbit']=r.simpan('nama_penerbit',publishers,'Identitas badan usaha di area kop penerbit, di atas judul')
    def _norm_pub(t): return re.sub(r'\s+','',t.strip()).casefold()
    unique_pub={_norm_pub(p['teks']) for p in publishers}
    if len(unique_pub)==1:
        best=max(publishers,key=lambda p:len(p['teks']))
        row['nama_penerbit']=r.simpan('nama_penerbit',[best],
            publisher_method,
            labels=publishers)
    elif publishers:
        row['nama_penerbit']=r.simpan('nama_penerbit',[],'Area kop penerbit; beberapa teks badan usaha berbeda ditemukan',
            reason='ambigu',labels=publishers)
        r.masalah.append('Identitas penerbit tidak tunggal (teks berbeda secara semantik).')
    else:
        row['nama_penerbit']=r.simpan('nama_penerbit',[],'Area kop penerbit',reason='ambigu' if publishers else 'tidak_ditemukan',labels=publishers)
        r.masalah.append('Identitas penerbit tidak tunggal.')
        row['nama_penerbit']=r.simpan('nama_penerbit',[],'Area kop penerbit',reason='tidak_ditemukan',labels=[])
        r.masalah.append('Identitas penerbit tidak ditemukan.')
    pph_labels=r.cari_label(['pph','pph23','pph 23','pph ps. 23','pph ps 23'],totals)
    if len(pph_labels)==1:
        row['label_pph']=r.simpan('label_pph',pph_labels,'Label lengkap pada area total')
        row['label_pph']=pph_labels[0]['teks'].strip().rstrip(':').strip()
        r.fields['label_pph']['nilai']=row['label_pph']
        r.fields['label_pph']['aturan_normalisasi']='Pertahankan ejaan label, hapus kolon penutup dan spasi pinggir.'
    discount_labels=r.cari_label(['discount final','diskon final'],totals)
    discount_before_tax=not discount_labels and len(r.cari_label(['discount','diskon'],totals))==1 and len(r.cari_label(['total setelah pajak'],totals))==1
    signed_recap=not discount_labels and all(len(r.cari_label(aa,totals))==1 for aa in
        [['total sebelum pajak'],['discount'],['ppn'],['pph ps. 23','pph ps 23'],['total']])
    for name,percent in [('diskon_final_persen',True),('diskon_final_nominal',False)]:
        ds=discount_labels
        candidates=[w for w in r.words if ds and w['bbox'][0]>ds[0]['bbox'][2] and abs(cy(w)-cy(ds[0]))<=max(tinggi(w),tinggi(ds[0]))*.5 and w['teks'] not in (':','IDR','Rp','Rp.') and ('%' in w['teks'])==percent]
        if len(ds)==1 and len(candidates)==1:
            row[name]=r.simpan(name,candidates,'Label Discount Final lengkap; pisahkan token bertanda % dan nominal','percent' if percent else 'money',labels=ds)
        else:
            r.simpan(name,candidates,'Label Discount Final',reason='tidak_ditemukan' if not ds else 'ambigu',labels=ds)
            r.fields[name]['nilai']=''
            r.fields[name]['alasan_kosong']='tidak_ditemukan' if not ds else 'ambigu'
            r.masalah.append(name+': pasangan label-nilai belum pasti.')
    if discount_before_tax or signed_recap:
        row['diskon_final_nominal']=r.nilai_label('diskon_final_nominal',['discount','diskon'],totals,'money')
        r.fields['diskon_final_persen']=r.evidence([], 'Label Discount nominal tanpa persen; tidak dihitung balik',reason='tidak_tercetak')
        r.masalah=[m for m in r.masalah if not m.startswith(('diskon_final_persen:', 'diskon_final_nominal:'))]
    for name,aliases in [('dpp_tercetak',['dpp']),('dpp_lain_tercetak',['dpp lain']),
                         ('ppn_persen_tercetak',['ppn']),('total_setelah_pajak',['total setelah pajak']),
                         ('catatan_termin_tercetak',['termin','catatan termin'])]:
        if name=='total_setelah_pajak' and discount_before_tax:
            row[name]=r.nilai_label(name,aliases,totals,'money')
            continue
        found=[line for line in r.lines if any(re.match(r'^'+re.escape(a)+r'(?:\s|:|$)',norm(line['teks'])) for a in aliases)]
        # Jangan menyamakan Total dengan Total Setelah Pajak atau Pajak dengan PPN.
        r.fields[name]=r.evidence(found,'Pencarian label opsional eksplisit seluruh halaman',reason='ambigu' if found else 'tidak_tercetak')
        if found: r.masalah.append(name+': label terdeteksi tetapi format opsional belum didukung, tinjau bukti.')
    grand_dpp=all(len(r.cari_label([label],totals))==1 for label in ['grand total','dpp','dpp lain (dpp x 11/12)'])
    if grand_dpp:
        for key,aliases in [('total_akhir',['grand total']),('dpp_tercetak',['dpp']),
                            ('dpp_lain_tercetak',['dpp lain (dpp x 11/12)']),('total_setelah_pajak',['total setelah pajak']),
                            ('pph_tercetak',['pph 23']),('diskon_final_nominal',['diskon final'])]:
            row[key]=r.nilai_label(key,aliases,totals,'money')
        ppns=[x for x in r.lines if x['bbox'][0]>=totals[0] and totals[1]<=cy(x)<=totals[3]
              and re.fullmatch(r'(?i)PPN\s+[\d.,]+\s*%\s*:?',x['teks'].strip())]
        if len(ppns)==1:
            pp=ppns[0];label=pp['teks'].strip().rstrip(':').strip()
            row['pajak_faktur']=r.nilai_label('pajak_faktur',[label],totals,'money')
            pct_words=[w for w in r.words if '%' in w['teks'] and pp['bbox'][0]-.1<=w['bbox'][0]
                       and w['bbox'][2]<=pp['bbox'][2]+.1 and abs(cy(w)-cy(pp))<=.5*max(tinggi(w),tinggi(pp))]
            row['ppn_persen_tercetak']=r.simpan('ppn_persen_tercetak',pct_words,'Kata persen asli pada label PPN; bbox kata PDF asli','percent',labels=[pp])
        pl=r.cari_label(['pph 23'],totals)
        if len(pl)==1:row['label_pph']=r.simpan('label_pph',pl,'Label PPh/pasal asli di ringkasan')
        for key in ['total_sebelum_pajak','diskon_final_persen']:
            row[key]='';r.fields[key]=r.evidence([], 'Label tidak ditemukan pada format DPP/Grand Total; tidak disalin dari kolom lain',reason='tidak_ditemukan')
        handled=('dpp_tercetak:','dpp_lain_tercetak:','ppn_persen_tercetak:','total_setelah_pajak:','diskon_final_persen:','diskon_final_nominal:')
        r.masalah=[m for m in r.masalah if not m.startswith(handled)]
    items,tambahan,checks,bounds=ekstrak_item(r,headers,summary[0]['bbox'][1]-r.h)
    if not row.get('subtotal_faktur'):
        original_subtotal = r.fields['subtotal_faktur']
        if (row.get('total_sebelum_pajak') and dec(row,'diskon_final_nominal')==Decimal(0)
            and (not items or all(dec(it['nilai'],'subtotal_item') is not None for it in items))):
            row['subtotal_faktur'] = row['total_sebelum_pajak']
            r.fields['subtotal_faktur'] = r.evidence([], 'Fallback global subtotal_faktur dari total_sebelum_pajak', row['subtotal_faktur'])
        else:
            subs_val = [dec(i['nilai'], 'subtotal_item') for i in items]
            if subs_val and all(x is not None for x in subs_val):
                sub_calc = format(sum(subs_val, Decimal(0)), 'f')
                row['subtotal_faktur'] = sub_calc
                r.fields['subtotal_faktur'] = r.evidence([], 'Fallback global subtotal_faktur dari akumulasi subtotal_item', sub_calc)
        if r.fields['subtotal_faktur'] is not original_subtotal:
            r.fields['subtotal_faktur']['bukti_label_awal'] = original_subtotal
            r.fields['subtotal_faktur']['catatan_asal'] = 'Nilai turunan; bukan pembacaan subtotal tercetak.'
    for key in ['nomor_invoice','tanggal_invoice','nama_pelanggan']:
        checks.append({'pemeriksaan':'kelengkapan '+key,'status':'lolos_otomatis' if row[key] else 'tidak_lengkap'})
        if not row[key]: r.masalah.append(key+': wajib direview karena kosong.')
    checks.append({'pemeriksaan':'kelengkapan item','status':'lolos_otomatis' if items else 'tidak_lengkap'})
    if not items: r.masalah.append('Tidak ada item yang dapat dibentuk.')
    subs=[dec(i['nilai'],'subtotal_item') for i in items]
    checks.append(cek_hitung('jumlah subtotal item = subtotal faktur',dec(row,'subtotal_faktur'),sum(subs,Decimal(0)) if subs and all(x is not None for x in subs) else None))
    before,sub,pph,final,disc,tax,pct=[dec(row,k) for k in ['total_sebelum_pajak','subtotal_faktur','pph_tercetak','total_akhir','diskon_final_nominal','pajak_faktur','diskon_final_persen']]
    if grand_dpp:
        checks.append(cek_hitung('DPP = subtotal',dec(row,'dpp_tercetak'),sub))
        checks.append(cek_hitung('DPP lain = DPP * 11/12',dec(row,'dpp_lain_tercetak'),dec(row,'dpp_tercetak')*Decimal(11)/12 if dec(row,'dpp_tercetak') is not None else None))
        dpp_other,pct_ppn,after=dec(row,'dpp_lain_tercetak'),dec(row,'ppn_persen_tercetak'),dec(row,'total_setelah_pajak')
        checks.append(cek_hitung('PPN dari label persen dan DPP lain',tax,dpp_other*pct_ppn/100 if None not in (dpp_other,pct_ppn) else None))
        checks.append(cek_hitung('setelah pajak = DPP + PPN',after,dec(row,'dpp_tercetak')+tax if None not in (dec(row,'dpp_tercetak'),tax) else None))
        checks.append(cek_hitung('Grand Total = setelah pajak + PPH bertanda',final,after+pph if None not in (after,pph) else None))
    elif discount_before_tax:
        after=dec(row,'total_setelah_pajak')
        checks.append(cek_hitung('total sebelum pajak = subtotal - diskon nominal',before,sub-disc if None not in (sub,disc) else None))
        checks.append(cek_hitung('total setelah pajak = sebelum pajak + pajak',after,before+tax if None not in (before,tax) else None))
        checks.append(cek_hitung('total akhir = total setelah pajak',final,after))
        checks.append(cek_hitung('total akhir = sebelum pajak + pajak (pemeriksaan independen)',final,before+tax if None not in (before,tax) else None))
    else:
        checks.append(cek_hitung('subtotal = total sebelum pajak (format PPH tercetak nol)',before,sub if pph==Decimal(0) else None,
                             batasan='PPH bukan nol/belum diketahui: hubungan ini belum didukung, tidak mengasumsikan tanda PPH.'))
        checks.append(cek_hitung('diskon final nominal = total sebelum pajak * persen/100',disc,before*pct/100 if before is not None and pct is not None else None))
        checks.append(cek_hitung('total akhir = total sebelum pajak - diskon final + pajak',final,before-disc+tax if None not in (before,disc,tax) else None))
    for check in checks:
        if check['status']!='lolos_otomatis': r.masalah.append(check['pemeriksaan']+': '+check['status'])
    identity={'nomor_invoice':row['nomor_invoice'],'tanggal_invoice':row['tanggal_invoice'],'nama_penerbit':row['nama_penerbit']}
    if not row['nomor_invoice']: identity['lokasi_ambigu']=[page['nomor_halaman'],title['bbox']]
    row['id_faktur']='FKT_'+hashlib.sha256((data['sha256_sumber_sebelum']+'|'+json.dumps(identity,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()[:24]
    row.update(folder_klien=doc['folder_klien'],file_sumber=doc['path_sumber'],halaman_sumber=str(page['nomor_halaman']),
               status_validasi='perlu_review' if r.masalah else 'lolos_otomatis_belum_verifikasi_manusia',
               catatan_validasi='; '.join(dict.fromkeys(r.masalah)) or 'Seluruh pemeriksaan otomatis yang didukung lolos; belum diverifikasi manusia.')
    for key in FAKTUR:
        if key not in r.fields:
            r.fields[key]=r.evidence([], 'Metadata sumber / identitas internal / hasil validasi',row[key],reason='tidak_tercetak' if not row[key] else None,
                                     rule='Nilai sistem, bukan angka/teks tercetak; id berasal dari hash sumber dan identitas faktur.' if key=='id_faktur' else 'Tidak ada normalisasi isi tercetak.')
    for item in items:
        item['nilai']['id_faktur']=row['id_faktur']
        for key in DETAIL:
            if key not in item['kolom']:
                item['kolom'][key]=r.evidence([], 'Identitas internal / urutan klaster geometris / validasi',item['nilai'][key])
    result={'nilai':row,'kolom':r.fields,'item':items,'keterangan_tabel':tambahan,'validasi':checks,
            'identitas_dalam_dokumen':identity,'format_angka':r.locale,
            'format_total':'DPP_DPP_lain_PPN_Total_Setelah_Pajak_Grand_Total' if grand_dpp else ('diskon_sebelum_pajak_dengan_total_setelah_pajak' if discount_before_tax else 'diskon_final_setelah_total_sebelum_pajak'),
            'bukti_format_angka':money+money_id,
            'area':{'penerbit':[0,0,width,title['bbox'][1]],'pelanggan':left,'identitas_invoice':right,
                    'tabel':[0,header_y,width,summary[0]['bbox'][1]-r.h],'total':totals,'batas_kolom_x':bounds},
            'toleransi_posisi':'Klaster baris 0.45 x median tinggi teks; label/nilai 0.5 x tinggi maksimum pasangan.'}
    from cakupan_dokumen import terapkan_cakupan
    terapkan_cakupan(result,r.lines,width,page['ukuran_halaman_pt']['tinggi'])
    from format_ringkasan import kenali_rekap_diskon_ppn_pph,identitas_invoice_fallback
    fallback=identitas_invoice_fallback(result)
    if fallback:
        result['status_identitas_invoice']=fallback
        result['identitas_lain']={'nomor_sj':r.fields['nomor_invoice']}
    if result.get('cakupan_dokumen'):
        result['status_record']='sementara_cakupan_belum_dipastikan'
    recap=kenali_rekap_diskon_ppn_pph(result)
    if recap:
        ev=r.fields['diskon_final_nominal']
        if dec(row,'diskon_final_nominal') is not None and dec(row,'diskon_final_nominal')<0:
            signed=row['diskon_final_nominal'];magnitude=format(abs(Decimal(signed)),'f')
            row['diskon_final_nominal']=ev['nilai']=magnitude
            ev.update(nominal_pengurangan_bertanda=signed,konvensi_diskon='magnitude_pengurangan_kurung',
                      analisis_normalisasi_bertanda=ev['analisis_normalisasi'])
            ev['analisis_normalisasi']={**ev['analisis_normalisasi'],'nilai':magnitude,
                'aturan':'Magnitude diskon; kurung pengurangan/fragmen dan nilai negatif asli tetap dalam bukti.'}
        result['format_total']=recap['format_total'];result['bukti_format_total']=recap
        from batch_common import validate_invoice
        result['validasi']=validate_invoice(result)
        old_names={c['pemeriksaan'] for c in checks}
        notes=[m for m in r.masalah if not any(m.startswith(n+':') for n in old_names)]
        notes += [c['pemeriksaan']+': '+c['status'] for c in result['validasi'] if c['status'] not in ('lolos_otomatis','tidak_berlaku')]
        row['catatan_validasi']='; '.join(dict.fromkeys(notes))
        r.fields['catatan_validasi']['nilai']=row['catatan_validasi']
    return result, None


def ekstrak_dokumen(data):
    """Tidak menggunakan nama file, acuan_visual_pengguna, atau jawaban evaluasi."""
    digest=data.get('sha256_sumber_sebelum','')
    if not re.fullmatch(r'[0-9a-f]{64}',digest) or digest!=data.get('sha256_sumber_sesudah'):
        raise ValueError('Hash dokumen sumber pada JSON tidak valid atau tidak konsisten.')
    doc=data['hasil_program']
    sales=[p for p in doc['halaman'] if p.get('kategori_sementara')=='faktur_penjualan']
    invoices,log=[],[]
    for page in doc['halaman']:
        entry={'path_sumber':doc['path_sumber'],'halaman':page['nomor_halaman'],'kategori_input':page.get('kategori_sementara')}
        if page.get('kategori_sementara')=='kwitansi':
            entry.update(status='dilewati',alasan='Kwitansi tidak menghasilkan transaksi penjualan tambahan.')
        elif page.get('kategori_sementara')!='faktur_penjualan':
            entry.update(status='perlu_review',alasan='Kategori tidak didukung/halaman lanjutan belum dikenali.',bukti_baris=page.get('baris',[]))
        elif len(sales)>1 or any(re.search(r'(?i)\b(lanjutan|continued)\b',line['teks']) for line in page['baris']):
            entry.update(status='perlu_review',alasan='Lebih dari satu halaman faktur atau penanda lanjutan; penggabungan/pemisahan faktur belum didukung.',bukti_baris=page['baris'])
        else:
            invoice,problem=ekstrak_halaman(data,page)
            if problem: entry.update(problem)
            else:
                invoices.append(invoice)
                entry.update(status='diproses',id_faktur=invoice['nilai']['id_faktur'])
        log.append(entry)
    if any(e['status']=='perlu_review' for e in log):
        for invoice in invoices:
            invoice['nilai']['status_validasi']='perlu_review'
            invoice['nilai']['catatan_validasi']+='; Ada halaman dokumen yang belum dikenali; kemungkinan keterkaitan/lanjutan perlu review.'
            for k in ['status_validasi','catatan_validasi']: invoice['kolom'][k]['nilai']=invoice['nilai'][k]
    return invoices,log


def evaluasi(invoices,log,acuan):
    """Dipanggil hanya setelah ekstraksi selesai; tidak mengubah hasil parser."""
    results=[]
    for expected in acuan:
        matches=[i for i in invoices if Path(i['nilai']['file_sumber']).name==expected['nama_file']]
        differences=[]
        if len(matches)!=1: differences.append(f'Jumlah faktur aktual {len(matches)}, acuan 1.')
        else:
            inv=matches[0]
            for k in ['nomor_invoice','tanggal_invoice']:
                if inv['nilai'][k]!=expected[k]: differences.append(f'{k}: aktual {inv["nilai"][k]!r}, acuan {expected[k]!r}')
            if dec(inv['nilai'],'total_akhir')!=Decimal(expected['total_akhir']): differences.append('Total akhir berbeda.')
            actual_names=[i['nilai']['nama_barang'] for i in inv['item']]
            if actual_names!=expected['nama_barang']: differences.append(f'Item aktual: {actual_names}; acuan: {expected["nama_barang"]}')
            if 'subtotal_item' in expected and [dec(i['nilai'],'subtotal_item') for i in inv['item']]!=[Decimal(v) for v in expected['subtotal_item']]: differences.append('Subtotal item berbeda.')
        results.append({'nama_file':expected['nama_file'],'status':'sesuai_acuan' if not differences else 'berbeda','perbedaan':differences,'acuan':expected})
    return results


def tulis_csv(path,rows,fields):
    with path.open('x',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)


def format_keterangan(nomor_invoice, note):
    kandidat = note['calon_item_terdekat']
    calon = str(kandidat) if kandidat is not None else 'tidak ditemukan'
    return (f"- {nomor_invoice}: {note['teks']}; status hubungan {note['status_hubungan']}; "
            f"kandidat item berdasarkan posisi: {calon}; hubungan item terverifikasi: tidak ditetapkan. "
            f"{note['alasan']}")


def jalankan(input_dir,output_dir,acuan_path=None):
    paths=sorted(input_dir.resolve().glob('*/teks_koordinat.json'))
    if not paths: raise ValueError('Tidak ada JSON teks_koordinat.json pada subfolder input.')
    docs=[(p,json.loads(p.read_text(encoding='utf-8'))) for p in paths]
    output_dir=output_dir.resolve()
    if output_dir.is_relative_to(input_dir.resolve()) or output_dir.is_relative_to(Path(r'C:\Data Klient Zahir')):
        raise ValueError('Keluaran tidak boleh berada di dalam input JSON atau dokumen sumber.')
    invoices,log,inputs=[],[],[]
    for path,data in docs:
        before=hashlib.sha256(path.read_bytes()).hexdigest()
        extracted,entries=ekstrak_dokumen(data)
        invoices.extend(extracted);log.extend(entries)
        inputs.append({'path_json':str(path),'sha256_json':before,'sha256_dokumen':data['sha256_sumber_sebelum']})
    ids=[i['nilai']['id_faktur'] for i in invoices]
    if len(ids)!=len(set(ids)): raise ValueError('Identitas faktur berulang dalam input; tinjau duplikasi JSON sebelum ekspor.')
    evaluated=evaluasi(invoices,log,json.loads(acuan_path.read_text(encoding='utf-8'))) if acuan_path else []
    run=output_dir/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid4().hex[:8])
    run.mkdir(parents=True,exist_ok=False)
    rows=[i['nilai'] for i in invoices];items=[item['nilai'] for i in invoices for item in i['item']]
    tulis_csv(run/'faktur.csv',rows,FAKTUR);tulis_csv(run/'detail_faktur.csv',items,DETAIL)
    evidence={'versi_skema':1,'input':inputs,'log_halaman':log,'faktur':invoices,
              'aturan':'Bukti semua nilai berasal dari JSON teks/bbox. Nilai kosong bukan nol. Diskon tanpa % memerlukan inferensi dan review. ID hash dokumen + identitas faktur; belum deduplikasi lintas salinan.',
              'evaluasi_terpisah':evaluated,'status_manusia':'belum_diverifikasi'}
    (run/'bukti_ekstraksi.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    report=['# Laporan ekstraksi faktur digital', '', f'Hasil aktual: **{len(rows)} faktur**, **{len(items)} item**.', '',
            '## Hasil program', '', '| Invoice | Tanggal | Pelanggan | Item | Total tercetak | Status |','| --- | --- | --- | ---: | ---: | --- |']
    for inv in invoices:
        r=inv['nilai']; report.append(f"| {r['nomor_invoice']} | {r['tanggal_invoice']} | {r['nama_pelanggan']} | {len(inv['item'])} | {r['total_akhir']} | {r['status_validasi']} |")
        print(r['nomor_invoice'],r['tanggal_invoice'],len(inv['item']),r['total_akhir'],r['status_validasi'])
    report+=['','## Halaman dan keterangan tambahan','']
    report += [f"- {Path(e['path_sumber']).name}, halaman {e['halaman']}: {e['status']}. {e.get('alasan','')}" for e in log]
    for inv in invoices:
        for note in inv['keterangan_tabel']:
            report.append(format_keterangan(inv['nilai']['nomor_invoice'],note))
    report+=['','## Validasi dan review','',f'Toleransi aritmetika absolut: {TOLERANSI} satuan uang. Selisih = aktual tercetak - harapan hitung. Angka tercetak tidak dikoreksi.','']
    for inv in invoices:
        report += [f"- {inv['nilai']['nomor_invoice']}: {inv['nilai']['catatan_validasi']}"]
        report += [f"  - {c['pemeriksaan']}: {c['status']}; selisih {c.get('selisih','tidak berlaku')}; toleransi {c.get('toleransi','tidak berlaku')}." for c in inv['validasi']]
        empty=[f"{k} ({v['alasan_kosong']})" for k,v in inv['kolom'].items() if v['nilai']=='']
        report.append('- Nilai kosong: '+', '.join(empty))
    report+=['','## Evaluasi terpisah terhadap acuan pengguna','', 'Acuan tidak dibaca oleh fungsi ekstraksi. Evaluasi hanya membandingkan hasil yang telah terbentuk.','']
    report += [f"- {e['nama_file']}: {e['status']}. {'; '.join(e['perbedaan']) or 'Tidak ada perbedaan pada field yang diberi acuan.'}" for e in evaluated] or ['Evaluasi acuan tidak diminta.']
    report+=['','## Metode dan keterbatasan','',
             'Label lengkap dan area geometris membedakan penerbit, pelanggan, tabel, serta total. Item mengikuti judul kolom dan klaster y relatif tinggi teks, tidak memakai ID blok/baris JSON. Bukti menyimpan fragmen asli, bbox, halaman, metode, alasan kosong dan normalisasi.',
             'Pilot mendukung satu area faktur lengkap dalam dokumen; beberapa halaman faktur, penanda lanjutan, atau susunan ambigu masuk review. Kwitansi tidak menambah transaksi. Kolom opsional yang terdeteksi dalam format belum didukung tetap kosong dengan bukti ambigu. Catatan termin tidak dibentuk sebagai transaksi.',
             'Diskon Disc. tanpa simbol % dapat diinferensi jika hanya rumus persen cocok; tetap perlu review manusia. PPH nol tercetak disimpan; hubungan PPH bukan nol belum didukung. Kelulusan aritmetika tidak sama dengan verifikasi manusia.',
             'ID stabil memakai hash PDF dari JSON dan identitas nomor/tanggal/penerbit; belum menyelesaikan deduplikasi lintas salinan dokumen. Keberhasilan dua sampel bukan estimasi akurasi seluruh arsip.',
             'Tidak membuka atau mengubah PDF, JSON sumber, inventaris, atau review manual; tidak memasang OCR. CSV adalah hasil pilot, bukan dataset final seluruh arsip.','']
    (run/'laporan_ekstraksi.md').write_text('\n'.join(report),encoding='utf-8')
    for info in inputs:
        if hashlib.sha256(Path(info['path_json']).read_bytes()).hexdigest()!=info['sha256_json']: raise ValueError('JSON sumber berubah selama pemrosesan.')
    for name,fields,expected in [('faktur.csv',FAKTUR,rows),('detail_faktur.csv',DETAIL,items)]:
        with (run/name).open(encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f)
            if reader.fieldnames!=fields or list(reader)!=expected: raise ValueError('Verifikasi CSV tersimpan gagal.')
    print('Keluaran:',run)
    return run


def main():
    root=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-dir',type=Path,default=root/'outputs/uji_pdf_digital/20260915T051835Z_6639819b')
    parser.add_argument('--output-dir',type=Path,default=root/'outputs/uji_ekstraksi_faktur')
    parser.add_argument('--acuan',type=Path,help='JSON acuan opsional, hanya dipakai setelah ekstraksi')
    args=parser.parse_args()
    try: jalankan(args.input_dir,args.output_dir,args.acuan)
    except (ValueError,KeyError,OSError,InvalidOperation) as error: parser.exit(1,f'Ekstraksi gagal: {error}\n')


if __name__=='__main__': main()
