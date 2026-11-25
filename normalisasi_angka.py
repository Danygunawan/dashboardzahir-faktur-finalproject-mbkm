"""Grammar angka eksplisit; kandidat ambigu tidak dipakai sebagai nilai."""
import re
from decimal import Decimal

VERSI='angka-grammar-v1'

def analisis_angka(raw,locale='en_US'):
    result={'teks_asli':raw,'locale':locale,'versi_aturan':VERSI,'nilai':'','status':'rusak',
            'aturan':'','kandidat':[],'wajib_review':True}
    if not isinstance(raw,str):result['alasan']='Input bukan teks';return result
    value=raw.strip()
    if value.startswith('Rp'):value=value[2:].strip()
    if value.endswith('%'):value=value[:-1].strip()
    result['teks_numerik']=value
    def candidate(number,rule):
        text=format(Decimal(number),'f')
        if not any(c['nilai']==text for c in result['kandidat']):result['kandidat'].append({'nilai':text,'aturan':rule,'hanya_usulan':True})
    def accept(number,rule,status='langsung',review=False):
        result.update(nilai=format(Decimal(number),'f'),status=status,aturan=rule,wajib_review=review)
        if review:
            candidate(number,rule)
            result['alasan']='Interpretasi struktur nonstandar dipertahankan, tetapi membutuhkan bukti tambahan; bukan angka pasti.'
        return result
    if not value:
        result.update(status='kosong',alasan='Tidak ada teks angka');return result
    if not re.fullmatch(r'-?\d[\d.,\s]*',value):
        result['alasan']='Karakter/tanda tidak termasuk grammar angka; tidak dibuang otomatis';return result
    if re.fullmatch(r'-?\d+',value):return accept(value,'Bilangan bulat literal; tanda negatif dipertahankan')
    # Jangan menerima dua pemisah berurutan atau grup pendek dengan menghapusnya.
    if re.search(r'[.,]\s*[.,]',value):
        tail=re.search(r'[.,](\d{2})$',value)
        if tail:
            digits=re.sub(r'\D','',value[:tail.start()])
            if digits:candidate(('-' if value.startswith('-') else '')+digits+'.'+tail[1],'Usulan menghapus pemisah berurutan; memerlukan bukti tambahan')
        result['alasan']='Dua pemisah berurutan / grup kosong';return result
    # Bentuk yang dahulu selalu dianggap ribuan mempunyai interpretasi lain.
    if re.fullmatch(r'-?\d{1,3}(?:[.,]\d{3})+',value):
        separators=re.findall(r'[.,]',value)
        if locale=='en_US' and all(s==',' for s in separators):
            return accept(value.replace(',',''),'Ribuan koma: setiap grup tepat tiga digit')
        if locale=='id_ID' and all(s=='.' for s in separators):
            return accept(value.replace('.',''),'Ribuan titik: setiap grup tepat tiga digit')
        # Beberapa grup titik tidak bisa merupakan desimal tunggal. Susunan
        # ini didukung sebagai konvensi sumber integer dengan grup tiga digit.
        if len(separators)>1 and len(set(separators))==1:
            return accept(re.sub('[.,]','',value),'Integer bergrup tiga digit; pemisah ribuan berbeda dari locale',
                          'normalisasi_terstruktur',True)
        candidate(re.sub('[.,]','',value),'Seluruh grup tiga digit sebagai ribuan')
        if locale in ('en_US','id_ID'):
            decimal='.' if locale=='en_US' else ',';group=',' if locale=='en_US' else '.'
            canonical=value.replace(group,'')
            if re.fullmatch(r'-?\d+'+re.escape(decimal)+r'\d+',canonical):candidate(canonical.replace(decimal,'.'),'Interpretasi desimal menurut locale')
        elif len(separators)==1:candidate(value.replace(',','.'),'Pemisah tunggal sebagai desimal tiga digit')
        result.update(status='ambigu',alasan='Grup tiga digit berkonflik dengan locale/desimal; tidak memilih berdasarkan hitungan')
        return result
    decimal='.' if locale=='en_US' else ',' if locale=='id_ID' else None
    group=',' if locale=='en_US' else '.' if locale=='id_ID' else None
    if decimal:
        pattern=r'-?(?:\d+|\d{1,3}(?:'+re.escape(group)+r'\d{3})+)(?:'+re.escape(decimal)+r'\d+)?'
        if re.fullmatch(pattern,value):return accept(value.replace(group,'').replace(decimal,'.'),'Grammar locale: ribuan tiga digit, satu tanda desimal')
    # Angka polos dengan satu tanda desimal (bukan tiga digit yang ambigu).
    if re.fullmatch(r'-?\d+[.,]\d+',value):
        return accept(value.replace(',','.'),'Desimal tunggal tanpa grup ribuan; locale berbeda/tidak diketahui dicatat',
                      'langsung' if decimal is None or decimal in value else 'normalisasi_terstruktur',
                      review=decimal is not None and decimal not in value)
    # Konvensi tercetak/OCR yang ditinjau: tiap grup ribuan tepat tiga digit,
    # lalu dua digit desimal. Tidak menghapus sembarang tanda baca.
    if locale in ('en_US','id_ID') and re.fullmatch(r'-?\d{1,3}(?:[.,]\d{3})+\.\d{2}',value):
        integer,fraction=value[:-3],value[-2:]
        return accept(re.sub('[.,]','',integer)+'.'+fraction,
                      'Konvensi sumber grup ribuan tiga digit + desimal dua digit; pemisah nonstandar eksplisit',
                      'normalisasi_terstruktur',True)
    # Ribuan spasi valid, dengan tanda desimal yang benar jika tercetak.
    space_pattern=r'-?\d{1,3}(?:\s+\d{3})+(?:[.,]\d+)?'
    if re.fullmatch(space_pattern,value):
        compact=re.sub(r'\s+','',value)
        a=analisis_angka(compact,locale)
        if a['nilai']:
            return accept(a['nilai'],'Ribuan spasi: semua grup tiga digit; '+a['aturan'],'normalisasi_terstruktur',a['wajib_review'])
        result.update(kandidat=a['kandidat'],status=a['status'],alasan=a.get('alasan','Tidak tunggal'));return result
    # Spasi yang menggantikan desimal tidak diputuskan dari skala uang.
    if re.fullmatch(r'-?\d{1,3}(?:\s+\d{3})+\s+\d{2}',value) or re.fullmatch(r'-?\d{1,3}\s+\d{5}',value):
        digits=re.sub(r'\s+','',value)
        candidate(digits[:-2]+'.'+digits[-2:],'Usulan spasi menggantikan desimal dua digit')
        candidate(digits,'Usulan digit bilangan bulat dengan segmentasi OCR')
        result.update(status='ambigu',alasan='Tanda desimal tidak terbaca; dua skala mungkin');return result
    tail=re.search(r'[.,](\d{2})$',value)
    if tail:
        digits=re.sub(r'\D','',value[:tail.start()])
        if digits:candidate(('-' if value.startswith('-') else '')+digits+'.'+tail[1],'Usulan mengubah grup rusak; bukan nilai ekstraksi')
    result['alasan']='Susunan grup/pemisah tidak sesuai grammar dan locale; perlu bukti tambahan'
    return result

def parse_angka(raw,locale='en_US'):
    a=analisis_angka(raw,locale)
    if a['nilai']=='':raise ValueError(a.get('alasan','Angka kosong/ambigu'))
    return Decimal(a['nilai'])
