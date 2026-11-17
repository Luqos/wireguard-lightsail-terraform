"use strict";

// The per-launch token arrives in the URL fragment (never sent to the server or logs);
// keep it for this tab only and hide it from the address bar.
const TOKEN = (() => {
  const m = location.hash.match(/token=([\w-]+)/);
  let t = m ? m[1] : null;
  try { if (t) sessionStorage.setItem("wgmon-token", t); else t = sessionStorage.getItem("wgmon-token"); } catch { /* storage blocked */ }
  if (m) history.replaceState(null, "", location.pathname);
  return t;
})();

let S = null;                       // last /api/state
let view = "users", filter = "all", query = "";
try { view = localStorage.getItem("wgmon-view") || "users"; } catch { /* storage blocked */ }

// ------------------------------------------------------------ utilities

async function api(method, path, body) {
  const r = await fetch("/api" + path, {
    method,
    headers: { "X-Token": TOKEN || "", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

// Tiny DOM builder: text always goes through textContent, never innerHTML.
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  return el;
}
const SVG = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  el.append(...kids);
  return el;
}

function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return `${b.toFixed(b < 10 && i ? 1 : 0)} ${u[i]}`;
}
const fmtGB = (gb) => gb == null ? "–" : gb < 1 ? `${Math.round(gb * 1024)} MB` : `${gb.toFixed(1)} GB`;
function ago(epoch) {
  if (!epoch) return "never";
  const d = Date.now() / 1000 - epoch;
  if (d < 90) return "just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} d ago`;
}
const fullTime = (epoch) => epoch ? new Date(epoch * 1000).toLocaleString() : "";
const STATUS_TEXT = { online: "Online", active: "Active", idle: "Idle", never: "Never connected", unknown: "No data" };
const statusEl = (st) => h("span", { class: "status" }, h("span", { class: `dot ${st}` }), STATUS_TEXT[st] || st);

function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove("show"), 2200);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Copy failed: select the text manually"); }
}
const cmdBox = (text) => h("div", { class: "cmd" }, h("pre", { text }), h("button", { class: "btn small", onclick: () => copy(text), text: "Copy" }));

async function act(fn, okMsg) {
  try { S = await fn(); render(); if (okMsg) toast(okMsg); return true; }
  catch (e) { toast(e.message); return false; }
}

// ------------------------------------------------------------ data helpers

const serverNames = () => (S?.servers || []).map((x) => x.name);
const runningServers = () => (S?.servers || []).filter((x) => x.state === "running" && x.ip).map((x) => x.name);
const PEER_NAME = "[a-z0-9][a-z0-9._\\-]{0,31}";

const FILTERS = {
  all:    { label: "Users",         test: (p) => p.stage !== "former" },
  online: { label: "Online now",    test: (p) => p.status === "online" },
  active: { label: "Active (7 d)",  test: (p) => p.status === "online" || p.status === "active" },
  idle:   { label: "Idle / never",  test: (p) => p.stage !== "former" && (p.status === "idle" || p.status === "never") },
  unpaid: { label: "Unpaid / overdue", test: (p) => p.stage !== "former" && (!p.paid || p.overdue) },
  trial:  { label: "Trials",        test: (p) => p.stage === "trial" },
  former: { label: "Former",        test: (p) => p.stage === "former" },
};

// ------------------------------------------------------------ views

function renderUsers() {
  const people = S.people;
  const tiles = h("div", { class: "tiles" }, Object.entries(FILTERS).map(([key, f]) =>
    h("button", { class: "tile", "aria-pressed": String(filter === key), onclick: () => { filter = key; render(); } },
      h("span", { class: "muted", text: f.label }), h("b", { text: people.filter(f.test).length }))));

  const notices = [];
  if (S.unknown_peers.length) {
    notices.push(h("section", { class: "card notice" },
      h("h2", { text: "Peers on a server that aren't in the tracker" }),
      h("div", { class: "muted", text: "Created with wg-setup add but not recorded here yet." }),
      h("ul", {}, S.unknown_peers.map((p) => h("li", {},
        h("span", { class: "chip" }, p.server, " · ", h("b", { text: p.peer_name })), " ",
        statusEl(p.status), " ",
        h("button", { class: "btn small", text: "Add as user", onclick: () => openPerson(null, { server: p.server, peer_name: p.peer_name }) }))))));
  }
  if (S.missing_peers.length) {
    notices.push(h("section", { class: "card notice" },
      h("h2", { text: "Tracked peers missing on the server" }),
      h("div", { class: "muted", text: "Removed on the server, or the name doesn't match. Fix the record or re-add the peer." }),
      h("ul", {}, S.missing_peers.map((p) => h("li", {},
        h("b", { text: p.person }), " expects ", h("span", { class: "chip missing" }, `${p.server} · ${p.peer_name}`))))));
  }

  const q = query.toLowerCase();
  const rows = people.filter(FILTERS[filter].test).filter((p) =>
    !q || [p.name, p.contact, p.notes, p.device, ...p.peers.map((x) => x.peer_name)].join(" ").toLowerCase().includes(q));

  const table = h("table", {},
    h("thead", {}, h("tr", {}, ["Status", "User", "Stage", "Peers", "Last seen", "This month", "Paid", "Paid until", "Notes", ""].map((t) => h("th", { text: t })))),
    h("tbody", {}, rows.length ? rows.map(personRow) :
      h("tr", {}, h("td", { colspan: 10, class: "empty", text: people.length ? "No users match this filter." : "No users yet. Add one, or adopt a peer from the list above." }))));

  return [tiles, ...notices, h("section", { class: "card" },
    h("div", { class: "toolbar" },
      h("input", { type: "search", placeholder: "Search name, contact, notes…", value: query, "aria-label": "Search users",
        oninput: (e) => { query = e.target.value; renderKeepFocus(); } }),
      h("button", { class: "btn", text: "Track existing peer", onclick: () => openPerson(null) }),
      h("button", { class: "btn primary", text: "+ Add user", onclick: openCreate })),
    h("div", { class: "table-wrap" }, table))];
}

function personRow(p) {
  const stage = h("span", { class: `badge ${p.stage}`, text: p.stage });
  const until = p.paid_until ? h("span", { class: p.overdue ? "bad" : "", text: p.paid_until + (p.overdue ? " (overdue)" : "") }) : h("span", { class: "muted", text: "–" });
  const actions = [
    h("button", { class: "btn small", text: "Edit", onclick: () => openPerson(p) }),
    h("button", { class: "btn small danger", text: p.stage === "trial" && !p.paid ? "Revoke" : "Remove", onclick: () => openRemove(p) }),
  ];

  return h("tr", {},
    h("td", {}, statusEl(p.status)),
    h("td", { class: "name" }, h("b", { text: p.name }), h("span", { class: "muted", text: [p.device, p.contact].filter(Boolean).join(" · ") })),
    h("td", {}, stage),
    h("td", {}, p.peers.length ? p.peers.map((x) => h("span", { class: `chip ${x.on_server === false ? "missing" : ""}`, title: `${x.ip || "no IP yet"} · ${STATUS_TEXT[x.status]}` },
      h("span", { class: `dot ${x.status}` }), `${x.server} · ${x.peer_name}`)) : h("span", { class: "muted", text: "none" })),
    h("td", { title: fullTime(p.last_seen), text: ago(p.last_seen) }),
    h("td", { text: fmtBytes(p.month_bytes) }),
    h("td", {}, h("input", { type: "checkbox", checked: p.paid, "aria-label": `${p.name} paid`,
      onchange: (e) => act(() => api("PATCH", `/people/${p.id}`, { paid: e.target.checked }), e.target.checked ? `${p.name} marked paid` : `${p.name} marked unpaid`) })),
    h("td", {}, until),
    h("td", { class: "notes", text: p.notes }),
    h("td", {}, h("div", { class: "actions" }, actions)));
}

function renderServers() {
  if (!S.servers.length) return h("section", { class: "card empty", text: S.refreshing || !S.refreshed_at ? "Loading servers…" : "No servers tagged project=wireguard-vpn found." });
  return h("div", { class: "grid2" }, S.servers.map((sv) => {
    const pct = sv.allowance_gb ? Math.min(100, (sv.used_gb / sv.allowance_gb) * 100) : 0;
    const tone = pct >= 90 ? "bad" : pct >= 70 ? "warn" : "";
    const over = sv.projected_gb != null && sv.projected_gb > sv.allowance_gb;
    const peers = S.people.flatMap((p) => p.peers.filter((x) => x.server === sv.name));
    const online = peers.filter((x) => x.status === "online").length;
    return h("section", { class: "card" },
      h("header", {}, h("h2", { text: sv.name }), h("span", { class: "muted", text: `${sv.region} · ${sv.ip || "no IP"} · ${sv.state}` }),
        h("span", { class: `badge ${sv.alarm === "ALARM" ? "bad" : ""}`, text: `alarm: ${sv.alarm}` })),
      h("div", { class: "big" }, fmtGB(sv.used_gb), h("span", { class: "muted", text: ` of ${sv.allowance_gb} GB this month` })),
      h("div", { class: "meter" }, h("div", { class: tone, style: { width: `${pct.toFixed(1)}%` } })),
      h("div", { class: "stats" },
        h("div", {}, h("span", { text: "Used" }), h("b", { text: `${pct.toFixed(1)}%` })),
        h("div", {}, h("span", { text: "Projected month-end" }), h("b", { class: over ? "bad" : "", text: sv.projected_gb == null ? "too early" : fmtGB(sv.projected_gb) })),
        h("div", {}, h("span", { text: "Tracked peers online" }), h("b", { text: `${online} / ${peers.length}` }))),
      bars(sv),
      h("div", { class: "legend" }, h("span", {}, h("i", { class: "out" }), "out"), h("span", {}, h("i", { class: "in" }), "in"), h("span", { text: "daily, UTC; in + out count toward the allowance" })),
      h("dl", { class: "kv" },
        h("dt", { text: "Plan" }), h("dd", { text: `${sv.bundle}${sv.static ? " · static IP" : " · ⚠ dynamic IP"}` }),
        h("dt", { text: "Per-user stats" }), h("dd", { class: sv.stats_ok ? "" : "bad", text: sv.stats_ok ? `OK · ${sv.peer_count} peers on server` : (sv.stats_error || "not fetched") }),
        h("dt", { text: "Pinned host key" }), h("dd", {}, h("code", { text: sv.fingerprint || "not pinned yet" })),
        ...(sv.errors.length ? [h("dt", { text: "Errors" }), h("dd", { class: "bad", text: sv.errors.join("; ") })] : [])));
  }));
}

function bars(sv) {
  const days = sv.daily, W = 600, H = 110;
  const max = Math.max(0.001, ...days.map((d) => d.in_gb + d.out_gb));
  const bw = W / 31;
  const svg = s("svg", { class: "bars", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img", "aria-label": `Daily transfer for ${sv.name}` });
  for (const d of days) {
    const x = (d.day - 1) * bw + 1, hi = (d.in_gb / max) * H, ho = (d.out_gb / max) * H;
    svg.append(s("g", {}, s("title", {}, `Day ${d.day}: in ${fmtGB(d.in_gb)}, out ${fmtGB(d.out_gb)}`),
      s("rect", { class: "in", x, y: H - hi, width: bw - 2, height: hi }),
      s("rect", { class: "out", x, y: H - hi - ho, width: bw - 2, height: ho })));
  }
  return svg;
}

let setupCache = null;
function renderSetup() {
  const box = h("div", {}, h("section", { class: "card empty", text: "Loading…" }));
  api("GET", "/setup").then((info) => { setupCache = info; box.replaceChildren(...setupContent(info)); })
    .catch((e) => box.replaceChildren(h("section", { class: "card bad", text: e.message })));
  if (setupCache) box.replaceChildren(...setupContent(setupCache));
  return box;
}

function setupContent(info) {
  const peerCmds = [
    "sudo cloud-init status --wait        # first boot only",
    "sudo wg-setup filter off             # full tunnel (already done once per server)",
    "sudo wg-setup add <name>             # QR code for phones, /root/wg-clients/<name>.conf for PCs",
    "sudo cat /root/wg-clients/<name>.conf",
    "sudo wg-setup list                   # peers, last handshake, transfer",
    "sudo wg-setup remove <name>",
  ].join("\n");
  return [
    h("section", { class: "card" },
      h("h2", { text: "Connect the monitor to a server (once per server)" }),
      h("ol", { class: "steps" },
        h("li", {}, "In the Lightsail console, open the server and click ", h("b", { text: "Connect using SSH" }), "."),
        h("li", {}, "Paste this one line. It installs the current wg-setup (checksum-verified), then sets up the read-only ",
          h("code", { text: "wgmon" }), " account (stats) and the ", h("code", { text: "wgadmin" }),
          " account (add/remove only, max 10 adds a day, logged to /var/log/wg-admin.log). Re-run it whenever wg-setup.sh changes:",
          cmdBox(info.update_cmd)),
        h("li", {}, "It prints the server's host key fingerprint. Press ", h("b", { text: "Refresh" }),
          " here, then check it matches the pinned key below. If it doesn't, stop and tell me."))),
    h("section", { class: "card" },
      h("h2", { text: "Servers" }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["Server", "IP", "Per-user stats", "Pinned host key"].map((t) => h("th", { text: t })))),
        h("tbody", {}, info.servers.map((sv) => h("tr", {},
          h("td", { text: sv.name }), h("td", { text: sv.ip || "–" }),
          h("td", { class: sv.stats_ok ? "" : "bad", text: sv.stats_ok ? "OK" : (sv.stats_error || "not fetched") }),
          h("td", {}, h("code", { text: sv.fingerprint || "not pinned yet" })))))))),
    h("section", { class: "card" }, h("h2", { text: "Peer commands (browser SSH)" }), cmdBox(peerCmds)),
    h("section", { class: "card" }, h("h2", { text: "This app's SSH public keys" }),
      h("div", { class: "muted", text: "Safe to share. The private keys stay in %LOCALAPPDATA%\\wg-monitor, readable only by you." }),
      h("div", { text: "wgmon (stats, read-only)" }), cmdBox(info.pubkey),
      h("div", { text: "wgadmin (add/remove peers)" }), cmdBox(info.admin_pubkey)),
    h("section", { class: "card" }, h("h2", { text: "Audit log (browser SSH)" }),
      cmdBox("sudo tail -n 20 /var/log/wg-admin.log     # every add/remove made from this app")),
  ];
}

// ------------------------------------------------------------ dialogs

const dialog = () => document.getElementById("dialog");
function showDialog(...content) { const d = dialog(); d.replaceChildren(...content); if (!d.open) d.showModal(); }
const closeBtn = (text = "Cancel") => h("button", { type: "button", class: "btn", text, onclick: () => dialog().close() });

function openPerson(p, adopt) {
  const editing = !!p;
  const servers = [...new Set([...serverNames(), ...(p?.peers || []).map((x) => x.server), ...(adopt ? [adopt.server] : [])])];
  const v = p || { name: adopt?.peer_name || "", stage: "member", device: "", contact: "", notes: "", paid: false, paid_until: "", peers: adopt ? [adopt] : [] };
  const err = h("div", { class: "error" });
  const f = {
    name: h("input", { required: true, value: v.name, pattern: "[A-Za-z0-9._-]{1,32}", title: "Letters, digits, . _ - (max 32), same as the wg-setup peer name" }),
    stage: h("select", {}, ["trial", "member", "former"].map((x) => h("option", { value: x, selected: v.stage === x, text: x }))),
    device: h("select", {}, ["", "phone", "pc", "phone + pc", "other"].map((x) => h("option", { value: x, selected: v.device === x, text: x || "–" }))),
    contact: h("input", { value: v.contact, placeholder: "Discord, phone…" }),
    paid: h("input", { type: "checkbox", checked: v.paid }),
    paid_until: h("input", { type: "date", value: v.paid_until }),
    notes: h("textarea", { text: v.notes }),
  };
  const peerInputs = servers.map((srv) => {
    const cur = v.peers.find((x) => x.server === srv);
    const on = h("input", { type: "checkbox", checked: !!cur });
    const name = h("input", { value: cur?.peer_name || "", placeholder: "peer name on server" });
    on.addEventListener("change", () => { if (on.checked && !name.value) name.value = f.name.value; });
    return { srv, cur, on, name, row: h("label", { class: "peer-row" }, h("span", {}, on, " ", srv), name) };
  });

  async function save(e) {
    e.preventDefault();
    const body = { name: f.name.value.trim(), stage: f.stage.value, device: f.device.value, contact: f.contact.value,
      paid: f.paid.checked, paid_until: f.paid_until.value, notes: f.notes.value };
    const wanted = peerInputs.filter((x) => x.on.checked).map((x) => ({ server: x.srv, peer_name: x.name.value.trim() || body.name }));
    try {
      if (!editing) {
        S = await api("POST", "/people", { ...body, peers: wanted });
      } else {
        await api("PATCH", `/people/${p.id}`, body);
        for (const x of peerInputs) {
          const want = x.on.checked ? (x.name.value.trim() || body.name) : null;
          if (x.cur && x.cur.peer_name !== want) await api("DELETE", `/peers/${x.cur.id}`);
          if (want && (!x.cur || x.cur.peer_name !== want)) await api("POST", `/people/${p.id}/peers`, { server: x.srv, peer_name: want });
        }
        S = await api("GET", "/state");
      }
      dialog().close(); render(); toast(editing ? "Saved" : `${body.name} added`);
    } catch (ex) { err.textContent = ex.message; if (editing) { S = await api("GET", "/state").catch(() => S); render(); } }
  }

  showDialog(h("form", { onsubmit: save },
    h("h2", { text: editing ? `Edit ${p.name}` : "Track an existing peer" }),
    editing ? null : h("p", { class: "muted", text: "For peers already created with wg-setup add. To create a new user, use + Add user instead." }),
    h("div", { class: "row" },
      h("label", { class: "field" }, h("span", { text: "Name" }), f.name),
      h("label", { class: "field" }, h("span", { text: "Stage" }), f.stage)),
    h("div", { class: "row" },
      h("label", { class: "field" }, h("span", { text: "Device" }), f.device),
      h("label", { class: "field" }, h("span", { text: "Contact" }), f.contact)),
    h("div", { class: "row" },
      h("label", { class: "check" }, f.paid, "Paid"),
      h("label", { class: "field" }, h("span", { text: "Paid until" }), f.paid_until)),
    h("fieldset", {}, h("legend", { text: "Peers (must match the name used in wg-setup add)" }), peerInputs.map((x) => x.row)),
    h("label", { class: "field" }, h("span", { text: "Notes" }), f.notes),
    err,
    h("div", { class: "dialog-actions" }, closeBtn(), h("button", { class: "btn primary", type: "submit", text: editing ? "Save" : "Add" }))));
  f.name.focus();
}

function openCreate() {
  const servers = runningServers();
  const err = h("div", { class: "error" });
  const f = {
    name: h("input", { required: true, pattern: PEER_NAME, autocomplete: "off", placeholder: "e.g. aiman",
      title: "Lowercase letters, digits, . _ - (max 32, starting with a letter or digit)" }),
    server: h("select", {}, servers.map((x) => h("option", { value: x, text: x }))),
    device: h("select", {}, ["", "phone", "pc", "phone + pc", "other"].map((x) => h("option", { value: x, text: x || "–" }))),
    contact: h("input", { placeholder: "Discord, phone…" }),
    paid: h("input", { type: "checkbox" }),
    paid_until: h("input", { type: "date" }),
    notes: h("textarea", {}),
  };
  f.name.addEventListener("input", () => { f.name.value = f.name.value.toLowerCase(); });
  const submit = h("button", { class: "btn primary", type: "submit", text: "Create on server", disabled: !servers.length });

  async function save(e) {
    e.preventDefault();
    submit.disabled = true; submit.textContent = "Creating…"; err.textContent = "";
    try {
      const r = await api("POST", "/users/add", { name: f.name.value.trim(), server: f.server.value, device: f.device.value,
        contact: f.contact.value, paid: f.paid.checked, paid_until: f.paid_until.value, notes: f.notes.value });
      S = r.state; render(); showCreated(r.created);
    } catch (ex) { err.textContent = ex.message; submit.disabled = false; submit.textContent = "Create on server"; }
  }

  showDialog(h("form", { onsubmit: save },
    h("h2", { text: "Add user" }),
    h("p", { class: "muted", text: "Creates their VPN peer on the server right away. Unpaid users start as a trial; tick Paid later to make them a member." }),
    servers.length ? null : h("p", { class: "bad", text: "No running server loaded yet. Close this and press Refresh." }),
    h("div", { class: "row" },
      h("label", { class: "field" }, h("span", { text: "Name (also the peer name)" }), f.name),
      h("label", { class: "field" }, h("span", { text: "Server" }), f.server)),
    h("div", { class: "row" },
      h("label", { class: "field" }, h("span", { text: "Device" }), f.device),
      h("label", { class: "field" }, h("span", { text: "Contact" }), f.contact)),
    h("div", { class: "row" },
      h("label", { class: "check" }, f.paid, "Already paid"),
      h("label", { class: "field" }, h("span", { text: "Paid until" }), f.paid_until)),
    h("label", { class: "field" }, h("span", { text: "Notes" }), f.notes),
    err,
    h("div", { class: "dialog-actions" }, closeBtn(), submit)));
  f.name.focus();
}

// The config holds the user's private key: shown once, never stored, cleared on close.
function showCreated(c) {
  let conf = c.conf;
  const d = dialog();
  const wipe = () => { conf = null; d.replaceChildren(); d.removeEventListener("close", wipe); };
  d.addEventListener("close", wipe);
  const download = () => {
    const url = URL.createObjectURL(new Blob([conf], { type: "application/octet-stream" }));
    const a = h("a", { href: url, download: `${c.name}.conf` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  showDialog(h("div", { class: "body" },
    h("h2", { text: `${c.name} is ready on ${c.server}` }),
    h("p", { class: "bad", text: "Shown once. This app doesn't keep it. If it's lost, remove and add the user again." }),
    h("div", { class: "qr" }, h("img", { src: `data:image/svg+xml;base64,${c.qr_svg_b64}`, alt: `WireGuard QR code for ${c.name}` })),
    h("p", { class: "muted", text: `Phone: WireGuard app → + → Scan from QR code. PC: download the .conf and import it. Tunnel IP ${c.ip}.` }),
    h("div", { class: "dialog-actions" },
      h("button", { class: "btn", text: "Copy config", onclick: () => copy(conf) }),
      h("button", { class: "btn", text: "Download .conf", onclick: download }),
      h("button", { class: "btn primary", text: "Done", onclick: () => d.close() }))));
}

function openRemove(p) {
  const err = h("div", { class: "error" });
  const typed = h("input", { autocomplete: "off", placeholder: p.name, "aria-label": "Type the name to confirm" });
  const go = h("button", { class: "btn danger", text: p.peers.length ? "Revoke access & remove" : "Remove", disabled: true });
  typed.addEventListener("input", () => { go.disabled = typed.value !== p.name; });
  go.addEventListener("click", async () => {
    go.disabled = true; go.textContent = "Revoking…"; err.textContent = "";
    try {
      const r = await api("POST", `/people/${p.id}/remove`, { confirm: typed.value });
      S = r.state; render(); dialog().close();
      toast(r.revoked.length ? `${p.name} revoked (${r.revoked.join(", ")})` : `${p.name} removed`);
    } catch (ex) { err.textContent = ex.message; go.disabled = false; go.textContent = "Revoke access & remove"; }
  });
  showDialog(h("div", { class: "body" },
    h("h2", { text: `Remove ${p.name}?` }),
    p.peers.length
      ? h("p", { text: `Their VPN access is revoked right away on ${p.peers.map((x) => `${x.server} (${x.peer_name})`).join(", ")}, then the record is deleted. Their current config stops working.` })
      : h("p", { text: "They have no peers linked, so only the record is deleted." }),
    h("label", { class: "field" }, h("span", {}, "Type ", h("b", { text: p.name }), " to confirm"), typed),
    err,
    h("div", { class: "dialog-actions" }, closeBtn(), go)));
  typed.focus();
}

// ------------------------------------------------------------ shell

function render() {
  for (const b of document.querySelectorAll(".tab")) b.setAttribute("aria-current", b.dataset.view === view ? "page" : "false");
  const main = document.getElementById("main");
  if (!S) { main.replaceChildren(h("section", { class: "card empty", text: TOKEN ? "Loading…" : "Missing token: open the URL printed in the console." })); return; }

  const banner = document.getElementById("banner");
  const errs = [...S.errors, ...S.servers.flatMap((x) => x.errors.map((e) => `${x.name}: ${e}`))];
  banner.hidden = !errs.length;
  banner.replaceChildren(...errs.map((e) => h("div", { text: e })));

  document.getElementById("refreshed").textContent = S.refreshing ? "Refreshing…" :
    S.refreshed_at ? `Updated ${ago(S.refreshed_at)} · auto every 15 min` : "Fetching first update…";
  const content = view === "servers" ? renderServers() : view === "setup" ? renderSetup() : renderUsers();
  main.replaceChildren(...[content].flat());
}

function renderKeepFocus() {
  const pos = document.activeElement?.selectionStart;
  render();
  const input = document.querySelector('.toolbar input[type="search"]');
  if (input) { input.focus(); if (pos != null) input.setSelectionRange(pos, pos); }
}

async function load() {
  try { S = await api("GET", "/state"); } catch (e) { S = null; document.getElementById("main").replaceChildren(h("section", { class: "card bad", text: e.message })); return; }
  if (!dialog().open && document.activeElement?.type !== "search") render();
}

document.getElementById("tabs").addEventListener("click", (e) => {
  const v = e.target.closest(".tab")?.dataset.view;
  if (!v) return;
  view = v; try { localStorage.setItem("wgmon-view", v); } catch { /* storage blocked */ }
  render();
});
document.getElementById("refresh").addEventListener("click", async (e) => {
  const btn = e.currentTarget; btn.disabled = true; btn.textContent = "Refreshing…";
  await act(() => api("POST", "/refresh"), "Refreshed");
  btn.disabled = false; btn.textContent = "Refresh";
});

render();
load();
setInterval(load, 30000);   // pick up the background refresh
