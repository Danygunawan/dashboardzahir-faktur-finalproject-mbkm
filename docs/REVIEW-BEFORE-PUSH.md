# Tinjauan sebelum push atau deployment

Persiapan ini sepenuhnya lokal; tidak ada push, remote baru, atau deployment.
Dokumen ini mencatat pekerjaan yang harus ditinjau sebelum tahap berikutnya,
bukan menyatakan paket telah disetujui untuk publikasi.

## Temuan sumber awal

- Akar workspace saat pemeriksaan bukan repositori Git dan tidak memiliki
  index/riwayat/remote yang dapat diperiksa. Status file yang pernah dilacak
  **tidak diketahui**. Belum dapat memastikan tidak ada arsip, cache, model,
  credential, atau data pelanggan dalam riwayat repositori lain.
- Ada direktori lokal privat seperti `outputs`, `manifests`, `masukan_audit`,
  `.venv`, dan `.local`; semuanya dikecualikan. Model di home pengguna juga
  tidak masuk kandidat. Tidak ada file lokal tersebut dihapus atau dibersihkan.
- Kandidat berisi 70 file dari `demo-manifest.json`. `.gitignore` memakai
  allowlist: file baru perlu ditambahkan sengaja ke manifest dan pengecualian
  ignore. Termasuk 35 modul runtime dan aset PNG/font yang dipakai UI.
  `.gitattributes` mempertahankan byte Python agar fingerprint mesin tidak
  berubah akibat konversi newline checkout.
- README penelitian, laporan lama, sampel dokumen, dataset batch, dan tes
  historis yang membaca pelanggan tetap lokal, tidak disertakan dalam kandidat.
  `README.rst` dan `README-DEMO.md` adalah pintu masuk salinan demo bersih.
- Pola credential pada kandidat tidak menemukan kecocokan saat pemeriksaan
  awal. Ini pemeriksaan pola terbatas, bukan jaminan seluruh rahasia aman.
- **Masih perlu ditinjau:** `baca_pdf_digital.py`, konstanta `NAMA_DOKUMEN`,
  mempunyai contoh nama dokumen pelanggan lama. Beberapa helper lama juga
  mencantumkan path Windows. Mesin tidak diubah untuk memenuhi batas tahap ini.
  Jangan menyatakan kandidat bebas identitas pelanggan atau mempublikasikannya
  sebelum pemilik memutuskan visibilitas repo dan penanganan contoh tersebut.

## Sebelum push

1. Tentukan pemilik, URL, dan visibilitas repositori. Bila ada repositori lama,
   periksa index, remote, dan seluruh riwayatnya secara terpisah; laporan
   workspace tanpa `.git` tidak menggantikan audit itu.
2. Tinjau contoh nama dokumen dan path lama di sumber yang dibutuhkan runtime.
   Redaksi kelak perlu perubahan yang ditinjau dan diuji ulang karena byte
   mesin masuk fingerprint cache. Jangan menyertakan data penelitian sebagai
   jalan pintas memperbaiki tes.
3. Periksa `demo-manifest.json`, hasil `scripts/audit_source.py`, dan daftar
   staged file sebelum commit. `.gitignore` tidak mengeluarkan file yang sudah
   terlanjur dilacak; audit memblokir file terlacak di luar manifest.
4. Tinjau hak penggunaan logo, lisensi Inter yang disertakan, dan lisensi
   dependensi/model untuk lingkungan tujuan. Pin langsung belum menjadi lock
   transitif lengkap; tinjau resolusi dependensi instalasi penuh.
5. Jalankan tes terkurasi dan tinjau hasilnya. Push merupakan tindakan tahap
   berikutnya; workflow saja bukan bukti CI sudah berjalan.

Salinan uji bersih yang diberi index Git sementara hanya untuk menguji ignore
dan kandidat. Index itu bukan repositori tujuan, tidak mempunyai remote dan
tidak membuktikan riwayat workspace asli aman. Tidak ada commit/push dibuat.

## Sebelum deployment

Demo saat ini memakai HTTP server lokal yang bind ke `127.0.0.1`, antrean lokal,
dan penyimpanan filesystem; belum ada konfigurasi hosting produksi. Sebelum
memindahkannya ke layanan publik, rencanakan autentikasi/otorisasi akses dokumen,
TLS, batas sumber daya dan waktu OCR, akun layanan/cache model, volume hasil
persisten, backup/retensi data, serta perilaku shutdown/restart. Metadata hasil
yang memakai path absolut perlu validasi migrasi. Jangan membuka bind publik
atau menyalin hasil pelanggan dalam tahap preparation ini.

Instalasi penuh dan kesiapan model perlu dibuktikan di mesin tujuan dengan
dokumen sintetis yang disetujui. CI tanpa OCR tidak membuktikan akurasi ekstraksi
atau keberhasilan model. Lampirkan URL run CI yang benar-benar selesai dan
hasil pemeriksaan runtime tujuan pada penilaian, setelah tahap itu dilakukan.
