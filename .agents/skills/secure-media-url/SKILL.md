---
name: secure-media-url
description: >-
  Use this skill when implementing media asset handling, image URL validation,
  SSRF defenses, temporary file uploads for connectors, or optional Cloudflare
  R2 custom domain integration.
---

# Secure Media URL & SSRF Protection Skill

Gunakan prosedur ini saat membangun pengelolaan aset media (`media_assets`), validasi URL gambar, integrasi Cloudflare R2, dan pengiriman media ke konektor platform di **Shopee Affiliate Content Studio**.

## 1. Kebijakan Tanpa Penyimpanan Video di VPS
1. VPS target (2 vCPU, 2 GB RAM, 60 GB disk) **tidak boleh** digunakan untuk menyimpan, mengunggah, mengonversi, atau merender file video.
2. Tolak input media bertipe `video/*` atau ekstensi video pada alur penyimpanan/unduhan server di MVP dengan pesan validasi yang jelas.
3. Untuk gambar, simpan hanya metadata di tabel `media_assets` (`canonical_url`, `mime_type`, `byte_size`, `width`, `height`, `sha256_checksum` opsional, `rights_note`, `storage_driver`), dan teruskan URL HTTPS publik langsung ke API platform bila endpoint platform mendukung penarikan via URL.

## 2. Integrasi Opsional Cloudflare R2 & Host Tepercaya
1. Konfigurasi Cloudflare R2 bersifat opsional dan seluruh kredensial (`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT`, `R2_BUCKET`) hanya boleh diakses di sisi backend.
2. Untuk lingkungan produksi (`APP_ENV=production`), wajibkan penggunaan `R2_PUBLIC_BASE_URL` dengan custom domain HTTPS (mis. `https://media.example.com`) dan **tolak** domain pengembangan `*.r2.dev` karena batasan rate-limit dan kebijakan produksi Cloudflare.
3. Pengguna tetap dapat memasukkan URL gambar HTTPS publik dari host eksternal yang valid tanpa mengaktifkan R2, selama URL tersebut lolos pemeriksaan SSRF.

## 3. Pertahanan SSRF Berlapis (`MediaUrlValidator`)
Setiap kali server perlu memvalidasi metadata gambar (HEAD/GET terbatas) atau mengunduh gambar sementara untuk konektor yang mewajibkan upload biner, wajib jalankan pemeriksaan berikut secara berurutan:
1. **Skema & Port:** Hanya izinkan skema `https://` pada port standar (`443` atau kosong). Tolak `http://`, `file://`, `ftp://`, `data://`, `gopher://`, serta URL yang mengandung kredensial (`user:pass@host`).
2. **Kebijakan Host & DNS Resolution:**
   - Tolak hostname `localhost`, `*.local`, `*.internal`, atau IP literal internal.
   - Lakukan resolusi DNS (`dns_get_record` / `gethostbynamel`) untuk seluruh record `A` (IPv4) dan `AAAA` (IPv6).
   - Validasi setiap IP hasil resolusi menggunakan `filter_var($ip, FILTER_VALIDATE_IP, FILTER_FLAG_NO_PRIV_RANGE | FILTER_FLAG_NO_RES_RANGE)` ditambah pemblokiran eksplisit untuk loopback (`127.0.0.0/8`, `::1`), link-local/cloud metadata (`169.254.0.0/16`, `fe80::/10`), CGNAT (`100.64.0.0/10`), multicast, dan unspecified (`0.0.0.0`, `::`). Jika ada satu saja IP yang melanggar, tolak URL tersebut.
3. **Perlindungan DNS Rebinding & Redirect Hop-by-Hop:**
   - Nonaktifkan auto-redirect bawaan HTTP client (`allow_redirects => false`).
   - Jika respons adalah redirect (`301`, `302`, `303`, `307`, `308`), batasi maksimal 3 hop, dan **validasi ulang** URL tujuan pada setiap hop menggunakan aturan #1 dan #2 sebelum mengikuti redirect.
   - Pin IP yang telah divalidasi pada opsi koneksi HTTP (`curl` `CURLOPT_RESOLVE` atau ekuivalen) bila melakukan fetch nyata agar mencegah serangan DNS rebinding antara waktu validasi dan waktu koneksi.
4. **Batas Waktu, Ukuran, dan MIME Sniffing:**
   - Tetapkan `connect_timeout` rendah (mis. 3 detik) dan `timeout` total rendah (mis. 8 detik).
   - Batasi jumlah byte maksimum yang dibaca (mis. maks 10 MB untuk gambar) dan hentikan stream segera jika melewati batas.
   - Jangan pernah mempercayai ekstensi URL atau header `Content-Type` semata; periksa *magic bytes* dari isi konten menggunakan `finfo_buffer` (`FILEINFO_MIME_TYPE`) dan hanya izinkan daftar putih gambar yang aman (`image/jpeg`, `image/png`, `image/webp`). Tolak `image/svg+xml` (risiko XSS/XXE), `text/html`, dan `video/*`.
5. **Larangan Open Proxy:** Jangan pernah membuat route/endpoint publik atau terautentikasi yang mem-proxy dan mengembalikan isi byte dari URL arbitrer ke browser pengguna.

## 4. Penanganan File Sementara untuk Konektor Upload
Jika dokumentasi resmi suatu konektor mengharuskan pengiriman file gambar secara multipart (bukan URL):
1. Unduh gambar menggunakan mekanisme aman di Bagian 3 ke file sementara di direktori privat (`storage/app/private/tmp-media/`) dengan nama acak.
2. Gunakan blok `try ... finally` agar file sementara **selalu dihapus** baik saat upload berhasil maupun saat terjadi exception/error.

## 5. Langkah Validasi (Gate Check)
Pastikan unit/feature test menguji dan membuktikan:
- URL `https://` gambar valid dengan magic bytes JPEG/PNG/WebP diterima.
- URL `http://` ditolak.
- URL dengan host `localhost`, `127.0.0.1`, `10.0.0.1`, `192.168.1.1`, `169.254.169.254`, dan `[::1]` ditolak sebelum koneksi keluar dilakukan.
- Redirect dari host publik yang sah menuju `http://127.0.0.1/admin` atau `https://169.254.169.254/latest/meta-data/` ditolak pada hop redirect.
- Respons yang melampaui batas ukuran byte atau memiliki isi byte bukan gambar (mis. HTML/SVG/video yang menyamar dengan ekstensi `.jpg`) ditolak.
- Domain `*.r2.dev` ditolak saat konfigurasi berada pada mode produksi.
