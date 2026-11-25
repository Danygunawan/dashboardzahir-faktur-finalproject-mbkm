"""I/O atomik, validasi terkurasi, dan antrean review batch lokal."""
import copy
import csv
import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from ekstrak_faktur_digital import FAKTUR, DETAIL, TOLERANSI, cek_hitung, dec
from format_ringkasan import kenali_ringkasan_nominal, kenali_setelah_pajak, kenali_setelah_diskon, kenali_rekap_diskon_ppn_pph, kenali_basis_sebelum_diskon


def now(): return datetime.now(timezone.utc).isoformat()
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def fingerprint(data): return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def read_json(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def atomic_json(path,data):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+'.tmp_'+uuid4().hex)
    with temp.open('x',encoding='utf-8') as f:
        json.dump(data,f,ensure_ascii=False,indent=2,allow_nan=False); f.flush(); os.fsync(f.fileno())
    os.replace(temp,path)


def csv_write(path,rows,fields):
    with Path(path).open('x',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows); f.flush(); os.fsync(f.fileno())


def seal(folder):
    folder=Path(folder)
    hashes={str(p.relative_to(folder)):digest(p) for p in folder.rglob('*') if p.is_file() and p.name!='complete.json'}
    atomic_json(folder/'complete.json',{'files':hashes,'selesai':now()})


def complete(folder):
    try:
        root=Path(folder).resolve(); files=read_json(root/'complete.json')['files']
        return bool(files) and all((root/p).resolve().is_relative_to(root) and (root/p).is_file() and digest(root/p)==h for p,h in files.items())
    except (OSError,ValueError,KeyError,TypeError): return False


@contextmanager
def lock_run(folder):
    path=Path(folder)/'.lock'; path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as f:
        if f.tell()==0: f.write(b'0'); f.flush()
        f.seek(0)
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try: yield
        finally:
            f.seek(0)
            if os.name=='nt': msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
            else: fcntl.flock(f,fcntl.LOCK_UN)


def validate_invoice(inv, observation=None):
    """Validasi nilai sekarang, dapat diulang setelah koreksi; tanpa mengubah angka."""
    from validasi_ringkasan import siapkan_metadata, pemeriksaan_subtotal, pemeriksaan_ringkasan, pemeriksaan_satuan_pajak, pemeriksaan_satuan_diskon
    siapkan_metadata(inv, observation)
    row=inv['nilai']; checks=[]; details=inv['item']
    from diskon_item_bersatuan import nominal_item,harapan_item
    for i,it in enumerate(details,1):
        v=it['nilai']; keys=['jumlah','harga_satuan','diskon_item_persen','subtotal_item']
        q,p,d,s=[dec(v,k) for k in keys]
        expected,method=harapan_item(it)
        checks.append(cek_hitung('subtotal item',s,expected,item=i,rumus_satuan=method,
            komponen_kosong=[k for k in keys if v.get(k,'')=='' and not (k=='diskon_item_persen' and nominal_item(it) is not None)]))
    checks.append(pemeriksaan_subtotal(inv, observation))
    checks.extend(pemeriksaan_satuan_pajak(inv))
    checks.extend(pemeriksaan_satuan_diskon(inv))
    def calc(label,actual,keys,fn):
        vals=[dec(row,k) for k in keys]
        checks.append(cek_hitung(label,dec(row,actual),fn(*vals) if None not in vals else None,komponen_kosong=[k for k in keys+[actual] if row.get(k,'')=='']))
    tax_format=kenali_setelah_pajak(inv);discount_format=kenali_setelah_diskon(inv)
    if kenali_rekap_diskon_ppn_pph(inv):
        calc('total akhir = sebelum diskon - discount + PPN + PPh bertanda','total_akhir',['total_sebelum_pajak','diskon_final_nominal','pajak_faktur','pph_tercetak'],lambda t,d,p,h:t-d+p+h)
        checks.append({'pemeriksaan':'diskon footer','status':'tidak_berlaku','alasan':'Subtotal item sudah setelah diskon; footer dibandingkan pada basis label sebelum diskon, bukan dikurangi lagi dari item.'})
    elif row.get('total_setelah_pajak','')!='' and row.get('dpp_tercetak','')!='':
        calc('DPP = subtotal','dpp_tercetak',['subtotal_faktur'],lambda x:x)
        calc('DPP lain = DPP * 11/12 (hipotesis format)','dpp_lain_tercetak',['dpp_tercetak'],lambda x:x*Decimal(11)/12)
        calc('PPN dari label persen dan DPP lain','pajak_faktur',['dpp_lain_tercetak','ppn_persen_tercetak'],lambda x,p:x*p/100)
        calc('total setelah pajak = DPP + PPN','total_setelah_pajak',['dpp_tercetak','pajak_faktur'],lambda x,p:x+p)
        calc('Grand Total = total setelah pajak + PPH bertanda','total_akhir',['total_setelah_pajak','pph_tercetak'],lambda x,p:x+p)
    elif tax_format:
        if tax_format['basis_pajak']=='total_sebelum_pajak':
            calc('total sebelum pajak = subtotal - diskon nominal','total_sebelum_pajak',['subtotal_faktur','diskon_final_nominal'],lambda s,d:s-d)
        calc('total setelah pajak = basis tercetak + pajak','total_setelah_pajak',[tax_format['basis_pajak'],'pajak_faktur'],lambda b,p:b+p)
        if tax_format['memiliki_pph']:
            calc('total akhir = setelah pajak - PPh pemotongan','total_akhir',['total_setelah_pajak','pph_tercetak'],lambda t,p:t-p)
        else:calc('total akhir = setelah pajak','total_akhir',['total_setelah_pajak'],lambda t:t)
    elif discount_format:
        calc('total setelah diskon = total sebelum diskon - diskon akhir','total_akhir',['subtotal_faktur','diskon_final_nominal'],lambda s,d:s-d)
    elif inv.get('format_total')=='diskon_sebelum_pajak_dengan_total_setelah_pajak':
        calc('total sebelum pajak = subtotal - diskon nominal','total_sebelum_pajak',['subtotal_faktur','diskon_final_nominal'],lambda s,d:s-d)
        calc('total setelah pajak = sebelum pajak + pajak','total_setelah_pajak',['total_sebelum_pajak','pajak_faktur'],lambda s,p:s+p)
        calc('total akhir = total setelah pajak','total_akhir',['total_setelah_pajak'],lambda t:t)
        calc('total akhir = sebelum pajak + pajak (pemeriksaan independen)','total_akhir',['total_sebelum_pajak','pajak_faktur'],lambda s,p:s+p)
    else:
        checks.extend(pemeriksaan_ringkasan(inv, observation))
    checks.append({'pemeriksaan':'kelengkapan','status':'lolos_otomatis' if details and all(row.get(k) for k in ['nomor_invoice','tanggal_invoice','nama_pelanggan']) else 'tidak_dapat_diperiksa'})
    nums=[it['nilai']['nomor_baris_item'] for it in details]
    checks.append({'pemeriksaan':'relasi ID dan nomor item','status':'lolos_otomatis' if len(nums)==len(set(nums)) and all(it['nilai']['id_faktur']==row['id_faktur'] for it in details) else 'selisih'})
    from validasi_ringkasan import tandai_hipotesis_diskon
    return tandai_hipotesis_diskon(checks,inv)


def review_queue(invoices,duplicates=()):
    queue=[]
    def add(priority,inv,kind,field='',item='',message='',ev=None):
        queue.append({'prioritas':priority,'id_faktur':inv['nilai']['id_faktur'],'nomor_item':item,'jenis':kind,'kolom':field,'pesan':message,'bukti':ev})
    for inv in invoices:
        for scope,cols in [('',inv.get('kolom',{}))]+[(str(i),it.get('kolom',{})) for i,it in enumerate(inv['item'],1)]:
            for k,ev in cols.items():
                if ev.get('pembacaan_ulang'):
                    add(3,inv,'kandidat_ocr_ulang',k,scope,'Nilai dari potongan angka; belum diverifikasi manusia',ev)
        if inv.get('catatan_layout'): add(2,inv,'struktur_inferensi',message=inv['catatan_layout'],ev=inv.get('bukti_struktur'))
        for k in ['nomor_invoice','tanggal_invoice','nama_pelanggan','nama_penerbit']:
            ev=inv.get('kolom',{}).get(k,{})
            low=any(s.get('skor',1) is not None and s.get('skor',1)<.85 for s in ev.get('segmen',[]))
            if not inv['nilai'].get(k) or low: add(1,inv,'identitas',k,message='Identitas kosong atau skor rendah',ev=ev)
        if not inv['item']: add(2,inv,'item_hilang',message='Tidak ada rincian item')
        for i,it in enumerate(inv['item'],1):
            for k in ['nama_barang','jumlah','harga_satuan','subtotal_item','diskon_item_persen']:
                ev=it.get('kolom',{}).get(k,{})
                low=any(s.get('skor',1) is not None and s.get('skor',1)<.85 for s in ev.get('segmen',[]))
                if it['nilai'].get(k,'')=='' or low: add(2 if k=='nama_barang' else 3,inv,'nilai_item',k,i,'Nilai kosong/ambigu atau skor rendah',ev)
                if k=='diskon_item_persen' and ('inferensi' in ev.get('metode','').lower() or ev.get('interpretasi_satuan',{}).get('inferensi_kolom_persen')):
                    add(3,inv,'satuan_diskon',k,i,'Persen merupakan inferensi, bukan verifikasi manusia',ev)
        for k in ['subtotal_faktur','total_akhir']:
            if inv['nilai'].get(k,'')=='': add(3,inv,'uang',k,message='Ringkasan uang belum ditemukan',ev=inv.get('kolom',{}).get(k))
        for c in validate_invoice(inv):
            if c['status']!='lolos_otomatis': add(3,inv,'validasi',item=c.get('item',''),message=json.dumps(c,ensure_ascii=False),ev=c)
        for note in inv.get('keterangan_tabel',[]):
            add(2 if note.get('risiko_item_hilang') else 8,inv,'kemungkinan_item_hilang' if note.get('risiko_item_hilang') else 'keterangan',message=note.get('nilai') or note.get('teks',''),ev=note)
        for k,ev in inv.get('kolom',{}).items():
            if ev.get('error_parsing') or any(s.get('skor') is not None and s['skor']<.85 and re.search(r'\w',s.get('teks_asli','')) for s in ev.get('segmen',[])):
                add(3,inv,'field_perlu_review',k,message=ev.get('error_parsing') or 'Skor rendah pada field terpakai',ev=ev)
        for s in inv.get('review_skor',{}).get('segmen',[]):
            add(9,inv,'informasi_skor',message=s['teks_asli'],ev=s)
    for d in duplicates:
        for inv in invoices:
            if inv['nilai']['id_faktur'] in d['id_faktur']: add(4,inv,'dugaan_duplikat',message=json.dumps(d,ensure_ascii=False),ev=d)
    return sorted(queue,key=lambda q:(q['prioritas'],q['id_faktur'],str(q['nomor_item']),q['kolom']))


def duplicate_candidates(invoices):
    groups={}
    for inv in invoices:
        r=inv['nilai']; key=tuple(re.sub(r'\s+',' ',r.get(k,'').strip()).casefold() for k in ['nama_penerbit','nomor_invoice'])
        if all(key): groups.setdefault(key,[]).append(inv)
    return [{'penerbit_nomor':key,'id_faktur':[i['nilai']['id_faktur'] for i in vals],
             'pendukung':[{'tanggal':i['nilai']['tanggal_invoice'],'pelanggan':i['nilai']['nama_pelanggan'],'total':i['nilai']['total_akhir'],'hash':i.get('hash_sumber')} for i in vals],
             'status':'perlu_review','aksi':'Tidak digabung otomatis'} for key,vals in groups.items() if len(vals)>1]


def export(folder,invoices,extra):
    folder=Path(folder); folder.mkdir(parents=True,exist_ok=False)
    ids=[i['nilai']['id_faktur'] for i in invoices]
    if len(ids)!=len(set(ids)): raise ValueError('ID faktur berulang; perlu review sebelum ekspor')
    if any(it['nilai']['id_faktur'] not in ids for inv in invoices for it in inv['item']): raise ValueError('Item tanpa induk')
    duplicates=duplicate_candidates(invoices); queue=review_queue(invoices,duplicates)
    for inv in invoices:
        # Status batch tidak menolak faktur hanya karena tanda baca/keterangan non-field.
        critical=[q for q in queue if q['id_faktur']==inv['nilai']['id_faktur'] and q['prioritas']<=4]
        inv['status_batch']='perlu_review' if critical else 'selesai'
    csv_write(folder/'faktur.csv',[i['nilai'] for i in invoices],FAKTUR)
    csv_write(folder/'detail_faktur.csv',[it['nilai'] for i in invoices for it in i['item']],DETAIL)
    fields=['prioritas','id_faktur','nomor_item','jenis','kolom','pesan']
    csv_write(folder/'daftar_review.csv',[{k:q[k] for k in fields} for q in queue],fields)
    atomic_json(folder/'bukti_ekstraksi.json',{'faktur':invoices,'dugaan_duplikat':duplicates,'review':queue,**extra})
    lines=['# Laporan batch/kurasi','',f'Hasil: {len(invoices)} faktur, {sum(len(i["item"]) for i in invoices)} item. Total faktur tidak dijumlah ulang per item.','']
    for inv in invoices:
        r=inv['nilai']; lines += [f'- {r["nomor_invoice"]}: {len(inv["item"])} item; total {r["total_akhir"]}; status batch {inv["status_batch"]}.',f'  Catatan: {r["catatan_validasi"]}']
        lines += [f'  {c["pemeriksaan"]}, item {c.get("item","-")}: {c["status"]}; selisih {c.get("selisih")}; toleransi {c.get("toleransi")}.' for c in validate_invoice(inv)]
    lines += ['', 'Status selesai/lolos otomatis bukan verifikasi manusia. Dugaan duplikat tidak disatukan. Keterangan termin bukan transaksi atau pembayaran terverifikasi. Lihat bukti dan daftar_review.csv.']
    (folder/'laporan.md').write_text('\n'.join(lines)+'\n',encoding='utf-8'); seal(folder)
    return folder
