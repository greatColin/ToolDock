"use strict";
/* 仪表盘 · Mac 风格桌面（白主题 · 自由网格）
 * 数据：GET /api/config（条目 {id?, name, span:[w,h], col, row}，锚点 1 起；可缺省=自由位）、
 *       GET /api/state、SSE /api/events、POST /api/modules/{name}/start|stop；
 *       布局改动乐观 PUT /api/config（写入前用 first-fit 补齐缺省锚点），失败回拉还原；
 *       成功后回拉合并后端生成的 id（不重建卡片）。PUT 的 key 顺序=标签页顺序。
 * 桌面：每标签一枚圆形图标（双击开窗，右键 打开/重命名/卸载）＋「新建标签页」；
 *       进页默认不开窗，窗口仅来自本会话双击图标。
 * 编辑模式：右上 iOS 滑块开关（会话态）；编辑态卡片全卡遮罩可自由拖动落锚（跟随网格、
 *           重叠先滑到最近可容位、找不到就近叠放且被拖卡置顶）、边缘 4px 手柄拖到哪 span 到哪
 *           （盖到邻居有 ghost 预告）、桌面图标可拖动重排序；浏览态（默认)无遮罩、
 *           iframe 可交互、不可拖/缩；右键菜单两态均可用。
 * 网格：24 列，cell 50px + gap 8px → STEP 58px；卡片定位 grid-column: col / span W；行数随内容扩展
 */
const STEP = 58, MIN = 4, MAX_W = 24, MAX_H = 16, DEF = [6, 5], MAX_SCAN_ROWS = 64;

const $ = id => document.getElementById(id);
const icons = $("icons"), addIcon = $("addIcon"), editSwitch = $("editSwitch"), editLabel = $("editLabel");
const menu = $("menu"), library = $("library"), libList = $("libList"), libClose = $("libClose");
const dragMask = $("dragMask");
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

const palette = [
  "linear-gradient(135deg,#60a5fa,#6366f1)",
  "linear-gradient(135deg,#34d399,#059669)",
  "linear-gradient(135deg,#fbbf24,#f59e0b)",
  "linear-gradient(135deg,#f472b6,#ec4899)",
  "linear-gradient(135deg,#a78bfa,#7c3aed)",
  "linear-gradient(135deg,#22d3ee,#0891b2)",
  "linear-gradient(135deg,#fb923c,#ea580c)",
  "linear-gradient(135deg,#f87171,#dc2626)",
];
const iconColor = name => palette[[...name].reduce((a, c) => a + c.charCodeAt(0), 0) % palette.length];

let tabs = {};         // 标签名 -> [{id?, name, span:[w,h], col, row}]；锚点可由前端 first-fit 补齐
let states = {};       // 模块名 -> 状态（同模块多份共享）
const winMap = new Map(); // 标签名 -> win {tab, el, body, grid, cards:Map(entry->卡片), max}
let zTop = 1;
let libWin = null;
let dropGuard = 0;
let editMode = false;

init().catch(e => alert("初始化失败：" + e.message));

async function init() {
  const [cfg, st] = await Promise.all([fetch("/api/config"), fetch("/api/state")]);
  if (!cfg.ok || !st.ok) throw new Error("HTTP " + (cfg.status || st.status));
  tabs = (await cfg.json()).tabs;
  for (const [name, m] of Object.entries((await st.json()).modules)) states[name] = m.state;
  renderIcons();
  startSSE(); // 默认不开窗：窗口仅由双击图标打开
}

/* ---- 状态订阅 ---- */
function startSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.snapshot) {
      Object.assign(states, d.snapshot);
      for (const n of Object.keys(d.snapshot)) updateStateUI(n);
    } else if (d.name) {
      states[d.name] = d.state;
      updateStateUI(d.name);
    }
  };
}

function postModule(name, action) {
  fetch(`/api/modules/${encodeURIComponent(name)}/${action}`, { method: "POST" }).catch(() => {});
}

const dotCls = st => `dot ${st}`;

function updateBadges() {
  const unused = Object.keys(states).filter(n => !refPlaces(n).length).length;
  document.querySelectorAll(".lamp.g").forEach(l => { l.dataset.badge = unused > 0 ? String(unused) : ""; });
}

function updateStateUI(name) {
  for (const win of winMap.values()) {
    for (const el of win.cards.values()) if (el.dataset.name === name) applyCardState(el);
  }
  const row = libList.querySelector(`.lib-row[data-name="${CSS.escape(name)}"]`);
  if (row) {
    const st = states[name] ?? "STOPPED";
    row.querySelector(".dot").className = dotCls(st);
    const act = row.querySelector(".lib-act");
    act.textContent = st === "RUNNING" ? "停止" : st === "STARTING" ? "启动中…" : "启动";
    act.disabled = st === "STARTING";
  }
  updateBadges();
}

/* ---- FLIP 与柔化助手 ---- */
/* 记录 rect → 应用变更 → 反向 transform 补间（~180ms ease），其余卡/图标平滑让位 */
function flipAnim(els, apply, dur = 180) {
  for (const el of els) { el.style.transition = ""; el.style.transform = ""; }
  const before = new Map();
  for (const el of els) before.set(el, el.getBoundingClientRect());
  apply();
  els.forEach(el => {
    const a = before.get(el), b = el.getBoundingClientRect();
    if (!a) return;
    const dx = a.left - b.left, dy = a.top - b.top;
    const sw = b.width ? a.width / b.width : 1, sh = b.height ? a.height / b.height : 1;
    if (Math.abs(dx) < .5 && Math.abs(dy) < .5 && Math.abs(sw - 1) < .002 && Math.abs(sh - 1) < .002) return;
    el.style.transition = "none";
    el.style.transformOrigin = "top left";
    el.style.transform = `translate(${dx}px, ${dy}px)` + (scaled(sw, sh) ? ` scale(${sw}, ${sh})` : "");
    void el.getBoundingClientRect(); // 强制回流后释放补间
    el.style.transition = `transform ${dur}ms ease`;
    el.style.transform = "";
    setTimeout(() => { el.style.transition = ""; el.style.transform = ""; }, dur + 40);
  });
}
const scaled = (sw, sh) => Math.abs(sw - 1) > .002 || Math.abs(sh - 1) > .002;

function flipCards(win, apply) {
  flipAnim([...win.grid.children].filter(el => el._entry && !el.classList.contains("slot-ghost")), apply);
}

function flipIcons(apply) {
  flipAnim([...icons.children].filter(c => c !== draggedIconEl() && c.classList.contains("icon")), apply);
}
const draggedIconEl = () => [...icons.children].find(c => c.classList.contains("lift"));

function syncRows(win) {
  let rows = 0;
  for (const e of tabs[win.tab]) rows = Math.max(rows, e.row + e.span[1] - 1);
  win.grid.style.gridTemplateRows = `repeat(${rows}, var(--cell))`;
}

/* 拖动/缩放期间显示 58px 网格线叠层，结束即收 */
function gridOverlay(win, show) {
  const o = win.grid.querySelector(".grid-overlay");
  if (o) o.classList.toggle("show", !!show);
}

/* ---- 自由网格几何：锚点解析 / 空位扫描 / 最近可容位 ---- */
function occupiedCells(list) {
  const occ = new Set();
  for (const e of list) {
    if (!Number.isInteger(e.col) || !Number.isInteger(e.row)) continue;
    for (let i = 0; i < e.span[0]; i++)
      for (let j = 0; j < e.span[1]; j++) occ.add((e.col + i) + "," + (e.row + j));
  }
  return occ;
}

function firstFit(occ, w, h) {
  for (let row = 1; row <= MAX_SCAN_ROWS; row++) {
    for (let col = 1; col <= MAX_W - w + 1; col++) {
      let ok = true;
      outer: for (let i = 0; i < w; i++) {
        for (let j = 0; j < h; j++) {
          if (occ.has((col + i) + "," + (row + j))) { ok = false; break outer; }
        }
      }
      if (ok) return { col, row };
    }
  }
  return { col: 1, row: MAX_SCAN_ROWS + 1 };
}

/* 为缺省锚点的条目就地 first-fit（并写回，随下一次 PUT 持久化） */
function normalizeEntries(list) {
  const occ = occupiedCells(list);
  for (const e of list) {
    if (Number.isInteger(e.col) && Number.isInteger(e.row) && e.col >= 1 && e.row >= 1) continue;
    const spot = firstFit(occ, e.span[0], e.span[1]);
    e.col = spot.col;
    e.row = spot.row;
    for (let i = 0; i < e.span[0]; i++)
      for (let j = 0; j < e.span[1]; j++) occ.add((e.col + i) + "," + (e.row + j));
  }
}

function rectFits(list, col, row, w, h, exclude) {
  if (col < 1 || row < 1 || col + w - 1 > MAX_W) return false;
  for (const e of list) {
    if (e === exclude || !Number.isInteger(e.col) || !Number.isInteger(e.row)) continue;
    const w2 = e.span[0], h2 = e.span[1];
    if (e.col < col + w && col < e.col + w2 && e.row < row + h && row < e.row + h2) return false;
  }
  return true;
}

/* 重叠落点：按 |dc| + 2|dr| 找最近可容锚点（含行下移）；全不可容返回 null */
function nearestFit(list, col, row, w, h, exclude) {
  const cands = [];
  for (let r = 1; r < row + 48; r++)
    for (let c = 1; c <= MAX_W - w + 1; c++) {
      const dc = Math.abs(c - col), dr = Math.abs(r - row);
      if (dc === 0 && dr === 0) continue;
      cands.push({ c, r, s: dc + 2 * dr });
    }
  cands.sort((a, b) => a.s - b.s);
  for (const cd of cands) if (rectFits(list, cd.c, cd.r, w, h, exclude)) return { col: cd.c, row: cd.r };
  return null;
}

function applyPos(card, e) {
  card.dataset.w = e.span[0];
  card.dataset.h = e.span[1];
  card.style.gridColumn = `${e.col} / span ${e.span[0]}`;
  card.style.gridRow = `${e.row} / span ${e.span[1]}`;
}

function applyRect(el, col, row, w, h) {
  el.style.gridColumn = `${col} / span ${w}`;
  el.style.gridRow = `${row} / span ${h}`;
}

/* 光标 → 格位锚点（保持抓取点在手内相对位置） */
function anchorAt(grid, entry, clientX, clientY, offX, offY) {
  const r = grid.getBoundingClientRect();
  const col = clamp(Math.floor((clientX - offX - r.left) / STEP) + 1, 1, MAX_W - entry.span[0] + 1);
  const row = Math.max(1, Math.floor((clientY - offY - r.top) / STEP) + 1);
  return { col, row };
}

/* ---- 桌面图标 ---- */
function renderIcons() {
  addIcon.remove();
  icons.textContent = "";
  for (const name of Object.keys(tabs)) icons.append(makeIcon(name));
  icons.append(addIcon);
  updateBadges();
}

function makeIcon(name) {
  const el = document.createElement("div");
  el.className = "icon";
  el.title = "双击打开；编辑模式可拖动排序；右键重命名/卸载";
  const disc = document.createElement("div");
  disc.className = "disc";
  disc.style.background = iconColor(name);
  disc.textContent = (name[0] || "?").toUpperCase();
  const label = document.createElement("div");
  label.className = "label";
  label.textContent = name;
  el.append(disc, label);
  el.ondblclick = () => openWindow(name);
  el.oncontextmenu = e => {
    e.preventDefault();
    e.stopPropagation();
    openMenu([
      { label: "打开", act: () => { closeMenu(); openWindow(name); } },
      { label: "重命名…", act: () => { closeMenu(); renameTab(name); } },
      { label: "卸载…", act: () => { closeMenu(); uninstallTab(name); } },
    ], e.clientX, e.clientY);
  };
  bindIconDrag(el, name);
  return el;
}

/* 编辑态：桌面图标拖动重排序（PUT 的 key 顺序即顺序） */
function bindIconDrag(el, name) {
  el.addEventListener("pointerdown", e => {
    if (e.button !== 0 || !editMode) return;
    e.preventDefault();
    let lastRef = null;
    const sx = e.clientX, sy = e.clientY;
    let moved = false;
    const move = ev => {
      if (!moved) {
        if (Math.hypot(ev.clientX - sx, ev.clientY - sy) < 8) return;
        moved = true;
        el.classList.add("lift");
        beginInteract(null, "");
      }
      const rest = [...icons.children].filter(c => c !== el && c.classList.contains("icon"));
      let ref = addIcon;
      for (const ic of rest) {
        const r = ic.getBoundingClientRect();
        const cy = r.top + r.height / 2;
        const sameRow = Math.abs(cy - ev.clientY) < r.height;
        if ((sameRow && ev.clientX < r.left + r.width / 2) || ev.clientY < cy) { ref = ic; break; }
      }
      if (lastRef === ref) return;
      lastRef = ref;
      flipAnim([...icons.children].filter(c => c !== el && c.classList.contains("icon")),
        () => icons.insertBefore(el, ref));
    };
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", up);
      el.classList.remove("lift");
      endInteract(null, "");
      if (!moved) return;
      const order = [...icons.querySelectorAll(".icon:not(.fresh)")].map(i => i.querySelector(".label").textContent);
      const next = {};
      for (const n of order) next[n] = tabs[n]; // 保留条目（含 id/锚点）
      tabs = next;
      renderIcons();
      saveConfig();
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  });
}

addIcon.onclick = createTab;

function createTab() {
  const n = promptTabName("新标签页名称：");
  if (!n) return;
  if (n in tabs) return alert(`标签"${n}"已存在`);
  tabs[n] = [];
  renderIcons();
  saveConfig(); // 新窗由用户双击打开
}

function promptTabName(msg, def) {
  return (prompt(msg, def ?? "") || "").trim();
}

function atLeastOneTab() {
  if (Object.keys(tabs).length > 1) return true;
  alert("至少保留一个标签页");
  return false;
}

function renameTab(name) {
  const n = promptTabName("重命名标签页：", name);
  if (!n || n === name) return;
  if (n in tabs) return alert(`标签"${n}"已存在`);
  const next = {};
  for (const [k, v] of Object.entries(tabs)) next[k === name ? n : k] = v;
  tabs = next;
  const win = winMap.get(name);
  if (win) {
    winMap.delete(name);
    winMap.set(n, win);
    win.tab = n;
    win.el.querySelector(".win-title").textContent = n;
    if (libWin === win) renderLibrary();
  }
  renderIcons();
  saveConfig();
}

function uninstallTab(name) {
  if (!atLeastOneTab()) return;
  const entries = tabs[name] ?? [];
  if (!confirm(`卸载 App「${name}」？\n其下 ${entries.length} 份模块引用将被移除，无引用的模块会被后端停止（模块本身不会被卸载）。`)) return;
  delete tabs[name];
  closeWindow(name);
  renderIcons();
  saveConfig();
}

/* ---- 窗口 ---- */
function openWindow(tab) {
  const dupe = winMap.get(tab);
  if (dupe) return focusWin(dupe);
  const el = document.createElement("section");
  el.className = "win";
  el.innerHTML = `
    <div class="win-head">
      <span class="lamp r" title="关闭窗口"></span>
      <span class="lamp y" title="最大化 / 还原"></span>
      <span class="lamp g" title="组件库"></span>
      <span class="win-title"></span>
    </div>
    <div class="win-body">
      <div class="grid" aria-label="模块网格"><div class="grid-overlay" aria-hidden="true"></div></div>
      <p class="hint" hidden>此 App 为空：点标题栏绿灯，从组件库添加模块。</p>
    </div>`;
  el.querySelector(".win-title").textContent = tab;
  const win = { tab, el, body: el.querySelector(".win-body"), grid: el.querySelector(".grid"), cards: new Map(), max: false };
  const n = winMap.size;
  const w = Math.min(920, window.innerWidth - 40), h = Math.min(560, window.innerHeight - 48);
  el.style.width = w + "px";
  el.style.height = h + "px";
  el.style.left = clamp(64 + n * 32, 16, window.innerWidth - w - 16) + "px";
  el.style.top = clamp(44 + n * 30, 8, window.innerHeight - h - 16) + "px";
  const [lr, ly, lg] = el.querySelectorAll(".lamp");
  lr.onclick = e => { e.stopPropagation(); closeWindow(tab); };
  ly.onclick = e => { e.stopPropagation(); toggleMax(win); };
  lg.onclick = e => { e.stopPropagation(); openLibrary(win); };
  bindTitlebar(win);
  el.addEventListener("pointerdown", () => focusWin(win), true);
  document.body.append(el);
  winMap.set(tab, win);
  renderWindow(win);
  focusWin(win);
}

function closeWindow(tab) {
  const win = winMap.get(tab);
  if (!win) return;
  winMap.delete(tab);
  if (libWin === win) closeLibrary();
  for (const el of win.cards.values()) el.remove();
  win.cards.clear();
  win.el.remove();
}

function focusWin(win) {
  win.el.style.zIndex = ++zTop;
  for (const w of winMap.values()) w.el.classList.toggle("focused", w === win);
}

function toggleMax(win) {
  const el = win.el;
  if (win.max) {
    Object.assign(el.style, JSON.parse(el.dataset.prev));
    win.max = false;
  } else {
    el.dataset.prev = JSON.stringify({ left: el.style.left, top: el.style.top, width: el.style.width, height: el.style.height });
    el.style.left = "0px";
    el.style.top = "0px";
    el.style.width = "100vw";
    el.style.height = "100vh";
    win.max = true;
  }
  libWinMove();
}

function libWinMove() {
  if (library.hidden || !libWin) return;
  const r = libWin.el.getBoundingClientRect(), w = library.offsetWidth, h = library.offsetHeight;
  library.style.left = clamp(r.left + 14, 8, window.innerWidth - w - 8) + "px";
  library.style.top = clamp(r.top + 36, 8, window.innerHeight - h - 8) + "px";
}

function bindTitlebar(win) {
  const head = win.el.querySelector(".win-head");
  head.addEventListener("pointerdown", e => {
    if (e.button !== 0 || e.target.closest(".lamp")) return;
    e.preventDefault();
    focusWin(win);
    if (win.max) return;
    beginInteract(null, "");
    const r = win.el.getBoundingClientRect();
    const ox = e.clientX - r.left, oy = e.clientY - r.top;
    const move = ev => {
      win.el.style.left = clamp(ev.clientX - ox, 24 - r.width, window.innerWidth - 24) + "px";
      win.el.style.top = clamp(ev.clientY - oy, 0, window.innerHeight - 40) + "px";
      libWinMove();
    };
    const stop = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", stop);
      document.removeEventListener("pointercancel", stop);
      endInteract(null, "");
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", stop);
    document.addEventListener("pointercancel", stop);
  });
}

/* ---- 窗口内网格渲染：锚点定位，行数随内容扩展 ---- */
function refPlaces(name) {
  const out = [];
  for (const [t, list] of Object.entries(tabs)) for (const e of list) if (e.name === name) out.push(t);
  return out;
}

function renderWindow(win) {
  const list = tabs[win.tab];
  normalizeEntries(list);
  for (const [e, el] of [...win.cards]) {
    if (!list.includes(e)) { el.remove(); win.cards.delete(e); }
  }
  for (const e of list) {
    let el = win.cards.get(e);
    if (!el) { el = createCard(e, win); win.cards.set(e, el); }
    if (el.parentElement !== win.grid) win.grid.append(el); // DOM 顺序仅决定叠放层级
    applyPos(el, e);
    applyCardState(el);
  }
  syncRows(win);
  win.body.querySelector(".hint").hidden = list.length > 0;
}

/* ---- 卡片：无装帧 + 编辑态全卡遮罩 + 手柄 ---- */
function createCard(entry, win) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.name = entry.name;
  card._entry = entry;

  const iframe = document.createElement("iframe");
  iframe.title = entry.name;
  const mask = document.createElement("div");
  mask.className = "card-mask"; // 编辑态全卡遮罩：自由拖动
  mask.title = "编辑模式：按住自由拖动落锚；边缘可缩放";
  const rzR = document.createElement("div");
  rzR.className = "rz rz-r";
  const rzB = document.createElement("div");
  rzB.className = "rz rz-b";
  const rzBR = document.createElement("div");
  rzBR.className = "rz rz-br";
  const chip = document.createElement("div");
  chip.className = "ctrl-chip";
  chip.innerHTML = `<span class="dot"></span><span class="chip-st"></span><button class="chip-btn" type="button"></button>`;
  // 浏览态 hover 浮现 / 非 RUNNING 常驻；编辑态常驻可点（z 高于遮罩）
  chip.addEventListener("click", e => {
    e.stopPropagation();
    const n = card.dataset.name, st = states[n] ?? "STOPPED";
    if (st === "STARTING") return;
    postModule(n, st === "RUNNING" ? "stop" : "start");
  });
  card.append(iframe, chip, mask, rzR, rzB, rzBR);

  bindCardDrag(mask, card, win);
  bindResize(rzR, card, win, "x");
  bindResize(rzB, card, win, "y");
  bindResize(rzBR, card, win, "xy");
  card.addEventListener("contextmenu", e => {
    e.preventDefault();
    e.stopPropagation();
    showCardMenu(card, win, e.clientX, e.clientY);
  });
  applyCardState(card);
  return card;
}

/* 控制芯片：状态点 + 状态文字 + 互斥操作按钮（同模块多卡经 updateStateUI→applyCardState 同步） */
function updateChip(card, name, st) {
  const chip = card.querySelector(".ctrl-chip");
  chip.querySelector(".dot").className = dotCls(st);
  chip.querySelector(".chip-st").textContent =
    st === "RUNNING" ? "运行中" : st === "STARTING" ? "启动中" : st === "ERROR" ? "已出错" : "已停止";
  const btn = chip.querySelector(".chip-btn");
  btn.textContent = st === "RUNNING" ? "关闭" : st === "STARTING" ? "启动中…" : st === "ERROR" ? "重试" : "启动";
  btn.disabled = st === "STARTING";
  chip.classList.toggle("always", st !== "RUNNING"); // 非 RUNNING 时浏览态也常驻
}

/* 仅 RUNNING 渲染 iframe；下线摘除不留死帧 */
function applyCardState(card) {
  const name = card.dataset.name, st = states[name] ?? "STOPPED";
  updateChip(card, name, st);
  const iframe = card.querySelector("iframe");
  if (st === "RUNNING") {
    if (!iframe.dataset.run) {
      probeSrc(name).then(src => {
        if (states[name] !== "RUNNING" || iframe.dataset.run) return;
        iframe.dataset.run = "1";
        iframe.src = src;
      });
    }
  } else if (iframe.dataset.run) {
    delete iframe.dataset.run;
    iframe.src = "about:blank";
  }
}

/* src 优先 /<名字>/card，探测（HEAD 200~399/405）不存在则回退 /<名字>/ */
function probeSrc(name) {
  if (!probeSrc.cache.has(name)) {
    probeSrc.cache.set(name,
      fetch(`/${name}/card`, { method: "HEAD" })
        .then(r => (r.ok || r.status === 405) ? `/${name}/card` : `/${name}/`)
        .catch(() => `/${name}/`));
  }
  return probeSrc.cache.get(name);
}
probeSrc.cache = new Map();

/* ---- 卡片右键菜单 ---- */
function showCardMenu(card, win, x, y) {
  const name = card.dataset.name;
  const st = states[name] ?? "STOPPED";
  focusWin(win);
  openMenu([
    {
      label: st === "RUNNING" ? "强制关闭" : st === "STARTING" ? "启动中…" : "启动",
      disabled: st === "STARTING",
      act: () => { postModule(name, st === "RUNNING" ? "stop" : "start"); closeMenu(); },
    },
    {
      label: "移动到 ▸",
      sub: Object.keys(tabs).filter(t => t !== win.tab)
        .map(t => ({ label: t, act: () => { moveToTab(card._entry, t); closeMenu(); } }))
        .concat({ label: "＋ 移入新标签页…", act: () => { moveToTab(card._entry, "__new"); closeMenu(); } }),
    },
    { label: "移出到组件库", act: () => { removeFromLibrary(card._entry); closeMenu(); } },
  ], x, y);
}

/* ---- 通用毛玻璃菜单 ---- */
function buildMenuItem(it) {
  const li = document.createElement("li");
  li.textContent = it.label;
  if (it.disabled) li.classList.add("disabled");
  else if (it.act) li.onclick = it.act;
  if (it.sub) {
    li.classList.add("has-sub");
    const sub = document.createElement("ul");
    sub.className = "sub";
    for (const s of it.sub) sub.append(buildMenuItem(s));
    li.append(sub);
  }
  return li;
}

function openMenu(items, x, y) {
  menu.textContent = "";
  for (const it of items) menu.append(buildMenuItem(it));
  menu.hidden = false;
  const r = menu.getBoundingClientRect();
  menu.style.left = clamp(x, 8, window.innerWidth - r.width - 8) + "px";
  menu.style.top = clamp(y, 8, window.innerHeight - r.height - 8) + "px";
}

function closeMenu() { menu.hidden = true; }

function overRect(x, y, el) {
  const r = el.getBoundingClientRect();
  return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
}

/* 组件库面板叠在窗口 body 之上：命中须在窗口内且不在面板上 */
function overWinBody(x, y, win) {
  return win && !library.hidden && overRect(x, y, win.body) && !overRect(x, y, library);
}

function openLibrary(win) {
  if (libWin === win && !library.hidden) return closeLibrary();
  libWin = win;
  library.hidden = false;
  renderLibrary();
  libWinMove();
}

function closeLibrary() {
  library.hidden = true;
  library.classList.remove("drop-target");
  libWin = null;
}

libClose.onclick = closeLibrary;

/* ---- 组件库列表 ---- */
function renderLibrary() {
  libList.textContent = "";
  let unused = 0;
  for (const name of Object.keys(states)) {
    const places = refPlaces(name);
    const row = document.createElement("div");
    row.className = "lib-row" + (places.length ? " used" : " unused");
    row.dataset.name = name;
    const dot = document.createElement("span");
    dot.className = dotCls(states[name] ?? "STOPPED");
    const label = document.createElement("span");
    label.className = "lib-name";
    label.textContent = name;
    const act = document.createElement("button");
    act.className = "lib-act";
    const st = states[name] ?? "STOPPED";
    act.textContent = st === "RUNNING" ? "停止" : st === "STARTING" ? "启动中…" : "启动";
    act.disabled = st === "STARTING";
    act.onclick = e => {
      e.stopPropagation();
      postModule(name, (states[name] ?? "STOPPED") === "RUNNING" ? "stop" : "start");
    };
    const tag = document.createElement("span");
    tag.className = "lib-tag";
    if (places.length) {
      tag.textContent = "位于 " + places[0] + (places.length > 1 ? " ×" + places.length : "");
      row.title = "点击=再添加一份（自动找空位）；按住拖到网格可指定位置";
    } else {
      unused++;
      tag.textContent = "未启用";
      row.title = "点击加入（自动找空位）；按住拖到网格可指定位置";
    }
    row.onclick = () => { if (libWin) addToTab(libWin, name); };
    bindLibraryDrag(row, name);
    row.append(dot, label, act, tag);
    libList.append(row);
  }
  updateBadges();
}

/* 点选添加：first-fit（行优先扫描）而不是尾部追加 */
function addToTab(win, name) {
  const list = tabs[win.tab];
  normalizeEntries(list);
  const spot = firstFit(occupiedCells(list), DEF[0], DEF[1]);
  const entry = { name, span: [...DEF], col: spot.col, row: spot.row };
  flipCards(win, () => {
    list.push(entry);
    renderWindow(win);
  });
  saveConfig();
}

function removeFromLibrary(entry) {
  const src = Object.keys(tabs).find(t => tabs[t].includes(entry));
  if (!src) return;
  if (!confirm(`把「${entry.name}」的这份摆放移出到组件库？\n该模块所有引用清空后，后端会自动停止它（模块本身不会被卸载）。`)) return;
  tabs[src].splice(tabs[src].indexOf(entry), 1);
  for (const win of winMap.values()) renderWindow(win);
  renderLibrary();
  saveConfig();
}

function moveToTab(entry, option) {
  let target = option;
  if (option === "__new") {
    const n = promptTabName("新标签页名称：");
    if (!n) return;
    if (n in tabs) return alert(`标签"${n}"已存在`);
    tabs[n] = [];
    target = n;
  }
  const src = Object.keys(tabs).find(t => tabs[t].includes(entry));
  if (!src) return;
  tabs[target].push(tabs[src].splice(tabs[src].indexOf(entry), 1)[0]);
  for (const win of winMap.values()) renderWindow(win);
  renderLibrary();
  saveConfig();
}

/* 行拖入：位移超过阈值才进拖放（纯点击保留原生 click=first-fit 追加） */
function bindLibraryDrag(row, name) {
  row.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    const sx = e.clientX, sy = e.clientY;
    let dragging = false, ghost = null, ph = null, inGrid = false;
    const start = () => {
      dragging = true;
      beginInteract(null, "");
      if (libWin) gridOverlay(libWin, true);
      ghost = document.createElement("div");
      ghost.className = "ghost";
      ghost.textContent = name;
      document.body.append(ghost);
      ph = document.createElement("div");
      ph.className = "card slot-ghost";
      inGrid = false;
    };
    const move = ev => {
      if (!dragging && Math.hypot(ev.clientX - sx, ev.clientY - sy) < 8) return;
      if (!dragging) start();
      ghost.style.left = ev.clientX + 10 + "px";
      ghost.style.top = ev.clientY + 10 + "px";
      if (libWin && overWinBody(ev.clientX, ev.clientY, libWin)) {
        const t = anchorAtRaw(libWin.grid, ev.clientX, ev.clientY, DEF[0] / 2 * STEP, DEF[1] / 2 * STEP);
        if (!inGrid) { libWin.grid.append(ph); inGrid = true; }
        ph.style.display = "";
        applyRect(ph, t.col, t.row, DEF[0], DEF[1]);
        ph.classList.toggle("overlap", !rectFits(tabs[libWin.tab], t.col, t.row, DEF[0], DEF[1], null));
      } else if (inGrid) {
        ph.remove();
        inGrid = false;
      }
    };
    const up = ev => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", up);
      if (!dragging) return; // 纯点击：交回 click 事件
      endInteract(null, "");
      if (libWin) gridOverlay(libWin, false);
      ghost.remove();
      ph.remove();
      if (libWin && overWinBody(ev.clientX, ev.clientY, libWin)) {
        const t = anchorAtRaw(libWin.grid, ev.clientX, ev.clientY, DEF[0] / 2 * STEP, DEF[1] / 2 * STEP);
        let col = t.col, roww = t.row;
        if (!rectFits(tabs[libWin.tab], col, roww, DEF[0], DEF[1], null)) {
          const fit = nearestFit(tabs[libWin.tab], col, roww, DEF[0], DEF[1], null);
          if (fit) { col = fit.col; roww = fit.row; }
        }
        const entry = { name, span: [...DEF], col, row: roww };
        flipCards(libWin, () => {
          tabs[libWin.tab].push(entry);
          renderWindow(libWin);
        });
        saveConfig();
      }
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  });
}

/* 中心对齐版锚点换算（组件库行拖：以格中心为落点） */
function anchorAtRaw(grid, clientX, clientY, offX, offY) {
  const r = grid.getBoundingClientRect();
  return {
    col: clamp(Math.floor((clientX - offX - r.left) / STEP) + 1, 1, MAX_W),
    row: Math.max(1, Math.floor((clientY - offY - r.top) / STEP) + 1),
  };
}

/* ---- 编辑模式开关（iOS 滑块） ---- */
function setEditMode(on) {
  editMode = on;
  document.body.classList.toggle("editing", on);
  editSwitch.classList.toggle("on", on);
  editSwitch.setAttribute("aria-checked", on ? "true" : "false");
  editSwitch.title = on ? "完成（退出编辑模式）" : "编辑模式：拖动排序与缩放";
  if (editLabel) editLabel.textContent = on ? "完成" : "编辑模式";
}
editSwitch.onclick = () => setEditMode(!editMode);

/* ---- 编辑态卡片自由拖动：跟随网格、重叠先滑位、不可容则叠放置顶 ---- */
function bindCardDrag(mask, card, win) {
  mask.addEventListener("pointerdown", e => {
    if (e.button !== 0 || !editMode) return;
    e.preventDefault();
    focusWin(win);
    gridOverlay(win, true);
    const entry = card._entry;
    const cr = card.getBoundingClientRect();
    const offX = e.clientX - cr.left, offY = e.clientY - cr.top;
    beginInteract(card, "dragging");
    const ph = document.createElement("div");
    ph.className = "card slot-ghost";
    win.grid.append(ph);
    let last = null;
    const move = ev => {
      if (overRect(ev.clientX, ev.clientY, library)) {
        last = null; // 悬停组件库：指示可放置（移出），隐藏格位预览
        ph.style.display = "none";
        library.classList.add("drop-target");
        return;
      }
      library.classList.remove("drop-target");
      const t = anchorAt(win.grid, entry, ev.clientX, ev.clientY, offX, offY);
      if (!last || last.col !== t.col || last.row !== t.row) {
        last = t;
        ph.style.display = "";
        applyRect(ph, t.col, t.row, entry.span[0], entry.span[1]);
        ph.classList.toggle("overlap", !rectFits(tabs[win.tab], t.col, t.row, entry.span[0], entry.span[1], entry));
      }
    };
    const up = ev => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", up);
      gridOverlay(win, false);
      endInteract(card, "dragging");
      ph.remove();
      library.classList.remove("drop-target");
      if (overRect(ev.clientX, ev.clientY, library)) {
        dropGuard = performance.now();
        removeFromLibrary(entry);
        return;
      }
      const t = last ?? anchorAt(win.grid, entry, ev.clientX, ev.clientY, offX, offY);
      let col = t.col, roww = t.row, stacked = false;
      if (!rectFits(tabs[win.tab], col, roww, entry.span[0], entry.span[1], entry)) {
        const fit = nearestFit(tabs[win.tab], col, roww, entry.span[0], entry.span[1], entry);
        if (fit) { col = fit.col; roww = fit.row; }
        else stacked = true; // 找不到可容位：叠放，被拖卡置顶
      }
      flipCards(win, () => {
        if (stacked) win.grid.append(card);
        entry.col = col;
        entry.row = roww;
        applyPos(card, entry);
        syncRows(win);
      });
      saveConfig();
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  });
}

/* ---- 编辑态缩放：右/下/右下拖到哪 span 到哪；盖到邻居 ghost 预告 ---- */
function bindResize(handle, card, win, ax) {
  handle.addEventListener("pointerdown", e => {
    if (e.button !== 0 || !editMode) return;
    e.preventDefault();
    e.stopPropagation();
    focusWin(win);
    gridOverlay(win, true);
    beginInteract(card, "resizing");
    const entry = card._entry;
    const ph = document.createElement("div");
    ph.className = "card slot-ghost";
    win.grid.append(ph);
    let w = entry.span[0], hh = entry.span[1], col = entry.col, roww = entry.row;
    const move = ev => {
      const r = win.grid.getBoundingClientRect();
      const cIdx = Math.floor((ev.clientX - r.left) / STEP);
      const rIdx = Math.floor((ev.clientY - r.top) / STEP);
      w = ax.includes("x") ? clamp(cIdx - entry.col + 2, MIN, MAX_W) : entry.span[0];
      hh = ax.includes("y") ? clamp(rIdx - entry.row + 2, MIN, MAX_H) : entry.span[1];
      col = entry.col;
      roww = entry.row;
      applyRect(ph, col, roww, w, hh);
      ph.classList.toggle("overlap", !rectFitsExclude(tabs[win.tab], entry, { col, row: roww, w, h: hh }));
      win.grid.style.gridTemplateRows = `repeat(${Math.max(+win.grid.style.gridTemplateRows.match(/\d+/)?.[0] || 0, roww + hh - 1)}, var(--cell))`;
    };
    move(e);
    const up = () => {
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", up);
      gridOverlay(win, false);
      endInteract(card, "resizing");
      ph.remove();
      flipCards(win, () => {
        entry.span = [w, hh];
        applyPos(card, entry);
        syncRows(win);
      });
      saveConfig();
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  });
}

/* 缩放预告用：排除自身、暂用待定 rect 判断重叠 */
function rectFitsExclude(list, exclude, rect) {
  if (rect.col < 1 || rect.row < 1 || rect.col + rect.w - 1 > MAX_W) return false;
  for (const e of list) {
    if (e === exclude || !Number.isInteger(e.col) || !Number.isInteger(e.row)) continue;
    if (e.col < rect.col + rect.w && rect.col < e.col + e.span[0]
        && e.row < rect.row + rect.h && rect.row < e.row + e.span[1]) return false;
  }
  return true;
}

/* ---- 交互期遮罩：拖拽/缩放期间禁 iframe 鼠标穿透 ---- */
function beginInteract(card, cls) {
  document.body.classList.add("interacting");
  dragMask.hidden = false;
  if (card) card.classList.add(cls);
}
function endInteract(card, cls) {
  document.body.classList.remove("interacting");
  dragMask.hidden = true;
  if (card) card.classList.remove(cls);
}

/* ---- 全局：Esc / 点外关闭 ---- */
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (!menu.hidden) closeMenu();
  if (!library.hidden) closeLibrary();
});
document.addEventListener("pointerdown", e => {
  const t = sel => e.target.closest && e.target.closest(sel);
  if (!menu.hidden && !t("#menu")) closeMenu();
  if (!library.hidden && performance.now() - dropGuard > 300
      && !t("#library")
      && !t(".card")
      && !t(".lamp.g")) {
    closeLibrary();
  }
}, true);

/* ---- 配置持久化：PUT 前补齐锚点；失败回拉；成功回拉合并 id ---- */
async function saveConfig() {
  for (const list of Object.values(tabs)) normalizeEntries(list);
  const r = await fetch("/api/config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tabs }),
  });
  renderLibrary();
  if (!r.ok) {
    const msg = await r.text().catch(() => "");
    alert("保存布局失败，正在还原：" + msg);
    hardReloadConfig();
  } else {
    syncIds();
  }
}

async function syncIds() {
  const r = await fetch("/api/config");
  if (!r.ok) return;
  const srv = (await r.json()).tabs ?? {};
  for (const [t, list] of Object.entries(srv)) {
    const loc = tabs[t];
    if (!loc) continue;
    loc.forEach((e, i) => { if (!e.id && list[i] && list[i].id) e.id = list[i].id; });
  }
}

async function hardReloadConfig() {
  const r = await fetch("/api/config");
  if (!r.ok) return;
  tabs = (await r.json()).tabs;
  for (const t of [...winMap.keys()]) if (!(t in tabs)) closeWindow(t);
  if (libWin && !(libWin.tab in tabs)) closeLibrary();
  renderIcons();
  for (const win of winMap.values()) renderWindow(win);
  renderLibrary();
}
