# TikTok Auto-Pilot Bot

Aplikasi bot otomasi konten TikTok **bilingual (Indonesia & English)** dengan dua mode pipeline dan **zero persistent local media storage**:

1. **YouTube Viral Clipper**: Riset video viral YouTube (`yt-dlp` / YouTube Data API) -> AI memilih segmen hook terbaik -> unduh potongan video saja (`--download-sections`) -> reframe `1080×1920` (9:16) -> subtitle `.ass` + opsional **auto-translate & dubbing**.
2. **Faceless AI Video**: Riset topik FYP & skrip AI -> fetch stock video portrait (Pexels) -> dubbing (`edge-tts` / ElevenLabs) + subtitle `.ass` + judul overlay.
3. **Zero Local Storage**: Setiap job memproses media di `/tmp/tiktok-bot/jobs/<job_id>/`, langsung mengunggah MP4 ke **Cloudflare R2** (dan thumbnail ke **Cloudflare Images**), lalu langsung menghapus folder temp lokal di blok `finally`. Opsional auto-hapus objek R2 setelah sukses ter-publish ke TikTok.
4. **Full Auto-Pilot + Multi-Akun TikTok API**: OAuth 2.0 PKCE terenkripsi Fernet (`Draft / Inbox Only` sebelum App Audit, `Direct Post` setelah App Audit), scheduler `Asia/Jakarta` -> `UTC`, dan kontrol/notifikasi via **Web Dashboard** + **Telegram Bot** (long polling).

## Menjalankan Lokal (Mac / Dev Mode)

1. Aktifkan virtualenv & install dependensi:
   ```bash
   uv venv -p 3.12 .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   ```
2. Salin `.env.example` ke `.env` dan sesuaikan konfigurasi (default `DRY_RUN=true` aman untuk simulasi tanpa API berbayar):
   ```bash
   cp .env.example .env
   ```
3. Jalankan Web Dashboard dan Worker di dua terminal terpisah:
   ```bash
   .venv/bin/uvicorn app.main:app --reload --port 8000
   .venv/bin/python -m app.worker
   ```
   Buka `http://localhost:8000`.

## Menjalankan Tes Otomatis

```bash
.venv/bin/pytest -v
```

## Deployment ke VPS (2 vCPU / 2 GB RAM)

Lihat panduan lengkap di [docs/DEPLOYMENT-VPS.md](docs/DEPLOYMENT-VPS.md).
