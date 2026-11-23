---
name: platform-integration
description: >-
  Use this skill when designing, implementing, or testing external platform
  connectors (Instagram, Facebook Page, Threads, TikTok, Shopee Affiliate,
  WhatsApp Channel/Status, and AI providers). Enforces official API verification,
  secure OAuth flows, honest capability states, no scraping, and mock contract tests.
---

# Platform Integration Skill

Gunakan prosedur ini setiap kali membuat atau memperbarui konektor platform eksternal di **Shopee Affiliate Content Studio**.

## 1. Verifikasi Dokumentasi Resmi & Pencatatan
1. Sebelum menulis kode konektor, verifikasi dokumentasi resmi terbaru untuk platform target (endpoint, versi API, jenis akun yang didukung, OAuth scopes, alur App Review/Audit, batas format media, rate limit, dan dukungan webhook/status query).
2. Catat hasil verifikasi beserta **tanggal pemeriksaan** di `docs/INTEGRATIONS.md`.
3. Jangan pernah mengarang endpoint atau mengasumsikan kemampuan lintas produk:
   - **Facebook MVP:** Hanya mendukung **Facebook Page** (bukan profil personal).
   - **Instagram:** Membutuhkan akun profesional (Business/Creator) yang terhubung sesuai syarat Meta Graph API.
   - **TikTok:** Gunakan Content Posting API resmi; akun/aplikasi yang belum lulus audit dipetakan sebagai `draft_only`, `pending_approval`, atau terbatas privat sesuai aturan TikTok. Jangan pasarkan sebagai auto-publish publik sebelum audit lulus.
   - **WhatsApp Status:** Wajib menggunakan alur berbagi manual (`manual` / Android Web Share API / salin caption & unduh/buka gambar), karena WhatsApp Cloud API tidak menyediakan publikasi otomatis ke WhatsApp Status pengguna.
   - **WhatsApp Channel:** Hanya aktifkan otomatis bila dokumentasi resmi dan akses akun membuktikan dukungan endpoint; jika belum, tetapkan sebagai `manual` atau `unavailable`.
   - **Shopee Affiliate:** Sediakan model domain netral untuk tautan produk, deeplink affiliate, komisi/kampanye, dan disclosure wajib. Dukung input manual penuh apabila akses API affiliate regional belum tersedia.
   - **Tren & Reply:** Dilarang keras melakukan web scraping atau otomasi browser. Konteks tren berasal dari URL yang dimasukkan pengguna atau API resmi yang diizinkan, dan setiap balasan wajib melewati persetujuan manual (`approvals`).

## 2. Kontrak Adapter Konektor (`ConnectorContract`)
Simpan semua logika spesifik platform di dalam adapter terpisah (mis. `app/Services/Connectors/`), jangan bocorkan ke Controller atau tampilan Blade. Setiap konektor wajib mengimplementasikan kontrak standar:
- `capabilities(): CapabilityManifest` — Mengembalikan status kapabilitas (`available`, `draft_only`, `manual`, `pending_approval`, `unavailable`), jenis media yang didukung (hanya gambar URL / upload terbatas, tanpa penyimpanan video di VPS), syarat scope OAuth, dukungan penjadwalan/reply, dan instruksi fallback manual.
- `authorize( array $context ): string` — Menghasilkan URL otorisasi OAuth dengan `state` kriptografis acak (disimpan di session/DB dengan TTL pendek), `redirect_uri` exact-match HTTPS, dan PKCE (`code_challenge` S256) bila didukung.
- `handleCallback / refresh / revoke` — Menukar authorization code, mengenkripsi `access_token` dan `refresh_token` di tabel `oauth_credentials`, mencatat `scopes` dan `expires_at`, serta mengubah status akun menjadi `needs_reconnect` bila refresh gagal atau token dicabut.
- `validatePayload( ScheduledDelivery $delivery ): ValidationResult` — Memeriksa panjang caption, keberadaan disclosure affiliate, kecocokan target kanal, dan kelayakan URL media sebelum request dikirim.
- `publish( ScheduledDelivery $delivery ): PublishResult` — Menjalankan pengiriman (atau mode `dry_run` / `sandbox` saat pengujian) dengan timeout ketat dan header/parameter idempotency bila didukung.
- `fetchStatus( ScheduledDelivery $delivery ): ReconciliationResult` — Memeriksa status eksternal untuk merekonsiliasi hasil pengiriman yang ambigu sebelum mengizinkan retry.
- `normalizeError( Throwable|Response $error ): NormalizedError` — Mengubah respons error provider menjadi kode internal yang aman (`retryable`, `permanent`, `auth_expired`, `rate_limited`, `manual_required`) dengan meredaksi seluruh token, secret, atau data sensitif.
- `health(): HealthStatus` — Memeriksa kelengkapan konfigurasi environment konektor tanpa membocorkan nilai secret.

## 3. Langkah Validasi (Gate Check)
Sebelum menyatakan pekerjaan konektor selesai, jalankan pemeriksaan berikut:
1. Pastikan setiap konektor memiliki contract test dengan HTTP fake/mock (`Http::fake()`) yang menguji skenario: sukses, rate limit (429), token kedaluwarsa (401/403 → `needs_reconnect`), timeout/5xx (status ambigu → butuh rekonsiliasi), dan fallback `manual`.
2. Pastikan tidak ada pemanggilan HTTP nyata ke provider eksternal selama `php artisan test`.
3. Periksa log dan respons test untuk memastikan `access_token`, `refresh_token`, `client_secret`, dan header `Authorization` tidak pernah tertulis.
