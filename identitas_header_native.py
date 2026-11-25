"""Identitas native sebelum judul Invoice; tidak membaca acuan/berkas jawaban."""
import copy,re
from datetime import datetime

VERSI='identitas-header-native-v1'


def tanggal_numerik_unik(raw):
    """Terima hanya jika semua interpretasi kalender sah menghasilkan satu tanggal."""
    if not re.fullmatch(r'\d{2}/\d{2}/\d{4}',raw):return None
    options={}
    for fmt in ('%d/%m/%Y','%m/%d/%Y'):
        try:options[fmt]=datetime.strptime(raw,fmt).date().isoformat()
        except ValueError:pass
    values=set(options.values())
    if len(values)!=1:return None
    return {'nilai':next(iter(values)),'interpretasi_kalender_sah':options,
            'aturan':'Seluruh interpretasi dd/mm dan mm/dd yang sah harus menghasilkan tanggal ISO yang sama; tidak memilih urutan dari nama/nomor/berkas.'}


def identitas_sebelum_judul(reader,title):
    """Label Nomor inline dari satu baris native; tanggal satu kolom di bawahnya.

    Header dapat berada sebelum judul, tetapi bukan alasan mengambil teks kop
    penerbit. Tidak memakai seluruh band horizontal yang bisa memuat Telp/Fax.
    Semua batas angka/label berasal dari kata native, tidak bbox substring OCR.
    """
    if title['teks'].strip().casefold()!='invoice':return {}
    from ekstrak_faktur_digital import cy,teks
    top=[s for s in reader.lines if s['bbox'][3]<title['bbox'][1]]
    labels=[s for s in top if re.match(r'(?i)^(?:nomor|nomor\s+invoice|no\.?\s+invoice|invoice\s+no)\s*:',s['teks'].strip())]
    if len(labels)!=1:return {}
    line=labels[0];box=line['bbox'];h=box[3]-box[1]
    words=sorted([w for w in reader.words if box[0]-.1<=w['bbox'][0] and w['bbox'][2]<=box[2]+.1
                  and abs(cy(w)-cy(line))<=.5*max(h,w['bbox'][3]-w['bbox'][1])],key=lambda w:w['bbox'][0])
    colon=next((i for i,w in enumerate(words) if w['teks']==':' or w['teks'].endswith(':')),None)
    if colon is None:return {}
    label_words=words[:colon+1];values=words[colon+1:]
    value=teks(values)
    if not re.fullmatch(r'(?=.*\d)[A-Za-z0-9][A-Za-z0-9/\\().\-]*',value):return {}
    saved=copy.deepcopy(reader.fields)
    try:
        reader.simpan('nomor_invoice',values,'Label Nomor inline sebelum judul Invoice; kata native dalam baris sumber yang sama',labels=label_words)
        numbered=copy.deepcopy(reader.fields['nomor_invoice'])
        numbered.update(label_asal=teks(label_words).rstrip(':').strip(),
            status_identitas='nomor_dokumen_berjudul_Invoice_belum_konfirmasi_transaksi_final',
            deduplikasi_otomatis_diizinkan=False,
            provenance_identitas_header={'versi':VERSI,'judul':title,'baris_asli':line,
                'area_nilai_adalah':'kata sesudah kolon dalam baris native yang sama; bukan kop/kontak penerbit',
                'identitas_dokumen_bukan_konfirmasi_transaksi_final':True})
        proposals={'nomor_invoice':numbered}
        dates=[s for s in top if 0<s['bbox'][1]-box[3]<=4*reader.h
               and abs(s['bbox'][0]-box[0])<=reader.h
               and re.fullmatch(r'\d{2}/\d{2}/\d{4}(?:\s+\d{1,2}:\d{2}\s+[AP]M)?',s['teks'].strip())]
        if len(dates)==1:
            dt=dates[0];raw=dt['teks'].strip()[:10];parsed=tanggal_numerik_unik(raw)
            tokens=[w for w in reader.words if w['teks']==raw and dt['bbox'][0]-.1<=w['bbox'][0]
                    and w['bbox'][2]<=dt['bbox'][2]+.1 and abs(cy(w)-cy(dt))<=.5*(dt['bbox'][3]-dt['bbox'][1])]
            if parsed and len(tokens)==1:
                ev=reader.evidence(tokens,'Tanggal native pada kolom header bernomor; interpretasi kalender tunggal',parsed['nilai'],labels=label_words)
                ev['aturan_normalisasi']=parsed['aturan']
                ev['provenance_identitas_header']={'versi':VERSI,'baris_tanggal_asli':dt,
                    'baris_nomor_pengikat':line,'interpretasi_kalender_sah':parsed['interpretasi_kalender_sah'],
                    'jam_tidak_masuk_tanggal':True}
                proposals['tanggal_invoice']=ev
        return proposals
    finally:reader.fields=saved
