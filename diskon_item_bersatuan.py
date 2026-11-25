"""Validator satuan diskon eksplisit; nominal disimpan di bukti, skema CSV tetap."""
import re
from decimal import Decimal


def nominal_item(item):
    ev=item.get('diskon_item_nominal_tercetak')
    if not ev or ev.get('satuan_tercetak')!='rupiah' or ev.get('nilai','')=='':return None
    fragments=ev.get('fragmen',[])
    if not any(re.fullmatch(r'(?i)Rp\.?|IDR',f['teks']) for f in fragments):return None
    if any('%' in f['teks'] for f in fragments):return None
    return Decimal(ev['nilai'])


def harapan_item(item):
    row=item['nilai']
    q,p=[Decimal(row[k]) if row.get(k,'')!='' else None for k in ['jumlah','harga_satuan']]
    nominal=nominal_item(item)
    if nominal is not None:
        return (q*p-nominal if None not in (q,p) else None),'jumlah * harga - diskon nominal Rupiah = subtotal item'
    d=Decimal(row['diskon_item_persen']) if row.get('diskon_item_persen','')!='' else None
    return (q*p*(1-d/100) if None not in (q,p,d) and 0<=d<=100 else None),'jumlah * harga * (1 - diskon/100) = subtotal item'
