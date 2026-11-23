---
name: scheduler-reliability
description: >-
  Use this skill when implementing or modifying content scheduling, timezone
  handling (Asia/Jakarta to UTC), version-bound approvals, atomic queue claiming,
  delivery state machines, retries, and ambiguous publish reconciliation.
---

# Scheduler Reliability & Delivery State Machine Skill

Gunakan prosedur ini saat membangun atau mengubah penjadwalan konten, antrean pengiriman, persetujuan (approval), dan worker di **Shopee Affiliate Content Studio**.

## 1. Aturan Zona Waktu (`Asia/Jakarta` ↔ `UTC`)
1. Antarmuka pengguna (form jadwal, kalender, filter harian) selalu menampilkan dan menerima input dalam zona waktu pemilik (`Asia/Jakarta`, atau nilai timezone yang dipilih).
2. Saat menyimpan ke database (`scheduled_deliveries`), konversi waktu jadwal ke **UTC** pada kolom `scheduled_at` dan simpan string zona waktu asal (mis. `'Asia/Jakarta'`) pada kolom `timezone`.
3. Semua perbandingan waktu jatuh tempo pada scheduler (`scheduled_at <= now()->utc()`) wajib dilakukan dalam UTC.

## 2. Persetujuan Terikat Versi & Target (`Version-Bound Approvals`)
1. Semua konten (baik manual maupun AI draft) serta draft reply tren wajib memiliki record `approvals` yang aktif sebelum dapat dijadwalkan atau dipublikasikan.
2. Record `approvals` mengikat:
   - `content_item_id` dan `content_version` (integer yang bertambah setiap kali ada perubahan substansial)
   - `social_account_id` / kanal tujuan (atau `reply_draft_id`)
   - `approved_by`, `approved_at`, dan `invalidated_at` (nullable)
3. **Invalidasi Otomatis:** Jika pengguna mengubah isi teks/caption variasi kanal, URL media, link/disclosure affiliate, atau akun tujuan setelah approval diberikan:
   - Naikkan `version` pada `content_items`.
   - Set `invalidated_at = now()` pada semua approval terkait yang masih aktif.
   - Kembalikan status `scheduled_deliveries` yang belum terkirim (`scheduled` / `approved`) menjadi `needs_review` agar worker menolak mengeksekusi delivery tersebut sampai disetujui ulang.

## 3. Isolasi Delivery Multi-Kanal & Idempotency
1. Satu item konten yang dijadwalkan ke $N$ akun tujuan wajib menghasilkan $N$ baris independen di `scheduled_deliveries`.
2. Kegagalan, rate limit, atau pencabutan token pada satu akun tujuan tidak boleh membatalkan atau mengubah status delivery akun tujuan lainnya.
3. Setiap baris `scheduled_deliveries` wajib memiliki `idempotency_key` deterministik/unik (mis. hash atau UUID yang terikat pada konten, versi, akun tujuan, dan slot jadwal) beserta constraint unik di database untuk mencegah duplikasi saat klik ganda atau eksekusi scheduler berulang.

## 4. Siklus Status & Klaim Batch Atomik
Alur status standar:
`draft → needs_review → approved → scheduled → queued → publishing → published`
Status cabang:
`retry_wait`, `reconciling`, `failed_permanent`, `needs_reconnect`, `manual_action_required`, `cancelled`.

### Klaim Job Bebas Duplikat
1. Saat `php artisan schedule:run` berjalan setiap menit, ambil kandidat `scheduled_deliveries` yang berstatus `scheduled` (atau `retry_wait` yang `next_retry_at <= now()`) dan `scheduled_at <= now()`.
2. Gunakan transaksi database dengan row-level locking (`SELECT ... FOR UPDATE SKIP LOCKED` di PostgreSQL atau update atomik bersyarat `WHERE status = 'scheduled'`) untuk mengubah status menjadi `queued` sebelum mendorong job ke tabel queue.
3. Saat worker memproses job:
   - Periksa ulang di dalam transaksi bahwa status belum `cancelled`, tidak sedang `publishing` oleh worker lain, dan approval untuk `content_version` saat ini masih valid (`invalidated_at IS NULL`).
   - Ubah status menjadi `publishing` dan catat baris baru di `delivery_attempts`.

## 5. Penanganan Kegagalan & Larangan Retry Buta pada Hasil Ambigu
1. **Error Sementara yang Jelas** (mis. HTTP 429, koneksi ditolak sebelum payload dikirim, HTTP 502/503 dari gateway sebelum pemrosesan): jadwalkan ulang ke `retry_wait` dengan *exponential backoff + jitter* hingga batas maksimum upaya (`max_attempts`).
2. **Hasil Ambigu** (mis. request HTTP timeout setelah body terkirim, koneksi terputus di tengah publish):
   - **Dilarang langsung melakukan retry publish ulang secara buta** karena berisiko menghasilkan posting ganda di akun sosial.
   - Tandai delivery sebagai membutuhkan rekonsiliasi (`reconciling` atau flag `requires_reconciliation = true`).
   - Jika konektor mendukung `fetchStatus()`, jalankan pemeriksaan status eksternal terlebih dahulu. Jika posting ditemukan sudah terbit, ubah status menjadi `published` dan simpan `external_post_id` / `permalink`. Jika belum terbit dan aman, baru izinkan retry; jika konektor tidak mendukung pemeriksaan status otomatis, arahkan ke `manual_action_required` agar pemilik memeriksa kanal sebelum memicu retry manual.
3. **Error Permanen / OAuth:**
   - Token dicabut/kedaluwarsa dan gagal refresh → `needs_reconnect` + buat notifikasi.
   - Kanal hanya mendukung tindakan manual (mis. WhatsApp Status) → `manual_action_required`.

## 6. Langkah Validasi (Gate Check)
Pastikan feature/unit test mencakup:
- Konversi jadwal `Asia/Jakarta` ke UTC dan tampilan kembali ke `Asia/Jakarta`.
- Edit caption, media URL, atau disclosure setelah approve otomatis membatalkan approval dan mencegah publish.
- Memanggil perintah scheduler dua kali berturut-turut pada menit yang sama hanya mengantrekan 1 job per delivery (`idempotency`).
- Kegagalan pada 1 kanal dari 3 kanal tujuan tetap membiarkan 2 kanal lain mencapai status `published`.
- Simulasi timeout/hasil ambigu menahan retry buta hingga rekonsiliasi atau konfirmasi manual dilakukan.
