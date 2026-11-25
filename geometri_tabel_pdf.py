"""Garis vertikal PDF dipadukan dari segmen/rect asli; tanpa OCR atau teks baru."""
from pathlib import Path
from batch_common import digest

VERSI='pdf-grid-native-v2'


def perkaya_halaman(source,page_number,data):
    """Tambahkan bukti vektor asli; tidak mengganti kata/teks/koordinat."""
    import copy
    result=copy.deepcopy(data)
    native=garis_vertikal(source,page_number)
    groups=[]
    for line in sorted(native,key=lambda s:s['x']):
        g=next((g for g in reversed(groups) if line['x']-g[0]['x']<=1.05
                and abs(line['y0']-g[0]['y0'])<=.05 and abs(line['y1']-g[0]['y1'])<=.05),None)
        if g is None:groups.append([line])
        else:g.append(line)
    # Border PDF dapat berupa rect tipis: dua tepinya bukan dua batas sel.
    # Sumbu stroke hanya dibentuk untuk tepi yang sejalan dan sama rentang y.
    result['garis_tabel_pt']=[{**g[0],'x':sum(s['x'] for s in g)/len(g),
        'tepi_stroke_asli':g,'aturan_stroke':'Tepi paralel <=1.05pt dengan y sama (0.05pt); sumbu stroke, bukan bbox kata.'} for g in groups]
    result['geometri_pdf']={'versi':VERSI,'hash_sumber':digest(source),'halaman':page_number,
                            'satuan':'point PDF','asal':'get_drawings garis/rect asli; tidak OCR'}
    return result


def garis_vertikal(source,page_number):
    import pymupdf
    source=Path(source);h=digest(source);lines=[]
    with pymupdf.open(source) as pdf:
        for draw in pdf[page_number-1].get_drawings():
            for item in draw['items']:
                points=[]
                if item[0]=='l':points=[item[1:3]]
                elif item[0]=='re':
                    r=item[1];points=[(pymupdf.Point(r.x0,r.y0),pymupdf.Point(r.x0,r.y1)),(pymupdf.Point(r.x1,r.y0),pymupdf.Point(r.x1,r.y1))]
                for a,b in points:
                    if abs(a.x-b.x)<.05 and abs(a.y-b.y)>1:
                        lines.append({'x':float(a.x),'y0':float(min(a.y,b.y)),'y1':float(max(a.y,b.y))})
    merged=[]
    for line in sorted(lines,key=lambda s:(round(s['x'],2),s['y0'],s['y1'])):
        # Celah antarsel sebesar satu point berasal dari stroke tepi rect.
        # Hanya segmen dengan x sama (0.05pt) yang boleh disambungkan.
        target=next((s for s in reversed(merged) if abs(s['x']-line['x'])<.05 and line['y0']<=s['y1']+1.05),None)
        if target:target['y1']=max(target['y1'],line['y1']);target['segmen_asli'].append(line)
        else:merged.append({**line,'segmen_asli':[line]})
    return [{**s,'asal':'Gabungan garis/tepi rect PDF asli yang sebidang dan tersambung','batas_celah_pt':1.05,'hash_sumber':h} for s in merged]
