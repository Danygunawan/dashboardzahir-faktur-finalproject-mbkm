# Instalasi demo dari salinan bersih

Versi demo: Pilih Dokumen -> Atur Konversi -> Hasil, dengan menu Home/Data,
tema terang/gelap, XLSX dua sheet, dan ZIP dua CSV. Database dan koreksi pengguna
belum termasuk. Mesin, konfigurasi OCR, dan tampilan tetap seperti versi lokal.

## Sumber dan prasyarat

Gunakan salinan sumber berdasarkan `demo-manifest.json`, bukan salinan seluruh
folder penelitian. Manifest berisi 35 modul Python runtime, aset lokal, dependensi,
tes, dan dokumentasi. Logo PNG transparan, font Inter beserta lisensinya, dan
JavaScript/CSS disertakan; tidak memerlukan CDN atau bundler.

Baseline aplikasi penuh adalah Windows 64-bit dengan Python 3.12, Git untuk
audit/ekspor/tes preparation, dan akses internet untuk instalasi paket serta
pengunduhan awal model. CI menggunakan
Python 3.12; Node.js 24 dan Chrome/Chromium hanya diperlukan untuk tes browser.
Tes Linux pada workflow belum membuktikan dukungan OCR penuh di Linux.

Di workspace lama, buat kandidat salinan lokal ke **folder tujuan yang belum ada**:

```powershell
python scripts/export_demo.py C:/demo-preparation/zahir-demo-review
```

Perintah tersebut hanya menyalin allowlist, tidak menghapus sumber, membuat
repositori, mengunggah, atau menyatakan sumber sudah layak dipublikasikan.
Tinjau [daftar sebelum push](REVIEW-BEFORE-PUSH.md) dahulu. Setelah repositori
tujuan tersedia dan ditinjau, clone repositori itu lalu masuk ke akar proyek;
seluruh perintah berikut dijalankan dari akar salinan bersih tersebut. Tidak ada
URL repositori atau deployment yang dibuat pada tahap preparation ini.

## Instalasi penuh dan model OCR

PowerShell; aktivasi environment tidak wajib:

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements-web.txt
.venv/Scripts/python.exe -m pip check
.venv/Scripts/python.exe scripts/prepare_models.py
```

`requirements-web.txt` menyertakan `requirements-ocr.txt`, `requirements-pdf.txt`,
serta openpyxl. Versi utama yang dipertahankan: paddlepaddle 3.3.0, paddleocr
3.3.2, paddlex 3.3.13, Pillow 12.3.0, PyMuPDF 1.28.2, openpyxl 3.1.5.
Ini pin dependensi langsung, belum lock lengkap semua dependensi transitif.

Pemeriksaan model terakhir hanya membaca versi paket dan keberadaan berkas;
exit 1 berarti ada paket/model yang belum tersedia. Untuk menyiapkan model:

```powershell
.venv/Scripts/python.exe scripts/prepare_models.py --prepare
.venv/Scripts/python.exe scripts/prepare_models.py
```

Helper memanggil `PaddleOCR(**CONFIG)` dari mesin yang sudah ada, memilih sumber
model BOS, dan tidak menjalankan `predict` atau membaca faktur. Konfigurasi tetap
CPU, detektor `PP-OCRv5_server_det`, pengenal `latin_PP-OCRv5_mobile_rec`, serta
opsi asli di `uji_ocr_gambar.py`. Constructor dapat mengunduh dan memuat model;
siapkan model menggunakan akun OS yang juga menjalankan web. Ketersediaan berkas
saja tidak membuktikan inferensi berhasil. Dokumentasi constructor dan model:
[PaddleOCR 3.3.2](https://www.paddleocr.ai/v3.3.2/en/version3.x/pipeline_usage/OCR.html).

Lokasi default model adalah `<home pengguna>/.paddlex/official_models/<nama model>`
(Windows: `%USERPROFILE%/.paddlex/official_models`). Model dan cache tidak masuk
Git. Pada mesin tanpa internet, siapkan cache model dari sumber tepercaya dengan
versi yang sama pada home akun layanan, lalu periksa kembali. Jangan membawa
cache OCR pelanggan untuk menggantikan model. Helper tidak mengubah konfigurasi
mesin untuk memakai path/model lain.

## Menjalankan web dan penyimpanan

```powershell
.venv/Scripts/python.exe web_faktur.py --port 8765
```

Buka `http://127.0.0.1:8765`. Jika port dipakai, pilih misalnya `--port 8766`.
Gunakan satu server per lokasi hasil. Pilih maksimal 60 gambar **atau** 10 PDF,
tanpa campuran. Batas yang dipertahankan: 25 MB per file, 20 halaman per PDF.
Ekstraksi baru dimulai setelah menekan Mulai konversi. Ctrl+C menunggu pekerjaan
yang sudah diterima selesai. Startup pertama OCR dapat memuat/mengunduh model;
model sebaiknya disiapkan lebih dahulu.

Semua data operasional dibuat relatif terhadap akar sumber, bukan bergantung
pada lokasi `C:/zahir-invoice-ocr`:

| Lokasi | Isi |
| --- | --- |
| `outputs/web_lokal/persiapan/` | Unggahan sementara sebelum konversi |
| `outputs/web_lokal/jobs/<id>/` | `job.json`, manifest, unggahan, log worker |
| `jobs/<id>/cache_ocr/` | Cache OCR sesi |
| `jobs/<id>/run/` | Progres, hasil dokumen, ekspor, dan bukti audit internal |

Menu Data membaca hasil valid semua sesi web di lokasi itu, termasuk bagian
berhasil dari sesi parsial. Browser baru/server restart tidak menghapus hasil.
Salinan bersih mula-mula kosong; dataset batch 1-16 tidak diperlukan. Jangan
menyalin hasil pelanggan ke Git atau paket demo. Beberapa metadata ekspor memakai
path absolut; pemindahan direktori hasil memerlukan rencana migrasi/validasi,
bukan sekadar menyalin folder ke lokasi lain. Untuk hosting kelak, pertahankan
volume/path data dan izin akun layanan yang konsisten.

Untuk demonstrasi **fiktif** pada salinan bersih terpisah, tanpa OCR:

```powershell
.venv/Scripts/python.exe scripts/seed_demo.py
```

Seed membuat dua sesi sintetis berlabel fiktif (3 faktur, 7 detail), tidak
menimpa ID yang sudah ada. Jalankan hanya pada lingkungan demo terpisah supaya
tidak mencampurkan data uji dengan sesi operasional. Tidak perlu seed pada
workspace pelanggan. Tanpa paket/model OCR, demo hasil dan unduhan bisa dibuka,
tetapi konversi dokumen nyata tetap memerlukan instalasi penuh.

## Tes ringan tanpa OCR

Untuk CI atau memeriksa UI/unduhan dengan data sintetis saja:

```powershell
py -3.12 -m venv .venv-ci
.venv-ci/Scripts/python.exe -m pip install -r requirements-ci.txt
.venv-ci/Scripts/python.exe scripts/test_ci.py
node --check web_lokal/static/app.js
.venv-ci/Scripts/python.exe uji_alur_web_lokal.py --output .local/qa-demo
```

Tes browser memerlukan Node.js 24 dan Chrome/Chromium. Tetapkan environment
`CHROME_BIN` ke executable browser jika tidak ditemukan otomatis. Pada Linux,
gunakan `python3.12 -m venv .venv-ci` dan `.venv-ci/bin/python` untuk perintah
Python. Jangan gunakan penemuan semua `test_*.py` pada workspace penelitian:
sebagian tes historis memang masih bergantung pada data lokal. Runner terkurasi
menentukan tes yang aman untuk salinan bersih. Lihat [ruang lingkup CI](CI.md).
