"use strict";

/* ---------------- State ---------------- */

const state = {
  httpUrl: "",
  wsUrl: "",
  adminKey: "",
  sessionId: "",
  examName: "",
  ws: null,
  authenticated: false,
  reconnectAttempt: 0,
  reconnectTimer: null,
  manuallyDisconnected: false,
  students: new Map(), // student_id -> STUDENT_STATUS fields
  events: [], // newest first
  maxEvents: 500,
  selectedStudentId: null,
  filters: { status: "all", student: "", eventType: "", severity: "" },
  knownEventTypes: new Set(),
};

const EL = {
  connectModal: document.getElementById("connect-modal"),
  app: document.getElementById("app"),
  connectError: document.getElementById("connect-error"),
  headerExamName: document.getElementById("header-exam-name"),
  headerSessionId: document.getElementById("header-session-id"),
  statTotal: document.getElementById("stat-total"),
  statReview: document.getElementById("stat-review"),
  serverStatusDot: document.getElementById("server-status-dot"),
  serverStatusText: document.getElementById("server-status-text"),
  studentGrid: document.getElementById("student-grid"),
  detailPanel: document.getElementById("detail-panel"),
  detailContent: document.getElementById("detail-content"),
  eventFeedList: document.getElementById("event-feed-list"),
  filterEventType: document.getElementById("filter-event-type"),
};

/* ---------------- Helpers ---------------- */

function severityBucket(student) {
  if (student.status !== "online") return "offline";
  return student.overall_severity || "green";
}

function statusMatchesFilter(student) {
  const bucket = severityBucket(student);
  switch (state.filters.status) {
    case "all": return true;
    case "normal": return bucket === "green";
    case "review": return bucket === "yellow";
    case "critical": return bucket === "red";
    case "offline": return bucket === "offline";
    default: return true;
  }
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour12: false });
}

function fmtRelative(iso) {
  if (!iso) return "never";
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function titleCase(s) {
  return String(s || "").replace(/_/g, " ").replace(/\w\S*/g, (t) => t[0].toUpperCase() + t.slice(1).toLowerCase());
}

function authHeaders() {
  return { "X-Admin-Api-Key": state.adminKey };
}

/* ---------------- Connect flow ---------------- */

document.getElementById("btn-connect").addEventListener("click", connect);
document.getElementById("btn-disconnect").addEventListener("click", disconnect);

function restoreSavedConnection() {
  try {
    const saved = JSON.parse(sessionStorage.getItem("proctoring_dashboard_conn") || "null");
    if (saved) {
      document.getElementById("input-server-url").value = saved.httpUrl || "http://localhost:8000";
      document.getElementById("input-admin-key").value = saved.adminKey || "";
      document.getElementById("input-session-id").value = saved.sessionId || "";
      document.getElementById("input-exam-name").value = saved.examName || "";
    }
  } catch (e) { /* ignore corrupt storage */ }
}

function connect() {
  const httpUrl = document.getElementById("input-server-url").value.trim().replace(/\/$/, "");
  const adminKey = document.getElementById("input-admin-key").value.trim();
  const sessionId = document.getElementById("input-session-id").value.trim();
  const examName = document.getElementById("input-exam-name").value.trim() || sessionId;

  if (!httpUrl || !adminKey || !sessionId) {
    showConnectError("Server URL, Admin API Key, and Session ID are required.");
    return;
  }

  state.httpUrl = httpUrl;
  state.wsUrl = httpUrl.replace(/^http/, "ws");
  state.adminKey = adminKey;
  state.sessionId = sessionId;
  state.examName = examName;

  sessionStorage.setItem("proctoring_dashboard_conn", JSON.stringify({ httpUrl, adminKey, sessionId, examName }));

  EL.headerExamName.textContent = examName;
  EL.headerSessionId.textContent = sessionId;

  state.manuallyDisconnected = false;
  openWebSocket();
}

function disconnect() {
  state.manuallyDisconnected = true;
  if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
  if (state.ws) state.ws.close();
  EL.app.hidden = true;
  EL.connectModal.hidden = false;
  state.students.clear();
  state.events = [];
}

function showConnectError(msg) {
  EL.connectError.textContent = msg;
  EL.connectError.hidden = false;
}

/* ---------------- WebSocket ---------------- */

function openWebSocket() {
  setServerStatus("connecting", "Connecting…");
  // The admin key is sent in an AUTH frame rather than the URL: query strings
  // land in server access logs, browser history, and proxy logs in plaintext.
  const ws = new WebSocket(`${state.wsUrl}/ws/admin`);
  state.ws = ws;
  state.authenticated = false;

  ws.onopen = () => {
    ws.send(JSON.stringify({ type: "AUTH", api_key: state.adminKey, session_id: state.sessionId }));
  };

  ws.onmessage = (evt) => {
    let msg;
    try { msg = JSON.parse(evt.data); } catch (e) { return; }

    if (!state.authenticated) {
      if (msg.type === "AUTH_OK") {
        state.authenticated = true;
        state.reconnectAttempt = 0;
        setServerStatus("online", "Connected");
        EL.connectModal.hidden = true;
        EL.app.hidden = false;
        EL.connectError.hidden = true;
        return;
      }
      if (msg.type === "AUTH_FAILED") {
        // Bad credentials or a missing session won't fix themselves on retry,
        // so drop back to the connect form instead of reconnect-looping.
        state.manuallyDisconnected = true;
        setServerStatus("offline", "Not connected");
        EL.app.hidden = true;
        EL.connectModal.hidden = false;
        showConnectError(msg.reason || "Authentication failed.");
        ws.close();
        return;
      }
      return;
    }

    handleServerMessage(msg);
  };

  ws.onerror = () => { /* onclose handles reconnect */ };

  ws.onclose = () => {
    state.authenticated = false;
    if (state.manuallyDisconnected) return;
    setServerStatus("offline", "Disconnected — retrying…");
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  state.reconnectAttempt += 1;
  const backoff = Math.min(1000 * 2 ** state.reconnectAttempt, 30000);
  state.reconnectTimer = setTimeout(openWebSocket, backoff);
}

// The server scopes every stream to the session this dashboard authenticated
// for; this is a second, client-side check so a payload for another exam can
// never render here even if that ever regressed server-side.
function belongsToThisSession(sessionId) {
  return !sessionId || sessionId === state.sessionId;
}

function handleServerMessage(msg) {
  switch (msg.type) {
    case "SNAPSHOT":
      state.students.clear();
      for (const s of msg.students || []) {
        if (belongsToThisSession(s.session_id)) state.students.set(s.student_id, s);
      }
      state.events = (msg.recent_events || []).filter((e) => belongsToThisSession(e.session_id)).reverse();
      state.events.forEach((e) => state.knownEventTypes.add(e.event_type));
      refreshEventTypeFilterOptions();
      renderAll();
      break;
    case "STUDENT_STATUS": {
      const { type, ...student } = msg;
      if (!belongsToThisSession(student.session_id)) break;
      state.students.set(student.student_id, student);
      renderStudentGrid();
      renderHeaderStats();
      if (state.selectedStudentId === student.student_id) renderDetailPanel();
      break;
    }
    case "PROCTOR_EVENT": {
      const event = msg.payload;
      if (!belongsToThisSession(event.session_id)) break;
      state.events.unshift(event);
      if (state.events.length > state.maxEvents) state.events.pop();
      state.knownEventTypes.add(event.event_type);
      refreshEventTypeFilterOptions();
      renderEventFeed();
      if (state.selectedStudentId === event.student_id) renderDetailPanel();
      break;
    }
    default:
      break;
  }
}

function setServerStatus(cls, text) {
  EL.serverStatusDot.className = `dot ${cls}`;
  EL.serverStatusText.textContent = text;
}

function ackStudent(studentId) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify({ type: "ACK_STUDENT", student_id: studentId }));
  }
}

/* ---------------- Rendering: header ---------------- */

function renderHeaderStats() {
  const all = [...state.students.values()];
  EL.statTotal.textContent = all.length;
  EL.statReview.textContent = all.filter((s) => s.status === "online" && s.overall_severity !== "green").length;
}

/* ---------------- Rendering: student grid ---------------- */

function renderStudentGrid() {
  const students = [...state.students.values()]
    .filter(matchesActiveFilters)
    .sort((a, b) => {
      const rank = { red: 0, yellow: 1, green: 2 };
      const ba = severityBucket(a), bb = severityBucket(b);
      const ra = ba === "offline" ? 3 : rank[ba];
      const rb = bb === "offline" ? 3 : rank[bb];
      if (ra !== rb) return ra - rb;
      return a.display_name.localeCompare(b.display_name);
    });

  EL.studentGrid.innerHTML = "";
  if (students.length === 0) {
    EL.studentGrid.innerHTML = `<div class="empty-hint">No students match the current filters.</div>`;
    return;
  }

  for (const student of students) {
    EL.studentGrid.appendChild(renderStudentCard(student));
  }
}

function matchesActiveFilters(student) {
  if (!statusMatchesFilter(student)) return false;
  if (state.filters.student) {
    const q = state.filters.student.toLowerCase();
    if (!student.display_name.toLowerCase().includes(q) && !student.student_id.toLowerCase().includes(q)) return false;
  }
  return true;
}

const SEVERITY_EMOJI = { green: "🟢", yellow: "🟡", red: "🔴", offline: "⚪" };

function renderStudentCard(student) {
  const bucket = severityBucket(student);
  const card = document.createElement("div");
  card.className = `student-card sev-${bucket}${state.selectedStudentId === student.student_id ? " selected" : ""}`;
  card.dataset.studentId = student.student_id;

  const statusLabel = student.status === "online" ? "Online" : "Offline";
  const reviewLabel = bucket === "green" ? "Normal" : bucket === "yellow" ? "Review" : bucket === "red" ? "Critical" : "Offline";

  card.innerHTML = `
    <div class="student-card-top">
      <div>
        <div class="student-name">${SEVERITY_EMOJI[bucket]} ${escapeHtml(student.display_name)}</div>
        <div class="student-id-sub">${escapeHtml(student.student_id)}</div>
      </div>
      <span class="badge sev-${bucket}">${reviewLabel}</span>
    </div>
    <div class="student-card-row">
      <span class="online-indicator"><span class="dot ${student.status === "online" ? "online" : "offline"}"></span>${statusLabel}</span>
      <span class="value">${student.alert_count} alert${student.alert_count === 1 ? "" : "s"}</span>
    </div>
    <div class="student-card-row">
      <span>Last event</span>
      <span class="value">${student.last_event_type ? titleCase(student.last_event_type) : "—"}</span>
    </div>
    <div class="student-card-row">
      <span>Last seen</span>
      <span class="value">${fmtRelative(student.last_heartbeat)}</span>
    </div>
  `;
  card.addEventListener("click", () => selectStudent(student.student_id));
  return card;
}

function escapeHtml(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

/* ---------------- Rendering: event feed ---------------- */

function renderEventFeed() {
  const filtered = state.events.filter((e) => {
    if (state.filters.eventType && e.event_type !== state.filters.eventType) return false;
    if (state.filters.severity && e.severity !== state.filters.severity) return false;
    if (state.filters.student) {
      const q = state.filters.student.toLowerCase();
      const student = state.students.get(e.student_id);
      const name = student ? student.display_name.toLowerCase() : "";
      if (!name.includes(q) && !e.student_id.toLowerCase().includes(q)) return false;
    }
    return true;
  });

  EL.eventFeedList.innerHTML = "";
  if (filtered.length === 0) {
    EL.eventFeedList.innerHTML = `<div class="empty-feed">No events yet.</div>`;
    return;
  }

  for (const event of filtered.slice(0, 200)) {
    const student = state.students.get(event.student_id);
    const row = document.createElement("div");
    row.className = "event-row";
    row.innerHTML = `
      <span class="e-time">${fmtTime(event.timestamp)}</span>
      <span class="e-student">${escapeHtml(student ? student.display_name : event.student_id)}</span>
      <span class="e-type">${titleCase(event.event_type)}</span>
      <span class="badge sev-${event.severity}">${event.severity}</span>
      <div class="e-meta">${escapeHtml(JSON.stringify(event.metadata, null, 2))}</div>
    `;
    row.addEventListener("click", () => row.classList.toggle("expanded"));
    EL.eventFeedList.appendChild(row);
  }
}

function refreshEventTypeFilterOptions() {
  const current = EL.filterEventType.value;
  const options = ['<option value="">All event types</option>'];
  for (const type of [...state.knownEventTypes].sort()) {
    options.push(`<option value="${type}">${titleCase(type)}</option>`);
  }
  EL.filterEventType.innerHTML = options.join("");
  EL.filterEventType.value = current;
}

/* ---------------- Rendering: detail panel ---------------- */

async function selectStudent(studentId) {
  state.selectedStudentId = studentId;
  EL.detailPanel.hidden = false;
  renderStudentGrid();
  await renderDetailPanel();
}

document.getElementById("detail-close").addEventListener("click", () => {
  state.selectedStudentId = null;
  EL.detailPanel.hidden = true;
  renderStudentGrid();
});

async function renderDetailPanel() {
  const student = state.students.get(state.selectedStudentId);
  if (!student) return;
  const bucket = severityBucket(student);

  EL.detailContent.innerHTML = `
    <h2>${escapeHtml(student.display_name)}</h2>
    <div class="sub">${escapeHtml(student.student_id)} · ${escapeHtml(student.session_id)}</div>

    <div class="detail-section">
      <h3>Status</h3>
      <div class="detail-kv"><span class="k">Connection</span><span>${student.status === "online" ? "🟢 Online" : "⚪ Offline"}</span></div>
      <div class="detail-kv"><span class="k">Overall</span><span class="badge sev-${bucket}">${bucket}</span></div>
      <div class="detail-kv"><span class="k">Current state</span><span>${titleCase(student.current_state)}</span></div>
      <div class="detail-kv"><span class="k">Alerts</span><span>${student.alert_count}</span></div>
      <div class="detail-kv"><span class="k">Last heartbeat</span><span>${fmtRelative(student.last_heartbeat)}</span></div>
      <button class="ack-btn" id="btn-ack">Acknowledge &amp; Clear Alerts</button>
    </div>

    <div class="detail-section">
      <h3>Event Timeline</h3>
      <div id="detail-timeline"><div class="empty-hint">Loading…</div></div>
    </div>

    <div class="detail-section">
      <h3>Evidence Snapshots</h3>
      <div id="detail-evidence" class="evidence-thumbs"><div class="empty-hint">Loading…</div></div>
    </div>
  `;

  document.getElementById("btn-ack").addEventListener("click", () => ackStudent(student.student_id));

  renderTimelineFromLocalEvents(student.student_id);
  loadEvidence(student.student_id);
}

function renderTimelineFromLocalEvents(studentId) {
  const items = state.events.filter((e) => e.student_id === studentId).slice(0, 50);
  const container = document.getElementById("detail-timeline");
  if (!container) return;
  if (items.length === 0) {
    container.innerHTML = `<div class="empty-hint">No events recorded yet.</div>`;
    return;
  }
  container.innerHTML = items
    .map(
      (e) => `
      <div class="timeline-item sev-${e.severity}">
        <div class="t-time">${fmtTime(e.timestamp)}</div>
        <div class="t-type">${titleCase(e.event_type)}</div>
        <div>${escapeHtml(Object.entries(e.metadata || {}).map(([k, v]) => `${k}: ${v}`).join(", "))}</div>
      </div>`
    )
    .join("");
}

async function loadEvidence(studentId) {
  const container = document.getElementById("detail-evidence");
  if (!container) return;
  try {
    const res = await fetch(
      `${state.httpUrl}/api/evidence?session_id=${encodeURIComponent(state.sessionId)}&student_id=${encodeURIComponent(studentId)}`,
      { headers: authHeaders() }
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const items = await res.json();
    if (items.length === 0) {
      container.innerHTML = `<div class="empty-hint">No evidence snapshots captured.</div>`;
      return;
    }
    container.innerHTML = "";
    for (const item of items) {
      const img = document.createElement("img");
      img.alt = `Evidence ${item.evidence_id}`;
      img.title = `Captured ${item.captured_at}`;
      loadAuthenticatedImage(`${state.httpUrl}/api/evidence/${item.evidence_id}`).then((url) => {
        if (url) img.src = url;
      });
      container.appendChild(img);
    }
  } catch (e) {
    container.innerHTML = `<div class="empty-hint">Could not load evidence (${escapeHtml(String(e.message || e))}).</div>`;
  }
}

async function loadAuthenticatedImage(url) {
  try {
    const res = await fetch(url, { headers: authHeaders() });
    if (!res.ok) return null;
    const blob = await res.blob();
    return URL.createObjectURL(blob);
  } catch (e) {
    return null;
  }
}

/* ---------------- Filters wiring ---------------- */

document.getElementById("status-filters").addEventListener("click", (evt) => {
  const btn = evt.target.closest(".filter-chip");
  if (!btn) return;
  document.querySelectorAll("#status-filters .filter-chip").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.filters.status = btn.dataset.status;
  renderStudentGrid();
});

document.getElementById("filter-student").addEventListener("input", (evt) => {
  state.filters.student = evt.target.value;
  renderStudentGrid();
  renderEventFeed();
});

EL.filterEventType.addEventListener("change", (evt) => {
  state.filters.eventType = evt.target.value;
  renderEventFeed();
});

document.getElementById("filter-severity").addEventListener("change", (evt) => {
  state.filters.severity = evt.target.value;
  renderEventFeed();
});

/* ---------------- Render everything ---------------- */

function renderAll() {
  renderHeaderStats();
  renderStudentGrid();
  renderEventFeed();
}

restoreSavedConnection();
