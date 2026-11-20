"use strict";
/* Photos web app. Served as /app.js so the page runs under a CSP with no inline script (script-src 'self'). */
/* a thumbnail that cannot be rendered shows a placeholder: a play mark for a video, a broken-image mark for a photo (PH86) */
document.addEventListener("error", e => {
  const t = e.target;
  if (!(t && t.tagName === "IMG" && t.parentElement && t.parentElement.classList.contains("tile"))) return;
  const vid = t.parentElement.dataset.video === "1";
  t.replaceWith(Object.assign(document.createElement("div"), vid ? {className: "vid", innerHTML: "&#9654;"} : {className: "broken", innerHTML: "<b>&#9888;</b>Cannot show this photo"}));
}, true);
const $ = id => document.getElementById(id);
const api = async (url, opt = {}) => {
  const r = await fetch(url, {credentials: "same-origin", ...opt, headers: {"Content-Type": "application/json", ...(opt.headers || {})}});
  if (r.status === 401 && !url.includes("/auth/login")) { showLogin(); throw new Error("401"); }
  if (!r.ok && r.status !== 204) { let m = r.statusText; try { m = (await r.json()).message || m; } catch (e) {} const x = new Error(m); x.status = r.status; throw x; }
  return r.status === 204 ? null : r.json();
};
const post = (u, b, m = "POST") => api(u, {method: m, body: JSON.stringify(b || {})});
const put = (u, b) => post(u, b, "PUT");
const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
let albums = [], route = "all", months = [], obs = null, favMode = false;
let FEAT = {faces: true, search: true};  // /api/server/features: People and the search box only when the server can do them
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const mobile = () => matchMedia("(max-width:768px)").matches;

/* PH86: one in-app dialog instead of the browser's confirm/prompt/alert (styled, dark mode, never blocked on iOS) */
let askDone = null;
function dialog({title = "", msg = "", ok = "OK", cancel = "Cancel", danger = false, input = null, value = "", typed = null, placeholder = ""}) {
  if (askDone) askDone(null);
  $("askTitle").textContent = title; $("askTitle").hidden = !title; $("askMsg").textContent = msg; $("askErr").textContent = "";
  $("askYes").textContent = ok; $("askYes").className = danger ? "danger" : "primary";
  $("askNo").textContent = cancel; $("askNo").hidden = cancel === null;
  const inp = $("askIn"); inp.hidden = input === null && typed === null; inp.value = value; inp.placeholder = typed ? `Type ${typed}` : placeholder;
  $("ask").showModal(); (inp.hidden ? $("askYes") : inp).focus(); if (!inp.hidden) inp.select();
  return new Promise(res => {
    askDone = v => { askDone = null; if ($("ask").open) $("ask").close(); res(v); };
    $("askForm").onsubmit = e => {
      e.preventDefault();
      if (typed !== null && inp.value.trim() !== typed) { $("askErr").textContent = `Type ${typed} to confirm`; return; }
      askDone(inp.hidden ? true : inp.value);
    };
  });
}
$("askNo").onclick = () => askDone && askDone(null);
$("ask").addEventListener("cancel", () => askDone && askDone(null));
const ask = (msg, o = {}) => dialog({msg, ...o}).then(v => v === true || (typeof v === "string"));
const askText = (msg, value = "", o = {}) => dialog({msg, input: true, value, ...o});
const tell = (msg, title = "") => dialog({msg, title, cancel: null});

/* theme + size (kept in localStorage) */
function setTheme(v) { v ? document.documentElement.dataset.theme = v : delete document.documentElement.dataset.theme; localStorage.photos_theme = v; }
function setSize(v) { localStorage.photos_tile = v; document.documentElement.style.setProperty("--tile", v + "px"); if (typeof layoutPlaceholders === "function" && months.length) layoutPlaceholders(); }
setTheme(localStorage.photos_theme || ""); setSize(localStorage.photos_tile || "160");

/* login */
let setupMode = false;
async function showLogin() {
  if ($("login").open) return;
  try { setupMode = (await (await fetch("/api/server/config")).json()).isInitialized === false; } catch (e) { setupMode = false; }
  $("loginSub").textContent = setupMode ? "First run: create your account" : "";
  $("loginBtn").textContent = setupMode ? "Create account" : "Sign in";
  $("pw2row").hidden = $("pinrow").hidden = !setupMode; $("pw2").required = setupMode;
  $("pw").autocomplete = setupMode ? "new-password" : "current-password";
  $("login").showModal();
}
$("login").addEventListener("cancel", e => e.preventDefault());
$("loginForm").onsubmit = async e => {
  e.preventDefault(); $("loginErr").textContent = "";
  try {
    if (setupMode) {
      if ($("pw").value !== $("pw2").value) throw new Error("The passwords do not match");
      await api("/api/auth/admin-sign-up", {method: "POST", body: JSON.stringify({email: $("email").value, password: $("pw").value, pinCode: $("pinsetup").value})});
      $("pw2").value = $("pinsetup").value = "";
    }
    await api("/api/auth/login", {method: "POST", body: JSON.stringify({email: $("email").value, password: $("pw").value})}); $("pw").value = ""; $("login").close(); start(); }
  catch (err) { $("loginErr").textContent = err.message; }
};
async function logout() { await post("/api/auth/logout").catch(() => {}); location.reload(); }

/* routing */
function go(r) { location.hash = "#/" + r; }
window.addEventListener("hashchange", () => {
  const r = location.hash.slice(2) || "all";
  if (r.startsWith("shared/")) return sharedDone(r);
  if (r !== route) clearSel();  // a selection never follows you into another view (Delete would hit photos you cannot see)
  route = r; render();
});

async function start() {
  try { await api("/api/users/me"); } catch (e) { return; }
  try { const f = await api("/api/server/features"); FEAT = {faces: !!f.facialRecognition, search: !!f.smartSearch}; } catch (e) { /* keep both on */ }
  $("searchf").hidden = !FEAT.search; $("vtag").hidden = !FEAT.faces;
  await loadAlbums(); watchScan(); lockTick();
  const h = location.hash.slice(2);
  if (h.startsWith("shared/")) return sharedDone(h);
  route = h || localStorage.photos_route || "all";
  render();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});  // PH87: installable, share target
}
/* PH87: back from the phone's share sheet */
function sharedDone(r) {
  const [s, d, f] = r.split("/").slice(1).map(Number);
  history.replaceState(null, "", "#/all"); route = "all";
  loadAlbums().then(render).then(() => uploadToast(s || 0, d || 0, f ? [`${f} could not be saved (not a photo or video, or too large)`] : []));
}
const ROW = (r, label, n, extra = "") => `<a class="item${route === r ? " sel" : ""}" href="#/${r}" data-r="${r}" ${extra}><span class="t">${label}</span><span class="n">${n ?? ""}</span></a>`;
async function loadAlbums() {
  const [al, cn] = await Promise.all([api("/api/albums"), api("/ui/counts").catch(() => ({}))]);
  albums = al;
  $("lib").innerHTML = ROW("all", "All photos", cn.all?.toLocaleString()) + ROW("recent", "Recently added") + ROW("explore", "Explore") + ROW("map", "Map") + ROW("fav", "Favorites", cn.favorites || "") +
    (FEAT.faces ? ROW("people", "People") : "") + ROW("hidden", "Hidden &#128274;") + ROW("trash", "Trash", cn.trash || "") + ROW("stats", "Library dashboard");
  $("albums").innerHTML = albums.map(a => ROW("album/" + a.id, esc(a.albumName) + (a.isUploadFolder ? " &#128274;" : ""), a.assetCount.toLocaleString(),
    `draggable="true" data-a="${a.id}" title="${esc(a.relPath || a.description)}${a.insideOf && a.insideOf.length ? " (inside " + esc(a.insideOf.join(", ")) + ")" : ""}"`)).join("") ||
    '<div class="empty">No albums yet. Use Add folder.</div>';
  document.querySelectorAll("#side .item").forEach(el => el.addEventListener("click", () => $("side").classList.remove("open")));
  albumDrag();
  const ua = albums.find(a => a.isUploadFolder);
  $("upload").title = `Upload photos and videos from this device into "${ua ? ua.albumName : "_Organize"}" (it opens with the PIN)`;  // PH86
}
/* PH79: drag albums in the sidebar into your own order (stored in albums.yml) */
function albumDrag() {
  let dragged = null;
  document.querySelectorAll("#albums .item[data-a]").forEach(el => {
    el.addEventListener("dragstart", e => { dragged = el; el.classList.add("drag"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/x-album", el.dataset.a); });
    el.addEventListener("dragend", () => { el.classList.remove("drag"); document.querySelectorAll(".dropto").forEach(x => x.classList.remove("dropto")); });
    el.addEventListener("dragover", e => { if (!dragged || dragged === el) return; e.preventDefault(); el.classList.add("dropto"); });
    el.addEventListener("dragleave", () => el.classList.remove("dropto"));
    el.addEventListener("drop", async e => {
      if (!dragged || dragged === el) return; e.preventDefault(); e.stopPropagation();
      el.before(dragged); dragged = null;
      const ids = [...document.querySelectorAll("#albums .item[data-a]")].map(x => x.dataset.a);
      try { await post("/ui/albums/order", {ids}); } catch (x) { tell(x.message); }
      loadAlbums();
    });
  });
}
/* PH55: a short message with an Undo (or another action), for changes that were just confirmed */
function toast(msg, undo, action) {
  document.querySelectorAll(".toast").forEach(t => t.remove());
  const t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status");
  const m = document.createElement("span"); m.textContent = msg; t.append(m);
  const btn = (label, fn) => { const b = document.createElement("button"); b.textContent = label; b.onclick = async () => { t.remove(); try { await fn(); } catch (e) { tell(e.message); } }; t.append(b); };
  if (undo) btn("Undo", undo);
  if (action) btn(action[0], action[1]);
  const x = document.createElement("button"); x.textContent = "✕"; x.setAttribute("aria-label", "Close"); x.onclick = () => t.remove(); t.append(x);
  document.body.append(t); setTimeout(() => t.remove(), 10000);
}
const nameOf = n => n ? `"${n}"` : "this person";
async function renamePerson(id, oldName, input, after) {
  const nu = input.value.trim();
  if (nu === oldName) return;
  const q = !nu ? `Remove the name ${nameOf(oldName)}? The person stays, unnamed.` : oldName ? `Rename ${nameOf(oldName)} to "${nu}"?` : `Name this person "${nu}"?`;
  if (!(await ask(q))) { input.value = oldName; return; }
  try { await put("/ui/people/" + id, {name: nu}); input.dataset.old = nu; toast(nu ? `Named "${nu}"` : "Name removed", async () => { await put("/ui/people/" + id, {name: oldName}); input.value = oldName; input.dataset.old = oldName; after && after(); }); after && after(); }
  catch (e) { input.value = oldName; tell(e.message); }
}
async function hidePerson(id, name, hide, after) {
  if (hide && !(await ask(`Stop showing ${nameOf(name)}? They disappear from People; "Show hidden" brings them back.`))) return;
  try { await put("/ui/people/" + id, {hidden: hide}); toast(hide ? `${name ? `"${name}"` : "Person"} hidden` : "Shown again", async () => { await put("/ui/people/" + id, {hidden: !hide}); after && after(); }); after && after(); }
  catch (e) { tell(e.message); }
}

let fmtSel = "";
async function loadFormats(q) {
  const f = await api("/ui/formats?" + q).catch(() => []);
  if (fmtSel && !f.some(x => x.format === fmtSel)) fmtSel = "";  // the chosen format does not exist in this view
  const html = '<option value="">All formats</option>' + f.map(x => `<option value="${x.format}">${x.format.toUpperCase()} (${x.count.toLocaleString()})</option>`).join("");
  $("fmt").innerHTML = $("fmt2").innerHTML = html;
  $("fmt").value = $("fmt2").value = fmtSel;
}
$("fmt").onchange = $("fmt2").onchange = e => { fmtSel = e.target.value; render(); };

let sortKey = localStorage.photos_sort || "capture", sortDir = localStorage.photos_dir || "desc";
let searchSort = localStorage.photos_ssort || "match";  // PH76: a search shows the best matches first unless chosen otherwise
const SORT_LABELS = {capture: "Capture date", modified: "Modification time", added: "Date added", name: "Name", size: "Size", type: "File type"};
function setSort(k, d) {
  if (route.startsWith("search/")) { searchSort = k; localStorage.photos_ssort = k; if (k === "match") { render(); return; } }
  if (k !== "match") { sortKey = k; localStorage.photos_sort = k; }
  sortDir = d; localStorage.photos_dir = d; render();
}
const curSort = () => route === "recent" ? "added" : route.startsWith("search/") && searchSort === "match" ? "match" : sortKey;
let renderGen = 0;  // each render() bumps it; an older run that wakes up after an await sees it changed and stops
const PAGES = {explore: () => renderExplore(), people: () => renderPeople(), map: () => renderMap(), stats: () => renderStats(), places: () => renderPlaces()};
async function render() {
  renderGen++;
  if ((!FEAT.faces && (route === "people" || route.startsWith("person/"))) || (!FEAT.search && route.startsWith("search/"))) route = "all";
  clearTimeout(peopleTimer);
  stopSlides();
  const page = Object.keys(PAGES).find(k => route === k || route.startsWith(k + "/"));
  if (page) { if (obs) obs.disconnect(); localStorage.photos_route = route; markSide(); $("rail").innerHTML = ""; return PAGES[page](); }
  const gen = renderGen;
  await renderMain();
  if (gen !== renderGen) return;
  if (route.startsWith("person/")) await personBar();
  if (/^(place|memory|search|month|spot)\//.test(route) || route === "recent") titleBar();
}
function markSide() { document.querySelectorAll("#side .item").forEach(el => el.classList.toggle("sel", el.dataset.r === route || (route.startsWith("map/") && el.dataset.r === "map"))); }
$("searchf").onsubmit = e => { e.preventDefault(); const v = $("search").value.trim(); if (v) go("search/" + encodeURIComponent(v)); };
const yearsAgo = n => (+n === 1 ? "1 year ago" : n + " years ago");
function titleBar() {
  const bar = document.createElement("div"); bar.className = "pbar";
  const r = route, arg = r.slice(r.indexOf("/") + 1);
  const [t, back, label] = r.startsWith("search/") ? ["Search: " + decodeURIComponent(arg), "all", "All photos"]
    : r.startsWith("place/") ? [decodeURIComponent(arg).replace("|", ", "), "explore", "Explore"]
    : r.startsWith("month/") ? [`${MONTHS[new Date().getMonth()]} ${new Date().getFullYear() - +arg} (${yearsAgo(arg)})`, "explore", "Explore"]
    : r.startsWith("spot/") ? ["Photos taken here", "map", "Map"]
    : r === "recent" ? ["Recently added: newest files first, whatever their capture date", "all", "All photos"]
    : [yearsAgo(arg.split("/")[0]) + ": around this day", "explore", "Explore"];
  bar.innerHTML = `<button id="xback">&larr; ${label}</button><b>${esc(t)}</b>` + (r.startsWith("search/") ? ` <label class="path"><input type="checkbox" id="xbest"${curSort() === "match" ? " checked" : ""}> Best matches first</label>` : "");
  $("grid").prepend(bar); $("xback").onclick = () => go(back);
  if ($("xbest")) $("xbest").onchange = e => setSort(e.target.checked ? "match" : sortKey, sortDir);
}
function emptyText() {  // PH86: one message per view, not "use Add folder" everywhere
  const r = route;
  if (r.startsWith("search/")) return `No matches for "${esc(decodeURIComponent(r.slice(7)))}". Try other words (smart search looks at what is in the photo).`;
  if (r === "fav") return "No favorites yet: open a photo and tap &#9734;.";
  if (r === "hidden") return "No hidden photos. Hide one from the viewer, the Tools menu or by right-click.";
  if (isUploadAlbum()) return "The upload album is empty. Drop photos here or use Upload to send them from this device.";
  if (r.startsWith("person/")) return "No photos of this person (the ones in Hidden or the upload album are not shown).";
  if (r.startsWith("album/")) return "This album has no photos yet.";
  if (/^(place|memory|month|spot)\//.test(r)) return "No photos here.";
  if (fmtSel) return "No photos in this format here.";
  return albums.length ? "No photos yet. Rescan folders, or add a folder." : "Nothing here yet. Use Add folder to pick a folder as an album.";
}
function railLabel(m) {
  if (m.key.startsWith("grp:") || m.key.startsWith("match:") || m.key.startsWith("page:")) return m.label || "";
  return "";
}
async function renderMain() {
  const gen = renderGen, stale = () => gen !== renderGen;
  localStorage.photos_route = route;
  markSide();
  if (obs) obs.disconnect();
  if (route === "trash") return renderTrash();
  const q = new URLSearchParams();
  if (route === "hidden") { if (!(await ensureUnlocked("Hidden photos"))) { if (!stale()) go("all"); return; } q.set("hidden", "true"); }
  if (isUploadAlbum()) { const a = albums.find(x => x.id === route.slice(6)); if (!(await ensureUnlocked(`Upload album "${a.albumName}"`))) { if (!stale()) go("all"); return; } }  // PH60
  if (route.startsWith("album/")) q.set("albumId", route.slice(6));
  if (route.startsWith("person/")) q.set("personId", route.slice(7));
  if (route.startsWith("place/")) q.set("place", decodeURIComponent(route.slice(6)));
  if (route.startsWith("memory/")) { const [n, d] = route.slice(7).split("/"); q.set("onThisDay", n); if (d) q.set("days", d); }
  if (route.startsWith("month/")) q.set("thisMonth", route.slice(6));
  if (route.startsWith("spot/")) q.set("bbox", route.slice(5));
  if (route.startsWith("search/")) q.set("search", decodeURIComponent(route.slice(7)));
  favMode = route === "fav"; if (favMode) q.set("isFavorite", "true");
  await loadFormats(q);
  if (stale()) return;
  if (fmtSel) q.set("format", fmtSel);
  const sort = curSort();
  q.set("sort", sort); q.set("order", route === "recent" ? "desc" : sortDir);
  const b = await api("/api/timeline/buckets?" + q);
  if (stale()) return;
  months = b.map(x => ({key: x.timeBucket, count: x.count, label: x.label, loaded: false, q: q.toString()}));
  const n = b.reduce((s, x) => s + x.count, 0);
  const where = route.startsWith("album/") ? " in this album" + (isUploadAlbum() ? ": drop photos here to upload them" : "") : favMode ? " in favorites"
    : route === "hidden" ? " hidden" + (hiddenDefaultPin ? " (still the default PIN: change it in Tools)" : "") : route.startsWith("person/") ? " of this person"
    : /^(place|spot)\//.test(route) ? " here" : /^(memory|month)\//.test(route) ? " then" : route.startsWith("search/") ? " match" : "";
  const sortTxt = sort === "match" ? "best matches first" : "sorted by " + SORT_LABELS[sort].toLowerCase() + (route === "recent" ? "" : sortDir === "asc" ? " (ascending)" : " (descending)");
  setStatus(n.toLocaleString() + " items" + where + ", " + sortTxt);
  $("grid").innerHTML = n ? "" : `<div class="empty">${emptyText()}</div>`;
  $("rail").innerHTML = ""; if (!n) return;
  let lastY = "";
  months.forEach((m, i) => {
    const grp = railLabel(m) || m.key.startsWith("page:"), start = m.key.startsWith("page:") ? +m.key.slice(5) * 500 + 1 : 0;
    const y = grp ? "" : m.key.slice(0, 4), mi = grp ? 0 : +m.key.slice(5, 7) - 1;
    const pageNo = +(m.key.split(":").pop() || 0);
    const head = m.key.startsWith("page:") ? start.toLocaleString() + " - " + (start + m.count - 1).toLocaleString() : grp ? esc(m.label) + (pageNo ? ` (part ${pageNo + 1})` : "") : MONTHS[mi] + " " + y;
    const sec = document.createElement("section"); sec.className = "month"; sec.id = "m" + i; sec.dataset.i = i;
    sec.innerHTML = `<h3>${head} <span style="font-weight:400">(${m.count.toLocaleString()})</span></h3><div class="tiles" role="grid" aria-label="${head}"></div>`;
    $("grid").appendChild(sec);
    const a = document.createElement("a");
    if (grp) { if (pageNo) return; a.textContent = m.key.startsWith("page:") ? start.toLocaleString() : (m.label || "").slice(0, 8); a.className = "y"; }
    else { a.textContent = y !== lastY ? y : MONTHS[mi].slice(0, 3); if (y !== lastY) a.className = "y"; lastY = y; }
    a.dataset.i = i; a.dataset.tip = grp ? (m.label || head) : MONTHS[mi] + " " + y;
    a.onclick = () => $("m" + i).scrollIntoView(); $("rail").appendChild(a);
  });
  layoutPlaceholders();
  obs = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) loadMonth(+e.target.dataset.i); }), {root: $("main"), rootMargin: "1200px 0px"});
  document.querySelectorAll(".month").forEach(s => obs.observe(s));
}
/* PH81: "last scanned N min ago" on the status line */
let scanInfo = null, statusBase = "";
function setStatus(t) { statusBase = t; paintStatus(); }
function paintStatus() {
  let x = "";
  if (scanInfo && scanInfo.ago != null) { const m = Math.round(scanInfo.ago / 60); x = ` | scanned ${m < 1 ? "just now" : m < 120 ? m + " min ago" : Math.round(m / 60) + " h ago"}`; }
  if (scanInfo && scanInfo.watching && scanInfo.watching.on) x += ", new files show within seconds";
  $("status").textContent = statusBase + x;
}
async function refreshScanInfo() { scanInfo = await api("/ui/scan/info").catch(() => null); paintStatus(); }
setInterval(refreshScanInfo, 60000);
function layoutPlaceholders() {
  const w = $("grid").clientWidth - 32, tile = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--tile")) || 160;
  const cols = mobile() ? 3 : Math.max(1, Math.floor((w + 8) / (tile + 8)));
  months.forEach((m, i) => { if (!m.loaded) { const t = document.querySelector(`#m${i} .tiles`); if (t) t.style.minHeight = Math.ceil(m.count / cols) * (w / cols) + "px"; } });
}
/* One request per month at a time: callers share the in-flight promise. A failure is not retried in a loop; the month
   is tried again only when it scrolls into view again (the observer), at most once every 5 seconds. */
function loadMonth(i) {
  const m = months[i]; if (!m) return Promise.resolve();
  if (m.ids) return Promise.resolve();
  if (m.p) return m.p;
  if (m.failedAt && Date.now() - m.failedAt < 5000) return Promise.resolve();
  m.loaded = true;
  return m.p = fillMonth(i, m).finally(() => { m.p = null; });
}
const fmtDay = iso => { const d = iso ? new Date(iso.slice(0, 19)) : null; return d && !isNaN(d) ? d.toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"}) : ""; };
async function fillMonth(i, m) {
  const q = new URLSearchParams(m.q); q.set("timeBucket", m.key);
  let c;
  try { c = await api("/api/timeline/bucket?" + q); } catch (e) { m.loaded = false; m.failedAt = Date.now(); return; }
  if (months[i] !== m) return;  // the view changed while this was loading: drop the stale answer
  m.ids = c.id; m.fav = c.isFavorite; m.img = c.isImage; m.rot = c.rotation || c.id.map(() => 0); m.live = c.livePhotoVideoId || c.id.map(() => null);
  m.names = c.fileName || c.id.map(() => ""); m.dates = c.fileCreatedAt || [];
  const box = document.querySelector(`#m${i} .tiles`); box.style.minHeight = "";
  box.innerHTML = c.id.map((id, k) => `<button class="tile${picked.has(id) ? " sel" : ""}" data-i="${i}" data-k="${k}" data-id="${id}" data-video="${c.isImage[k] ? 0 : 1}" role="gridcell" aria-label="${esc((c.isImage[k] ? "Photo " : "Video ") + (m.names[k] || "") + ", " + fmtDay(m.dates[k]))}${c.isFavorite[k] ? ", favorite" : ""}"><img loading="lazy" decoding="async" src="/api/assets/${id}/thumbnail?v=${m.rot[k]}" alt="">${c.isImage[k] ? (m.live[k] ? '<span class="badge" title="Live Photo">LIVE</span>' : "") : '<span class="badge">&#9654;</span>'}${c.isFavorite[k] ? '<span class="star">&#9733;</span>' : ""}</button>`).join("");
}
/* right-click (or long press on a phone, PH86) a thumbnail: the same actions as the viewer, without opening it */
let ctxEl = null;
const closeCtx = () => { if (ctxEl) { ctxEl.remove(); ctxEl = null; } };
document.addEventListener("click", closeCtx); document.addEventListener("scroll", closeCtx, true);
async function tileMenu(t, x, y) {
  closeCtx();
  const i = +t.dataset.i, k = +t.dataset.k, p = await at(i, k); if (!p) return;
  const a = await api("/api/assets/" + p.id).catch(() => null); if (!a) return;
  pos = {...p, a, ctx: true};
  const img = months[i].img[k];
  const items = [["Open", () => openViewer(i, k)], ["Select", () => { picked.add(p.id); lastPick = t; selMode = true; paintSel(); }], ["Convert...", () => $("vconv").click()], ["Download", download],
    ["Rotate left", () => rotate("left"), !img], ["Rotate right", () => rotate("right"), !img], ["Favorite", () => $("vfav").click()], [route === "hidden" ? "Unhide" : "Hide", toggleHide],
    ["Play Live Photo", () => { openViewer(i, k).then(() => playLive(false)); }, !months[i].live[k]], ["Delete Live Photo video...", deleteLiveVideo, !months[i].live[k]],
    ["Tag person...", () => { openViewer(i, k).then(startTag); }, !img || !FEAT.faces], ["Use as album cover", useAsCover, !route.startsWith("album/")], ["Slideshow from here", () => openViewer(i, k).then(() => startSlides())],
    ["Info", () => $("vinfo").click()], "-", ["Delete...", () => $("vdel").click()]];
  const m = document.createElement("div"); m.className = "mnu ctx";
  m.style.cssText = `position:fixed;display:block;left:${Math.max(4, Math.min(x, innerWidth - 230))}px;top:${Math.max(4, Math.min(y, innerHeight - 420))}px;max-height:${innerHeight - 8}px;overflow:auto`;
  items.forEach(it => { if (it === "-") { m.appendChild(document.createElement("hr")); return; }
    const b = document.createElement("button"); b.textContent = it[0]; b.disabled = !!it[2]; b.onclick = e => { e.stopPropagation(); closeCtx(); it[1](); }; m.appendChild(b); });
  document.body.appendChild(m); ctxEl = m;
}
$("grid").addEventListener("contextmenu", e => { const t = e.target.closest(".tile[data-id]"); if (!t) return; e.preventDefault(); tileMenu(t, e.clientX, e.clientY); });
let pressTimer = 0, pressed = false, px = 0, py = 0;  // iOS Safari never sends contextmenu on a long press
$("grid").addEventListener("pointerdown", e => {
  const t = e.target.closest(".tile[data-id]"); if (!t || e.pointerType === "mouse") return;
  pressed = false; px = e.clientX; py = e.clientY; clearTimeout(pressTimer);
  pressTimer = setTimeout(() => { pressed = true; tileMenu(t, px, py); }, 550);
});
["pointerup", "pointercancel"].forEach(ev => $("grid").addEventListener(ev, () => clearTimeout(pressTimer)));
$("grid").addEventListener("pointermove", e => { if (Math.hypot(e.clientX - px, e.clientY - py) > 12) clearTimeout(pressTimer); });
/* multi-select: Select button (phones), Ctrl/Cmd-click, Shift-click for a range */
const picked = new Set(); let selMode = false, lastPick = null;
function paintSel() {
  document.querySelectorAll(".tile[data-id]").forEach(t => t.classList.toggle("sel", picked.has(t.dataset.id)));
  $("selbar").hidden = !picked.size; $("selcount").textContent = picked.size + " selected";
  $("selmode").classList.toggle("on", selMode);
}
function clearSel() { picked.clear(); selMode = false; lastPick = null; paintSel(); }
function toggleSelMode() { selMode = !selMode; if (!selMode) picked.clear(); paintSel(); }
$("selmode").onclick = toggleSelMode;
$("selclear").onclick = clearSel;
$("seldel").onclick = async () => {
  const n = picked.size; if (!n) return;
  if (!(await ask(`Move ${n} selected photo${n > 1 ? "s" : ""} to the trash?\nThey disappear from their folders (and from any device that syncs those folders). You can restore them for 30 days.`, {ok: "Delete", danger: true}))) return;
  const y = $("main").scrollTop;
  try {
    const r = await post("/ui/assets/delete", {ids: [...picked], confirm: true});
    clearSel(); await Promise.all([loadAlbums(), render()]); $("main").scrollTop = y;
    if (r.failed) tell(r.failed + " could not be deleted (file already gone).");
  } catch (e) { tell(e.message); }
};
$("grid").addEventListener("click", async e => {
  const t = e.target.closest(".tile[data-id]"); if (!t) return;
  if (pressed) { pressed = false; e.preventDefault(); return; }  // the long press opened the menu
  if (!(selMode || picked.size || e.ctrlKey || e.metaKey || e.shiftKey)) { openViewer(+t.dataset.i, +t.dataset.k); return; }
  if (e.shiftKey && lastPick && lastPick.isConnected) {
    const a = {i: +lastPick.dataset.i, k: +lastPick.dataset.k}, b = {i: +t.dataset.i, k: +t.dataset.k};
    const [lo, hi] = a.i < b.i || (a.i === b.i && a.k <= b.k) ? [a, b] : [b, a];
    for (let i = lo.i; i <= hi.i; i++) await loadMonth(i);  // months in between that were never scrolled to count too
    for (let i = lo.i; i <= hi.i; i++) {
      const ids = (months[i] && months[i].ids) || [];
      ids.forEach((id, k) => { if ((i > lo.i || k >= lo.k) && (i < hi.i || k <= hi.k)) picked.add(id); });
    }
  } else if (picked.has(t.dataset.id)) picked.delete(t.dataset.id); else picked.add(t.dataset.id);
  lastPick = t; paintSel();
});
/* PH86: arrow keys move between tiles (up/down by a row), Enter opens */
$("grid").addEventListener("keydown", e => {
  const t = e.target.closest(".tile[data-id]"); if (!t || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) return;
  const all = [...document.querySelectorAll(".tile[data-id]")], i = all.indexOf(t);
  let j = i;
  if (e.key === "ArrowLeft") j = i - 1; else if (e.key === "ArrowRight") j = i + 1; else if (e.key === "Home") j = 0; else if (e.key === "End") j = all.length - 1;
  else {
    const r = t.getBoundingClientRect(), down = e.key === "ArrowDown";
    const cand = all.filter(x => { const q = x.getBoundingClientRect(); return down ? q.top > r.top + 4 : q.top < r.top - 4; });
    if (cand.length) {
      const rowTop = down ? Math.min(...cand.map(x => x.getBoundingClientRect().top)) : Math.max(...cand.map(x => x.getBoundingClientRect().top));
      const row = cand.filter(x => Math.abs(x.getBoundingClientRect().top - rowTop) < 4);
      j = all.indexOf(row.reduce((b, x) => Math.abs(x.getBoundingClientRect().left - r.left) < Math.abs(b.getBoundingClientRect().left - r.left) ? x : b));
    }
  }
  if (all[j]) { e.preventDefault(); all[j].focus(); all[j].scrollIntoView({block: "nearest"}); }
});

/* flat navigation across months (loads neighbours on demand) */
async function at(i, k) {  // one load attempt per month; a month that fails gives null instead of a request loop
  const ready = async j => { if (months[j] && !months[j].ids) await loadMonth(j); return !!(months[j] && months[j].ids); };
  if (!(await ready(i))) return null;
  if (k < 0) { i--; if (!(await ready(i))) return null; k = months[i].ids.length - 1; }
  if (k >= months[i].ids.length) { i++; k = 0; if (!(await ready(i))) return null; }
  return months[i].ids[k] ? {i, k, id: months[i].ids[k]} : null;
}
let pos = null;
let fullSize = false;
let pplFor = null;
async function showPeople(id) {
  pplFor = id; const box = $("vppl"); box.hidden = true; box.innerHTML = "";
  if (!FEAT.faces) return;
  const fc = await api(`/ui/assets/${id}/faces`).catch(() => []);
  if (pplFor !== id) return;
  const seen = new Set(), named = fc.filter(f => f.name && f.personId && !seen.has(f.personId) && seen.add(f.personId));
  box.innerHTML = named.map(f => `<button data-go="${f.personId}" title="All photos of ${esc(f.name)} (long press: more)"><img src="/ui/faces/${f.faceId}/crop" alt="">${esc(f.name)}</button>`).join("");
  box.hidden = !named.length;
  box.querySelectorAll("button[data-go]").forEach(b => {
    let timer = 0, held = false;
    const mine = () => fc.filter(f => f.personId === b.dataset.go);
    const menu = (x, y) => { held = true; chipMenu(x, y, b.dataset.go, mine(), id); };
    let sx = 0, sy = 0;
    b.onpointerdown = e => { e.stopPropagation(); held = false; sx = e.clientX; sy = e.clientY; clearTimeout(timer); timer = setTimeout(() => menu(e.clientX, e.clientY), 550); };
    for (const ev of ["pointerup", "pointerleave", "pointercancel"]) b.addEventListener(ev, () => clearTimeout(timer));
    b.addEventListener("pointermove", e => { if (Math.hypot(e.clientX - sx, e.clientY - sy) > 12) clearTimeout(timer); });   // a finger wobbles a little
    b.oncontextmenu = e => { e.preventDefault(); clearTimeout(timer); menu(e.clientX, e.clientY); };   // right-click, and long press where the browser sends it
    b.onclick = e => { if (held) { held = false; e.stopPropagation(); return; } closeViewer(); go("person/" + b.dataset.go); };
  });
}
/* PH50/PH51: long press (or right-click) on a name under the photo: take the tag off, or make it someone else */
function chipMenu(x, y, personId, faces, assetId) {
  closeCtx();
  const who = faces[0].name, m = document.createElement("div"); m.className = "mnu ctx";
  m.style.cssText = `position:fixed;display:block;z-index:200;left:${Math.max(4, Math.min(x, innerWidth - 240))}px;top:${Math.max(4, Math.min(y - 90, innerHeight - 120))}px`;
  const item = (label, fn) => { const b = document.createElement("button"); b.textContent = label; b.onclick = e => { e.stopPropagation(); closeCtx(); fn(); }; m.appendChild(b); };
  item(`Delete the tag of ${who}`, async () => {
    if (!(await ask(`Take ${who} off this photo?\nThe photo and ${who}'s other photos stay.`))) return;
    try { for (const f of faces) await post(`/ui/faces/${f.faceId}/not-this-person`); } catch (e) { tell(e.message); }
    pplFor = null; showPeople(assetId);
  });
  item("Change the person...", () => pickPerson(async person => {
    try { for (const f of faces) await put(`/ui/faces/${f.faceId}/person`, {personId: person.id}); } catch (e) { tell(e.message); }
    pplFor = null; showPeople(assetId);
  }));
  $("viewer").appendChild(m); ctxEl = m;
}
let tagPick = null;
async function pickPerson(done) {
  tagPeople = (await api("/ui/people").catch(() => [])).filter(p => p.name);
  tagPick = done; $("tagq").value = ""; paintTagList(); $("tagd").showModal(); $("tagq").focus();
}
/* PH49: pick a person who already has a name, then click their face on the photo */
let tagging = null, tagPeople = [];
function endTag() { tagging = null; $("tagbar").hidden = true; $("viewer").classList.remove("tagging"); }
function paintTagList() {
  const q = $("tagq").value.trim().toLowerCase();
  const rows = tagPeople.filter(p => !q || p.name.toLowerCase().includes(q));
  $("taglist").innerHTML = rows.length ? rows.map(p => `<button data-p="${p.id}"><img src="/ui/people/${p.id}/thumbnail" alt="">${esc(p.name)}</button>`).join("") : '<div class="path">Nobody with that name. Name a person in People first.</div>';
  $("taglist").querySelectorAll("button").forEach(b => b.onclick = () => {
    const person = tagPeople.find(p => p.id === b.dataset.p); $("tagd").close(); tagPick(person);
  });
}
function startTag() {
  if (!FEAT.faces || !photoOpen() || !months[pos.i].img[pos.k]) return;
  pickPerson(person => {
    tagging = {id: pos.id, person}; $("viewer").classList.add("tagging");
    $("tagbar").innerHTML = `<span>Click on ${esc(person.name)}'s face (Esc to cancel)</span>`; $("tagbar").hidden = false;
  });
}
$("vtag").onclick = startTag;
$("tagq").oninput = paintTagList;
$("tagno").onclick = () => $("tagd").close();
$("stage").addEventListener("click", async e => {
  const img = $("stage").querySelector("img");
  if (!tagging || !img || e.target !== img) return;
  e.preventDefault(); e.stopPropagation();
  const r = img.getBoundingClientRect(), t = tagging; endTag();
  try { await post(`/ui/assets/${t.id}/faces`, {personId: t.person.id, x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height}); }
  catch (err) { tell(err.message); return; }
  if (pos && pos.id === t.id) { pplFor = null; showPeople(t.id); }
}, true);
$("viewer").addEventListener("cancel", e => { if (tagging) { e.preventDefault(); endTag(); } else if (slides) { e.preventDefault(); stopSlides(); } });
function viewerImg() {
  const p = pos, m = months[p.i];
  if (pplFor !== p.id) showPeople(p.id);
  zoom = null;
  if (!m.img[p.k]) { const v = document.createElement("video"); v.controls = v.autoplay = true; v.playsInline = true; $("stage").replaceChildren(v); playVideo(v, p.id, true); if (slides) v.addEventListener("ended", slideNext, {once: true}); return; }
  const q = fullSize ? "size=full" : "size=preview";
  $("stage").innerHTML = `<img src="/api/assets/${p.id}/thumbnail?${q}&v=${m.rot[p.k]}" alt="${esc(m.names?.[p.k] || "")}">`;
  $("viewer").classList.toggle("full", fullSize); $("vfit").textContent = fullSize ? "Fit" : "Full size";
}
function photoOpen() { return !!(pos && pos.a && ($("viewer").open || pos.ctx)); }  // the open photo, or the one last right-clicked
async function openViewer(i, k) {
  const p = await at(i, k); if (!p) return; pos = p; endTag();
  const id = p.id, m = months[p.i];
  viewerImg();
  const a = await api("/api/assets/" + id).catch(() => null);
  $("vname").textContent = a ? a.originalFileName : ""; pos.a = a;
  clearTimeout(liveTimer);
  const lv = m.live && m.live[p.k]; $("vlive").hidden = !lv;
  if (lv && (liveAuto() || slides)) liveTimer = setTimeout(() => { if (pos === p && $("viewer").open) playLive(true); }, slides ? 300 : LIVE_DELAY_MS);
  const isImg = m.img[p.k]; $("vrl").disabled = $("vrr").disabled = $("vfit").disabled = !isImg;
  $("vfav").innerHTML = m.fav[p.k] ? "&#9733;" : "&#9734;";
  $("vhid").textContent = route === "hidden" ? "Unhide" : "Hide";
  if (!$("viewer").open) $("viewer").showModal();
  if (slides) slideArm();
  const nx = await at(p.i, p.k + 1); if (nx && months[nx.i].img[nx.k]) new Image().src = `/api/assets/${nx.id}/thumbnail?size=preview&v=${months[nx.i].rot[nx.k]}`;
}
/* Play the original first (HEVC included: Safari and most Chrome builds play it). Only if this browser cannot decode
   it, ask the server for an H.264 copy, made in the background, and poll until it is ready. */
function playVideo(v, id, transcode, onFail) {
  v.src = `/api/assets/${id}/original`;
  let done = false;
  // Chrome without HEVC plays such a file as sound only, with no error: no picture after the metadata = cannot decode
  v.addEventListener("loadedmetadata", () => { if (!v.videoWidth && v.isConnected) fallback(); });
  v.addEventListener("error", fallback);
  async function fallback() {
    if (done) return; done = true;
    v.removeEventListener("error", fallback);
    if (!transcode) { if (onFail) onFail(); return; }
    const note = document.createElement("div"); note.className = "vnote"; note.textContent = "Preparing a version this browser can play...";
    v.after(note);
    for (let n = 0; n < 600 && v.isConnected; n++) {
      const s = await api(`/ui/playback/${id}?state=1`).catch(e => ({state: "error", error: e.message}));
      if (s.state === "ready") { note.remove(); v.src = `/ui/playback/${id}`; v.play().catch(() => {}); return; }
      if (s.state === "error") { note.textContent = "This video cannot be played here: " + (s.error || "conversion failed"); if (onFail) onFail(); return; }
      await new Promise(r => setTimeout(r, 2000));
    }
    note.remove();
  }
}
let liveTimer = 0;
const LIVE_DELAY_MS = 3000;
const liveAuto = () => localStorage.photos_liveauto !== "0";   // on unless the user turned it off in the menu (PH48)
/* the motion half of a Live Photo plays once over the still, then gives way to it */
function playLive(auto) {
  if (!photoOpen()) return;
  const lv = months[pos.i].live && months[pos.i].live[pos.k]; const img = $("stage").querySelector("img"); if (!lv || !img) return;
  clearTimeout(liveTimer);
  const v = document.createElement("video"); v.autoplay = true; v.playsInline = true; v.muted = !!auto;
  const deg = months[pos.i].rot[pos.k]; if (deg) v.style.transform = `rotate(${deg}deg)`;
  const back = () => { if (v.isConnected) v.replaceWith(img); };
  v.addEventListener("ended", back);
  img.replaceWith(v);
  playVideo(v, lv, !auto, back);  // auto-play never starts a conversion: stepping through photos must stay cheap
}
$("vlive").onclick = () => playLive(false);
/* PH91: hover a video or Live Photo thumbnail (mouse only) and it plays in place, with sound, looping; leaving stops it.
   Loaded on hover only, after a short pause so sweeping across the grid starts nothing. The browser may refuse sound
   before the first click on the page: then it plays muted. No hover on a touch screen = no preview. */
const canHover = matchMedia("(hover: hover) and (pointer: fine)");
let hoverTile = null, hoverTimer = 0;
function hoverStop() {
  clearTimeout(hoverTimer); hoverTimer = 0;
  if (hoverTile) { const v = hoverTile.querySelector("video.hv"); if (v) { v.pause(); v.removeAttribute("src"); v.load(); v.remove(); } }
  hoverTile = null;
}
function hoverStart(t) {
  const m = months[+t.dataset.i], k = +t.dataset.k; if (!m) return;
  const id = t.dataset.video === "1" ? t.dataset.id : m.live && m.live[k]; if (!id) return;
  const v = document.createElement("video"); v.className = "hv"; v.loop = true; v.playsInline = true; v.preload = "auto";
  const deg = m.rot[k]; if (deg) v.style.transform = `rotate(${deg}deg)`;
  v.addEventListener("error", () => v.remove());   // no conversion for a preview: a video this browser cannot play just stays a still
  v.addEventListener("loadedmetadata", () => { if (!v.videoWidth) v.remove(); });
  t.appendChild(v); playVideo(v, id, false, () => v.remove());
  v.play().catch(() => { v.muted = true; v.play().catch(() => v.remove()); });
}
document.addEventListener("mouseover", e => {
  if (!canHover.matches) return;
  const t = e.target.closest && e.target.closest(".tile[data-id]"); if (t === hoverTile) return;
  hoverStop(); if (!t || !t.closest("#main")) return;
  hoverTile = t; hoverTimer = setTimeout(() => hoverStart(t), 250);
});
document.addEventListener("mouseleave", hoverStop);
document.addEventListener("scroll", hoverStop, true);
async function deleteLiveVideo() {
  if (!photoOpen() || !months[pos.i].live[pos.k]) return;
  if (!(await ask(`Move the video of "${pos.a.originalFileName}" to the trash?\nThe photo stays.`, {ok: "Delete video", danger: true}))) return;
  try { await post(`/ui/assets/${pos.id}/live-video`, {confirm: true}, "DELETE"); months[pos.i].live[pos.k] = null; $("vlive").hidden = true; viewerImg(); const y = $("main").scrollTop; await Promise.all([loadAlbums(), render()]); $("main").scrollTop = y; }
  catch (e) { tell(e.message); }
}
function setFit(full) { fullSize = full; if (photoOpen()) viewerImg(); }
$("vfit").onclick = () => setFit(!fullSize);
async function rotate(dir) {
  if (!photoOpen() || !months[pos.i].img[pos.k]) return;
  const m = months[pos.i];
  try {
    const r = await post("/ui/rotate", {id: pos.id, direction: dir});
    m.rot[pos.k] = r.rotation; viewerImg();
    const t = document.querySelector(`.tile[data-i="${pos.i}"][data-k="${pos.k}"] img`); if (t) t.src = `/api/assets/${pos.id}/thumbnail?v=${r.rotation}`;
    pos.a = await api("/api/assets/" + pos.id).catch(() => pos.a);
  } catch (e) { tell(e.message); }
}
$("vrl").onclick = () => rotate("left"); $("vrr").onclick = () => rotate("right");
function download() { if (!photoOpen()) return; const a = document.createElement("a"); a.href = `/api/assets/${pos.id}/original?download=1`; a.download = pos.a.originalFileName; document.body.appendChild(a); a.click(); a.remove(); }
$("vdown").onclick = download;
async function step(d) { if (!pos) return; const p = await at(pos.i, pos.k + d); if (p) openViewer(p.i, p.k); }
$("prev").onclick = () => step(-1);
$("next").onclick = () => step(1);
$("vclose").onclick = () => { stopSlides(); pplFor = null; $("viewer").close(); $("stage").innerHTML = ""; };
$("viewer").addEventListener("close", () => { stopSlides(); $("stage").innerHTML = ""; if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); });
document.addEventListener("keydown", e => {
  if (!$("viewer").open || document.querySelector("dialog[open]:not(#viewer)") || e.target.closest("input,select,textarea") || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "ArrowLeft") $("prev").click(); if (e.key === "ArrowRight") $("next").click();
  if (e.key === "f" || e.key === "F") $("vfit").click();
  if (e.key === "r") rotate("right"); if (e.key === "R") rotate("left");
  if (e.key === "i" || e.key === "I") $("vinfo").click();
  if (e.key === "t" || e.key === "T") startTag();
  if (e.key === "l" || e.key === "L") $("vlive").click();
  if (e.key === "s" || e.key === "S") slides ? stopSlides() : startSlides();
  if (e.key === " " && slides) { e.preventDefault(); pauseSlides(); }
});
/* PH86 (IMPROVE 3): swipe left/right = next/previous, swipe down = close; pinch or double-tap to zoom, drag to pan */
let zoom = null;
(() => {
  const st = $("stage"), pts = new Map();
  let start = null, pinch = null, lastTap = 0;
  const img = () => st.querySelector("img");
  const apply = () => { const i = img(); if (!i || !zoom) return; i.style.transform = `translate(${zoom.x}px,${zoom.y}px) scale(${zoom.s})`; };
  const unzoom = () => { zoom = null; const i = img(); if (i) { i.classList.remove("zoomed"); i.style.transform = ""; } };
  const zoomAt = (cx, cy, s) => {
    const i = img(); if (!i) return;
    if (!zoom) { const r = i.getBoundingClientRect(); i.classList.add("zoomed"); i.style.width = r.width + "px"; i.style.height = r.height + "px"; zoom = {s: 1, x: 0, y: 0, ox: r.left, oy: r.top}; }
    const ns = Math.min(8, Math.max(1, s));
    zoom.x = (cx - zoom.ox) - ((cx - zoom.ox - zoom.x) * ns / zoom.s); zoom.y = (cy - zoom.oy) - ((cy - zoom.oy - zoom.y) * ns / zoom.s); zoom.s = ns;
    if (ns <= 1.01) { const i2 = img(); if (i2) { i2.style.width = i2.style.height = ""; } unzoom(); } else apply();
  };
  st.addEventListener("pointerdown", e => {
    if (tagging || e.pointerType === "mouse") return;
    pts.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (pts.size === 1) { start = {x: e.clientX, y: e.clientY, t: Date.now(), zx: zoom ? zoom.x : 0, zy: zoom ? zoom.y : 0}; }
    if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = {d: Math.hypot(a.x - b.x, a.y - b.y), s: zoom ? zoom.s : 1}; start = null; }
  });
  st.addEventListener("pointermove", e => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (pinch && pts.size === 2) { const [a, b] = [...pts.values()]; zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, pinch.s * Math.hypot(a.x - b.x, a.y - b.y) / pinch.d); }
    else if (zoom && start) { zoom.x = start.zx + e.clientX - start.x; zoom.y = start.zy + e.clientY - start.y; apply(); }
  });
  const end = e => {
    if (!pts.has(e.pointerId)) return;
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (!start || pts.size) return;
    const dx = e.clientX - start.x, dy = e.clientY - start.y, quick = Date.now() - start.t < 600;
    if (!zoom && quick && Math.abs(dx) > 60 && Math.abs(dx) > 1.5 * Math.abs(dy)) step(dx < 0 ? 1 : -1);
    else if (!zoom && quick && dy > 90 && dy > 1.5 * Math.abs(dx)) $("vclose").click();
    else if (Math.hypot(dx, dy) < 10) {
      if (Date.now() - lastTap < 300) { zoom ? (img() && (img().style.width = img().style.height = ""), unzoom()) : zoomAt(e.clientX, e.clientY, 2.5); lastTap = 0; } else lastTap = Date.now();
    }
    start = null;
  };
  st.addEventListener("pointerup", end); st.addEventListener("pointercancel", end);
})();
$("vfav").onclick = async () => {
  if (!pos) return;
  const m = months[pos.i], k = pos.k, i = pos.i, id = pos.id, on = !m.fav[k];
  try { await post("/ui/favorite", {id, favorite: on}); }  // PH86: a failure (PIN timed out, network) says so
  catch (e) { tell("The favorite could not be saved: " + e.message); return; }
  m.fav[k] = on; if (pos && pos.id === id) $("vfav").innerHTML = on ? "&#9733;" : "&#9734;";
  const t = document.querySelector(`.tile[data-i="${i}"][data-k="${k}"]`); if (t) { const s = t.querySelector(".star"); if (on && !s) t.insertAdjacentHTML("beforeend", '<span class="star">&#9733;</span>'); if (!on && s) s.remove(); }
  loadAlbums();
};
$("vinfo").onclick = async () => {
  if (!pos) return;
  $("infoBody").innerHTML = '<div class="path">Reading...</div>'; $("info").showModal();
  try {
    const [sec, fc] = await Promise.all([api("/ui/metadata/" + pos.id), FEAT.faces ? api(`/ui/assets/${pos.id}/faces`).catch(() => []) : []]);
    const who = fc.length ? '<h3>People in this photo</h3><div class="chips">' + fc.map(f => `<span class="chip"><img src="/ui/faces/${f.faceId}/crop" alt=""><a data-go="${f.personId}">${esc(f.name || "Unnamed")}</a> <button class="link" data-no="${f.faceId}" data-who="${esc(f.name || "")}" data-pid="${f.personId || ""}" title="This is not them">not them</button></span>`).join("") + "</div>" : "";
    const val = (k, v) => k === "Map" && v.startsWith("#/map/") ? `<a href="${esc(v)}" data-map>Show on the map</a>` : esc(v);
    $("infoBody").innerHTML = who + sec.map(s => `<h3>${esc(s.title)}</h3><table class="kv">${s.rows.map(r => `<tr><td>${esc(r[0])}</td><td>${val(r[0], r[1])}</td></tr>`).join("")}</table>`).join("");
    $("infoBody").querySelectorAll("[data-go]").forEach(a => a.onclick = () => { $("info").close(); closeViewer(); go("person/" + a.dataset.go); });
    $("infoBody").querySelectorAll("[data-map]").forEach(a => a.onclick = () => { $("info").close(); closeViewer(); });
    $("infoBody").querySelectorAll("[data-no]").forEach(b => b.onclick = async () => {  // PH86: asks first, then offers Undo
      const who = b.dataset.who, pid = b.dataset.pid;
      if (!(await ask(`Take ${who ? `"${who}"` : "this face"} off this photo?\nThe photo stays; the face goes to a group of its own.`))) return;
      try {
        await post(`/ui/faces/${b.dataset.no}/not-this-person`);
        toast("Face taken off", who && pid ? () => put(`/ui/faces/${b.dataset.no}/person`, {personId: pid}).then(() => { pplFor = null; if (pos) showPeople(pos.id); }) : null);
        pplFor = null; showPeople(pos.id); $("vinfo").click();
      } catch (e) { tell(e.message); }
    });
  } catch (e) { $("infoBody").textContent = e.message; }
};
$("iclose").onclick = () => $("info").close();
$("vdel").onclick = () => { $("cpath").textContent = pos.a ? pos.a.originalPath : pos.id; $("confirm").showModal(); };
$("cno").onclick = () => $("confirm").close();
$("cyes").onclick = async () => {
  $("cyes").disabled = true;
  const {i, k} = pos, y = $("main").scrollTop, wasOpen = $("viewer").open;
  try {
    await post("/ui/assets/" + pos.id, {confirm: true}, "DELETE"); $("confirm").close();
    await Promise.all([loadAlbums(), render()]); $("main").scrollTop = y;
    if (!wasOpen) return;
    await afterRemoved(i, k);
  } catch (e) { tell(e.message); } finally { $("cyes").disabled = false; }
};
/* PH79: the open (or right-clicked) photo becomes the cover of the album being viewed */
async function useAsCover() {
  if (!photoOpen() || !route.startsWith("album/")) return;
  try { await post(`/ui/folders/${route.slice(6)}/cover`, {assetId: pos.id}); toast("Album cover changed"); loadAlbums(); } catch (e) { tell(e.message); }
}

/* hidden photos and the upload album: PIN (PH78: the dialog says which album asks; forgot PIN = the password) */
let hiddenDefaultPin = false, lockLeft = 0;
async function ensureUnlocked(what = "Hidden photos") {
  const st = await api("/api/auth/status").catch(() => ({}));
  hiddenDefaultPin = !!st.pinIsDefault;
  if (st.isElevated) { lockShow(st.secondsLeft); return true; }
  $("pinTitle").textContent = what; $("pinWhy").textContent = `${what} open${what.endsWith("s") ? "" : "s"} with the PIN, for 15 idle minutes.`;
  $("pinForm").hidden = false; $("pinResetForm").hidden = true;
  $("pinIn").value = ""; $("pinErr").textContent = ""; pinPaint(); $("pinDlg").showModal(); $("pinPad").querySelector("button").focus();
  return new Promise(res => { pinDone = res; });
}
let pinDone = null;
/* PH90: number pad + dots; the keyboard's digits, Backspace and Enter work too */
const pinPaint = () => { const n = $("pinIn").value.length; $("pinDots").innerHTML = Array.from({length: Math.max(4, n)}, (_, i) => `<span class="${i < n ? "on" : ""}"></span>`).join(""); };
function pinKey(d) {
  const i = $("pinIn"); $("pinErr").textContent = "";
  if (d === "back") i.value = i.value.slice(0, -1); else if (i.value.length < 12) i.value += d;
  pinPaint();
}
$("pinPad").querySelectorAll("button[data-d]").forEach(b => b.onclick = () => pinKey(b.dataset.d));
$("pinDlg").addEventListener("keydown", e => {
  if ($("pinForm").hidden) return;
  if (/^[0-9]$/.test(e.key)) { e.preventDefault(); pinKey(e.key); }
  else if (e.key === "Backspace") { e.preventDefault(); pinKey("back"); }
  else if (e.key === "Enter") { e.preventDefault(); $("pinForm").requestSubmit(); }
});
const pinFinish = ok => { if ($("pinDlg").open) $("pinDlg").close(); if (pinDone) pinDone(ok); pinDone = null; if (ok) lockTick(); };
$("pinNo").onclick = () => pinFinish(false);
$("pinDlg").addEventListener("cancel", () => { if (pinDone) pinDone(false); pinDone = null; });
$("pinForm").onsubmit = async e => {
  e.preventDefault();
  try { await post("/api/auth/session/unlock", {pinCode: $("pinIn").value}); pinFinish(true); }
  catch (x) { $("pinErr").textContent = x.message; $("pinIn").value = ""; pinPaint(); }
};
$("pinForgot").onclick = () => { $("pinForm").hidden = true; $("pinResetForm").hidden = false; $("prPw").value = $("prNew").value = ""; $("prErr").textContent = ""; $("prPw").focus(); };
$("prBack").onclick = () => { $("pinResetForm").hidden = true; $("pinForm").hidden = false; };
$("pinResetForm").onsubmit = async e => {
  e.preventDefault();
  try {
    await put("/api/auth/pin-code", {password: $("prPw").value, newPinCode: $("prNew").value});
    await post("/api/auth/session/unlock", {pinCode: $("prNew").value});
    $("prPw").value = ""; hiddenDefaultPin = false; pinFinish(true); toast("New PIN set");
  } catch (x) { $("prErr").textContent = x.message; }
};
/* PH78: while Hidden or the upload album is open the header shows the minutes left and a Lock button */
let lockTimer = 0;
async function lockTick() {
  clearTimeout(lockTimer);
  const st = await api("/api/auth/status").catch(() => null);
  lockShow(st && st.isElevated ? st.secondsLeft : 0);
}
function lockShow(secs) {
  lockLeft = secs || 0; clearTimeout(lockTimer);
  if (!lockLeft) { $("lockind").hidden = true; return; }
  $("lockind").hidden = false;
  $("lockind").innerHTML = `<span title="Hidden photos and the upload album are open">&#128275; ${Math.max(1, Math.ceil(lockLeft / 60))} min</span><button id="lockbtn" title="Lock Hidden and the upload album now">Lock</button>`;
  $("lockbtn").onclick = lockHidden;
  lockTimer = setTimeout(lockTick, Math.min(60000, lockLeft * 1000 + 500));
}
async function lockHidden() { await post("/api/auth/session/lock").catch(() => {}); lockShow(0); if (route === "hidden" || isUploadAlbum()) { closeViewer(); go("all"); } }
function changePin() { $("pcCur").value = $("pcNew").value = $("pcNew2").value = ""; $("pcErr").textContent = ""; $("pinChg").showModal(); }
$("pcNo").onclick = () => $("pinChg").close();
$("pinChgForm").onsubmit = async e => {
  e.preventDefault();
  if ($("pcNew").value !== $("pcNew2").value) { $("pcErr").textContent = "The new PINs do not match"; return; }
  try { await put("/api/auth/pin-code", {pinCode: $("pcCur").value, newPinCode: $("pcNew").value}); $("pinChg").close(); hiddenDefaultPin = false; render(); }
  catch (x) { $("pcErr").textContent = x.message; }
};
async function toggleHide() {
  if (!pos || !pos.id) return;
  const unhide = route === "hidden", {i, k} = pos, y = $("main").scrollTop, wasOpen = $("viewer").open;
  try {
    await post("/ui/hide", {id: pos.id, hidden: !unhide});
    await Promise.all([loadAlbums(), render()]); $("main").scrollTop = y;
    if (wasOpen) await afterRemoved(i, k);
  } catch (e) { tell(e.message); }
}
$("vhid").onclick = toggleHide;
async function afterRemoved(i, k) {  // stay in the viewer: the next photo took this one's place; at the very end show the one before
  let p = await at(i, k) || (k > 0 && await at(i, k - 1));
  if (!p && i > 0) { p = await at(i - 1, 0); if (p) p = await at(p.i, months[p.i].ids.length - 1); }
  if (p) openViewer(p.i, p.k); else $("viewer").close();
}

/* PH88 (FEATURE 4): slideshow of the current view, full screen; delay 3/5/10 s, shuffle, Live Photos play, Space pauses */
let slides = null;
const slideDelay = () => +(localStorage.photos_slide || 5);
function startSlides() {
  if (!months.length) { tell("Open a view with photos first."); return; }
  const begin = async () => {
    if (!$("viewer").open) { const p = await at(0, 0); if (!p) return; await openViewer(p.i, p.k); }
    slides = {paused: false, shuffle: localStorage.photos_shuffle === "1", timer: 0};
    $("slide").hidden = false; $("slidedelay").value = String(slideDelay()); paintSlide();
    $("viewer").requestFullscreen?.().catch(() => {});
    slideArm();
  };
  begin();
}
function paintSlide() { if (!slides) return; $("slideshuf").setAttribute("aria-pressed", slides.shuffle); $("slideshuf").textContent = slides.shuffle ? "Shuffle: on" : "Shuffle: off"; $("slidepause").textContent = slides.paused ? "Play" : "Pause"; $("slidest").textContent = slides.paused ? "Paused" : "Slideshow"; }
function slideArm() {
  if (!slides) return; clearTimeout(slides.timer);
  if (slides.paused) return;
  const m = months[pos.i], vid = m && !m.img[pos.k];
  if (vid) return;  // a video advances when it ends (viewerImg)
  const live = m && m.live && m.live[pos.k];
  slides.timer = setTimeout(slideNext, slideDelay() * 1000 + (live ? 3000 : 0));
}
async function slideNext() {
  if (!slides || slides.paused) return;
  let p;
  if (slides.shuffle) {
    const tot = months.reduce((s, m) => s + m.count, 0); let r = Math.floor(Math.random() * tot), i = 0;
    while (i < months.length && r >= months[i].count) { r -= months[i].count; i++; }
    p = await at(i, r);
  } else p = await at(pos.i, pos.k + 1) || await at(0, 0);  // at the end: from the start again
  if (slides && p) openViewer(p.i, p.k);
}
function pauseSlides() { if (!slides) return; slides.paused = !slides.paused; paintSlide(); slides.paused ? clearTimeout(slides.timer) : slideNext(); }
function stopSlides() { if (!slides) return; clearTimeout(slides.timer); slides = null; $("slide").hidden = true; if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); }
$("slidedelay").onchange = e => { localStorage.photos_slide = e.target.value; slideArm(); };
$("slideshuf").onclick = () => { if (!slides) return; slides.shuffle = !slides.shuffle; localStorage.photos_shuffle = slides.shuffle ? "1" : "0"; paintSlide(); };
$("slidepause").onclick = pauseSlides;
$("slidestop").onclick = stopSlides;

async function showQr() {
  try { const r = await post("/ui/qr", {origin: location.origin}); $("qrImg").innerHTML = r.svg; $("qrImg").firstElementChild.style.width = "100%"; $("qrImg").firstElementChild.style.height = "auto"; $("qrSrv").textContent = r.server; $("qrDlg").showModal(); }
  catch (e) { tell(e.message); }
}
$("qrNo").onclick = () => $("qrDlg").close();

/* convert */
const PARAM_ROWS = {quality: "cvq", percent: "cvp", width: "cvw", height: "cvh"};
let cvOps = [];
function cvShow() {
  const o = cvOps.find(x => x.op === $("cvop").value) || {params: []};
  for (const [k, id] of Object.entries(PARAM_ROWS)) $(id).hidden = !o.params.includes(k);
  const vid = o.op && o.op.startsWith("mp4"); $("cvpercent").min = vid ? 10 : 1; $("cvpercent").max = vid ? 100 : 400;
}
$("vconv").onclick = async () => {
  $("cverr").textContent = ""; $("cvprog").textContent = ""; $("cvgo").disabled = false;
  cvOps = await api("/ui/convert/ops?id=" + pos.id).catch(() => []);
  $("cvname").textContent = pos.a ? pos.a.originalPath : "";
  if (!cvOps.length) { tell("No conversion applies to this file type."); return; }
  $("cvop").innerHTML = cvOps.map(o => `<option value="${o.op}">${esc(o.label)}</option>`).join("");
  cvShow(); $("convd").showModal();
};
$("cvop").onchange = cvShow;
$("cvclose").onclick = () => $("convd").close();
$("cvform").onsubmit = async e => {
  e.preventDefault(); $("cverr").textContent = ""; $("cvgo").disabled = true;
  const o = cvOps.find(x => x.op === $("cvop").value), body = {id: pos.id, op: o.op};
  if (o.params.includes("quality")) body.quality = $("cvquality").value;
  if (o.params.includes("percent")) body.percent = $("cvpercent").value;
  if (o.params.includes("width")) { body.width = $("cvwidth").value; body.height = $("cvheight").value; }
  try {
    let r = await api("/ui/convert", {method: "POST", body: JSON.stringify(body)});
    if (r.job) {
      for (;;) {
        const j = await api("/ui/convert/" + r.job);
        $("cvprog").textContent = j.state === "queued" ? "Waiting for another conversion..." : "Converting... " + j.progress + "%";
        if (j.state === "done") { r = j; break; }
        if (j.state === "error") throw new Error(j.error || "Conversion failed");
        await new Promise(res => setTimeout(res, 1000));
      }
    }
    $("cvprog").textContent = "Created " + r.name;
    await loadAlbums(); const y = $("main").scrollTop; await render(); $("main").scrollTop = y;
  } catch (err) { $("cverr").textContent = err.message; }
  finally { $("cvgo").disabled = false; }
};

/* explore: memories (PH85: a wider window, this month in past years), people, places */
const card = (go_, cover, title, sub) => `<div class="xcard" data-go="${go_}" tabindex="0" role="link"><img loading="lazy" src="/api/assets/${cover}/thumbnail" alt=""><div class="xl"><b>${esc(title)}</b><span>${esc(sub)}</span></div></div>`;
async function renderExplore() {
  const gen = renderGen;
  setStatus("Explore");
  const days = +(localStorage.photos_memdays || 3);
  const [ppl, pl, mem, mon, st] = await Promise.all([api("/ui/people").catch(() => []), api("/ui/places").catch(() => []), api("/ui/memories?days=" + days).catch(() => []),
    api("/ui/memories/month").catch(() => []), api("/ui/faces/status").catch(() => null)]);
  if (gen !== renderGen) return;
  const sec = (title, more, body) => `<section class="xsec"><div class="xhead"><h3>${title}</h3>${more || ""}</div>${body}</section>`;
  const scanning = st && st.enabled && st.scanned < st.photos;
  const win = `<label class="path">Around this day: <select id="memdays">${[[3, "± 3 days"], [7, "± 1 week"], [14, "± 2 weeks"]].map(([v, l]) => `<option value="${v}"${v === days ? " selected" : ""}>${l}</option>`).join("")}</select></label>`;
  $("grid").innerHTML =
    sec("Memories", win, mem.length ? '<div class="xrow">' + mem.map(m => card(`memory/${m.yearsAgo}/${days}`, m.cover, yearsAgo(m.yearsAgo), `${m.count} ${m.count === 1 ? "item" : "items"}, ${m.year}`)).join("") + "</div>" : '<div class="path">No photos from around this day in earlier years.</div>') +
    (mon.length ? sec(`${MONTHS[new Date().getMonth()]} in past years`, "", '<div class="xrow">' + mon.map(m => card(`month/${m.yearsAgo}`, m.cover, `${MONTHS[m.month - 1]} ${m.year}`, `${m.count} items, ${yearsAgo(m.yearsAgo)}`)).join("") + "</div>") : "") +
    (FEAT.faces ? sec("People", ppl.length ? '<a class="more" data-go="people">View all</a>' : "", ppl.length ? '<div class="xrow">' + ppl.slice(0, 14).map(p => `<div class="xperson" data-go="person/${p.id}" tabindex="0" role="link"><img loading="lazy" src="/ui/people/${p.id}/thumbnail" alt=""><span>${esc(p.name || "Unnamed")}</span></div>`).join("") + "</div>" : `<div class="path">${scanning ? `Looking for faces (${st.scanned.toLocaleString()} of ${st.photos.toLocaleString()} photos done). People appear as photos are scanned.` : "No people yet."}</div>`) : "") +
    sec("Places", pl.length ? `<span><a class="more" data-go="map">Map</a> &middot; <a class="more" data-go="places">View all ${pl.length}</a></span>` : "", pl.length ? '<div class="xrow">' + pl.slice(0, 30).map(p => card(`place/${encodeURIComponent(p.key)}`, p.cover, p.city, `${p.cc}, ${p.count}`)).join("") + "</div>" : '<div class="path">No places yet: photos with GPS are being read.</div>');
  wireGo();
  $("memdays").onchange = e => { localStorage.photos_memdays = e.target.value; render(); };
}
function wireGo() { $("grid").querySelectorAll("[data-go]").forEach(e => { e.onclick = () => go(e.dataset.go); e.onkeydown = k => { if (k.key === "Enter") go(e.dataset.go); }; }); }
/* PH85: every place, by count or by name */
async function renderPlaces() {
  const gen = renderGen, by = localStorage.photos_placesort || "count";
  const pl = await api("/ui/places").catch(() => []);
  if (gen !== renderGen) return;
  pl.sort((a, b) => by === "name" ? a.city.localeCompare(b.city) : b.count - a.count);
  setStatus(pl.length + " places");
  $("grid").innerHTML = `<div class="pbar"><button id="xback">&larr; Explore</button><b>Places</b><label class="path">Sort <select id="plsort"><option value="count">most photos first</option><option value="name">by name</option></select></label><a class="more" data-go="map">Show on the map</a></div>` +
    (pl.length ? '<div class="tiles" style="--tile:200px">' + pl.map(p => card(`place/${encodeURIComponent(p.key)}`, p.cover, p.city, `${p.cc}, ${p.count}`).replace('class="xcard"', 'class="xcard" style="width:auto"')).join("") + "</div>" : '<div class="empty">No places yet.</div>');
  $("plsort").value = by; $("plsort").onchange = e => { localStorage.photos_placesort = e.target.value; render(); };
  $("xback").onclick = () => go("explore"); wireGo();
}

/* PH77 (FEATURE 3): map of every photo with GPS. Leaflet and a world outline are served by Photos itself: no tiles, nothing leaves the tailnet */
let leafletP = null, map = null;
function loadLeaflet() {
  if (leafletP) return leafletP;
  const css = h => { const l = document.createElement("link"); l.rel = "stylesheet"; l.href = h; document.head.append(l); };
  const js = s => new Promise((ok, no) => { const t = document.createElement("script"); t.src = s; t.onload = ok; t.onerror = no; document.head.append(t); });
  ["/vendor/leaflet.css", "/vendor/MarkerCluster.css", "/vendor/MarkerCluster.Default.css"].forEach(css);
  return leafletP = js("/vendor/leaflet.js").then(() => js("/vendor/leaflet.markercluster.js"));
}
async function renderMap() {
  const gen = renderGen;
  $("grid").innerHTML = '<div class="pbar"><b>Map</b><span class="path" id="mapst">Loading...</span></div><div id="map"></div>';
  try { await loadLeaflet(); } catch (e) { $("mapst").textContent = "The map could not be loaded."; return; }
  const [pts, world] = await Promise.all([api("/ui/map"), fetch("/vendor/world.json").then(r => r.json())]);
  if (gen !== renderGen) return;
  setStatus(pts.id.length.toLocaleString() + " photos with a place");
  $("mapst").textContent = `${pts.id.length.toLocaleString()} photos. Click a group to see its photos.`;
  const L = window.L;
  if (map) { map.remove(); map = null; }
  map = L.map("map", {worldCopyJump: true, minZoom: 2, maxZoom: 18, zoomSnap: 0.5, attributionControl: false});
  const dark = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme:dark)").matches);
  L.geoJSON(world, {interactive: false, style: {color: dark ? "#6b7380" : "#8c8f94", weight: 0.7, fillColor: dark ? "#3a3f47" : "#f2efe9", fillOpacity: 1}}).addTo(map);
  const cl = L.markerClusterGroup({chunkedLoading: true, showCoverageOnHover: false, maxClusterRadius: 50, zoomToBoundsOnClick: false});
  const icon = L.divIcon({className: "", html: '<div class="mapdot"></div>', iconSize: [12, 12]});
  const ms = pts.id.map((id, i) => { const m = L.marker([pts.lat[i], pts.lon[i]], {icon}); m.pid = id; return m; });
  cl.addLayers(ms); map.addLayer(cl);
  const pop = (latlng, ids, b) => {
    const pad = 0.0005, bbox = [b.getSouth() - pad, b.getWest() - pad, b.getNorth() + pad, b.getEast() + pad].map(v => v.toFixed(5)).join(",");
    const div = document.createElement("div"); div.className = "mappop";
    div.innerHTML = `<img src="/api/assets/${ids[0]}/thumbnail" alt=""><b>${ids.length.toLocaleString()} photo${ids.length > 1 ? "s" : ""}</b><br><a href="#/spot/${bbox}">Open ${ids.length > 1 ? "these photos" : "this photo"}</a>`;
    L.popup().setLatLng(latlng).setContent(div).openOn(map);
  };
  cl.on("clusterclick", e => { const kids = e.layer.getAllChildMarkers(); pop(e.layer.getLatLng(), kids.map(m => m.pid), e.layer.getBounds()); });
  cl.on("click", e => pop(e.layer.getLatLng(), [e.layer.pid], L.latLngBounds(e.layer.getLatLng(), e.layer.getLatLng())));
  const c = route.startsWith("map/") ? route.slice(4).split(",").map(Number) : null;
  if (c && c.length === 2 && !c.some(isNaN)) map.setView(c, 13);
  else if (ms.length) map.fitBounds(cl.getBounds(), {maxZoom: 10, padding: [20, 20]}); else map.setView([30, 0], 2);
}

/* PH80 (FEATURE 9): library dashboard */
const bytes = n => { for (const u of ["bytes", "KB", "MB", "GB", "TB"]) { if (n < 1024 || u === "TB") return u === "bytes" ? n + " bytes" : n.toFixed(1) + " " + u; n /= 1024; } };
async function renderStats() {
  const gen = renderGen;
  $("grid").innerHTML = '<div class="path">Counting...</div>';
  const s = await api("/ui/stats").catch(e => ({error: e.message}));
  if (gen !== renderGen) return;
  if (s.error) { $("grid").innerHTML = `<div class="empty">${esc(s.error)}</div>`; return; }
  setStatus("Library dashboard");
  const pct = (d, o) => o ? Math.min(100, Math.round(d * 100 / o)) : 100;
  const W = s.workers, wrow = (name, w, note = "") => `<tr><td>${name}</td><td class="r">${w.done.toLocaleString()} of ${w.of.toLocaleString()}</td><td><div class="bar"><span style="width:${pct(w.done, w.of)}%"></span></div></td><td class="r">${pct(w.done, w.of)}%</td><td class="path">${note}</td></tr>`;
  const SRC = {exif: "EXIF capture date", video: "video creation time", name: "file name", folder: "folder name", file: "file modification time"};
  $("grid").innerHTML = `<div class="stats">
    <h3>Library</h3><table><tr><th>Photos</th><th>Videos</th><th>Size</th><th>Oldest</th><th>Newest</th><th>Last full scan</th><th>Disk free</th></tr>
    <tr><td>${s.total.photos.toLocaleString()}</td><td>${s.total.videos.toLocaleString()}</td><td>${bytes(s.total.bytes)}</td>
    <td>${s.oldest ? esc(fmtDay(s.oldest.taken) + " (" + s.oldest.name + ")") : ""}</td><td>${s.newest ? esc(fmtDay(s.newest.taken) + " (" + s.newest.name + ")") : ""}</td>
    <td>${s.lastScan ? new Date(s.lastScan).toLocaleString() : "not since the start"}${s.watching.on ? " (and watching live)" : s.watching.error ? " (watcher off: " + esc(s.watching.error) + ")" : ""}</td>
    <td>${s.disk ? bytes(s.disk.free) + " of " + bytes(s.disk.total) : "?"}</td></tr></table>
    <h3>Background work</h3><table>${wrow("Thumbnails", W.thumbnails)}${wrow("Faces", W.faces, `${s.people.toLocaleString()} people, ${s.named.toLocaleString()} named`)}${wrow("Smart search", W.search)}${wrow("Places", W.places, `${W.places.withGps.toLocaleString()} with GPS`)}${wrow("Checksums (phone sync)", W.checksums)}</table>
    <h3>Albums</h3><table><tr><th>Album</th><th class="r">Photos</th><th class="r">Videos</th><th class="r">Size</th><th>From</th><th>To</th></tr>${s.albums.map(a => `<tr><td><a href="#/album/${a.id}">${esc(a.name)}</a>${a.upload ? " &#128274;" : ""}</td><td class="r">${a.photos.toLocaleString()}</td><td class="r">${a.videos.toLocaleString()}</td><td class="r">${bytes(a.bytes)}</td><td>${fmtDay(a.first)}</td><td>${fmtDay(a.last)}</td></tr>`).join("")}</table>
    <h3>Formats</h3><table><tr><th>Format</th><th class="r">Files</th><th class="r">Size</th></tr>${s.formats.map(f => `<tr><td>${esc(f.format.toUpperCase())}</td><td class="r">${f.count.toLocaleString()}</td><td class="r">${bytes(f.bytes)}</td></tr>`).join("")}</table>
    <h3>Where the dates come from</h3><table>${Object.entries(s.dates).map(([k, v]) => `<tr><td>${esc(SRC[k] || k)}</td><td class="r">${v.toLocaleString()}</td></tr>`).join("")}</table>
    <p class="path">Album counts include nested albums; All photos counts each photo once and leaves out Hidden and the upload album.</p></div>`;
}

/* people (PH83: keeps the scroll position, filter by name, several suggestions at once, hide the small unnamed groups) */
let peopleTimer = null, showHiddenPeople = false, peopleQ = "";
async function renderPeople(refresh = false) {
  const gen = renderGen;
  const [ppl, st] = await Promise.all([api("/ui/people" + (showHiddenPeople ? "?hidden=1" : "")), api("/ui/faces/status").catch(() => null)]);
  if (gen !== renderGen) return;
  const scanning = st && st.enabled && st.scanned < st.photos;
  setStatus(ppl.length + " people" + (st ? ` | faces found in ${st.scanned.toLocaleString()} of ${st.photos.toLocaleString()} photos` + (scanning ? " (still looking...)" : "") : "") + (st && !st.enabled ? " | face detection is off" : ""));
  if (scanning) peopleTimer = setTimeout(() => route === "people" && renderPeople(true), 15000);
  const y = $("main").scrollTop;
  if (refresh && (y > 0 || document.activeElement.closest("#grid") || $("grid").querySelector(".sg input:checked"))) return;  // PH86: no jump to the top while you work
  const q = peopleQ.toLowerCase(), shown = ppl.filter(p => !q || (p.name || "unnamed").toLowerCase().includes(q));
  $("grid").innerHTML = `<div class="pbar"><input id="pq" type="search" placeholder="Filter by name" aria-label="Filter people by name" value="${esc(peopleQ)}"><button id="phid">${showHiddenPeople ? "Show people" : "Show hidden"}</button>${showHiddenPeople ? "" : '<button id="psmall">Hide small unnamed groups...</button>'}</div>` +
    (shown.length ? '<div class="people">' + shown.map(p => `<div class="pcard"><img data-p="${p.id}" loading="lazy" src="/ui/people/${p.id}/thumbnail" alt="${esc(p.name || "Unnamed")}"><input data-n="${p.id}" value="${esc(p.name)}" placeholder="Add a name" aria-label="Name"><div class="path">${p.count} photo${p.count === 1 ? "" : "s"}</div><button data-h="${p.id}">${showHiddenPeople ? "Show again" : "Don't show more"}</button></div>`).join("") + "</div>"
      : '<div class="empty">' + (q ? "Nobody matches that name." : scanning ? "Looking for faces. People appear here as photos are scanned." : "No people yet. Faces seen in at least 3 photos, or that you name, are listed.") + "</div>");
  $("pq").oninput = e => { peopleQ = e.target.value; const c = e.target.selectionStart; renderPeople().then(() => { $("pq").focus(); $("pq").setSelectionRange(c, c); }); };
  $("phid").onclick = () => { showHiddenPeople = !showHiddenPeople; renderPeople(); };
  if ($("psmall")) $("psmall").onclick = hideSmall;
  $("grid").querySelectorAll("img[data-p]").forEach(i => i.onclick = () => go("person/" + i.dataset.p));
  $("grid").querySelectorAll("button[data-h]").forEach(b => b.onclick = () => { const pr = ppl.find(x => x.id === b.dataset.h); hidePerson(b.dataset.h, pr ? pr.name : "", !showHiddenPeople, () => renderPeople().then(() => { $("main").scrollTop = y; })); });
  $("grid").querySelectorAll("input[data-n]").forEach(i => { i.dataset.old = i.value; i.onchange = () => { i.blur(); renamePerson(i.dataset.n, i.dataset.old, i, null); }; });
  if (!showHiddenPeople && !q) await suggestions(ppl);
  $("main").scrollTop = y;
}
async function hideSmall() {
  const v = await askText("Hide every unnamed person seen in fewer than this many photos (named people are never touched):", "5", {title: "Hide small unnamed groups", ok: "Hide them"});
  if (v === null) return;
  try {
    const r = await post("/ui/people/hide-unnamed", {below: +v || 5, confirm: true});
    toast(`${r.hidden.toLocaleString()} unnamed people hidden`, async () => { await post("/ui/people/show", {ids: r.ids}); renderPeople(); });
    renderPeople();
  } catch (e) { tell(e.message); }
}
async function personBar() {
  const id = route.slice(7), all = await api("/ui/people"), me = all.find(p => p.id === id) || (await api("/ui/people?hidden=1")).find(p => p.id === id);
  const bar = document.createElement("div"); bar.className = "pbar";
  bar.innerHTML = `<button id="pback">&larr; People</button><img class="pface" src="/ui/people/${id}/thumbnail" alt=""><input id="pname" value="${esc(me ? me.name : "")}" placeholder="Add a name" aria-label="Name"><select id="pmerge" aria-label="Merge into"><option value="">Same person as...</option>${all.filter(p => p.id !== id).map(p => `<option value="${p.id}">${esc(p.name || "Unnamed")} (${p.count})</option>`).join("")}</select><button id="phide">Don't show more</button><button id="pcheck">Check faces</button>`;
  $("grid").prepend(bar);
  $("pback").onclick = () => go("people");
  $("pname").dataset.old = $("pname").value;
  $("pname").onchange = e => renamePerson(id, e.target.dataset.old, e.target, null);
  $("pmerge").onchange = async e => { if (!e.target.value) return; if (!(await ask("Merge this person into the selected one? They become one person."))) { e.target.value = ""; return; } try { await post(`/ui/people/${id}/merge`, {into: e.target.value}); go("person/" + e.target.value); } catch (x) { tell(x.message); } };
  $("phide").onclick = () => hidePerson(id, $("pname").value, true, () => go("people"));
  $("pcheck").onclick = () => checkFaces(id, all.filter(p => p.name && p.id !== id));
}

/* PH55 merge suggestions (PH83: tick several, merge them at once), PH56 face check */
async function suggestions(ppl) {
  const sg = await api("/ui/people/suggestions").catch(() => []);
  if (!sg.length || route !== "people" || !$("grid").querySelector(".pbar")) return;
  const box = document.createElement("div"); box.className = "suggest";
  box.innerHTML = `<h3>Same person?</h3><div class="pbar"><button id="sgall">Tick all</button><button id="sgyes" class="primary">Merge ticked</button></div>` + sg.map(x => `<div class="sg" data-s="${x.id}"><input type="checkbox" aria-label="Tick to merge"><img src="/ui/people/${x.id}/thumbnail" alt=""><span>&rarr;</span><img src="/ui/people/${x.intoId}/thumbnail" alt=""><div>Is this ${esc(x.intoName)}?<br><span class="path">${x.count} photos, match ${Math.round(x.score * 100)}%</span></div><button data-y>Yes, same</button><button data-n>No</button></div>`).join("");
  $("grid").querySelector(".pbar").after(box);
  box.querySelectorAll(".sg").forEach((row, i) => {
    const x = sg[i];
    row.querySelector("[data-y]").onclick = async () => { if (!(await ask(`Merge this group into "${x.intoName}"? They become one person.`))) return; try { await post(`/ui/people/${x.id}/merge`, {into: x.intoId}); row.remove(); } catch (e) { tell(e.message); } };
    row.querySelector("[data-n]").onclick = async () => { try { await post(`/ui/people/${x.id}/not-same`, {as: x.intoId}); row.remove(); } catch (e) { tell(e.message); } };
  });
  $("sgall").onclick = () => box.querySelectorAll(".sg input").forEach(c => { c.checked = true; });
  $("sgyes").onclick = async () => {
    const rows = [...box.querySelectorAll(".sg")].filter(r => r.querySelector("input").checked);
    if (!rows.length) { tell("Tick the suggestions to merge first."); return; }
    if (!(await ask(`Merge ${rows.length} group${rows.length > 1 ? "s" : ""} into the people suggested?`))) return;
    let bad = 0;
    for (const r of rows) { const x = sg.find(s => s.id === r.dataset.s); try { await post(`/ui/people/${x.id}/merge`, {into: x.intoId}); r.remove(); } catch (e) { bad++; } }
    toast(`${rows.length - bad} merged` + (bad ? `, ${bad} failed` : ""));
  };
}
async function checkFaces(id, named) {
  const old = $("grid").querySelector(".fcheck"); if (old) { old.remove(); return; }
  const fs = await api(`/ui/people/${id}/faces`);
  const box = document.createElement("div"); box.className = "fcheck";
  box.innerHTML = `<p class="path">The faces least like this person come first. Remove any that are someone else.</p><div class="people">` + fs.map(f => `<div class="pcard" data-f="${f.faceId}"><img src="/ui/faces/${f.faceId}/crop" alt=""><div class="path">match ${Math.round(f.score * 100)}%</div><button data-rm>Not them</button><select data-mv aria-label="Move to"><option value="">Move to...</option>${named.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select></div>`).join("") + "</div>";
  $("grid").querySelector(".pbar").after(box);
  box.querySelectorAll(".pcard").forEach(c => {
    const fid = c.dataset.f;
    c.querySelector("[data-rm]").onclick = async () => { try { await post(`/ui/faces/${fid}/not-this-person`); c.remove(); toast("Face taken off this person"); } catch (e) { tell(e.message); } };
    c.querySelector("[data-mv]").onchange = async e => { if (!e.target.value) return; const who = e.target.selectedOptions[0].text; if (!(await ask(`Give this face to "${who}"?`))) { e.target.value = ""; return; } try { await put(`/ui/faces/${fid}/person`, {personId: e.target.value}); c.remove(); toast(`Moved to "${who}"`); } catch (x) { tell(x.message); e.target.value = ""; } };
  });
}

/* trash (PH74: select, restore all, delete forever, empty with a typed word; a taken name restores under a new one) */
const tpicked = new Set();
let trashItems = [], trashShown = 0;
async function renderTrash() {
  const gen = renderGen;
  trashItems = await api("/ui/trash"); if (gen !== renderGen) return;
  tpicked.clear(); trashShown = 0;
  $("rail").innerHTML = "";
  paintTrashBar();
  if (!trashItems.length) { $("grid").innerHTML = '<div class="empty">The trash is empty.</div>'; return; }
  $("grid").innerHTML = `<div class="pbar" id="tbar"></div><div class="tiles" id="ttiles"></div><div id="tmore"></div>`;
  paintTrashBar(); moreTrash();
  obs = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) moreTrash(); }, {root: $("main"), rootMargin: "800px 0px"});
  obs.observe($("tmore"));
}
function moreTrash() {  // 120 at a time as you scroll, not every thumbnail at once
  const box = $("ttiles"); if (!box || trashShown >= trashItems.length) return;
  const part = trashItems.slice(trashShown, trashShown + 120); trashShown += part.length;
  box.insertAdjacentHTML("beforeend", part.map(x => `<div class="trashitem" data-t="${x.id}"><input type="checkbox" aria-label="Select ${esc(x.name)}"><div class="tile" style="cursor:default" data-video="${x.kind === "VIDEO" ? 1 : 0}"><img loading="lazy" src="/ui/trash/${x.id}/thumbnail" alt=""></div><div class="path" style="margin:4px 0">${esc(x.name)}<br>deleted ${fmtDay(x.deleted)}, ${x.daysLeft} days left</div><button data-r style="width:100%">Restore</button></div>`).join(""));
  box.querySelectorAll(".trashitem:not([data-w])").forEach(el => {
    el.dataset.w = 1; const id = el.dataset.t, it = trashItems.find(x => x.id === id);
    el.querySelector("input").onchange = e => { e.target.checked ? tpicked.add(id) : tpicked.delete(id); el.classList.toggle("sel", e.target.checked); paintTrashBar(); };
    el.querySelector("[data-r]").onclick = () => restoreOne(it);
  });
}
function paintTrashBar() {
  const n = trashItems.length, s = tpicked.size;
  setStatus(n.toLocaleString() + " in trash (each is deleted for good 30 days after it was deleted)");
  const b = $("tbar"); if (!b) return;
  b.innerHTML = s ? `<b>${s} selected</b><button id="trs">Restore selected</button><button id="trd" class="danger">Delete selected forever...</button><button id="trc">Clear</button>`
    : `<button id="tra">Restore all</button><button id="tre" class="danger">Empty trash...</button><button id="trp">Select all</button>`;
  const on = (id, fn) => { if ($(id)) $(id).onclick = fn; };
  on("tra", async () => { if (await ask(`Restore all ${n.toLocaleString()} items to where they were? A name that is taken now gets " (1)" added.`)) trashMany("/ui/trash/restore", {}); });
  on("trs", () => trashMany("/ui/trash/restore", {ids: [...tpicked]}));
  on("trd", async () => { if (await ask(`Delete ${s} item${s > 1 ? "s" : ""} forever? This cannot be undone.`, {ok: "Delete forever", danger: true})) trashMany("/ui/trash/delete", {ids: [...tpicked], confirm: true}); });
  on("tre", async () => { const v = await dialog({title: "Empty trash", msg: `Delete all ${n.toLocaleString()} items forever? This cannot be undone.`, typed: "EMPTY", ok: "Empty trash", danger: true}); if (v !== null) trashMany("/ui/trash/delete", {all: true, confirm: "EMPTY"}); });
  on("trp", () => { trashItems.forEach(x => tpicked.add(x.id)); document.querySelectorAll(".trashitem").forEach(el => { el.classList.add("sel"); el.querySelector("input").checked = true; }); paintTrashBar(); });
  on("trc", () => { tpicked.clear(); document.querySelectorAll(".trashitem").forEach(el => { el.classList.remove("sel"); el.querySelector("input").checked = false; }); paintTrashBar(); });
}
async function trashMany(url, body) {
  try { const r = await post(url, body); toast(r.restored !== undefined ? `${r.restored} restored` + (r.failed ? `, ${r.failed} failed` : "") : `${r.deleted} deleted forever`); } catch (e) { tell(e.message); }
  await loadAlbums(); renderTrash();
}
async function restoreOne(it) {
  try { await post(`/ui/trash/${it.id}/restore`, {}); }
  catch (e) {
    if (e.status !== 409) { tell(e.message); return; }
    const stem = it.name.replace(/(\.[^.]*)?$/, m => " (1)" + m);
    if (!(await ask(`Another file is already called "${it.name}" there. Restore this one as "${stem}" (or the next free name)?`, {ok: "Restore under a new name"}))) return;
    try { const r = await post(`/ui/trash/${it.id}/restore`, {rename: true}); toast(`Restored as "${r.name}"`); } catch (x) { tell(x.message); return; }
  }
  await loadAlbums(); renderTrash();
}

/* folder picker */
let here = null;
async function browse(path) {
  const r = await api("/ui/tree" + (path ? "?path=" + encodeURIComponent(path) : ""));
  here = r; $("here").textContent = r.path; $("up").disabled = !r.parent;
  $("tree").innerHTML = r.dirs.map(d => `<div class="tr"><span class="nm" data-p="${esc(d.path)}">&#128193; ${esc(d.name)}</span>${d.picked ? '<span class="tag">Album</span>' : `<button data-add="${esc(d.path)}">Pick</button>`}</div>`).join("") || '<div class="empty">No subfolders.</div>';
  $("tree").querySelectorAll(".nm").forEach(e => e.onclick = () => browse(e.dataset.p));
  $("tree").querySelectorAll("[data-add]").forEach(b => b.onclick = async () => { b.disabled = true; b.textContent = "Scanning..."; try { await post("/ui/folders", {path: b.dataset.add}); await loadAlbums(); browse(here.path); watchScan(); } catch (e) { tell(e.message); } });
}
/* one poller for scans (PH66: Rescan used to run a second one beside it) */
let scanTimer = null, scanWaiters = [];
function watchScan() {
  return new Promise(res => {
    scanWaiters.push(res);
    if (scanTimer) return;
    const tick = async () => {
      const j = await api("/ui/scan").catch(() => []);
      if (j.length) { setStatus("Scanning " + j.map(x => x.name + " (" + x.done.toLocaleString() + " files)").join(", ") + "..."); scanTimer = setTimeout(tick, 1500); return; }
      scanTimer = null; await loadAlbums(); await refreshScanInfo(); render();
      const w = scanWaiters; scanWaiters = []; w.forEach(f => f());
    };
    scanTimer = -1; setTimeout(tick, 300);
  });
}
async function rescanFolders() {
  const b = $("rescan"); if (b.disabled) return;
  b.disabled = true; b.textContent = "Scanning...";
  try { await post("/ui/rescan"); await new Promise(r => setTimeout(r, 900)); await watchScan(); toast("Rescan finished"); }
  catch (e) { tell(e.message); } finally { b.disabled = false; b.textContent = "Rescan folders"; }
}
$("rescan").onclick = rescanFolders;
function openPicker() { $("viewer").open && $("viewer").close(); $("picker").showModal(); browse(null); }
$("up").onclick = () => here.parent && browse(here.parent);
$("pclose").onclick = () => $("picker").close();
$("menu").onclick = () => $("side").classList.toggle("open");
$("aclose").onclick = () => $("aboutd").close();

/* PH58/PH86: upload from this device into the upload album: 3 at a time, byte progress, Cancel, later drops queue up */
const upQ = []; let upRun = null;
function uploadFiles(files) {
  files = [...files].filter(f => f && f.size !== undefined); if (!files.length) return;
  upQ.push(...files);
  if (upRun) { upRun.total += files.length; upRun.bytes += files.reduce((s, f) => s + f.size, 0); paintUp(); return; }
  upRun = {total: files.length, done: 0, saved: 0, dup: 0, failed: [], bytes: files.reduce((s, f) => s + f.size, 0), sent: 0, live: new Map(), xhrs: new Set(), cancelled: false};
  $("upbar").hidden = false; paintUp();
  const worker = async () => { while (upQ.length && !upRun.cancelled) await sendOne(upQ.shift()); };
  Promise.all([worker(), worker(), worker()]).then(finishUp);
}
function sendOne(f) {
  return new Promise(res => {
    const x = new XMLHttpRequest(), run = upRun;
    run.xhrs.add(x);
    x.open("POST", `/ui/upload?name=${encodeURIComponent(f.name)}&mtime=${f.lastModified || ""}`);
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = e => { run.live.set(x, e.loaded); paintUp(); };
    x.onloadend = () => {
      run.xhrs.delete(x); run.live.delete(x); run.sent += f.size; run.done++;
      let j = {}; try { j = JSON.parse(x.responseText || "{}"); } catch (e) {}
      if (x.status === 401) { run.cancelled = true; showLogin(); }
      else if (x.status === 0) { if (!run.cancelled) run.failed.push(`${f.name}: connection lost`); }
      else if (x.status >= 400) run.failed.push(`${f.name}: ${j.message || x.statusText}`);
      else if (j.status === "duplicate") run.dup++; else run.saved++;
      paintUp(); res();
    };
    x.send(f);
  });
}
function paintUp() {
  const r = upRun; if (!r) return;
  const sent = r.sent + [...r.live.values()].reduce((s, v) => s + v, 0);
  $("uptext").textContent = `Uploading ${Math.min(r.done + 1, r.total)} of ${r.total}`;
  $("upprog").value = r.bytes ? sent / r.bytes : 0; $("upprog").title = `${bytes(sent)} of ${bytes(r.bytes)}`;
}
$("upcancel").onclick = () => { if (!upRun) return; upRun.cancelled = true; upQ.length = 0; upRun.xhrs.forEach(x => x.abort()); };
async function finishUp() {
  const r = upRun; upRun = null; $("upbar").hidden = true; $("upfiles").value = "";
  await loadAlbums(); await render();
  uploadToast(r.saved, r.dup, r.failed, r.cancelled);
}
function uploadToast(saved, dup, failed, cancelled = false) {  // PH86: says where the photos went, with a way there
  const ua = albums.find(a => a.isUploadFolder), name = ua ? ua.albumName : "the upload album";
  const msg = (cancelled ? "Upload cancelled. " : "") + `${saved} uploaded to "${name}"` + (dup ? `, ${dup} already in the library` : "") + (failed.length ? `, ${failed.length} failed` : "") + (saved ? " (it opens with the PIN; uploads stay out of All photos)" : "");
  toast(msg, null, ua && saved ? ["Open album", () => go("album/" + ua.id)] : null);
  if (failed.length) tell("Not uploaded:\n" + failed.join("\n"));
}
/* PH60: drop photos from the computer onto the upload album to upload them; anywhere else the drop never navigates
   the tab away (PH86), it says where to drop instead */
const isUploadAlbum = () => route.startsWith("album/") && !!(albums.find(x => x.id === route.slice(6)) || {}).isUploadFolder;
const hasFiles = e => [...(e.dataTransfer?.types || [])].includes("Files");
["dragenter", "dragover"].forEach(t => document.addEventListener(t, e => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  const ok = isUploadAlbum(); e.dataTransfer.dropEffect = ok ? "copy" : "none"; $("main").classList.toggle("dropping", ok);
}));
$("main").addEventListener("dragleave", e => { if (e.target === $("main") || !$("main").contains(e.relatedTarget)) $("main").classList.remove("dropping"); });
document.addEventListener("drop", e => {
  if (!hasFiles(e)) return;
  e.preventDefault(); $("main").classList.remove("dropping");
  if (isUploadAlbum()) { uploadFiles(e.dataTransfer.files); return; }
  const ua = albums.find(a => a.isUploadFolder);
  toast(`Drop photos in "${ua ? ua.albumName : "the upload album"}" to upload them, or use Upload.`, null, ua ? ["Open it", () => go("album/" + ua.id)] : null);
});
/* PH61 + PH79: album manager: inline rename, cover and dates, nested albums, order, new album, choose the upload album */
async function openAlbums() {
  $("viewer").open && $("viewer").close();
  await loadAlbums(); drawAlbums(); if (!$("albd").open) $("albd").showModal();
}
function drawAlbums() {
  $("alblist").innerHTML = albums.map((x, i) => `<div class="albrow" data-a="${x.id}">${x.albumThumbnailAssetId ? `<img src="/api/assets/${x.albumThumbnailAssetId}/thumbnail" alt="">` : "<img alt=\"\">"}<span class="nm"><input class="rn" value="${esc(x.albumName)}" aria-label="Album name" title="Type a name and press Enter (empty = the folder's own name)"><br><span class="path">${x.assetCount.toLocaleString()} items${x.startDate ? " &middot; " + fmtDay(x.startDate) + " - " + fmtDay(x.endDate) : ""} &middot; ${esc(x.relPath || x.description)}${x.nested && x.nested.length ? ` &middot; contains ${esc(x.nested.join(", "))} (counted in both)` : ""}${x.insideOf && x.insideOf.length ? ` &middot; inside ${esc(x.insideOf.join(", "))}` : ""}</span></span>` +
    `<button class="icon" data-mv="-1" aria-label="Move up"${i ? "" : " disabled"}>&#8593;</button><button class="icon" data-mv="1" aria-label="Move down"${i < albums.length - 1 ? "" : " disabled"}>&#8595;</button>` +
    (x.isUploadFolder ? '<span class="tag">Upload album</span>' : '<button data-up>Use for uploads</button>') + (x.isUploadFolder ? "" : '<button data-rm>Remove</button>') + "</div>").join("") || '<div class="empty">No albums yet.</div>';
  $("alblist").querySelectorAll(".albrow").forEach(r => {
    const id = r.dataset.a, al = albums.find(x => x.id === id), act = async fn => { try { await fn(); await loadAlbums(); drawAlbums(); render(); } catch (e) { tell(e.message); } };
    const rn = r.querySelector(".rn");
    rn.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); rn.blur(); } if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); rn.value = al.albumName; rn.blur(); } };
    rn.onchange = () => { if (rn.value.trim() !== al.albumName) act(() => put(`/ui/folders/${id}`, {name: rn.value.trim()})); };
    r.querySelectorAll("[data-mv]").forEach(b => b.onclick = () => {
      const ids = albums.map(a => a.id), i = ids.indexOf(id), j = i + +b.dataset.mv; [ids[i], ids[j]] = [ids[j], ids[i]];
      act(() => post("/ui/albums/order", {ids}));
    });
    const rm = r.querySelector("[data-rm]"); if (rm) rm.onclick = async () => { if (await ask(`Remove "${al.albumName}" from the list?\nThe folder and its photos stay where they are.`)) act(async () => { await post(`/ui/folders/${id}`, {}, "DELETE"); if (route === "album/" + id) go("all"); }); };
    const up = r.querySelector("[data-up]"); if (up) up.onclick = async () => {
      if (!(await ensureUnlocked(`Moving the upload album to "${al.albumName}"`))) return;
      if (await ask(`Send uploads to "${al.albumName}" from now on? It will need the PIN to open; the current upload album stays as a normal album.`)) act(() => post(`/ui/folders/${id}/upload-folder`, {}));
    };
  });
}
$("albcap").onclick = openAlbums;
$("albclose").onclick = () => $("albd").close();
$("albadd").onclick = () => { $("albd").close(); openPicker(); };
$("albnew").onclick = async () => {
  const n = await askText("Name of the new album (it creates an empty folder in your photo library):", "", {title: "New album", ok: "Create"}); if (!n || !n.trim()) return;
  try { const r = await post("/ui/albums", {name: n}); await loadAlbums(); drawAlbums(); render(); go("album/" + r.id); $("albd").close(); } catch (e) { tell(e.message); }
};
$("upload").onclick = () => $("upfiles").click();
$("upfiles").onchange = e => uploadFiles(e.target.files);

/* PH86: the timeline rail can be dragged to scrub, with a floating month label */
(() => {
  const rail = $("rail"); let tip = null, down = false;
  const pick = y => {
    const as = [...rail.querySelectorAll("a[data-i]")]; if (!as.length) return;
    const r = rail.getBoundingClientRect(), f = Math.min(1, Math.max(0, (y - r.top) / r.height));
    const a = as[Math.min(as.length - 1, Math.floor(f * as.length))];
    const sec = $("m" + a.dataset.i); if (sec) sec.scrollIntoView();
    if (!tip) { tip = document.createElement("div"); tip.className = "railtip"; document.body.append(tip); }
    tip.textContent = a.dataset.tip; tip.style.top = Math.max(r.top, Math.min(y - 14, r.bottom - 28)) + "px";
  };
  rail.addEventListener("pointerdown", e => { if (!rail.querySelector("a")) return; down = true; rail.setPointerCapture(e.pointerId); pick(e.clientY); e.preventDefault(); });
  rail.addEventListener("pointermove", e => { if (down) pick(e.clientY); });
  const up = () => { down = false; if (tip) { tip.remove(); tip = null; } };
  rail.addEventListener("pointerup", up); rail.addEventListener("pointercancel", up);
})();

/* menu bar (File / View / Tools / Help), in the window and inside the viewer; on a phone one ⋯ menu (PH86) */
const closeViewer = () => { stopSlides(); pplFor = null; if ($("viewer").open) $("viewer").close(); };
const view = r => () => { closeViewer(); go(r); };
const MENUS = () => [
  ["File", [
    {l: "Manage albums...", run: openAlbums},
    {l: "Add folder...", run: openPicker},
    {l: "Upload photos...", run: () => $("upfiles").click()},
    {l: "Download", k: "", run: download, on: photoOpen},
    {l: "Delete...", run: () => $("vdel").click(), on: photoOpen},
    {l: "Sign in on another device (QR)...", run: showQr},
    "-", {l: "Sign out", run: logout}]],
  ["View", [
    {l: "All photos", run: view("all")}, {l: "Recently added", run: view("recent")}, {l: "Favorites", run: view("fav")}, {l: "Explore", run: view("explore")}, {l: "Map", run: view("map")},
    ...(FEAT.faces ? [{l: "People", run: view("people")}] : []), {l: "Hidden", run: view("hidden")}, {l: "Trash", run: view("trash")}, {l: "Library dashboard", run: view("stats")},
    "-", {l: "Slideshow", k: "S", run: () => startSlides(), on: () => months.length > 0},
    "-", ...(route.startsWith("search/") ? [{l: "Sort by best match", run: () => { closeViewer(); setSort("match", sortDir); }, chk: () => curSort() === "match"}] : []),
    ...Object.entries(SORT_LABELS).map(([k, l]) => ({l: "Sort by " + l.toLowerCase(), run: () => { closeViewer(); setSort(k, sortDir); }, chk: () => curSort() === k})),
    {l: "Ascending", run: () => { closeViewer(); setSort(curSort(), "asc"); }, chk: () => sortDir === "asc"}, {l: "Descending", run: () => { closeViewer(); setSort(curSort(), "desc"); }, chk: () => sortDir === "desc"},
    "-", {l: "Fit to screen", k: "F", run: () => setFit(false), on: photoOpen, chk: () => !fullSize}, {l: "Full size", k: "F", run: () => setFit(true), on: photoOpen, chk: () => fullSize},
    {l: "Play Live Photos automatically (after 3 s)", run: () => { localStorage.photos_liveauto = liveAuto() ? "0" : "1"; }, chk: liveAuto},
    "-", ...[["Small thumbnails", "100"], ["Medium thumbnails", "160"], ["Large thumbnails", "240"]].map(([l, v]) => ({l, run: () => setSize(v), chk: () => (localStorage.photos_tile || "160") === v})),
    "-", ...[["System theme", ""], ["Light theme", "light"], ["Dark theme", "dark"]].map(([l, v]) => ({l, run: () => setTheme(v), chk: () => (localStorage.photos_theme || "") === v}))]],
  ["Tools", [
    {l: "Select photos", run: toggleSelMode, chk: () => selMode},
    {l: "Rotate left", k: "Shift+R", run: () => rotate("left"), on: () => photoOpen() && months[pos.i].img[pos.k]},
    {l: "Rotate right", k: "R", run: () => rotate("right"), on: () => photoOpen() && months[pos.i].img[pos.k]},
    {l: "Favorite", run: () => $("vfav").click(), on: photoOpen},
    {l: "Hide / Unhide", run: toggleHide, on: photoOpen},
    {l: "Play Live Photo", k: "L", run: () => playLive(false), on: () => photoOpen() && !!(months[pos.i].live && months[pos.i].live[pos.k])},
    {l: "Delete Live Photo video...", run: deleteLiveVideo, on: () => photoOpen() && !!(months[pos.i].live && months[pos.i].live[pos.k])},
    ...(FEAT.faces ? [{l: "Tag person...", k: "T", run: startTag, on: () => photoOpen() && !!months[pos.i].img[pos.k]}] : []),
    {l: "Use as album cover", run: useAsCover, on: () => photoOpen() && route.startsWith("album/")},
    {l: "Info", k: "I", run: () => $("vinfo").click(), on: photoOpen},
    {l: "Convert...", run: () => $("vconv").click(), on: photoOpen},
    ...(photoOpen() ? [] : [{l: "(open a photo or right-click one)", run: () => {}, on: () => false}]),
    "-", {l: "Lock hidden photos", run: lockHidden}, {l: "Change hidden photos PIN...", run: changePin},
    "-", {l: "Rescan folders", run: rescanFolders}, {l: "Filter by format...", run: () => { $("side").classList.add("open"); $("fmt2").focus(); }, on: mobile}]],
  ["Help", [{l: "Keyboard shortcuts", run: () => $("keys").showModal()}, {l: "About", run: () => $("aboutd").showModal()}]],
];
/* the viewer's ⋯ on a phone: the toolbar buttons that do not fit, then the menus */
const VIEWER_EXTRA = () => [["Photo", [
  {l: "Info", run: () => $("vinfo").click()}, ...(FEAT.faces ? [{l: "Tag person...", run: startTag, on: () => photoOpen() && !!months[pos.i].img[pos.k]}] : []),
  {l: "Rotate left", run: () => rotate("left"), on: () => photoOpen() && months[pos.i].img[pos.k]}, {l: "Rotate right", run: () => rotate("right"), on: () => photoOpen() && months[pos.i].img[pos.k]},
  {l: fullSize ? "Fit to screen" : "Full size", run: () => setFit(!fullSize), on: () => photoOpen() && months[pos.i].img[pos.k]},
  {l: "Download", run: download}, {l: route === "hidden" ? "Unhide" : "Hide", run: toggleHide}, {l: "Convert...", run: () => $("vconv").click()},
  {l: "Use as album cover", run: useAsCover, on: () => route.startsWith("album/")}, {l: slides ? "Stop slideshow" : "Slideshow from here", run: () => slides ? stopSlides() : startSlides()}]]];
function mountMenubar(el, mode = "bar") {
  el.innerHTML = ""; const tops = [];
  const closeAll = () => tops.forEach(t => t.classList.remove("open"));
  const groups = () => mode === "viewer" ? [...VIEWER_EXTRA(), ...MENUS()] : MENUS();
  const tabs = mode === "bar" ? MENUS().map(m => m[0]) : ["⋯"];
  tabs.forEach(label => {
    const t = document.createElement("div"); t.className = "mb"; tops.push(t);
    const b = document.createElement("button"); b.textContent = label; b.setAttribute("aria-haspopup", "menu"); if (mode !== "bar") b.setAttribute("aria-label", "Menu");
    const m = document.createElement("div"); m.className = "mnu" + (mode === "bar" ? "" : " wide"); m.setAttribute("role", "menu");
    const fill = () => {
      m.innerHTML = "";
      const list = mode === "bar" ? [[null, groups().find(x => x[0] === label)[1]]] : groups();
      list.forEach(([head, items], gi) => {
        if (head) { if (gi) m.appendChild(document.createElement("hr")); const h = document.createElement("div"); h.className = "head"; h.textContent = head; m.appendChild(h); }
        items.forEach(it => {
          if (it === "-") { m.appendChild(document.createElement("hr")); return; }
          const i = document.createElement("button"); i.setAttribute("role", "menuitem");
          i.innerHTML = `<span>${it.chk ? (it.chk() ? "&#10003; " : "&nbsp;&nbsp;&nbsp; ") : ""}${esc(it.l)}</span><span class="k">${esc(it.k || "")}</span>`;
          i.disabled = it.on ? !it.on() : false;
          i.onclick = e => { e.stopPropagation(); closeAll(); it.run(); };
          m.appendChild(i);
        });
      });
    };
    const open = () => { closeAll(); fill(); t.classList.add("open"); };
    b.onclick = e => { e.stopPropagation(); t.classList.contains("open") ? closeAll() : open(); };
    b.onmouseenter = () => { if (tops.some(x => x.classList.contains("open")) && !t.classList.contains("open")) open(); };
    t.append(b, m); el.appendChild(t);
  });
  document.addEventListener("click", closeAll);
  el.addEventListener("keydown", e => { if (e.key === "Escape" && tops.some(x => x.classList.contains("open"))) { closeAll(); e.preventDefault(); e.stopPropagation(); } });
}
$("kclose").onclick = () => $("keys").close();
mountMenubar($("mbMain")); mountMenubar($("mbView")); mountMenubar($("mbCompact"), "compact"); mountMenubar($("mbVCompact"), "viewer");
(async () => {  // opened from a QR code: #/qr/<one-time code> signs this device in
  const m = location.hash.match(/^#\/qr\/(.+)$/);
  if (m) { try { await api("/api/auth/qr-login", {method: "POST", body: JSON.stringify({code: m[1]})}); } catch (e) { tell("This QR code is no longer valid. Show a new one."); } history.replaceState(null, "", "#/all"); }
  await start(); refreshScanInfo();
})();
