---
name: secret-hygiene
description: >-
  Use this skill whenever configuring environment variables, handling OAuth
  tokens, setting up logging/auditing, or performing gate reviews to ensure zero
  credential leakage in Git, logs, responses, or browser caches.
---

# Secret Hygiene & Credential Protection Skill

Gunakan prosedur ini di setiap fase untuk memastikan tidak ada secret, API key, atau token OAuth yang bocor ke repository, log, antarmuka pengguna, atau cache browser.

## 1. Aturan Environment & Repository
1. File `.env` wajib tercantum di `.gitignore` dan tidak boleh pernah dilacak oleh Git.
2. Hanya commit `.env.example` yang berisi daftar variabel sesuai `docs/SECURITY-AND-CONFIG.md` dengan nilai kosong atau placeholder yang jelas tidak valid (mis. `AI_API_KEY=`, `META_APP_SECRET=`, `R2_SECRET_ACCESS_KEY=`). Jangan pernah menaruh string acak panjang yang menyerupai key nyata di `.env.example` atau file test.
3. Pada lingkungan produksi VPS, secret di-inject melalui file di luar repository dan di luar document root (`/etc/apps-affiliate/app.env` dengan pemilik user aplikasi dan permission `0600`) atau secret manager.

## 2. Penyimpanan Token OAuth di Database
1. Semua kolom sensitif pada `oauth_credentials` (`access_token`, `refresh_token`, atau payload rahasia ekstra) wajib dienkripsi pada level aplikasi menggunakan cast enkripsi Laravel (`'encrypted'`).
2. Tambahkan atribut sensitif tersebut ke properti `$hidden` pada Model Eloquent agar tidak pernah ikut ter-serialisasi ke JSON, array, Livewire/Alpine state, atau respons API.
3. Simpan hanya metadata non-rahasia dalam bentuk teks biasa (`scopes`, `expires_at`, `token_type`, `last_refreshed_at`).

## 3. Redaction pada Log, Audit Event, dan Delivery Attempt
1. Tabel `delivery_attempts` dan `audit_events` maupun file log (`storage/logs/laravel.log`) hanya boleh mencatat:
   - `correlation_id`, `scheduled_delivery_id`, nama konektor, kode status HTTP/provider, durasi eksekusi, dan pesan error yang sudah disanitasi (`normalized_error`).
2. Buat helper/formatter sanitasi yang secara otomatis menghapus atau mengganti (`[REDACTED]`) pola berikut sebelum dicatat ke DB atau log:
   - Header `Authorization`, `X-Api-Key`, `Cookie`, `Set-Cookie`
   - Parameter query bertanda tangan seperti `X-Amz-Signature`, `X-Amz-Credential`, `access_token`, `client_secret`, `code`
   - Nilai environment secret yang sedang terkonfigurasi bila tidak sengaja terpantul di pesan exception HTTP client.
3. Jangan pernah mengirim token OAuth sosial, secret internal, atau PII yang tidak relevan ke dalam prompt provider AI eksternal.

## 4. Keamanan Browser & Service Worker PWA
1. Jangan pernah merender secret server (`AI_API_KEY`, `R2_SECRET_ACCESS_KEY`, `META_APP_SECRET`, token OAuth) ke dalam atribut HTML/Alpine `x-data`, script inline, atau meta tag.
2. Konfigurasikan Service Worker PWA (`public/sw.js`) agar **hanya** meng-cache aset statis publik (CSS, JS, ikon, font, dan halaman fallback offline statis). Jangan meng-cache respons HTML yang memerlukan autentikasi, endpoint OAuth, atau respons JSON yang memuat data akun.

## 5. Langkah Validasi (Gate Check)
Sebelum menutup setiap fase, jalankan pemeriksaan berikut:
1. Pastikan `.env` tidak muncul sebagai file tracked/staged di `git status -- .`.
2. Pindai direktori proyek untuk mendeteksi pola credential yang tidak sengaja tertulis (mis. `sk-`, `ghp_`, `EAAB`, `AKIA`, `BEGIN PRIVATE KEY`, atau nilai secret non-kosong di `.env.example`).
3. Buat unit/feature test yang memverifikasi bahwa:
   - `OAuthCredential` mengenkripsi `access_token` saat disimpan ke DB mentah (`DB::table('oauth_credentials')->first()->access_token !== $plainToken`).
   - Serialisasi `OAuthCredential::toArray()` menyembunyikan `access_token` dan `refresh_token`.
   - Kegagalan HTTP konektor yang mengandung token di URL/header disanitasi menjadi `[REDACTED]` pada `delivery_attempts.error_message` dan `audit_events`.
