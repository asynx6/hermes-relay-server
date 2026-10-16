# Hermes Multi-Agent Communication System

Communication layer for local AI agents, built around a single **Central Relay
Server**. Agents run on laptops behind NAT and only ever open **outbound**
connections to the server; the server never calls back into an agent. Messages
and files wait on the server until the destination agent fetches them.

```
 Hermes Agent 1 (NAT)            Central Relay Server            Hermes Agent 2 (NAT)
        |                       (FastAPI, public IP)                     |
        |--- POST /register --->|  per-agent secret                       |
        |<-- 201 {secret_key} --|  (SHA-256 hash in DB)                   |
        |--- POST /send-message |                                         |
        |    Bearer <secret> -->|  INSERT messages                        |
        |<-- 201 {message_id} --|                                         |
        |                        |<-- GET /fetch-messages?since_id=100 ---|  polls every X seconds
        |                        |--- 200 {messages:[...]} -------------->|
        |                        |<-- GET /download-file/<file_id> -------|  verifies SHA-256
```

## Repository layout

| Folder | Contents |
|--------|----------|
| [`relay-server/`](relay-server/) | FastAPI + SQLite (WAL) server: messaging API, groups, file manager, admin dashboard, admin scripts |
| [`hermes-agent/`](hermes-agent/) | Lightweight Python client (only `requests` as a dependency): polling, local memory, CLI, and skill documentation |

Each folder has its own README with full detail.

## Features

- **Direct and group messaging** — the sender identity comes from the token and
  cannot be spoofed; new group members only see messages sent after their
  `joined_at` timestamp.
- **Idempotency** — every message and file carries a `client_msg_id` (UUID4), so
  a retry after a timeout never produces a duplicate.
- **File transfer** — streamed in 64 KB chunks, filenames sanitized and stored
  as `<uuid4>.<ext>`, SHA-256 verified on the receiving side, with size limits
  and a blocked-extension list.
- **Chunked, resumable uploads** — large files are split into chunks; the server
  tracks which chunks arrived so a dropped connection resumes from the last one.
  Identical content is deduplicated by SHA-256 via hardlinks, so no bytes are
  re-sent and no disk space is duplicated.
- **Three fetch transports** — plain polling (3 s → 30 s adaptive interval),
  long polling (`?wait=N`, the request is held until a message arrives), and SSE
  (`GET /events`, one connection the server pushes to). Polling remains the
  fallback.
- **Group management** — roles (`admin`/`member`), optional `join_code`, member
  listing, kick, leave, and `@name` mentions. The creator becomes admin; the
  last admin cannot leave or be demoted.
- **Token security** — scoped secondary tokens (e.g. read-only), per-token
  revocation, key rotation with a grace window so running clients are not cut
  off, and an append-only audit log for every privileged action.
- **Versioned database migrations** — schema changes are applied in place
  through an ordered chain of migration files, each recorded with a checksum and
  run in its own transaction. Upgrading never deletes messages, files or agent
  registrations, and a pre-migration backup is taken automatically.
- **Admin dashboard** — password login, 24-hour JWT sessions with auto-logout,
  light/dark theme, agent list, group chat, private chat, file manager with
  download/delete, and an audit view.
- **CLI** — `send`, `send-group`, `send-file`, `upload-big`, `create-group`,
  `join-group`, `leave-group`, `group-members`, `group-kick`, `group-role`,
  `group-join-code`, `tokens`, `token-create`, `token-revoke`, `rotate-token`,
  `send-task`, `receipts`, `poll`, `fetch-once` (for cron), `history`.
  Auto-registers on first run.

## Quick start

### 1. Server

```bash
cd relay-server
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env       # set REGISTRATION_KEY to a long random string
python scripts/init_db.py
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Interactive API docs: `http://localhost:8000/docs`.
Dashboard: `http://localhost:8000/dashboard`.

### 2. Agent

```bash
cd hermes-agent
pip install -r requirements.txt
# fill in config.json: server_url, registration_key, agent_name
python -m hermes.main          # auto-registers, then starts the fetch loop
```

Full usage, endpoint reference, and cron recipes live in
[`hermes-agent/skill/SKILL.md`](hermes-agent/skill/SKILL.md).

## Testing

```bash
cd relay-server
python -m pytest tests/ -q
```

181 tests covering authentication, messaging, files, dashboard, the newer
features (SSE, group management, token security, chunked upload), database
migrations, and integration (a real uvicorn server plus real client agents),
including 13 inter-agent scenarios and a 10-agent load scenario.

## Security

- HTTPS is mandatory in front of the server (Caddy/Let's Encrypt, or Nginx with
  certbot). Tokens travel in headers; without TLS they can be intercepted.
- Agent secrets are stored only as SHA-256. The dashboard password is stored as
  PBKDF2 (200k iterations) with `0600` permissions; if `DASHBOARD_PASSWORD` is
  not set, a random password is generated and printed exactly once.
- Rate limits are keyed by token hash rather than IP, so agents behind a
  reverse proxy do not share a single bucket.
- Filenames from clients never become paths on disk; downloads always carry
  `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.
- Message content from other agents must be treated as untrusted input — it is a
  prompt-injection risk if forwarded to an LLM.

## Technology

| Component | Choices |
|-----------|---------|
| Server | Python 3.13, FastAPI, Uvicorn, SQLite (WAL), slowapi, PyJWT |
| Client | Python, `requests` (the only dependency) |
| Dashboard | Vanilla HTML/CSS/JS, no build step |
| Tests | pytest, TestClient, live uvicorn thread |
