"""Pilah metadata dokumen dan pilih sampel review; tanpa OCR atau salinan sumber."""

import argparse
import csv
import io
import re
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from uuid import uuid4


ASLI = ["folder_klien", "nama_file", "path_relatif", "ekstensi", "ukuran_byte"]
KLASIFIKASI = ASLI + ["kategori_sementara", "alasan_klasifikasi"]
REVIEW = KLASIFIKASI + ["file_tersedia", "jenis_dokumen_terverifikasi", "catatan_review"]
MANUAL = ["jenis_dokumen_terverifikasi", "catatan_review"]
FORMAT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp", ".pdf"}
FAKTUR = "calon_faktur_penjualan"
TRANSFER = "calon_bukti_transfer"
PAJAK = "calon_faktur_pajak"
KONFLIK = "petunjuk_bertentangan"
PERIKSA = "perlu_diperiksa"
LUAR = "di_luar_cakupan_awal"
KATEGORI = [FAKTUR, TRANSFER, PAJAK, KONFLIK, PERIKSA, LUAR]
LAIN = [PERIKSA, KONFLIK, PAJAK]
POLA_FAKTUR = re.compile(r"\b(?:faktur|invoice|inv)(?=\b|\d)")
POLA_BAYAR = re.compile(r"\b(?:transfer|trf|bukti bayar|kwitansi|kuitansi)\b")
POLA_PAJAK = re.compile(r"\bfaktur pajak\b")


def path_relatif(row):
    path = PureWindowsPath(row["path_relatif"])
    if not path.parts or path.drive or path.root or ".." in path.parts:
        raise ValueError(f"Path relatif tidak aman: {row['path_relatif']}")
    klien = path.parts[0] if len(path.parts) > 1 else "."
    if row["folder_klien"] != klien or row["nama_file"] != path.name:
        raise ValueError(f"Nama file/folder klien tidak konsisten: {path}")
    return path


def klasifikasikan(row):
    path = path_relatif(row)
    if row["ekstensi"].lower() not in FORMAT:
        return LUAR, "Ekstensi tidak termasuk delapan format cakupan awal."

    # Setiap komponen dinilai terpisah: frasa tidak dirangkai melintasi folder.
    komponen = [("subfolder", nama) for nama in path.parts[1:-1]]
    komponen.append(("nama file", path.stem))
    faktur, bayar, pajak = [], [], []
    for jenis, nama in komponen:
        # Garis bawah, titik, tanda hubung, dan spasi menjadi pemisah kata.
        teks = re.sub(r"[\W_]+", " ", nama.casefold()).strip()
        for pola, hasil in [(POLA_FAKTUR, faktur), (POLA_BAYAR, bayar), (POLA_PAJAK, pajak)]:
            if pola.search(teks):
                hasil.append(f"{jenis} '{nama}'")
    if faktur and bayar:
        return KONFLIK, "Petunjuk faktur pada " + "; ".join(faktur) + "; pembayaran pada " + "; ".join(bayar) + ". Dokumen mungkin gabungan; perlu pemeriksaan isi, belum terverifikasi."
    if pajak:
        return PAJAK, "Frasa faktur pajak pada " + "; ".join(pajak) + "."
    if faktur:
        return FAKTUR, "Kata faktur/invoice/inv pada " + "; ".join(faktur) + "."
    if bayar:
        return TRANSFER, "Petunjuk pembayaran (transfer/trf/bukti bayar/kwitansi/kuitansi) pada " + "; ".join(bayar) + ". Isi belum terverifikasi."
    return PERIKSA, "Gambar/PDF tanpa petunjuk yang dikenali pada nama file atau subfolder."


def baca_csv(path, kolom, data=None):
    try:
        if data is None:
            data = path.read_bytes()
        handle = io.StringIO(data.decode("utf-8-sig"), newline="")
        reader = csv.DictReader(handle, strict=True)
        if reader.fieldnames != kolom:
            raise ValueError(f"Header tidak sesuai: {reader.fieldnames}")
        rows = list(reader)
        if any(None in row or any(value is None for value in row.values()) for row in rows):
            raise ValueError("Jumlah kolom tidak konsisten")
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        raise ValueError(f"CSV tidak dapat dibaca dengan benar ({path}): {error}") from error
    return rows


def review_terisi(row):
    return any(row[kolom] != "" for kolom in MANUAL)


def baca_review_lama(path, data):
    rows = baca_csv(path, REVIEW, data)
    indeks = {}
    for row in rows:
        path_relatif(row)
        key = row["path_relatif"]
        if key in indeks:
            raise ValueError(f"CSV review lama ({path}) memiliki path_relatif berulang: {key}; penimpaan dihentikan agar review tidak hilang.")
        indeks[key] = row
    return indeks


def nama_unik():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid4().hex[:12]


def backup_keluaran(snapshot, identitas):
    backups = []
    for path, data in snapshot.items():
        if path.read_bytes() != data:
            raise ValueError(f"Berkas berubah selama pemilahan: {path}; penimpaan dihentikan. Jalankan ulang.")
        backup = path.with_name(f"{path.stem}.backup_{identitas}{path.suffix}")
        # Mode eksklusif memastikan backup sebelumnya tidak pernah tertimpa.
        with backup.open("xb") as handle:
            handle.write(data)
        backups.append(backup)
    return backups


def tulis_csv(path, rows, kolom):
    sementara = path.with_suffix(".csv.tmp")
    try:
        with sementara.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=kolom)
            writer.writeheader()
            writer.writerows(rows)
        sementara.replace(path)
    finally:
        sementara.unlink(missing_ok=True)


class Alokasi:
    """Aliran biaya minimum: kuota kategori dan kapasitas dua file per klien.

    Jalur balik memungkinkan alokasi dipindahkan agar pilihan awal tidak
    menghabiskan folder yang dibutuhkan kategori lain. Semua iterasi terurut.
    """

    def __init__(self):
        self.graf = defaultdict(list)

    def tambah(self, asal, tujuan, kapasitas, biaya=0):
        maju = [tujuan, kapasitas, biaya, None]
        balik = [asal, 0, -biaya, maju]
        maju[3] = balik
        self.graf[asal].append(maju)
        self.graf[tujuan].append(balik)
        return maju

    def jalankan(self):
        total = 0
        while total < 50:
            jarak = {"sumber": 0}
            sebelumnya = {}
            antrean = deque(["sumber"])
            dalam_antrean = {"sumber"}
            while antrean:
                node = antrean.popleft()
                dalam_antrean.remove(node)
                for sisi in self.graf[node]:
                    tujuan, kapasitas, biaya, _ = sisi
                    baru = jarak[node] + biaya
                    if kapasitas and baru < jarak.get(tujuan, float("inf")):
                        jarak[tujuan] = baru
                        sebelumnya[tujuan] = (node, sisi)
                        if tujuan not in dalam_antrean:
                            antrean.append(tujuan)
                            dalam_antrean.add(tujuan)
            if "hasil" not in jarak:
                break
            node = "hasil"
            while node != "sumber":
                node, sisi = sebelumnya[node]
                sisi[1] -= 1
                sisi[3][1] += 1
            total += 1


def pilih_sampel(rows):
    kelompok = defaultdict(list)
    for indeks, row in enumerate(rows):
        kategori = row["kategori_sementara"]
        if kategori in [FAKTUR] + LAIN:
            kelompok[(kategori, row["ekstensi"].lower(), row["folder_klien"])].append(indeks)
    alokasi = Alokasi()
    alokasi.tambah("sumber", "penjualan", 35)
    alokasi.tambah("sumber", "lain", 15)
    for kategori in [FAKTUR] + LAIN:
        node = ("kategori", kategori)
        if kategori == FAKTUR:
            alokasi.tambah("penjualan", node, 35)
        else:
            # Prioritas tertinggi: wakili ketiga kategori review jika memungkinkan.
            alokasi.tambah("lain", node, 1, -1_000_000_000)
            for urutan in range(1, 15):
                alokasi.tambah("lain", node, 1, urutan)
        ekstensi = sorted({ext for kat, ext, _ in kelompok if kat == kategori})
        for ext in ekstensi:
            tujuan = ("ekstensi", kategori, ext)
            alokasi.tambah(node, tujuan, 1, -1_000_000)
            alokasi.tambah(node, tujuan, 49)
    for klien in sorted({klien for _, _, klien in kelompok}):
        alokasi.tambah(("klien", klien), "hasil", 1, -10_000)
        alokasi.tambah(("klien", klien), "hasil", 1)
    sisi_kelompok = []
    for (kategori, ext, klien), indeks in sorted(kelompok.items()):
        kapasitas = min(2, len(indeks))
        sisi = alokasi.tambah(("ekstensi", kategori, ext), ("klien", klien), kapasitas)
        sisi_kelompok.append((indeks, sisi, kapasitas))
    alokasi.jalankan()
    terpilih = []
    for indeks, sisi, kapasitas in sisi_kelompok:
        urut = sorted(indeks, key=lambda i: (rows[i]["path_relatif"].casefold(), rows[i]["path_relatif"], i))
        terpilih.extend(urut[:kapasitas - sisi[1]])
    return sorted(terpilih, key=lambda i: (rows[i]["kategori_sementara"], rows[i]["path_relatif"].casefold(), rows[i]["path_relatif"], i))


def cek_file(row, sumber):
    path = sumber.joinpath(*path_relatif(row).parts)
    try:
        if not path.resolve().is_relative_to(sumber.resolve()):
            return "tidak_diperiksa", "Path mengarah keluar folder sumber."
        if path.is_file():
            return "ya", ""
        return "tidak", "File tidak ditemukan atau bukan file biasa."
    except OSError as error:
        return "tidak_dapat_diperiksa", str(error)


def nilai_asli(rows):
    return Counter(tuple(row[kolom] for kolom in ASLI) for row in rows)


def aman_md(teks):
    return str(teks).replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("`", "'")


def jalankan(inventaris, sumber, output):
    inventaris, sumber, output = inventaris.resolve(), sumber.resolve(), output.resolve()
    tujuan = [output / nama for nama in ["klasifikasi_dokumen.csv", "sampel_review.csv", "laporan_pemilahan.md"]]
    for path in tujuan:
        if path.resolve().is_relative_to(sumber) or path.resolve() == inventaris:
            raise ValueError("Keluaran tidak boleh menimpa inventaris atau berada di dalam sumber.")
    # Baca dan validasi review sebelum menulis keluaran apa pun.
    snapshot = {path: path.read_bytes() for path in tujuan if path.exists()}
    review_lama = baca_review_lama(tujuan[1], snapshot[tujuan[1]]) if tujuan[1] in snapshot else {}
    hitung_lama = None
    if tujuan[0] in snapshot:
        hitung_lama = Counter(row["kategori_sementara"] for row in baca_csv(tujuan[0], KLASIFIKASI, snapshot[tujuan[0]]))
    rows = baca_csv(inventaris, ASLI)
    hasil = []
    for row in rows:
        kategori, alasan = klasifikasikan(row)
        hasil.append({**row, "kategori_sementara": kategori, "alasan_klasifikasi": alasan})
    terpilih = pilih_sampel(hasil)
    sampel, masalah_file = [], []
    for indeks in terpilih:
        row = hasil[indeks]
        tersedia, masalah = cek_file(row, sumber)
        manual = review_lama.get(row["path_relatif"], {})
        sampel.append({**row, "file_tersedia": tersedia, **{kolom: manual.get(kolom, "") for kolom in MANUAL}})
        if masalah:
            masalah_file.append((row["path_relatif"], tersedia, masalah))

    path_baru = {row["path_relatif"] for row in sampel}
    keluar = [row for key, row in review_lama.items() if key not in path_baru and review_terisi(row)]
    dipertahankan = sum(review_terisi(row) for row in sampel)
    output.mkdir(parents=True, exist_ok=True)
    identitas = nama_unik()
    backups = backup_keluaran(snapshot, identitas)
    arsip = None
    if keluar:
        arsip = output / f"arsip_review_{identitas}.csv"
        with arsip.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REVIEW)
            writer.writeheader()
            writer.writerows(keluar)
        if baca_csv(arsip, REVIEW) != keluar:
            raise ValueError(f"Verifikasi arsip review gagal: {arsip}; penimpaan dihentikan.")
    # Backup dan arsip review harus berhasil sebelum keluaran lama diganti.
    for path, data in snapshot.items():
        if path.read_bytes() != data:
            raise ValueError(f"Berkas berubah selama pemilahan: {path}; penimpaan dihentikan. Jalankan ulang.")
    tulis_csv(tujuan[0], hasil, KLASIFIKASI)
    tulis_csv(tujuan[1], sampel, REVIEW)
    # Verifikasi berkas yang benar-benar tersimpan, termasuk multiplicity baris.
    tersimpan = baca_csv(tujuan[0], KLASIFIKASI)
    sampel_disk = baca_csv(tujuan[1], REVIEW)
    if len(tersimpan) != len(rows) or nilai_asli(tersimpan) != nilai_asli(rows):
        raise ValueError("Verifikasi gagal: ada baris inventaris hilang atau terhitung berlebih.")
    if tersimpan != hasil or sampel_disk != sampel:
        raise ValueError("Verifikasi gagal: hasil tersimpan berbeda dari hasil pemilahan.")
    hitung = Counter(row["kategori_sementara"] for row in tersimpan)
    sampel_kat = Counter(row["kategori_sementara"] for row in sampel_disk)
    sampel_klien = Counter(row["folder_klien"] for row in sampel_disk)
    if (len(terpilih) != len(set(terpilih)) or len(sampel) > 50
            or max(sampel_klien.values(), default=0) > 2
            or sampel_kat[FAKTUR] > 35 or sum(sampel_kat[k] for k in LAIN) > 15):
        raise ValueError("Verifikasi gagal: sampel melanggar kuota atau batas per klien.")

    kendala = []
    if len(rows) != 23181:
        kendala.append(f"Inventaris berisi {len(rows):,} baris, berbeda dari acuan 23.181; semua baris aktual tetap diproses.")
    duplikat_path = sum(n - 1 for n in Counter(row["path_relatif"] for row in rows).values())
    if duplikat_path:
        kendala.append(f"Inventaris memuat {duplikat_path} pengulangan path; setiap baris tetap dipertahankan, tanpa deduplikasi.")
    for label, kategori, target in [("calon faktur penjualan", [FAKTUR], 35), ("review lainnya", LAIN, 15)]:
        aktual = sum(sampel_kat[k] for k in kategori)
        tersedia = sum(hitung[k] for k in kategori)
        if aktual < target:
            kendala.append(f"Target {label} {target} hanya terpenuhi {aktual}: tersedia {tersedia} kandidat; alokasi maksimum dibatasi ketersediaan kategori dan kapasitas bersama dua file per klien. Kuota kelompok lain tidak ditambah sebagai pengganti.")
    for kategori in LAIN:
        if not hitung[kategori]:
            kendala.append(f"Kategori {kategori} tidak tersedia sehingga tidak dapat diwakili.")
        elif not sampel_kat[kategori]:
            kendala.append(f"Kategori {kategori} tersedia tetapi tidak terwakili karena kendala kapasitas bersama per klien.")
    if masalah_file:
        kendala.append(f"Ada {len(masalah_file)} file sampel yang tidak ditemukan atau tidak dapat diperiksa; rinciannya di bawah.")

    laporan = ["# Laporan pemilahan dokumen", "", f"Inventaris: `{inventaris}`", "", f"Sumber: `{sumber}`", "",
        f"Jumlah inventaris aktual: **{len(rows):,}**. Acuan sebelumnya: **23.181**.", "",
        "## Jumlah dan contoh per kategori", "", "| Kategori | Jumlah | Contoh nama file (maksimal 3) |", "| --- | ---: | --- |"]
    for kategori in KATEGORI:
        contoh = [row["nama_file"] for row in hasil if row["kategori_sementara"] == kategori][:3]
        laporan.append(f"| {kategori} | {hitung[kategori]} | " + "; ".join(aman_md(n) for n in contoh) + " |")
    if hitung_lama is not None:
        laporan += ["", "## Perubahan dari keluaran sebelumnya", "", "| Kategori | Sebelumnya | Sekarang | Perubahan |", "| --- | ---: | ---: | ---: |"]
        laporan += [f"| {kat} | {hitung_lama[kat]} | {hitung[kat]} | {hitung[kat] - hitung_lama[kat]:+d} |" for kat in KATEGORI]
    laporan += ["", "## Aturan pemilahan", "",
        "1. Hanya .jpg, .jpeg, .png, .bmp, .tif, .tiff, .webp, dan .pdf masuk cakupan awal; ekstensi lain tetap dicatat sebagai di_luar_cakupan_awal.",
        "2. Kata kunci hanya diperiksa pada nama file tanpa ekstensi dan masing-masing subfolder di bawah folder klien. Nama folder klien dan folder sumber tidak diperiksa. Frasa tidak digabung melintasi komponen path.",
        "3. Pencocokan tidak membedakan kapital. Spasi, garis bawah, titik, dan tanda hubung menjadi pemisah. Faktur/invoice/inv dikenali sebagai kata utuh atau langsung diikuti angka (Faktur1_1, invoice123, inv001 cocok; inventory tidak). Transfer, trf, kwitansi, kuitansi, dan frasa bukti bayar menjadi petunjuk pembayaran dengan batas kata.",
        "4. Petunjuk faktur dan pembayaran bersama selalu menjadi petunjuk_bertentangan, termasuk jika berasal dari komponen path berbeda atau mengandung faktur pajak. Faktur dan Kwitansi.pdf mungkin dokumen gabungan; perlu pemeriksaan isi dan belum terverifikasi.",
        "5. Setelah konflik, frasa faktur pajak dipisahkan sebelum faktur umum. Pembayaran saja masuk calon_bukti_transfer; gambar/PDF tanpa petunjuk masuk perlu_diperiksa.",
        "6. Semua kategori bersifat sementara. Nama file bukan bukti pasti isi. Akhiran _1, _2, atau 1.0 tidak dianggap bukti duplikat; tidak dilakukan deduplikasi.", "",
        "## Sampel review", "",
        f"Terpilih **{len(sampel)}** file dari **{len(sampel_klien)}** folder klien; maksimum aktual **{max(sampel_klien.values(), default=0)}** file per klien.", "",
        "Alokasi deterministik memakai aliran biaya minimum dengan kuota maksimum 35 calon faktur penjualan dan 15 review lainnya. Jumlah sampel dimaksimalkan hingga 50. Dalam alokasi yang memenuhi kapasitas, prioritasnya adalah keterwakilan ketiga kategori review, keragaman ekstensi per kategori, keragaman folder klien, kemudian keseimbangan kategori review. Urutan kategori, ekstensi, folder, dan path menjadi pemecah seri; tidak memakai pengacakan.", "",
        "Pemilihan deterministik ini merupakan sampel pengembangan, bukan sampel acak untuk mengestimasi akurasi seluruh arsip.", "",
        "| Kategori sampel | Jumlah |", "| --- | ---: |"]
    laporan += [f"| {kategori} | {sampel_kat[kategori]} |" for kategori in [FAKTUR] + LAIN]
    laporan += ["", "| Ekstensi sampel | Jumlah |", "| --- | ---: |"]
    laporan += [f"| {ext} | {n} |" for ext, n in sorted(Counter(row["ekstensi"].lower() for row in sampel).items())]
    laporan += ["", "Lokasi file diperoleh dengan menggabungkan folder sumber dan path_relatif. Tidak ada gambar yang disalin. file_tersedia berisi ya/tidak/tidak_diperiksa/tidak_dapat_diperiksa. Dua kolom review manual dipertahankan persis untuk path_relatif yang sama; untuk sampel baru tetap kosong.", "",
        "## Perlindungan review manual", "",
        f"- Baris review terisi pada sampel lama: {sum(review_terisi(row) for row in review_lama.values())}.",
        f"- Baris review terisi yang dipertahankan dalam sampel baru: {dipertahankan}.",
        f"- Baris review terisi yang keluar dari sampel dan diarsipkan: {len(keluar)}.",
        "- CSV review lama divalidasi sebelum keluaran ditulis. CSV rusak atau path berulang menghentikan penimpaan."]
    laporan += [f"- Backup: `{path}`" for path in backups]
    laporan += [f"- Arsip review: `{arsip}`." if arsip else "- Arsip baru tidak diperlukan karena tidak ada review terisi yang keluar dari sampel."]
    laporan += ["- Backup menyimpan seluruh byte berkas lama. Arsip bernama unik menyimpan seluruh kolom baris review yang keluar dan tidak ditimpa pada eksekusi berikutnya.", "",
        "## Hasil verifikasi", "",
        f"- Baris klasifikasi: {len(tersimpan)}; sama dengan inventaris.",
        "- Kelima nilai kolom asli dan jumlah kemunculan setiap baris cocok persis setelah CSV dibaca kembali; tidak ada baris hilang atau terhitung berlebih.",
        f"- Pengulangan path dalam inventaris: {duplikat_path}.",
        "- Setiap baris mendapat tepat satu kategori; sampel menggunakan indeks baris berbeda dan mematuhi kuota serta batas dua file per klien.",
        f"- File sampel tersedia: {sum(row['file_tersedia'] == 'ya' for row in sampel)} dari {len(sampel)}.", "",
        "## Kendala", ""]
    laporan += ["- " + pesan for pesan in kendala] or ["Tidak ditemukan kendala pada pemilahan dan pemeriksaan ketersediaan sampel."]
    if masalah_file:
        laporan += ["", "| Path relatif | Status | Kendala |", "| --- | --- | --- |"]
        laporan += ["| " + " | ".join(aman_md(v) for v in masalah) + " |" for masalah in masalah_file]
    laporan += ["", "Skrip tidak memverifikasi isi dokumen; isian review manual lama hanya dipertahankan. Tidak ada OCR, hasil ekstraksi faktur, pemasangan paket, atau pengiriman dokumen ke layanan luar.", ""]
    tujuan[2].write_text("\n".join(laporan), encoding="utf-8")
    print(f"Inventaris: {len(rows)}; klasifikasi: {len(tersimpan)}; verifikasi baris: LULUS")
    for kategori in KATEGORI:
        perubahan = f" (sebelumnya {hitung_lama[kategori]}, perubahan {hitung[kategori] - hitung_lama[kategori]:+d})" if hitung_lama is not None else ""
        print(f"{kategori}: {hitung[kategori]}{perubahan}")
    print(f"Sampel: {len(sampel)}; folder klien: {len(sampel_klien)}; bermasalah: {len(masalah_file)}")
    print(f"Review terisi dipertahankan: {dipertahankan}; diarsipkan: {len(keluar)}")
    for path in backups:
        print(f"Backup: {path}")
    if arsip:
        print(f"Arsip review: {arsip}")
    for pesan in kendala:
        print(f"Kendala: {pesan}")
    for path, status, masalah in masalah_file:
        print(f"File sampel {status}: {path} ({masalah})")
    for path in tujuan:
        print(f"Keluaran: {path}")


def main():
    proyek = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=proyek / "outputs" / "inventory.csv")
    parser.add_argument("--source", type=Path, default=Path(r"C:\Data Klient Zahir"))
    parser.add_argument("--output-dir", type=Path, default=proyek / "outputs")
    args = parser.parse_args()
    try:
        jalankan(args.inventory, args.source, args.output_dir)
    except (OSError, ValueError, csv.Error) as error:
        parser.exit(1, f"Pemilahan gagal: {error}\n")


if __name__ == "__main__":
    main()
