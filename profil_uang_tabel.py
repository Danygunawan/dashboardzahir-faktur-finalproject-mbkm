"""Profil area tabel uang dari literal/layout; tidak memakai footer atau rumus."""
import copy,re,statistics
from normalisasi_angka import analisis_angka
VERSI='profil-uang-tabel-v1'

def bangun(cells,anchors):
    result={'versi':VERSI,'status':'belum_cukup','anchor':[], 'alasan':'Belum ada anchor pengelompokan literal pada tabel uang.'}
    money=[c for c in cells if c['kolom'] in ('harga_satuan','subtotal_item')]
    if not money or any(not c['header_valid'] or not c['pemetaan_valid'] for c in money):return {**result,'alasan':'Header atau pemetaan uang tabel belum sah seluruhnya.'}
    if len({c['kolom'] for c in money})!=2 or len({c['item'] for c in money})<2:return {**result,'alasan':'Perlu dua kolom uang dan sedikitnya dua baris.'}
    hs=[s for c in money for s in c['bukti'].get('judul_kolom',{}).get('segmen',[])]
    if not hs:return result
    h=statistics.median(s['bbox_asal'][3]-s['bbox_asal'][1] for s in hs)
    yy=[(s['bbox_asal'][1]+s['bbox_asal'][3])/2 for s in hs]
    if max(yy)-min(yy)>.6*h:return {**result,'alasan':'Header kolom tidak berada pada satu band tabel.'}
    raw=[c['teks'].strip() for c in money]
    # Nol bukan anchor locale. Integer tanpa pemisah nonnol, desimal eksplisit,
    # koma/format campuran dan field rusak menahan profil lintas kolom.
    nonzero=[t for t in raw if t not in ('0','0.00','0,00','-0','-0.00','-0,00')]
    if not nonzero or any(not re.fullmatch(r'-?\d{1,3}(?:\.\d{3})+',t) for t in nonzero):return {**result,'alasan':'Konvensi tabel tidak homogen: ada desimal/format campuran/kosong atau token rusak; tidak meminjam locale antar kolom.'}
    if any((m:=re.fullmatch(r'-?\d{1,3}\.(\d{3})',t)) and m[1]!='000' for t in nonzero):
        return {**result,'alasan':'Ekor3digit nonnol dapat merupakan tiga desimal yang sah; profil lintas kolom ini tidak cukup untuk memutuskannya.'}
    local=[a for a in anchors if a['locale']=='id_ID' and a['presisi']=='integer' and re.fullmatch(r'-?\d{1,3}(?:\.\d{3}){2,}',a['teks'])]
    if not local:return result
    if len(set(nonzero))<3:return {**result,'alasan':'Variasi literal tabel belum cukup untuk profil dokumen.'}
    return {'versi':VERSI,'status':'pengelompokan_tabel_didukung','locale':'id_ID','anchor':copy.deepcopy(local),
        'bukti_konsistensi':[{'kolom':c['kolom'],'item':c['item'],'teks':c['teks'],'segmen':copy.deepcopy(c['bukti']['segmen'])} for c in money],
        'wajib_review':True,'alasan':'Tabel uang berheader/sejajar, >=2baris/2kolom, semua literal nonnol bergrup titik3, >=3literal berbeda dan anchor dengan >=2grup di area tabel. Inferensi konvensi bersama, bukan nilai dari kolom lain/rumus/footer; format campuran ditahan.'}

def nilai(cell,profile):
    if profile.get('status')!='pengelompokan_tabel_didukung' or not re.fullmatch(r'-?\d{1,3}\.\d{3}',cell['teks'].strip()):return None
    a=analisis_angka(cell['teks'],'id_ID')
    if a['nilai']=='':return None
    a.update(status='normalisasi_terstruktur',wajib_review=True,alasan='Konvensi pengelompokan ditafsirkan dari tabel yang konsisten dan anchor multigrup, bukan verifikasi manusia.')
    return {'nilai':a['nilai'],'locale':'id_ID','status':'konteks_format_tabel_lintas_kolom','wajib_review':True,
        'anchor':profile['anchor'],'profil_dokumen':profile,'analisis_normalisasi':a,'alasan':profile['alasan']}
