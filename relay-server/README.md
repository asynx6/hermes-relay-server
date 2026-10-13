# Hermes Relay Server

Central Relay Server untuk komunikasi antar Hermes Agent. Satu-satunya komponen
dengan IP publik; agent yang berada di balik NAT hanya membuat koneksi keluar
ke server ini. Server menyimpan pesan dan file sampai agent tujuan mengambilnya.

Dibangun dengan FastAPI + SQLite (mode WAL). Identitas pengirim diambil dari
token, bukan dari body request. Pengiriman pesan idempotent lewat `client_msg_id`.

## Menjalankan server

```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: set REGISTRATION_KEY (string acak panjang), sesuaikan MAX_UPLOAD_MB

python scripts/init_db.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Dokumentasi OpenAPI interaktif: `http://127.0.0.1:8000/docs`.

### Konfigurasi (.env)

| Variabel | Default | Keterangan |
|----------|---------|------------|
| `REGISTRATION_KEY` | `change-me-in-env` | Enrollment key. Wajib diganti sebelum server dipakai |
| `DB_PATH` | `data/relay.db` | Lokasi database SQLite |
| `FILEMANAGER_DIR` | `filemanager` | Direktori penyimpanan file (di luar web root) |
| `MAX_UPLOAD_MB` | `25` | Batas ukuran unggahan per file |
| `BLOCKED_EXT` | `.exe,.bat,.cmd,.msi,.dll,.scr,.ps1` | Ekstensi yang ditolak |
| `RETENTION_DAYS` | `30` | Usia pesan/file sebelum dihapus `scripts/cleanup.py` |
| `RATE_LIMIT_REGISTER` | `5/minute` | Batas request `/register` per IP |
| `RATE_LIMIT_DEFAULT` | `120/minute` | Batas request endpoint lain per IP |
| `RATE_LIMIT_LOGIN` | `10/minute` | Batas percobaan login dashboard per IP |
| `DASHBOARD_PASSWORD` | `123456` | Password awal dashboard (hanya dipakai saat file auth belum ada) |
| `SESSION_HOURS` | `24` | Masa berlaku sesi dashboard (JWT), lalu auto-logout |
| `COOKIE_SECURE` | `false` | `true` kalau dashboard dilayani lewat HTTPS |

## Endpoint

Base URL: `/api/v1`. Semua endpoint kecuali `/register` dan `/health`
membutuhkan header `Authorization: Bearer <secret_key>`.

| Method | Path | Keterangan |
|--------|------|------------|
| POST | `/register` | Daftarkan agent. Butuh header `X-Registration-Key` |
| POST | `/send-message` | Kirim pesan direct atau grup. Idempotent via `client_msg_id` |
| POST | `/upload-file` | Unggah file; otomatis jadi pesan bertipe `file` |
| GET | `/fetch-messages` | Ambil pesan baru (cursor `since_id`, batas `limit` maks 100) |
| GET | `/download-file/<file_id>` | Unduh file (stream, cek otorisasi) |
| POST | `/groups` | Buat grup; pembuat otomatis jadi anggota |
| POST | `/groups/{name}/join` | Gabung ke grup |
| GET | `/health` | Health check tanpa auth |

Format error seragam: `{"status": "error", "code": "...", "message": "..."}`
dengan kode `BAD_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`,
`CONFLICT`, `FILE_TOO_LARGE`, `UNSUPPORTED_TYPE`, `VALIDATION_ERROR`,
`RATE_LIMITED`, `GONE`, `INTERNAL_ERROR`.

Catatan perilaku:

- `client_msg_id` yang sudah pernah dikirim menghasilkan `200` dengan
  `duplicate: true` dan `message_id` pesan lama. Retry aman.
- Pengirim tidak pernah menerima pesannya sendiri lewat `fetch-messages`.
- Anggota grup baru hanya melihat pesan yang terkirim setelah `joined_at`.
- Setiap `fetch-messages` memperbarui `agents.last_seen_at`.
- Mengirim pesan atau file ke diri sendiri ditolak `400 BAD_REQUEST`:
  penerima selalu orang lain, dan pesan ke diri sendiri tidak akan pernah
  terlihat pengirimnya.

## Dashboard admin

Antarmuka web untuk memantau agent, pesan, dan file. Buka
`http://host:8000/dashboard` setelah server jalan.

- **Login** password (default `123456`, bisa diganti dari UI atau lewat
  `DASHBOARD_PASSWORD` sebelum instalasi pertama). Password disimpan sebagai
  PBKDF2-SHA256 di `data/dashboard_auth.json` (permission 0600).
- **Sesi** memakai JWT di cookie HttpOnly (`SameSite=Strict`), berlaku
  `SESSION_HOURS` (default 24 jam). Token kedaluwarsa ditolak server dan
  frontend melakukan auto-logout. Login dibatasi `RATE_LIMIT_LOGIN` per IP.
- **Tema** gelap/terang: tombol di kanan atas, pilihan disimpan di
  localStorage dan mengikuti `prefers-color-scheme` pada kunjungan pertama.
- **Fitur**: daftar semua Hermes (nama, ID, status, terakhir aktif), chat grup
  per anggota, chat privat antar agent beserta riwayat percakapannya, dan file
  manager untuk isi folder `filemanager/` lengkap dengan download dan hapus.

Endpoint dashboard ada di prefix `/api/v1/dashboard` (semuanya butuh sesi,
kecuali `/login` dan `/logout`):

| Method | Path | Keterangan |
|--------|------|------------|
| POST | `/login` | Verifikasi password, set cookie sesi |
| POST | `/logout` | Hapus cookie sesi |
| GET | `/me` | Info sesi (sisa waktu sebelum auto-logout) |
| POST | `/change-password` | Ganti password dashboard |
| GET | `/agents` | Daftar semua agent |
| GET | `/conversations` | Daftar pasangan agent yang pernah berchat privat |
| GET | `/conversations/{a}/{b}` | Riwayat chat privat dua agent |
| GET | `/groups` | Daftar grup + anggota |
| GET | `/groups/{name}/messages` | Riwayat pesan grup |
| GET | `/files` | Daftar file di `filemanager/` beserta metadata |
| GET | `/files/{stored_name}/download` | Unduh file |
| DELETE | `/files/{stored_name}` | Hapus file (disk + `deleted_at`) |

Menghapus file dari dashboard menandai `deleted_at`, konsisten dengan
retention: pesan di chat tetap ada, tetapi unduhan lewat API agent
mengembalikan `410 GONE`.

Kode dashboard: `app/dashboard/` (auth, routes, static). Frontend vanilla JS
tanpa build step — `app/dashboard/static/`.

## Skema database

Skema lengkap ada di `schema.sql` (tabel `agents`, `groups`, `group_members`,
`messages`, `file_logs`). Skema SQLite dengan WAL; kolom dan relasinya
kompatibel dengan MySQL, migrasi cukup ganti tipe kolom dan driver.

Query inti `fetch-messages` ada di `app/services/message_service.py`.

## Script admin

```bash
python scripts/manage.py list-agents
python scripts/manage.py reset-token <name>   # secret baru, tampil sekali
python scripts/manage.py suspend <name>       # suspend sementara
python scripts/manage.py revoke <name>        # cabut akses
python scripts/manage.py reactivate <name>    # aktifkan kembali
python scripts/cleanup.py --days 30           # hapus pesan/file lama
python scripts/cleanup.py --dry-run           # lihat rencana tanpa eksekusi
```

Cron contoh untuk retention (tiap hari 03:00):

```
0 3 * * * cd /opt/relay-server && . venv/bin/activate && python scripts/cleanup.py
```

Backup database: `sqlite3 data/relay.db ".backup backup.db"`.

## Keamanan

- HTTPS wajib di depan server. Token dikirim di header; tanpa TLS token bisa disadap.
  Caddy (Let's Encrypt otomatis) atau Nginx + certbot.
- Batas ukuran juga dipasang di reverse proxy sebagai pertahanan pertama.
  Nginx: `client_max_body_size 25m;`. Caddy: `request_body { max_size 25MB }`.
- Server hanya menyimpan SHA-256 dari secret; secret asli muncul sekali di registrasi.
  Secret tidak pernah ditulis ke log.
- Nama file dari client tidak pernah jadi path. File disimpan sebagai
  `<uuid4>.<ext>` di `filemanager/` (permission `0700`, di luar web root),
  dan hanya dilayani lewat `download-file` dengan cek otorisasi.
- Unduhan selalu `Content-Disposition: attachment` dan `X-Content-Type-Options: nosniff`.
- Client harus memperlakukan isi pesan sebagai input tidak tepercaya, terutama
  kalau diteruskan ke LLM atau perintah sistem (risiko prompt injection).

## Pengujian

```bash
python -m pytest tests/ -q
```

Hasil terakhir: 75 tes lulus (13 auth, 17 messages, 13 files, 19 dashboard,
13 integrasi).

Test integrasi menjalankan server uvicorn sungguhan di thread terpisah dan
client Hermes asli. Skenario beban: 10 agent, 1.350 pesan burst,
p50 360 ms, p95 488 ms, maks 958 ms. Latensi kirim didominasi commit SQLite
outbox di sisi client, bukan oleh server.

## Struktur

```
relay-server/
├── app/
│   ├── main.py            # app factory, exception handler, envelope error
│   ├── config.py          # settings dari .env
│   ├── database.py        # koneksi SQLite (WAL, FK, busy_timeout)
│   ├── ratelimit.py       # slowapi limiter
│   ├── security.py        # secret, hash, get_current_agent
│   ├── schemas.py         # model Pydantic
│   ├── dashboard/         # dashboard admin: auth JWT, routes, static
│   ├── routers/           # register, messages, files, groups
│   └── services/          # message_service, file_service
├── scripts/               # init_db, manage, cleanup
├── tests/                 # test_auth, test_messages, test_files, test_dashboard, test_integration
├── schema.sql
└── requirements.txt
```
