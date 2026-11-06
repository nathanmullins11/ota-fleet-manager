// OTA Fleet Manager visual demo: polls /api/state every second and renders it.
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const timeOf = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour12: false }) : "");

let state = null;
let selectedStep = null;          // index; null = follow the next step to run
let drawerDevice = null;
const seen = { commands: {}, decisions: 0 };

// --------------------------------------------------------------------------- api

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function toast(text, error = false) {
  const el = document.createElement("div");
  el.className = "toast" + (error ? " error" : "");
  el.textContent = text;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), error ? 7000 : 4000);
}

async function act(path, body, okText) {
  try {
    const data = await api(path, body ?? {});
    toast(okText ? okText(data) : data.message || "Done");
    poll(true);
  } catch (e) {
    toast(e.message, true);
  }
}

// --------------------------------------------------------------------------- render

function renderHeader(s) {
  $("#mode").textContent = s.mode === "cloud"
    ? `control plane on the cloud VM (${s.cp_label}), devices on this computer`
    : "control plane and devices on this computer";
  const names = { mosquitto: "Broker", postgres: "Device registry",
    "software-registry": "Software registry", orchestrator: "Orchestrator" };
  const svc = Object.entries(names).map(([k, label]) => {
    const ok = s.services[k];
    return `<span class="svc"><i class="dot ${ok ? "ok" : ok === false ? "bad" : ""}"></i>${label}</span>`;
  });
  svc.push(`<span class="svc"><i class="dot ${s.mqtt_connected ? "ok" : "bad"}"></i>MQTT listener</span>`);
  $("#services").innerHTML = svc.join("");
  const banner = $("#banner");
  if (s.control_plane_error) {
    banner.textContent = "Control plane: " + s.control_plane_error;
    banner.classList.remove("hidden");
  } else banner.classList.add("hidden");
}

function deviceTile(d) {
  const status = d.status || "unregistered";
  const fresh = d.heartbeat_seen_s != null && d.heartbeat_seen_s < 1.2;
  const stale = d.heartbeat_seen_s == null || d.heartbeat_seen_s > 12;
  const badges = [];
  if (d.crashes > 0) badges.push(`<span class="badge red">crash ×${d.crashes}</span>`);
  if (d.errors > 0) badges.push(`<span class="badge yellow">${d.errors} errors</span>`);
  if (d.rollout_state) {
    const color = { updated: "green", failed: "red", rolled_back: "yellow" }[d.rollout_state] || "blue";
    badges.push(`<span class="badge ${color}">rollout: ${esc(d.rollout_state)}</span>`);
  }
  if (d.last_command) {
    badges.push(`<span class="badge purple">⇢ ${esc(d.last_command.payload.command)} ${esc(d.last_command.payload.target_version)}</span>`);
  }
  const cmdKey = d.last_command ? d.last_command.time + d.last_command.payload.command : "";
  const flash = cmdKey && seen.commands[d.device_id] && seen.commands[d.device_id] !== cmdKey;
  seen.commands[d.device_id] = cmdKey;

  const local = d.container != null;
  const actions = local ? `
      ${d.container === "running"
        ? `<button class="small" data-dev="stop" data-id="${esc(d.device_id)}">Stop</button>`
        : `<button class="small" data-dev="start" data-id="${esc(d.device_id)}">Start</button>`}
      <button class="small" data-dev="crash" data-id="${esc(d.device_id)}" title="Simulated crash heartbeat">Crash</button>
      <button class="small" data-dev="bad-message" data-id="${esc(d.device_id)}" title="Publish a malformed heartbeat">Bad msg</button>` : "";
  const seenTxt = d.seconds_since_heartbeat == null ? "never" : `${d.seconds_since_heartbeat}s ago`;
  return `
    <div class="device ${esc(status)}${flash ? " flash" : ""}" data-open="${esc(d.device_id)}">
      <div class="top">
        <span class="id">${esc(d.device_id)}</span>
        <i class="pulse${fresh ? " beat" : ""}${stale ? " stale" : ""}" title="heartbeat"></i>
      </div>
      <div class="version">${esc(d.version || "–")} <small>${d.previous_version ? "prev " + esc(d.previous_version) : ""}</small></div>
      <div class="status"><i class="dot ${esc(status)}"></i> ${esc(status)}</div>
      <div class="meta">last heartbeat ${esc(seenTxt)}${d.container === "stopped" ? " · container stopped" : ""}</div>
      <div class="badges">${badges.join("")}</div>
      <div class="actions">${actions}</div>
    </div>`;
}

function renderDevices(s) {
  $("#devices").innerHTML = s.devices.length
    ? s.devices.map(deviceTile).join("")
    : `<div class="empty">No devices yet. Run guided step 2 or press <b>Start fleet</b>.</div>`;
}

function renderRollout(s) {
  const r = s.rollout;
  if (!r) {
    $("#rollout").innerHTML = `<div class="empty">No rollout yet. Start one to send update commands.</div>`;
    return;
  }
  const segs = r.devices.map((d) =>
    `<div class="seg ${esc(d.state)}" title="${esc(d.device_id)}: ${esc(d.state)}">${esc(d.device_id.replace("device-", ""))} · ${esc(d.state)}</div>`).join("");
  const events = r.events.slice(-6).map((e) => `
    <div class="event"><span class="muted">${esc(timeOf(e.time))}</span><span class="ev">${esc(e.event)}</span><span>${esc(e.reason || "")}</span></div>`).join("");
  $("#rollout").innerHTML = `
    <div class="rollout-title">
      <span class="big">#${r.id} · ${esc(r.target_version)}</span>
      <span class="state ${esc(r.state)}">${esc(r.state.replace("_", " ").toUpperCase())}</span>
      <span class="muted">rollback version ${esc(r.rollback_version)}</span>
    </div>
    <div class="progress">${segs}</div>
    <div class="events">${events}</div>`;
}

function renderDecisions(s) {
  const lines = s.decisions;
  const fresh = Math.max(0, lines.length - seen.decisions);
  const box = $("#decisions");
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
  box.innerHTML = lines.map((d, i) => {
    const cls = [d.level, d.source.includes("rollouts") ? "rollout" : "",
                 seen.decisions && i >= lines.length - fresh ? "new" : ""].join(" ");
    return `<div class="line ${cls}"><span class="t">${esc(d.time.slice(11))}</span><span>${esc(d.text)}</span></div>`;
  }).join("") || `<div class="muted">No decisions yet.</div>`;
  seen.decisions = lines.length;
  if (atBottom || fresh) box.scrollTop = box.scrollHeight;
}

function renderFirmware(s) {
  $("#firmware").innerHTML = s.firmware.map((f) => `
    <div class="fw"><span class="v">${esc(f.version)}</span><span class="mono">${esc(f.sha256.slice(0, 16))}…</span><span class="muted">${esc(f.notes)}</span></div>`).join("")
    || `<div class="muted">Not synced yet.</div>`;
  const fc = s.firmware_check;
  $("#firmware-check").innerHTML = fc ? `<h3>Verification of ${esc(fc.version)} (device checks)</h3>` + fc.cases.map((c) => `
    <div class="case">
      <span>${c.accepted === c.expected ? "✔" : "✘"}</span>
      <span>${esc(c.title)}</span>
      <span class="verdict ${c.accepted ? "acc" : "rej"}">${c.accepted ? "ACCEPTED" : "REJECTED"}</span>
      ${c.reason ? `<span class="why">${esc(c.reason)}</span>` : ""}
    </div>`).join("") : "";
  $("#crashes").innerHTML = s.crashes.length ? s.crashes.map((c) => `
    <div class="fw"><span class="v">${esc(timeOf(c.time))}</span><span>${esc(c.device_id)} · v${esc(c.version)}</span><span class="muted">errors ${c.errors} · crashes ${c.crashes}</span></div>`).join("")
    : `<div class="muted">None.</div>`;
}

const STATUS_ICON = { ready: "", running: '<span class="spinner"></span>', done: "✔", failed: "✘" };

function renderSteps(s) {
  const steps = s.steps;
  const running = steps.find((st) => st.status === "running");
  if (selectedStep === null) {
    const next = steps.find((st) => st.status !== "done");
    selectedStep = next ? next.index : steps.length - 1;
  }
  $("#step-rail").innerHTML = steps.map((st) => `
    <div class="step-card ${st.status}${st.index === selectedStep ? " selected" : ""}" data-step="${st.index}">
      <span class="st">${STATUS_ICON[st.status]}</span>
      <div class="n">Step ${st.index + 1}</div>
      <div class="t">${esc(st.title)}</div>
      <div class="c">${esc(st.covers)}</div>
    </div>`).join("");

  const st = steps[selectedStep];
  const log = st.log.map((l) => `<div class="l ${esc(l.kind)}">${esc(l.text)}</div>`).join("");
  $("#step-detail").innerHTML = `
    <div>
      <h3>Step ${st.index + 1}: ${esc(st.title)} <span class="muted">· ${esc(st.covers)}</span></h3>
      <div class="explain">${esc(st.explain)}</div>
      <div class="look">👀 ${esc(st.look)}</div>
      <div class="run">
        <button class="primary" data-run="${st.index}" ${running ? "disabled" : ""}>
          ${st.status === "done" || st.status === "failed" ? "Run again" : "Run step " + (st.index + 1)}</button>
        ${st.index + 1 < steps.length ? `<button data-step-go="${st.index + 1}">Next →</button>` : ""}
      </div>
      ${st.progress ? `<div class="progress-text"><span class="spinner"></span> ${esc(st.progress)}</div>` : ""}
    </div>
    <div class="step-log" id="step-log">${log || '<span class="muted">Press Run to start this step.</span>'}</div>`;
  const logBox = $("#step-log");
  logBox.scrollTop = logBox.scrollHeight;

  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("focus", p.dataset.focus === st.focus));
  document.querySelectorAll(".svc").forEach((el) =>
    el.style.boxShadow = st.focus === "services" ? "0 0 0 2px rgba(76,154,255,.5)" : "");
}

function renderDrawer(s) {
  if (!drawerDevice) return;
  const d = s.devices.find((x) => x.device_id === drawerDevice);
  if (!d) return;
  $("#drawer-title").textContent = d.device_id;
  const cmd = d.last_command;
  $("#drawer-body").innerHTML = `
    <div class="kv">
      <div>Version</div><div>${esc(d.version || "–")}</div>
      <div>Previous version</div><div>${esc(d.previous_version || "–")}</div>
      <div>Status</div><div>${esc(d.status)}</div>
      <div>Last heartbeat</div><div>${d.seconds_since_heartbeat == null ? "never" : d.seconds_since_heartbeat + " s ago"}</div>
      <div>Errors / crashes</div><div>${d.errors} / ${d.crashes}</div>
      <div>Container</div><div>${esc(d.container || "not on this computer")}</div>
      <div>Rollout state</div><div>${esc(d.rollout_state || "–")}</div>
    </div>
    <h3>Last command received ${cmd ? "· " + esc(timeOf(cmd.time)) : ""}</h3>
    ${cmd ? `<pre>${esc(JSON.stringify(cmd.payload, null, 2))}</pre>`
          : `<div class="muted">No command yet. Start a rollout to send one.</div>`}`;
}

function render(s) {
  renderHeader(s);
  renderDevices(s);
  renderRollout(s);
  renderDecisions(s);
  renderFirmware(s);
  renderSteps(s);
  renderDrawer(s);
}

// --------------------------------------------------------------------------- loop

let timer = null;
async function poll(now = false) {
  clearTimeout(timer);
  try {
    state = await api("/api/state");
    render(state);
  } catch (e) {
    const banner = $("#banner");
    banner.textContent = "Lost contact with the visual demo server (" + e.message + "). Is run.sh still running?";
    banner.classList.remove("hidden");
  }
  timer = setTimeout(poll, 1000);
}

// --------------------------------------------------------------------------- events

document.addEventListener("click", (ev) => {
  const t = ev.target.closest("[data-action],[data-dev],[data-run],[data-step],[data-step-go],[data-open],#drawer-close");
  if (!t) return;
  if (t.id === "drawer-close") { drawerDevice = null; $("#drawer").classList.add("hidden"); return; }
  if (t.dataset.dev) {
    ev.stopPropagation();
    return act(`/api/devices/${t.dataset.id}/${t.dataset.dev}`);
  }
  if (t.dataset.run !== undefined) {
    return act(`/api/steps/${t.dataset.run}/run`, {}, () => `Running step ${+t.dataset.run + 1}`);
  }
  if (t.dataset.step !== undefined) { selectedStep = +t.dataset.step; return state && render(state); }
  if (t.dataset.stepGo !== undefined) { selectedStep = +t.dataset.stepGo; return state && render(state); }
  if (t.dataset.open) {
    drawerDevice = t.dataset.open;
    $("#drawer").classList.remove("hidden");
    return state && renderDrawer(state);
  }
  switch (t.dataset.action) {
    case "start-fleet": return act("/api/devices/start-fleet");
    case "add-device": return act("/api/devices/add");
    case "firmware-check": return act("/api/firmware/check", {}, () => "Firmware verified");
    case "rollout": return act("/api/rollouts", { version: $("#rollout-version").value },
      (d) => `Rollout ${d.id} created: ${d.commanding.length} device(s) to ${d.target_version}`
             + (d.skipped_offline.length ? `, skipped offline: ${d.skipped_offline.join(", ")}` : ""));
    case "rollback": return act("/api/rollouts/rollback", {},
      (d) => `Rolling back rollout ${d.id} to ${d.rollback_version}`);
    case "reply-succeed": return act("/api/rollouts/reply", { outcome: "succeed" });
    case "reply-fail": return act("/api/rollouts/reply", { outcome: "fail_one" });
    case "reset-steps": selectedStep = null; return act("/api/steps/reset", {}, () => "Steps reset");
  }
});

document.addEventListener("keydown", (ev) => {
  // Buttons handle Enter themselves (as a click); selects use the arrow keys.
  if (!state || ["SELECT", "BUTTON", "INPUT"].includes(ev.target.tagName)) return;
  const n = state.steps.length;
  if (ev.key === "ArrowRight") { selectedStep = Math.min(n - 1, (selectedStep ?? 0) + 1); render(state); }
  else if (ev.key === "ArrowLeft") { selectedStep = Math.max(0, (selectedStep ?? 0) - 1); render(state); }
  else if (ev.key === "Enter" && selectedStep !== null) {
    if (!state.steps.some((s) => s.status === "running")) act(`/api/steps/${selectedStep}/run`, {}, () => `Running step ${selectedStep + 1}`);
  } else if (ev.key === "Escape") { drawerDevice = null; $("#drawer").classList.add("hidden"); }
});

poll();
