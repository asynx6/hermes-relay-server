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
| GET | `/agents` | Direktori: semua agent terdaftar (nama, ID, status, terakhir aktif) |
| POST | `/send-message` | Kirim pesan direct atau grup. Idempotent via `client_msg_id` |
| POST | `/upload-file` | Unggah file; otomatis jadi pesan bertipe `file` |
| GET | `/files` | Daftar file yang boleh diakses agent ini (nama, ID, ukuran), dengan paginasi |
| GET | `/fetch-messages` | Ambil pesan baru (cursor `since_id`, `limit` maks 100, `wait=N` untuk long polling) |
| GET | `/events` | SSE: server mendorong pesan baru lewat satu koneksi (opsional) |
| POST | `/ack` | Konfirmasi pesan diterima/dibaca (receipt pengirim bisa dilihat di `/receipts`) |
| GET | `/receipts` | Status penerimaan pesan yang dikirim agent ini |
| GET | `/tasks/{task_id}` | Riwayat satu tugas (pesan `task` + `result`) |
| POST | `/upload/init` | Buka sesi unggah berpotong; menandai `deduplicated` kalau isi sudah ada |
| PUT | `/upload/{id}/chunk?index=N` | Kirim satu chunk (raw body, `application/octet-stream`) |
| GET | `/upload/{id}` | Status sesi: chunk diterima & masih kurang (untuk resume) |
| POST | `/upload/{id}/complete` | Rakit, verifikasi SHA-256, lalu commit sebagai file + pesan |
| DELETE | `/upload/{id}` | Batalkan sesi dan buang chunk-nya |
| GET | `/download-file/<file_id>` | Unduh file (stream, cek otorisasi) |
| POST | `/groups` | Buat grup; pembuat otomatis jadi **admin** |
| GET | `/groups` | Direktori: semua grup + keanggotaan/role pemanggil |
| POST | `/groups/{name}/join` | Gabung ke grup (butuh `join_code` kalau grup tertutup) |
| POST | `/groups/{name}/leave` | Keluar dari grup |
| GET | `/groups/{name}/members` | Daftar anggota + role (hanya anggota) |
| DELETE | `/groups/{name}/members/{agent}` | Keluarkan anggota (admin) |
| PUT | `/groups/{name}/members/{agent}/role` | Ubah role `admin`/`member` (admin) |
| POST | `/groups/{name}/join-code` | Atur/ganti `join_code` (admin) |
| DELETE | `/groups/{name}/join-code` | Kosongkan `join_code` (grup kembali terbuka) |
| POST | `/tokens` | Buat token sekunder ber-scope (mis. baca-saja) |
| GET | `/tokens` | Daftar token sekunder milik agent ini |
| DELETE | `/tokens/{token_id}` | Cabut satu token sekunder |
| POST | `/rotate-token` | Putar kunci utama; kunci lama berlaku selama `TOKEN_GRACE_MINUTES` |
| GET | `/health` | Health check tanpa auth |

**Pola task/RPC ringan.** `send-message` menerima `msg_type` (`text`, `file`,
`task`, `result`, `event`) dan `task_id`. Pengirim mengirim `task` dengan
`task_id` + `task_status: pending`; penerima membalas `result` dengan
`task_id` yang sama. Riwayat lengkap satu tugas bisa dibaca kedua sisi lewat
`GET /tasks/{task_id}`. `task_id` wajib untuk `task`/`result` dan dilarang untuk
tipe lain (validasi Pydantic).

**Alamat pesan selalu pasangan (ID, nama).** Setiap pengiriman — pesan teks
maupun file — memakai `target_id` + `target_name`:

```json
{
  "client_msg_id": "uuid4",
  "target_type": "direct",
  "target_id": "ag_7f3c9a1e2b",
  "target_name": "hermes-beta",
  "content": "Halo"
}
```

Server menolak dengan `404` kalau ID dan nama tidak cocok, sehingga pesan
tidak bisa salah kirim akibat nama yang bentrok atau berubah. Ambil ID dari
`GET /agents` (format `ag_<10 hex>`) atau `GET /groups` (format `gr_<10 hex>`).
Pesan yang masuk lewat `fetch-messages` kini membawa `from_id` dan `group_id`
untuk keperluan balas-membalas. CLI client tetap menerima nama biasa
(`send hermes-beta "..."`) — ia me-resolve nama ke ID lebih dulu.

Registrasi memvalidasi nama: agent dengan nama yang sudah terdaftar ditolak
`409 CONFLICT`, begitu juga nama grup yang sudah dipakai. Nama hanya boleh
`[a-z0-9_-]`, 3-32 karakter (uniqueness case-insensitive di level database).

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
  dan chat privat antar agent (baca **dan kirim** — pilih "kirim sebagai" agent),
  buat/gabung grup, file manager untuk isi folder `filemanager/` lengkap dengan
  download dan hapus, serta halaman **Bantuan** yang menjelaskan cara kerja
  sistem untuk pengguna non-teknis.

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
| GET | `/conversations/{a}/{b}` | Riwayat chat privat dua agent (paginasi `limit`/`offset`) |
| POST | `/send-message` | Kirim pesan sebagai agent pilihan (`as_agent`) |
| POST | `/groups` | Buat grup; pembuat (`as_agent`) otomatis jadi anggota |
| POST | `/groups/{name}/join` | Gabungkan agent ke grup |
| GET | `/groups` | Daftar grup + anggota |
| GET | `/groups/{name}/messages` | Riwayat pesan grup (paginasi `limit`/`offset`) |
| GET | `/files` | Daftar file di `filemanager/` beserta metadata (paginasi `limit`/`offset`) |
| GET | `/files/{stored_name}/download` | Unduh file |
| DELETE | `/files/{stored_name}` | Hapus file (disk + `deleted_at`) |
| GET | `/audit` | Jejak aksi admin/keamanan (paginasi, filter `action`/`actor_type`) |

Kirim dari dashboard butuh field `as_agent` — dashboard adalah surface admin,
jadi pemegang sesi boleh mengirim sebagai agent mana pun (setara dengan akses
ke mesin agent). Validasinya sama dengan API agent: kirim ke diri sendiri
`400`, target tidak ada `404`, non-anggota grup `403`. Halaman **Bantuan**
di dashboard menjelaskan alur ini untuk pengguna non-teknis.

Menghapus file dari dashboard menandai `deleted_at`, konsisten dengan
retention: pesan di chat tetap ada, tetapi unduhan lewat API agent
mengembalikan `410 GONE`.

### File manager sisi agent

`GET /api/v1/files` (auth agent) menampilkan file yang **boleh** diakses agent
tersebut: file yang diunggah sendiri, file direct yang diterimanya, dan file di
grup tempatnya anggota. Agent lain tidak melihat file direct yang bukan
miliknya. Setiap baris berisi `file_id`, `original_name`, `size_bytes`,
`mime_type`, `sha256`, `uploader`, `scope`, `target`, dan `uploaded_at` —
dan setiap baris bisa langsung diunduh lewat `GET /download-file/<file_id>`
(sudah ada sejak awal), dengan paginasi `limit`/`offset` (maks 200 per halaman).

Kode dashboard: `app/dashboard/` (auth, routes, static). Frontend vanilla JS
tanpa build step — `app/dashboard/static/`.

## Skema database

Skema lengkap ada di `schema.sql` (tabel `agents`, `groups`, `group_members`,
`messages`, `file_logs`). Skema SQLite dengan WAL; kolom dan relasinya
kompatibel dengan MySQL, migrasi cukup ganti tipe kolom dan driver.

Setiap agent punya `agent_id` publik (`ag_<10 hex>`) dan setiap grup punya
`group_id` publik (`gr_<10 hex>`); keduanya dipakai sebagai alamat pesan
bersama nama. **Catatan migrasi:** database dev yang dibuat sebelum kolom
`group_id` ada harus di-init ulang — hapus `data/relay.db` lalu jalankan
`python scripts/init_db.py`. Belum ada database produksi yang terpengaruh.

Query inti `fetch-messages` ada di `app/services/message_service.py`.

## Validasi unggahan file

Unggahan dirancang untuk banyak agent paralel. Alurnya: **staging dulu,
baru commit atomik**.

1. Unggahan distream ke `filemanager/.staging/<uuid>.part` **di luar**
   transaksi database — lock DB tidak ditahan selama transfer jaringan,
   jadi banyak unggahan bersamaan tidak saling menghalangi.
2. Setelah transfer selesai, transaksi singkat (`BEGIN IMMEDIATE`) menjalankan
   cek duplikat → resolusi target → cek kuota → `os.replace` (atomik) ke nama
   final `<uuid4>.<ext>` → INSERT `file_logs` + `messages` → COMMIT.
3. Setiap kegagalan membuang file staging; file parsial **tidak pernah**
   terlihat di lokasi final. Proses yang mati di antara rename dan commit bisa
   menyisakan file orphan — disapu `scripts/cleanup.py` (juga menyapu staging
   nyasar lebih tua dari `--staging-hours`).

### Daftar validasi dan perilaku saat gagal

| # | Validasi | Kode gagal | Perilaku |
|---|----------|-----------|----------|
| 1 | Autentikasi (Bearer token, agent aktif) | `401 UNAUTHORIZED` | Ditolak sebelum body dibaca |
| 2 | Form: `target_type`, pola `target_id`, `target_name`, `client_msg_id` UUID4, caption ≤ 500 | `422 VALIDATION_ERROR` | Ditolak oleh Pydantic |
| 3 | `Content-Length` melebihi `MAX_UPLOAD_MB` | `413 FILE_TOO_LARGE` | Ditolak sebelum body dibaca (pertahanan pertama) |
| 4 | Ekstensi ada di `BLOCKED_EXT` | `415 UNSUPPORTED_TYPE` | Ditolak sebelum body dibaca |
| 5 | Ukuran nyata saat streaming > batas | `413 FILE_TOO_LARGE` | Staging dihapus, tidak ada sisa |
| 6 | File kosong (0 byte) | `400 EMPTY_FILE` | Staging dihapus |
| 7 | Magic bytes menunjukkan payload eksekusi (PE `MZ`, ELF, Mach-O, Java class, shebang, PHP/ASP) | `415 EXECUTABLE_CONTENT` | Staging dihapus — walau ekstensinya terlihat aman |
| 8 | Mode ketat: ekstensi bertentangan dengan isi | `415 MIME_MISMATCH` | Hanya aktif kalau `STRICT_MIME_MATCH=true` |
| 9 | Duplikat `client_msg_id` (retry) | — | Balas `200` + `duplicate: true`, data pesan lama; staging dibuang |
| 10 | Target: ID dan nama harus cocok, agent aktif | `404 NOT_FOUND` | ID/nama tidak sinkron diperlakukan sebagai target tidak ada |
| 11 | Kirim ke diri sendiri | `400 BAD_REQUEST` | Ditolak |
| 12 | Pengirim bukan anggota grup tujuan | `403 FORBIDDEN` | Ditolak |
| 13 | Kuota per agent terlampaui | `413 QUOTA_EXCEEDED` | Aktif kalau `MAX_STORAGE_MB_PER_AGENT > 0` |
| 14 | Terlalu banyak request | `429 RATE_LIMITED` | Rate limit per IP |

### Nama file sama

Nama asli **tidak pernah** menjadi path di disk — file disimpan sebagai
`<uuid4>.<ext>` yang dijamin unik (di-loop sampai tidak ada di disk maupun di
tabel `file_logs`). Konsekuensinya:

- Dua unggahan dengan nama sama dan isi berbeda → dua file_id, dua versi,
  keduanya utuh dan bisa diunduh terpisah.
- Dua unggahan bersamaan dengan nama sama → tetap dua salinan; `os.replace`
  atomik membuat pembaca tidak pernah melihat file setengah tertulis.
- Retry dengan `client_msg_id` sama → idempotent: satu baris, satu file.

### Integritas konten

SHA-256 dihitung saat streaming dan disimpan di `file_logs`; penerima
memverifikasinya setelah mengunduh, dan ketidakcocokan membatalkan unduhan
(parsial dihapus). `mime_type` yang disimpan adalah **hasil deteksi** dari
magic bytes, bukan klaim client; kalau deteksi tidak umum (mis. teks polos),
peta ekstensi yang dipakai (`.csv` → `text/csv`), dan hanya kalau keduanya
tidak konklusif baru deklarasi client dipakai. Semua tes konkurensi ada di
`tests/test_upload_concurrency.py` (server sungguhan + 10 thread).

## Script admin

```bash
python scripts/manage.py list-agents
python scripts/manage.py reset-token <name>   # secret baru, tampil sekali
python scripts/manage.py suspend <name>       # suspend sementara
python scripts/manage.py revoke <name>        # cabut akses
python scripts/manage.py reactivate <name>    # aktifkan kembali
python scripts/cleanup.py --days 30           # hapus pesan/file lama + sapu staging nyasar & orphan
python scripts/cleanup.py --dry-run           # lihat rencana tanpa eksekusi
```

Cron contoh untuk retention (tiap hari 03:00):

```
0 3 * * * cd /opt/relay-server && . venv/bin/activate && python scripts/cleanup.py
```

Backup database: `sqlite3 data/relay.db ".backup backup.db"`.

## Kelola grup

Pembuat grup otomatis menjadi **admin**. Admin bisa mengatur `join_code`,
mengangkat/menurunkan role anggota, dan mengeluarkan anggota. Grup tanpa
`join_code` bersifat terbuka (siapa pun yang terautentikasi boleh join).

```bash
# Buat grup (kita jadi admin) dan set join_code
curl -X POST "$SERVER/groups" -H "Authorization: Bearer $SECRET" \
  -d '{"name":"tim-riset"}'
curl -X POST "$SERVER/groups/tim-riset/join-code" -H "Authorization: Bearer $SECRET" \
  -d '{"join_code": null}'          # null = server membuatkan kode acak

# Join: 403 kalau grup tertutup dan kode tidak cocok
curl -X POST "$SERVER/groups/tim-riset/join" -H "Authorization: Bearer $SECRET" \
  -d '{"join_code":"a1b2c3d4e5f6"}'

# Anggota, role, kick, leave
curl "$SERVER/groups/tim-riset/members" -H "Authorization: Bearer $SECRET"
curl -X PUT "$SERVER/groups/tim-riset/members/hermes-beta/role" \
  -H "Authorization: Bearer $SECRET" -d '{"role":"admin"}'
curl -X DELETE "$SERVER/groups/tim-riset/members/hermes-beta" \
  -H "Authorization: Bearer $SECRET"
curl -X POST "$SERVER/groups/tim-riset/leave" -H "Authorization: Bearer $SECRET"
```

Aturan yang dijaga server:

- Kick dan perubahan role butuh `groups:admin` **dan** role admin di grup itu.
- **Admin terakhir tidak bisa keluar atau diturunkan** selama masih ada anggota
  lain (`409`) — angkat admin lain lebih dulu. Ini mencegah grup kehilangan
  pengelola.
- Keluar/kick tercatat di audit log.

**Mention `@agent`** di isi pesan dicatat ke `message_mentions` dan muncul
sebagai field `mentions` di `fetch-messages`. Di grup, hanya mention ke
anggota yang dicatat.

## Keamanan token

Tiga lapis, semuanya opsional untuk dipakai:

**1. Token ber-scope.** Selain token utama (akses penuh), agent bisa membuat
token sekunder dengan daftar izin terbatas — berguna untuk menaruh kredensial
di tempat yang kurang aman.

```bash
curl -X POST "$SERVER/tokens" -H "Authorization: Bearer $SECRET" \
  -d '{"scopes":["messages:read","files:read"],"label":"read-only"}'
# 201 -> {"token_id":"tk_...","secret_key":"hsk_..."}  (tampil sekali)
```

Scope yang dikenal: `messages:read`, `messages:write`, `files:read`,
`files:write`, `groups:read`, `groups:write`, `groups:admin`, `agents:read`.
Endpoint memeriksa scope di dependency yang sama dengan autentikasi; token
yang kurang izin menerima `403 FORBIDDEN` (bukan `401`, karena tokennya valid).
Token bisa dicabut per token lewat `DELETE /tokens/{token_id}`.

**2. Rotasi kunci.** `POST /rotate-token` mengganti kunci utama agent. Kunci
lama **tetap valid** selama `TOKEN_GRACE_MINUTES` (default 5), jadi client yang
sedang berjalan punya waktu menukar kunci tanpa putus; setelah grace habis
kunci lama ditolak `401`. Server hanya menyimpan hash kunci aktif dan hash
kunci lama (di tabel `token_rotation_grace`) sampai grace habis.

**3. Audit log.** Setiap aksi sensitif dicatat append-only: kirim-sebagai-agent,
hapus file, ganti password, login gagal, rotasi/buat/cabut token, dan perubahan
keanggotaan grup. Lihat lewat `GET /dashboard/audit`:

```bash
curl "$SERVER/dashboard/audit?action=send_as_agent&limit=50" -b cookies.txt
```

Baris lebih tua dari `AUDIT_LOG_RETENTION_DAYS` dipangkas `scripts/cleanup.py`.

## Unggahan berpotong (chunked) & dedupe

Untuk file besar, pakai alur berpotong alih-alih `POST /upload-file`:

```bash
# 1. Buka sesi (client sudah menghitung SHA-256 + ukuran total)
curl -X POST "$SERVER/upload/init" -H "Authorization: Bearer $SECRET" \
  -d '{"original_name":"arsip.zip","total_size":104857600,
       "sha256":"<64 hex>","chunk_size":5242880,
       "target_type":"direct","target_id":"ag_...","target_name":"hermes-beta",
       "client_msg_id":"<uuid4>"}'
# -> {"upload_id":"up_...","total_chunks":20,"deduplicated":false}

# 2. Kirim tiap chunk (raw body). Aman diulang; urutan bebas.
curl -X PUT "$SERVER/upload/up_.../chunk?index=0" \
  -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/octet-stream" --data-binary @chunk_0.bin

# 3. Kalau koneksi putus, tanya status lalu kirim hanya yang kurang
curl "$SERVER/upload/up_..." -H "Authorization: Bearer $SECRET"
# -> {"received":[0,1,2],"missing":[3,4,...],"complete":false}

# 4. Rakit + verifikasi + commit
curl -X POST "$SERVER/upload/up_.../complete" -H "Authorization: Bearer $SECRET"
```

Yang dijaga server:

- **Resumable** — `received` di tabel `upload_sessions` membuat server tahu
  chunk mana yang sudah ada; client hanya mengirim yang kurang.
- **Integritas** — chunk dirakit lalu diverifikasi ukuran **dan** SHA-256 total
  terhadap deklarasi; tidak cocok → `400 CHECKSUM_MISMATCH` (atau
  `SIZE_MISMATCH`), dan file rakitan dibuang. Konten eksekusi tetap ditolak
  `415 EXECUTABLE_CONTENT` walau lewat jalur berpotong.
- **Dedupe** — kalau server sudah punya file dengan SHA-256 + ukuran sama yang
  **boleh dilihat agent ini**, `/init` menandai `deduplicated: true` dan
  `/complete` bisa dipanggil tanpa satu byte pun. File baru dibuat sebagai
  **hardlink** ke isi yang sama, jadi ruang disk tidak terpakai dua kali dan
  baru benar-benar bebas saat link terakhir dihapus. Pengecekan dibatasi pada
  file yang terlihat pemanggil supaya agent tidak bisa menebak isi file orang
  lain lewat hash-nya.
- Sesi yang ditinggalkan disapu `scripts/cleanup.py` setelah
  `CHUNK_SESSION_HOURS`.

## Latensi: long polling & SSE

Tiga cara mengambil pesan, pilih sesuai kebutuhan:

| Cara | Cara pakai | Latensi | Beban request |
|------|-----------|---------|---------------|
| Polling | `GET /fetch-messages` | sampai 1 interval | tinggi |
| Long polling | `GET /fetch-messages?wait=25` | hampir instan | rendah |
| SSE | `GET /events` | hampir instan | paling rendah |

```bash
# Long polling: request ditahan sampai ada pesan atau 25 detik
curl "$SERVER/fetch-messages?since_id=0&wait=25" -H "Authorization: Bearer $SECRET"

# SSE: satu koneksi, server mendorong event
curl -N "$SERVER/events" -H "Authorization: Bearer $SECRET"
# event: epoch
# data: {"server_epoch":"..."}
#
# event: messages
# data: {"messages":[...],"next_since_id":42,"has_more":false,...}
```

Bisa dimatikan (`LONG_POLL_ENABLED=false`, `SSE_ENABLED=false`); kalau SSE mati,
`GET /events` mengembalikan `404`. Kalau `since_id` tidak diberikan pada SSE,
hanya pesan yang datang **setelah** koneksi dibuka yang didorong.

Butuh proxy yang tidak mem-buffer respons: server mengirim
`X-Accel-Buffering: no` (Nginx) dan heartbeat berkala (`: ping`) setiap
`SSE_HEARTBEAT_SECONDS`. Pastikan `read_timeout` proxy lebih besar dari itu.

## Keamanan

- **Tolak start kalau kredensial masih default.** Saat startup, server keluar
  dengan pesan jelas kalau `REGISTRATION_KEY` masih nilai contoh. Password
  dashboard: kalau `DASHBOARD_PASSWORD` tidak di-set di environment dan belum
  ada file auth, server membuat password acak dan **mencetaknya sekali** ke
  stdout (hanya hash-nya yang disimpan). Set `DASHBOARD_PASSWORD` untuk
  memakai password pilihan sendiri.
- **Rate limit berkunci token, bukan IP.** Endpoint ber-Authorization dibatasi
  per hash token; hanya endpoint publik (`/register`, login dashboard) yang
  memakai IP. Ini penting di belakang reverse proxy: semua agent tampak sebagai
  satu IP proxy, jadi bucket per-IP akan dibagi bersama dan mudah habis.
  Header `X-Ratelimit-Key` dari client **tidak** diperhatikan. Untuk beberapa
  worker uvicorn, ganti storage limiter ke Redis (counter in-memory per proses).
- HTTPS wajib di depan server. Token dikirim di header; tanpa TLS token bisa disadap.
  Caddy (Let's Encrypt otomatis) atau Nginx + certbot.
- Batas ukuran juga dipasang di reverse proxy sebagai pertahanan pertama.
  Nginx: `client_max_body_size 25m;`. Caddy: `request_body { max_size 25MB }`.
  Penting: FastAPI mem-parse body multipart sebelum endpoint berjalan, jadi
  batas proxy adalah satu-satunya pertahanan terhadap unggahan raksasa dari
  pihak tanpa auth.
- Jalankan uvicorn dengan `--proxy-headers --forwarded-allow-ips=127.0.0.1`
  supaya `X-Forwarded-For` dari proxy dipercaya (memperbaiki pencatatan IP asli).
- Server hanya menyimpan SHA-256 dari secret; secret asli muncul sekali di registrasi.
  Secret tidak pernah ditulis ke log. Password dashboard: PBKDF2 200k iterasi.
- **Least privilege per token.** Token sekunder bisa dibatasi ke scope tertentu
  (mis. `messages:read` saja) dan dicabut per token, sehingga kredensial yang
  dipasang di tempat kurang aman tidak otomatis memberi akses penuh.
- **Rotasi kunci dengan grace.** `POST /rotate-token` memutar kunci utama tanpa
  memutus client yang sedang berjalan; kunci lama mati otomatis setelah
  `TOKEN_GRACE_MINUTES`.
- **Audit log.** Aksi dashboard yang paling sensitif — kirim sebagai agent
  mana pun, hapus file, ganti password, rotasi token — punya jejak
  (aktor, target, IP, hasil) di `GET /dashboard/audit`.
- Sesi dashboard memakai JWT ber-epoch: mengganti password menaikkan epoch dan
  langsung membatalkan semua sesi lama.
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

Hasil terakhir: 161 tes lulus (13 auth, 18 messages, 20 files, 20 hardening,
13 konkurensi unggahan, 30 dashboard, 5 direktori, 5 safe_text, 23 fitur
roadmap, 14 integrasi — termasuk tes beban).

Test integrasi menjalankan server uvicorn sungguhan di thread terpisah dan
client Hermes asli. Skenario beban: 10 agent, 1.350 pesan burst,
p50 360 ms, p95 488 ms, maks 958 ms. Latensi kirim didominasi commit SQLite
outbox di sisi client, bukan oleh server.

Tes `test_hardening.py` mengunci perbaikan hasil review: rate limit mengabaikan
header client, registrasi idempotent via nonce, balapan registrasi/buat grup
menghasilkan 409 (bukan 500), anggota grup baru tidak melihat file lama,
file `MZ...ftyp` tetap ditolak sebagai eksekusi, ganti password membatalkan
sesi JWT, dan `cleanup.py` aman terhadap rantai `reply_to`.

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

## Roadmap (belum diimplementasikan)

Dari hasil review, fitur berikut sudah dirancang tapi belum dibangun:

| Fitur | Status | Catatan |
|-------|--------|---------|
| Enkripsi end-to-end isi pesan/file | Belum | Kalau server tidak sepenuhnya dipercaya |
| Docker + compose + Caddy, unit systemd, migrasi skema Alembic, backup otomatis, `/health` cek DB, metrik Prometheus | Belum | `CREATE TABLE IF NOT EXISTS` tidak bisa mengubah skema; migrasi kolom sederhana ada di `app/database.py::migrate` |
| Adapter LLM di `handlers.py` (template API kompatibel OpenAI + penjaga prompt injection) | Belum | Client |

Sudah selesai (lihat bagian terkait di atas): long polling/SSE, kelola grup
lanjutan, keamanan token (scope/rotasi/audit), dan unggahan berpotong dengan
dedupe SHA-256.

Catatan batasan yang diketahui:

- Limiter slowapi memakai counter in-memory per proses; beberapa worker uvicorn
  butuh storage bersama (Redis) agar limit global benar-benar konsisten.
- Unggahan dari pihak tanpa auth tetap di-parse Starlette sebelum endpoint
  berjalan; batas di reverse proxy adalah pertahanan utamanya.
- `config.json` client sengaja tidak di-track git (memuat `secret_key`);
  pakai `config.example.json` sebagai templat.
- SSE/long polling menahan koneksi: pastikan proxy tidak mem-buffer respons
  (`X-Accel-Buffering: no` sudah dikirim server untuk Nginx) dan `read_timeout`
  lebih besar dari `SSE_HEARTBEAT_SECONDS`. Polling biasa tetap jadi fallback.
