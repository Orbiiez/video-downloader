/* Video Downloader — the interface. Talks to the local server (vdl_server.py) over JSON + Server-Sent Events. */
"use strict";

// ---------- connection ----------
const HASH = location.hash;  // read once: it is cleared from the address right after
const TOKEN = (() => {
  const m = HASH.match(/t=([^&]+)/);
  let t = m ? decodeURIComponent(m[1]) : "";
  try {
    if (t) sessionStorage.setItem("vdl-token", t);
    else t = sessionStorage.getItem("vdl-token") || "";
  } catch (e) { /* storage blocked: the hash is enough */ }
  if (m) history.replaceState(null, "", location.pathname);
  return t;
})();

async function api(path, body) {
  const r = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "X-Token": TOKEN, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (!r.ok) throw new Error(data.error || `request failed (${r.status})`);
  return data;
}
// The launcher passes the system theme (#...&sys=dark|light) because WebView2 can take a few seconds to report it.
const SYS_HINT = (HASH.match(/sys=(dark|light)/) || [])[1] || null;
if (SYS_HINT) document.documentElement.setAttribute("data-theme", SYS_HINT);
const tok = (url) => url + (url.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(TOKEN);

// ---------- state ----------
const S = {
  role: "local", settings: {}, jobs: new Map(), login: {}, engines: {}, phone: { on: false }, choices: {},
  update: null, sysHint: SYS_HINT, filter: "all", view: "downloads", clipOpen: false, picker: null, mode: "auto", platform: "",
};
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const LIVE = ["queued", "starting", "picking", "downloading", "processing"];
const ACTIVE = ["starting", "downloading", "processing"];

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
}
const icon = (name, cls) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  if (cls) s.setAttribute("class", cls);
  const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
  u.setAttribute("href", "#i-" + name);
  s.append(u);
  return s;
};

// ---------- formatting ----------
function fmtBytes(n) {
  if (!n && n !== 0) return "";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return i === 0 ? `${n.toFixed(0)} B` : `${n.toFixed(n >= 100 ? 0 : 1)} ${u[i]}`;
}
function fmtTime(s) {
  if (s == null || !isFinite(s)) return "";
  s = Math.round(s);
  const hh = Math.floor(s / 3600), mm = Math.floor(s / 60) % 60, ss = s % 60;
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${mm}:${String(ss).padStart(2, "0")}`;
}
function parseTime(t) {
  t = (t || "").trim();
  if (!t) return null;
  if (!/^\d+(\.\d+)?(:\d+(\.\d+)?){0,2}$/.test(t)) return NaN;
  return t.split(":").reduce((a, p) => a * 60 + parseFloat(p), 0);
}
function ago(ts) {
  const d = (Date.now() / 1000 - ts);
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  if (d < 86400 * 7) return `${Math.floor(d / 86400)} d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}
const QUALITY_LABEL = { max: "Best", 4320: "8K", 2160: "4K", 1440: "1440p", 1080: "1080p", 720: "720p", 480: "480p", 360: "360p", 240: "240p", 144: "144p" };
const CODEC_LABEL = { h264: "H.264", av1: "AV1", vp9: "VP9", any: "Any codec" };
const AUDIO_LABEL = { best: "Original", mp3: "MP3", m4a: "M4A", opus: "Opus", ogg: "OGG", wav: "WAV", flac: "FLAC" };
const TOOL_ICON = { remux: "film", h264: "video", audio: "music", gif: "gif", trim: "cut", mute: "mute" };

function statusText(j) {
  switch (j.state) {
    case "queued": return "Waiting for a free slot…";
    case "starting": return j.step || "Looking up the link…";
    case "picking": return `Choose what to download · ${j.count || (j.picker || []).length} items`;
    case "processing": return (j.step || "Finishing") + (j.fraction != null && j.fraction < 1 ? ` · ${Math.round(j.fraction * 100)}%` : "…");
    case "downloading": {
      const p = [];
      if (j.items > 1) p.push(`item ${j.item} of ${j.items}`);
      if (j.fraction != null) p.push(`${Math.round(j.fraction * 100)}%`);
      if (j.total) p.push(`${fmtBytes(j.downloaded)} of ${fmtBytes(j.total)}`);
      else if (j.downloaded) p.push(fmtBytes(j.downloaded));
      if (j.speed) p.push(`${fmtBytes(j.speed)}/s`);
      if (j.eta) p.push(`${fmtTime(j.eta)} left`);
      return p.join("  ·  ") || "Downloading…";
    }
    case "done": {
      if (j.spawned) return `Added ${j.spawned} video${j.spawned === 1 ? "" : "s"} to the queue`;
      const n = j.files.length;
      const names = n === 1 ? j.files[0].name : `${n} files`;
      return [fmtBytes(j.size), names].filter(Boolean).join("  ·  ");
    }
    case "failed": return j.error || "Failed";
    case "cancelled": return "Cancelled";
  }
  return "";
}

// ---------- queue rendering ----------
const queueEl = () => $("#queue");
const rows = new Map();

function jobTags(j) {
  const t = [];
  if (j.kind === "tool") t.push(h("span", { class: "tag accent" }, (S.choices.tools || {})[j.tool] || "Tool"));
  else {
    t.push(h("span", { class: "tag site" }, (j.site || "link").replace(/^www\./, "")));
    if (j.mode === "audio") t.push(h("span", { class: "tag" }, "Audio"));
    if (j.mode === "mute") t.push(h("span", { class: "tag" }, "Muted"));
    if (j.clip) t.push(h("span", { class: "tag accent" }, `Clip ${fmtTime(j.clip[0])}–${j.clip[1] != null ? fmtTime(j.clip[1]) : "end"}`));
    if (j.media === "images" || j.media === "image") t.push(h("span", { class: "tag" }, j.count > 1 ? `${j.count} items` : "Picture"));
    if (j.media === "playlist") t.push(h("span", { class: "tag" }, `Playlist · ${j.count || "?"}`));
  }
  if (j.state === "done" && j.files.length) {
    const exts = [...new Set(j.files.map((f) => f.ext))].slice(0, 3);
    exts.forEach((e) => t.push(h("span", { class: "tag" }, e)));
  }
  return t;
}

function actionsFor(j) {
  const a = [];
  const btn = (name, title, fn, cls = "icon-btn") => h("button", { class: cls, title, "aria-label": title, onclick: (e) => { e.stopPropagation(); fn(e); } }, icon(name));
  if (j.state === "picking") {
    a.push(h("button", { class: "primary", onclick: () => openPicker(j.id) }, "Choose…"));
    a.push(btn("x", "Cancel", () => jobAction(j, "cancel")));
  } else if (LIVE.includes(j.state)) {
    a.push(btn("x", "Cancel", () => jobAction(j, "cancel")));
  } else if (j.state === "done") {
    if (S.role === "remote") {
      j.files.forEach((f, n) => a.push(h("a", { class: "btn accent", href: tok(`/api/file?id=${j.id}&n=${n}`), download: f.name }, icon("save"), j.files.length > 1 ? `Save ${n + 1}` : "Save")));
    } else if (j.files.length) {
      a.push(btn("play", "Open", () => jobAction(j, "open"), "icon-btn primary-icon"));
      a.push(btn("folder", "Show in folder", () => jobAction(j, "show")));
    }
    a.push(btn("more", "More", (e) => jobMenu(j, e.currentTarget)));
  } else {
    a.push(btn("retry", "Try again", () => jobAction(j, "retry"), "icon-btn primary-icon"));
    a.push(btn("more", "More", (e) => jobMenu(j, e.currentTarget)));
  }
  return a;
}

function buildRow(j) {
  const li = h("li", { class: "job", "data-id": j.id });
  const thumb = h("div", { class: "thumb" });
  const body = h("div", { class: "job-body" },
    h("div", { class: "job-title" }), h("div", { class: "job-meta" }), h("div", { class: "job-status" }));
  const actions = h("div", { class: "job-actions" });
  const bar = h("div", { class: "bar" }, h("i"));
  li.append(thumb, body, actions, bar);
  thumb.addEventListener("click", () => { const jj = S.jobs.get(j.id); if (jj && jj.state === "done" && S.role === "local" && jj.files.length) jobAction(jj, "open"); });
  li.addEventListener("dblclick", () => { const jj = S.jobs.get(j.id); if (jj && jj.state === "done" && S.role === "local" && jj.files.length) jobAction(jj, "open"); });
  li.addEventListener("contextmenu", (e) => { e.preventDefault(); jobMenu(S.jobs.get(j.id), { getBoundingClientRect: () => ({ left: e.clientX, right: e.clientX, top: e.clientY, bottom: e.clientY }) }); });
  const row = { li, thumb, title: $(".job-title", body), meta: $(".job-meta", body), status: $(".job-status", body), actions, bar, last: {} };
  rows.set(j.id, row);
  return row;
}

function renderJob(j) {
  let row = rows.get(j.id);
  const isNew = !row;
  if (!row) row = buildRow(j);
  const L = row.last;
  const prevState = L.state;
  if (L.state !== j.state) {
    row.li.className = `job state-${j.state}` + (j.parent ? " child" : "");
    if (prevState && prevState !== "done" && j.state === "done") {
      row.li.classList.add("just-done");
      setTimeout(() => row.li.classList.remove("just-done"), 700);
    }
  }
  // thumbnail
  const thumbKey = `${j.thumb}|${j.media}|${j.state === "done"}|${j.duration}`;
  if (L.thumbKey !== thumbKey) {
    row.thumb.textContent = "";
    const kindIcon = j.kind === "tool" ? TOOL_ICON[j.tool] || "wand" : j.media === "audio" ? "music" : (j.media === "images" || j.media === "image") ? "image" : j.media === "playlist" ? "list" : "video";
    row.thumb.append(icon(kindIcon));
    if (j.thumb) {
      const img = h("img", { alt: "", loading: "lazy", decoding: "async", src: tok(`/api/thumb?id=${j.id}`) });
      img.onload = () => img.classList.add("loaded");
      img.onerror = () => img.remove();
      row.thumb.append(img);
    }
    if (j.duration) row.thumb.append(h("span", { class: "dur" }, fmtTime(j.duration)));
    if (j.media === "audio" || j.media === "images" || j.media === "playlist") row.thumb.append(h("span", { class: "kind" }, icon(kindIcon)));
    if (j.state === "done" && S.role === "local") row.thumb.append(h("span", { class: "done-mark" }, icon("play")));
    L.thumbKey = thumbKey;
  }
  const title = j.title || j.url || "Download";
  if (L.title !== title) { row.title.textContent = title; row.title.title = title; L.title = title; }
  const tagsKey = JSON.stringify([j.site, j.mode, j.clip, j.media, j.count, j.state === "done" && j.files.map((f) => f.ext)]);
  if (L.tags !== tagsKey) { row.meta.replaceChildren(...jobTags(j)); L.tags = tagsKey; }
  const st = statusText(j);
  if (L.status !== st) { row.status.textContent = st; row.status.title = st; L.status = st; }
  const actKey = `${j.state}|${j.files.length}|${S.role}`;
  if (L.act !== actKey) { row.actions.replaceChildren(...actionsFor(j)); L.act = actKey; }
  // progress bar: transform only, so it stays smooth
  const indet = (ACTIVE.includes(j.state) && j.fraction == null) || j.state === "starting";
  if (L.indet !== indet) { row.bar.classList.toggle("indet", indet); L.indet = indet; }
  const frac = j.state === "done" || j.state === "failed" ? 1 : (j.fraction || 0);
  if (!indet && L.frac !== frac) { row.bar.firstChild.style.transform = `scaleX(${frac})`; L.frac = frac; }
  if (indet) { row.bar.firstChild.style.transform = ""; L.frac = null; }
  L.state = j.state;
  if (isNew) queueEl().append(row.li);
  applyFilter(row, j);
}

function applyFilter(row, j) {
  const f = S.filter;
  const show = f === "all" || (f === "active" && LIVE.includes(j.state)) || (f === "done" && j.state === "done") ||
    (f === "failed" && (j.state === "failed" || j.state === "cancelled"));
  row.li.hidden = !show;
}

function removeRow(id) {
  const row = rows.get(id);
  if (!row) return;
  rows.delete(id);
  row.li.classList.add("leaving");
  setTimeout(() => row.li.remove(), 260);
}

function refreshQueueChrome() {
  const jobs = [...S.jobs.values()];
  const active = jobs.filter((j) => LIVE.includes(j.state)).length;
  $("#queueHead").hidden = jobs.length === 0;
  $("#empty").hidden = jobs.length !== 0;
  $("#queueCount").textContent = jobs.length ? `· ${jobs.length}` : "";
  const badge = $("#activeBadge");
  badge.hidden = !active;
  badge.textContent = active;
  document.title = active ? `(${active}) Video Downloader` : "Video Downloader";
  $("#cancelAllBtn").hidden = !active;
  $("#clearBtn").hidden = !jobs.some((j) => ["done", "failed", "cancelled"].includes(j.state));
}

async function jobAction(j, action, extra = {}) {
  try {
    const r = await api("/api/job", { id: j.id, action, ...extra });
    if (action === "copy_file") toast("File copied — paste it anywhere");
    if (action === "copy_link") toast("Link copied");
    return r;
  } catch (e) { toast(e.message, "err"); }
}

function jobMenu(j, anchor) {
  if (!j) return;
  const items = [];
  if (j.state === "done" && j.files.length && S.role === "local") {
    items.push(["copy", "Copy file", () => jobAction(j, "copy_file")]);
    if (j.files.length > 1) j.files.forEach((f, n) => items.push(["play", `Open ${f.name}`, () => jobAction(j, "open", { n })]));
  }
  if (j.url) items.push(["link", "Copy link", () => S.role === "local" ? jobAction(j, "copy_link") : navigator.clipboard?.writeText(j.url)]);
  if (j.state === "done" && j.url) items.push(["retry", "Download again", () => api("/api/add", { urls: [j.url], overrides: { mode: j.mode, clip: j.clip } }).catch((e) => toast(e.message, "err"))]);
  items.push(["info", "Details", () => showDetails(j)]);
  items.push("sep");
  items.push(["trash", LIVE.includes(j.state) ? "Cancel and remove" : "Remove from list", () => jobAction(j, "remove")]);
  menu(anchor, items);
}

async function showDetails(j) {
  const r = await jobAction(j, "log");
  if (!r) return;
  const row = rows.get(j.id);
  if (!row) return;
  $(".details", row.li)?.remove();
  const pre = h("pre", { class: "details" }, [j.url, r.error ? `Error: ${r.error}` : null, "", ...r.log].filter((x) => x != null).join("\n"));
  row.li.append(pre);
  pre.scrollTop = pre.scrollHeight;
}

// ---------- events from the server ----------
function onState(st) {
  S.role = st.role;
  document.body.classList.toggle("remote", st.role === "remote");
  $$("[data-local]").forEach((el) => { el.hidden = st.role === "remote"; });
  $("#ver").textContent = "v" + st.version;
  Object.assign(S, {
    settings: st.settings || S.settings, login: st.login || S.login, engines: st.engines || S.engines,
    phone: st.phone || S.phone, choices: st.choices || S.choices, update: st.update, platform: st.platform || "",
  });
  if (S.role === "local") S.mode = S.settings.mode || "auto";
  const seen = new Set();
  for (const j of st.jobs) { S.jobs.set(j.id, j); seen.add(j.id); renderJob(j); }
  for (const id of [...S.jobs.keys()]) if (!seen.has(id)) { S.jobs.delete(id); removeRow(id); }
  applySettings();
  showUpdate();
  refreshQueueChrome();
  requestAnimationFrame(() => requestAnimationFrame(() => document.documentElement.classList.remove("preload")));
  if (S.role === "local" && !S.toolsBuilt) buildTools();
  if (S.role === "local" && S.settings && !S.settings.onboarded) onboard();
}

function onJobs(ev) {
  for (const j of ev.jobs) {
    const prev = S.jobs.get(j.id);
    S.jobs.set(j.id, j);
    renderJob(j);
    if (j.state === "picking" && (!prev || prev.state !== "picking") && !S.picker) openPicker(j.id);
    if (S.picker === j.id && j.state !== "picking") closeSheet();
    if (prev && prev.state !== "done" && j.state === "done" && S.view === "history") loadHistory();
  }
  for (const id of ev.removed || []) { S.jobs.delete(id); removeRow(id); }
  refreshQueueChrome();
}

function connect() {
  let offlineTimer = null;
  const es = new EventSource(tok("/api/events"));
  es.onopen = () => { clearTimeout(offlineTimer); $("#offline").hidden = true; };
  es.onerror = () => {
    clearTimeout(offlineTimer);
    offlineTimer = setTimeout(() => { $("#offline").hidden = false; }, 1500);
  };
  es.onmessage = (m) => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    switch (ev.type) {
      case "state": onState(ev); break;
      case "jobs": onJobs(ev); break;
      case "settings": S.settings = ev.settings; applySettings(); if ($("#drawer").classList.contains("show") && S.drawer === "settings") renderSettings(); break;
      case "update": S.update = ev.update; showUpdate(); break;
      case "systheme":
        if (S.sysHint) {
          document.documentElement.classList.add("preload");
          S.sysHint = ev.theme;
          applySettings();
          setTimeout(() => document.documentElement.classList.remove("preload"), 120);
        }
        break;
      case "clipboard": offerClipboard(ev.urls); break;
      case "files": filesDropped(ev.paths); break;
      case "toast": toast(ev.message, ev.kind); break;
      case "attention": if (ev.reason === "pick" && !S.picker) openPicker(ev.id); break;
    }
  };
}

// ---------- settings plumbing ----------
let saveTimers = {};
function setSetting(key, value, debounce = 0) {
  S.settings[key] = value;
  if (key === "mode") S.mode = value;
  applySettings();
  if (S.role !== "local") return;
  clearTimeout(saveTimers[key]);
  saveTimers[key] = setTimeout(() => {
    api("/api/settings", { patch: { [key]: value } }).then((r) => { S.settings = r.settings; applySettings(); })
      .catch((e) => toast(e.message, "err"));
  }, debounce);
}

function applySettings() {
  const s = S.settings || {};
  const theme = s.theme || "system";
  if (theme === "system" && S.sysHint) document.documentElement.setAttribute("data-theme", S.sysHint);
  else if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  // mode switch
  const mode = S.role === "local" ? s.mode || "auto" : S.mode;
  $$("#modeSeg button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === mode)));
  placeGlider($("#modeSeg"));
  // format chip
  let fmt;
  if (mode === "audio") fmt = AUDIO_LABEL[s.audio_format] + (["mp3", "m4a", "opus", "ogg"].includes(s.audio_format) ? ` · ${s.audio_bitrate}k` : "");
  else fmt = `${QUALITY_LABEL[s.quality] || "1080p"} · ${CODEC_LABEL[s.codec] || "H.264"}`;
  $("#formatChip span").textContent = S.role === "local" ? fmt : (mode === "audio" ? "Audio" : "Video");
  $("#formatChip").hidden = S.role !== "local";
  // folder chip
  const out = s.out_dir || "";
  const home = out.match(/^([A-Za-z]:\\Users\\[^\\]+|\/Users\/[^/]+|\/home\/[^/]+)/);
  $("#folderChip span").textContent = home ? "~" + out.slice(home[1].length) : out || "Downloads";
  $("#folderChip").title = out;
  // login chip
  const lg = s.login || "none";
  $("#loginChip span").textContent = lg === "none" ? "No login" : lg === "saved" ? `Saved login${S.login.days_left ? ` · ${S.login.days_left}d` : ""}` : `From ${cap(lg)}`;
  $("#loginChip").classList.toggle("on", lg !== "none");
  $("#clipChip").classList.toggle("on", S.clipOpen);
}
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function placeGlider(seg) {
  if (!seg) return;
  const on = $("[aria-checked=true]", seg), g = $(".seg-glider", seg);
  if (!on || !g) return;
  g.style.width = on.offsetWidth + "px";
  g.style.transform = `translateX(${on.offsetLeft}px)`;
}

function showUpdate() {
  const b = $("#updateBtn");
  if (S.update && S.role === "local") {
    b.hidden = false;
    $("span", b).textContent = `Update to ${S.update.tag}`;
    b.onclick = () => api("/api/open", { path: S.update.url }).catch((e) => toast(e.message, "err"));
  } else b.hidden = true;
}

// ---------- composer ----------
const links = () => $("#links");
function autoGrow() {
  const t = links();
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 140) + "px";
}
function clipOverride() {
  if (!S.clipOpen) return null;
  const a = parseTime($("#clipStart").value), b = parseTime($("#clipEnd").value);
  $("#clipStart").classList.toggle("bad", Number.isNaN(a));
  $("#clipEnd").classList.toggle("bad", Number.isNaN(b) || (b != null && a != null && b <= a));
  if (Number.isNaN(a) || Number.isNaN(b)) throw new Error("Clip times look like 1:30 or 90");
  if (b != null && b <= (a || 0)) throw new Error("The clip has to end after it starts");
  if (a == null && b == null) return null;
  return [a || 0, b];
}
async function submit(text) {
  text = (text ?? links().value).trim();
  if (!text) { links().focus(); return; }
  let clip;
  try { clip = clipOverride(); } catch (e) { toast(e.message, "err"); return; }
  const overrides = { mode: S.role === "local" ? S.settings.mode : S.mode };
  if (clip) overrides.clip = clip;
  try {
    const r = await api("/api/add", { text, overrides });
    links().value = "";
    autoGrow();
    if (S.view !== "downloads") showView("downloads");
    const msg = r.added ? `Added ${r.added} link${r.added === 1 ? "" : "s"}` : "Already in the queue";
    toast(r.skipped && r.added ? `${msg} · ${r.skipped} already in the queue` : msg);
  } catch (e) { toast(e.message, "err"); }
}
async function pasteAndGo() {
  let text = "";
  try {
    text = S.role === "local" ? (await api("/api/clipboard")).text : await navigator.clipboard.readText();
  } catch (e) { /* clipboard unavailable */ }
  if (!text || !text.trim()) { toast("The clipboard is empty — copy a link first"); links().focus(); return; }
  submit(text);
}

function wireComposer() {
  const t = links(), comp = $("#composer");
  t.addEventListener("input", autoGrow);
  t.addEventListener("focus", () => comp.classList.add("focus"));
  t.addEventListener("blur", () => comp.classList.remove("focus"));
  t.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  t.addEventListener("paste", (e) => {
    const text = e.clipboardData?.getData("text") || "";
    if (!t.value.trim() && /https?:\/\/|www\./i.test(text)) { e.preventDefault(); submit(text); }
  });
  document.addEventListener("paste", (e) => {
    if (e.target.closest("input, textarea, select") || $("#sheetWrap").classList.contains("show")) return;
    const text = e.clipboardData?.getData("text") || "";
    if (text.trim()) { e.preventDefault(); submit(text); }
  });
  $("#goBtn").onclick = () => submit();
  $("#pasteBtn").onclick = pasteAndGo;
  $$("#modeSeg button").forEach((b) => b.onclick = () => setSetting("mode", b.dataset.mode));
  $("#clipChip").onclick = () => {
    S.clipOpen = !S.clipOpen;
    $("#clipRow").hidden = !S.clipOpen;
    applySettings();
    if (S.clipOpen) $("#clipStart").focus();
  };
  $("#clipClear").onclick = () => { $("#clipStart").value = $("#clipEnd").value = ""; S.clipOpen = false; $("#clipRow").hidden = true; applySettings(); };
  ["#clipStart", "#clipEnd"].forEach((s) => $(s).addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); }));
  $("#formatChip").onclick = (e) => formatMenu(e.currentTarget);
  $("#folderChip").onclick = (e) => menu(e.currentTarget, [
    ["folder", "Change folder…", chooseFolder],
    ["ext", "Open folder", () => api("/api/open", { folder: true }).catch((er) => toast(er.message, "err"))],
  ]);
  $("#loginChip").onclick = (e) => loginMenu(e.currentTarget);
  $$("#filters button").forEach((b) => b.onclick = () => {
    S.filter = b.dataset.filter;
    $$("#filters button").forEach((x) => x.classList.toggle("on", x === b));
    for (const [id, row] of rows) applyFilter(row, S.jobs.get(id));
  });
  $("#clearBtn").onclick = () => api("/api/jobs", { action: "clear_finished" });
  $("#cancelAllBtn").onclick = () => api("/api/jobs", { action: "cancel_all" });
  window.addEventListener("resize", () => placeGlider($("#modeSeg")));
}

async function chooseFolder() {
  try {
    const r = await api("/api/dialog", { kind: "folder" });
    if (r.paths && r.paths[0]) { setSetting("out_dir", r.paths[0]); toast("Saving to " + r.paths[0]); }
  } catch (e) { toast(e.message, "err"); }
}

// ---------- popovers ----------
let popAnchor = null;
function openPopover(anchor, content) {
  const p = $("#popover");
  p.replaceChildren(...content);
  p.classList.add("show");
  popAnchor = anchor;
  const r = anchor.getBoundingClientRect();
  const pw = p.offsetWidth, ph = p.offsetHeight;
  let x = Math.min(r.left, innerWidth - pw - 12), y = r.bottom + 6;
  if (y + ph > innerHeight - 12) y = Math.max(12, r.top - ph - 6);
  p.style.left = Math.max(12, x) + "px";
  p.style.top = y + "px";
}
function closePopover() { $("#popover").classList.remove("show"); popAnchor = null; }
function menu(anchor, items) {
  openPopover(anchor, items.map((it) => it === "sep" ? h("div", { class: "sep" }) :
    typeof it === "string" ? h("div", { class: "phint" }, it) :
    h("button", { class: "item" + (it[3] ? " on" : ""), onclick: () => { closePopover(); it[2](); } }, icon(it[0]), it[1], it[4] ? h("small", {}, it[4]) : null)));
}
function optGroup(label, key, options, after) {
  return h("div", { class: "group" }, h("div", { class: "glabel" }, label),
    h("div", { class: "opts" }, options.map(([v, l]) => h("button", {
      class: "opt" + (String(S.settings[key]) === String(v) ? " on" : ""),
      onclick: (e) => { setSetting(key, v); $$(".opt", e.target.parentNode).forEach((o) => o.classList.toggle("on", o === e.target)); after && after(); },
    }, l))));
}
function formatMenu(anchor) {
  const mode = S.settings.mode;
  const groups = [];
  if (mode === "audio") {
    groups.push(optGroup("Audio format", "audio_format", Object.entries(AUDIO_LABEL).filter(([k]) => k !== "best").concat([["best", "Original"]])));
    groups.push(optGroup("Bitrate", "audio_bitrate", ["320", "256", "192", "128"].map((b) => [b, b + "k"])));
  } else {
    groups.push(optGroup("Quality", "quality", ["max", "2160", "1440", "1080", "720", "480", "360"].map((q) => [q, QUALITY_LABEL[q]])));
    groups.push(optGroup("Codec", "codec", [["h264", "H.264"], ["av1", "AV1"], ["vp9", "VP9"], ["any", "Any"]]));
    groups.push(h("div", { class: "phint" }, S.settings.codec === "h264" ? "H.264 plays everywhere. On YouTube it tops out at 1080p." : "AV1/VP9: higher quality and smaller files; some older players and editors can't open them."));
  }
  groups.push(h("div", { class: "group" }, h("button", { class: "item", onclick: () => { closePopover(); openSettings(); } }, icon("gear"), "More options…")));
  openPopover(anchor, groups);
}
function loginMenu(anchor) {
  const lg = S.settings.login;
  const items = [["x", "No login", () => setSetting("login", "none"), lg === "none"]];
  if (S.login.saved) items.push(["lock", "Saved login", () => setSetting("login", "saved"), lg === "saved", `${S.login.sites.length} site${S.login.sites.length === 1 ? "" : "s"} · ${S.login.days_left}d`]);
  if (S.platform === "win32" || !S.platform) items.push(["paste", S.login.saved ? "Import a new cookies.txt…" : "Import cookies.txt…", importLogin]);
  items.push("sep", "Or use the login from your browser:");
  for (const b of S.choices.browsers || []) items.push(["ext", `Read from ${cap(b)}`, () => {
    setSetting("login", b);
    if (b !== "firefox") toast("Chrome-based browsers often block this while open; a saved login is more reliable.");
  }, lg === b]);
  if (S.login.saved) items.push("sep", ["trash", "Forget saved login", forgetLogin]);
  menu(anchor, items);
}

// ---------- login import ----------
async function importLogin() {
  const go = await confirmSheet("Import a login", [
    h("p", {}, "Choose a cookies.txt exported from your browser while logged in to the site (for example with the “Get cookies.txt LOCALLY” extension)."),
    h("div", { class: "note" }, "Anyone with that file can log in as you. The app keeps only the sites you tick, stores them encrypted so only your Windows account can read them, forgets them after 30 days, and offers to delete the original."),
  ], "Choose file…");
  if (!go) return;
  let path;
  try { path = (await api("/api/dialog", { kind: "cookies" })).paths?.[0]; } catch (e) { toast(e.message, "err"); return; }
  if (!path) return;
  let info;
  try { info = await api("/api/login", { action: "inspect", path }); } catch (e) { toast("Couldn't read that file: " + e.message, "err"); return; }
  const checks = info.sites.map((s) => h("label", {}, h("input", { type: "checkbox", value: s, checked: info.preselected.includes(s) }), s));
  const wipe = h("input", { type: "checkbox", checked: true });
  const ok = await confirmSheet(`This file has logins for ${info.sites.length} site${info.sites.length === 1 ? "" : "s"}`, [
    h("p", { class: "muted" }, "Tick only the sites you download from. The rest are thrown away."),
    h("div", { class: "checklist" }, checks),
    h("label", { class: "set-row", style: "margin-top:12px" }, wipe, "Delete the original file afterwards (recommended)"),
  ], "Keep selected");
  if (!ok) return;
  const sites = checks.map((l) => $("input", l)).filter((c) => c.checked).map((c) => c.value);
  if (!sites.length) { toast("Pick at least one site", "err"); return; }
  try {
    const r = await api("/api/login", { action: "save", path, sites, wipe: wipe.checked });
    S.login = r.login;
    applySettings();
    toast(`Saved an encrypted login for ${r.saved.length} site${r.saved.length === 1 ? "" : "s"}` + (wipe.checked && !r.wiped ? " — couldn't delete the original, please delete it yourself" : ""));
  } catch (e) { toast(e.message, "err"); }
}
async function forgetLogin() {
  if (!await confirmSheet("Forget the saved login?", [h("p", {}, "Login-only videos will need a fresh import.")], "Forget")) return;
  try { S.login = (await api("/api/login", { action: "forget" })).login; applySettings(); toast("Saved login deleted"); } catch (e) { toast(e.message, "err"); }
}

// ---------- sheets ----------
let sheetResolve = null;
function openSheet(nodes, cls = "") {
  const sh = $("#sheet");
  sh.className = "sheet " + cls;
  sh.replaceChildren(...nodes);
  $("#sheetWrap").classList.add("show");
  $("#sheetWrap").setAttribute("aria-hidden", "false");
  setTimeout(() => ($(".sheet-foot .btn.accent", sh) || sh).focus?.(), 60);
}
function closeSheet(result = false) {
  $("#sheetWrap").classList.remove("show");
  $("#sheetWrap").setAttribute("aria-hidden", "true");
  S.picker = null;
  if (sheetResolve) { const r = sheetResolve; sheetResolve = null; r(result); }
}
function confirmSheet(title, body, okLabel = "OK") {
  return new Promise((resolve) => {
    if (sheetResolve) sheetResolve(false);
    sheetResolve = resolve;
    openSheet([
      h("div", { class: "sheet-head" }, h("div", {}, h("h2", {}, title)), h("button", { class: "icon-btn", onclick: () => closeSheet(false) }, icon("x"))),
      h("div", { class: "sheet-body" }, body),
      h("div", { class: "sheet-foot" }, h("span", { class: "spacer" }),
        h("button", { class: "btn", onclick: () => closeSheet(false) }, "Cancel"),
        h("button", { class: "btn accent", onclick: () => closeSheet(true) }, okLabel)),
    ], "narrow");
  });
}

// ---------- picker ----------
function openPicker(id) {
  const j = S.jobs.get(id);
  if (!j || j.state !== "picking" || !j.picker) return;
  S.picker = id;
  const items = j.picker;
  const sel = new Set(items.map((_, i) => i));
  const isList = j.media === "playlist" || items.length > 40;
  const wide = items.some((it) => it.kind !== "image");
  const count = h("span", { class: "muted" });
  const goBtn = h("button", { class: "btn accent" });
  const update = () => {
    count.textContent = `${sel.size} of ${items.length} selected`;
    goBtn.textContent = sel.size ? `Download ${sel.size}` : "Select something";
    goBtn.disabled = !sel.size;
    allBtn.textContent = sel.size === items.length ? "Select none" : "Select all";
  };
  const allBtn = h("button", { class: "ghost small", onclick: () => {
    const all = sel.size !== items.length;
    sel.clear();
    if (all) items.forEach((_, i) => sel.add(i));
    $$(".tile", grid).forEach((t, i) => t.classList.toggle("on", sel.has(i)));
    update();
  } });
  const grid = h("div", { class: "pick-grid" + (isList ? " list" : "") + (wide ? " wide" : "") });
  let lastClick = null;
  items.forEach((it, i) => {
    const th = h("div", { class: "tthumb" }, icon(it.kind === "image" ? "image" : it.kind === "audio" ? "music" : "video"));
    if (it.thumb) th.append(h("img", { alt: "", loading: "lazy", decoding: "async", src: tok(`/api/thumb?id=${id}&i=${i}`), onerror: (e) => e.target.remove() }));
    if (it.duration) th.append(h("span", { class: "dur" }, fmtTime(it.duration)));
    const tile = h("button", { class: "tile on", title: it.title || "" }, th,
      h("div", { class: "tlabel" }, isList ? `${i + 1}. ${it.title || "Item " + (i + 1)}` : it.title || `Item ${i + 1}`),
      h("span", { class: "tcheck" }, icon("check")));
    tile.onclick = (e) => {
      const range = e.shiftKey && lastClick != null ? [Math.min(lastClick, i), Math.max(lastClick, i)] : [i, i];
      const turnOn = !sel.has(i);
      for (let k = range[0]; k <= range[1]; k++) { turnOn ? sel.add(k) : sel.delete(k); grid.children[k].classList.toggle("on", turnOn); }
      lastClick = i;
      update();
    };
    grid.append(tile);
  });
  goBtn.onclick = async () => {
    const indices = [...sel].sort((a, b) => a - b);
    closeSheet();
    await jobAction(j, "pick", { indices });
  };
  openSheet([
    h("div", { class: "sheet-head" }, h("div", {}, h("h2", {}, "Choose what to download"), h("p", {}, j.title || j.url)),
      h("button", { class: "icon-btn", title: "Close", onclick: () => closeSheet() }, icon("x"))),
    h("div", { class: "sheet-body" }, grid),
    h("div", { class: "sheet-foot" }, allBtn, count, h("span", { class: "spacer" }),
      h("button", { class: "btn", onclick: () => { closeSheet(); jobAction(j, "cancel"); } }, "Cancel download"), goBtn),
  ]);
  update();
}

// ---------- settings drawer ----------
function openDrawer(kind, render) {
  S.drawer = kind;
  render();
  $("#drawer").classList.add("show");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("show");
}
function closeDrawer() {
  $("#drawer").classList.remove("show");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("show");
  S.drawer = null;
}
const openSettings = () => openDrawer("settings", renderSettings);

function schema() {
  const langs = S.choices.languages || [["", "Off"]];
  const q = ["max", "4320", "2160", "1440", "1080", "720", "480", "360", "240", "144"];
  return [
    ["Video", [
      { k: "quality", t: "select", l: "Highest quality", o: q.map((v) => [v, v === "max" ? "Best available" : v === "4320" ? "8K (4320p)" : v === "2160" ? "4K (2160p)" : v + "p"]), help: "Videos are never downloaded above this." },
      { k: "codec", t: "seg", l: "Codec", o: [["h264", "H.264"], ["av1", "AV1"], ["vp9", "VP9"], ["any", "Best"]], help: "H.264 plays everywhere — phones, TVs, editors — but on YouTube stops at 1080p. AV1 and VP9 reach 4K and 8K with smaller files." },
      { k: "container", t: "seg", l: "File type", o: [["auto", "Auto"], ["mp4", "MP4"], ["mkv", "MKV"], ["webm", "WebM"]] },
      { k: "subtitles", t: "select", l: "Subtitles", o: langs },
      { k: "auto_subtitles", t: "switch", l: "Use auto-generated captions", help: "When there are no real subtitles in that language.", show: (s) => s.subtitles },
      { k: "subtitle_file", t: "switch", l: "Save subtitles as a separate .srt file", show: (s) => s.subtitles },
    ]],
    ["Audio", [
      { k: "audio_format", t: "seg", l: "Format", o: [["mp3", "MP3"], ["m4a", "M4A"], ["opus", "Opus"], ["ogg", "OGG"], ["wav", "WAV"], ["flac", "FLAC"], ["best", "Original"]], help: "“Original” keeps the site’s own audio exactly, with no conversion." },
      { k: "audio_bitrate", t: "select", l: "Bitrate", o: ["320", "256", "192", "128", "96", "64"].map((b) => [b, b + " kbps"]), show: (s) => ["mp3", "m4a", "opus", "ogg"].includes(s.audio_format) },
      { k: "cover_art", t: "switch", l: "Add cover art", help: "Puts the thumbnail in as the album picture." },
      { k: "dub_language", t: "select", l: "Audio language", o: [["", "Original"], ...langs.slice(1)], help: "For YouTube videos that have dubbed audio tracks." },
    ]],
    ["Posts & playlists", [
      { k: "multi", t: "seg", l: "When a link has several items", o: [["ask", "Let me choose"], ["all", "Get all"], ["first", "Only the first"]] },
      { k: "images", t: "switch", l: "Save pictures from posts", help: "Photo posts on Instagram, X, Bluesky, Reddit, Tumblr and more." },
      { k: "twitter_gif", t: "switch", l: "Save X/Twitter GIFs as real .gif files" },
      { k: "tiktok_full_audio", t: "switch", l: "TikTok: full original sound", help: "In Audio mode, get the whole sound instead of only the part the clip uses." },
      { k: "sponsorblock", t: "switch", l: "Cut out YouTube sponsor segments", help: "Uses SponsorBlock’s community-made segment list." },
    ]],
    ["Files", [
      { k: "out_dir", t: "folder", l: "Save to" },
      { k: "filename", t: "select", l: "File names", o: Object.entries(S.choices.filename_styles || {}) },
      { k: "filename_template", t: "text", l: "Pattern", stack: true, help: "Use %(title)s, %(uploader)s, %(site)s, %(resolution)s, %(codec)s, %(id)s, %(upload_date)s", show: (s) => s.filename === "custom", debounce: 600 },
      { k: "metadata", t: "switch", l: "Write title, artist and chapters into files" },
      { k: "exact_cuts", t: "switch", l: "Exact clip cuts", help: "Starts exactly where you asked. Off is faster but may start a few seconds early." },
    ]],
    ["Downloading", [
      { k: "parallel", t: "select", l: "Downloads at the same time", o: [1, 2, 3, 4, 5, 6, 8, 10].map((n) => [n, String(n)]), num: true },
      { k: "rate_limit", t: "text", l: "Speed limit", ph: "e.g. 5M — empty = no limit", debounce: 700 },
      { k: "proxy", t: "proxy", l: "Proxy", ph: "socks5://127.0.0.1:9050", help: "Send downloads through another server, so sites see its address instead of yours." },
    ]],
    ["Login", [{ t: "login" }]],
    ["Convenience", [
      { k: "clipboard_watch", t: "switch", l: "Offer links I copy", help: "Copy a video link anywhere and a one-click Download appears here.", hide: () => S.platform !== "win32" },
      { k: "keep_history", t: "switch", l: "Keep a download history" },
      { k: "theme", t: "seg", l: "Theme", o: [["system", "System"], ["dark", "Dark"], ["light", "Light"]] },
    ]],
    ["About", [{ t: "about" }]],
  ];
}

function control(it) {
  const s = S.settings;
  const v = s[it.k];
  if (it.t === "switch") {
    return h("button", { class: "switch", role: "switch", "aria-checked": String(!!v), "aria-label": it.l, onclick: (e) => {
      const nv = e.currentTarget.getAttribute("aria-checked") !== "true";
      e.currentTarget.setAttribute("aria-checked", String(nv));
      setSetting(it.k, nv);
      setTimeout(renderSettingsSoft, 30);
    } });
  }
  if (it.t === "select") {
    const sel = h("select", { "aria-label": it.l, onchange: (e) => { setSetting(it.k, it.num ? Number(e.target.value) : e.target.value); setTimeout(renderSettingsSoft, 30); } },
      it.o.map(([val, label]) => h("option", { value: val, selected: String(val) === String(v) }, label)));
    return sel;
  }
  if (it.t === "seg") {
    const seg = h("div", { class: "seg", role: "radiogroup", "aria-label": it.l }, h("i", { class: "seg-glider" }),
      it.o.map(([val, label]) => h("button", { role: "radio", "aria-checked": String(String(val) === String(v)), onclick: (e) => {
        $$("button", seg).forEach((b) => b.setAttribute("aria-checked", String(b === e.currentTarget)));
        placeGlider(seg);
        setSetting(it.k, val);
        setTimeout(renderSettingsSoft, 30);
      } }, label)));
    requestAnimationFrame(() => placeGlider(seg));
    return seg;
  }
  if (it.t === "text") {
    return h("input", { type: "text", value: v || "", placeholder: it.ph || "", spellcheck: "false", oninput: (e) => setSetting(it.k, e.target.value, it.debounce || 500) });
  }
  if (it.t === "folder") {
    return h("div", { class: "set-row" }, h("span", { class: "url-box" }, v), h("button", { class: "btn", onclick: chooseFolder }, icon("folder"), "Change"));
  }
  if (it.t === "proxy") {
    const out = h("div", { class: "help" });
    const input = h("input", { type: "text", value: v || "", placeholder: it.ph, spellcheck: "false", oninput: (e) => setSetting(it.k, e.target.value.trim(), 600) });
    const test = h("button", { class: "btn", onclick: async () => {
      out.replaceChildren(h("span", { class: "muted" }, "Testing…"));
      try {
        const r = await api("/api/proxy_test", { proxy: input.value.trim() });
        out.replaceChildren(h("div", { class: "note ok" }, `Sites see you as ${r.ip}${r.city ? ` in ${r.city}, ${r.country}` : r.country ? ` (${r.country})` : ""}${r.org ? ` · ${r.org}` : ""}`));
      } catch (e) { out.replaceChildren(h("div", { class: "note err" }, "Proxy test failed: " + e.message)); }
    } }, "Test");
    return [h("div", { class: "set-row" }, input, test), out];
  }
  return null;
}

let settingsScroll = 0;
function renderSettingsSoft() { if (S.drawer === "settings") { settingsScroll = $("#drawerBody").scrollTop; renderSettings(); $("#drawerBody").scrollTop = settingsScroll; } }
function renderSettings() {
  $("#drawerTitle").textContent = "Settings";
  const body = $("#drawerBody");
  body.replaceChildren();
  for (const [title, items] of schema()) {
    const sec = h("section", { class: "set-section" }, h("h3", {}, title));
    for (const it of items) {
      if (it.hide && it.hide()) continue;
      if (it.show && !it.show(S.settings)) continue;
      if (it.t === "login") { sec.append(loginBlock()); continue; }
      if (it.t === "about") { sec.append(aboutBlock()); continue; }
      const c = control(it);
      const row = h("div", { class: "set" + (it.stack || it.t === "seg" || it.t === "proxy" || it.t === "folder" ? " stack" : "") },
        h("span", { class: "label" }, it.l), c, it.help ? h("div", { class: "help" }, it.help) : null);
      sec.append(row);
    }
    body.append(sec);
  }
}
function loginBlock() {
  const lg = S.login || {};
  const wrap = h("div", {});
  if (lg.saved) wrap.append(h("div", { class: "note ok" }, `Saved login for ${lg.sites.join(", ")} · forgotten in ${lg.days_left} day${lg.days_left === 1 ? "" : "s"}`));
  else wrap.append(h("p", { class: "muted", style: "margin:0 0 10px" }, "Only needed for private, age-restricted or members-only videos."));
  const rowEl = h("div", { class: "set-row", style: "margin-top:10px" });
  if (S.platform === "win32") rowEl.append(h("button", { class: "btn", onclick: importLogin }, icon("lock"), lg.saved ? "Import new cookies.txt" : "Import cookies.txt"));
  rowEl.append(h("button", { class: "btn", onclick: (e) => loginMenu(e.currentTarget) }, "Choose login…"));
  if (lg.saved) rowEl.append(h("button", { class: "ghost small danger", onclick: forgetLogin }, "Forget"));
  wrap.append(rowEl);
  return wrap;
}
function aboutBlock() {
  const e = S.engines || {};
  const out = h("div", { class: "help", style: "margin-top:8px" });
  const run = (label, fn) => h("button", { class: "btn", onclick: async (ev) => {
    const b = ev.currentTarget; b.disabled = true;
    out.replaceChildren(h("span", { class: "muted" }, label + "…"));
    try { const r = await fn(); out.replaceChildren(h("div", { class: "note ok" }, r)); } catch (er) { out.replaceChildren(h("div", { class: "note err" }, er.message)); }
    b.disabled = false;
  } }, label);
  return h("div", {},
    h("p", { style: "margin:0 0 6px" }, h("b", {}, `Video Downloader ${$("#ver").textContent}`)),
    h("p", { class: "mono muted", style: "margin:0 0 12px;font-size:12px" }, `yt-dlp ${e["yt-dlp"] || "missing"} · gallery-dl ${e["gallery-dl"] || "missing"} · ffmpeg ${e.ffmpeg ? "bundled" : "missing"}`),
    h("div", { class: "set-row" },
      run("Update download engines", async () => (await api("/api/update", { what: "engine" })).message),
      run("Check for app updates", async () => { const r = await api("/api/update", { what: "app" }); if (r.newer) { S.update = { tag: r.latest, url: r.url }; showUpdate(); return `Version ${r.latest} is out — use the button at the top.`; } return `You have the latest version (${r.latest}).`; })),
    out,
    h("p", { class: "faint", style: "font-size:12px;margin-top:14px" }, "If a site stops working, update the download engines first — sites change often."));
}

// ---------- phone link ----------
function openPhone() {
  openDrawer("phone", renderPhone);
}
function renderPhone() {
  $("#drawerTitle").textContent = "Phone link";
  const p = S.phone || {};
  const sw = h("button", { class: "switch", role: "switch", "aria-checked": String(!!p.on), onclick: async (e) => {
    const on = e.currentTarget.getAttribute("aria-checked") !== "true";
    try { S.phone = await api("/api/phone", { on }); } catch (er) { toast(er.message, "err"); }
    renderPhone();
  } });
  const body = [
    h("section", { class: "set-section" },
      h("p", { class: "muted", style: "margin:0 0 12px" }, "Use this app from your phone on the same Wi-Fi: send links to download here, and save finished files straight to your phone."),
      h("div", { class: "set" }, h("span", { class: "label" }, "Allow my phone to connect"), sw)),
  ];
  if (p.on) {
    const qr = h("div", { class: "qr" }, h("img", { src: p.qr, alt: "QR code for the phone link", width: 180, height: 180 }));
    body.push(h("section", { class: "set-section" },
      h("h3", {}, "Scan with your phone’s camera"), p.qr ? qr : null,
      h("div", { class: "url-box" }, p.url),
      h("div", { class: "set-row", style: "margin-top:10px" }, h("button", { class: "btn", onclick: () => api("/api/copy", { text: p.url }).then(() => toast("Link copied")) }, icon("copy"), "Copy link")),
      h("div", { class: "note", style: "margin-top:14px" }, "Anyone with this link on your network can add downloads and save finished files. It stops working when you turn this off or close the app. Only turn it on at home or on networks you trust.")));
  }
  $("#drawerBody").replaceChildren(...body);
  $("#phoneBtn").classList.toggle("on", !!p.on);
}

// ---------- tools ----------
const TOOL_DESC = {
  remux: "Repackage a video that won’t open or won’t import into an editor. Fast, no quality loss.",
  h264: "Re-encode AV1, VP9 or HEVC so phones, TVs and editors like Premiere can play it.",
  audio: "Save just the sound as MP3, M4A, Opus, WAV or FLAC.",
  gif: "Turn a clip into a sharp, looping GIF.",
  trim: "Cut out the part you want.",
  mute: "Remove the sound and keep the picture. Instant.",
};
function toolOptions(tool) {
  const sel = (name, opts, val) => h("select", { "data-p": name }, opts.map(([v, l]) => h("option", { value: v, selected: v === val }, l)));
  const inp = (name, ph) => h("input", { "data-p": name, placeholder: ph });
  switch (tool) {
    case "remux": return ["Into", sel("container", [["mp4", "MP4"], ["mkv", "MKV"], ["mov", "MOV"], ["webm", "WebM"]], "mp4")];
    case "h264": return ["Quality", sel("quality", [["high", "High"], ["balanced", "Balanced"], ["small", "Small file"]], "balanced"), "Max", sel("max_height", [["", "Original size"], ["2160", "4K"], ["1080", "1080p"], ["720", "720p"], ["480", "480p"]], "")];
    case "audio": return ["As", sel("format", [["mp3", "MP3"], ["m4a", "M4A"], ["opus", "Opus"], ["wav", "WAV"], ["flac", "FLAC"]], "mp3"), sel("bitrate", [["320", "320k"], ["256", "256k"], ["192", "192k"], ["128", "128k"]], "320")];
    case "gif": return ["From", inp("start", "0:00"), "to", inp("end", "end"), sel("fps", [["10", "10 fps"], ["15", "15 fps"], ["24", "24 fps"]], "15"), sel("width", [["320", "320px"], ["480", "480px"], ["640", "640px"], ["800", "800px"]], "480")];
    case "trim": return ["From", inp("start", "0:00"), "to", inp("end", "end"), sel("exact", [["1", "Exact"], ["", "Fast"]], "1")];
    default: return [];
  }
}
function readToolParams(card) {
  const p = {};
  $$("[data-p]", card).forEach((el) => {
    let v = el.value.trim();
    if (el.dataset.p === "exact") v = v === "1";
    if (v !== "") p[el.dataset.p] = v;
  });
  return p;
}
function buildTools() {
  S.toolsBuilt = true;
  const grid = $("#toolGrid");
  grid.replaceChildren();
  for (const [tool, name] of Object.entries(S.choices.tools || {})) {
    const card = h("div", { class: "tool", "data-tool": tool },
      h("div", { class: "tool-head" }, h("div", { class: "tool-icon" }, icon(TOOL_ICON[tool] || "wand")),
        h("div", {}, h("h3", {}, name), h("p", {}, TOOL_DESC[tool] || ""))),
      h("div", { class: "tool-opts" }, toolOptions(tool)),
      h("button", { class: "btn accent", onclick: () => pickToolFiles(tool, card) }, icon("folder"), "Choose files…"));
    grid.append(card);
  }
}
async function pickToolFiles(tool, card) {
  try {
    const r = await api("/api/dialog", { kind: "files" });
    if (r.paths && r.paths.length) runTool(tool, r.paths, readToolParams(card));
  } catch (e) { toast(e.message, "err"); }
}
async function runTool(tool, files, params) {
  try {
    const r = await api("/api/tool", { tool, files, params });
    showView("downloads");
    toast(`${S.choices.tools[tool]}: ${r.added} file${r.added === 1 ? "" : "s"} added`);
  } catch (e) { toast(e.message, "err"); }
}
function filesDropped(paths) {
  if (!paths || !paths.length) return;
  const names = paths.map((p) => p.split(/[\\/]/).pop());
  const buttons = Object.entries(S.choices.tools || {}).map(([tool, name]) => h("button", { class: "btn", onclick: () => {
    const card = $(`.tool[data-tool="${tool}"]`);
    closeSheet();
    runTool(tool, paths, card ? readToolParams(card) : {});
  } }, icon(TOOL_ICON[tool]), name));
  openSheet([
    h("div", { class: "sheet-head" }, h("div", {}, h("h2", {}, `What should happen to ${paths.length === 1 ? "this file" : `these ${paths.length} files`}?`),
      h("p", {}, names.slice(0, 3).join(", ") + (names.length > 3 ? ` and ${names.length - 3} more` : ""))), h("button", { class: "icon-btn", onclick: () => closeSheet() }, icon("x"))),
    h("div", { class: "sheet-body" }, h("div", { class: "set-row" }, buttons),
      h("p", { class: "faint", style: "font-size:12.5px" }, "Options from the Tools tab are used. A new file is made next to the original.")),
  ], "narrow");
}

// ---------- history ----------
let historyTimer = null;
async function loadHistory() {
  const q = $("#historySearch").value;
  let items = [];
  try { items = (await api("/api/history?q=" + encodeURIComponent(q))).items; } catch (e) { toast(e.message, "err"); }
  const list = $("#historyList");
  list.replaceChildren(...items.map((it) => {
    const kindIcon = it.kind === "tool" ? TOOL_ICON[it.tool] || "wand" : it.media === "audio" ? "music" : (it.media === "images" || it.media === "image") ? "image" : "video";
    const files = (it.paths || []).length;
    const act = h("div", { class: "job-actions" });
    const btn = (name, title, fn) => h("button", { class: "icon-btn", title, "aria-label": title, onclick: async () => {
      try { await fn(); } catch (e) { toast(e.message, "err"); }
    } }, icon(name));
    if (it.exists) { act.append(btn("play", "Open", () => api("/api/history", { action: "open", id: it.id })), btn("folder", "Show in folder", () => api("/api/history", { action: "show", id: it.id }))); }
    if (it.url) act.append(btn("retry", "Download again", async () => { await api("/api/history", { action: "redownload", id: it.id }); toast("Added to the queue"); }));
    act.append(btn("x", "Remove from history", async () => { await api("/api/history", { action: "remove", id: it.id }); loadHistory(); }));
    return h("li", { class: "hrow" + (it.exists ? "" : " missing"), title: it.exists ? "" : "The file was moved or deleted" },
      h("div", { class: "hicon" }, icon(kindIcon)),
      h("div", { style: "min-width:0" }, h("div", { class: "htitle" }, it.title || it.url),
        h("div", { class: "hmeta" }, [it.site, ago(it.time), fmtBytes(it.size), files > 1 ? `${files} files` : null].filter(Boolean).join("  ·  "))),
      act);
  }));
  $("#historyEmpty").hidden = items.length !== 0;
  $("#historyClear").hidden = items.length === 0 || !!q;
}

// ---------- views ----------
function showView(v) {
  S.view = v;
  $$("#tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.view === v)));
  $$(".view").forEach((el) => el.classList.toggle("active", el.id === "view-" + v));
  if (v === "history") loadHistory();
  if (v === "downloads") requestAnimationFrame(() => placeGlider($("#modeSeg")));
}

// ---------- toasts ----------
function toast(msg, kind = "", action) {
  const t = h("div", { class: "toast " + kind, role: kind === "err" ? "alert" : "status" }, h("span", { class: "tmsg" }, msg));
  if (action) t.append(h("button", { class: "btn accent", onclick: () => { action[1](); dismiss(); } }, action[0]));
  const box = $("#toasts");
  box.append(t);
  while (box.children.length > 3) box.firstChild.remove();
  const dismiss = () => { t.classList.add("out"); setTimeout(() => t.remove(), 260); };
  setTimeout(dismiss, action ? 9000 : kind === "err" ? 6000 : 2800);
}
function offerClipboard(urls) {
  if (!urls || !urls.length || document.activeElement === links()) return;
  const label = urls.length === 1 ? urls[0].replace(/^https?:\/\/(www\.)?/, "").slice(0, 60) : `${urls.length} copied links`;
  toast(`Download ${label}?`, "", ["Download", () => submit(urls.join("\n"))]);
}

// ---------- drag & drop ----------
function wireDrop() {
  let depth = 0;
  const zone = $("#dropzone");
  const show = (on) => zone.classList.toggle("show", on);
  window.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; show(true); });
  window.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) show(false); });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", (e) => {
    e.preventDefault();
    depth = 0;
    show(false);
    const dt = e.dataTransfer;
    if (dt.files && dt.files.length) {
      // Inside the app window, Python receives the real file paths and sends a "files" event.
      if (S.role === "remote") toast("Files can only be dropped on the computer", "err");
      return;
    }
    const text = dt.getData("text/uri-list") || dt.getData("text/plain");
    if (text) submit(text);
  });
}

// ---------- onboarding ----------
function onboard() {
  S.settings.onboarded = true;
  setSetting("onboarded", true);
  setTimeout(() => toast("Tip: copy a link and press Ctrl+V anywhere here — or turn on “Offer links I copy” in Settings."), 900);
}

// ---------- keyboard & global wiring ----------
function wireGlobal() {
  $$("#tabs button").forEach((b) => b.onclick = () => showView(b.dataset.view));
  $("#settingsBtn").onclick = openSettings;
  $("#phoneBtn").onclick = openPhone;
  $("#scrim").onclick = closeDrawer;
  $("[data-close]").onclick = closeDrawer;
  $("#sheetWrap").addEventListener("mousedown", (e) => { if (e.target === e.currentTarget) closeSheet(); });
  document.addEventListener("mousedown", (e) => { if (popAnchor && !e.target.closest("#popover") && !popAnchor.contains?.(e.target)) closePopover(); });
  window.addEventListener("blur", closePopover);
  $("main").addEventListener("scroll", closePopover, { passive: true });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if ($("#popover").classList.contains("show")) closePopover();
      else if ($("#sheetWrap").classList.contains("show")) closeSheet();
      else if ($("#drawer").classList.contains("show")) closeDrawer();
      return;
    }
    if (e.target.closest("input, textarea, select")) return;
    if (e.key === "/" || (e.ctrlKey && e.key.toLowerCase() === "l")) { e.preventDefault(); showView("downloads"); links().focus(); }
    if (e.ctrlKey && e.key === ",") { e.preventDefault(); openSettings(); }
  });
  $("#historySearch").addEventListener("input", () => { clearTimeout(historyTimer); historyTimer = setTimeout(loadHistory, 200); });
  $("#historyClear").onclick = async () => {
    if (await confirmSheet("Clear the history?", [h("p", {}, "Your downloaded files stay where they are.")], "Clear")) {
      await api("/api/history", { action: "clear" });
      loadHistory();
    }
  };
}

// A browser's colour scheme can change while the page is open; switch instantly instead of fading every
// surface. (Inside the app window the launcher tells us the Windows theme instead: WebView2's own report lags
// and can be wrong.)
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
  const root = document.documentElement;
  root.classList.add("preload");
  setTimeout(() => root.classList.remove("preload"), 120);
});

document.addEventListener("DOMContentLoaded", () => {
  wireComposer();
  wireGlobal();
  wireDrop();
  if (!TOKEN) { toast("Open the app from its window or the phone link QR code", "err"); return; }
  connect();
  if (innerWidth < 560) links().placeholder = "Paste a link";
  links().focus();
});
