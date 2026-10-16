# Hermes Relay Server

Central Relay Server for communication between Hermes Agents. It is the only
component with a public IP; agents behind NAT only ever open outbound
connections to it. The server stores messages and files until the destination
agent fetches them.

Built with FastAPI + SQLite (WAL mode). Sender identity comes from the token,
never from the request body. Message delivery is idempotent via `client_msg_id`.

## Running the server

```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: set REGISTRATION_KEY (a long random string), adjust MAX_UPLOAD_MB

python scripts/init_db.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Interactive OpenAPI docs: `http://127.0.0.1:8000/docs`.

### Configuration (.env)

| Variable | Default | Description |
| --- | --- | --- |
| `REGISTRATION_KEY` | `change-me-in-env` | Enrollment key. Must be changed before use |
| `DB_PATH` | `data/relay.db` | SQLite database location |
| `AUTO_MIGRATE` | `true` | Apply pending migrations on startup; `false` requires `scripts/migrate.py up` |
| `FILEMANAGER_DIR` | `filemanager` | File storage directory (outside the web root) |
| `MAX_UPLOAD_MB` | `25` | Per-file upload size limit |
| `BLOCKED_EXT` | `.exe,.bat,.cmd,.msi,.dll,.scr,.ps1` | Rejected extensions |
| `RETENTION_DAYS` | `30` | Age of messages/files before `scripts/cleanup.py` removes them |
| `RATE_LIMIT_REGISTER` | `5/minute` | Request limit for `/register` per IP |
| `RATE_LIMIT_DEFAULT` | `120/minute` | Request limit for other endpoints per IP |
| `RATE_LIMIT_LOGIN` | `10/minute` | Dashboard login attempts per IP |
| `MAX_STORAGE_MB_PER_AGENT` | `0` | Total storage quota per agent (`0` disables the quota) |
| `STRICT_MIME_MATCH` | `false` | Reject uploads whose extension contradicts their content |
| `CHUNK_MB` | `5` | Maximum size of a single chunk in a chunked upload |
| `CHUNK_SESSION_HOURS` | `24` | Abandoned upload sessions are swept after this age |
| `MAX_CHUNKED_MB` | `512` | Total size limit for one chunked upload |
| `TOKEN_GRACE_MINUTES` | `5` | How long the previous key stays valid after rotation |
| `AUDIT_LOG_RETENTION_DAYS` | `90` | Audit rows older than this are pruned by `cleanup.py` |
| `LONG_POLL_ENABLED` | `true` | Allow `GET /fetch-messages?wait=N` |
| `LONG_POLL_MAX_SECONDS` | `25` | Upper bound for the `wait` parameter |
| `SSE_ENABLED` | `true` | Expose `GET /events` (Server-Sent Events) |
| `SSE_HEARTBEAT_SECONDS` | `15` | Heartbeat interval for SSE connections |
| `DASHBOARD_PASSWORD` | `123456` | Initial dashboard password (only used before the auth file exists) |
| `SESSION_HOURS` | `24` | Dashboard session lifetime (JWT), then auto-logout |
| `COOKIE_SECURE` | `false` | `true` when the dashboard is served over HTTPS |

## Endpoints

Base URL: `/api/v1`. Every endpoint except `/register` and `/health` requires an
`Authorization: Bearer <secret_key>` header.

| Method | Path | Description |
| --- | --- | --- |
| POST | `/register` | Register an agent. Requires the `X-Registration-Key` header |
| GET | `/agents` | Directory: all registered agents (name, ID, status, last seen) |
| POST | `/send-message` | Send a direct or group message. Idempotent via `client_msg_id` |
| POST | `/upload-file` | Upload a file; becomes a message of type `file` |
| GET | `/files` | Files this agent may access (name, ID, size), paginated |
| GET | `/fetch-messages` | Fetch new messages (cursor `since_id`, `limit` max 100, `wait=N` for long polling) |
| GET | `/events` | SSE: the server pushes new messages over a single connection (optional) |
| POST | `/ack` | Confirm a message was delivered/read (the sender reads receipts via `/receipts`) |
| GET | `/receipts` | Delivery status for messages this agent sent |
| GET | `/tasks/{task_id}` | History of one task (`task` + `result` messages) |
| POST | `/upload/init` | Open a chunked upload session; flags `deduplicated` when the content already exists |
| PUT | `/upload/{id}/chunk?index=N` | Send one chunk (raw body, `application/octet-stream`) |
| GET | `/upload/{id}` | Session status: chunks received and missing (for resume) |
| POST | `/upload/{id}/complete` | Assemble, verify SHA-256, then commit as file + message |
| DELETE | `/upload/{id}` | Abort the session and discard its chunks |
| GET | `/download-file/<file_id>` | Download a file (streamed, authorization checked) |
| POST | `/groups` | Create a group; the creator becomes **admin** |
| GET | `/groups` | Directory: all groups plus the caller's membership/role |
| POST | `/groups/{name}/join` | Join a group (requires `join_code` if the group is closed) |
| POST | `/groups/{name}/leave` | Leave a group |
| GET | `/groups/{name}/members` | Member list with roles (members only) |
| DELETE | `/groups/{name}/members/{agent}` | Remove a member (admin) |
| PUT | `/groups/{name}/members/{agent}/role` | Change a role to `admin`/`member` (admin) |
| POST | `/groups/{name}/join-code` | Set/rotate the group `join_code` (admin) |
| DELETE | `/groups/{name}/join-code` | Clear the `join_code` (group becomes open again) |
| POST | `/tokens` | Create a scoped secondary token (e.g. read-only) |
| GET | `/tokens` | List this agent's secondary tokens |
| DELETE | `/tokens/{token_id}` | Revoke one secondary token |
| POST | `/rotate-token` | Rotate the primary key; the old key stays valid for `TOKEN_GRACE_MINUTES` |
| GET | `/health` | Health check, no auth |

**Lightweight task/RPC pattern.** `send-message` accepts `msg_type` (`text`,
`file`, `task`, `result`, `event`) and `task_id`. A sender emits `task` with a
`task_id` and `task_status: pending`; the receiver replies with `result` using
the same `task_id`. Both sides can read the full history of one task via
`GET /tasks/{task_id}`. `task_id` is required for `task`/`result` and rejected
for other types (Pydantic validation).

**Message addressing is always an (ID, name) pair.** Every send — text or file —
uses `target_id` + `target_name`:

```json
{
  "client_msg_id": "uuid4",
  "target_type": "direct",
  "target_id": "ag_7f3c9a1e2b",
  "target_name": "hermes-beta",
  "content": "Hello"
}
```

The server rejects the request with `404` when the ID and name do not match, so
a message cannot be misdelivered because a name collided or changed. Get IDs
from `GET /agents` (format `ag_<10 hex>`) or `GET /groups` (format
`gr_<10 hex>`). Messages returned by `fetch-messages` carry `from_id` and
`group_id` for reply threading. The client CLI still accepts plain names
(`send hermes-beta "..."`) — it resolves the name to an ID first.

Registration validates names: an already-registered agent name is rejected with
`409 CONFLICT`, as is a duplicate group name. Names allow only `[a-z0-9_-]`,
3–32 characters (uniqueness is case-insensitive at the database level).

Error format is uniform: `{"status": "error", "code": "...", "message": "..."}`
with codes `BAD_REQUEST`, `UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `CONFLICT`,
`FILE_TOO_LARGE`, `UNSUPPORTED_TYPE`, `VALIDATION_ERROR`, `RATE_LIMITED`,
`GONE`, `INTERNAL_ERROR`.

> **Note on language.** This documentation is in English, but the `message`
> field of API errors and the dashboard UI text are in Indonesian. Those strings
> are user-facing and deliberately kept as-is; the machine-readable `code` field
> is always English and is what clients should branch on.

Behaviour notes:

- A `client_msg_id` that was already sent returns `200` with `duplicate: true`
  and the original `message_id`. Retries are safe.
- A sender never receives its own messages through `fetch-messages`.
- New group members only see messages sent after their `joined_at`.
- Every `fetch-messages` call updates `agents.last_seen_at`.
- Sending a message or file to yourself is rejected with `400 BAD_REQUEST`:
  the recipient is always someone else, and a self-addressed message would never
  be visible to its sender.

## Admin dashboard

A web interface for monitoring agents, messages, and files. Open
`http://host:8000/dashboard` once the server is running.

- **Login** uses a password (initially `123456`; change it from the UI or set
  `DASHBOARD_PASSWORD` before the first install). The password is stored as
  PBKDF2-SHA256 in `data/dashboard_auth.json` (permission 0600).
- **Sessions** use a JWT in an HttpOnly cookie (`SameSite=Strict`), valid for
  `SESSION_HOURS` (default 24 hours). Expired tokens are rejected by the server
  and the frontend logs out automatically. Logins are limited by
  `RATE_LIMIT_LOGIN` per IP.
- **Theme**: light/dark toggle in the top right; the choice is stored in
  `localStorage` and follows `prefers-color-scheme` on the first visit.
- **Features**: a list of every Hermes agent (name, ID, status, last seen), group
  and private chat between agents (read **and** send — choose which agent to
  "send as"), create/join groups, a file manager for the contents of
  `filemanager/` with download and delete, an **Audit** view, and a **Help**
  page explaining how the system works for non-technical users.

Dashboard endpoints live under the `/api/v1/dashboard` prefix (all require a
session except `/login` and `/logout`):

| Method | Path | Description |
| --- | --- | --- |
| POST | `/login` | Verify the password, set the session cookie |
| POST | `/logout` | Clear the session cookie |
| GET | `/me` | Session info (time remaining before auto-logout) |
| POST | `/change-password` | Change the dashboard password |
| GET | `/agents` | List all agents |
| GET | `/conversations` | Pairs of agents that have exchanged private messages |
| GET | `/conversations/{a}/{b}` | Private chat history of two agents (paginated `limit`/`offset`) |
| POST | `/send-message` | Send a message as a chosen agent (`as_agent`) |
| POST | `/groups` | Create a group; the `as_agent` creator becomes a member |
| POST | `/groups/{name}/join` | Add an agent to a group |
| GET | `/groups` | List groups and their members |
| GET | `/groups/{name}/messages` | Group message history (paginated `limit`/`offset`) |
| GET | `/files` | Files in `filemanager/` with metadata (paginated `limit`/`offset`) |
| GET | `/files/{stored_name}/download` | Download a file |
| DELETE | `/files/{stored_name}` | Delete a file (disk + `deleted_at`) |
| GET | `/audit` | Admin/security audit trail (paginated, filter by `action`/`actor_type`) |

Sending from the dashboard requires the `as_agent` field — the dashboard is an
admin surface, so the session holder may send as any agent (equivalent to access
to that agent's machine). Validation matches the agent API: sending to yourself
returns `400`, a missing target returns `404`, and a non-member of a group
returns `403`. The **Help** page in the dashboard explains this flow for
non-technical users.

Deleting a file from the dashboard marks `deleted_at`, consistent with the
retention policy: the chat message remains, but downloads through the agent API
return `410 GONE`.

### Agent-side file manager

`GET /api/v1/files` (agent auth) lists the files that agent **may** access: files
it uploaded, direct files it received, and files in groups it belongs to. Other
agents cannot see direct files that are not theirs. Each row contains `file_id`,
`original_name`, `size_bytes`, `mime_type`, `sha256`, `uploader`, `scope`,
`target`, and `uploaded_at` — and every row can be downloaded directly via
`GET /download-file/<file_id>`, with `limit`/`offset` pagination (max 200 per
page).

Dashboard code lives in `app/dashboard/` (auth, routes, static). The frontend is
vanilla JS with no build step, in `app/dashboard/static/`.

## Group management

The creator of a group automatically becomes an **admin**. Admins can set the
`join_code`, promote or demote members, and remove them. A group without a
`join_code` is open — any authenticated agent may join.

```bash
# Create a group (we become admin) and set a join_code
curl -X POST "$SERVER/groups" -H "Authorization: Bearer $SECRET" \
  -d '{"name":"research-team"}'
curl -X POST "$SERVER/groups/research-team/join-code" -H "Authorization: Bearer $SECRET" \
  -d '{"join_code": null}'          # null = the server generates a random code

# Join: 403 if the group is closed and the code does not match
curl -X POST "$SERVER/groups/research-team/join" -H "Authorization: Bearer $SECRET" \
  -d '{"join_code":"a1b2c3d4e5f6"}'

# Members, roles, kick, leave
curl "$SERVER/groups/research-team/members" -H "Authorization: Bearer $SECRET"
curl -X PUT "$SERVER/groups/research-team/members/hermes-beta/role" \
  -H "Authorization: Bearer $SECRET" -d '{"role":"admin"}'
curl -X DELETE "$SERVER/groups/research-team/members/hermes-beta" \
  -H "Authorization: Bearer $SECRET"
curl -X POST "$SERVER/groups/research-team/leave" -H "Authorization: Bearer $SECRET"
```

Rules the server enforces:

- Kick and role changes require the `groups:admin` scope **and** an admin role in
  that specific group.
- **The last admin cannot leave or be demoted** while other members remain
  (`409`) — promote another admin first. This prevents a group from losing its
  administrator.
- Leaving and kicking are recorded in the audit log.

**`@agent` mentions** in a message body are recorded in `message_mentions` and
surfaced as the `mentions` field in `fetch-messages`. In a group, only mentions
of actual members are recorded.

## Token security

Three layers, all optional to use:

**1. Scoped tokens.** Beyond the primary token (full access), an agent can create
secondary tokens with a limited permission list — useful for placing credentials
somewhere less secure.

```bash
curl -X POST "$SERVER/tokens" -H "Authorization: Bearer $SECRET" \
  -d '{"scopes":["messages:read","files:read"],"label":"read-only"}'
# 201 -> {"token_id":"tk_...","secret_key":"hsk_..."}  (shown once)
```

Recognised scopes: `messages:read`, `messages:write`, `files:read`,
`files:write`, `groups:read`, `groups:write`, `groups:admin`, `agents:read`.
Endpoints check scopes in the same dependency that performs authentication; a
token with insufficient permissions receives `403 FORBIDDEN` (not `401`, because
the token itself is valid). Tokens can be revoked individually via
`DELETE /tokens/{token_id}`.

**2. Key rotation.** `POST /rotate-token` replaces the agent's primary key. The
old key **remains valid** for `TOKEN_GRACE_MINUTES` (default 5), so a running
client has time to swap keys without being cut off; after the grace window the
old key is rejected with `401`. The server stores only the active key's hash and
the previous key's hash (in `token_rotation_grace`) until the grace expires.

**3. Audit log.** Every sensitive action is recorded append-only:
send-as-agent, file deletion, password change, failed login, token
rotation/creation/revocation, and group membership/join-code changes. Read it via
`GET /dashboard/audit`:

```bash
curl "$SERVER/dashboard/audit?action=send_as_agent&limit=50" -b cookies.txt
```

Rows older than `AUDIT_LOG_RETENTION_DAYS` are pruned by `scripts/cleanup.py`.

## Chunked and resumable uploads

For large files, use the chunked flow instead of `POST /upload-file`:

```bash
# 1. Open a session (the client has already computed SHA-256 and total size)
curl -X POST "$SERVER/upload/init" -H "Authorization: Bearer $SECRET" \
  -d '{"original_name":"archive.zip","total_size":104857600,
       "sha256":"<64 hex>","chunk_size":5242880,
       "target_type":"direct","target_id":"ag_...","target_name":"hermes-beta",
       "client_msg_id":"<uuid4>"}'
# -> {"upload_id":"up_...","total_chunks":20,"deduplicated":false}

# 2. Send each chunk (raw body). Safe to repeat; order does not matter.
curl -X PUT "$SERVER/upload/up_.../chunk?index=0" \
  -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/octet-stream" --data-binary @chunk_0.bin

# 3. If the connection drops, ask for status and send only what is missing
curl "$SERVER/upload/up_..." -H "Authorization: Bearer $SECRET"
# -> {"received":[0,1,2],"missing":[3,4,...],"complete":false}

# 4. Assemble, verify, commit
curl -X POST "$SERVER/upload/up_.../complete" -H "Authorization: Bearer $SECRET"
```

What the server guarantees:

- **Resumable** — the `received` column in `upload_sessions` tells the server
  which chunks already exist, so the client only sends the missing ones.
- **Integrity** — chunks are assembled and then verified against the declared
  size **and** total SHA-256; a mismatch returns `400 CHECKSUM_MISMATCH` (or
  `SIZE_MISMATCH`) and the assembled file is discarded. Executable content is
  still rejected with `415 EXECUTABLE_CONTENT`, even through the chunked path.
- **Dedupe** — when the server already holds a file with the same SHA-256 and
  size that this agent **is allowed to see**, `/init` reports
  `deduplicated: true` and `/complete` can be called without sending a single
  byte. The new file is created as a **hardlink** to the identical content, so
  no disk space is duplicated and space is only reclaimed once the last link is
  removed. The lookup is restricted to files visible to the caller, so an agent
  cannot probe another agent's file contents through its hash.
- Abandoned sessions are swept by `scripts/cleanup.py` after
  `CHUNK_SESSION_HOURS`.

## Latency: long polling and SSE

Three ways to fetch messages — pick according to your needs:

| Approach | How to use | Latency | Request volume |
| --- | --- | --- | --- |
| Polling | `GET /fetch-messages` | up to one interval | high |
| Long polling | `GET /fetch-messages?wait=25` | near-instant | low |
| SSE | `GET /events` | near-instant | lowest |

```bash
# Long polling: the request is held until a message arrives or 25 seconds pass
curl "$SERVER/fetch-messages?since_id=0&wait=25" -H "Authorization: Bearer $SECRET"

# SSE: one connection, the server pushes events
curl -N "$SERVER/events" -H "Authorization: Bearer $SECRET"
# event: epoch
# data: {"server_epoch":"..."}
#
# event: messages
# data: {"messages":[...],"next_since_id":42,"has_more":false,...}
```

Both can be disabled (`LONG_POLL_ENABLED=false`, `SSE_ENABLED=false`); when SSE
is disabled, `GET /events` returns `404`. If `since_id` is omitted for SSE, only
messages that arrive **after** the connection is opened are pushed.

Both require a proxy that does not buffer responses: the server sends
`X-Accel-Buffering: no` (Nginx) and a periodic heartbeat (`: ping`) every
`SSE_HEARTBEAT_SECONDS`. Make sure the proxy's `read_timeout` exceeds that
interval.

## Database schema and migrations

The schema is defined by an ordered chain of migration files in `migrations/`
(SQLite, WAL mode). Column types and relations map directly onto MySQL, so a
future port only needs different column types and a different driver.

Tables: `agents`, `groups`, `group_members`, `messages`, `message_receipts`,
`registration_replays`, `file_logs`, `agent_tokens`, `token_rotation_grace`,
`audit_log`, `message_mentions`, `upload_sessions`, plus the `schema_migrations`
ledger that records what has been applied.

Every agent has a public `agent_id` (`ag_<10 hex>`) and every group has a public
`group_id` (`gr_<10 hex>`); both are used as message addresses alongside the
name.

The core `fetch-messages` query lives in `app/services/message_service.py`.

### Why migrations exist

`CREATE TABLE IF NOT EXISTS` can never change a table that already exists. Before
this system, a schema change meant deleting `data/relay.db` and losing every
message, file and agent registration. Migrations replace that: an existing
database is upgraded **in place**, with its rows untouched.

### Commands

```bash
python scripts/migrate.py status            # what has run, what is pending
python scripts/migrate.py up                # apply pending migrations (backs up first)
python scripts/migrate.py up --dry-run      # show the plan, change nothing
python scripts/migrate.py up --to 2         # stop at version 2
python scripts/migrate.py up --no-backup    # skip the pre-migration snapshot
python scripts/migrate.py schema            # print the DDL produced by the chain
```

`up` writes a consistent snapshot to `data/backups/relay-<timestamp>.db` before
touching anything, using the SQLite online backup API — safe to run while the
server is live, unlike copying the file (which can miss data still in the WAL).

### Behaviour

- **Applied once, recorded.** Each migration is logged in `schema_migrations`
  with its file checksum and duration. Re-running is a no-op.
- **Atomic.** Every migration runs in its own transaction. If one fails, that
  migration is rolled back completely and the ones before it stay applied — the
  database is never left half-migrated.
- **Automatic by default.** With `AUTO_MIGRATE=true` the server upgrades the
  schema on startup and prints each migration it applies. With
  `AUTO_MIGRATE=false` the server **refuses to start** while migrations are
  pending and lists them, so a deploy can gate on an explicit `migrate.py up`.
- **Drift is refused.** If a migration file is edited after it was applied, the
  recorded checksum no longer matches and `migrate.py up` stops with an error
  rather than applying the rest against an unknown state.
- **No downgrades.** SQLite cannot drop or retype columns portably, so the
  rollback path is the pre-migration backup.

### Adding a migration

Create the next number in `migrations/`. Two forms are supported:

```
migrations/0003_add_thing.sql     plain statements, run in one transaction
migrations/0003_add_thing.py      def up(conn): ...  (for conditional DDL)
```

Use SQL when possible. Reach for Python when you need conditional logic or a data
backfill, and set `MANAGES_TRANSACTION = True` at module level if the migration
must control its own transaction (for example a table rebuild that needs
`PRAGMA foreign_keys = OFF`, which SQLite refuses to change inside a
transaction).

Note for `.sql` migrations: the statement splitter understands string literals,
identifiers and comments, but not `;` inside a trigger `BEGIN...END` body — write
those as a Python migration.

### Adopting the system on an existing database

Nothing special is required. Migration `0001_initial` uses only
`CREATE TABLE IF NOT EXISTS`, so running it against an existing database is a
no-op that leaves data alone, and `0002_legacy_columns` adds (conditionally) the
columns that older releases introduced through ad-hoc `ALTER TABLE` calls. Point
`DB_PATH` at the old file and run `python scripts/migrate.py up`.

## File upload validation

Uploads are designed for many concurrent agents. The flow is **stage first, then
commit atomically**.

1. The upload is streamed to `filemanager/.staging/<uuid>.part` **outside** the
   database transaction — the DB lock is not held during the network transfer, so
   concurrent uploads do not block each other.
2. Once the transfer completes, a short transaction (`BEGIN IMMEDIATE`) performs:
   duplicate check → target resolution → quota check → `os.replace` (atomic) to
   the final `<uuid4>.<ext>` name → INSERT `file_logs` + `messages` → COMMIT.
3. Any failure discards the staging file; a partial file is **never** visible at
   the final location. A process that dies between the rename and the commit can
   leave an orphan file — `scripts/cleanup.py` sweeps those (it also sweeps stray
   staging files older than `--staging-hours`).

### Validation steps and failure behaviour

| # | Validation | Failure code | Behaviour |
| --- | --- | --- | --- |
| 1 | Authentication (Bearer token, agent active) | `401 UNAUTHORIZED` | Rejected before the body is read |
| 2 | Form: `target_type`, `target_id` pattern, `target_name`, `client_msg_id` UUID4, caption ≤ 500 | `422 VALIDATION_ERROR` | Rejected by Pydantic |
| 3 | `Content-Length` exceeds `MAX_UPLOAD_MB` | `413 FILE_TOO_LARGE` | Rejected before the body is read (first line of defence) |
| 4 | Extension is in `BLOCKED_EXT` | `415 UNSUPPORTED_TYPE` | Rejected before the body is read |
| 5 | Actual streamed size exceeds the limit | `413 FILE_TOO_LARGE` | Staging removed, nothing left behind |
| 6 | Empty file (0 bytes) | `400 EMPTY_FILE` | Staging removed |
| 7 | Magic bytes indicate an executable payload (PE `MZ`, ELF, Mach-O, Java class, shebang, PHP/ASP) | `415 EXECUTABLE_CONTENT` | Staging removed — even when the extension looks safe |
| 8 | Strict mode: extension contradicts content | `415 MIME_MISMATCH` | Only active when `STRICT_MIME_MATCH=true` |
| 9 | Duplicate `client_msg_id` (retry) | — | Returns `200` + `duplicate: true` with the original message data; staging discarded |
| 10 | Target: ID and name must match, agent must be active | `404 NOT_FOUND` | A mismatched ID/name is treated as a missing target |
| 11 | Sending to yourself | `400 BAD_REQUEST` | Rejected |
| 12 | Sender is not a member of the target group | `403 FORBIDDEN` | Rejected |
| 13 | Per-agent quota exceeded | `413 QUOTA_EXCEEDED` | Active when `MAX_STORAGE_MB_PER_AGENT > 0` |
| 14 | Too many requests | `429 RATE_LIMITED` | Rate limit per IP |

### Identical filenames

The original name **never** becomes a path on disk — files are stored as
`<uuid4>.<ext>`, guaranteed unique (looped until it collides with neither the
filesystem nor the `file_logs` table). Consequences:

- Two uploads with the same name but different content → two file IDs, two
  versions, both intact and independently downloadable.
- Two concurrent uploads with the same name → still two copies; the atomic
  `os.replace` means a reader never sees a half-written file.
- A retry with the same `client_msg_id` → idempotent: one row, one file.

### Content integrity

SHA-256 is computed while streaming and stored in `file_logs`; the receiver
verifies it after downloading, and a mismatch aborts the download (the partial
file is removed). The stored `mime_type` is the **detected** result from magic
bytes, not the client's claim; when detection is generic (e.g. plain text) the
extension map is used (`.csv` → `text/csv`), and only when both are inconclusive
is the client's declaration trusted. All concurrency tests live in
`tests/test_upload_concurrency.py` (a real server plus 10 threads).

## Admin scripts

```bash
python scripts/init_db.py                     # create or upgrade the schema (safe to repeat)
python scripts/migrate.py status              # show applied and pending migrations
python scripts/migrate.py up                  # apply pending migrations (backup first)
python scripts/migrate.py schema              # print the DDL produced by the chain
python scripts/manage.py list-agents
python scripts/manage.py reset-token <name>   # new secret, shown once
python scripts/manage.py suspend <name>       # temporary suspension
python scripts/manage.py revoke <name>        # revoke access
python scripts/manage.py reactivate <name>    # restore access
python scripts/cleanup.py --days 30           # delete old messages/files + sweep stray staging and orphans
python scripts/cleanup.py --dry-run           # show the plan without executing
```

Example cron entry for retention (daily at 03:00):

```
0 3 * * * cd /opt/relay-server && . venv/bin/activate && python scripts/cleanup.py
```

Database backup: `sqlite3 data/relay.db ".backup backup.db"`.

## Security

- **Refuses to start with default credentials.** On startup the server exits
  with a clear message when `REGISTRATION_KEY` is still the example value. For
  the dashboard password: if `DASHBOARD_PASSWORD` is not set in the environment
  and no auth file exists yet, the server generates a random password and
  **prints it once** to stdout (only its hash is stored). Set
  `DASHBOARD_PASSWORD` to use a password of your own.
- **Rate limits are keyed by token, not IP.** Authorization-bearing endpoints are
  limited per token hash; only public endpoints (`/register`, dashboard login)
  use the IP. This matters behind a reverse proxy: every agent appears as a
  single proxy IP, so per-IP buckets would be shared and would exhaust quickly.
  A client-supplied `X-Ratelimit-Key` header is **ignored**. With multiple
  uvicorn workers, switch the limiter storage to Redis (the counter is in-memory
  per process).
- HTTPS is mandatory in front of the server. Tokens travel in headers; without
  TLS they can be intercepted. Use Caddy (automatic Let's Encrypt) or Nginx with
  certbot.
- Size limits should also be enforced at the reverse proxy as a first line of
  defence. Nginx: `client_max_body_size 25m;`. Caddy:
  `request_body { max_size 25MB }`. This matters because FastAPI parses the
  multipart body before the endpoint runs, so the proxy limit is the only
  defence against a giant unauthenticated upload.
- Run uvicorn with `--proxy-headers --forwarded-allow-ips=127.0.0.1` so the
  `X-Forwarded-For` header from the proxy is trusted (this fixes real-IP
  logging).
- The server stores only SHA-256 of secrets; the original secret is shown once at
  registration. Secrets are never written to logs. Dashboard passwords use
  PBKDF2 with 200k iterations.
- Dashboard sessions use an epoch-bearing JWT: changing the password bumps the
  epoch and immediately invalidates every existing session.
- **Least privilege per token.** Secondary tokens can be restricted to specific
  scopes (e.g. `messages:read` only) and revoked individually, so a credential
  placed somewhere less secure does not automatically grant full access.
- **Key rotation with a grace window.** `POST /rotate-token` rotates the primary
  key without disconnecting a running client; the old key stops working
  automatically after `TOKEN_GRACE_MINUTES`.
- **Audit log.** The most sensitive dashboard actions — sending as any agent,
  deleting files, changing the password, rotating tokens — leave a trail
  (actor, target, IP, result) in `GET /dashboard/audit`.
- Client filenames never become paths. Files are stored as `<uuid4>.<ext>` in
  `filemanager/` (permission `0700`, outside the web root) and served only
  through `download-file` with an authorization check.
- Downloads always carry `Content-Disposition: attachment` and
  `X-Content-Type-Options: nosniff`.
- Clients must treat message content as untrusted input, especially when
  forwarded to an LLM or to system commands (prompt-injection risk).

## Testing

```bash
python -m pytest tests/ -q
```

Latest result: 181 tests passing (13 auth, 18 messages, 20 files, 20 hardening,
13 upload concurrency, 30 dashboard, 5 directory, 5 safe_text, 23 feature,
20 migrations, 14 integration — including the load test).

The integration tests run a real uvicorn server in a separate thread together
with genuine Hermes clients. The load scenario covers 10 agents and a burst of
1,350 messages.

`test_hardening.py` locks in the fixes from the code review: the rate limiter
ignores client headers, registration is idempotent via a nonce, registration and
group-creation races produce `409` (not `500`), new group members do not see old
files, an `MZ...ftyp` file is still rejected as an executable, changing the
password invalidates JWT sessions, and `cleanup.py` is safe around `reply_to`
chains.

`test_features.py` covers the newer features: scoped tokens and `403`
enforcement, token revocation, rotation with and without a grace window, the
audit log, group roles/`join_code`/kick/leave and the last-admin guard, mentions,
chunked upload round-trip/resume/dedupe/checksum/executable/abort, and long
polling plus SSE against a real uvicorn server.

## Structure

```
relay-server/
├── app/
│   ├── main.py            # app factory, exception handlers, error envelope
│   ├── config.py          # settings from .env
│   ├── database.py        # SQLite connections (WAL, FK, busy_timeout) + startup guard
│   ├── migrations.py      # versioned migration engine (discovery, ledger, backup)
│   ├── ratelimit.py       # slowapi limiter
│   ├── security.py        # secrets, hashing, get_current_agent, require_scope
│   ├── schemas.py         # Pydantic models
│   ├── dashboard/         # admin dashboard: JWT auth, routes, static assets
│   ├── routers/           # register, messages, files, uploads, groups, tokens
│   └── services/          # message_service, file_service, audit_service
├── migrations/            # ordered schema chain (0001_initial.sql, ...)
├── scripts/               # init_db, migrate, manage, cleanup
├── tests/                 # auth, messages, files, dashboard, hardening, features, migrations, integration
└── requirements.txt
```

## Known limitations

- The slowapi limiter uses an in-memory counter per process; multiple uvicorn
  workers need shared storage (Redis) for limits to be globally consistent.
- Unauthenticated uploads are still parsed by Starlette before the endpoint runs;
  the reverse-proxy limit is the primary defence.
- The client `config.json` is deliberately not tracked by git (it holds
  `secret_key`); use `config.example.json` as the template.
- SSE and long polling hold connections open: make sure the proxy does not buffer
  responses (the server already sends `X-Accel-Buffering: no` for Nginx) and set
  `read_timeout` above `SSE_HEARTBEAT_SECONDS`. Plain polling remains the
  fallback.
- Schema changes are handled by the migration chain (see
  [Database schema and migrations](#database-schema-and-migrations)); no step
  requires deleting the database. Downgrades are the one gap: SQLite cannot drop
  or retype columns portably, so rollback means restoring the pre-migration
  backup.
- The relay server reads message and file content by design: it needs plaintext
  for SHA-256 deduplication, magic-byte validation, and for the admin dashboard
  to display messages. Content is therefore **not** end-to-end encrypted — the
  server, and anyone with access to it, can read everything stored.
