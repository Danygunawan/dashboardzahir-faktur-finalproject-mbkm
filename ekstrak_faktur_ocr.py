"""Ekstraksi pilot segmen OCR piksel; tanpa OCR ulang dan tanpa acuan dalam parser."""
import argparse
import hashlib
import json
import re
import statistics
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from ekstrak_faktur_digital import FAKTUR, DETAIL, HEADER, angka, tanggal, cek_hitung, dec, TOLERANSI, tulis_csv, cakupan_dari_teks
from format_ringkasan import kenali_ringkasan_nominal, identitas_invoice_fallback, kenali_setelah_pajak, kenali_setelah_diskon, kenali_rekap_diskon_ppn_pph
from normalisasi_angka import analisis_angka

VERSI_LAYOUT = 'layout-ringkasan-v3'


def norm(s):
    s = re.sub(r'(?i)^nama\s+barang\s*(?:/\s*)?pesana[nr]\s*$', 'nama barang / pesanan', s.strip())
    t = re.sub(r'(?i)nama\s+barang\s*[/i!|1]\s*[pf]esanan', 'nama barang / pesanan', s.strip().rstrip(':').strip())
    t = re.sub(r'(?i)nama\s+barangi[pf]esanan', 'nama barang / pesanan', t)
    return re.sub(r'\s+', ' ', re.sub(r'\s*/\s*', '/', t)).casefold()


def cy(s): return (s['bbox'][1]+s['bbox'][3])/2
def s_box(s): return s.get('bbox') or s.get('bbox_asal')

def ht(s): return s['bbox'][3]-s['bbox'][1]

def cy(s): b = s_box(s); return (b[1]+b[3])/2


def ht(s): b = s_box(s); return b[3]-b[1]


def sejajar(a,b): return abs(cy(a)-cy(b)) <= .60*max(ht(a),ht(b))


def input_angka_sumber(raw):
    """Mata uang/kurung akuntansi eksplisit; tidak memperbaiki pemisah."""
    text=raw.strip()
    match=re.fullmatch(r'\(\s*(?:Rp\.?|IDR)\s+([\d.,]+)\s*\)',text,re.I)
    if match:return '-'+match[1], 'Kurung akuntansi dengan mata uang eksplisit menjadi tanda negatif; teks/bbox asal tetap.'
    return re.sub(r'^(?:IDR|Rp\.?)\s+', '', text, flags=re.I), 'Awalan mata uang eksplisit dipisahkan; grammar angka tidak dilonggarkan.'


def pilih_header(r, title):
    """Band header tunggal; fallback profil hanya dengan baris tabel pendukung."""
    aliases={k:{norm(a) for a in aa} for k,aa in HEADER.items()}
    aliases['_tax_persen']={'tax','tax %','tax%'}
    anchors=[s for s in r.s if norm(s['teks_asli']) in aliases['nama_barang'] and cy(s)>title['bbox'][3]]
    candidates=[]
    profile=['nama_barang','jumlah','unit','harga_satuan','diskon_item_persen','subtotal_item','pajak_item_tercetak']
    for anchor in anchors:
        band=sorted([s for s in r.s if sejajar(s,anchor)],key=lambda s:s['bbox'][0])
        found={}
        for k,aa in aliases.items():
            hits=[s for s in band if norm(s['teks_asli']) in aa]
            if len(hits)>1:break
            if hits:found[k]=hits[0]
        else:
            mandatory=profile[:-1]
            inferred=[]
            if not all(k in found for k in mandatory):
                # Profil legacy 7 kolom: dua label jangkar jelas, sisanya region
                # header tersendiri dan minimal dua baris data dengan peran sel.
                if len(band)!=7 or found.get('nama_barang') is not band[0] or found.get('harga_satuan') is not band[3]:continue
                if any(k not in profile or band[profile.index(k)] is not s for k,s in found.items()):continue
                edges=[0]+[(a['bbox'][2]+b['bbox'][0])/2 for a,b in zip(band,band[1:])]+[r.w]
                ytop=max(s['bbox'][3] for s in band);rows=[]
                for s in r.s:
                    if not ytop<cy(s)<ytop+12*r.h:continue
                    g=next((g for g in rows if abs(cy(s)-statistics.median(cy(v) for v in g))<=.6*r.h),None)
                    if g is None:rows.append([s])
                    else:g.append(s)
                support=0
                for group in rows:
                    cells=[[s for s in group if edges[i]<=(s['bbox'][0]+s['bbox'][2])/2<edges[i+1]] for i in range(7)]
                    if (cells[0] and all(len(cells[i])==1 for i in (1,2,3,4,5)) and
                        re.fullmatch(r'-?\d+(?:[.,]\d+)?',cells[1][0]['teks_asli'].strip()) and
                        all(re.fullmatch(r'-?[\d.,]+%?',cells[i][0]['teks_asli'].strip()) for i in (3,4,5)) and
                        re.search(r'[A-Za-z]',cells[2][0]['teks_asli'])):support+=1
                if support<2:continue
                inferred=[k for k in profile if k not in found]
                found={k:s for k,s in zip(profile,band)}
            candidates.append((found,{'metode':'Band header berdasarkan label utuh dan posisi',
                                      'kolom_diinferensi':inferred,
                                      'segmen_header':r.bukti(segments=band,method='Region header OCR asli; label rusak tidak diganti'),
                                      'status_hubungan':'inferensi_struktur_belum_verifikasi_manusia' if inferred else 'label_dan_posisi'}))
    if not candidates:
        from header_grid import inferensi_header_grid
        try:
            return inferensi_header_grid(r,title)
        except (ValueError,OSError,KeyError):
            pass
    if len(candidates)!=1:raise ValueError('Band header tabel tidak tunggal atau dukungan struktur tidak cukup; perlu review')
    return candidates[0]


def judul_typo_didukung(data):
    r=PembacaOCR(data)
    titles=[s for s in r.s if norm(s['teks_asli'])=='faktur peniualan' and cy(s)<r.height*.35]
    if len(titles)!=1:return None
    if any(re.search(r'(?i)\bkwitansi|kuitansi|penawaran|registrasi|faktur pajak\b',s['teks_asli']) and cy(s)<r.height*.35 for s in r.s):return None
    try:headers,proof=pilih_header(r,titles[0])
    except ValueError:return None
    top=titles[0]['bbox'][3];bottom=min(s['bbox'][1] for s in headers.values())
    identity=[s for s in r.s if top<cy(s)<bottom]
    predicates=[r'(?i)^kepada\s*:?',r'(?i)^(?:nomor\s*(?:invoice|sj)|no\.?\s*invoice)\s*:?',r'(?i)^tanggal\s*:?']
    if not all(any(re.match(p,s['teks_asli'].strip()) for s in identity) for p in predicates):return None
    return {'judul':titles[0],'bukti_header':proof,'bukti_identitas':r.bukti(segments=[s for s in identity if any(re.match(p,s['teks_asli'].strip()) for p in predicates)],method='Label identitas mendukung judul typo spesifik')}


class PembacaOCR:
    def __init__(self,data):
        if data['satuan_koordinat']!='piksel' or any(s['tingkat_segmen']!='baris' for s in data['segmen']):
            raise ValueError('Hanya segmen baris/region piksel yang didukung; tidak mengonversi menjadi kata PDF.')
        self.data=data; self.s=sorted(data['segmen'],key=lambda s:(cy(s),s['bbox'][0]))
        if len({s['id_segmen'] for s in self.s})!=len(self.s): raise ValueError('ID segmen tidak unik')
        if {s['nomor_halaman'] for s in self.s}!={1}: raise ValueError('Pilot satu halaman virtual; perlu review susunan lainnya')
        self.h=statistics.median(ht(s) for s in self.s); self.locale=None; self.masalah=[]
        self.w=data['ukuran_gambar']['lebar']; self.height=data['ukuran_gambar']['tinggi']
        for s in self.s:
            if not (0<=s['bbox'][0]<s['bbox'][2]<=self.w and 0<=s['bbox'][1]<s['bbox'][3]<=self.height):
                raise ValueError('BBox tidak valid/di luar gambar; perlu review')
        self.low=[s for s in self.s if s.get('skor_keyakinan') is None or s['skor_keyakinan']<.85]
        if self.low: self.masalah.append('Skor OCR < 0.85 atau tidak tersedia: '+', '.join(s['id_segmen'] for s in self.low)+'. Skor tinggi bukan verifikasi manusia.')

    def area(self,area):
        x0,y0,x1,y1=area
        return [s for s in self.s if x0<=s['bbox'][0]<x1 and (s['bbox'][2]<=x1 or (s['bbox'][0]+s['bbox'][2])/2<x1) and y0<=cy(s)<y1]

    def bukti(self,value='',segments=(),method='',reason=None,rule='Teks OCR dipertahankan; trim spasi pinggir.'):
        return {'nilai':value,'alasan_kosong':reason if value=='' else None,'metode':method,
                'aturan_normalisasi':rule,'satuan_koordinat':'piksel','halaman':1,
                'sumber_gambar':self.data['sumber_gambar'],
                'segmen':[{'id_segmen':s['id_segmen'],'teks_asli':s['teks_asli'],'bbox_asal':s['bbox'],
                           'poligon_asal':s['poligon'],'halaman':s['nomor_halaman'],'skor':s['skor_keyakinan'],
                           'tingkat_segmen':s['tingkat_segmen']} for s in segments]}

    def nilai(self,raw,segments,method,kind='text',reason=None):
        value=raw.strip(); rule='Trim spasi dan pemisah label : di awal; bbox tetap seluruh segmen asal, bukan bbox substring.'
        numeric_input=None
        if kind=='angka':value,numeric_input=input_angka_sumber(raw)
        reason=reason or ('tidak_ditemukan' if not value else None)
        error=None; reread=None;analysis=None
        column=method.removeprefix('Sel tabel ') if method.startswith('Sel tabel ') else None
        local_evidence=getattr(self,'locale_kolom',{}).get(column)
        numeric_locale=local_evidence['locale'] if local_evidence else self.locale
        total_context=None
        if kind=='angka' and analisis_angka(value,numeric_locale)['status'] in ('ambigu','rusak'):
            from konteks_angka_total import locale_total
            total_context=locale_total(value,segments,self.s,self.w)
            if total_context:numeric_locale=total_context['locale']
        format_context=None
        if kind=='angka' and analisis_angka(value,numeric_locale)['status'] in ('ambigu','rusak'):
            from konteks_format_uang import grand_nonstandar,ringkasan_integer
            format_context=grand_nonstandar(value,segments,self.s,self.w) or ringkasan_integer(value,segments,self.s,self.w)
            if format_context:numeric_locale=format_context['locale']
        try:
            if value and kind=='angka':
                analysis=analisis_angka(value,numeric_locale)
                rule=f'Grammar eksplisit format {numeric_locale}; Decimal, tanpa hitung balik atau penghapusan bebas tanda baca.'
                if total_context and total_context.get('teks_integer_konteks'):
                    value=total_context['teks_integer_konteks']
                    analysis={**analisis_angka(value,numeric_locale),'teks_asli':raw,
                        'status':'normalisasi_terstruktur','wajib_review':True,
                        'alasan':'Pemisah campuran sel Total ditafsirkan dengan skala terbilang independen; bukan literal standar.'}
                    rule+=' Grup tiga digit yang valid dipastikan sebagai integer oleh skala terbilang lengkap pada sel Total.'
                if format_context:
                    value=format_context['input_grammar']
                    analysis={**analisis_angka(value,numeric_locale),'teks_asli':raw,'status':'normalisasi_terstruktur',
                        'wajib_review':True,'aturan':format_context['aturan'],'alasan':'Interpretasi format sumber nonstandar; bukan literal standar/konfirmasi manusia.'}
                    rule+=' '+format_context['aturan']
                value=format(angka(value,numeric_locale),'f')
            elif value and kind=='tanggal':
                # Hanya tahun di akhir field tanggal berbulan huruf; bukan
                # penghapusan spasi bebas atau penebakan dari tanggal lain.
                joined=re.sub(r'(?<=[A-Za-z])\s+(\d{2})\s+(\d{2})$',r' \1\2',value)
                value=tanggal(joined); rule='Hari + nama bulan Inggris/Indonesia + tahun menjadi YYYY-MM-DD.'
                if joined!=raw.strip():rule+=' Dua pasangan digit tahun yang terpisah spasi disatukan pada akhir field tanggal; raw dan bbox utuh dipertahankan.'
            elif kind in ('email','npwp'):
                if not re.search(r'\w',value): value=''; reason='tidak_tercetak_dalam_OCR'
                elif kind=='email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):
                    raise ValueError('Format email tidak valid')
                elif kind=='npwp' and not re.fullmatch(r'[\d.\-\s]+',value): raise ValueError('Format NPWP tidak dikenali')
        except ValueError as e:
            error=str(e); value=''; reason='gagal_parsing_atau_ambigu'
        if kind=='angka' and (error or analysis and analysis['wajib_review']):
                matches=[x for x in self.data.get('pembacaan_ulang_angka',[]) if
                         x.get('hash_sumber')==self.data['sha256_sumber_sebelum'] and
                         x.get('locale')==numeric_locale and x.get('kandidat') and
                         x.get('nomor_halaman')==self.data.get('halaman_dokumen',1) and
                         any(x['segmen']==s and s['teks_asli'].strip()==raw.strip() for s in segments)]
                if len(matches)==1:
                    from baca_ulang_angka import kandidat_angka
                    from batch_common import read_json,complete,digest
                    x=matches[0]; raw_path=Path(x['raw_json'])
                    if (complete(raw_path.parent) and read_json(raw_path.parent/'bukti.json')==x and
                        digest(x['path_sumber'])==x['hash_sumber']==x['sha256_sumber_sesudah']):
                        fresh=kandidat_angka(read_json(raw_path),raw,numeric_locale)
                        if fresh and all(x['kandidat'].get(k)==v for k,v in fresh.items()):
                            reread=x; value=fresh['nilai']; reason=None
                            self.masalah.append('Angka dari OCR ulang potongan: '+raw+' -> '+fresh['teks']+'; kandidat, belum diverifikasi manusia.')
        if error and reread is None:self.masalah.append(f'{method}: {raw!r}: {error}')
        ev=self.bukti(value,segments,method,reason,rule); ev['teks_diambil']=raw; ev['error_parsing']=error
        if kind=='angka':
            if raw.strip()!=input_angka_sumber(raw)[0]:ev['bukti_pemisahan_mata_uang']={'teks_asli':raw,'input_angka':input_angka_sumber(raw)[0],'aturan':numeric_input}
            analysis=analysis or analisis_angka(raw,numeric_locale)
            ev['analisis_normalisasi']=analysis
            ev['locale_pemakaian']=numeric_locale
            if local_evidence:ev['bukti_locale_kolom']=local_evidence
            if local_evidence and local_evidence.get('wajib_review'):
                analysis.update(wajib_review=True,status='normalisasi_terstruktur',alasan='Locale kolom uang diinferensi dari pola lokal dan bukti independen; perlu review.')
            if format_context:
                ev['bukti_format_ringkasan']=format_context
                ev['analisis_normalisasi_tanpa_konteks']=analisis_angka(raw,self.locale)
            if total_context:
                ev['bukti_locale_total']=total_context
                ev['analisis_normalisasi_tanpa_konteks']=analisis_angka(raw,self.locale)
            if analysis['wajib_review']:self.masalah.append(f'{method}: interpretasi pemisah memerlukan bukti tambahan; teks asli dipertahankan.')
        if reread:
            ev.update(error_parsing=None,error_parsing_awal=error,pembacaan_ulang=reread,
                      metode=method+'; kandidat OCR potongan tervalidasi format, bukan verifikasi manusia',
                      aturan_normalisasi=rule+' Input numerik berasal dari teks OCR potongan; teks baseline tetap disimpan.')
            ev['analisis_normalisasi_baseline']=analysis
            ev['analisis_normalisasi']=analisis_angka(reread['kandidat']['teks'],numeric_locale)
        return ev

    def total_akhir(self, area):
        """Label akhir eksplisit didahulukan dari label antara, bukan urutan/rumus.

        Dua label utama tetap ambigu. Label utama tanpa pasangan nominal tidak
        diganti Saldo atau nilai antara. Semua region asli tetap menjadi bukti.
        """
        groups={'utama':[], 'antara':[]}
        for s in self.area(area):
            head=norm(s['teks_asli'].split(':',1)[0])
            if head in ('total','grand total'):groups['utama'].append(s)
            elif head=='total setelah diskon':groups['antara'].append(s)
        chosen=groups['utama'] or groups['antara']
        proof={'versi':VERSI_LAYOUT,'label_utama':self.bukti(segments=groups['utama']),
               'label_antara':self.bukti(segments=groups['antara']),
               'aturan':'Label utuh utama versus antara dalam area ringkasan; pasangan nominal kanan/inline. Tidak memakai urutan, skor atau rumus.'}
        blocked=not groups['utama'] and chosen and any(
            cy(s)>cy(chosen[0]) and re.fullmatch(r'(?i)(?:pajak|ppn|pph)(?:\s+.*)?',s['teks_asli'].strip().rstrip(':'))
            for s in self.area(area))
        if len(chosen)!=1 or blocked:
            ev=self.bukti('',chosen,'Pemilihan label Total akhir', 'ambigu' if chosen else 'tidak_ditemukan')
            proof['alasan']='Label akhir tidak tunggal atau fallback masih diikuti penyesuaian pajak/PPh.'
        else:
            label=chosen[0]['teks_asli'].split(':',1)[0].strip()
            ev=self.label([label],area,'angka')
            proof['jenis_label']='utama' if groups['utama'] else 'fallback_antara_tanpa_penyesuaian_berikutnya'
        ev['pemilihan_label_total']=proof
        return ev

    def label(self,aliases,area,kind='text',optional=False):
        candidates=[]
        for s in self.area(area):
            text=s['teks_asli'].strip()
            for alias in aliases:
                m=re.fullmatch(re.escape(alias)+r'\s*(?::\s*(.*))?',text,re.I)
                pat=re.sub(r'\\\s+', r'\\s*', re.escape(alias))
                m=re.fullmatch(pat+r'\s*(?::\s*(.*))?',text,re.I)
                if m: candidates.append((s,m[1])); break
        if not candidates and aliases[0]=='Nomor Invoice':
            from label_region_invoice import invoice_region
            embedded=invoice_region(self,area)
            if embedded is not None:return embedded
        if len(candidates)!=1:
            if candidates: self.masalah.append('Label ambigu: '+aliases[0])
            return self.bukti('',[s for s,_ in candidates],'Label lengkap dalam area '+aliases[0],
                              'ambigu' if candidates else ('tidak_tercetak_dalam_OCR' if optional else 'tidak_ditemukan'))
        label,inline=candidates[0]; segments=[label]
        if inline and inline.strip(): raw=inline; method='Label lengkap dan nilai satu segmen; split pada kolon pertama'
        else:
            all_vals=[s for s in self.area(area) if s is not label and s['bbox'][0]>=label['bbox'][2] and sejajar(s,label)]
            all_vals=[s for s in self.area(area) if s is not label and (s['bbox'][0]+s['bbox'][2])/2>(label['bbox'][0]+label['bbox'][2])/2 and s['bbox'][0]>=label['bbox'][2]-self.h and sejajar(s,label)]
            segments+=all_vals; method='Label lengkap + segmen kanan sejajar, toleransi 0.45 tinggi teks'
            content_vals=[s for s in all_vals if re.search(r'\w', s['teks_asli']) and not re.fullmatch(r'(?i)(?:IDR|Rp\.?)',s['teks_asli'].strip())]
            if len(content_vals)>1:
                self.masalah.append('Pemetaan label-nilai ambigu: '+aliases[0])
                return self.bukti('',segments,method,'ambigu')
            raw=content_vals[0]['teks_asli'].strip().lstrip(':').strip() if content_vals else ''
        if kind == 'text' and raw:
            m_embed = re.search(r'(?i)\b(?:nomor\s*invoice|no\.?\s*invoice|nomorsj|nomor\s*sj)\s*:\s*(.+)$', raw)
            if m_embed and aliases[0] in ('Kepada',):
                raw = raw[:m_embed.start()].strip().rstrip(':').strip()
        return self.nilai(raw,segments,method,kind,'tidak_tercetak_dalam_OCR' if not raw else None)


def ekstrak(data):
    r=PembacaOCR(data)
    titles=[s for s in r.s if norm(s['teks_asli']) in ('faktur penjualan','sales invoice','invoice')]
    typo=judul_typo_didukung(data) if not titles else None
    if typo:titles=[typo['judul']]
    if not titles:
        from faktur_tanpa_header import ekstrak_tanpa_header,struktur_tanpa_header
        if struktur_tanpa_header(data): return ekstrak_tanpa_header(data)
    if len(titles)!=1: raise ValueError('Judul faktur tunggal tidak ditemukan; perlu review, multi-faktur belum didukung')
    headers,header_proof=pilih_header(r,titles[0])
    if header_proof['kolom_diinferensi']:r.masalah.append('Header sebagian rusak; peran kolom diinferensi dari band, label jangkar dan baris data: '+', '.join(header_proof['kolom_diinferensi']))
    if typo:r.masalah.append('Judul OCR Peniualan diterima hanya dengan dukungan label identitas dan tabel; bukan verifikasi manusia.')
    ytop=max(s['bbox'][3] for s in headers.values())
    summary=[s for s in r.s if s['bbox'][0]>r.w/2 and cy(s)>ytop+r.h and norm(s['teks_asli'].split(':')[0]) in ('sub total','subtotal')]
    if not summary:
        # Format lama tidak mencetak Sub Total pada ringkasan. Batas diambil dari
        # blok label total, tanpa mengisi subtotal dari total/hitungan.
        summary=[x for x in r.s if x['bbox'][0]>r.w/2 and cy(x)>ytop+r.h and
                 (norm(x['teks_asli'].split(':')[0]) in ('total sebelum pajak','total') or
                  re.fullmatch(r'(?i)(discount|diskon)(?:\s+final)?(?:\s+[\d.,]+\s*%?)?\s*:?',x['teks_asli']) or
                  norm(x['teks_asli'].split(':')[0])=='total sebelum diskon')]
        discount_summary=[x for x in r.s if x['bbox'][0]>r.w/2 and cy(x)>ytop+r.h and norm(x['teks_asli'].split(':')[0])=='total setelah diskon']
        if not any(norm(x['teks_asli'].split(':')[0])=='total' for x in summary) and not (any(norm(x['teks_asli'].split(':')[0])=='total sebelum diskon' for x in summary) and len(discount_summary)==1):
            raise ValueError('Ringkasan total lengkap tidak ditemukan; perlu review')
        ybottom=min(x['bbox'][1] for x in summary)
        sections=[x for x in r.s if norm(x['teks_asli'])=='keterangan' and ytop<cy(x)<ybottom and x['bbox'][0]<r.w/2]
        table_bottom=min([x['bbox'][1] for x in sections]+[ybottom])
    elif len(summary)==1:
        ybottom=summary[0]['bbox'][1]; table_bottom=ybottom
    else: raise ValueError('Batas tabel/total tidak tunggal; perlu review lanjutan')
    numeric=[s for s in r.s if cy(s)>ytop and s['bbox'][0]>r.w*.4]
    def token_number(s):return input_angka_sumber(s['teks_asli'])[0]
    locales={loc for loc,pat in [('en_US',r'\d{1,3}(?:[,\s]\d{3})+(?:\.\d{2})?|\d+(?:\.\d+)+\.\d{2}'),('id_ID',r'\d{1,3}(?:[.\s]\d{3})+,\d{2}')]
             if any(re.fullmatch(pat,re.sub(r'(\d)\s+(\d)', r'\1\2', token_number(s))) for s in numeric)}
    r.locale=next(iter(locales)) if len(locales)==1 else None
    locale_anchors={loc:[s for s in numeric if re.fullmatch(pat,token_number(s))]
                    for loc,pat in [('id_ID',r'-?\d{1,3}(?:\.\d{3}){2,}'),('en_US',r'-?\d{1,3}(?:,\d{3}){2,}')]}
    anchored=[loc for loc,ss in locale_anchors.items() if ss]
    if r.locale is None and not locales and len(anchored)==1:r.locale=anchored[0]
    if r.locale is None: r.masalah.append('Format pemisah angka dokumen tidak tunggal; angka tetap kosong.')
    il=[s for s in r.s if re.match(r'(?i)^nomor invoice\s*:',s['teks_asli']) and titles[0]['bbox'][3]<cy(s)<ytop]
    split=il[0]['bbox'][0]-r.h/2 if len(il)==1 else r.w/2
    il=[s for s in r.s if re.search(r'(?i)\b(nomor\s*invoice|nomorsj|nomor\s*sj|no\.?\s*invoice|tanggal)\b',s['teks_asli']) and titles[0]['bbox'][3]<cy(s)<ytop and s['bbox'][0]>r.w*0.4]
    split=min((s['bbox'][0] for s in il), default=r.w*0.52)-r.h/2
    top=titles[0]['bbox'][3]; bottom=min(s['bbox'][1] for s in headers.values())
    left=[0,top,split,bottom]; right=[split,top,r.w,bottom]; totals=[r.w/2,ybottom-r.h/2,r.w,r.height]
    fields={}
    for key,aliases,area,kind in [
        ('nama_pelanggan',['Kepada','Nama Perusahaan'],left,'text'),('contact_person',['Contact Person'],left,'text'),
        ('telp',['Telp','Telp.','Telepon','Telp/Fax/Email','Telp/Fax','Telp/Email'],left,'text'),('email',['Email'],left,'email'),
        ('npwp_pelanggan',['NPWP'],right,'npwp'),
        ('nomor_invoice',['Nomor Invoice','No. Invoice','No Invoice','Invoice No','Nomor Faktur','No. Faktur','No Faktur'],right,'text'),
        ('tanggal_invoice',['Tanggal'],right,'tanggal'),('subtotal_faktur',['Sub Total','Subtotal','Total Sebelum Diskon'],totals,'angka'),
        ('total_sebelum_pajak',['Total Sebelum Pajak','Total Sebelum Paiak'],totals,'angka'),('total_akhir',['Total','Total Setelah Diskon'],totals,'angka'),
        ('pajak_faktur',['Pajak','PPN'],totals,'angka'),('pph_tercetak',['PPH'],totals,'angka')]:
        fields[key]=r.label(aliases,area,kind)
    fields['total_akhir']=r.total_akhir(totals)
    # Deteksi penerbit: normalisasi spasi internal agar varian OCR (ZAHIRINTERNASIONAL vs
    # ZAHIR INTERNASIONAL) dianggap identik. Teks terpanjang dipilih sebagai nilai kanonik.
    publisher=[s for s in r.s if (s['bbox'][3]<titles[0]['bbox'][1] or s['bbox'][1]>max((cy(x) for x in r.area(totals) if norm(x['teks_asli'])=='total'), default=ybottom))
               and re.match(r'(?i)^(?:P\.?\s*T\.?|PT\.?|CV\.?|UD\.?)\s*\w',s['teks_asli'])]
    def _norm_pub(t): return re.sub(r'\s+','',t.strip()).casefold()
    kop=[s for s in publisher if s['bbox'][3]<titles[0]['bbox'][1]]
    unique_kop={_norm_pub(p['teks_asli']) for p in kop}
    unique_pub={_norm_pub(p['teks_asli']) for p in publisher}
    if kop and len(unique_kop)==1:
        best=max(kop,key=lambda p:len(p['teks_asli']))
        fields['nama_penerbit']=r.nilai(best['teks_asli'],kop,
            'Nama badan usaha eksplisit pada kop; varian spasi OCR digabung, teks terpanjang dipilih')
    elif publisher and len(unique_pub)==1:
        best=max(publisher,key=lambda p:len(p['teks_asli']))
        fields['nama_penerbit']=r.nilai(best['teks_asli'],publisher,
            'Nama badan usaha eksplisit pada kop/footer; varian spasi OCR digabung, teks terpanjang dipilih')
    else:
        fields['nama_penerbit']=r.bukti('',publisher,'Cari badan usaha kop/footer','ambigu' if publisher else 'tidak_ditemukan')
    pph=[s for s in r.area(totals) if norm(s['teks_asli'].split(':')[0])=='pph']
    fields['label_pph']=r.nilai(pph[0]['teks_asli'].split(':')[0].strip(),pph,'Label PPH lengkap') if len(pph)==1 else r.bukti(reason='tidak_ditemukan')
    discount_pattern=r'(?i)((?:discount|diskon)\s+final)\s+([\d.,]+\s*%)\s*:?'
    discounts=[s for s in r.area(totals) if re.fullmatch(discount_pattern,s['teks_asli'].strip())]
    if len(discounts)==1:
        ds=discounts[0]; m=re.fullmatch(discount_pattern,ds['teks_asli'].strip())
        fields['diskon_final_persen']=r.nilai(m[2].replace(' ',''),[ds],'Regex label Discount Final: ambil substring persen, bbox segmen utuh; spasi sebelum % diterima','angka')
        fields['diskon_final_persen']['teks_label_asli']=ds['teks_asli']
        fields['diskon_final_persen']['teks_persen_asli']=m[2]
        fields['diskon_final_persen']['aturan_pemisahan']='Substring angka dan % dari label; hanya spasi pemisah sebelum % dihapus, bbox segmen asal tetap.'
        fields['diskon_final_nominal']=r.label([ds['teks_asli'].split(':')[0].strip()],totals,'angka')
    else:
        for k in ['diskon_final_persen','diskon_final_nominal']: fields[k]=r.bukti('',discounts,'Label diskon final','ambigu' if discounts else 'tidak_ditemukan')
    if fields['diskon_final_nominal']['nilai']=='':
        nominal=r.label(['Discount Final','Diskon Final','Discount','Diskon','Diskon akhir'],totals,'angka',optional=True)
        if nominal['segmen']: fields['diskon_final_nominal']=nominal
    # Angka pada label tanpa unit tidak menjadi persen; nominal ada di sel kanan.
    unitless=[s for s in r.area(totals) if re.fullmatch(r'(?i)(?:discount|diskon)\s+final\s+([\d.,]+)\s*:?\s*',s['teks_asli'].strip())]
    if not discounts and len(unitless)==1:
        ds=unitless[0]
        fields['diskon_final_nominal']=r.label([ds['teks_asli'].split(':')[0].strip()],totals,'angka')
        fields['diskon_final_persen']=r.bukti(segments=[ds],method='Angka pada label final tanpa unit: simpan teks, jangan inferensi persen',reason='satuan_persen_tidak_dinyatakan')
        fields['diskon_final_persen']['angka_label_tanpa_satuan']=re.fullmatch(r'(?i)(?:discount|diskon)\s+final\s+([\d.,]+)\s*:?\s*',ds['teks_asli'].strip())[1]
        fields['diskon_final_persen']['status_satuan']='belum_dipastikan'
    if any(re.fullmatch(r'(?i)total\s*sebelum\s*paiak\s*:?',s['teks_asli'].strip()) for s in fields['total_sebelum_pajak']['segmen']):
        fields['total_sebelum_pajak']['aturan_label']='Varian OCR i/j hanya pada frasa lengkap Total Sebelum Pajak dalam area ringkasan; teks dan bbox asal tetap.'
    for k,alias in [('dpp_tercetak','DPP'),('dpp_lain_tercetak','DPP lain'),('total_setelah_pajak','Total Setelah Pajak'),('ppn_persen_tercetak','PPN %'),('catatan_termin_tercetak','Catatan Termin')]:
        fields[k]=r.label([alias,'Total Setelah PPN'] if k=='total_setelah_pajak' else [alias],totals,'text' if k=='catatan_termin_tercetak' else 'angka',optional=True)
    # PPN/PPh yang eksplisit pada ringkasan, termasuk format tanpa Grand Total.
    ppns=[x for x in r.area(totals) if re.fullmatch(r'(?i)PPN\s+([\d.,]+)\s*%\s*:?',x['teks_asli'].strip())]
    if len(ppns)==1:
        pp=ppns[0];pct=re.fullmatch(r'(?i)PPN\s+([\d.,]+)\s*%\s*:?',pp['teks_asli'].strip())[1]
        fields['ppn_persen_tercetak']=r.nilai(pct,[pp],'Substring persen label PPN; bbox segmen penuh','angka')
        if not fields['pajak_faktur']['segmen']:fields['pajak_faktur']=r.label([pp['teks_asli'].strip().rstrip(':')],totals,'angka')
    pphs=[x for x in r.area(totals) if re.fullmatch(r'(?i)PPH(?:\s*(?:Ps\.?\s*)?23)?(?:\s*\([\d.,]+\s*%\))?\s*:?',x['teks_asli'].strip())]
    if len(pphs)==1:
        label=pphs[0]['teks_asli'].strip().rstrip(':').strip()
        fields['pph_tercetak']=r.label([label],totals,'angka')
        fields['label_pph']=r.nilai(label,[pphs[0]],'Label PPh lengkap pada ringkasan; pasal/persen tetap teks')
    grand=any(norm(x['teks_asli'])=='grand total' for x in r.area(totals))
    if grand:
        for key,aliases in [('total_akhir',['Grand Total']),('pph_tercetak',['PPH 23']),('dpp_tercetak',['DPP']),('dpp_lain_tercetak',['DPP Lain (DPP x 11/12)']),('total_setelah_pajak',['Total Setelah Pajak']),('diskon_final_nominal',['Diskon Final'])]:
            fields[key]=r.total_akhir(totals) if key=='total_akhir' else r.label(aliases,totals,'angka')
        pph=[x for x in r.area(totals) if re.fullmatch(r'(?i)PPH\s*23\s*:?',x['teks_asli'].strip())]
        fields['label_pph']=r.nilai(pph[0]['teks_asli'],pph,'Label PPH dan nomor pasal tercetak') if len(pph)==1 else r.bukti(reason='tidak_ditemukan')
        ppns=[x for x in r.area(totals) if re.fullmatch(r'(?i)PPN\s+([\d.,]+)%\s*:?',x['teks_asli'])]
        if len(ppns)==1:
            pp=ppns[0]; pct=re.fullmatch(r'(?i)PPN\s+([\d.,]+)%\s*:?',pp['teks_asli'])[1]
            fields['ppn_persen_tercetak']=r.nilai(pct,[pp],'Ambil substring persen label PPN; bbox tetap segmen utuh','angka')
            fields['pajak_faktur']=r.label([pp['teks_asli'].rstrip(':')],totals,'angka')
        term=[x for x in r.area(totals) if norm(x['teks_asli'])=='pembayaran termin']
        if len(term)==1:
            ts=[x for x in r.area(totals) if x['bbox'][0]>=term[0]['bbox'][0]-r.h and cy(x)>=cy(term[0]) and (x is term[0] or re.match(r'(?i)^-?\s*Termin\b|^Rp\b',x['teks_asli']))]
            fields['catatan_termin_tercetak']=r.nilai('\n'.join(x['teks_asli'] for x in ts),ts,'Blok Pembayaran Termin, baris Termin/Rp; teks sumber, bukan transaksi/pembayaran terverifikasi')
        # Field yang tidak dicetak tidak disalin dari label lain atau dari hitungan.
        fields['total_sebelum_pajak']=r.bukti(reason='tidak_tercetak_dalam_OCR',method='Label Total Sebelum Pajak tidak ditemukan')
        fields['diskon_final_persen']=r.bukti(reason='tidak_tercetak_dalam_OCR',method='Diskon Final tidak mencetak persen')
    ordered=sorted(headers,key=lambda k:headers[k]['bbox'][0])
    bounds=[0]+[(headers[a]['bbox'][2]+headers[b]['bbox'][0])/2 for a,b in zip(ordered,ordered[1:])]+[r.w]
    if header_proof.get('batas_kolom_grid'):
        bounds=header_proof['batas_kolom_grid']
    r.locale_kolom={}
    table_anchors={loc:[s for s in r.s if ytop<cy(s)<table_bottom and s['bbox'][0]>r.w*.4 and
                       re.fullmatch(pat,token_number(s)) and s.get('skor_keyakinan',0)>=.85]
                   for loc,pat in [('id_ID',r'-?\d{1,3}(?:\.\d{3}){2,}'),('en_US',r'-?\d{1,3}(?:,\d{3}){2,}') ]}
    table_modes=[loc for loc,ss in table_anchors.items() if ss]
    for n,key in enumerate(ordered):
        if key not in ('harga_satuan','subtotal_item'):continue
        evidence={loc:[s for s in r.s if ytop<cy(s)<table_bottom and bounds[n]<=(s['bbox'][0]+s['bbox'][2])/2<bounds[n+1]
                       and re.fullmatch(pat,token_number(s))]
                  for loc,pat in [('id_ID',r'-?\d{1,3}(?:\.\d{3}){2,}'),('en_US',r'-?\d{1,3}(?:,\d{3}){2,}') ]}
        modes=[loc for loc,ss in evidence.items() if ss]
        if len(modes)==1:
            loc=modes[0];r.locale_kolom[key]={'locale':loc,'kolom':key,
                'aturan':'Anchor integer dengan dua atau lebih grup ribuan tiga digit pada kolom geometris yang sama; tidak memakai hitungan.',
                'bukti':r.bukti(segments=evidence[loc],method='Bukti locale per kolom')}
        elif not modes and len(table_modes)==1:
            loc=table_modes[0];r.locale_kolom[key]={'locale':loc,'kolom':key,
                'aturan':'Anchor integer dua atau lebih grup tiga digit pada area tabel yang sama, tanpa anchor lawan; konvensi tabel terpisah dari ringkasan. Tidak menyalin nilai dari kolom lain atau hitungan.',
                'bukti':r.bukti(segments=table_anchors[loc],method='Bukti format area tabel dari pemisah yang eksplisit')}
        if key not in r.locale_kolom and not modes and not table_modes:
            from konteks_format_uang import locale_kolom_integer
            local=[s for s in r.s if ytop<cy(s)<table_bottom and bounds[n]<=(s['bbox'][0]+s['bbox'][2])/2<bounds[n+1]]
            context=locale_kolom_integer(r.s,r.w,local,key)
            if context:r.locale_kolom[key]=context
    groups=[]
    for s in [s for s in r.s if ytop<cy(s)<table_bottom-r.h/2]:
        group=next((g for g in groups if abs(cy(s)-statistics.median(cy(v) for v in g))<=.60*statistics.median([ht(s)]+[ht(v) for v in g])),None)
        if group is None: groups.append([s])
        else: group.append(s)
    items=[]; notes=[]; unassigned=[]
    for group in groups:
        cells={k:[s for s in group if bounds[j]<=(s['bbox'][0]+s['bbox'][2])/2<bounds[j+1]] for j,k in enumerate(ordered)}
        names=cells['nama_barang']; name=' '.join(s['teks_asli'] for s in names)
        metadata=bool(re.match(r'(?i)^\s*(no\.?\s*seri|serial|special code|free training|expired|tanggal expired)\b',name))
        structural=bool(names) and sum(bool(cells[k]) for k in ('jumlah','harga_satuan','subtotal_item'))>=2
        if structural and not metadata:
            cols={}; problems=[]
            for j,k in enumerate(ordered):
                ss=cells[k]
                ambiguous=len(ss)>1 or any(s['bbox'][0]<bounds[j]-.75*ht(s) or s['bbox'][2]>bounds[j+1]+.75*ht(s) for s in ss)
                if ambiguous:
                    cols[k]=r.bukti('',ss,'Pemetaan kolom dari judul dan keselarasan y','pemetaan_kolom_ambigu'); problems.append(k+': pemetaan kolom ambigu')
                if k == 'nama_barang':
                    next_col_left = min((z['bbox'][0] for z in cells.get('jumlah', [])), default=headers.get('jumlah', {}).get('bbox', [0, 0, bounds[j+1]])[0])
                    max_x = max(bounds[j+1] + .75*ht(ss[0]) if ss else bounds[j+1], next_col_left + 5)
                    ambiguous = any(s['bbox'][0] < bounds[j] - .75*ht(s) or s['bbox'][2] > max_x for s in ss)
                    if ambiguous:
                        cols[k] = r.bukti('', ss, 'Pemetaan kolom dari judul dan keselarasan y', 'pemetaan_kolom_ambigu')
                        problems.append(k + ': pemetaan kolom ambigu')
                    else:
                        text = ' '.join(s['teks_asli'] for s in sorted(ss, key=lambda s: (s['bbox'][1], s['bbox'][0]))).strip()
                        cols[k] = r.nilai(text, ss, 'Sel tabel ' + k, 'text')
                else:
                    cols[k]=r.nilai(ss[0]['teks_asli'] if ss else '',ss,'Sel tabel '+k,'text' if k in ('nama_barang','unit') else 'angka')
                    ambiguous = len(ss) > 1 or any(s['bbox'][0] < bounds[j] - .75*ht(s) or s['bbox'][2] > bounds[j+1] + .75*ht(s) for s in ss)
                    if ambiguous:
                        cols[k] = r.bukti('', ss, 'Pemetaan kolom dari judul dan keselarasan y', 'pemetaan_kolom_ambigu')
                        problems.append(k + ': pemetaan kolom ambigu')
                    else:
                        cols[k] = r.nilai(ss[0]['teks_asli'] if ss else '', ss, 'Sel tabel ' + k, 'text' if k in ('nama_barang', 'unit') else 'angka')
                cols[k]['judul_kolom']=r.bukti(segments=[headers[k]],method='Judul kolom asal')
                if k=='pajak_item_tercetak' and cols[k].get('teks_diambil','').strip().endswith('%'):
                    cols[k]['satuan_tercetak']='persen'
                    cols[k]['bukan_nominal_rupiah']=True
                    cols[k]['catatan_satuan']='Digit persen tercetak dipertahankan; tidak dinyatakan sebagai nominal Rp.'
                if cols[k]['nilai']=='': problems.append(k+': '+str(cols[k]['alasan_kosong']))
                if any(s in r.low for s in ss): problems.append(k+': skor OCR rendah/tidak tersedia')
            if 'pajak_item_tercetak' not in cols: cols['pajak_item_tercetak']=r.bukti(reason='tidak_tercetak_dalam_OCR',method='Tabel tanpa judul kolom pajak per item')
            items.append({'kolom':cols,'nilai':{k:v['nilai'] for k,v in cols.items()},'y':statistics.median(cy(s) for s in group),'masalah':problems})
        else:
            prior=[(i,item) for i,item in enumerate(items,1) if 0<cy(group[0])-item['y']<3*r.h]
            candidate=prior[-1][0] if prior else None
            note=r.bukti(' '.join(s['teks_asli'] for s in group),group,'Keterangan/non-item: struktur angka tidak membentuk baris item')
            note.update(status_hubungan='belum_dipastikan',nomor_baris_item_terkait=None,kandidat_item=candidate,
                        alasan='Kedekatan posisi hanya kandidat; cakupan item/paket belum dipastikan.')
            note['risiko_item_hilang']=not metadata and any(cells[k] for k in ('jumlah','harga_satuan','subtotal_item'))
            notes.append(note)
            if not metadata and not names: unassigned.append(note)
    # Inferensi satuan kolom, bukan mengisi nilai harga yang hilang.
    supports=[]; contradictions=[]; hypotheses=[]
    for i,it in enumerate(items,1):
        row=it['nilai']; q,p,d,s=(dec(row,k) for k in ['jumlah','harga_satuan','diskon_item_persen','subtotal_item'])
        if None not in (q,p,d,s):
            pct=0<=d<=100 and abs(q*p*(1-d/100)-s)<=TOLERANSI
            nominal=abs(q*p-d-s)<=TOLERANSI
            hypotheses.append({'nomor_baris_item':i,'persen':cek_hitung('hipotesis persen',s,q*p*(1-d/100)), 'nominal':cek_hitung('hipotesis nominal',s,q*p-d)})
            if pct and not nominal: supports.append(i)
            elif not pct: contradictions.append(i)
    percent_explicit='%' in headers['diskon_item_persen']['teks_asli'] or bool(items) and all(it['kolom']['diskon_item_persen'].get('teks_diambil','').strip().endswith('%') for it in items)
    percent_inferred=bool(supports) and not contradictions
    checks=[]
    for i,it in enumerate(items,1):
        row=it['nilai']; cols=it['kolom']; dv=dec(row,'diskon_item_persen')
        cols['diskon_item_persen']['interpretasi_satuan']={'eksplisit':percent_explicit,'inferensi_kolom_persen':percent_inferred,'baris_pendukung':supports,'baris_bertentangan':contradictions,'bukti_hipotesis':hypotheses,'status':'belum_diverifikasi_manusia'}
        if not percent_explicit:
            dv_raw=dec(row,'diskon_item_persen')
            if percent_inferred:
                it['masalah'].append('Disc. tanpa %: satuan persen diinferensi dari baris lengkap pada kolom yang sama; perlu review.')
            elif dv_raw is not None and 0<=dv_raw<=100:
                # Pertahankan digit dalam kolom skema, tanpa menganggap rentang
                # 0..100 sebagai konfirmasi unit persen pada sumber.
                cols['diskon_item_persen']['interpretasi_catatan']=(
                    'Digit diskon tanpa simbol % dipertahankan; satuan persen belum dinyatakan eksplisit. Rentang/hipotesis hitungan bukan konfirmasi manusia.'
                )
                cols['diskon_item_persen']['status_satuan']='belum_dipastikan'
                if dv_raw==0:
                    it['masalah'].append('Disc. tanpa %: nilai nol, satuan tidak menentukan subtotal; tetap perlu review jika satuan diperlukan.')
            else:
                row['diskon_item_persen']=''; cols['diskon_item_persen'].update(nilai='',alasan_kosong='satuan_diskon_ambigu')
                it['masalah'].append('Satuan diskon belum dipastikan.')
        if dv is not None and not 0<=dv<=100:
            row['diskon_item_persen']=''; cols['diskon_item_persen'].update(nilai='',alasan_kosong='diskon_di_luar_rentang'); it['masalah'].append('Diskon di luar 0..100.')
        q,p,d,s=(dec(row,k) for k in ['jumlah','harga_satuan','diskon_item_persen','subtotal_item'])
        missing=[k for k in ['jumlah','harga_satuan','diskon_item_persen','subtotal_item'] if row[k]=='']
        check=cek_hitung('subtotal_item',s,q*p*(1-d/100) if None not in (q,p,d) else None,nomor_baris_item=i,komponen_kosong=missing)
        checks.append(check); it['validasi']=[check]
        if check['status']!='lolos_otomatis': it['masalah'].append('Subtotal '+check['status']+'; komponen kosong: '+', '.join(missing))
    if not fields['subtotal_faktur']['nilai']:
        original_subtotal = fields['subtotal_faktur']
        subs_val = [dec(it['nilai'], 'subtotal_item') for it in items]
        if subs_val and all(v is not None for v in subs_val) and sum(subs_val, Decimal(0)) > 0:
            sub_sum = format(sum(subs_val, Decimal(0)), 'f')
            fields['subtotal_faktur'] = r.bukti(sub_sum, [], 'Fallback global subtotal_faktur dari akumulasi subtotal_item')
        elif (fields.get('total_sebelum_pajak') and fields['total_sebelum_pajak']['nilai']
              and fields.get('diskon_final_nominal',{}).get('nilai') not in (None,'')
              and Decimal(fields['diskon_final_nominal']['nilai'])==0
              and (not items or all(v is not None for v in subs_val))):
            tsp = fields['total_sebelum_pajak']
            fields['subtotal_faktur'] = {**tsp, 'metode': 'Fallback global subtotal_faktur dari total_sebelum_pajak'}
        elif subs_val and all(v is not None for v in subs_val):
            sub_sum = format(sum(subs_val, Decimal(0)), 'f')
            fields['subtotal_faktur'] = r.bukti(sub_sum, [], 'Fallback global subtotal_faktur dari akumulasi subtotal_item')
        if fields['subtotal_faktur'] is not original_subtotal:
            fields['subtotal_faktur']['bukti_label_awal'] = original_subtotal
            fields['subtotal_faktur']['catatan_asal'] = 'Nilai turunan; bukan pembacaan subtotal tercetak. Bukti pencarian label awal dipertahankan.'
        elif items and any(v is None for v in subs_val):
            fields['subtotal_faktur']['alasan_kosong']='komponen_subtotal_item_tidak_lengkap'
            fields['subtotal_faktur']['komponen_turunan']=[{'nomor_item':n,'nilai':it['nilai']['subtotal_item'],'bukti':it['kolom']['subtotal_item']} for n,it in enumerate(items,1)]
            fields['subtotal_faktur']['catatan_asal']='Subtotal tidak dapat diturunkan dari item yang sebagian kosong; tidak diganti dengan total sebelum pajak.'
    if not fields['nomor_invoice']['nilai']:
        sj = r.label(['Nomor SJ', 'No. SJ', 'No SJ', 'Nomor S.J', 'No. Surat Jalan', 'Nomor Surat Jalan'], right, 'text')
        if sj['nilai']:
            fields['nomor_invoice'] = {**sj, 'metode': 'Fallback nomor_invoice dari label Nomor SJ'}
        else:
            for seg in fields.get('nama_pelanggan', {}).get('segmen', []):
                m_inv = re.search(r'(?i)\b(?:nomor\s*invoice|no\.?\s*invoice|nomorsj|nomor\s*sj)\s*:\s*(.+)$', seg['teks_asli'])
                if m_inv:
                    inv_val = m_inv.group(1).strip()
                    original=[s for s in r.s if s['id_segmen']==seg['id_segmen'] and s['bbox']==seg['bbox_asal'] and s['teks_asli']==seg['teks_asli']]
                    if len(original)!=1:raise ValueError('Segmen asal identitas gabungan tidak tunggal')
                    fields['nomor_invoice'] = r.nilai(inv_val, original, 'Fallback nomor invoice dari segmen gabungan OCR; substring setelah label lengkap')
                    fields['nomor_invoice']['aturan_pemisahan']='Substring sesudah label nomor invoice/SJ; bbox tetap segmen asli, bukan bbox substring.'
                    break
    if not fields['tanggal_invoice']['nilai']:
        for s in r.s:
            if re.search(r'(?i)\bTanggal\s*:?$', s['teks_asli']):
                vals = [v for v in r.area(right) if abs(cy(v) - cy(s)) <= .55 * ht(s) and v['bbox'][0] >= s['bbox'][2] - r.h]
                if vals:
                    try:
                        tgl_str = ' '.join(v['teks_asli'] for v in vals).lstrip(':').strip()
                        iso = tanggal(tgl_str)
                        fields['tanggal_invoice'] = r.nilai(iso, vals, 'Fallback tanggal dari segmen label Tanggal di akhir teks')
                        break
                    except ValueError:
                        pass
        if not fields['tanggal_invoice']['nilai']:
            for s in r.area(right):
                try:
                    iso = tanggal(s['teks_asli'].lstrip(':').strip())
                    fields['tanggal_invoice'] = r.nilai(iso, [s], 'Fallback deteksi pola tanggal di area header kanan')
                    break
                except ValueError:
                    pass
    if fields.get('telp') and fields['telp']['nilai']:
        clean_telp = re.sub(r'(?i)\s*Tanggal\b.*$', '', fields['telp']['nilai']).strip()
        if clean_telp != fields['telp']['nilai']:
            fields['telp']['nilai'] = clean_telp
    row={k:v['nilai'] for k,v in fields.items()}
    def calc(name,actual,keys,fn):
        missing=[k for k in keys if row.get(k,'')=='']; values=[dec(row,k) for k in keys]
        checks.append(cek_hitung(name,dec(row,actual),fn(*values) if not missing else None,komponen_kosong=missing+([actual] if row.get(actual,'')=='' else [])))
    subs=[dec(it['nilai'],'subtotal_item') for it in items]
    checks.append(cek_hitung('jumlah subtotal item = subtotal faktur',dec(row,'subtotal_faktur'),sum(subs,Decimal(0)) if subs and all(v is not None for v in subs) else None))
    calc('subtotal ke total sebelum pajak (PPH nol)', 'total_sebelum_pajak',['subtotal_faktur','pph_tercetak'],lambda s,p:s if p==0 else None)
    calc('diskon final nominal', 'diskon_final_nominal',['total_sebelum_pajak','diskon_final_persen'],lambda t,d:t*d/100 if 0<=d<=100 else None)
    calc('total akhir','total_akhir',['total_sebelum_pajak','diskon_final_nominal','pajak_faktur'],lambda t,d,p:t-d+p)
    required=['nomor_invoice','tanggal_invoice','nama_pelanggan']
    checks.append({'pemeriksaan':'kelengkapan identitas dan item','status':'lolos_otomatis' if all(row.get(k) for k in required) and items else 'tidak_dapat_diperiksa','kolom_kosong':[k for k in required if not row.get(k)],'jumlah_item_aktual':len(items),'catatan':'Jumlah berdasarkan struktur; tidak dibandingkan dengan angka acuan dalam parser.'})
    identity=[data['sha256_sumber_sebelum'],row.get('nomor_invoice'),row.get('tanggal_invoice'),row.get('nama_penerbit')]
    if not row.get('nomor_invoice'): identity.append(titles[0]['bbox'])
    fid='FKT_OCR_'+hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()[:24]
    if notes: r.masalah.append('Hubungan '+str(len(notes))+' keterangan tabel belum_dipastikan; kandidat posisi bukan hubungan terverifikasi.')
    r.masalah.extend('Item '+str(i)+': '+p for i,it in enumerate(items,1) for p in it['masalah'])
    r.masalah.extend(c['pemeriksaan']+': '+c['status'] for c in checks if c['status']!='lolos_otomatis')
    for k in ['nama_penerbit']+required:
        if not row.get(k): r.masalah.append(k+': belum berhasil diekstrak')
    metadata={'id_faktur':fid,'folder_klien':data['inventaris']['folder_klien'],'file_sumber':data['sumber_gambar'],'halaman_sumber':'1',
              'status_validasi':'perlu_review','catatan_validasi':'; '.join(dict.fromkeys(r.masalah)) or 'Belum diverifikasi manusia.'}
    for k,v in metadata.items(): fields[k]=r.bukti(v,[],'Metadata input atau turunan validasi; identitas hash sumber + invoice/tanggal/penerbit'); fields[k]['dasar_metadata']=identity if k=='id_faktur' else k
    row={k:fields[k]['nilai'] for k in FAKTUR}
    for i,it in enumerate(items,1):
        for k,v in {'id_faktur':fid,'nomor_baris_item':str(i),'halaman_sumber':'1','status_validasi':'perlu_review','catatan_validasi':'; '.join(it['masalah']) or 'Belum diverifikasi manusia.'}.items():
            it['kolom'][k]=r.bukti(v,[],'Turunan urutan geometris/relasi faktur/validasi'); it['kolom'][k]['dasar_metadata']={'id_faktur':fid,'y_baris':it['y']}
        it['nilai']={k:it['kolom'][k]['nilai'] for k in DETAIL}
    checks.append({'pemeriksaan':'relasi ID dan nomor item unik','status':'lolos_otomatis' if all(it['nilai']['id_faktur']==fid for it in items) and len({it['nilai']['nomor_baris_item'] for it in items})==len(items) else 'selisih'})
    result={'nilai':row,'kolom':fields,'item':items,'keterangan_tabel':notes,'validasi':checks,
            'review_skor':r.bukti(segments=r.low,method='Ambang review skor < 0.85; bukan verifikasi manusia'),
            'struktur':{'area_pelanggan':left,'area_invoice_npwp':right,'area_total':totals,'batas_kolom_piksel':bounds,'format_angka':r.locale,'bukti_locale_anchor':r.bukti(segments=locale_anchors.get(r.locale,[]),method='Anchor locale global bila locale awal tidak diketahui'),'locale_kolom':r.locale_kolom,'toleransi_y':'0.45 * tinggi teks','non_item_tanpa_kolom_nama':unassigned}}
    result['struktur']['bukti_header']=header_proof
    from cakupan_dokumen import terapkan_cakupan
    terapkan_cakupan(result,r.s,r.w,r.height)
    if header_proof.get('versi','').startswith('header-grid-v'):
        result['status_record']='sementara_pemulihan_header_belum_keputusan_transaksi'
    if header_proof['kolom_diinferensi']:
        result['catatan_layout']='Sebagian header OCR tidak terbaca; band/posisi dan baris data mendukung pemetaan, tetap inferensi struktur.'
        result['bukti_struktur']=header_proof
    if typo:result['bukti_judul_typo']=typo
    for it in items:
        if '_tax_persen' in it['kolom']:
            it['tax_persen_tercetak']=it['kolom']['_tax_persen']
            it['kolom']['pajak_item_tercetak']['catatan_asal']='Header Tax menyatakan persen, bukan nominal rupiah; tidak menyalin persen ke nominal.'
    recap=kenali_rekap_diskon_ppn_pph(result)
    if recap:
        ev=fields['diskon_final_nominal']
        if ev['nilai']:
            signed=ev['nilai'];magnitude=format(abs(Decimal(signed)),'f')
            ev['nilai']=row['diskon_final_nominal']=magnitude
            ev['nominal_pengurangan_bertanda']=signed
            ev['konvensi_diskon']='magnitude_pengurangan_kurung'
            ev['analisis_normalisasi_bertanda']=ev.get('analisis_normalisasi')
            ev['analisis_normalisasi']={**ev['analisis_normalisasi'],'nilai':magnitude,'aturan':'Magnitude nominal potongan; tanda kurung negatif tersimpan terpisah, sesuai format label/posisi.'}
        result['bukti_format_total']=recap
        result['format_total']=recap['format_total']
    nominal_format = kenali_ringkasan_nominal(result)
    tax_format=kenali_setelah_pajak(result);discount_format=kenali_setelah_diskon(result)
    if grand or nominal_format or tax_format or discount_format or recap:
        from batch_common import validate_invoice
        result['validasi']=validate_invoice(result)
        known=recap or tax_format or discount_format or nominal_format
        result['format_total']=known['format_total'] if known else 'DPP, DPP Lain (DPP x 11/12), PPN persen, Total Setelah Pajak, PPH bertanda, Grand Total'
        if known:result['bukti_format_total']=known
        if nominal_format:
            result['bukti_format_total']=nominal_format
        # Buang hanya pesan validasi format lama; nilai dan kesalahan parsing tetap ada.
        notes=[m for m in r.masalah if not any(m.startswith(prefix) for prefix in ['subtotal ke total sebelum pajak','diskon final nominal','total akhir:'])]
        notes += [c['pemeriksaan']+': '+c['status']+'; selisih '+str(c.get('selisih')) for c in result['validasi'] if c['status'] not in ('lolos_otomatis','tidak_berlaku')]
        row['catatan_validasi']='; '.join(dict.fromkeys(notes)); fields['catatan_validasi']['nilai']=row['catatan_validasi']
    if not row['nomor_invoice']:
        result['identitas_lain']={'nomor_sj':r.label(['Nomor SJ'],right)}
    elif fields['nomor_invoice'].get('metode', '').startswith('Fallback nomor_invoice dari label Nomor SJ'):
        result['identitas_lain']={'nomor_sj':fields['nomor_invoice']}
        result['status_identitas_invoice']=identitas_invoice_fallback(result)
    so=r.label(['Nomor SO'],right,optional=True)
    if so['nilai']:
        result.setdefault('identitas_lain',{})['nomor_so']=so
        so['catatan_semantik']='Label sumber Nomor SO dipertahankan; bukan otomatis nomor_invoice atau kategori Sales Order.'
    # Komisi merupakan informasi berlabel, bukan sinonim Discount Final.
    # Simpan bukti terpisah tanpa menambah/mengisi kolom CSV diskon.
    extra=[]
    for s in r.area(totals):
        m=re.fullmatch(r'(?i)komisi\s+partner\s+([\d.,]+)\s*%\s*:?',s['teks_asli'].strip())
        if not m:continue
        vv=[v for v in r.area(totals) if v is not s and v['bbox'][0]>=s['bbox'][2] and sejajar(s,v)
            and re.fullmatch(r'-?[\d.,]+',v['teks_asli'].strip())]
        pct=r.nilai(m[1],[s],'Persen pada label Komisi Partner; bbox seluruh region, bukan bbox substring','angka')
        pct['satuan_tercetak']='persen';pct['aturan_pemisahan_teks']='Ambil grup numerik sebelum simbol % pada label lengkap Komisi Partner.'
        nominal=r.nilai(vv[0]['teks_asli'],[s]+vv,'Nominal kanan sejajar label Komisi Partner','angka') if len(vv)==1 else r.bukti('',[s]+vv,'Nominal Komisi Partner tidak tunggal','ambigu')
        extra.append({'label_asli':s['teks_asli'],'persen':pct,'nominal':nominal,'bukan_discount_final':True})
    if extra:result['informasi_ringkasan_tambahan']=extra
    return result


def evaluasi(inv,reference):
    result=[]
    for scope,actual,expected in [('faktur',inv['nilai'],reference['faktur'])]+[(f'item {i}',inv['item'][i-1]['nilai'] if i<=len(inv['item']) else {},v) for i,v in enumerate(reference['item'],1)]:
        for k,v in expected.items():
            a=actual.get(k,''); same=a==v
            if a!='' and v!='':
                try: same=Decimal(a)==Decimal(v)
                except Exception: pass
            result.append({'cakupan':scope,'kolom':k,'aktual':a,'acuan':v,'status':'cocok' if same else ('belum_berhasil_diekstrak' if a=='' else 'berbeda')})
    result.append({'cakupan':'dokumen','kolom':'jumlah_item','aktual':len(inv['item']),'acuan':len(reference['item']),'status':'cocok' if len(inv['item'])==len(reference['item']) else 'berbeda'})
    return result


def jalankan(input_path,output_dir,acuan=None):
    source=input_path.resolve(); before=hashlib.sha256(source.read_bytes()).hexdigest()
    data=json.loads(source.read_text(encoding='utf-8')); inv=ekstrak(data)
    # Acuan eksternal baru dibaca setelah parser selesai; evaluasi_terpisah input diabaikan.
    evaluated=evaluasi(inv,json.loads(acuan.read_text(encoding='utf-8-sig'))) if acuan else []
    output_dir=output_dir.resolve()
    if output_dir.is_relative_to(source.parent) or output_dir.is_relative_to(Path(r'C:\Data Klient Zahir')): raise ValueError('Keluaran tidak boleh di area sumber')
    run=output_dir/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid4().hex[:8]); run.mkdir(parents=True,exist_ok=False)
    tulis_csv(run/'faktur.csv',[inv['nilai']],FAKTUR); tulis_csv(run/'detail_faktur.csv',[i['nilai'] for i in inv['item']],DETAIL)
    evidence={'versi_skema':1,'input_json':str(source),'sha256_json_sebelum':before,'sha256_json_sesudah':hashlib.sha256(source.read_bytes()).hexdigest(),'sha256_gambar_dari_input':data['sha256_sumber_sebelum'],'hasil':inv,'evaluasi_terpisah':evaluated,'status_manusia':'belum_diverifikasi','aturan':'Segmen baris piksel, bukan kata PDF. Bbox bukti tetap segmen penuh. Kosong bukan nol. ID belum menyelesaikan deduplikasi lintas salinan.'}
    assert evidence['sha256_json_sesudah']==before
    p=run/'bukti_ekstraksi.json'; p.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); assert json.loads(p.read_text(encoding='utf-8'))==evidence
    lines=['# Laporan ekstraksi OCR','','Hasil aktual: 1 faktur, '+str(len(inv['item']))+' item. Belum diverifikasi manusia.','',
           '## Hasil aktual','', '| Kolom | Nilai |','| --- | --- |']
    lines += ['| '+k+' | '+str(v).replace('|','\\|')+' |' for k,v in inv['nilai'].items()]
    for i,it in enumerate(inv['item'],1):
        lines+=['',f'### Item {i}','']+[f'- {k}: {v if v!="" else "(kosong)"}' for k,v in it['nilai'].items()]
    lines+=['','## Validasi','', 'Toleransi absolut 0.01; selisih = aktual - harapan. Komponen kosong tidak dianggap nol.']
    lines += [f'- {c["pemeriksaan"]} (item {c.get("nomor_baris_item","-")}): {c["status"]}; selisih {c.get("selisih")}; komponen kosong {c.get("komponen_kosong",[])}.' for c in inv['validasi']]
    lines+=['','## Keterangan dan nilai kosong','']
    lines += [f'- {n["nilai"]}: {n["status_hubungan"]}; kandidat posisi {n["kandidat_item"]}; item terverifikasi tidak ditetapkan. {n["alasan"]}' for n in inv['keterangan_tabel']]
    for scope,cols in [('faktur',inv['kolom'])]+[(f'item {i}',it['kolom']) for i,it in enumerate(inv['item'],1)]:
        lines += [f'- {scope}.{k} kosong: {v["alasan_kosong"]}; teks diambil {v.get("teks_diambil","")!r}; {v.get("error_parsing") or ""}' for k,v in cols.items() if v['nilai']=='']
    lines+=['','## Evaluasi terpisah setelah ekstraksi','', '| Cakupan | Kolom | Aktual | Acuan pengguna | Hasil |','| --- | --- | --- | --- | --- |']
    lines += ['| '+' | '.join(str(e[k]).replace('|','\\|') for k in ['cakupan','kolom','aktual','acuan','status'])+' |' for e in evaluated]
    lines+=['','## Metode dan batasan','','Label lengkap dan area geometris memisahkan penerbit, pelanggan, tabel dan total. Nilai satu segmen dipisah secara teks saja; bbox asal tidak dipotong. Baris item dikelompokkan memakai tinggi teks dan judul kolom, bukan ID segmen. Nama badan usaha dapat berasal dari kop/footer; logo tidak otomatis menjadi nama penerbit.',
            'Angka diparsing Decimal dengan format dokumen eksplisit dan validasi penuh. Format cacat tetap kosong, tidak dihitung balik. Disc. tanpa % diinferensi dari hipotesis persen pada baris lengkap kolom yang sama dan tetap perlu review, bukan verifikasi independen.',
            'Kosong berarti tidak ditemukan dalam OCR atau ambigu, bukan kepastian bahwa gambar tidak mencetak nilai. Keterangan hanya memiliki kandidat posisi. Skor tinggi bukan jaminan. Tata letak lain, sel terpecah/bergabung ambigu, dan beberapa halaman/faktur belum didukung otomatis. Tidak ada OCR ulang, perubahan input, pemrosesan Maria, atau estimasi akurasi seluruh arsip.']
    (run/'laporan_ekstraksi_ocr.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(str(run)); print(json.dumps(inv['nilai'],ensure_ascii=True)); print('Item:',len(inv['item']))
    return run


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,required=True); parser.add_argument('--output-dir',type=Path,default=Path('outputs/uji_ekstraksi_ocr')); parser.add_argument('--acuan',type=Path)
    args=parser.parse_args(); jalankan(args.input,args.output_dir,args.acuan)
