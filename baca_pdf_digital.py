"""Uji teks digital dua PDF dari inventaris, tanpa OCR atau ekstraksi dataset."""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pilah_dokumen import ASLI, baca_csv, path_relatif

try:
    import pymupdf
except ImportError:
    pymupdf = None


NAMA_DOKUMEN = [
    "Faktur dan Kwitansi ABBASY JAYA PERKASA, PT 3.pdf",
    "Faktur dan Bukti Transfer Astro Com_5.pdf",
]
ACUAN_VISUAL = {
    "asal": "Pemeriksaan visual yang diberikan pengguna; bukan hasil ekstraksi program.",
    "jumlah_halaman": 2,
    "kategori_per_halaman": ["faktur_penjualan", "kwitansi"],
    "memiliki_teks_digital": True,
    "batasan": "Hanya acuan untuk dua sampel ini; bukan aturan susunan seluruh arsip.",
}
DOKUMENTASI = [
    "https://pymupdf.readthedocs.io/en/latest/page.html",
    "https://pymupdf.readthedocs.io/en/latest/app1.html",
    "https://pymupdf.readthedocs.io/en/latest/changes.html",
]


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def cari_dokumen(inventaris, sumber, nama_dokumen=NAMA_DOKUMEN):
    """Cocokkan nama lengkap secara persis; laporkan semua kandidat ambigu."""
    rows = baca_csv(inventaris, ASLI)
    ditemukan, masalah = [], []
    for nama in nama_dokumen:
        kandidat = [row for row in rows if row["nama_file"] == nama]
        if len(kandidat) != 1:
            daftar = "\n".join("  - " + row["path_relatif"] for row in kandidat)
            masalah.append(f"{nama}: {len(kandidat)} kecocokan.\n{daftar}")
            continue
        row = kandidat[0]
        path = sumber.joinpath(*path_relatif(row).parts).resolve()
        if not path.is_relative_to(sumber) or not path.is_file():
            masalah.append(f"File tidak tersedia atau keluar dari sumber: {path}")
        else:
            ditemukan.append((row, path))
    if masalah:
        raise ValueError("Pencarian dihentikan; tidak memilih kandidat diam-diam:\n" + "\n".join(masalah))
    return ditemukan


def kategori_judul(baris, tinggi):
    """Heuristik judul mandiri di 35% bagian atas; tidak menerima nomor halaman."""
    bukti = []
    for line in baris:
        if line["bbox"][1] > tinggi * 0.35:
            continue
        teks = re.sub(r"\s+", " ", line["teks"].strip()).casefold()
        if teks in {"faktur penjualan", "sales invoice", "invoice"}:
            kategori = "faktur_penjualan"
        elif teks in {"kwitansi", "kuitansi", "receipt"}:
            kategori = "kwitansi"
        else:
            continue
        bukti.append({**line, "petunjuk_kategori": kategori})
    kategori = {item["petunjuk_kategori"] for item in bukti}
    if len(kategori) == 1:
        return kategori.pop(), "Judul mandiri dikenali pada bagian atas halaman; kategori masih sementara.", bukti
    if not kategori:
        from cakupan_dokumen import observasi_cakupan
        scope=observasi_cakupan(baris,tinggi)
        if scope and scope['status']=='di_luar_cakupan_faktur_penjualan':
            return 'perlu_diperiksa',scope['alasan'],scope['bukti']
    alasan = "Petunjuk judul bertentangan." if kategori else "Judul digital yang dikenali tidak ditemukan pada bagian atas halaman."
    return "perlu_diperiksa", alasan, bukti


def ekstrak_halaman(page, sumber, output, dpi):
    """Ambil teks native, kata, blok dan baris; render pratinjau halaman."""
    raw = page.get_text("text", sort=False)
    terurut = page.get_text("text", sort=True)
    words = page.get_text("words", sort=False)
    blocks = page.get_text("blocks", sort=False)
    detail = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    lines = []
    for block in detail["blocks"]:
        for nomor_baris, line in enumerate(block.get("lines", [])):
            spans = line["spans"]
            lines.append({"teks": "".join(span["text"] for span in spans),
                          "bbox": list(line["bbox"]), "nomor_blok": block["number"],
                          "nomor_baris": nomor_baris,
                          "ukuran_font_maks_pt": max((s["size"] for s in spans), default=0),
                          "arah_teks": list(line["dir"])})
    kategori, alasan, bukti = kategori_judul(lines, detail["height"])
    # Ukur lompatan vertikal dalam urutan kata native, bukan kesalahan semantik.
    lompatan = [i for i in range(1, len(words)) if words[i][1] < words[i - 1][1] - 5]
    urut_kata = sorted(range(len(words)), key=lambda i: (words[i][1], words[i][0], i))
    nomor = page.number + 1
    nama_png = f"halaman_{nomor:03d}.png"
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    pixmap.save(output / nama_png)
    result = {
        "nomor_halaman": nomor, "indeks_halaman": page.number, "path_sumber": str(sumber),
        "ukuran_halaman_pt": {"lebar": page.rect.width, "tinggi": page.rect.height},
        "ukuran_tanpa_rotasi_pt": {"lebar": detail["width"], "tinggi": detail["height"]},
        "page_rect": list(page.rect), "cropbox": list(page.cropbox), "mediabox": list(page.mediabox),
        "rotasi_derajat": page.rotation, "matriks_rotasi": list(page.rotation_matrix),
        "sistem_koordinat": "bbox [x0,y0,x1,y1], point (1/72 inci), asal kiri atas halaman tanpa rotasi; x ke kanan, y ke bawah. Gunakan matriks_rotasi lalu skala dpi/72 untuk pratinjau.",
        "teks_digital_tersedia": bool(raw.strip()), "jumlah_karakter": len(raw),
        "jumlah_kata": len(words), "jumlah_blok_teks": sum(b[6] == 0 for b in blocks),
        "teks_mentah": raw, "teks_terurut": terurut,
        "kata": [{"bbox": list(w[:4]), "teks": w[4], "nomor_blok": w[5],
                  "nomor_baris": w[6], "nomor_kata": w[7]} for w in words],
        "blok": [{"bbox": list(b[:4]), "teks": b[4], "nomor_blok": b[5], "tipe_blok": b[6]} for b in blocks],
        "baris": lines, "kategori_sementara": kategori, "alasan_kategori": alasan,
        "bukti_judul": bukti,
        "diagnostik_urutan_baca": {
            "teks_native_dan_sort_berbeda": raw != terurut,
            "urutan_kata_native_dan_yx_berbeda": urut_kata != list(range(len(words))),
            "jumlah_lompatan_y_ke_atas_lebih_5pt": len(lompatan),
            "contoh_indeks_kata_lompatan": lompatan[:10],
            "batasan": "Indikator geometris, bukan bukti salah baca. Sort tidak menjamin label-nilai dan sel tabel sudah berpasangan benar.",
        },
        "pratinjau": {"file": nama_png, "dpi": dpi, "lebar_px": pixmap.width, "tinggi_px": pixmap.height},
    }
    from geometri_tabel_pdf import perkaya_halaman
    return perkaya_halaman(sumber,nomor,result)


def ekstrak_dokumen(row, path, folder_run, dpi):
    identitas = hashlib.sha256(row["path_relatif"].encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", path.stem).strip("_")[:65]
    output = folder_run / f"{slug}__{identitas}"
    output.mkdir()
    sebelum = sha256(path)
    with pymupdf.open(path) as doc:
        if not doc.is_pdf or doc.needs_pass:
            raise ValueError(f"Bukan PDF atau memerlukan kata sandi: {path}")
        hasil = {"path_sumber": str(path), "path_relatif": row["path_relatif"],
                 "folder_klien": row["folder_klien"], "nama_file": row["nama_file"],
                 "jumlah_halaman": doc.page_count,
                 "halaman": [ekstrak_halaman(page, path, output, dpi) for page in doc]}
    if sha256(path) != sebelum:
        raise ValueError(f"Hash sumber berubah selama pembacaan: {path}")
    data = {"versi_skema": 1, "versi_pymupdf": pymupdf.VersionBind,
            "metode": "PyMuPDF get_text (lapisan teks digital) dan get_pixmap; tanpa OCR.",
            "sha256_sumber_sebelum": sebelum, "sha256_sumber_sesudah": sha256(path),
            "dokumentasi_resmi": DOKUMENTASI, "hasil_program": hasil,
            "acuan_visual_pengguna": ACUAN_VISUAL.copy()}
    with (output / "teks_koordinat.json").open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return output, data


def tulis_laporan(folder_run, hasil):
    lines = ["# Uji PDF digital", "", f"PyMuPDF terpasang: **{pymupdf.VersionBind}**.", "",
             "## Hasil yang dibaca program", "",
             "Kedua dokumen ditemukan melalui nama lengkap yang masing-masing memiliki tepat satu kecocokan inventaris.", ""]
    for output, data in hasil:
        doc = data["hasil_program"]
        lines += [f"### {doc['nama_file']}", "", f"Sumber: `{doc['path_sumber']}`", "",
                  f"Jumlah halaman yang dibaca: **{doc['jumlah_halaman']}**.", "",
                  f"[JSON teks dan koordinat]({output.name}/teks_koordinat.json)", "",
                  "| Halaman | Teks digital | Kata | Blok teks | Judul terbaca | Kategori sementara |", "| --- | --- | ---: | ---: | --- | --- |"]
        for page in doc["halaman"]:
            judul = "; ".join(b["teks"] for b in page["bukti_judul"]) or "Tidak dikenali"
            lines.append(f"| {page['nomor_halaman']} | {'Ada' if page['teks_digital_tersedia'] else 'Tidak terdeteksi'} | {page['jumlah_kata']} | {page['jumlah_blok_teks']} | {judul} | {page['kategori_sementara']} |")
        lines += [""]
        for page in doc["halaman"]:
            diag = page["diagnostik_urutan_baca"]
            bukti = page["bukti_judul"]
            posisi = "; ".join(f"{b['teks']}: bbox {', '.join(f'{v:.2f}' for v in b['bbox'])} pt" for b in bukti)
            lines.append(f"- Halaman {page['nomor_halaman']}: ukuran {page['ukuran_halaman_pt']['lebar']:g} × {page['ukuran_halaman_pt']['tinggi']:g} pt, rotasi {page['rotasi_derajat']}°. {posisi}. [Pratinjau]({output.name}/{page['pratinjau']['file']}).")
            lines.append(f"- Diagnostik halaman {page['nomor_halaman']}: teks native dan sort berbeda = {diag['teks_native_dan_sort_berbeda']}; lompatan urutan kata ke atas lebih dari 5 pt = {diag['jumlah_lompatan_y_ke_atas_lebih_5pt']}.")
        lines += ["", "Hash SHA-256 sumber sebelum dan sesudah pembacaan sama.", ""]
    lines += ["## Acuan visual dari pengguna (terpisah dari hasil program)", "",
              "Pengguna menyatakan masing-masing PDF terdiri dari dua halaman, halaman 1 faktur penjualan, halaman 2 kwitansi, dan keduanya memiliki teks digital. Informasi ini tidak dipakai untuk mengisi jumlah halaman, teks, atau kategori hasil program. Ini bukan aturan umum susunan PDF arsip.", "",
              "## Metode dan keterbatasan", "",
              "- get_text('text', sort=False) mempertahankan urutan teks yang dikembalikan dari PDF; versi sort=True disimpan terpisah. Kata dan blok menyimpan bbox; baris menyimpan bukti judul beserta ukuran font. PNG dibuat pada 144 dpi secara default.",
              "- Kategori berasal dari judul mandiri pada 35% bagian atas halaman, bukan nama file atau nomor halaman. Judul tidak dikenal atau bertentangan masuk perlu_diperiksa. Heuristik ini dapat melewatkan judul berupa gambar, judul terpecah, atau tata letak lain.",
              "- Perbedaan urutan native dan sort serta lompatan vertikal menunjukkan susunan internal yang perlu ditinjau. Pengurutan posisi tidak membuktikan pasangan label-nilai atau baris/kolom tabel benar. Judul dapat muncul jauh di belakang dalam teks mentah walaupun posisinya di atas tabel.",
              "- Teks digital tersedia berarti ada karakter yang dapat diekstrak, bukan jaminan semua tulisan, gambar, cap, atau tanda tangan sudah terbaca. Lapisan teks tersembunyi atau OCR lama dalam PDF juga belum dibedakan asalnya.",
              "- Hasil ini belum mengekstrak item, memvalidasi angka, atau membentuk faktur.csv/detail_faktur.csv. Tidak ada OCR, perubahan sumber, atau penimpaan review manual.", "",
              "## Dokumentasi resmi", "",
              "Dokumentasi diperiksa pada 15 September 2026 dan menyatakan mencakup versi hingga 1.28.2, sama dengan versi terpasang.", ""]
    lines += [f"- [Referensi {i + 1}]({url})" for i, url in enumerate(DOKUMENTASI)]
    laporan = folder_run / "laporan_pdf_digital.md"
    laporan.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return laporan


def jalankan(inventaris, sumber, output, dpi=144):
    if pymupdf is None:
        raise ValueError("PyMuPDF belum tersedia. Pasang dengan .venv\\Scripts\\python.exe -m pip install -r requirements-pdf.txt")
    sumber, output = sumber.resolve(), output.resolve()
    if output.is_relative_to(sumber):
        raise ValueError("Keluaran harus berada di luar folder sumber.")
    if not 36 <= dpi <= 300:
        raise ValueError("DPI pratinjau harus antara 36 dan 300.")
    dokumen = cari_dokumen(inventaris, sumber)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    folder_run = output / run_id
    folder_run.mkdir(parents=True, exist_ok=False)
    hasil = [ekstrak_dokumen(row, path, folder_run, dpi) for row, path in dokumen]
    laporan = tulis_laporan(folder_run, hasil)
    for folder, data in hasil:
        doc = data["hasil_program"]
        print(f"{doc['nama_file']}: {doc['jumlah_halaman']} halaman")
        for page in doc["halaman"]:
            print(f"  Halaman {page['nomor_halaman']}: {page['jumlah_kata']} kata; {page['kategori_sementara']}")
        print(f"  JSON: {folder / 'teks_koordinat.json'}")
    print(f"Laporan: {laporan}")
    return folder_run


def main():
    proyek = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=proyek / "outputs/inventory.csv")
    parser.add_argument("--source", type=Path, default=Path(r"C:\Data Klient Zahir"))
    parser.add_argument("--output-dir", type=Path, default=proyek / "outputs/uji_pdf_digital")
    parser.add_argument("--dpi", type=int, default=144)
    args = parser.parse_args()
    try:
        if sys.prefix == sys.base_prefix:
            raise ValueError("Gunakan lingkungan virtual proyek: .venv\\Scripts\\python.exe baca_pdf_digital.py")
        jalankan(args.inventory, args.source, args.output_dir, args.dpi)
    except Exception as error:
        parser.exit(1, f"Uji PDF digital gagal: {error}\nKeluaran run yang belum lengkap tidak dianggap berhasil. Sumber dan review manual tidak ditulis.\n")


if __name__ == "__main__":
    main()
