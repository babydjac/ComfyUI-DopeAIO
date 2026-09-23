// DOPE All-In-One Loader — frontend
// LoRA stack (DOM widget, works in classic canvas + Vue "Nodes 2.0"), Civitai thumbnails,
// Grok prompt builder button, API key dialog, and mode-dependent widget visibility.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE = "DopeAIOLoader";
const EXT = "DopeAIO.Loader";
const NSFW_SETTING = "DopeAIO.ThumbnailRating";
const THUMB_SETTING = "DopeAIO.AutoFetchCivitai";
const ROW_H = 34;

// ------------------------------------------------------------------ helpers
const $ = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};

function setting(id, fallback) {
  try {
    const v = app.extensionManager?.setting?.get?.(id) ?? app.ui?.settings?.getSettingValue?.(id);
    return v ?? fallback;
  } catch {
    return fallback;
  }
}

function toast(severity, summary, detail, life = 4000) {
  try {
    app.extensionManager.toast.add({ severity, summary, detail, life });
  } catch {
    console[severity === "error" ? "error" : "log"](`[DopeAIO] ${summary}: ${detail ?? ""}`);
  }
}

async function getJSON(path, opts) {
  const r = await api.fetchApi(path, opts);
  let j = null;
  try { j = await r.json(); } catch { /* non-json */ }
  if (!r.ok || (j && j.ok === false)) throw new Error(j?.error || `${r.status} ${r.statusText}`);
  return j;
}

const postJSON = (path, body) =>
  getJSON(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

const W = (node, name) => node.widgets?.find((w) => w.name === name);

function setWidgetValue(node, name, value) {
  const w = W(node, name);
  if (!w) return;
  w.value = value;
  w.callback?.(value, app.canvas, node);
}

// shrink=false only grows the node (keeps a user's manual resize, e.g. after loading a workflow)
function fitHeight(node, shrink = false) {
  requestAnimationFrame(() => {
    const min = node.computeSize();
    node.setSize([Math.max(node.size[0], min[0]), shrink ? min[1] : Math.max(node.size[1], min[1])]);
    node.setDirtyCanvas?.(true, true);
    app.graph?.setDirtyCanvas?.(true, true);
  });
}

// ------------------------------------------------------------------ styles
const DOPE_CSS = `
.dope-stack{display:flex;flex-direction:column;gap:3px;font:12px/1.3 system-ui,sans-serif;color:var(--input-text,#ddd);
  background:var(--comfy-input-bg,#222);border:1px solid var(--border-color,#444);border-radius:8px;padding:5px;box-sizing:border-box;overflow:hidden}
.dope-head{display:flex;align-items:center;gap:6px;padding:0 2px 3px;border-bottom:1px solid var(--border-color,#444)}
.dope-head .t{font-weight:600;flex:1;opacity:.9}
.dope-btn{cursor:pointer;border:1px solid var(--border-color,#555);background:var(--comfy-menu-bg,#333);color:inherit;border-radius:6px;padding:2px 8px;font:inherit;white-space:nowrap}
.dope-btn:hover{filter:brightness(1.25)}
.dope-btn.primary{background:linear-gradient(135deg,var(--dope-accent,#ff5e3a),color-mix(in srgb,var(--dope-accent,#ff5e3a) 42%,#1a1014));border-color:transparent;color:#fff;font-weight:600;transition:background .5s ease,filter .15s ease}
.dope-row{display:flex;align-items:center;gap:5px;height:${ROW_H - 4}px;padding:0 2px;border-radius:6px}
.dope-row:hover{background:rgba(255,255,255,.04)}
.dope-row.off{opacity:.45}
.dope-row.drag-over{outline:1px dashed #ff5e3a}
.dope-grip{cursor:grab;opacity:.4;user-select:none;width:8px;text-align:center}
.dope-thumb{width:28px;height:28px;border-radius:5px;object-fit:cover;flex:none;background:#0003;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:11px;opacity:.9;overflow:hidden}
.dope-thumb img{width:100%;height:100%;object-fit:cover}
.dope-blur img{filter:blur(6px)}
.dope-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;padding:3px 6px;border-radius:5px;background:#0002}
.dope-name:hover{background:#0004}
.dope-num{width:52px;background:#0003;color:inherit;border:1px solid var(--border-color,#444);border-radius:5px;padding:2px 3px;font:inherit;text-align:center}
.dope-x{cursor:pointer;opacity:.5;padding:0 3px}.dope-x:hover{opacity:1;color:#ff5e3a}
.dope-link{cursor:pointer;opacity:.55;text-decoration:none;color:inherit}.dope-link:hover{opacity:1}
.dope-empty{opacity:.5;text-align:center;padding:6px}
.dope-pop{position:fixed;z-index:10000;background:var(--comfy-menu-bg,#222);color:var(--input-text,#ddd);border:1px solid var(--border-color,#555);
  border-radius:10px;box-shadow:0 12px 40px #000a;font:12px/1.35 system-ui,sans-serif}
.dope-picker{width:560px;height:440px;display:flex;flex-direction:column;overflow:hidden}
.dope-picker input.q{margin:8px;padding:6px 9px;border-radius:7px;border:1px solid var(--border-color,#555);background:var(--comfy-input-bg,#111);color:inherit;font:inherit}
.dope-picker .body{display:flex;flex:1;min-height:0}
.dope-picker .list{flex:1;overflow:auto;padding:0 4px 6px 8px}
.dope-picker .item{display:flex;align-items:center;gap:7px;padding:3px 5px;border-radius:6px;cursor:pointer}
.dope-picker .item.sel,.dope-picker .item:hover{background:color-mix(in srgb,var(--dope-accent,#ff5e3a) 22%,transparent)}
.dope-picker .item .dir{opacity:.45}
.dope-picker .side{width:200px;border-left:1px solid var(--border-color,#444);padding:8px;display:flex;flex-direction:column;gap:6px;overflow:auto}
.dope-picker .side img,.dope-picker .side video{width:100%;border-radius:8px;background:#0003}
.dope-hover{width:300px;padding:8px;pointer-events:none}
.dope-hover img{width:100%;border-radius:8px;display:block}
.dope-hover .words{margin-top:5px;opacity:.85}
.dope-tag{display:inline-block;background:#ff5e3a33;border-radius:4px;padding:0 4px;margin:1px}
.dope-status{font:11px/1.3 system-ui,sans-serif;opacity:.85;padding:2px 4px;white-space:normal;color:var(--input-text,#ccc)}
.dope-tabs{display:flex;gap:4px;padding:8px 8px 0}
.dope-tabs button{flex:1;cursor:pointer;border:1px solid var(--border-color,#555);background:transparent;color:inherit;border-radius:7px;padding:5px 8px;font:inherit;transition:background .4s ease,color .4s ease,border-color .4s ease}
.dope-tabs button.on{background:var(--dope-accent,#ff5e3a);color:#16120f;border-color:transparent;font-weight:650}
.dope-bases{display:flex;gap:4px;flex-wrap:wrap;padding:0 8px}
.dope-bases button{cursor:pointer;border:1px solid var(--border-color,#555);background:transparent;color:inherit;border-radius:99px;padding:2px 8px;font:11px/1.4 system-ui,sans-serif;transition:background .35s ease,color .35s ease,border-color .35s ease}
.dope-bases button.on{background:var(--dope-accent,#ff5e3a);color:#16120f;border-color:transparent}
.dope-card{display:flex;gap:8px;padding:6px;border-radius:8px;align-items:center}
.dope-card:hover{background:rgba(255,255,255,.04)}
.dope-cardpic{position:relative;width:64px;height:64px;flex:none;border-radius:6px;overflow:hidden;background:#0003}
.dope-cardpic img{width:100%;height:100%;object-fit:cover;transition:filter .35s ease}
.dope-cardpic img.blur{filter:blur(8px)}
.dope-cardpic[data-video="1"]::after{content:"▶";position:absolute;right:3px;bottom:2px;font-size:10px;color:#fff;text-shadow:0 0 3px #000}
.dope-sort{background:var(--comfy-input-bg,#111);color:inherit;border:1px solid var(--border-color,#555);border-radius:6px;padding:3px 4px;font:inherit}
.dope-card .meta{flex:1;min-width:0}
.dope-card .meta b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dope-card .sub{opacity:.65;font-size:11px;margin:2px 0}
.dope-style{display:flex;flex-direction:column;gap:4px;padding:5px;border-radius:8px;border:1px solid var(--border-color,#444);background:var(--comfy-input-bg,#222);transition:border-color .5s ease,background .5s ease}
.dope-style .cap{font:11px/1 system-ui,sans-serif;opacity:.6;padding:0 2px}
.dope-style .top{display:flex;align-items:center;justify-content:space-between;min-height:20px}
.dope-nsfw{cursor:pointer;border:1px solid var(--border-color,#555);background:transparent;color:inherit;border-radius:99px;padding:1px 9px;font:600 11px/1.4 system-ui,sans-serif;transition:background .35s ease,color .35s ease,border-color .35s ease}
.dope-nsfw.on{background:#ff2a68;border-color:transparent;color:#fff}
.dope-applied{flex:none;font:600 10px/1 system-ui,sans-serif;padding:3px 5px;border-radius:5px;white-space:nowrap}
.dope-applied.ok{background:#1f6f43;color:#d7ffe6}
.dope-applied.bad{background:#7a1d2b;color:#ffd9de}
.dope-seg{display:flex;gap:3px;flex-wrap:wrap}
.dope-seg button{flex:1;min-width:0;cursor:pointer;border:1px solid var(--border-color,#555);background:transparent;color:inherit;border-radius:6px;padding:5px 3px;font:12px/1.15 system-ui,sans-serif;transition:background .45s ease,color .45s ease,border-color .45s ease,box-shadow .45s ease}
.dope-seg button.on{background:var(--dope-accent,#ff5e3a);color:#16120f;border-color:transparent;font-weight:650;box-shadow:0 0 0 1px color-mix(in srgb,var(--dope-accent,#ff5e3a) 40%,transparent)}
.dope-modal-bg{position:fixed;inset:0;background:#0008;z-index:10001;display:flex;align-items:center;justify-content:center}
.dope-modal{width:440px;padding:16px;display:flex;flex-direction:column;gap:10px;position:relative}
.dope-modal input{width:100%;box-sizing:border-box;padding:6px 8px;border-radius:6px;border:1px solid var(--border-color,#555);background:var(--comfy-input-bg,#111);color:inherit;font:inherit}
.dope-modal .muted{opacity:.6;font-size:11px}
`;
function injectCSS() {
  if (document.getElementById("dope-aio-css")) return;
  document.head.append($("style", { id: "dope-aio-css" }, DOPE_CSS));
}

// ------------------------------------------------------------------ thumbnails
const thumbCache = new Map(); // key -> Promise<{url, blurred}|null>
function thumbFor(name, fetchRemote = true) {
  const rating = setting(NSFW_SETTING, "XXX");
  const key = `${name}|${rating}|${fetchRemote ? 1 : 0}`;
  if (thumbCache.has(key)) return thumbCache.get(key);
  const p = (async () => {
    try {
      const r = await api.fetchApi(`/dope_aio/lora/thumb?file=${encodeURIComponent(name)}&fetch=${fetchRemote ? 1 : 0}&nsfw=${rating}`);
      if (!r.ok) return null;
      const blob = await r.blob();
      return { url: URL.createObjectURL(blob), blurred: r.headers.get("X-Dope-Blurred") === "1" };
    } catch {
      return null;
    }
  })();
  thumbCache.set(key, p);
  // a remote hit also satisfies the local-only lookup
  if (fetchRemote) p.then((v) => v && thumbCache.set(`${name}|${rating}|0`, Promise.resolve(v)));
  return p;
}

const infoCache = new Map();
function infoFor(name, fetchRemote = true) {
  const key = `${name}|${fetchRemote ? 1 : 0}`;
  if (!infoCache.has(key)) {
    infoCache.set(key, getJSON(`/dope_aio/lora/info?file=${encodeURIComponent(name)}&fetch=${fetchRemote ? 1 : 0}`).catch(() => null));
  }
  return infoCache.get(key);
}

function placeholder(name) {
  const base = (name || "?").split(/[\\/]/).pop().replace(/\.[^.]+$/, "");
  return base.replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() || "?";
}

function fillThumb(box, name, fetchRemote) {
  box.replaceChildren(placeholder(name));
  box.classList.remove("dope-blur");
  if (!name) return;
  thumbFor(name, fetchRemote).then((t) => {
    if (!t || box.dataset.name !== name) return;
    box.replaceChildren($("img", { src: t.url, draggable: "false" }));
    box.classList.toggle("dope-blur", t.blurred);
  });
}

// ------------------------------------------------------------------ hover card
let hoverEl = null;
function showHover(anchor, name) {
  hideHover();
  if (!name) return;
  hoverEl = $("div", { class: "dope-pop dope-hover" }, $("div", {}, "Loading Civitai info…"));
  document.body.append(hoverEl);
  const r = anchor.getBoundingClientRect();
  const x = r.right + 310 > innerWidth ? r.left - 310 : r.right + 8;
  Object.assign(hoverEl.style, { left: `${Math.max(4, x)}px`, top: `${Math.max(4, Math.min(r.top - 20, innerHeight - 420))}px` });
  const el = hoverEl;
  Promise.all([thumbFor(name, true), infoFor(name, true)]).then(([t, info]) => {
    if (hoverEl !== el) return;
    const c = info?.civitai;
    const kids = [];
    const ceiling = NSFW_CEIL[setting(NSFW_SETTING, "XXX")] ?? 16;
    const img = c?.images?.find((i) => i.nsfwLevel > 0 && i.nsfwLevel <= ceiling);
    if (img) kids.push($("img", { src: img.thumb.replace(/width=\d+/, "width=450") }));
    else if (t) kids.push($("img", { src: t.url, style: t.blurred ? { filter: "blur(8px)" } : {} }));
    kids.push($("div", { style: { marginTop: "6px", fontWeight: 600 } }, c?.modelName || name.split(/[\\/]/).pop()));
    if (c) {
      kids.push($("div", { style: { opacity: 0.7 } }, [c.versionName, c.baseModel].filter(Boolean).join(" · ")));
      if (c.trainedWords?.length) kids.push($("div", { class: "words" }, "Triggers: ", c.trainedWords.slice(0, 12).map((w) => $("span", { class: "dope-tag" }, w))));
    } else {
      kids.push($("div", { style: { opacity: 0.6 } }, info?.status === "not_found" ? "Not found on Civitai" : "No Civitai info"));
    }
    el.replaceChildren(...kids);
  });
}
function hideHover() {
  hoverEl?.remove();
  hoverEl = null;
}

// ------------------------------------------------------------------ lora picker
let pickerEl = null;
// chip -> Civitai baseModels values (names from civitai.com/api/v1/enums)
const CIVITAI_BASES = [
  ["All", []],
  ["Krea 2", ["Krea 2"]],
  ["MiniMax H3", ["MiniMax H3"]],
  ["Flux.2", ["Flux.2 D", "Flux.2 Klein 9B", "Flux.2 Klein 9B-base", "Flux.2 Klein 4B", "Flux.2 Klein 4B-base"]],
  ["Flux.1", ["Flux.1 D", "Flux.1 S", "Flux.1 Krea"]],
  ["Z-Image", ["ZImageTurbo", "ZImageBase"]],
  ["Qwen", ["Qwen", "Qwen 2", "Qwen 3"]],
  ["Wan", ["Wan Video 2.2 T2V-A14B", "Wan Video 2.2 I2V-A14B", "Wan Video 2.2 TI2V-5B", "Wan Video 14B t2v", "Wan Video 2.5 T2V", "Wan Video 2.7", "Wan Image 2.7", "Wan Video 3.0"]],
  ["SDXL", ["SDXL 1.0"]],
  ["Pony", ["Pony", "Pony V7"]],
  ["Illustrious", ["Illustrious"]],
  ["SD 1.5", ["SD 1.5"]],
];
// prompt family (style buttons) -> default chip in the browser
const FAMILY_CHIP = { krea: "Krea 2", h3: "MiniMax H3", flux: "Flux.2", zimage: "Z-Image" };
const LAST_TAB_KEY = "dope-aio.picker-tab";
const NSFW_CEIL = { PG: 1, PG13: 2, R: 4, X: 8, XXX: 16 };

function tooHot(level) {
  return (level || 0) > (NSFW_CEIL[setting(NSFW_SETTING, "XXX")] ?? 16);
}

function fmtCount(n) {
  return n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : String(n);
}

function fmtMB(kb) {
  if (!kb) return "";
  const mb = kb / 1024;
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

async function downloadCivitai(versionId, filename, sha256, button) {
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Starting…";
  try {
    const start = await postJSON("/dope_aio/civitai/download", { version_id: versionId, filename, sha256 });
    for (;;) {
      await new Promise((r) => setTimeout(r, 700));
      const st = await getJSON(`/dope_aio/civitai/download/${start.job}`);
      if (st.status === "running") {
        const got = (st.bytes || 0) / 1048576;
        const tot = st.total ? ` / ${(st.total / 1048576).toFixed(0)} MB` : " MB";
        button.textContent = `${got.toFixed(0)}${tot}`;
        continue;
      }
      if (st.status === "error") throw new Error(st.error || "download failed");
      for (const k of [...thumbCache.keys()]) if (k.startsWith(`${st.name}|`)) thumbCache.delete(k);
      for (const k of [...infoCache.keys()]) if (k.startsWith(`${st.name}|`)) infoCache.delete(k);
      return st.name;
    }
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function openPicker(anchor, current, onPick, startTab = "local", node = null) {
  closePicker();
  injectCSS();
  let loras = [];
  try {
    loras = (await getJSON("/dope_aio/loras")).loras;
  } catch (e) {
    toast("error", "DopeAIO", `Could not list LoRAs: ${e.message}`);
    return;
  }
  const q = $("input", { class: "q", placeholder: `Search ${loras.length} local LoRAs…  (↑↓ Enter, Esc)` });
  const list = $("div", { class: "list" });
  const side = $("div", { class: "side" }, $("div", { style: { opacity: 0.6 } }, "Hover a LoRA to preview"));
  const localPane = $("div", { style: { display: "flex", flexDirection: "column", flex: "1", minHeight: "0" } }, q, $("div", { class: "body" }, list, side));
  const cq = $("input", { class: "q", placeholder: "Search Civitai LoRAs…  (Enter)" });
  const cList = $("div", { class: "list" });
  const cStatus = $("div", { class: "dope-empty" }, "Search Civitai, or browse what’s popular.");
  const famId = node ? familyFor(W(node, "grok_style")?.value).id : null;
  let chip = FAMILY_CHIP[famId] || "All";
  const nsfwOn = node ? W(node, "grok_nsfw")?.value !== false : true;
  const baseRow = $("div", { class: "dope-bases" }, ...CIVITAI_BASES.map(([label]) => $("button", {
    class: label === chip ? "on" : "",
    type: "button",
    onclick: () => {
      chip = label;
      for (const b of baseRow.querySelectorAll("button")) b.classList.toggle("on", b.textContent === label);
      runCivitai();
    },
  }, label)));
  const sortSel = $("select", { class: "dope-sort", title: "Sort", onchange: () => runCivitai() },
    ...["Most Downloaded", "Highest Rated", "Newest"].map((x) => $("option", { value: x }, x)));
  const cTop = $("div", { style: { display: "flex", alignItems: "center", gap: "6px", paddingRight: "8px" } },
    $("div", { style: { flex: "1" } }, cq), sortSel,
    $("span", { class: `dope-nsfw${nsfwOn ? " on" : ""}`, title: "Follows the node's 🔞 toggle" }, nsfwOn ? "🔞 on" : "SFW"));
  const civPane = $("div", { style: { display: "none", flexDirection: "column", flex: "1", minHeight: "0" } }, cTop, baseRow, cStatus, cList);
  const tabLocal = $("button", { type: "button", class: startTab === "local" ? "on" : "" }, "On this machine");
  const tabCiv = $("button", { type: "button", class: startTab === "civitai" ? "on" : "" }, "Civitai");
  const pop = $("div", { class: "dope-pop dope-picker" }, $("div", { class: "dope-tabs" }, tabLocal, tabCiv), localPane, civPane);
  pop.style.setProperty("--dope-accent", dopeAccent);
  pickerEl = pop;
  document.body.append(pop);
  const r = anchor.getBoundingClientRect();
  Object.assign(pop.style, {
    left: `${Math.max(4, Math.min(r.left, innerWidth - 570))}px`,
    top: `${Math.max(4, Math.min(r.bottom + 4, innerHeight - 450))}px`,
  });

  let items = [];
  let sel = 0;
  let sideToken = 0;
  const preview = (name) => {
    const token = ++sideToken;
    const box = $("div", { class: "dope-thumb", style: { width: "100%", height: "auto", aspectRatio: "1", fontSize: "28px" }, "data-name": name });
    fillThumb(box, name, true);
    const meta = $("div", {}, $("b", {}, name.split(/[\\/]/).pop()));
    side.replaceChildren(box, meta);
    infoFor(name, true).then((info) => {
      if (token !== sideToken) return;
      const c = info?.civitai;
      if (c) {
        meta.append(
          $("div", { style: { opacity: 0.75 } }, c.modelName || ""),
          $("div", { style: { opacity: 0.6 } }, [c.versionName, c.baseModel].filter(Boolean).join(" · ")),
          c.trainedWords?.length ? $("div", {}, c.trainedWords.slice(0, 10).map((w) => $("span", { class: "dope-tag" }, w))) : null,
          c.url ? $("a", { href: c.url, target: "_blank", class: "dope-link" }, "Open on Civitai ↗") : null,
        );
      } else meta.append($("div", { style: { opacity: 0.55 } }, info?.status === "not_found" ? "Not on Civitai" : ""));
    });
  };
  const render = () => {
    const terms = q.value.toLowerCase().split(/\s+/).filter(Boolean);
    const hits = loras.filter((n) => terms.every((t) => n.toLowerCase().includes(t)));
    list.replaceChildren();
    items = hits.slice(0, 400).map((name, i) => {
      const parts = name.split(/[\\/]/);
      const file = parts.pop();
      const thumb = $("div", { class: "dope-thumb", "data-name": name });
      const it = $("div", {
        class: `item${name === current ? " sel" : ""}`,
        onmouseenter: () => { sel = i; mark(); preview(name); },
        onclick: () => { onPick(name); closePicker(); },
      }, thumb, $("div", { style: { minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } },
        parts.length ? $("span", { class: "dir" }, parts.join("/") + "/") : null, file));
      it._name = name;
      list.append(it);
      // local previews only while listing (no hashing / network until hover or pick)
      fillThumb(thumb, name, setting(THUMB_SETTING, false));
      return it;
    });
    if (hits.length > 400) list.append($("div", { class: "dope-empty" }, `+${hits.length - 400} more — refine the search`));
    if (!hits.length) list.append($("div", { class: "dope-empty" }, "No matches"));
    sel = Math.max(0, items.findIndex((it) => it._name === current));
    mark();
  };
  const mark = () => items.forEach((it, i) => it.classList.toggle("sel", i === sel));
  q.addEventListener("input", render);
  q.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "ArrowDown") { sel = Math.min(items.length - 1, sel + 1); mark(); items[sel]?.scrollIntoView({ block: "nearest" }); e.preventDefault(); }
    else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); mark(); items[sel]?.scrollIntoView({ block: "nearest" }); e.preventDefault(); }
    else if (e.key === "Enter" && items[sel]) { onPick(items[sel]._name); closePicker(); }
    else if (e.key === "Escape") closePicker();
  });
  pop.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });
  pop.addEventListener("pointerdown", (e) => e.stopPropagation());

  let civToken = 0;
  let civTimer = 0;
  let civCursor = null;
  const runCivitai = async (append = false) => {
    const token = append ? civToken : ++civToken;
    if (!append) {
      civCursor = null;
      cStatus.textContent = "Searching Civitai…";
      cList.replaceChildren();
    }
    const qs = new URLSearchParams({ q: cq.value, sort: sortSel.value, nsfw: nsfwOn ? "1" : "0" });
    for (const b of (CIVITAI_BASES.find(([l]) => l === chip)?.[1] ?? [])) qs.append("base", b);
    if (append && civCursor) qs.set("cursor", civCursor);
    try {
      const res = await getJSON(`/dope_aio/civitai/search?${qs}`);
      if (token !== civToken) return;
      const items = res.items || [];
      cList.querySelector(".dope-more")?.remove();
      if (!append) cStatus.textContent = items.length ? "" : "No Civitai matches";
      cStatus.style.display = cStatus.textContent ? "" : "none";
      for (const model of items) cList.append(civCard(model));
      civCursor = res.next_cursor || null;
      if (civCursor && items.length) {
        cList.append($("button", {
          class: "dope-btn dope-more", type: "button",
          style: { margin: "6px auto", display: "block" },
          onclick: () => runCivitai(true),
        }, "Load more"));
      }
    } catch (e) {
      if (token !== civToken) return;
      cStatus.style.display = "";
      cStatus.textContent = e.message;
    }
  };
  const civCard = (model) => {
    let ver = model.versions[0];
    const pic = $("img", { alt: "", loading: "lazy" });
    const picBox = $("div", { class: "dope-cardpic" }, pic);
    const sub = $("div", { class: "sub" });
    const words = $("div");
    const btn = $("button", { class: "dope-btn primary", type: "button" });
    const file = () => ver.files?.find((x) => x.primary) || ver.files?.[0];
    const paint = () => {
      const img = ver.images?.[0];
      if (img?.thumb) {
        pic.src = img.thumb;
        pic.classList.toggle("blur", tooHot(img.nsfwLevel));
        picBox.dataset.video = img.type === "video" ? "1" : "";
      } else pic.removeAttribute("src");
      const f = file();
      sub.textContent = [ver.baseModel, ver.name, fmtMB(f?.sizeKB), model.downloads ? `⬇ ${fmtCount(model.downloads)}` : ""].filter(Boolean).join(" · ");
      words.replaceChildren(...(ver.trainedWords || []).slice(0, 4).map((w) => $("span", { class: "dope-tag" }, w)));
      btn.textContent = f?.local ? "✓ Add" : "Download";
      btn.title = f?.local ? `Already on this machine: ${f.local}` : `Download ${f?.name || ""} into models/loras`;
    };
    btn.onclick = async (e) => {
      e.stopPropagation();
      const f = file();
      if (!f?.name) return toast("error", "Civitai", "That version has no .safetensors file");
      if (f.local) { onPick(f.local); closePicker(); return; }
      try {
        const name = await downloadCivitai(ver.id, f.name, f.sha256, btn);
        toast("success", "LoRA downloaded", name, 3000);
        onPick(name);
        closePicker();
      } catch (err) {
        toast("error", "Civitai download failed", err.message, 9000);
        if (/API key|login/i.test(err.message)) openKeyDialog();
      }
    };
    const title = $("b", { title: model.name }, $("a", { href: model.url, target: "_blank", class: "dope-link", style: { opacity: 1 } }, model.name));
    const meta = $("div", { class: "meta" }, title);
    if (model.versions.length > 1) {
      meta.append($("select", {
        class: "dope-sort", style: { maxWidth: "100%", margin: "2px 0" },
        onchange: (e) => { ver = model.versions.find((v) => String(v.id) === e.target.value) || ver; paint(); },
      }, ...model.versions.map((v) => $("option", { value: String(v.id) }, [v.name, v.baseModel].filter(Boolean).join(" · ")))));
    }
    meta.append(sub, words);
    paint();
    return $("div", { class: "dope-card" }, picBox, meta, btn);
  };
  cq.addEventListener("input", () => {
    clearTimeout(civTimer);
    civTimer = setTimeout(() => runCivitai(), 450);
  });
  cq.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") closePicker(); if (e.key === "Enter") { e.preventDefault(); clearTimeout(civTimer); runCivitai(); } });

  const show = (tab) => {
    const civ = tab === "civitai";
    try { localStorage.setItem(LAST_TAB_KEY, tab); } catch { /* private mode */ }
    localPane.style.display = civ ? "none" : "flex";
    civPane.style.display = civ ? "flex" : "none";
    tabLocal.classList.toggle("on", !civ);
    tabCiv.classList.toggle("on", civ);
    if (civ && !cList.childElementCount) runCivitai();
    (civ ? cq : q).focus();
  };
  tabLocal.onclick = () => show("local");
  tabCiv.onclick = () => show("civitai");

  render();
  let remembered = null;
  try { remembered = localStorage.getItem(LAST_TAB_KEY); } catch { /* private mode */ }
  show(startTab === "auto" ? (remembered || (loras.length ? "local" : "civitai")) : startTab);
  setTimeout(() => document.addEventListener("pointerdown", outside, true), 0);
}
function outside(e) {
  if (pickerEl && !pickerEl.contains(e.target)) closePicker();
}
function closePicker() {
  document.removeEventListener("pointerdown", outside, true);
  pickerEl?.remove();
  pickerEl = null;
}

// ------------------------------------------------------------------ lora stack widget
// stats come from the last run: [{name, applied, strength}]
function appliedBadge(node, l) {
  if (!l.on || !l.lora) return null;
  const st = node._dopeLoraStats?.find((s) => s.name === l.lora);
  if (!st) return null;
  if (st.status === "missing")
    return $("span", { class: "dope-applied bad", title: "Last run: this file wasn't found in models/loras, so it was skipped." }, "✕ missing");
  if (st.status === "zero")
    return $("span", { class: "dope-applied", title: "Strength is 0 — skipped." }, "0×");
  return st.applied
    ? $("span", { class: "dope-applied ok", title: `Last run: patched ${st.model_keys ?? st.applied} model + ${st.clip_keys ?? 0} CLIP weights at strength ${st.strength}` }, `✓ ${st.applied}`)
    : $("span", { class: "dope-applied bad", title: "Last run: matched 0 weights, so it did nothing. It was trained for a different base model." }, "✕ 0");
}

function createLoraStack(node, inputName) {
  injectCSS();
  const state = { loras: [], show_clip: false };
  const root = $("div", { class: "dope-stack" });
  // keep the canvas from zooming/dragging while interacting with the stack
  root.addEventListener("wheel", (e) => { if (e.target.closest?.("input")) e.stopPropagation(); }, { passive: true });

  const emit = () => {
    widget.callback?.(widget.value);
    node.setDirtyCanvas?.(true, true);
  };

  const render = () => {
    const n = state.loras.length;
    const on = state.loras.filter((l) => l.on && l.lora).length;
    const head = $("div", { class: "dope-head" },
      $("span", { class: "t" }, `LoRAs ${on}/${n}`),
      n ? $("span", { class: "dope-btn", title: "Toggle all", onclick: () => {
        const all = state.loras.every((l) => l.on);
        state.loras.forEach((l) => (l.on = !all));
        render(); emit();
      } }, "⏻ all") : null,
      $("label", { title: "Separate CLIP strength per LoRA", style: { cursor: "pointer", opacity: 0.8 } },
        $("input", { type: "checkbox", ...(state.show_clip ? { checked: true } : {}), onchange: (e) => { state.show_clip = e.target.checked; render(); emit(); } }),
        " clip"),
      $("span", { class: "dope-btn primary", title: "Search Civitai or pick a LoRA already on this machine", onclick: (e) => openPicker(e.currentTarget, null, (name) => {
        state.loras.push({ on: true, lora: name, strength: 1.0, strength_clip: 1.0 });
        render(); emit(); fitHeight(node);
      }, "auto", node) }, "+ Add LoRA"),
    );
    const rows = state.loras.map((l, idx) => {
      const thumb = $("div", { class: "dope-thumb", "data-name": l.lora || "",
        onmouseenter: (e) => showHover(e.currentTarget, l.lora), onmouseleave: hideHover });
      fillThumb(thumb, l.lora, true);
      const num = (key, title) => $("input", {
        class: "dope-num", type: "number", step: "0.05", min: "-10", max: "10", title,
        value: Number(l[key] ?? 1).toFixed(2),
        onchange: (e) => { l[key] = parseFloat(e.target.value) || 0; e.target.value = l[key].toFixed(2); emit(); },
        onwheel: (e) => {
          e.preventDefault(); e.stopPropagation();
          l[key] = Math.round(((l[key] ?? 1) + (e.deltaY < 0 ? 0.05 : -0.05)) * 100) / 100;
          e.target.value = l[key].toFixed(2); emit();
        },
      });
      const row = $("div", { class: `dope-row${l.on ? "" : " off"}`, draggable: "true" },
        $("span", { class: "dope-grip", title: "Drag to reorder" }, "⋮⋮"),
        $("input", { type: "checkbox", title: "Enable", ...(l.on ? { checked: true } : {}), onchange: (e) => { l.on = e.target.checked; render(); emit(); } }),
        thumb,
        $("div", { class: "dope-name", title: l.lora || "Pick a LoRA",
          onclick: (e) => openPicker(e.currentTarget, l.lora, (name) => { l.lora = name; render(); emit(); }, "auto", node) },
          (l.lora || "— pick —").split(/[\\/]/).pop().replace(/\.safetensors$/i, "")),
        appliedBadge(node, l),
        num("strength", state.show_clip ? "Model strength" : "Strength (model + clip)"),
        state.show_clip ? num("strength_clip", "CLIP strength") : null,
        $("span", { class: "dope-link", title: "Civitai page", onclick: async () => {
          const info = await infoFor(l.lora, true);
          if (info?.civitai?.url) window.open(info.civitai.url, "_blank");
          else toast("info", "DopeAIO", "No Civitai page found for this LoRA");
        } }, "↗"),
        $("span", { class: "dope-x", title: "Remove", onclick: () => { state.loras.splice(idx, 1); render(); emit(); fitHeight(node, true); } }, "✕"),
      );
      row.addEventListener("dragstart", (e) => { e.dataTransfer.setData("text/dope-idx", String(idx)); e.dataTransfer.effectAllowed = "move"; });
      row.addEventListener("dragover", (e) => { e.preventDefault(); row.classList.add("drag-over"); });
      row.addEventListener("dragleave", () => row.classList.remove("drag-over"));
      row.addEventListener("drop", (e) => {
        e.preventDefault();
        const from = parseInt(e.dataTransfer.getData("text/dope-idx"), 10);
        if (Number.isNaN(from) || from === idx) return;
        const [m] = state.loras.splice(from, 1);
        state.loras.splice(idx, 0, m);
        render(); emit();
      });
      return row;
    });
    root.replaceChildren(head, ...(rows.length ? rows : [$("div", { class: "dope-empty" }, "No LoRAs — click + Add LoRA")]));
  };

  node._dopeStack = root;
  node._dopeStackRender = render;
  root.style.setProperty("--dope-accent", dopeAccent);
  const stackValue = () => ({
    loras: state.loras.map((l) => ({
      on: !!l.on, lora: l.lora, strength: Number(l.strength ?? 1),
      ...(state.show_clip ? { strength_clip: Number(l.strength_clip ?? l.strength ?? 1) } : {}),
    })),
    show_clip: state.show_clip,
  });
  const widget = node.addDOMWidget(inputName, "DOPE_LORA_STACK", root, {
    getValue: stackValue,
    setValue: (v) => {
      const arr = Array.isArray(v) ? v : Array.isArray(v?.loras) ? v.loras : [];
      state.loras = arr.filter((x) => x && typeof x === "object").map((x) => ({
        on: x.on !== false, lora: x.lora ?? null, strength: Number(x.strength ?? 1),
        strength_clip: Number(x.strength_clip ?? x.strengthTwo ?? x.strength ?? 1),
      }));
      state.show_clip = !!(v && v.show_clip);
      render();
      fitHeight(node);
    },
    getMinHeight: () => 40 + Math.max(1, state.loras.length) * ROW_H,
    getMaxHeight: () => 40 + Math.max(1, state.loras.length) * ROW_H,
    margin: 6,
    hideOnZoom: false,
  });
  // graphToPrompt prefers serializeValue over the value getter
  widget.serializeValue = stackValue;
  render();
  return widget;
}

// ------------------------------------------------------------------ key dialog
async function openKeyDialog() {
  injectCSS();
  let keys = {};
  try { keys = (await getJSON("/dope_aio/keys")).keys; } catch { /* ignore */ }
  const xai = $("input", { type: "password", placeholder: keys.xai?.set ? `saved (${keys.xai.redacted}, ${keys.xai.source}) — type to replace` : "xai-…", autocomplete: "off" });
  const civ = $("input", { type: "password", placeholder: keys.civitai?.set ? `saved (${keys.civitai.redacted}) — type to replace` : "optional — needed for some downloads + your NSFW browsing settings", autocomplete: "off" });
  const msg = $("div", { class: "muted" }, "Keys are stored server-side in ComfyUI's private user/__dope_aio folder — never in your workflow or image metadata. XAI_API_KEY / CIVITAI_API_KEY env vars also work.");
  const bg = $("div", { class: "dope-modal-bg", onpointerdown: (e) => { if (e.target === bg) bg.remove(); } });
  const save = async (kind, input, clear = false) => {
    const key = clear ? "" : input.value.trim();
    if (!clear && !key) return;
    try {
      const r = await postJSON("/dope_aio/keys", { kind, key });
      if (r.warning) toast("warn", "Key saved, but not used", r.warning, 9000);
      if (r.test_error) toast("warn", "Grok key saved, but test failed", r.test_error, 8000);
      else toast("success", "DopeAIO", clear ? `${kind} key cleared` : `${kind} key saved${r.test ? ` ✓ (${r.test.redacted})` : ""}`);
      input.value = "";
      if (kind === "xai") refreshModels(true);
      bg.remove();
    } catch (e) {
      toast("error", "DopeAIO", e.message);
    }
  };
  bg.append($("div", { class: "dope-pop dope-modal" },
    $("b", { style: { fontSize: "14px" } }, "🔑 DOPE AIO — API keys"),
    $("div", {}, "xAI (Grok) API key ", $("a", { href: "https://console.x.ai", target: "_blank", class: "dope-link" }, "console.x.ai ↗")), xai,
    $("div", { style: { display: "flex", gap: "6px" } },
      $("span", { class: "dope-btn primary", onclick: () => save("xai", xai) }, "Save & test"),
      keys.xai?.source === "saved" ? $("span", { class: "dope-btn", onclick: () => save("xai", xai, true) }, "Clear") : null),
    $("div", {}, "Civitai API key ", $("a", { href: "https://civitai.com/user/account", target: "_blank", class: "dope-link" }, "civitai.com ↗")), civ,
    $("div", { style: { display: "flex", gap: "6px" } },
      $("span", { class: "dope-btn", onclick: () => save("civitai", civ) }, "Save"),
      keys.civitai?.source === "saved" ? $("span", { class: "dope-btn", onclick: () => save("civitai", civ, true) }, "Clear") : null),
    msg,
  ));
  document.body.append(bg);
  xai.focus();
}

// ------------------------------------------------------------------ grok models
let modelsPromise = null;
function refreshModels(force = false) {
  if (!modelsPromise || force) {
    modelsPromise = getJSON(`/dope_aio/grok/models${force ? "?refresh=1" : ""}`).catch(() => null);
    modelsPromise.then((res) => {
      if (!res?.models?.length) return;
      const ids = res.models.map((m) => m.id);
      for (const node of app.graph?._nodes ?? []) if (node.comfyClass === NODE) applyModels(node, ids);
    });
  }
  return modelsPromise;
}
function applyModels(node, ids) {
  const w = W(node, "grok_model");
  if (!w || !ids?.length) return;
  const cur = w.value;
  w.options.values = ids.includes(cur) ? ids : [cur, ...ids];
}

// ------------------------------------------------------------------ grok generate
async function grokGenerate(node, button) {
  const v = (n) => W(node, n)?.value;
  const idea = (v("grok_idea") || "").trim();
  if (!idea) {
    toast("warn", "Grok", "Type a rough idea in grok_idea first");
    return;
  }
  const stack = W(node, "loras")?.value?.loras ?? [];
  const label = button.label ?? button.name;
  button.label = "⏳ Grok is writing…";
  node.setDirtyCanvas?.(true, true);
  const t0 = performance.now();
  try {
    const res = await postJSON("/dope_aio/grok/generate", {
      idea, style: v("grok_style"), model: v("grok_model"), effort: v("grok_effort"), detail: v("grok_detail"),
      instructions: v("grok_instructions"), negative: !!v("negative_enabled"),
      width: v("width"), height: v("height"), length: v("length"),
      lora_triggers: !!v("append_lora_triggers"),
      nsfw: v("grok_nsfw") !== false,
      loras: stack.filter((l) => l.on && l.lora).map((l) => l.lora),
    });
    if (res.positive) setWidgetValue(node, "positive", res.positive);
    if (v("negative_enabled") && res.negative) setWidgetValue(node, "negative", res.negative);
    const secs = ((performance.now() - t0) / 1000).toFixed(1);
    setStatus(node, `✨ ${res.model} · ${res.style} · ${secs}s${res.cost_usd ? ` · $${res.cost_usd.toFixed(4)}` : ""}`);
    toast("success", "Grok prompt ready", `${res.style} · ${res.model} · ${secs}s`, 2500);
  } catch (e) {
    toast("error", "Grok failed", e.message, 9000);
    if (/key/i.test(e.message)) openKeyDialog();
  } finally {
    button.label = label;
    node.setDirtyCanvas?.(true, true);
  }
}

// ------------------------------------------------------------------ visibility
function updateVisibility(node, shrink = true) {
  const v = (n) => W(node, n)?.value;
  const hide = (name, hidden) => {
    const w = W(node, name);
    if (!w || !!w.hidden === !!hidden) return false;
    w.hidden = hidden;
    for (const lw of w.linkedWidgets ?? []) lw.hidden = hidden;
    return true;
  };
  const ckptMode = v("model_source") === "checkpoint";
  const clip = v("clip_source");
  const needCkpt = ckptMode || clip === "checkpoint" || v("vae_name") === "(checkpoint VAE)";
  let changed = false;
  changed |= hide("ckpt_name", !needCkpt);
  changed |= hide("unet_name", ckptMode);
  changed |= hide("weight_dtype", ckptMode);
  changed |= hide("clip_name1", clip === "checkpoint");
  changed |= hide("clip_type", clip !== "single");
  changed |= hide("clip_name2", clip !== "dual");
  changed |= hide("dual_clip_type", clip !== "dual");
  changed |= hide("negative", !v("negative_enabled"));
  if (changed) fitHeight(node, shrink);
}

function setStatus(node, text, grokText = "") {
  const el = node._dopeStatus;
  if (!el) return;
  el.replaceChildren(text);
  if (grokText) {
    el.append(" · ", $("a", {
      class: "dope-link", style: { opacity: 1, textDecoration: "underline" },
      title: `Auto-Grok prompt used for this run (click to copy it into the positive box and switch to manual):\n\n${grokText}`,
      onclick: () => {
        setWidgetValue(node, "positive", grokText);
        setWidgetValue(node, "grok_auto", false);
        toast("success", "DopeAIO", "Grok prompt copied into positive — auto mode off", 2500);
      },
    }, "📋 use Grok prompt"));
  }
}

function applyResolution(node) {
  const r = W(node, "resolution")?.value;
  const m = /^(\d+)x(\d+)/.exec(r || "");
  if (!m) return;
  const w = W(node, "width"), h = W(node, "height");
  if (w) w.value = parseInt(m[1], 10);
  if (h) h.value = parseInt(m[2], 10);
  node.setDirtyCanvas?.(true, true);
}

const PROMPT_FAMILIES = [
  { id: "krea", label: "Krea 2", accent: "#ff5e3a", title: "#3a2018", body: "#1c1412",
    variants: [["Turbo", "Krea 2 (Turbo)"], ["Raw", "Krea 2 (Raw)"]] },
  { id: "h3", label: "MiniMax H3", accent: "#7c6bff", title: "#1a1840", body: "#12121f",
    variants: [["video+audio", "MiniMax H3 (video+audio)"], ["director brief", "MiniMax H3 (director brief)"]] },
  { id: "flux", label: "Flux", accent: "#3ddc84", title: "#10241c", body: "#101614",
    variants: [["Flux.2", "Flux.2"], ["Klein", "Flux.2 Klein"], ["Flux.1", "Flux.1 (dev/schnell)"], ["Krea", "Flux.1 Krea [dev]"]] },
  { id: "zimage", label: "Z-Image", accent: "#f0b429", title: "#2a220f", body: "#17140e",
    variants: [["Turbo", "Z-Image Turbo"], ["Base", "Z-Image Base"]] },
];
let dopeAccent = PROMPT_FAMILIES[0].accent;
const colorAnims = new WeakMap();

function hexToRgb(hex) {
  const m = /^#([0-9a-f]{6})$/i.exec(hex || "");
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
function rgbToHex(c) {
  return "#" + c.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, "0")).join("");
}
function mixRgb(a, b, t) {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}
function familyFor(label) {
  return PROMPT_FAMILIES.find((f) => f.variants.some((v) => v[1] === label)) || PROMPT_FAMILIES[0];
}
function setAccent(node, accent) {
  dopeAccent = accent;
  node._dopeStyle?.style.setProperty("--dope-accent", accent);
  node._dopeStack?.style.setProperty("--dope-accent", accent);
  pickerEl?.style.setProperty("--dope-accent", accent);
}
// node.color / node.bgcolor are tracked state: the classic canvas and Vue nodes both repaint from them
function paintNode(node, label, animate) {
  try {
    const fam = familyFor(label);
    const prev = colorAnims.get(node);
    if (prev) cancelAnimationFrame(prev);
    setAccent(node, fam.accent);
    const apply = (title, body) => {
      node.color = title;
      node.bgcolor = body;
      node.setDirtyCanvas?.(true, true);
    };
    const fromT = hexToRgb(node.color);
    const fromB = hexToRgb(node.bgcolor);
    const toT = hexToRgb(fam.title);
    const toB = hexToRgb(fam.body);
    if (!animate || !fromT || !fromB || !toT || !toB) {
      apply(fam.title, fam.body);
      colorAnims.delete(node);
      return;
    }
    const t0 = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - t0) / 500);
      const e = 1 - (1 - t) ** 3;
      apply(rgbToHex(mixRgb(fromT, toT, e)), rgbToHex(mixRgb(fromB, toB, e)));
      if (t < 1) colorAnims.set(node, requestAnimationFrame(step));
      else colorAnims.delete(node);
    };
    colorAnims.set(node, requestAnimationFrame(step));
  } catch (e) {
    console.warn("[DopeAIO] node color", e);
  }
}

function createStyleControl(node) {
  injectCSS();
  const root = $("div", { class: "dope-style" });
  const top = $("div", { class: "top" });
  const famRow = $("div", { class: "dope-seg" });
  const varRow = $("div", { class: "dope-seg" });
  root.append(top, famRow, varRow);
  node._dopeStyle = root;

  const render = (animate) => {
    const label = W(node, "grok_style")?.value || PROMPT_FAMILIES[0].variants[0][1];
    const fam = familyFor(label);
    const nsfwW = W(node, "grok_nsfw");
    const nsfwOn = nsfwW?.value !== false;
    top.replaceChildren(
      $("span", { class: "cap" }, "Prompt model"),
      nsfwW ? $("button", {
        type: "button",
        class: `dope-nsfw${nsfwOn ? " on" : ""}`,
        title: nsfwOn ? "Grok writes explicit adult (21+) prompts. Click for SFW." : "Grok keeps prompts non-explicit. Click for NSFW.",
        onclick: () => setWidgetValue(node, "grok_nsfw", !nsfwOn),
      }, nsfwOn ? "🔞 NSFW" : "SFW") : null,
    );
    famRow.replaceChildren(...PROMPT_FAMILIES.map((f) => $("button", {
      type: "button",
      class: f.id === fam.id ? "on" : "",
      onclick: () => {
        if (f.id === fam.id) return;
        const idx = Math.max(0, fam.variants.findIndex((v) => v[1] === label));
        choose(f.variants[Math.min(idx, f.variants.length - 1)][1]);
      },
    }, f.label)));
    varRow.replaceChildren(...fam.variants.map(([short, full]) => $("button", {
      type: "button",
      class: full === label ? "on" : "",
      onclick: () => { if (full !== label) choose(full); },
    }, short)));
    paintNode(node, label, !!(animate && node._dopeStyleAnim));
  };
  const choose = (label) => {
    node._dopeStyleAnim = true;
    setWidgetValue(node, "grok_style", label);
    node._dopeStyleAnim = false;
  };
  node._dopeRenderStyle = render;

  const widget = node.addDOMWidget("dope_style", "dope_style", root, {
    getValue: () => "",
    setValue: () => {},
    getMinHeight: () => 96,
    getMaxHeight: () => 96,
    serialize: false,
    margin: 4,
    hideOnZoom: false,
  });
  widget.serialize = false;
  return widget;
}

function placeBefore(node, widget, beforeName) {
  const ws = node.widgets;
  const from = ws.indexOf(widget);
  const to = ws.findIndex((w) => w.name === beforeName);
  if (from < 0 || to < 0 || from === to) return;
  ws.splice(from, 1);
  ws.splice(ws.findIndex((w) => w.name === beforeName), 0, widget);
}

function moveWidget(node, widget, afterName) {
  const ws = node.widgets;
  const from = ws.indexOf(widget);
  if (from < 0) return;
  ws.splice(from, 1);
  let at = ws.findIndex((w) => w.name === afterName);
  if (at < 0) { ws.push(widget); return; }
  // skip linked widgets (e.g. control_after_generate) that belong to the anchor
  const anchor = ws[at];
  while (ws[at + 1] && anchor.linkedWidgets?.includes(ws[at + 1])) at++;
  ws.splice(at + 1, 0, widget);
}

// ------------------------------------------------------------------ extension
app.registerExtension({
  name: EXT,
  settings: [
    {
      id: NSFW_SETTING, category: ["DOPE AIO", "Thumbnails", "Rating"], name: "Max Civitai thumbnail rating (higher ratings are blurred)",
      type: "combo", options: ["PG", "PG13", "R", "X", "XXX"], defaultValue: "XXX",
      onChange: () => thumbCache.clear(),
    },
    {
      id: THUMB_SETTING, category: ["DOPE AIO", "Thumbnails", "Picker"], name: "Fetch Civitai thumbnails for every LoRA in the picker list (hashes files — slow first time)",
      type: "boolean", defaultValue: false,
    },
  ],

  getCustomWidgets() {
    return {
      DOPE_LORA_STACK(node, inputName) {
        return { widget: createLoraStack(node, inputName), minHeight: 40 + ROW_H };
      },
    };
  },

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE) return;
    injectCSS();

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated?.apply(this, arguments);
      const node = this;

      const styleUi = createStyleControl(node);
      for (const [name, animate] of [["grok_style", true], ["grok_nsfw", false]]) {
        const w = W(node, name);
        if (!w) continue;
        w.hidden = true;
        const cb = w.callback;
        w.callback = function () {
          const out = cb?.apply(this, arguments);
          node._dopeRenderStyle?.(animate);
          return out;
        };
      }
      placeBefore(node, styleUi, "grok_style");

      // buttons
      const gen = node.addWidget("button", "✨ Generate prompt with Grok", null, () => grokGenerate(node, gen), { serialize: false });
      gen.serialize = false;
      const keys = node.addWidget("button", "🔑 API keys / refresh Grok models", null, () => openKeyDialog(), { serialize: false });
      keys.serialize = false;

      // status line
      const status = $("div", { class: "dope-status" }, "Ready");
      node._dopeStatus = status;
      const sw = node.addDOMWidget("dope_status", "dope_status", status, {
        getValue: () => "", setValue: () => {}, getMinHeight: () => 24, getMaxHeight: () => 24, margin: 2, serialize: false, hideOnZoom: true,
      });
      sw.serialize = false;

      // layout: loras after VAE, buttons after grok_seed(+control), status at the very end
      const audioVae = W(node, "audio_vae_name");
      if (audioVae) moveWidget(node, audioVae, "vae_name");
      const stack = W(node, "loras");
      if (stack) moveWidget(node, stack, audioVae ? "audio_vae_name" : "vae_name");
      moveWidget(node, gen, "grok_seed");
      moveWidget(node, keys, gen.name);

      // grok seed defaults to fixed (a new Grok call only when you change it)
      const seed = W(node, "grok_seed");
      for (const lw of seed?.linkedWidgets ?? []) if (lw.name === "control_after_generate") lw.value = "fixed";

      // react to mode toggles
      for (const name of ["model_source", "clip_source", "vae_name", "negative_enabled"]) {
        const w = W(node, name);
        if (!w) continue;
        const cb = w.callback;
        w.callback = function () {
          const res = cb?.apply(this, arguments);
          updateVisibility(node);
          return res;
        };
      }
      const res = W(node, "resolution");
      if (res) {
        const cb = res.callback;
        res.callback = function () {
          const out = cb?.apply(this, arguments);
          applyResolution(node);
          return out;
        };
      }

      refreshModels().then((m) => m?.models && applyModels(node, m.models.map((x) => x.id)));
      updateVisibility(node);
      node._dopeStyleAnim = false;
      node._dopeRenderStyle?.(false);
      fitHeight(node);
      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = onConfigure?.apply(this, arguments);
      requestAnimationFrame(() => {
        updateVisibility(this, false);
        for (const name of ["grok_style", "grok_nsfw"]) {
          const w = W(this, name);
          if (w) w.hidden = true;
        }
        this._dopeStyleAnim = false;
        this._dopeRenderStyle?.(false);
      });
      return r;
    };

    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (out) {
      const r = onExecuted?.apply(this, arguments);
      if (out?.dope_status?.[0]) setStatus(this, out.dope_status[0], out?.dope_grok?.[0] || "");
      if (Array.isArray(out?.dope_loras)) {
        this._dopeLoraStats = out.dope_loras;
        this._dopeStackRender?.();
      }
      return r;
    };

    const onRemoved = nodeType.prototype.onRemoved;
    nodeType.prototype.onRemoved = function () {
      hideHover();
      closePicker();
      const anim = colorAnims.get(this);
      if (anim) cancelAnimationFrame(anim);
      return onRemoved?.apply(this, arguments);
    };
  },
});
