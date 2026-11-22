# AI Document Processing & Security Service

Submission tes Software Engineer (AI/ML Focus) — Surya Ramadhani.

FastAPI menerima PDF, PNG, dan JPG. RQ memakai Redis untuk menjalankan OCR Tesseract local, redaksi PII, klasifikasi, dan penyimpanan hasil ke PostgreSQL. PostgreSQL menyediakan search full-text, audit, dan risk level.

## Instalasi dan menjalankan aplikasi

Siapkan **Python 3** di komputer dan **Docker dengan Compose v2**. Di macOS dan Windows, buka Docker Desktop. Di Windows, gunakan mode Linux containers. Di Linux, pastikan Docker Engine sudah berjalan dan plugin Compose tersedia.

Buka Terminal atau PowerShell di folder yang berisi `compose.yaml`. Jalankan `docker compose version` untuk mengecek Compose. Build pertama membutuhkan koneksi internet untuk mengunduh image dan dependensi.

**macOS dan Linux**

```sh
python3 setup_demo.py
docker compose up --build -d --wait
```

**Windows PowerShell**

```powershell
py -3 setup_demo.py
docker compose up --build -d --wait
```

Skrip setup membuat `.env` jika belum ada. File tersebut berisi password demo, JWT auth, dan encription key. Simpan file ini secara lokal dan jangan commit ke Git. Pertahankan `FILE_KEY` selama masih menggunakan volume dokumen yang sama.

Buka [Swagger UI](http://localhost:8000/docs). Klik **Authorize**, masukkan username `demo` dan nilai `DEMO_PASSWORD` dari `.env`, lalu klik **Authorize → Close**. Biarkan `client_id` dan `client_secret` kosong. Token berlaku 60 menit sejak diterbitkan.

Jika port 8000 dipakai, jalankan perintah berikut dan buka [Swagger UI port 8001](http://localhost:8001/docs).

```sh
API_PORT=8001 docker compose up --build -d --wait
```

Di PowerShell, jalankan `$env:API_PORT = '8001'` sebelum perintah Compose. Untuk menghentikan aplikasi, jalankan `docker compose down`. Volume data tetap tersedia saat aplikasi dijalankan kembali.

## Demo dengan Swagger

1. Upload [invoice sintetis](examples/invoice.pdf) melalui `POST /api/documents/upload`. Salin ID dari respon di swagger.
2. Jalankan `POST /api/documents/analyze/{id}`. Respons 202 menunjukkan analisis diproses asinkron. Panggilan ulang mengembalikan job yang sama.
3. Panggil `GET /api/documents/{id}` sampai status `completed`. Contoh ini diharapkan berjenis `financial`, berisiko `high`, dan teks memuat `[NIK]` serta `[EMAIL]`.
4. Cari `q=invoice&risk=high` lewat `GET /api/documents/search`, lalu cek `GET /api/documents/risk-report`.
5. Lakukan soft delete lewat `DELETE /api/documents/{id}`. Dokumen hilang dari detail, pencarian, dan laporan.

## Verifikasi

```sh
python3 -m unittest discover -s tests -v
python3 tests/smoke.py
docker compose exec -T db psql -U documents -d documents -c 'SELECT job_id, attempts, error_code FROM dlq_records;'
docker compose exec -T redis redis-cli XRANGE documents-dlq - +
docker compose logs worker
```

Di Windows, ganti `python3` dengan `py -3`. Jika demo menggunakan port 8001, jalankan `API_PORT=8001 python3 tests/smoke.py` di macOS/Linux, atau set `$env:API_PORT = '8001'` sebelum menjalankan `py -3 tests/smoke.py` di PowerShell.

Smoke test menguji autentikasi, validasi MIME dan ukuran, OCR PDF, redaksi, pengajuan analisis ulang, pencarian, agregasi, soft delete, serta kegagalan dengan PDF sintetis yang korup. Barcode dan tulisan tangan berada di luar cakupan OCR demo.

## Penyimpanan dan pemrosesan

- API membandingkan MIME yang dideklarasikan dengan signature PDF atau format hasil verifikasi Pillow. Batasnya 5 MiB per file, 20 megapiksel per gambar, dan 12 halaman per PDF. Upload multipart memerlukan Content-Length. Parser PDF berjalan di worker non-root dengan batas CPU, memori, proses, dan waktu.
- File asli disimpan terenkripsi dengan Fernet pada volume privat bernama, diakses lewat UUID internal. API dan worker memakai direktori sementara berbasis memori untuk upload multipart dan OCR. Nama file asli dan teks mentah OCR tidak dicatat. API hanya mengembalikan teks teredaksi sebagai JSON.
- Analisis memakai tiga retry setelah percobaan awal. RQ menyimpan antrean di Redis. Kegagalan terakhir dicatat di `dlq_records` dan stream `documents-dlq`. Audit merekam status dan kode error generik. Lock per dokumen mencegah duplikasi trigger, dan transaksi terakhir menyimpan teks, temuan, risiko, serta status secara atomik.
- Kategori ditentukan dengan kata kunci. Confidence PII merupakan skor heuristik. Deteksi nama berfokus pada label seperti `Nama:`, dan nomor kartu menggunakan Luhn. Hasil OCR, variasi format, dan nama dalam narasi bisa menyebabkan temuan terlewat. Gunakan hanya dokumen sintetis pada demo.
- Pengguna demo tunggal menggunakan JWT HS256. Pembatasan laju dan pemeriksaan kepemilikan berlaku pada endpoint dokumen. API dipublikasikan ke loopback, Redis dan PostgreSQL tetap di jaringan internal Compose. Soft delete mempertahankan file terenkripsi serta audit untuk retensi demo.
- Deploy ini memakai satu host. Status di PostgreSQL dan antrean Redis bisa berbeda jika proses atau salah satu datastore berhenti di tengah penulisan. Untuk deployment produksi, gunakan transactional outbox dan proses rekonsiliasi untuk job yang tertunda dan pengiriman DLQ. Ganti akun demo dengan identitas organisasi, gunakan TLS, pengelolaan kunci dan rotasi, retensi/purge otomatis, serta isolasi parser lebih ketat.
