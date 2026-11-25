"""Konteks format uang lokal; tidak membaca acuan, nama sumber atau rumus."""
import re
from decimal import Decimal
from konteks_angka_total import locale_total,segmen,terbilang_lengkap,SKALA

VERSI_KONTEKS_ITEM='konteks-uang-item-v2-kolom'

def cy(s):return (s['bbox'][1]+s['bbox'][3])/2


def pasangan(label,source,width):
    b=label['bbox'];h=b[3]-b[1]
    return [s for s in source if s is not label and s['bbox'][0]>=b[2] and
            abs(cy(s)-cy(label))<=.5*max(h,s['bbox'][3]-s['bbox'][1]) and
            re.fullmatch(r'-?[\d.,]+',s['teks_asli'].strip())]


def total_integer(source,width):
    labels=[s for s in source if s['bbox'][0]>=width/2 and re.fullmatch(r'(?i)total\s*:',s['teks_asli'].strip())]
    if len(labels)!=1:return None
    values=pasangan(labels[0],source,width)
    if len(values)!=1:return None
    return locale_total(values[0]['teks_asli'],[labels[0],values[0]],source,width)


def locale_kolom_integer(source,width,segments,key):
    """Formatter integer dibuktikan kolom itu sendiri, bukan skala footer.

    API lama tetap; Total/terbilang tidak menentukan locale item. Kasus .000
    saja tanpa anchor literal satu kolom ditahan, walau footer tampak cocok.
    """
    if key not in ('harga_satuan','subtotal_item'):return None
    numeric=[s for s in segments if re.fullmatch(r'-?[\d.,]+',s['teks_asli'].strip())]
    if len(numeric)<3 or len({s['teks_asli'] for s in numeric})<2:return None
    if len({cy(s) for s in numeric})<3:return None
    if not all(re.fullmatch(r'-?\d{1,3}(?:\.\d{3})+',s['teks_asli'].strip()) for s in numeric):return None
    local=[s for s in numeric if re.fullmatch(r'-?\d{1,3}(?:\.\d{3}){2,}',s['teks_asli'].strip())
           and s.get('skor_keyakinan') is not None and s['skor_keyakinan']>=.85]
    if len(local)<2 or len({s['teks_asli'] for s in local})<2:return None
    return {'locale':'id_ID','kolom':key,'wajib_review':True,
        'versi':VERSI_KONTEKS_ITEM,
        'aturan':'Kolom uang dengan >=3 baris bertitik; >=2 literal berbeda mempunyai sedikitnya dua grup ribuan di kolom yang sama. Footer/jumlah/persen tidak menentukan locale atau nominal.',
        'anchor_kolom':[segmen(s) for s in local]}


def ringkasan_integer(raw,segments,source,width):
    """Grup campuran tepat tiga digit; dua label uang lain membuktikan konvensi."""
    if not re.fullmatch(r'-?\d{1,3}(?:[.,]\d{3}){2,}',raw.strip()) or not ('.' in raw and ',' in raw):return None
    labels=[s for s in segments if re.fullmatch(r'(?i)(?:discount\s+final|diskon\s+final|total\s+sebelum\s+pajak)\s*:?',s['teks_asli'].strip())]
    values=[s for s in segments if s['teks_asli'].strip()==raw.strip()]
    if len(labels)!=1 or len(values)!=1 or len(segments)!=2:return None
    if values!=pasangan(labels[0],source,width):return None
    context=total_integer(source,width)
    if not context:return None
    loc=context['locale'];sep=',' if loc=='en_US' else '.'
    anchors=[]
    for label in source:
        if label is labels[0] or label['bbox'][0]<width/2:continue
        if not re.fullmatch(r'(?i)(?:total|total\s+sebelum\s+pajak|discount\s+final|diskon\s+final)\s*:?',label['teks_asli'].strip()):continue
        if abs(cy(label)-cy(labels[0]))>8*(labels[0]['bbox'][3]-labels[0]['bbox'][1]):continue
        vv=pasangan(label,source,width)
        if len(vv)==1 and re.fullmatch(r'-?\d{1,3}(?:'+re.escape(sep)+r'\d{3})+',vv[0]['teks_asli'].strip()):
            anchors.append({'label':segmen(label),'nilai_format':segmen(vv[0])})
    if len(anchors)<2:return None
    canonical=''.join(re.split(r'[.,]',raw.strip()))
    return {'nilai':format(Decimal(canonical),'f'),'input_grammar':canonical,'locale':loc,'wajib_review':True,
        'aturan':'Grup campuran seluruhnya tepat tiga digit; dua label ringkasan uang lain memakai integer homogen, Total didukung skala terbilang. Normalisasi konteks lokal, bukan literal standar atau nominal dari field lain.',
        'anchor_format':anchors,'bukti_Total_independen':context,'teks_asli':raw}


def grand_nonstandar(raw,segments,source,width):
    """Grup3+desimal2; format area dan skala integer terbilang independen."""
    if not re.fullmatch(r'-?\d{1,3}(?:[.,]\d{3})+[.,]\d{2}',raw.strip()):return None
    labels=[s for s in segments if re.fullmatch(r'(?i)grand\s+total\s*:?',s['teks_asli'].strip())]
    values=[s for s in segments if s['teks_asli'].strip()==raw.strip()]
    if len(labels)!=1 or len(values)!=1 or len(segments)!=2 or values!=pasangan(labels[0],source,width):return None
    value=values[0];b=value['bbox'];h=b[3]-b[1]
    nearby=[s for s in source if s['bbox'][0]>=width/2 and b[3]<=s['bbox'][1]<=b[3]+6*h]
    words=[];ws=[]
    from konteks_angka_total import KATA
    for s in sorted(nearby,key=lambda s:(cy(s),s['bbox'][0])):
        parts=s['teks_asli'].casefold().split()
        if parts and all(w in KATA or w=='koma' for w in parts):words.extend(parts);ws.append(s)
    if 'rupiah' not in words:return None
    integer_words=words[:words.index('rupiah')+1]
    if not terbilang_lengkap(integer_words):return None
    scales=[SKALA[w] for w in integer_words if w in SKALA]
    if not scales:return None
    anchors=[s for s in source if s not in segments and s['bbox'][0]>=b[0]-.5*h and
        b[1]-12*h<=s['bbox'][1]<b[1] and re.fullmatch(r'-?(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}',s['teks_asli'].strip())]
    if len(anchors)<2:return None
    integer,fraction=raw.strip()[:-3],raw.strip()[-2:]
    canonical=''.join(re.split(r'[.,]',integer))+'.'+fraction
    number=Decimal(canonical);low=Decimal(max(scales))
    if not low<=abs(number)<low*1000:return None
    if number<0 and 'minus' not in integer_words or number>=0 and 'minus' in integer_words:return None
    return {'nilai':format(number,'f'),'input_grammar':canonical,'locale':'en_US','wajib_review':True,
        'aturan':'Grand Total: setiap grup integer tepat tiga digit, akhir dua desimal; >=2 anchor format .dd pada area ringkasan dan skala integer terbilang. Pecahan kata yang tidak lengkap tidak dipakai menentukan digit. Interpretasi nonstandar perlu review.',
        'anchor_format':[segmen(s) for s in anchors],'terbilang_asli':[segmen(s) for s in ws],
        'batas_skala':[str(low),str(low*1000)],'teks_asli':raw}
