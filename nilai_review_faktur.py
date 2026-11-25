"""Penilaian cakupan dan review dari bukti tersimpan; tidak mengekstrak/mengubah angka."""
import argparse
import copy
import json
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from batch_common import read_json,digest,complete,atomic_json,csv_write,seal,fingerprint,duplicate_candidates
from ekstrak_faktur_digital import FAKTUR,DETAIL,angka,cek_hitung
from baca_ulang_angka import kandidat_angka, kandidat_sel_persen
from uji_ocr_gambar import CONFIG
from format_ringkasan import kenali_ringkasan_nominal, identitas_invoice_fallback, kenali_setelah_pajak, kenali_setelah_diskon, asal_subtotal, kenali_basis_sebelum_diskon

NUM_ITEM=['jumlah','harga_satuan','diskon_item_persen','subtotal_item','pajak_item_tercetak']
NUM_DOC=['subtotal_faktur','total_sebelum_pajak','diskon_final_persen','diskon_final_nominal','pajak_faktur','total_akhir','dpp_tercetak','dpp_lain_tercetak','ppn_persen_tercetak','total_setelah_pajak','pph_tercetak']
MUTABLE={'status_validasi','catatan_validasi'}
VERSI='penilai-alasan-v4-satuan-label'


def raw(ev):
    return ev.get('teks_diambil',' '.join(ev.get('teks_asli',[])))


def boxes(ev):
    roi=ev.get('pembacaan_ulang',{})
    if roi.get('mode')=='sel_persen' and roi.get('kandidat',{}).get('bbox_sumber'):
        return [roi['kandidat']['bbox_sumber']]
    return [s['bbox_asal'] for s in ev.get('segmen',[])] or ([ev['bbox']] if ev.get('bbox') else [])


def diskon_eksplisit(ev):
    from validasi_ringkasan import metadata_satuan
    return metadata_satuan(ev)['satuan']=='persen'


def pemetaan_sel(inv,it,key):
    """Slot kolom dan pasangan vertikal harus ada; ID segmen bukan nomor barang."""
    ev=it['kolom'][key];bs=boxes(ev);ref=boxes(it['kolom']['jumlah'])
    labels=boxes(ev.get('judul_kolom',{})) or [x['bbox'] for x in ev.get('label',[])]
    bounds=inv.get('struktur',{}).get('batas_kolom_piksel',inv.get('area',{}).get('batas_kolom_x',[]))
    if len(bs)!=1 or len(ref)!=1 or len(labels)!=1 or not bounds:return False
    b=bs[0];r=ref[0];h=labels[0];cx=lambda x:(x[0]+x[2])/2;cy=lambda x:(x[1]+x[3])/2
    slots=lambda x:[n for n,(a,z) in enumerate(zip(bounds,bounds[1:])) if a<=cx(x)<z]
    return slots(b)==slots(h) and bool(slots(b)) and abs(cy(b)-cy(r))<=.6*max(b[3]-b[1],r[3]-r[1]) and b[1]>h[3]


def pembacaan_valid(inv,ev):
    """Bukti harus mendukung nilai yang sudah ada. Tidak mengganti nilai gagal."""
    if ev.get('analisis_normalisasi',{}).get('wajib_review'):return False
    if ev.get('nilai','')=='':return False
    locale=ev.get('locale_pemakaian',inv.get('format_angka',inv.get('struktur',{}).get('format_angka')))
    context=ev.get('bukti_locale_total')
    if context:
        from konteks_angka_total import locale_total
        if (locale_total(raw(ev),ev.get('segmen',[]),context.get('segmen_terbilang',[]),
                         context.get('lebar_gambar',0))!=context or context.get('locale')!=locale):return False
    try:
        supplemental=ev.get('bukti_ROI_tambahan')
        if supplemental:
            from bukti_ocr_tambahan import validasi_record, kandidat
            r=supplemental;raw_roi=validasi_record(r)
            if r['hash_sumber']!=inv['hash_sumber'] or r['nomor_halaman']!=int(inv['nilai']['halaman_sumber']):return False
            baseline=ev.get('riwayat_baseline',{})
            req={**r['permintaan'],'teks_baseline':baseline.get('teks_diambil',''),'locale_baseline':baseline.get('locale_pemakaian')}
            c=kandidat(raw_roi,req)
            if not c or c['nilai']!=ev['nilai'] or c['teks']!=raw(ev) or c['locale']!=locale:return False
            numeric=[s for s in ev.get('segmen',[]) if s['id_segmen'].startswith('ROI_')]
            segs=[s for s in r['segmen_sumber'] if s['asal_hasil']=='pengenalan']
            if len(numeric)!=1 or len(segs)!=1 or numeric[0]['bbox_asal']!=segs[0]['bbox'] or numeric[0]['teks_asli']!=segs[0]['teks_asli']:return False
        roi=ev.get('pembacaan_ulang')
        if roi:
            p=Path(roi['raw_json'])
            if (not complete(p.parent) or read_json(p.parent/'bukti.json')!=roi or roi['konfigurasi']!=CONFIG
                or roi['hash_sumber']!=inv['hash_sumber'] or digest(roi['path_sumber'])!=inv['hash_sumber']):return False
            if not any(s['id_segmen']==roi['segmen']['id_segmen'] and s['bbox_asal']==roi['segmen']['bbox'] and s['teks_asli']==roi['segmen']['teks_asli'] for s in ev.get('segmen',[])):return False
            if roi.get('mode')=='sel_persen':
                if roi.get('id_faktur')!=inv['nilai']['id_faktur'] or roi.get('sha256_sumber_sesudah')!=inv['hash_sumber']:return False
                if digest(roi['crop_ocr'])!=roi['sha256_crop_ocr'] or digest(roi['crop_asli'])!=roi['sha256_crop_asli']:return False
                meta=read_json(p.parent/'metadata.json')
                if meta['konfigurasi_base']!=CONFIG or meta['konfigurasi']!=roi['konfigurasi_aktual']:return False
                if meta['bbox_crop_sumber']!=roi['crop_bbox_sumber'] or meta['skala']!=roi['skala']:return False
                if Path(read_json(p).get('res',read_json(p))['input_path']).resolve()!=Path(roi['crop_ocr']).resolve():return False
                # Cocokkan piksel crop dengan sumber asli, bukan hanya klaim bbox.
                from PIL import Image
                with Image.open(roi['path_sumber']) as source:
                    if list(source.size)!=roi['ukuran_sumber']:return False
                    expected=source.crop(roi['crop_bbox_sumber']).convert('RGB')
                with Image.open(roi['crop_asli']) as crop:
                    if crop.size!=expected.size or crop.convert('RGB').tobytes()!=expected.tobytes():return False
                if roi['skala']==3:
                    expected=expected.resize((expected.width*3,expected.height*3),Image.Resampling.LANCZOS)
                with Image.open(roi['crop_ocr']) as crop:
                    if crop.size!=expected.size or crop.convert('RGB').tobytes()!=expected.tobytes():return False
                candidate=kandidat_sel_persen(read_json(p),roi)
            else:
                candidate=kandidat_angka(read_json(p),raw(ev),roi['locale'])
            return bool(candidate) and candidate['nilai']==ev['nilai'] and all(roi['kandidat'].get(k)==v for k,v in candidate.items())
        from normalisasi_angka import analisis_angka
        from ekstrak_faktur_ocr import input_angka_sumber
        numeric=input_angka_sumber(raw(ev))[0]
        if context and context.get('teks_integer_konteks'):numeric=context['teks_integer_konteks']
        convention=ev.get('konvensi_diskon')
        if convention:
            if convention!='magnitude_pengurangan_kurung' or not re.fullmatch(r'(?i)\(\s*(?:Rp\.?|IDR)\s+[\d.,]+\s*\)',raw(ev).strip()):return False
            numeric=numeric.lstrip('-')
        if analisis_angka(numeric,locale)['wajib_review']:return False
        scores=[s.get('skor') for s in ev.get('segmen',[])]
        if any(s is None or s<.85 for s in scores):return False
        return bool(boxes(ev)) and angka(numeric,locale)==Decimal(ev['nilai']) and not ev.get('error_parsing')
    except (OSError,ValueError,KeyError,TypeError):return False


def diagnosis_pembacaan(inv, ev, it=None, key=None):
    """Penjelasan terpisah; tidak dipakai mengganti predicate kelulusan lama.

    Bukti tersedia berbeda dari pembacaan terkonfirmasi. Untuk ROI, validasi
    file/piksel/model dan hubungan nilai dijalankan walau format perlu review.
    Status pemeriksaan manusia tidak disimpulkan dari pemeriksaan ini.
    """
    from normalisasi_angka import analisis_angka
    from ekstrak_faktur_ocr import input_angka_sumber
    locale=ev.get('locale_pemakaian',inv.get('format_angka',inv.get('struktur',{}).get('format_angka')))
    a=ev.get('analisis_normalisasi') or analisis_angka(input_angka_sumber(raw(ev))[0],locale)
    d={'versi':VERSI,'format':{'status':a.get('status','belum_diketahui'),
       'wajib_review':bool(a.get('wajib_review')),'alasan':a.get('alasan',''),
       'teks_asli':raw(ev),'locale':locale},
       'pemetaan':('sah' if pemetaan_sel(inv,it,key) else 'gagal_atau_belum_cukup') if it is not None else 'label_ringkasan_dinilai_terpisah',
       'dukungan_sumber':'bukti_teks_dan_bbox_tersedia' if raw(ev) and boxes(ev) else 'bukti_belum_cukup',
       'lolos_pembacaan_otomatis':pembacaan_valid(inv,ev)}
    if ev.get('error_parsing'):d['gagal_parsing']=ev['error_parsing']
    r=ev.get('bukti_ROI_tambahan')
    if r:
        try:
            from bukti_ocr_tambahan import validasi_record,kandidat
            rr=validasi_record(r);base=ev.get('riwayat_baseline',{})
            req={**r['permintaan'],'teks_baseline':base.get('teks_diambil',''),'locale_baseline':base.get('locale_pemakaian')}
            c=kandidat(rr,req)
            segs=[s for s in r['segmen_sumber'] if s['asal_hasil']=='pengenalan']
            nums=[s for s in ev.get('segmen',[]) if s['id_segmen'].startswith('ROI_')]
            ok=(r['hash_sumber']==inv['hash_sumber'] and r['nomor_halaman']==int(inv['nilai']['halaman_sumber'])
                and c and c['nilai']==ev.get('nilai') and c['teks']==raw(ev) and c['locale']==locale
                and len(nums)==len(segs)==1 and nums[0]['bbox_asal']==segs[0]['bbox'] and nums[0]['teks_asli']==segs[0]['teks_asli'])
            d['dukungan_sumber']='ROI_hash_piksel_model_dan_nilai_cocok' if ok else 'ROI_tidak_cocok_dengan_nilai_atau_region'
        except (OSError,ValueError,KeyError,TypeError) as e:
            d['dukungan_sumber']='ROI_gagal_validasi';d['alasan_bukti']=str(e)
    return d


def alasan_pembacaan(d):
    parts=[]
    if d['format']['wajib_review']:
        parts.append('Format '+d['format']['status']+': '+(d['format']['alasan'] or 'interpretasi belum pasti'))
    if d['pemetaan']=='gagal_atau_belum_cukup':parts.append('Pemetaan sel gagal/belum cukup pada bbox, header atau keselarasan')
    if d['dukungan_sumber'] in ('bukti_belum_cukup','ROI_gagal_validasi','ROI_tidak_cocok_dengan_nilai_atau_region'):
        parts.append('Dukungan pembacaan sumber: '+d['dukungan_sumber'])
    if not d['lolos_pembacaan_otomatis'] and not parts:
        parts.append('Pembacaan belum lolos pemeriksaan teks/nilai, skor atau konteks; tersedianya bukti bukan konfirmasi kebenaran')
    return '; '.join(parts)+'. Nilai dipertahankan; bukan verifikasi manusia.'


def diagnosis_turunan(inv, origin, observation):
    """Kelengkapan nilai, aritmetika turunan, format dan cetakan terpisah."""
    rows=inv['item'] if origin=='penjumlahan_item' else [inv]
    key='subtotal_item' if origin=='penjumlahan_item' else 'total_sebelum_pajak'
    components=[{'nomor_item':str(n) if origin=='penjumlahan_item' else '',
                 'nilai':it['nilai'].get(key,''),
                 'diagnosis':diagnosis_pembacaan(inv,it['kolom'].get(key,{}),it if origin=='penjumlahan_item' else None,key)}
                for n,it in enumerate(rows,1)]
    filled=bool(components) and all(c['nilai']!='' for c in components)
    expected=sum((Decimal(c['nilai']) for c in components),Decimal(0)) if filled else None
    ev=inv['kolom'].get('subtotal_faktur',{});initial=ev.get('bukti_label_awal',{})
    existence=('tidak_tercetak_menurut_observasi' if 'subtotal_faktur' in observation.get('tidak_tercetak',[]) else
               'label_awal_ada_pembacaan_belum_selesai' if initial.get('segmen') or initial.get('label') or initial.get('error_parsing') else 'belum_diketahui')
    return {'versi':VERSI,'asal':origin,'komponen_lengkap':filled,
            'jumlah_komponen':len(components),'komponen':components,
            'akumulasi_komponen':str(expected) if expected is not None else '',
            'cocok_nilai_turunan':filled and expected==Decimal(inv['nilai']['subtotal_faktur']),
            'keberadaan_subtotal_tercetak':existence}


def assess(inv,observation=None):
    """Pure assessment: group roots, retain dependencies and separate human status."""
    from validasi_ringkasan import siapkan_metadata, observasi_sah, pemeriksaan_ringkasan, pemeriksaan_subtotal, pemeriksaan_satuan_pajak, pemeriksaan_satuan_diskon
    if observation and observation.get('jenis')=='observasi_visual_AI' and not observasi_sah(inv,observation):
        raise ValueError('Observasi AI tidak cocok dengan hash/path sumber atau provenance belum lengkap')
    obs=observation or observasi_sah(inv);meta=siapkan_metadata(inv,obs)
    row=inv['nilai'];fid=row['id_faktur'];tasks={};infos=[];checks=[];availability={}
    checks.extend(pemeriksaan_satuan_pajak(inv))
    checks.extend(pemeriksaan_satuan_diskon(inv))
    def add(item,kind,root,cols,reason,impact='',ev=None,diagnosis=None):
        key=(str(item),kind,root)
        target=row if item=='' else inv['item'][int(item)-1]['nilai']
        q=tasks.setdefault(key,{'id_tugas':'REV_'+fingerprint([fid,*key])[:16],'id_faktur':fid,'nomor_invoice':row['nomor_invoice'],
            'nomor_item':str(item),'jenis':kind,'akar_masalah':root,'kolom':[],'nilai_saat_ini':{},'alasan':[],
            'dampak':[],'sumber':row['file_sumber'],'halaman':row['halaman_sumber'],'bukti':[], 'status':'perlu_review'})
        for col in cols:
            if col not in q['kolom']:q['kolom'].append(col);q['nilai_saat_ini'][col]=target.get(col,'')
        if reason not in q['alasan']:q['alasan'].append(reason)
        if impact and impact not in q['dampak']:q['dampak'].append(impact)
        if ev and ev not in q['bukti']:q['bukti'].append(ev)
        if diagnosis and diagnosis not in q.setdefault('diagnosis',[]):q['diagnosis'].append(diagnosis)
        return q
    def info(kind,message,evidence=None):
        infos.append({'id_faktur':fid,'jenis':kind,'pesan':message,'bukti':evidence,'sumber':row['file_sumber']})
    def missing(item,key,ev):
        state='tidak_tercetak' if item=='' and key in obs.get('tidak_tercetak',[]) else ('gagal_atau_ambigu' if raw(ev) or ev.get('error_parsing') else 'belum_diketahui')
        availability[f'{item or "faktur"}.{key}']=state
        return state
    required=['nomor_invoice','tanggal_invoice','nama_pelanggan','nama_penerbit']
    for k in required:
        ev=inv['kolom'].get(k,{})
        if not row.get(k) or any(s.get('skor') is not None and s['skor']<.85 for s in ev.get('segmen',[])):
            add('','identitas_transaksi',k,[k],'Identitas kosong atau pembacaan identitas berisiko; jangan menebak dari nama folder.',ev=ev)
    issuer=row['nama_penerbit']
    if inv.get('cakupan_dokumen',{}):
        add('','identitas_transaksi','cakupan_transaksi_final',['nomor_invoice'],
            inv['cakupan_dokumen']['alasan'],ev=inv['cakupan_dokumen'])
    if issuer and not re.search(r'(?i)\bzahir\b|\bakun?tansi bisnis solution\b',issuer):
        add('','identitas_transaksi','cakupan_penerbit',['nama_penerbit'],'Hubungan penerbit dengan penjualan Zahir belum jelas; tetap disertakan untuk review.',ev=inv['kolom'].get('nama_penerbit'))
    if not inv['item']:add('','struktur','item_hilang',[],'Tidak ada rincian barang.')
    if inv.get('catatan_layout'):add('','struktur','tabel_tanpa_header',[],'Header belum terbaca; peran kolom belum pasti.',ev=inv.get('bukti_struktur'))
    subtotal_origin=asal_subtotal(inv);tax_format=kenali_setelah_pajak(inv);discount_format=kenali_setelah_diskon(inv)
    for k,ev in inv['kolom'].items():
        if row.get(k,'')=='':missing('',k,ev)
        if k=='subtotal_faktur' and row.get(k,'')!='' and subtotal_origin in ('penjumlahan_item','salinan_total_sebelum_pajak'):
            original_label=ev.get('bukti_label_awal',{})
            if original_label.get('segmen') or original_label.get('label') or original_label.get('error_parsing'):
                add('','ekstraksi_angka','subtotal_faktur_tercetak_gagal',[k],'Bukti label subtotal awal ditemukan tetapi pembacaan gagal/ambigu. Nilai turunan tidak menggantikan angka tercetak yang belum terbaca.',ev=original_label)
            if subtotal_origin=='penjumlahan_item':
                components=inv['item'];valid=bool(components) and all(it['nilai']['subtotal_item']!='' and pembacaan_valid(inv,it['kolom']['subtotal_item']) and pemetaan_sel(inv,it,'subtotal_item') for it in components)
                expected=sum((Decimal(it['nilai']['subtotal_item']) for it in components),Decimal(0)) if valid else None
                valid=valid and expected==Decimal(row[k])
            else:valid=row.get('total_sebelum_pajak','')==row[k] and pembacaan_valid(inv,inv['kolom'].get('total_sebelum_pajak',{}))
            dt=diagnosis_turunan(inv,subtotal_origin,obs)
            info('asal_subtotal', 'Subtotal adalah '+subtotal_origin+'; bukan pembacaan subtotal tercetak. Dukungan komponen diperiksa terpisah.',
                 {'asal':subtotal_origin,'nilai':row[k],'komponen_didukung':valid,'diagnosis':dt,'bukti':ev})
            if not valid:
                reason=('Komponen numerik turunan belum lengkap' if not dt['komponen_lengkap'] else
                        'Akumulasi komponen tidak cocok dengan nilai turunan' if not dt['cocok_nilai_turunan'] else
                        'Komponen numerik lengkap dan akumulasi cocok, tetapi format/pembacaan atau pemetaan komponennya belum lolos pemeriksaan otomatis')
                add('','ekstraksi_angka','subtotal_faktur',['subtotal_faktur'],reason+'; bukan pembacaan subtotal tercetak.',ev=ev,diagnosis=dt)
            if k not in obs.get('tidak_tercetak',[]):
                add('','struktur','keberadaan_subtotal_tercetak',[k],'Nilai subtotal turunan tersedia, tetapi keberadaan subtotal tercetak belum diketahui; absennya OCR bukan bukti tidak tercetak.',ev=ev)
            continue
        if k in NUM_DOC and row.get(k,'')!='' and not pembacaan_valid(inv,ev):
            d=diagnosis_pembacaan(inv,ev)
            add('','ekstraksi_angka',k,[k],alasan_pembacaan(d),ev=ev,diagnosis=d)
        elif k in NUM_DOC and ev.get('error_parsing'):
            add('','ekstraksi_angka',k,[k],'Angka tercetak/terdeteksi tetapi parsing gagal; nilai tetap kosong.',ev=ev)
    required_total=['total_akhir','total_sebelum_pajak','pajak_faktur','diskon_final_nominal']
    if tax_format:required_total=['subtotal_faktur','total_setelah_pajak','pajak_faktur','total_akhir']+(['diskon_final_nominal','total_sebelum_pajak'] if tax_format['basis_pajak']=='total_sebelum_pajak' else [])+(['pph_tercetak'] if tax_format['memiliki_pph'] else [])
    elif discount_format:required_total=['subtotal_faktur','diskon_final_nominal','total_akhir']
    for k in required_total:
        if k=='total_sebelum_pajak' and row.get('dpp_tercetak'):continue
        if not row.get(k) and k not in obs.get('tidak_tercetak',[]):
            add('','ekstraksi_angka',k,[k],'Komponen ringkasan wajib pada format yang dipakai kosong/ambigu.',ev=inv['kolom'].get(k))
    subs_vals = [it['nilai']['subtotal_item'] for it in inv['item']]
    item_sum = sum((Decimal(x) for x in subs_vals), Decimal(0)) if subs_vals and all(x != '' for x in subs_vals) else None
    tsp_val = Decimal(row['total_sebelum_pajak']) if row.get('total_sebelum_pajak', '') != '' else None
    ta_val = Decimal(row['total_akhir']) if row.get('total_akhir', '') != '' else None
    subtotal_cocok = (item_sum is not None) and (
        (tsp_val is not None and abs(item_sum - tsp_val) <= Decimal('.01')) or
        (ta_val is not None and abs(item_sum - ta_val) <= Decimal('.01'))
    )
    if not row.get('subtotal_faktur') and 'subtotal_faktur' not in obs.get('tidak_tercetak', []):
        add('','ekstraksi_angka','subtotal_faktur',['subtotal_faktur'],'Keberadaan subtotal belum dipastikan; absennya label OCR atau kecocokan penjumlahan bukan bukti tidak tercetak.',ev=inv['kolom'].get('subtotal_faktur'))
    def calc(name,item,actual,keys,fn,root=None,kind='aritmetika'):
        target=row if item=='' else inv['item'][item-1]['nilai']; cols=inv['kolom'] if item=='' else inv['item'][item-1]['kolom']
        absent=[k for k in set(keys+[actual]) if target.get(k,'')=='']
        c=cek_hitung(name,Decimal(target[actual]) if target.get(actual,'')!='' else None,
                     fn(*[Decimal(target[k]) for k in keys]) if not absent else None)
        c.update(item=item,komponen=keys,kolom_aktual=actual);checks.append(c)
        if absent:
            known_absent=[k for k in absent if item=='' and k in obs.get('tidak_tercetak',[])]
            if known_absent:
                c.update(status='tidak_berlaku',alasan='Rumus ini memerlukan kolom yang menurut pemeriksaan sumber tidak tercetak; tidak diisi dari hitungan.',kolom_tidak_tercetak=known_absent)
                add(item,'struktur','format_total_kolom_tidak_tersedia',[],'Susunan total membutuhkan validasi format terpisah; rumus dengan kolom tidak tercetak tidak dipaksakan.',name,{'pemeriksaan':c,'observasi':obs})
                for k in absent:
                    if k not in known_absent:add(item,'ekstraksi_angka',k,[k],'Komponen numerik kosong/ambigu.',name,cols.get(k))
                return
            for k in absent:
                add(item,'ekstraksi_angka',k,[k],'Komponen numerik kosong/ambigu.',name,cols.get(k))
        elif c['status']=='selisih':
            add(item,kind,root or name,list(dict.fromkeys([actual]+keys)),f'{name}: selisih {c["selisih"]}, toleransi {c["toleransi"]}. Angka tetap dipertahankan.',name,c)
    for n,it in enumerate(inv['item'],1):
        ir=it['nilai'];cols=it['kolom']
        from diskon_item_bersatuan import nominal_item
        nominal=nominal_item(it)
        if ir['id_faktur']!=fid or ir['nomor_baris_item']!=str(n):add(n,'struktur','relasi_item',['id_faktur','nomor_baris_item'],'ID/urutan item tidak konsisten.')
        if not ir.get('nama_barang'):add(n,'struktur','nama_barang',['nama_barang'],'Nama item tidak terbentuk.',ev=cols.get('nama_barang'))
        for k in NUM_ITEM:
            ev=cols.get(k,{})
            if k=='diskon_item_persen' and nominal is not None:
                info('diskon_nominal_tercetak','Disc mencetak Rupiah; persen kosong bukan gagal ekstraksi. Nominal dan satuan ada pada bukti item.',{'item':n,'bukti':it['diskon_item_nominal_tercetak']})
                continue
            if ir.get(k,'')=='':missing(n,k,ev)
            if ir.get(k,'')!='' and (not pembacaan_valid(inv,ev) or not pemetaan_sel(inv,it,k)):
                d=diagnosis_pembacaan(inv,ev,it,k)
                add(n,'ekstraksi_angka',k,[k],alasan_pembacaan(d),ev=ev,diagnosis=d)
        unit=cols.get('pajak_item_tercetak',{}).get('metadata_satuan',{})
        if ir.get('pajak_item_tercetak','')!='':
            if unit.get('satuan')=='belum_dipastikan' and Decimal(ir['pajak_item_tercetak'])!=0:
                add(n,'satuan','satuan_pajak_item',['pajak_item_tercetak'],'Digit pajak terbaca tetapi header/sel tidak memastikan persen atau Rupiah; tidak menjumlah sebagai nominal.',ev=unit)
            else:info('satuan_pajak_item','Satuan pajak dari simbol/header; nol tanpa unit juga tidak membuktikan satuan. CSV tidak diubah.',{'item':n,**unit})
        de=cols['diskon_item_persen'];d=ir['diskon_item_persen']
        if d and not 0<=Decimal(d)<=100:
            add(n,'ekstraksi_angka','diskon_item_persen',['diskon_item_persen'],'Persentase diskon di luar rentang 0..100.',ev=de)
        if d and not diskon_eksplisit(de):
            if Decimal(d)==0:
                # Diskon nol: satuan (persen/nominal) tidak mempengaruhi subtotal sama sekali.
                # Catat sebagai informasi, bukan tugas wajib review satuan.
                info('interpretasi_diskon',
                     'Diskon nol; satuan (persen/nominal) tidak membedakan subtotal. Nilai dipertahankan sebagai informasi.',
                     {'item':n,'bukti':de})
            else:
                unit_check=next(c for c in checks if c.get('pemeriksaan')=='satuan diskon item' and c.get('item')==n)
                add(n,'satuan','satuan_diskon_item',['diskon_item_persen'],
                    'Satuan Disc nonnol belum eksplisit. Perhitungan berbasis persen tetap hipotesis; kecocokan aritmetika bukan bukti satuan tercetak.',ev=unit_check)
        if nominal is not None:
            calc('subtotal item dengan diskon nominal Rupiah',n,'subtotal_item',['jumlah','harga_satuan'],lambda q,p:q*p-nominal)
            if not pembacaan_valid(inv,it['diskon_item_nominal_tercetak']):
                add(n,'ekstraksi_angka','diskon_item_nominal_tercetak',[],'Bukti nominal diskon Rupiah belum lolos pembacaan sumber; tidak mengisi kolom persen.',ev=it['diskon_item_nominal_tercetak'])
        else:calc('subtotal item',n,'subtotal_item',['jumlah','harga_satuan','diskon_item_persen'],lambda q,p,d:q*p*(1-d/100))
    # Ringkasan hanya memakai rumus yang sesuai label/keberadaan kolom sumber.
    if row.get('subtotal_faktur') and subtotal_origin=='penjumlahan_item':
        checks.append({'pemeriksaan':'jumlah subtotal item = subtotal turunan','status':'tidak_berlaku','alasan':'Nilai turunan berasal dari komponen yang sama; bukan pembandingan angka tercetak yang independen.'})
    elif row.get('subtotal_faktur'):
        subs=[it['nilai']['subtotal_item'] for it in inv['item']]
        check=pemeriksaan_subtotal(inv,obs);checks.append(check)
        if check['status']=='cakupan_belum_dipastikan':add('','struktur','cakupan_subtotal',['subtotal_faktur'],'Cakupan subtotal lokal versus ringkasan kandidat lintas halaman belum dipastikan; hasil hitung disimpan, bukan kesalahan digit terkonfirmasi.',ev=check)
        if check['status']=='selisih':add('','aritmetika','jumlah_subtotal',['subtotal_faktur'],'Jumlah subtotal item berbeda dari subtotal faktur.',ev=check)
        elif check['status']=='tidak_dapat_diperiksa':
            for n,it in enumerate(inv['item'],1):
                if it['nilai']['subtotal_item']=='':add(n,'ekstraksi_angka','subtotal_item',['subtotal_item'],'Komponen numerik kosong/ambigu.','jumlah subtotal item',it['kolom']['subtotal_item'])
    elif subtotal_cocok and 'subtotal_faktur' in obs.get('tidak_tercetak',[]):
        target_ref = tsp_val if (tsp_val is not None and abs(item_sum - tsp_val) <= Decimal('.01')) else ta_val
        target_label = 'total sebelum pajak' if target_ref == tsp_val else 'total akhir'
        checks.append(cek_hitung(f'jumlah subtotal item = {target_label} (subtotal tidak tercetak)', target_ref, item_sum))
    else:checks.append({'pemeriksaan':'jumlah subtotal item = subtotal faktur','status':'tidak_berlaku' if 'subtotal_faktur' in obs.get('tidak_tercetak',[]) else 'tidak_dapat_diperiksa','alasan':'Subtotal tidak tercetak menurut inspeksi sumber' if 'subtotal_faktur' in obs.get('tidak_tercetak',[]) else 'Keberadaan subtotal belum diketahui','bukti':obs})
    from format_ringkasan import kenali_rekap_diskon_ppn_pph
    recap=kenali_rekap_diskon_ppn_pph(inv)
    if recap:
        calc('total = sebelum diskon - discount + PPN + PPh bertanda','','total_akhir',['total_sebelum_pajak','diskon_final_nominal','pajak_faktur','pph_tercetak'],lambda t,d,p,h:t-d+p+h)
        info('format_ringkasan','Subtotal item sudah setelah diskon. Discount footer tidak dikurangi ulang dari item; basis label sebelum diskon dan PPh bertanda dipertahankan.',recap)
    elif row.get('dpp_tercetak') and row.get('total_setelah_pajak'):
        calc('DPP = subtotal','','dpp_tercetak',['subtotal_faktur'],lambda x:x)
        calc('DPP lain = DPP * 11/12','','dpp_lain_tercetak',['dpp_tercetak'],lambda x:x*11/12,'rantai_dpp_ppn')
        calc('PPN = DPP lain * persen','','pajak_faktur',['dpp_lain_tercetak','ppn_persen_tercetak'],lambda d,p:d*p/100,'rantai_dpp_ppn')
        calc('setelah pajak = DPP + PPN','','total_setelah_pajak',['dpp_tercetak','pajak_faktur'],lambda d,p:d+p)
        calc('Grand Total = setelah pajak + PPH','','total_akhir',['total_setelah_pajak','pph_tercetak'],lambda t,p:t+p)
    elif tax_format:
        if tax_format['basis_pajak']=='total_sebelum_pajak':
            calc('sebelum pajak = subtotal - discount','','total_sebelum_pajak',['subtotal_faktur','diskon_final_nominal'],lambda s,d:s-d)
        calc('setelah pajak = basis tercetak + pajak','','total_setelah_pajak',[tax_format['basis_pajak'],'pajak_faktur'],lambda b,p:b+p)
        if tax_format['memiliki_pph']:
            calc('total akhir = setelah pajak - PPh pemotongan','','total_akhir',['total_setelah_pajak','pph_tercetak'],lambda t,p:t-p)
        else:calc('total akhir = setelah pajak','','total_akhir',['total_setelah_pajak'],lambda t:t)
        info('format_ringkasan','Rumus mengikuti label Total Setelah Pajak/PPN dan PPh; diskon tidak dikurangi dua kali.',tax_format)
    elif discount_format:
        calc('total setelah diskon = sebelum diskon - diskon akhir','','total_akhir',['subtotal_faktur','diskon_final_nominal'],lambda s,d:s-d)
        info('format_ringkasan','Total Setelah Diskon adalah total faktur; Total Terbayar/Terhutang bukan total transaksi tambahan.',discount_format)
    elif inv.get('format_total')=='diskon_sebelum_pajak_dengan_total_setelah_pajak':
        calc('sebelum pajak = subtotal - discount','','total_sebelum_pajak',['subtotal_faktur','diskon_final_nominal'],lambda s,d:s-d)
        calc('setelah pajak = sebelum + pajak','','total_setelah_pajak',['total_sebelum_pajak','pajak_faktur'],lambda t,p:t+p,'ringkasan_setelah_pajak')
        calc('total = setelah pajak','','total_akhir',['total_setelah_pajak'],lambda t:t,'ringkasan_setelah_pajak')
        calc('total = sebelum pajak + pajak','','total_akhir',['total_sebelum_pajak','pajak_faktur'],lambda t,p:t+p)
    else:
        common=pemeriksaan_ringkasan(inv,obs)
        checks.extend(common)
        info('format_ringkasan','Basis dipilih dari label/area/urutan sebelum hitungan; gap dan Harga@ bukan penentu. Kasus ambigu tetap ditahan.',meta['basis_diskon'])
        for c in common:
            actual=c.get('kolom_aktual');keys=c.get('komponen',[])
            if c['status']=='cakupan_belum_dipastikan':
                add('','struktur','cakupan_subtotal',['subtotal_faktur'],'Cakupan subtotal lokal/ringkasan belum setara; simpan selisih tanpa menyatakannya salah digit.',ev=c)
            elif c.get('akar_masalah')=='basis_diskon_belum_dipastikan':
                absent=[k for k in ('total_sebelum_pajak','diskon_final_nominal','pajak_faktur','total_akhir') if k in obs.get('tidak_tercetak',[])]
                root='format_ringkasan_kolom_tidak_tersedia' if absent else 'basis_diskon_belum_dipastikan'
                conflict=' Kedua hipotesis basis menghasilkan selisih terhadap angka tercetak; konflik tetap wajib ditinjau.' if c.get('konflik_di_kedua_basis') else ''
                add('','struktur',root,[],c['alasan']+conflict+' Pemeriksaan item dan komponen lain tetap dijalankan.',ev=c)
            elif c['status']=='selisih':
                add('','aritmetika',c['pemeriksaan'],list(dict.fromkeys([actual]+keys)),
                    f"{c['pemeriksaan']}: selisih {c['selisih']}, toleransi {c['toleransi']}. Angka cetak dipertahankan.",ev=c)
            elif c['status']=='tidak_dapat_diperiksa':
                if c.get('akar_masalah'):
                    add('','struktur',c['akar_masalah'],[],c['alasan'],ev=c)
                for k in c.get('komponen_kosong',[]):
                    if k not in obs.get('tidak_tercetak',[]):add('','ekstraksi_angka',k,[k],'Komponen numerik kosong/ambigu.',c['pemeriksaan'],inv['kolom'].get(k))
            if c.get('diagnosis_pembulatan_persen_tampilan'):
                info('pembulatan_persen_tampilan','Diagnosis tambahan; tidak mengubah selisih, toleransi, nominal atau status pemeriksaan.',c)
    if obs.get('konflik_terbilang_AI'):
        add('','struktur','konflik_terbilang',['total_akhir'],'Observasi visual AI: terbilang berbeda dari Total tercetak; tidak mengubah nominal dan bukan konfirmasi manusia.',ev=obs['konflik_terbilang_AI'])
    fallback = identitas_invoice_fallback(inv)
    if fallback:
        add('','identitas_transaksi','nomor_invoice_fallback',['nomor_invoice'],'Nomor berasal dari label Nomor SJ; belum dipastikan sebagai nomor invoice dan tidak boleh menjadi dasar pengecualian duplikat otomatis.',ev=fallback)
    for n in inv.get('keterangan_tabel',[]):
        if n.get('risiko_item_hilang'):add('','struktur','kemungkinan_item_hilang',[],'Ada baris dengan angka yang belum dipetakan menjadi item.',ev=n)
    notes=inv.get('keterangan_tabel',[])
    if notes:info('informasi_tambahan','Nomor seri/uraian/termin dan kandidat hubungan tetap belum dipastikan; tidak menghalangi angka kecuali berisiko item hilang.',notes)
    for state in ['tidak_tercetak','belum_diketahui','gagal_atau_ambigu']:
        cols=[k for k,v in availability.items() if v==state]
        if cols:info(state,'Status keberadaan kolom kosong; lihat tugas wajib untuk field kritis. Kosong bukan nol.',{'kolom':cols,'observasi':obs if state=='tidak_tercetak' else None})
    rois=[{'item':n,'kolom':k,'bukti':ev['pembacaan_ulang']} for n,obj in [('',inv)]+[(str(n),it) for n,it in enumerate(inv['item'],1)] for k,ev in obj['kolom'].items() if ev.get('pembacaan_ulang')]
    if rois:info('asal_ocr_ulang','Asal OCR ulang bukan penghalang sendiri; pembacaan, pemetaan dan pemeriksaan aritmetika dinilai terpisah.',rois)
    if inv.get('review_skor',{}).get('segmen'):info('skor_tambahan','Skor rendah yang tidak mengenai field kritis adalah informasi, bukan kegagalan seluruh faktur.',inv['review_skor'])
    # Lengkapi lokasi evidence untuk tugas aritmetika serta tautan sel yang menjadi dampak.
    for task in tasks.values():
        target=inv if task['nomor_item']=='' else inv['item'][int(task['nomor_item'])-1]
        task['bukti_kolom']={k:target['kolom'].get(k,{}) for k in task['kolom']}
    for t in tasks.values():
        key=t['akar_masalah'];n=t['nomor_item']
        if t['jenis']=='ekstraksi_angka' and key in (NUM_ITEM if n else NUM_DOC) and not t.get('diagnosis'):
            it=inv['item'][int(n)-1] if n else None
            ev=(it or inv)['kolom'].get(key,{})
            d=diagnosis_pembacaan(inv,ev,it,key)
            t['diagnosis']=[d]
            if raw(ev) and (it or inv)['nilai'].get(key,'')=='':
                t['alasan'].append(alasan_pembacaan(d)+' Sel tetap kosong, bukan nol.')
    from validasi_ringkasan import tandai_hipotesis_diskon
    tandai_hipotesis_diskon(checks,inv)
    return {'versi_penilai':VERSI,'tugas':list(tasks.values()),'informasi':infos,'pemeriksaan':checks,'keberadaan_kolom':availability,
            'status_otomatis':'perlu_review' if tasks else 'lolos_otomatis','status_manusia':'belum_diverifikasi'}


def run(input_path,observations_path,output_root,preserve_duplicates=False):
    input_path=Path(input_path).resolve();old=read_json(input_path);observations=read_json(observations_path)
    if not complete(input_path.parent):raise ValueError('Checksum ekspor input gagal')
    before=digest(input_path);main=[];excluded=[];tasks=[];infos=[];statuses=[]
    roi_records=[]
    if observations.get('hasil_angka'):
        rp=Path(observations['hasil_angka'])
        if not complete(rp.parent):raise ValueError('Checksum ROI tambahan gagal')
        roi_records=read_json(rp)['hasil']
    for orig in old['faktur']:
        inv=copy.deepcopy(orig);obs=observations['dokumen'].get(inv['hash_sumber'],{})
        if digest(inv['nilai']['file_sumber'])!=inv['hash_sumber']:raise ValueError('Hash sumber berubah')
        if obs and (obs['sumber']!=inv['nilai']['file_sumber']):raise ValueError('Observasi tidak cocok dengan sumber')
        if obs.get('cakupan')=='penjualan_milik_pelanggan':
            if not obs.get('bukti_visual') or not obs.get('penerbit_terlihat'):raise ValueError('Pengecualian tanpa bukti penerbit ditolak')
            excluded.append({'ekstraksi_asli':orig,'keputusan':obs,'status_manusia':'belum_diverifikasi'});continue
        a=assess(inv,obs)
        for t in a['tugas']:
            t['potongan_gambar']=[];t['bukti_potongan']=[]
            for r in roi_records:
                if r['hash_sumber']==inv['hash_sumber'] and r['kolom'] in t['kolom'] and str(r['item'] or 'faktur')==(t['nomor_item'] or 'faktur'):
                    t['potongan_gambar'].append(r['crop_asli']);t['bukti_potongan'].append(r)
        inv['penilaian_review']=a;tasks.extend(a['tugas']);infos.extend(a['informasi'])
        inv['riwayat_penilaian_asal']={k:inv.pop(k) for k in ['status_batch','validasi','validasi_batch'] if k in inv}
        inv['status_batch']=a['status_otomatis'];inv['status_manusia']=a['status_manusia'];inv['validasi']=a['pemeriksaan']
        inv['status_sebelumnya']={'faktur':{k:inv['nilai'][k] for k in MUTABLE},'item':[{k:it['nilai'][k] for k in MUTABLE} for it in inv['item']]}
        for n,obj in [('',inv)]+[(str(n),it) for n,it in enumerate(inv['item'],1)]:
            if n:
                obj['riwayat_penilaian_asal']={k:obj.pop(k) for k in ['masalah','validasi'] if k in obj}
                obj['validasi']=[c for c in a['pemeriksaan'] if str(c.get('item',''))==n]
            relevant=a['tugas'] if n=='' else [t for t in a['tugas'] if t['nomor_item']==n or
                t['jenis']=='struktur' and not t['nomor_item'] and t['akar_masalah'] in
                ('tabel_tanpa_header','kemungkinan_item_hilang','item_hilang')]
            state='perlu_review' if relevant else 'lolos_otomatis'
            obj['nilai']['status_validasi']=state;obj['nilai']['catatan_validasi']='; '.join(t['id_tugas']+': '+' / '.join(t['alasan']) for t in relevant) or 'Pemeriksaan otomatis relevan lolos; status manusia terpisah.'
            for k in MUTABLE:obj['kolom'][k]={'nilai':obj['nilai'][k],'metode':'Penilaian kebijakan review, bukan nilai tercetak'}
            statuses.append({'id_faktur':inv['nilai']['id_faktur'],'nomor_item':n,'status_otomatis':state,'status_manusia':'belum_diverifikasi'})
        main.append(inv)
    duplicates = duplicate_candidates(main)
    discard_id_set = set()
    dup_map = {}

    def doc_signature(inv):
        items = tuple(
            (it['nilai'].get('nama_barang', '').strip().casefold(),
             it['nilai'].get('jumlah', '').strip(),
             it['nilai'].get('harga_satuan', '').strip(),
             it['nilai'].get('subtotal_item', '').strip())
            for it in inv.get('item', [])
        )
        subtotal = inv['nilai'].get('subtotal_faktur', '').strip()
        total = inv['nilai'].get('total_akhir', '').strip()
        return (items, subtotal, total)

    doc_by_id = {i['nilai']['id_faktur']: i for i in main}
    for dup in duplicates:
        if preserve_duplicates:
            for fid in dup['id_faktur']:
                infos.append({'id_catatan':'','id_faktur':fid,'jenis':'kandidat_duplikat_dipertahankan',
                              'pesan':'Nomor/penerbit serupa; hubungan transaksi/halaman belum dipastikan. Opsi tanpa Keep First mempertahankan semua record.',
                              'sumber':doc_by_id[fid]['nilai']['file_sumber'],'lokasi_bukti':''})
        if any(identitas_invoice_fallback(doc_by_id[fid]) for fid in dup['id_faktur']):
            for fid in dup['id_faktur']:
                infos.append({'id_catatan':'','id_faktur':fid,'jenis':'duplikat_identitas_fallback',
                              'pesan':'Kesamaan penerbit/nomor termasuk fallback Nomor SJ; semua transaksi dipertahankan untuk review, tanpa Keep First otomatis.',
                              'sumber':doc_by_id[fid]['nilai']['file_sumber'],'lokasi_bukti':''})
            continue
        if len({Path(doc_by_id[fid]['nilai']['file_sumber']).suffix.casefold() for fid in dup['id_faktur']}) > 1:
            # Signature item yang sama pada format berbeda bukan verifikasi
            # salinan transaksi. Pertahankan kedua sumber, tanpa Keep First.
            evidence=[{'id_faktur':fid,'hash_sumber':doc_by_id[fid].get('hash_sumber'),
                       'file_sumber':doc_by_id[fid]['nilai']['file_sumber'],
                       'halaman':doc_by_id[fid]['nilai']['halaman_sumber'],
                       'identitas':{k:doc_by_id[fid]['nilai'][k] for k in ('nama_penerbit','nomor_invoice','tanggal_invoice','nama_pelanggan','total_akhir')},
                       'item':[it['nilai'] for it in doc_by_id[fid]['item']]} for fid in dup['id_faktur']]
            for fid in dup['id_faktur']:
                inv=doc_by_id[fid];row=inv['nilai']
                message='Kandidat salinan transaksi lintas format; cocokkan sumber/halaman dan rincian sebelum menghitung transaksi unik. Tidak dikeluarkan otomatis.'
                task={'id_tugas':'REV_'+fingerprint([fid,'','identitas_transaksi','duplikat_lintas_format'])[:16],
                      'id_faktur':fid,'nomor_invoice':row['nomor_invoice'],'nomor_item':'',
                      'jenis':'identitas_transaksi','akar_masalah':'duplikat_lintas_format','kolom':['nomor_invoice'],
                      'nilai_saat_ini':{'nomor_invoice':row['nomor_invoice']},'alasan':[message],
                      'dampak':['jumlah transaksi unik belum dipastikan'],'sumber':row['file_sumber'],
                      'halaman':row['halaman_sumber'],'bukti':evidence,'status':'perlu_review',
                      'potongan_gambar':[],'bukti_potongan':[]}
                tasks.append(task);inv['penilaian_review']['tugas'].append(task)
                inv['penilaian_review']['status_otomatis']='perlu_review';inv['status_batch']='perlu_review'
                row['status_validasi']='perlu_review';row['catatan_validasi']+='; '+task['id_tugas']+': '+message
                for k in MUTABLE:inv['kolom'][k]={'nilai':row[k],'metode':'Penilaian kebijakan review, bukan nilai tercetak'}
                for s in statuses:
                    if s['id_faktur']==fid and s['nomor_item']=='':s['status_otomatis']='perlu_review'
                infos.append({'id_catatan':'','id_faktur':fid,'jenis':'kandidat_duplikat_lintas_format',
                              'pesan':message,'sumber':row['file_sumber'],'lokasi_bukti':''})
            continue
        kept_signatures = []
        for fid in dup['id_faktur']:
            inv = doc_by_id[fid]
            sig = doc_signature(inv)
            matching_primary = next((k_id for k_id, k_sig in kept_signatures if k_sig == sig), None)
            if matching_primary is not None:
                primary=doc_by_id[matching_primary]
                if primary['hash_sumber']!=inv['hash_sumber']:
                    evidence=[{'id_faktur':x['nilai']['id_faktur'],'hash_sumber':x['hash_sumber'],
                               'file_sumber':x['nilai']['file_sumber'],'halaman':x['nilai']['halaman_sumber'],
                               'identitas':{k:x['nilai'][k] for k in ['nomor_invoice','tanggal_invoice','nama_penerbit','nama_pelanggan','total_akhir']},
                               'item':[it['nilai'] for it in x['item']], 'keterangan':x.get('keterangan_tabel',[])} for x in (primary,inv)]
                    message='Nomor/penerbit dan signature item sama, tetapi hash dokumen berbeda; revisi, footer atau keterangan dapat berbeda. Hubungan transaksi belum dipastikan; tidak Keep First otomatis.'
                    for x in (primary,inv):
                        row=x['nilai'];current_id=row['id_faktur'];tid='REV_'+fingerprint([current_id,'','identitas_transaksi','duplikat_signature_belum_dipastikan'])[:16]
                        existing=next((t for t in tasks if t['id_tugas']==tid),None)
                        if existing:
                            existing['bukti'].extend(e for e in evidence if e not in existing['bukti']);continue
                        task={'id_tugas':tid,'id_faktur':current_id,'nomor_invoice':row['nomor_invoice'],'nomor_item':'',
                              'jenis':'identitas_transaksi','akar_masalah':'duplikat_signature_belum_dipastikan','kolom':['nomor_invoice'],
                              'nilai_saat_ini':{'nomor_invoice':row['nomor_invoice']},'alasan':[message],
                              'dampak':['jumlah transaksi unik belum dipastikan'],'sumber':row['file_sumber'],'halaman':row['halaman_sumber'],
                              'bukti':copy.deepcopy(evidence),'status':'perlu_review','potongan_gambar':[],'bukti_potongan':[]}
                        tasks.append(task);x['penilaian_review']['tugas'].append(task)
                        x['penilaian_review']['status_otomatis']='perlu_review';x['status_batch']='perlu_review'
                        row['status_validasi']='perlu_review';row['catatan_validasi']+='; '+tid+': '+message
                        for k in MUTABLE:x['kolom'][k]={'nilai':row[k],'metode':'Penilaian kebijakan review, bukan nilai tercetak'}
                        for s in statuses:
                            if s['id_faktur']==current_id and s['nomor_item']=='':s['status_otomatis']='perlu_review'
                    kept_signatures.append((fid,sig))
                    continue
                if not preserve_duplicates:
                    discard_id_set.add(fid)
                    dup_map[fid] = (dup, matching_primary)
            else:
                if kept_signatures:
                    first_fid = kept_signatures[0][0]
                    first_inv = doc_by_id[first_fid]
                    infos.append({
                        'id_catatan': '',
                        'id_faktur': fid,
                        'jenis': 'halaman_sambungan_faktur',
                        'pesan': f"Dokumen {fid} dipertahankan: rincian berbeda dari {first_fid}. Kemungkinan halaman sambungan/revisi belum dipastikan; nomor sama tidak membuktikan hubungan.",
                        'sumber': inv['nilai']['file_sumber'],
                        'lokasi_bukti': ''
                    })
                    infos.append({
                        'id_catatan': '',
                        'id_faktur': first_fid,
                        'jenis': 'halaman_sambungan_faktur',
                        'pesan': f"Dokumen {fid} memiliki rincian berbeda. Kandidat sambungan/revisi belum dipastikan; keduanya dipertahankan tanpa penggabungan transaksi.",
                        'sumber': first_inv['nilai']['file_sumber'],
                        'lokasi_bukti': ''
                    })
                kept_signatures.append((fid, sig))
    if discard_id_set:
        original = {i['nilai']['id_faktur']: i for i in old['faktur']}
        new_main = []
        for inv in main:
            fid = inv['nilai']['id_faktur']
            if fid in discard_id_set:
                dup, primary_id = dup_map[fid]
                orig_doc = original.get(fid, inv)
                excluded.append({
                    'ekstraksi_asli': orig_doc,
                    'keputusan': {
                        'cakupan': 'duplikat_faktur_penjualan',
                        'alasan': f"Dokumen duplikat identik untuk penerbit dan nomor invoice {dup['penerbit_nomor']}. Kemunculan pertama ({primary_id}) dipertahankan (Keep First).",
                        'id_faktur_utama': primary_id,
                        'id_faktur_duplikat': fid,
                        'detail_duplikat': dup
                    },
                    'status_manusia': 'belum_diverifikasi'
                })
            else:
                new_main.append(inv)
        main = new_main
        tasks = [t for t in tasks if t['id_faktur'] not in discard_id_set]
        statuses = [s for s in statuses if s['id_faktur'] not in discard_id_set]
        infos = [i for i in infos if i.get('id_faktur') not in discard_id_set]
        for fid, (dup, primary_id) in dup_map.items():
            first_inv = next((i for i in main if i['nilai']['id_faktur'] == primary_id), None)
            sumber_ref = first_inv['nilai']['file_sumber'] if first_inv else ''
            infos.append({
                'id_catatan': '',
                'id_faktur': primary_id,
                'jenis': 'duplikat_dikecualikan',
                'pesan': f"Dokumen duplikat {fid} ({dup['penerbit_nomor']}) dikecualikan dari ekspor dengan kebijakan Keep First.",
                'sumber': sumber_ref,
                'lokasi_bukti': ''
            })
    # Tidak satu nilai isi/angka/ID boleh berubah, juga pada item yang tetap kosong.
    original={i['nilai']['id_faktur']:i for i in old['faktur']}
    for inv in main:
        o=original[inv['nilai']['id_faktur']]
        assert len(o['item'])==len(inv['item'])
        for a,b in [(o,inv)]+list(zip(o['item'],inv['item'])):
            assert {k:v for k,v in a['nilai'].items() if k not in MUTABLE}=={k:v for k,v in b['nilai'].items() if k not in MUTABLE}
    out=Path(output_root).resolve()/uuid4().hex[:12];out.mkdir(parents=True,exist_ok=False)
    csv_write(out/'faktur.csv',[i['nilai'] for i in main],FAKTUR);csv_write(out/'detail_faktur.csv',[t['nilai'] for i in main for t in i['item']],DETAIL)
    for t in tasks:t['lokasi_bukti']=str(out/'bukti_ekstraksi.json')+'#tugas/'+t['id_tugas'];t['bukti_asal']=str(input_path)+'#'+t['id_faktur']
    for t in tasks:t.setdefault('diagnosis',[])
    cols=['id_tugas','id_faktur','nomor_invoice','nomor_item','jenis','akar_masalah','kolom','nilai_saat_ini','alasan','diagnosis','dampak','sumber','halaman','potongan_gambar','lokasi_bukti','bukti_asal']
    csv_write(out/'daftar_review.csv',[{k:json.dumps(t[k],ensure_ascii=False) if isinstance(t[k],(list,dict)) else t[k] for k in cols} for t in tasks],cols)
    for n,i in enumerate(infos,1):
        i['id_catatan']=f'INFO_{n:03d}';i['lokasi_bukti']=str(out/'bukti_ekstraksi.json')+'#informasi/'+i['id_catatan']
    info_cols=['id_catatan','id_faktur','jenis','pesan','sumber','lokasi_bukti']
    csv_write(out/'catatan_informatif.csv',[{k:i[k] for k in info_cols} for i in infos],info_cols)
    csv_write(out/'status_pemeriksaan.csv',statuses,['id_faktur','nomor_item','status_otomatis','status_manusia'])
    atomic_json(out/'dikecualikan.json',excluded)
    atomic_json(out/'bukti_ekstraksi.json',{'versi_penilai':VERSI,'faktur':main,'tugas':tasks,'informasi':infos,'sumber_bukti':str(input_path),'sha256_input':before,'observasi':observations,'sha256_observasi':digest(observations_path),'sha256_skrip':digest(__file__),'peringatan_lama':old['review'],
        'jejak_lama':'Peringatan lama utuh; identitas id_faktur dan nomor_item menghubungkan dengan penilaian baru dan pengecualian. Tidak menghapus bukti.'})
    summary={'versi_penilai':VERSI,'faktur':len(main),'item':sum(len(i['item']) for i in main),'status_faktur':dict(Counter(i['nilai']['status_validasi'] for i in main)),
        'status_item':dict(Counter(it['nilai']['status_validasi'] for i in main for it in i['item'])), 'tugas_unik':len(tasks),'catatan_informatif':len(infos),'peringatan_lama':len(old['review']),
        'faktur_dikecualikan':len(excluded),'item_dikecualikan':sum(len(x['ekstraksi_asli']['item']) for x in excluded),'nilai_isi_tidak_berubah':True,'status_manusia':'belum_diverifikasi','Keep_First_diizinkan':not preserve_duplicates}
    assert digest(input_path)==before
    atomic_json(out/'ringkasan.json',summary)
    lines=['# Penilaian dataset penjualan Zahir','',json.dumps(summary,ensure_ascii=False,indent=2),'',
        'Pengecualian yang benar-benar diterapkan tercatat di dikecualikan.json beserta ekstraksi asli dan alasan. Pengecualian cakupan memerlukan observasi pihak pada sumber; tidak ditentukan dari folder/produk. Hash berbeda dengan signature sama tetap kandidat, bukan duplikat pasti. Penerbit belum jelas tetap meminta review.',
        '', 'Aturan: angka/struktur dan identitas kritis meminta review; selisih aritmetika merupakan jenis terpisah dan tidak mengubah angka. Satu akar masalah menjadi satu tugas dengan beberapa dampak. Item hanya mengikuti masalahnya sendiri, bukan seluruh masalah faktur.',
        'OCR ulang harus lolos bukti raw/checksum/hash, format, pemetaan sel dan pemeriksaan relevan. Inferensi persen memerlukan label Disc dalam slot kolom, nilai valid, pemetaan vertikal unik, rentang persen serta penolakan alternatif nominal; bukan kecocokan hitungan saja. Inferensi tetap dicatat sebagai informasi, bukan verifikasi manusia.',
        'Kolom tidak tercetak hanya dinyatakan berdasarkan inspeksi sumber terpisah. Kolom OCR tidak ditemukan tetap belum_diketahui. Pemeriksaan yang mensyaratkan kolom tidak tercetak ditandai tidak_berlaku; kosong kritis/ambigu tetap perlu_review. Nilai kosong tidak dianggap nol.',
        'Keterangan/seri/typo nama tidak menghalangi angka kecuali ada risiko kehilangan item/identitas transaksi. Status manusia selalu belum_diverifikasi; status otomatis tidak menyatakan verifikasi manusia.',
        '', '## Hasil per faktur','']
    lines += [f'- {i["nilai"]["nomor_invoice"] or "(Nomor SJ; invoice kosong)"}: {len(i["item"])} item; {i["nilai"]["status_validasi"]}; {len(i["penilaian_review"]["tugas"])} tugas.' for i in main]
    lines += ['','## Tugas wajib','']+[f'- {t["id_tugas"]}: {t["nomor_invoice"]}, item {t["nomor_item"] or "faktur"}, {", ".join(t["kolom"])}. {"; ".join(t["alasan"])} Dampak: {", ".join(t["dampak"])}. Sumber: {t["sumber"]}.' for t in tasks]
    lines += ['','Nilai isi dan ID sama persis dengan input. Sumber/input tidak ditimpa. Tidak ada OCR, model atau paket baru. Pilot ini bukan ukuran akurasi seluruh arsip.']
    (out/'laporan.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');seal(out)
    print(out);print(json.dumps(summary,ensure_ascii=False));return out


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--observasi',type=Path,required=True);p.add_argument('--output-dir',type=Path,default=Path('outputs/penilaian_review'))
    p.add_argument('--tanpa-keep-first',action='store_true',help='Pertahankan kandidat salinan/halaman; jangan menghapus otomatis')
    a=p.parse_args();run(a.input,a.observasi,a.output_dir,a.tanpa_keep_first)
