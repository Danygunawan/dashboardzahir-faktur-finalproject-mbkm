# CI tanpa data pelanggan atau OCR nyata

Workflow `.github/workflows/ci.yml` disiapkan untuk `push`, `pull_request`, dan
`workflow_dispatch`. Tidak ada langkah deployment, unggah data uji, secret
aplikasi, atau download model. Hak token `contents: read`, credential checkout
tidak dipertahankan. Versi action diperiksa terhadap dokumentasi resmi saat
persiapan: [checkout](https://github.com/actions/checkout),
[setup-python](https://github.com/actions/setup-python),
[setup-node](https://github.com/actions/setup-node).

## Pekerjaan workflow

| Job | Lingkungan | Pemeriksaan |
| --- | --- | --- |
| `synthetic-tests` | Ubuntu dan Windows, Python 3.12, Node 24 | Audit index Git, 34 unit/integrasi HTTP terkurasi, sintaks JS |
| `browser-demo` | Ubuntu, Python 3.12, Node 24, browser runner | Tiga layar, Home/Data, Kembali, tema desktop/ponsel, batas, unduhan dan restart |

`requirements-ci.txt` hanya memerlukan Pillow, PyMuPDF, openpyxl, et-xmlfile.
Paddle/model tidak dipasang. `scripts/test_ci.py` juga memblokir impor
Paddle/PaddleOCR/PaddleX dan pemanggilan ekstraksi nyata. Worker pada tes yang
memerlukan progres diganti fixture deterministik. Tes browser memakai subclass
worker fiktif, bukan endpoint tambahan pada aplikasi produksi.

`tests/support.py` membuat dokumen kecil dan sesi fiktif di direktori uji baru.
Dua sesi sengaja mempunyai ID faktur yang sama, nol awal, nilai kosong/nol,
review, dan status parsial. Pemeriksaan mencakup hubungan komposit sesi-faktur,
agregasi tanpa duplikasi, XLSX dengan dua sheet dan identitas string, ZIP dua CSV,
penolakan campuran/60-61 gambar/10-11 PDF, serta persistensi restart. Tes
preparation memeriksa sumber bersih, allowlist Git, penolakan penimpaan ekspor,
dan deteksi paksa file rahasia sintetis; nilainya tidak dicetak.

Jalankan lokal:

```powershell
python scripts/test_ci.py
python scripts/audit_source.py --check
node --check web_lokal/static/app.js
node --check web_lokal/static/theme.js
node --check uji_tampilan_web_lokal.mjs
python uji_alur_web_lokal.py --output .local/qa-demo
```

Gunakan interpreter environment dengan `requirements-ci.txt`. Audit `--check`
exit 0 hanya bila ada repository/index pada akar yang diperiksa dan tidak ada
temuan kritis pada kandidat/index. Exit 1: temuan kritis; exit 2: metadata Git
tidak tersedia. Audit tidak membuktikan riwayat remote bebas data sensitif.
Pada ZIP sumber tanpa `.git`, gunakan tanpa `--check` untuk laporan; status
pelacakan tetap tidak diketahui. Tes lain tetap dapat dijalankan dari ZIP.

## Status dan batas bukti

Pada tahap preparation 8 November 2026, hasil yang tersedia adalah **tes lokal**
Windows dengan environment baru dan data sintetis. Workflow belum pernah
dipicu pada repositori tujuan: belum ada URL run, badge hijau, bukti CI Ubuntu,
atau deployment yang dapat dilaporkan. Laporan lokal rinci berada di
`.local/preparation/` workspace pengembangan dan tidak disertakan di Git.

Setelah peninjauan sumber dan push yang diotorisasi terpisah, periksa kedua job
dan kedua OS di tab Actions repositori, catat commit SHA serta URL run sukses
untuk bukti penilaian. Kegagalan runner/dependensi harus diselesaikan sebelum
menyatakan CI berhasil. CI ini memverifikasi kontrak web dan pengemasan hasil;
belum menguji akurasi OCR, instalasi OCR penuh pada mesin baru, ketersediaan
model offline, beban paralel, maupun kesiapan hosting publik.
