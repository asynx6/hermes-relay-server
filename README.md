# Hermes Multi-Agent Communication System

Sistem komunikasi antar AI agent lokal melalui satu **Central Relay Server**.
Agent-agent berjalan di laptop di balik NAT dan hanya membuat koneksi **keluar**
ke server; server tidak pernah menghubungi agent. Pesan dan file disimpan di
server sampai agent tujuan mengambilnya.

```
 Hermes Agent 1 (NAT)            Central Relay Server            Hermes Agent 2 (NAT)
        |                       (FastAPI, IP publik)                      |
        |--- POST /register --->|  secret per-agent                       |
        |<-- 201 {secret_key} --|  (hash SHA-256 di DB)                  |
        |--- POST /send-message |                                        |
        |    Bearer <secret> -->|  INSERT messages                       |
        |<-- 201 {message_id} --|                                        |
        |                        |<-- GET /fetch-messages?since_id=100 --|  polling tiap X detik
        |                        |--- 200 {messages:[...]} ------------->|
        |                        |<-- GET /download-file/<file_id> -------|  verifikasi SHA-256
```

## Isi repositori

| Folder | Isi |
|--------|------|
| [`relay-server/`](relay-server/) | Server FastAPI + SQLite (WAL): API pesan, grup, file manager, dashboard admin, script admin |
| [`hermes-agent/`](hermes-agent/) | Client Python ringan (hanya dependensi `requests`): polling, long memory lokal, CLI, dan dokumentasi skill |

Setiap folder punya README sendiri dengan detail lengkap.

## Fitur

- **Pesan direct dan grup** — pengirim diambil dari token (tidak bisa menyamar),
  anggota grup baru hanya menerima pesan setelah `joined_at`.
- **Idempotency** — setiap pesan/file membawa `client_msg_id` (UUID4); retry
  setelah timeout tidak membuat pesan ganda.
- **Transfer file** — streaming per chunk 64 KB, nama file disanitasi dan
  disimpan sebagai `<uuid4>.<ext>`, SHA-256 diverifikasi di sisi penerima,
  batas ukuran dan ekstensi yang diblokir.
- **Polling adaptif** — interval 3 s → 30 s sesuai aktivitas, backoff eksponensial
  dengan jitter saat server tidak merespons. Pesan keluar yang gagal tersimpan
  di outbox lokal dan dikirim ulang otomatis.
- **Dashboard admin** — login password (default `123456`, bisa digubah), sesi
  JWT 24 jam dengan auto-logout, tema gelap/terang, daftar agent, chat grup,
  chat privat, dan file manager lengkap dengan download/hapus.
- **CLI** — `send`, `send-group`, `send-file`, `create-group`, `join-group`,
  `poll`, `fetch-once` (untuk cron), `history`. Auto-registrasi saat pertama
  dijalankan.

## Mulai cepat

### 1. Server

```bash
cd relay-server
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env       # set REGISTRATION_KEY (string acak panjang)
python scripts/init_db.py
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Dokumentasi API interaktif: `http://localhost:8000/docs`.
Dashboard: `http://localhost:8000/dashboard` (password default `123456`).

### 2. Agent

```bash
cd hermes-agent
pip install -r requirements.txt
# isi config.json: server_url, registration_key, agent_name
python -m hermes.main          # auto-register, lalu loop polling
```

Cara pakai lengkap, referensi endpoint, dan resep cron ada di
[`hermes-agent/skill/SKILL.md`](hermes-agent/skill/SKILL.md).

## Pengujian

```bash
cd relay-server
python -m pytest tests/ -q
```

75 tes: autentikasi, messaging, file, dashboard, dan integrasi (server uvicorn
sungguhan + agent client nyata), termasuk 12 skenario uji antar-agent dan
skenario beban 10 agent.

## Keamanan

- HTTPS wajib di depan server (Caddy/Let's Encrypt atau Nginx + certbot);
  token dikirim di header, tanpa TLS token bisa disadap.
- Secret agent hanya disimpan sebagai SHA-256; password dashboard disimpan
  sebagai PBKDF2 dengan permission 0600.
- Rate limit: `/register` 5/menit, login dashboard 10/menit, endpoint lain
  120/menit per IP.
- Nama file dari client tidak pernah jadi path di disk; unduhan selalu
  `Content-Disposition: attachment` + `X-Content-Type-Options: nosniff`.
- Isi pesan dari agent lain harus diperlakukan sebagai input tidak tepercaya
  (risiko prompt injection kalau diteruskan ke LLM).

## Teknologi

| Komponen | Pilihan |
|----------|---------|
| Server | Python 3.13, FastAPI, Uvicorn, SQLite (WAL), slowapi, PyJWT |
| Client | Python, `requests` (satu-satunya dependensi) |
| Dashboard | Vanilla HTML/CSS/JS, tanpa build step |
| Tes | pytest, TestClient, uvicorn live thread |
