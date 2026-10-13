/* Hermes Relay Dashboard — aplikasi vanilla JS.
 * Semua request pakai cookie sesi (credentials same-origin). Response 401
 * berarti sesi habis/tidak valid: UI kembali ke halaman login.
 */

const API = "/api/v1/dashboard";
const state = {
  view: "agents",
  agents: [],
  agentIds: {},    // nama -> agent_id
  groupIds: {},    // nama -> group_id
  selectedGroup: null,
  selectedPair: null,
  autoLogoutTimer: null,
};

const $ = (id) => document.getElementById(id);

/* ---------------- util ---------------- */

function toast(message, kind = "") {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast ${kind}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add("hidden"), 3000);
}

function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return d.toLocaleString("id-ID", { hour12: false });
}

function fmtSize(bytes) {
  if (bytes == null) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function esc(text) {
  const div = document.createElement("div");
  div.textContent = text ?? "";
  return div.innerHTML;
}

function fillSelect(el, values, selected) {
  el.innerHTML = values
    .map((v) => `<option value="${esc(v)}" ${v === selected ? "selected" : ""}>${esc(v)}</option>`)
    .join("");
}

async function api(path, options = {}) {
  const resp = await fetch(API + path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (resp.status === 401) {
    showLogin("Sesi berakhir. Silakan masuk lagi.");
    throw new Error("unauthorized");
  }
  const body = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(body.message || `HTTP ${resp.status}`);
  }
  return body.data;
}

async function ensureAgents() {
  if (state.agents.length) return state.agents.map((a) => a.name);
  const { agents } = await api("/agents");
  state.agents = agents;
  state.agentIds = Object.fromEntries(agents.map((a) => [a.name, a.agent_id]));
  return agents.map((a) => a.name);
}

/* ---------------- tema ---------------- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("hermes-theme", theme);
  $("theme-toggle").textContent = theme === "dark" ? "☀️" : "🌙";
}

function initTheme() {
  const saved = localStorage.getItem("hermes-theme");
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
}

/* ---------------- login / sesi ---------------- */

function showLogin(message) {
  clearTimeout(state.autoLogoutTimer);
  $("login-view").classList.remove("hidden");
  $("app-view").classList.add("hidden");
  if (message) {
    $("login-error").textContent = message;
    $("login-error").classList.remove("hidden");
  }
}

function showApp(sessionSeconds) {
  $("login-view").classList.add("hidden");
  $("app-view").classList.remove("hidden");
  // Auto-logout tepat saat token kedaluwarsa (default 24 jam).
  clearTimeout(state.autoLogoutTimer);
  state.autoLogoutTimer = setTimeout(
    () => showLogin("Sesi 24 jam berakhir. Silakan masuk lagi."),
    Math.max(1000, sessionSeconds * 1000)
  );
}

async function checkSession() {
  try {
    const data = await api("/me");
    showApp(data.session_seconds);
    return true;
  } catch {
    showLogin("");
    return false;
  }
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").classList.add("hidden");
  try {
    const data = await api("/login", {
      method: "POST",
      body: JSON.stringify({ password: $("login-password").value }),
    });
    $("login-password").value = "";
    showApp(data.session_seconds);
    switchView("agents");
    toast("Berhasil masuk.", "ok");
  } catch (err) {
    $("login-error").textContent = err.message;
    $("login-error").classList.remove("hidden");
  }
});

$("logout-btn").addEventListener("click", async () => {
  try {
    await api("/logout", { method: "POST", body: "{}" });
  } catch {
    /* tetap keluar dari UI */
  }
  showLogin("");
});

$("theme-toggle").addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});

/* ---------------- navigasi ---------------- */

const VIEWS = ["agents", "groups", "direct", "files", "help", "password"];

function switchView(view) {
  state.view = view;
  document.querySelectorAll(".nav-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view)
  );
  VIEWS.forEach((v) => $(`view-${v}`).classList.toggle("hidden", v !== view));
  if (view === "agents") loadAgents();
  if (view === "groups") loadGroups();
  if (view === "direct") loadPairs();
  if (view === "files") loadFiles();
}

document.querySelectorAll(".nav-btn").forEach((b) =>
  b.addEventListener("click", () => switchView(b.dataset.view))
);

/* ---------------- agents ---------------- */

async function loadAgents() {
  try {
    const { agents } = await api("/agents");
    state.agents = agents;  // segar untuk dropdown pengirim
    state.agentIds = Object.fromEntries(agents.map((a) => [a.name, a.agent_id]));
    const tbody = $("agents-body");
    if (!agents.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state">Belum ada agent terdaftar.</td></tr>`;
      return;
    }
    tbody.innerHTML = agents
      .map(
        (a) => `<tr>
          <td><strong>${esc(a.name)}</strong></td>
          <td class="muted">${esc(a.agent_id)}</td>
          <td><span class="badge badge-${esc(a.status)}">${esc(a.status)}</span></td>
          <td>${fmtTime(a.last_seen_at)}</td>
          <td>${fmtTime(a.created_at)}</td>
          <td class="muted">${esc(a.description || "-")}</td>
        </tr>`
      )
      .join("");
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ---------------- grup ---------------- */

async function loadGroups() {
  try {
    const [names, { groups }] = await Promise.all([ensureAgents(), api("/groups")]);
    state.groupIds = Object.fromEntries(groups.map((g) => [g.name, g.group_id]));
    fillSelect($("new-group-creator"), names, names[0]);
    const list = $("groups-list");
    if (!groups.length) {
      list.innerHTML = `<div class="empty-state">Belum ada grup. Buat dengan tombol di atas.</div>`;
      return;
    }
    list.innerHTML = groups
      .map(
        (g) => `<div class="side-item ${g.name === state.selectedGroup ? "active" : ""}"
                   data-group="${esc(g.name)}">
          <div class="title">${esc(g.name)}</div>
          <div class="sub">${g.members.length} anggota · ${g.members.map(esc).join(", ")}</div>
        </div>`
      )
      .join("");
    list.querySelectorAll(".side-item").forEach((el) =>
      el.addEventListener("click", () => openGroup(el.dataset.group))
    );
    if (!groups.some((g) => g.name === state.selectedGroup)) {
      state.selectedGroup = null;
    }
    if (!state.selectedGroup) openGroup(groups[0].name);
  } catch (err) {
    toast(err.message, "err");
  }
}

$("new-group-btn").addEventListener("click", () =>
  $("new-group-form").classList.toggle("hidden")
);

$("new-group-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/groups", {
      method: "POST",
      body: JSON.stringify({
        as_agent: $("new-group-creator").value,
        name: $("new-group-name").value.trim(),
        description: $("new-group-desc").value.trim() || null,
      }),
    });
    $("new-group-form").reset();
    $("new-group-form").classList.add("hidden");
    toast("Grup dibuat.", "ok");
    await loadGroups();
  } catch (err) {
    toast(err.message, "err");
  }
});

async function openGroup(name) {
  state.selectedGroup = name;
  document.querySelectorAll("#groups-list .side-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.group === name)
  );
  $("group-chat-head").textContent = `Grup: ${name}`;
  $("group-messages").innerHTML = `<div class="empty-state">Memuat...</div>`;
  try {
    const { messages, members } = await loadGroupThread(name);
    renderMessages($("group-messages"), messages, { group: true });

    const names = state.agents.map((a) => a.name);
    state.groupMembers = members;
    const current = $("group-as-agent").value;
    fillSelect($("group-as-agent"), members, members.includes(current) ? current : members[0]);
    $("group-composer").classList.remove("hidden");
    updateGroupJoinButton();
  } catch (err) {
    $("group-messages").innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    $("group-composer").classList.add("hidden");
  }
}

async function loadGroupThread(name) {
  const data = await api(`/groups/${encodeURIComponent(name)}/messages`);
  // Daftar anggota diambil dari endpoint /groups (biar UI tahu siapa yang boleh kirim).
  const { groups } = await api("/groups");
  const g = groups.find((x) => x.name === name);
  return { messages: data.messages, members: g ? g.members : [] };
}

function updateGroupJoinButton() {
  const me = $("group-as-agent").value;
  const isMember = (state.groupMembers || []).includes(me);
  $("group-join-btn").classList.toggle("hidden", isMember);
}

$("group-as-agent").addEventListener("change", updateGroupJoinButton);

$("group-join-btn").addEventListener("click", async () => {
  try {
    await api(`/groups/${encodeURIComponent(state.selectedGroup)}/join`, {
      method: "POST",
      body: JSON.stringify({ as_agent: $("group-as-agent").value }),
    });
    toast(`${$("group-as-agent").value} bergabung ke grup.`, "ok");
    await openGroup(state.selectedGroup);
    await loadGroups();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("group-composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("group-input");
  const content = input.value.trim();
  if (!content) return;
  try {
    await api("/send-message", {
      method: "POST",
      body: JSON.stringify({
        as_agent: $("group-as-agent").value,
        target_type: "group",
        target_id: state.groupIds[state.selectedGroup],
        target_name: state.selectedGroup,
        content,
      }),
    });
    input.value = "";
    await openGroup(state.selectedGroup);
    await loadGroups();
  } catch (err) {
    toast(err.message, "err");
  }
});

/* ---------------- privat ---------------- */

async function loadPairs() {
  try {
    const names = await ensureAgents();
    fillSelect($("new-direct-from"), names, names[0]);
    fillSelect($("new-direct-to"), names, names[1] || names[0]);

    const { conversations } = await api("/conversations");
    const list = $("pairs-list");
    if (!conversations.length) {
      list.innerHTML = `<div class="empty-state">Belum ada chat privat. Buat dengan tombol di atas.</div>`;
      return;
    }
    list.innerHTML = conversations
      .map((c) => {
        const key = c.agents.join("|");
        return `<div class="side-item ${key === state.selectedPair ? "active" : ""}"
                     data-pair="${esc(key)}">
          <div class="title">${esc(c.agents[0])} ⇄ ${esc(c.agents[1])}</div>
          <div class="sub">${c.message_count} pesan · ${esc(c.last_preview.slice(0, 40))}</div>
        </div>`;
      })
      .join("");
    list.querySelectorAll(".side-item").forEach((el) =>
      el.addEventListener("click", () => openPair(el.dataset.pair.split("|")))
    );
    if (!state.selectedPair) openPair(conversations[0].agents);
  } catch (err) {
    toast(err.message, "err");
  }
}

$("new-direct-btn").addEventListener("click", () =>
  $("new-direct-form").classList.toggle("hidden")
);

$("new-direct-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const from = $("new-direct-from").value;
  const to = $("new-direct-to").value;
  if (from === to) {
    toast("Pilih dua agent yang berbeda.", "err");
    return;
  }
  $("new-direct-form").classList.add("hidden");
  openPair([from, to]);
});

async function openPair(agents) {
  state.selectedPair = agents.join("|");
  document.querySelectorAll("#pairs-list .side-item").forEach((el) =>
    el.classList.toggle("active", el.dataset.pair === state.selectedPair)
  );
  $("direct-chat-head").textContent = `${agents[0]} ⇄ ${agents[1]}`;
  $("direct-messages").innerHTML = `<div class="empty-state">Memuat...</div>`;
  try {
    const { messages } = await api(
      `/conversations/${encodeURIComponent(agents[0])}/${encodeURIComponent(agents[1])}`
    );
    renderMessages($("direct-messages"), messages, {});
    const current = $("direct-as-agent").value;
    fillSelect(
      $("direct-as-agent"),
      agents,
      agents.includes(current) ? current : agents[0]
    );
    $("direct-composer").classList.remove("hidden");
  } catch (err) {
    $("direct-messages").innerHTML = `<div class="empty-state">${esc(err.message)}</div>`;
    $("direct-composer").classList.add("hidden");
  }
}

$("direct-composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("direct-input");
  const content = input.value.trim();
  if (!content) return;
  const from = $("direct-as-agent").value;
  const to = state.selectedPair.split("|").find((a) => a !== from);
  try {
    await api("/send-message", {
      method: "POST",
      body: JSON.stringify({
        as_agent: from,
        target_type: "direct",
        target_id: state.agentIds[to],
        target_name: to,
        content,
      }),
    });
    input.value = "";
    await openPair(state.selectedPair.split("|"));
    // Segarkan daftar pasangan (preview dan urutan bisa berubah).
    const { conversations } = await api("/conversations");
    renderPairsList(conversations);
  } catch (err) {
    toast(err.message, "err");
  }
});

function renderPairsList(conversations) {
  const list = $("pairs-list");
  list.innerHTML = conversations
    .map((c) => {
      const key = c.agents.join("|");
      return `<div class="side-item ${key === state.selectedPair ? "active" : ""}"
                   data-pair="${esc(key)}">
        <div class="title">${esc(c.agents[0])} ⇄ ${esc(c.agents[1])}</div>
        <div class="sub">${c.message_count} pesan · ${esc(c.last_preview.slice(0, 40))}</div>
      </div>`;
    })
    .join("");
  list.querySelectorAll(".side-item").forEach((el) =>
    el.addEventListener("click", () => openPair(el.dataset.pair.split("|")))
  );
}

function renderMessages(container, messages, { group }) {
  if (!messages.length) {
    container.innerHTML = `<div class="empty-state">Belum ada pesan.</div>`;
    return;
  }
  container.innerHTML = messages
    .map((m) => {
      let body = "";
      if (m.msg_type === "file") {
        body = `<div>${esc(m.content || "")}</div>
          <div class="msg-file"><span class="badge badge-file">file</span>
            <span class="muted">${esc(m.file?.original_name || "")} (${fmtSize(m.file?.size_bytes)})</span></div>`;
      } else {
        body = esc(m.content || "");
      }
      const to = m.to ? ` → ${esc(m.to)}` : "";
      return `<div class="msg">
        <div class="bubble">${body}</div>
        <div class="meta"><strong>${esc(m.from)}</strong>${to} · ${fmtTime(m.created_at)}</div>
      </div>`;
    })
    .join("");
  container.scrollTop = container.scrollHeight;
}

/* ---------------- file manager ---------------- */

async function loadFiles() {
  try {
    const { files } = await api("/files");
    $("files-count").textContent = `(${files.length} file)`;
    const tbody = $("files-body");
    if (!files.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="empty-state">Belum ada file.</td></tr>`;
      return;
    }
    tbody.innerHTML = files
      .map((f) => {
        const status = f.deleted_at
          ? `<span class="badge badge-revoked">dihapus</span>`
          : f.exists_on_disk
            ? `<span class="badge badge-active">ada</span>`
            : `<span class="badge badge-suspended">hilang</span>`;
        const actions = f.deleted_at
          ? `<span class="muted">-</span>`
          : `<button class="btn btn-sm" data-download="${esc(f.stored_name)}">Download</button>
             <button class="btn btn-sm btn-danger" data-delete="${esc(f.stored_name)}">Hapus</button>`;
        return `<tr>
          <td><strong>${esc(f.original_name)}</strong><br>
              <span class="muted small">${esc(f.stored_name)}</span></td>
          <td>${fmtSize(f.size_bytes)}</td>
          <td>${esc(f.uploader)}</td>
          <td>${esc(f.target)}</td>
          <td>${fmtTime(f.uploaded_at)}</td>
          <td>${f.download_count}</td>
          <td>${status}</td>
          <td class="actions">${actions}</td>
        </tr>`;
      })
      .join("");
    tbody.querySelectorAll("[data-download]").forEach((b) =>
      b.addEventListener("click", () => {
        window.location = `${API}/files/${encodeURIComponent(b.dataset.download)}/download`;
      })
    );
    tbody.querySelectorAll("[data-delete]").forEach((b) =>
      b.addEventListener("click", () => deleteFile(b.dataset.delete))
    );
  } catch (err) {
    toast(err.message, "err");
  }
}

async function deleteFile(storedName) {
  if (!confirm(`Hapus file "${storedName}" secara permanen?`)) return;
  try {
    await api(`/files/${encodeURIComponent(storedName)}`, { method: "DELETE" });
    toast("File dihapus.", "ok");
    loadFiles();
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ---------------- ganti password ---------------- */

$("password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("password-message");
  msg.className = "hidden";
  if ($("new-password").value !== $("new-password2").value) {
    msg.textContent = "Password baru tidak sama.";
    msg.className = "err";
    return;
  }
  try {
    await api("/change-password", {
      method: "POST",
      body: JSON.stringify({
        old_password: $("old-password").value,
        new_password: $("new-password").value,
      }),
    });
    msg.textContent = "Password diperbarui.";
    msg.className = "ok";
    $("password-form").reset();
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "err";
  }
});

/* ---------------- start ---------------- */

initTheme();
checkSession().then((ok) => {
  if (ok) switchView("agents");
});
