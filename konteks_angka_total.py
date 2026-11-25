"""Locale sel Total dari skala terbilang dekatnya, tanpa nominal acuan/rumus."""
import re
from decimal import Decimal

KATA = set(('nol kosong satu dua tiga empat lima enam tujuh delapan sembilan '
            'sepuluh sebelas belas puluh ratus seratus ribu seribu juta sejuta '
            'miliar milyar semiliar triliun rupiah minus').split())
SKALA = {'ribu': 1000, 'seribu': 1000, 'juta': 1000000, 'sejuta': 1000000,
         'miliar': 1000000000, 'milyar': 1000000000, 'semiliar': 1000000000,
         'triliun': 1000000000000}
VERSI = 'total-terbilang-v3'


def kata_bilangan(teks):
    """Pisahkan gabungan kata OCR hanya bila segmentasi kosakata tunggal.

    Tidak mengganti huruf, membuang tanda baca, menambah digit atau mengubah
    region sumber. Tata bahasa seluruh kalimat diperiksa sesudahnya.
    """
    result=[];changes=[]
    for token in teks.casefold().strip().split():
        if token in KATA:
            result.append(token);continue
        if not token.isalpha() or len(token)>40:return None
        paths={0:[[]]}
        for n in range(len(token)):
            for prefix in paths.get(n,[]):
                if len(prefix)>=5:continue
                for word in sorted(KATA):
                    if token.startswith(word,n):
                        target=paths.setdefault(n+len(word),[])
                        if len(target)<2:target.append(prefix+[word])
        splits=paths.get(len(token),[])
        if len(splits)!=1:return None
        result.extend(splits[0]);changes.append({'token_asli':token,'kata':splits[0]})
    return result,changes


def terbilang_lengkap(words):
    """Validasi susunan bahasa bilangan, tanpa menghasilkan nominal jawaban."""
    units='satu dua tiga empat lima enam tujuh delapan sembilan'.split()
    def under100(ws):
        if not ws:return False
        if len(ws)==1:return ws[0] in units+['nol','kosong','sepuluh','sebelas']
        if ws[0] in units[1:] and ws[1]=='belas':return len(ws)==2
        if ws[0] in units[1:] and ws[1]=='puluh':return len(ws)==2 or len(ws)==3 and ws[2] in units
        return False
    def small(ws):
        if not ws:return False
        if ws[0]=='seratus':return len(ws)==1 or under100(ws[1:])
        if len(ws)>1 and ws[0] in units and ws[1]=='ratus':return len(ws)==2 or under100(ws[2:])
        return under100(ws)
    ws=list(words)
    if ws and ws[0]=='minus':ws=ws[1:]
    if not ws or ws[-1]!='rupiah':return False
    ws=ws[:-1];head=[];previous=float('inf');scales=0
    for word in ws:
        if word not in SKALA:head.append(word);continue
        if SKALA[word]>=previous:return False
        if word in ('seribu','sejuta','semiliar'):
            if head:return False
        elif not small(head):return False
        head=[];previous=SKALA[word];scales+=1
    return (small(head) if head else bool(scales))


def segmen(s):
    return {'id_segmen': s['id_segmen'], 'teks_asli': s['teks_asli'],
            'bbox_asal': s.get('bbox_asal', s.get('bbox')),
            'halaman': s.get('halaman', s.get('nomor_halaman')),
            'skor': s.get('skor', s.get('skor_keyakinan'))}


def cek_konflik_terbilang(words, candidate):
    """Guard tambahan token rusak: kata tidak pernah menjadi digit keluaran."""
    if not terbilang_lengkap(words):return False
    units=dict(zip('nol kosong satu dua tiga empat lima enam tujuh delapan sembilan'.split(),[0,0,1,2,3,4,5,6,7,8,9]))
    def small(ws):
        if not ws:return 0
        if ws[0]=='seratus':return 100+small(ws[1:])
        if len(ws)>1 and ws[1]=='ratus':return units[ws[0]]*100+small(ws[2:])
        if len(ws)==1:return {'sepuluh':10,'sebelas':11}.get(ws[0],units.get(ws[0]))
        if ws[1]=='belas':return units[ws[0]]+10
        if ws[1]=='puluh':return units[ws[0]]*10+small(ws[2:])
        raise ValueError('Tata bahasa bilangan tidak didukung')
    negative=words[0]=='minus';ws=words[1:-1] if negative else words[:-1]
    value=0;head=[]
    for word in ws:
        if word not in SKALA:head.append(word);continue
        value+=(1 if word in ('seribu','sejuta','semiliar') else small(head))*SKALA[word];head=[]
    value+=small(head)
    return Decimal(-value if negative else value)==candidate


def locale_total(raw, total_segments, source_segments, width):
    """Grup tiga digit; pemisah campuran hanya dengan terbilang lengkap.

    Skala terbilang harus membedakan kandidat ribuan dan desimal. Sel dan
    terbilang satu halaman, area kanan, dekat vertikal, skor >= .85.
    Tidak mengekstrak nominal dari terbilang atau memilih berdasarkan rumus.
    """
    spaced=bool(re.fullmatch(r'-?\d{1,3}(?:[.,]\d{3})+ \d{3}',raw.strip()))
    if not spaced and not re.fullmatch(r'-?\d{1,3}(?:[.,]\d{3})+', raw.strip()): return None
    total = [segmen(s) for s in total_segments]
    labels = [s for s in total if re.fullmatch(r'(?i)total\s*:', s['teks_asli'].strip())]
    values = [s for s in total if s['teks_asli'].strip() == raw.strip()]
    if len(labels) != 1 or len(values) != 1 or len(total) != 2: return None
    label, value = labels[0], values[0]
    b, l = value['bbox_asal'], label['bbox_asal']; height = b[3]-b[1]
    if width <= 0 or not 0 <= b[0] < b[2] <= width: return None
    if height <= 0 or l[0] < width/2 or b[0] < l[2]: return None
    if abs((b[1]+b[3]-l[1]-l[3])/2) > .6*max(height,l[3]-l[1]): return None
    if any(s['skor'] is None or s['skor'] < .85 for s in total): return None
    candidates=[]
    for original in source_segments:
        s=segmen(original); sb=s['bbox_asal']
        if s['halaman'] != value['halaman'] or sb[0] < width/2: continue
        if not b[3] <= sb[1] <= b[3]+5*height: continue
        parsed=kata_bilangan(s['teks_asli'])
        if parsed and parsed[0]: candidates.append(s)
    candidates.sort(key=lambda s:(s['bbox_asal'][1],s['bbox_asal'][0]))
    if not 1 <= len(candidates) <= 8: return None
    if any(s['skor'] is None or s['skor'] < .85 for s in candidates): return None
    # OCR dapat memecah satu baris terbilang menjadi beberapa region. Kelompok
    # y lalu urut x; jangan menganggap region sejajar sebagai baris bertumpuk.
    lines=[]
    for s in candidates:
        row=next((g for g in lines if abs((s['bbox_asal'][1]+s['bbox_asal'][3])/2-
                     sum((v['bbox_asal'][1]+v['bbox_asal'][3])/2 for v in g)/len(g))<=.6*height),None)
        if row is None:lines.append([s])
        else:row.append(s)
    for a,z in zip(lines,lines[1:]):
        if not -.5*height <= min(v['bbox_asal'][1] for v in z)-max(v['bbox_asal'][3] for v in a) <= height:return None
    candidates=[s for row in lines for s in sorted(row,key=lambda s:s['bbox_asal'][0])]
    parsed=[kata_bilangan(s['teks_asli']) for s in candidates]
    words=[w for ws,changes in parsed for w in ws]
    if words[-1] != 'rupiah' or words.count('rupiah') != 1: return None
    scales=[SKALA[w] for w in words if w in SKALA]
    if not scales: return None
    lower=Decimal(max(scales)); upper=lower*1000
    # Spasi tunggal hanya diterima sebagai batas grup tiga digit yang sudah
    # tervalidasi di atas, bukan penghapusan spasi/pemisah bebas.
    digits=raw.replace('.','').replace(',','').replace(' ','') if spaced else raw.replace('.','').replace(',','')
    grouped=abs(Decimal(digits))
    mixed='.' in raw and ',' in raw
    if (mixed or spaced) and not terbilang_lengkap(words):return None
    alternatives=[]
    for group,decimal in ((',','.'),('.',',')):
        canonical=raw.replace(group,'')
        if re.fullmatch(r'-?\d+'+re.escape(decimal)+r'\d+',canonical):alternatives.append(abs(Decimal(canonical.replace(decimal,'.'))))
    if spaced:
        groups=re.split(r'[., ]',raw.lstrip('-'))
        alternatives=[Decimal(''.join(groups[:n])+'.'+''.join(groups[n:])) for n in range(1,len(groups))]
    if not lower <= grouped < upper or any(lower<=v<upper for v in alternatives): return None
    if raw.startswith('-') != ('minus' in words): return None
    result={'locale':'id_ID' if '.' in raw else 'en_US',
            'aturan':'Skala terbilang dekat label Total membedakan ribuan dari desimal; tidak mengambil nominal dari kata.',
            'batas_skala':[str(lower),str(upper)], 'lebar_gambar':width,
            'segmen_total':total, 'segmen_terbilang':candidates,
            'status_manusia':'belum_diverifikasi'}
    if mixed:
        result.update(teks_integer_konteks=raw.replace('.','').replace(',',''),
                      aturan='Seluruh grup tepat tiga digit; skala terbilang lengkap dekat Total menolak kedua tafsir desimal. Tidak memakai nominal kata, field lain atau rumus.',
                      grammar_sumber='grup_tiga_digit_campuran_dengan_bukti_skala_Total',
                      kandidat_desimal_ditolak=[str(v) for v in alternatives])
    if spaced:
        if not cek_konflik_terbilang(words,Decimal(digits)):return None
        result.update(teks_integer_konteks=digits,versi_pola_spasi=VERSI,
            aturan='Satu pemisah hilang menjadi spasi pada grup integer tiga digit; skala dan kalimat terbilang lengkap menolak kandidat lain. Digit hanya dari sel Total, bukan kata/rumus/field lain.',
            grammar_sumber='grup_tiga_digit_satu_spasi_dengan_bukti_Total',
            kandidat_desimal_ditolak=[str(v) for v in alternatives],
            guard_terbilang={'status':'tidak_bertentangan','kata_bukan_sumber_digit':True})
    changes=[{'id_segmen':s['id_segmen'],'pemisahan':p[1]} for s,p in zip(candidates,parsed) if p[1]]
    if changes:
        if not terbilang_lengkap(words):return None
        result.update(versi_pemisahan_kata='total-terbilang-v2',normalisasi_kata_bilangan=changes,
                      kata_terbilang_dipisahkan=words,
                      aturan_pemisahan_kata='Segmentasi kosakata bilangan tunggal, tanpa substitusi huruf; raw dan bbox region tetap.')
    return result
