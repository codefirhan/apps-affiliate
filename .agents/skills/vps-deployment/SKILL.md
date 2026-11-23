---
name: vps-deployment
description: >-
  Use this skill when creating or validating VPS deployment artifacts, systemd
  units, Caddyfile, PHP-FPM/PostgreSQL memory tuning, cron scheduler configs,
  encrypted off-server backups, and rollback scripts for the 2 vCPU / 2 GB RAM Ubuntu target.
---

# VPS Deployment & Low-Memory Operations Skill

Gunakan prosedur ini saat menyiapkan artefak deployment, skrip operasional, backup, dan pemeriksaan kesehatan untuk target VPS Ubuntu (2 vCPU, RAM 2 GB, disk 60 GB) di **Shopee Affiliate Content Studio**.

## 1. Batasan Sumber Daya & Arsitektur VPS
1. **Jangan menjalankan eksekusi deploy ke server produksi nyata** kecuali ada instruksi eksplisit dan kredensial host yang disetujui pengguna. Buat seluruh file konfigurasi dan skrip secara lengkap di direktori `deployment/` dan dokumentasikan di `docs/DEPLOYMENT-VPS.md`.
2. **Hemat Memori (Target 2 GB RAM):**
   - Hanya jalankan proses inti: Caddy, PHP-FPM 8.3+, PostgreSQL 17/18 lokal, 1 proses queue worker Laravel via systemd (`concurrency=1`), dan cron untuk `schedule:run`.
   - **Dilarang** menambahkan Redis, Docker daemon berat, server Node.js/Vite yang terus aktif di produksi, worker video/FFmpeg, model AI lokal, atau stack monitoring berat (Prometheus/Grafana/ELK) pada VPS yang sama.
3. **Isolasi Jaringan & Pengguna:**
   - PostgreSQL wajib di-bind hanya ke `127.0.0.1` (`listen_addresses = 'localhost'`).
   - Firewall (`ufw`) hanya membuka port SSH (dibatasi/key-only), `80/tcp`, dan `443/tcp`.
   - Aplikasi dan worker berjalan sebagai user sistem non-root (mis. `affiliate`) dengan hak akses minimum.
   - File environment produksi berada di `/etc/apps-affiliate/app.env` (di luar repo dan document root) dengan permission `0600`.

## 2. Artefak Wajib di `deployment/`
Siapkan artefak konfigurasi statis yang siap pakai dan tervalidasi:
1. **Caddy (`deployment/caddy/Caddyfile`):**
   - HTTPS otomatis, reverse proxy `php_fastcgi`, mengarah ke `current/public`.
   - Blokir akses ke file tersembunyi (`.env`, `.git`), aktifkan security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Content-Security-Policy` yang sesuai), dan batasi ukuran body request.
2. **Systemd Queue Worker (`deployment/systemd/affiliate-worker.service`):**
   - Menjalankan `php artisan queue:work database --sleep=3 --tries=1 --max-time=3600 --memory=192` (retry dikelola secara eksplisit oleh domain/job agar aturan rekonsiliasi dipatuhi).
   - Gunakan `User=affiliate`, `Group=www-data`, `EnvironmentFile=/etc/apps-affiliate/app.env`, `Restart=always`, `RestartSec=5`, `MemoryMax=256M`, dan hardening systemd (`NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`).
3. **Cron Scheduler (`deployment/cron/apps-affiliate-scheduler`):**
   - Menjalankan `php /var/www/apps-affiliate/current/artisan schedule:run >> /dev/null 2>&1` setiap menit sebagai user `affiliate`.
4. **Tuning Konservatif (`deployment/config/`):**
   - Contoh tuning PostgreSQL untuk 2 GB RAM (`shared_buffers = 256MB`, `effective_cache_size = 768MB`, `work_mem = 4MB`, `max_connections = 30`).
   - Contoh pool PHP-FPM (`pm = dynamic`, `pm.max_children = 8`, `pm.start_servers = 2`, `pm.min_spare_servers = 1`, `pm.max_spare_servers = 3`, `pm.max_requests = 500`).
5. **Backup Terenkripsi & Restore Drill (`deployment/scripts/backup-db.sh` & `restore-drill.sh`):**
   - Menggunakan `pg_dump --format=custom`, mengenkripsi arsip (mis. `gpg --symmetric --cipher-algo AES256` atau `age` dengan kunci di luar direktori backup), lalu mengirimkannya ke penyimpanan off-server sebelum menghapus file lokal sementara.
   - Menyertakan skrip/prosedur *restore drill* ke database uji terisolasi untuk membuktikan backup dapat dipulihkan beserta verifikasi integritas tabel utama.
6. **Deploy & Rollback (`deployment/scripts/deploy-release.sh` & `rollback-release.sh`):**
   - Pola direktori `releases/<timestamp>`, symlink `storage` dan `/etc/apps-affiliate/app.env`, optimasi cache Laravel, migrasi aman, pergantian symlink `current` secara atomik, dan `systemctl reload php*-fpm` + `php artisan queue:restart`.
   - Peringatan jelas untuk tidak melakukan rollback skema database destruktif secara otomatis jika ada job pengiriman yang sedang berjalan.

## 3. Pemeriksaan Kesehatan & Observabilitas Ringan
1. Sediakan endpoint/command health check yang memeriksa: koneksi PostgreSQL, usia job tertua di tabel `jobs` (queue lag), detak jantung scheduler (`cache` / DB timestamp terakhir `schedule:run`), serta persentase ruang disk bebas.
2. Sediakan konfigurasi `logrotate` (`deployment/logrotate/apps-affiliate`) agar log tidak menghabiskan disk 60 GB.

## 4. Langkah Validasi (Gate Check)
1. Jalankan `bash -n` pada seluruh skrip `.sh` di `deployment/scripts/` untuk memastikan tidak ada kesalahan sintaks shell.
2. Pastikan automated test memeriksa endpoint/service health check (DB sehat, deteksi queue lag, deteksi scheduler stale).
3. Di laporan akhir, nyatakan secara jujur bahwa artefak deployment telah disiapkan dan divalidasi secara statis, tetapi **belum di-deploy ke server produksi**.
