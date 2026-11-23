# Panduan Deployment VPS Ubuntu 24.04 (2 vCPU / 2 GB RAM)

Dokumen ini menjelaskan cara menyiapkan dan men-deploy **TikTok Auto-Pilot Bot** ke VPS Ubuntu 24.04 (2 vCPU, 2 GB RAM) menggunakan artefak di folder `deployment/`.

> [!NOTE]
> Seluruh konfigurasi dan skrip di `deployment/` sudah disiapkan dan divalidasi secara statis (`bash -n`), tetapi **belum dijalankan ke server VPS produksi** sampai Anda masuk via SSH dan mengeksekusi langkah di bawah ini.

## 1. Persiapan Sebelum SSH ke VPS

1. **Domain & DNS Cloudflare**:
   - Arahkan subdomain dashboard (mis. `bot.domainanda.com`) ke IP publik VPS (`A` record).
   - Siapkan **Cloudflare R2** bucket + **Custom Domain HTTPS** (mis. `https://media.domainanda.com`) untuk menyimpan sementara video `.mp4` hasil render. Mode produksi menolak domain dev `*.r2.dev`.
   - *(Opsional)* Siapkan **Cloudflare Images** API Token & Account ID untuk thumbnail.
2. **TikTok Developer Portal**:
   - Buat aplikasi di TikTok for Developers, aktifkan **Login Kit** & **Content Posting API**.
   - Daftarkan Redirect URI exact-match: `https://bot.domainanda.com/auth/tiktok/callback`.
   - Sebelum App Audit disetujui, hubungkan akun di dashboard dengan mode **Draft / Inbox Only**. Setelah audit disetujui TikTok, ubah akun ke **Direct Post**.
3. **API Keys AI & Bot**:
   - `GEMINI_API_KEY` (atau `OPENAI_API_KEY`) untuk riset konten FYP & pemilihan klip viral.
   - `GROQ_API_KEY` (atau `OPENAI_API_KEY`) untuk Whisper Cloud Transcription (0 MB RAM lokal).
   - `PEXELS_API_KEY` untuk stock video mode Faceless AI.
   - `TELEGRAM_BOT_TOKEN` & `TELEGRAM_ADMIN_CHAT_ID` untuk kontrol via Telegram (long-polling, tidak perlu webhook/tunnel).

## 2. Langkah Provisioning di VPS (Via SSH)

1. Login ke VPS Anda via SSH (`ssh root@IP_VPS`), lalu clone/salin folder proyek ke `/tmp/src-tiktok-bot`.
2. Jalankan skrip provisioning (memasang `ffmpeg`, `python3`, `caddy`, `ufw`, user `tiktokbot`, dan **swap 2 GB**):
   ```bash
   sudo bash /tmp/src-tiktok-bot/deployment/scripts/setup-vps.sh
   ```
3. Isi variabel rahasia produksi di `/etc/tiktok-bot/app.env` (file berada di luar repo dengan permission `640`):
   ```bash
   sudo nano /etc/tiktok-bot/app.env
   ```
   Pastikan `APP_ENV=production`, `DRY_RUN=false`, `APP_SECRET_KEY` diisi string acak kuat (minimal 32 karakter), serta seluruh key R2, AI, TikTok, dan Telegram terisi lengkap.
4. Jalankan deploy release pertama dengan domain Anda:
   ```bash
   sudo bash /tmp/src-tiktok-bot/deployment/scripts/deploy.sh /tmp/src-tiktok-bot bot.domainanda.com
   ```

## 3. Verifikasi & Operasi Harian

- **Cek status service & RAM**:
  ```bash
  sudo systemctl status tiktok-bot-web tiktok-bot-worker caddy
  free -h
  ```
- **Cek endpoint health**:
  ```bash
  curl -s https://bot.domainanda.com/health | python3 -m json.tool
  ```
  Pastikan `"active_temp_dirs": 0` saat tidak ada render aktif (membuktikan tidak ada penumpukan file lokal).
- **Rollback cepat jika update bermasalah**:
  ```bash
  sudo bash /opt/tiktok-bot/current/deployment/scripts/rollback.sh
  ```
