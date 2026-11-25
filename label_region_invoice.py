"""Label invoice tertanam pada region pelanggan; bbox tetap region asli."""
import re

VERSI = 'label-region-invoice-v2'

def invoice_region(reader,area):
    x0,y0,x1,y1=area
    found=[]
    # Toleransi 0 hanya pada kata label invoice, tidak pada nilai identitas.
    # Kolon boleh hilang hanya ketika region berakhir tepat setelah label.
    # Nilai tetap harus satu region kanan sejajar, bukan string bebas sesudahnya.
    pattern=r'(?i)\b(?:nomor|no\.?)\s+inv[o0]ice(?:\s*:\s*(.*)|\s*)$'
    for s in reader.s:
        b=s['bbox'];cy=(b[1]+b[3])/2
        if not y0<=cy<y1 or b[2]<=x0 or b[0]>=x1:continue
        m=re.search(pattern,s['teks_asli'])
        if not m:continue
        # Region boleh melintasi batas pelanggan/identitas. Tidak ada estimasi
        # posisi kata di dalam region; pemisahan semata indeks karakter.
        if b[2]<reader.w*.55:continue
        value=(m[1] or '').strip();ss=[s]
        if not value:
            stops=[t for t in reader.s if t is not s and t['bbox'][0]>=b[2]-reader.h
                   and abs((t['bbox'][1]+t['bbox'][3])/2-cy)<=.60*max(t['bbox'][3]-t['bbox'][1],b[3]-b[1])
                   and re.fullmatch(r'(?i)(?:telp\.?|telepon|fax|telp\s*/\s*fax|npwp|no\.?\s*npwp|tanggal|up|pembayaran)\s*(?::.*)?',t['teks_asli'].strip())]
            edge=min([t['bbox'][0] for t in stops]+[x1])
            vv=[t for t in reader.s if t is not s and t['bbox'][0]>=max(reader.w*.55,b[2]-reader.h)
                and t['bbox'][2]<=edge and y0<=(t['bbox'][1]+t['bbox'][3])/2<y1
                and abs((t['bbox'][1]+t['bbox'][3])/2-cy)<=.60*max(t['bbox'][3]-t['bbox'][1],b[3]-b[1])
                and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9/\\().\-]*',t['teks_asli'].strip())]
            if len(vv)!=1:continue
            ss+=vv;value=vv[0]['teks_asli'].strip()
        if not re.fullmatch(r'(?=.*\d)[A-Za-z0-9][A-Za-z0-9/\\().\-]*',value):continue
        ev=reader.nilai(value,ss,'Label Nomor Invoice tertanam di region pelanggan; nilai inline atau region kanan sejajar')
        ev['pemisahan_label_region']={'label_asli':m[0][:-len(m[1])] if m[1] else m[0],
            'rentang_karakter_label':[m.start(),m.start(1) if m[1] is not None else m.end()],
            'rentang_karakter_nilai':[m.start(1),m.end(1)] if m[1] is not None else None,
            'versi':VERSI,'kolon_hilang_pada_ujung_label':m[1] is None,
            'bbox_adalah':'Seluruh region asal; tidak mengklaim bbox kata/substring.',
            'aturan':'Label lengkap inv[o0]ice + posisi area identitas + satu pola identitas bernumeral. Tidak mengoreksi karakter nilai.'}
        if len(ss)>1:
            ev['pemisahan_label_region']['batas_kanan_label_tetangga']=edge
            ev['pemisahan_label_region']['label_penghalang']=[{'teks_asli':t['teks_asli'],'bbox_asal':t['bbox']} for t in stops]
        if '0' in ev['pemisahan_label_region']['label_asli']:
            ev['perlu_review_identitas']='Typo label ditoleransi dengan posisi; karakter nilai OCR tidak diverifikasi manusia.'
        found.append(ev)
    if len(found)==1:return found[0]
    if len(found)>1:return reader.bukti('',[],'Label invoice tertanam tidak tunggal','ambigu')
    return None
