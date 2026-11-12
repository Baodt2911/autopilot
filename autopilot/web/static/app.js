"use strict";
// Autopilot dashboard. Vanilla JS. Server text is ONLY ever set via textContent (it is untrusted).
const POLL_MS = 5000;
const $ = (id) => document.getElementById(id);
const STATE_VI = { none: "chưa có session", idle: "rảnh", working: "đang chạy", waiting: "chờ bạn",
  dead: "lỗi", limited: "hết limit", owner_busy: "bạn đang mở" };
const STATE_TONE = { none: "muted", idle: "ok", working: "info", waiting: "warn", dead: "bad", limited: "bad", owner_busy: "warn" };
const BATCH_VI = { new: "mới", delivered: "đã giao", acked: "đã nhận", in_progress: "đang làm",
  done: "xong", escalated: "đã báo owner", cancelled: "đã hủy" };
const SETTINGS = [
  ["limits.thread_rounds", "Số vòng tối đa mỗi thread", "number"],
  ["limits.wakes_per_pr_day", "Số lần đánh thức tối đa mỗi PR mỗi ngày", "number"],
  ["notify.hours", "Khung giờ thông báo (HH:MM-HH:MM)", "text"],
  ["notify.remind_after_h", "Nhắc lại sau (giờ)", "number"],
  ["notify.digest_at", "Giờ gửi tóm tắt (HH:MM)", "text"],
  ["reply.decision_template", "Mẫu reply quyết định", "textarea"],
  ["max_workers", "Số worker tối đa", "number"],
  ["debounce_min", "Gom comment trong (phút)", "number"],
];

const state = { project: null, prs: [], overview: null, tlPr: "", backlogKind: "spec-gap", backlog: [],
  busy: false, timer: null, settingsLoaded: false };

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "data") for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
}
const badge = (text, tone = "muted", title) => h("span", { class: `badge ${tone}`, title }, text);
const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : null);
const clear = (el) => el.replaceChildren();
const fmtTime = (iso) => { if (!iso) return "–"; const d = new Date(iso); return isNaN(d) ? "–" : d.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" }); };
const fmtDateTime = (iso) => { if (!iso) return "–"; const d = new Date(iso); return isNaN(d) ? "–" : d.toLocaleString("vi-VN", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }); };

// ---- API ---------------------------------------------------------------------------
class ApiErr extends Error { constructor(status, msg) { super(msg); this.status = status; } }
async function api(path, body) {
  const url = new URL(path, location.origin);
  if (state.project) url.searchParams.set("project", state.project);
  const opt = { credentials: "same-origin" };
  if (body !== undefined) Object.assign(opt, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let res;
  try { res = await fetch(url, opt); } catch { throw new ApiErr(0, "Không kết nối được máy chủ dashboard."); }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new ApiErr(res.status, (data && data.error) || `Lỗi ${res.status}`);
  return data;
}

let toastTimer;
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg; t.className = "toast" + (isErr ? " err" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3500);
}
function setBanner(msg) { const b = $("banner"); b.hidden = !msg; b.textContent = msg || ""; }

async function copyText(text, what) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = h("textarea", { "aria-hidden": "true", style: "position:fixed;opacity:0" }); ta.value = text;
    document.body.append(ta); ta.select();
    try { document.execCommand("copy"); } catch { toast("Không copy được.", true); ta.remove(); return; }
    ta.remove();
  }
  toast(`Đã copy ${what}.`);
}

// Run an owner action: disable buttons, toast the result, refresh.
async function act(btns, path, body, okMsg, errBox) {
  btns.forEach((b) => { b.disabled = true; });
  if (errBox) errBox.hidden = true;
  try {
    const res = await api(path, body);
    toast(okMsg(res)); await refresh(); return true;
  } catch (e) {
    if (errBox) { errBox.textContent = e.message; errBox.hidden = false; }
    toast(e.message, true);
    btns.forEach((b) => { b.disabled = false; });
    return false;
  }
}

// ---- header ------------------------------------------------------------------------
function renderOverview(o) {
  state.overview = o;
  const rs = $("router-state");
  let txt = "Đang chạy", tone = "ok";
  if (o.paused) { txt = "Đang tạm dừng"; tone = "warn"; }
  else if (o.limit_until) { txt = `Đang chờ reset lúc ${fmtTime(o.limit_until)}`; tone = "bad"; }
  rs.textContent = txt; rs.className = `badge ${tone}`;
  const s = $("slots"); s.textContent = `Slot ${o.slots.working}/${o.slots.max}`;
  const inc = $("incidents"); inc.hidden = !o.incidents_24h; inc.textContent = `${o.incidents_24h} sự cố / 24 giờ`;
  const b = $("toggle-router"); b.disabled = false;
  b.textContent = o.paused ? "Chạy lại" : "Dừng";
  b.classList.toggle("primary", o.paused);
  b.dataset.action = o.paused ? "resume" : "pause";
}

// ---- decisions ---------------------------------------------------------------------
const LABELS = /^(Reviewer hỏi|Spec|Vì sao không tự quyết|A|B)(\s*:)/;
function qBlock(body) {
  const box = h("div", { class: "qblock" });
  for (const line of (body || "").split("\n")) {
    const m = LABELS.exec(line);
    box.append(h("div", {}, m ? [h("b", { text: m[1] + m[2] }), line.slice(m[0].length)] : line));
  }
  return box;
}
function cardHead(it) {
  const kind = { Q: ["Câu hỏi", "warn"], P: ["Kế hoạch B", "info"], R: ["Nháp reply", "ok"] }[it.kind] || [it.kind, "muted"];
  const pr = state.prs.find((p) => p.number === it.pr);
  const link = safeUrl(pr && pr.url);
  return h("header", {}, badge(kind[0], kind[1]), h("span", { class: "code", text: it.code }),
    link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: `PR #${it.pr}` }) : h("span", { text: `PR #${it.pr}` }),
    h("span", { class: "muted", text: fmtDateTime(it.created_at) }));
}
function field(label, el) { return h("label", {}, label, el); }

function buildCard(it) {
  const err = h("p", { class: "err-msg", hidden: true });
  const card = h("article", { class: "card", data: { code: it.code } }, cardHead(it));
  const note = h("input", { type: "text", placeholder: "Ghi chú cho worker (tùy chọn)", maxlength: "1000" });
  if (it.kind === "Q") {
    const src = safeUrl(it.title);
    card.append(src ? h("p", {}, h("a", { href: src, target: "_blank", rel: "noopener noreferrer", text: "Mở thread trên GitHub" })) : h("p", { class: "muted", text: it.title || "" }), qBlock(it.body_md));
    const hint = h("textarea", { rows: "2", placeholder: "Viết ý bằng tiếng Việt, worker sẽ viết lại bằng tiếng Anh" });
    const source = h("input", { type: "text", placeholder: "Link nguồn của quyết định (tùy chọn)" });
    const a = h("button", { class: "btn primary", type: "button", title: "Chọn phương án A", "aria-label": `Chọn A cho ${it.code}`, text: "Chọn A" });
    const b = h("button", { class: "btn primary", type: "button", title: "Chọn phương án B", "aria-label": `Chọn B cho ${it.code}`, text: "Chọn B" });
    const go = (choice) => act([a, b], "/api/answer", { code: it.code, choice, note: note.value, reply_hint: hint.value, source: source.value.trim() || "web" },
      (r) => `Đã gửi ${it.code}: chọn ${choice}.`, err);
    a.onclick = () => go("A"); b.onclick = () => go("B");
    card.append(h("div", { class: "form-grid" }, field("Ghi chú", note), field("Cách reply", hint), field("Link nguồn", source)), h("div", { class: "actions" }, a, b));
  } else if (it.kind === "P") {
    card.append(qBlock(it.body_md));
    const ok = h("button", { class: "btn primary", type: "button", text: "Duyệt", title: "Duyệt kế hoạch", "aria-label": `Duyệt ${it.code}` });
    const no = h("button", { class: "btn danger", type: "button", text: "Từ chối", title: "Từ chối kế hoạch", "aria-label": `Từ chối ${it.code}` });
    ok.onclick = () => act([ok, no], "/api/approve", { code: it.code, ok: true, note: note.value }, () => `Đã duyệt ${it.code}.`, err);
    no.onclick = () => act([ok, no], "/api/approve", { code: it.code, ok: false, note: note.value }, () => `Đã từ chối ${it.code}.`, err);
    card.append(h("div", { class: "form-grid" }, field("Ghi chú", note)), h("div", { class: "actions" }, ok, no));
  } else {
    card.append(h("p", { class: "muted", text: it.title || "Nháp reply tiếng Anh:" }), h("div", { class: "qblock draft", text: it.body_md || "" }));
    const send = h("button", { class: "btn primary", type: "button", text: "Gửi", title: "Gửi bản nháp này", "aria-label": `Gửi ${it.code}` });
    const edit = h("button", { class: "btn", type: "button", text: "Sửa", title: "Yêu cầu worker sửa nháp theo ghi chú", "aria-label": `Sửa ${it.code}` });
    note.placeholder = "Ghi chú: cần sửa gì? (bắt buộc khi bấm Sửa)";
    send.onclick = () => act([send, edit], "/api/approve", { code: it.code, ok: true, note: note.value }, () => `Đã gửi ${it.code}.`, err);
    edit.onclick = () => {
      if (!note.value.trim()) { err.textContent = "Hãy ghi chú cần sửa gì trước khi bấm Sửa."; err.hidden = false; note.focus(); return; }
      act([send, edit], "/api/approve", { code: it.code, ok: false, note: note.value }, () => `Đã yêu cầu sửa ${it.code}.`, err);
    };
    card.append(h("div", { class: "form-grid" }, field("Ghi chú", note)), h("div", { class: "actions" }, send, edit));
  }
  card.append(err);
  return card;
}

function renderItems(items) {
  const list = $("decisions-list"), want = new Set(items.map((i) => i.code));
  for (const c of [...list.children]) if (!want.has(c.dataset.code)) c.remove(); // keep existing cards => typed text survives
  const have = new Set([...list.children].map((c) => c.dataset.code));
  for (const it of items) if (!have.has(it.code)) list.append(buildCard(it));
  const order = new Map(items.map((i, n) => [i.code, n]));
  [...list.children].sort((x, y) => order.get(x.dataset.code) - order.get(y.dataset.code)).forEach((c) => list.append(c));
  $("decisions-count").textContent = items.length || "";
  $("decisions-empty").hidden = items.length > 0;
}

// ---- PRs ---------------------------------------------------------------------------
function prBadges(p) {
  const rv = { APPROVED: ["Đã duyệt", "ok"], CHANGES_REQUESTED: ["Cần sửa", "bad"], REVIEW_REQUIRED: ["Chờ review", "warn"] }[p.review] || ["Chưa review", "muted"];
  const out = [];
  if (p.draft) out.push(badge("Draft", "muted"));
  out.push(badge(`Review: ${rv[0]}`, rv[1]));
  out.push(p.conflict ? badge("Conflict", "bad") : badge("Không conflict", "ok"));
  out.push(p.failed_checks.length ? badge(`CI lỗi (${p.failed_checks.length})`, "bad", p.failed_checks.join(", ")) : badge("CI ổn", "ok"));
  if (p.batch_status) out.push(badge(`Lô: ${BATCH_VI[p.batch_status] || p.batch_status}`, "info"));
  return out;
}
function renderPrs(prs) {
  state.prs = prs;
  const body = document.querySelector("#prs tbody"); clear(body);
  const paused = new Set((state.overview && state.overview.paused_prs) || []);
  for (const p of prs) {
    const link = safeUrl(p.url);
    const isPaused = paused.has(p.number);
    const pause = h("button", { class: "btn small", type: "button", text: isPaused ? "Chạy lại" : "Dừng", title: `${isPaused ? "Chạy lại" : "Dừng"} PR #${p.number}`, "aria-label": `${isPaused ? "Chạy lại" : "Dừng"} PR #${p.number}` });
    pause.onclick = () => act([pause], isPaused ? "/api/resume" : "/api/pause", { pr: p.number }, () => (isPaused ? `Đã chạy lại PR #${p.number}.` : `Đã dừng PR #${p.number}.`));
    const copy = h("button", { class: "btn small", type: "button", text: "Copy link", title: `Copy link PR #${p.number}`, "aria-label": `Copy link PR #${p.number}` });
    copy.onclick = () => copyText(p.url, `link PR #${p.number}`);
    body.append(h("tr", {},
      h("td", { data: { label: "PR" } }, h("div", { class: "pr-title" }, link ? h("a", { href: link, target: "_blank", rel: "noopener noreferrer", text: `#${p.number}` }) : `#${p.number}`, " ", p.title)),
      h("td", { data: { label: "Nhánh" } }, h("div", { class: "mono", text: `${p.head} → ${p.base}` })),
      h("td", { data: { label: "Trạng thái" } }, h("div", { class: "badges" }, prBadges(p), isPaused ? badge("Đang tạm dừng", "warn") : null),
        p.warnings.map((w) => h("div", { class: "warn-line", text: `⚠ ${w}` }))),
      h("td", { data: { label: "Việc gần nhất" } }, h("span", { class: p.last_action ? "" : "muted", text: p.last_action || "Chưa có" })),
      h("td", { data: { label: "Thao tác" } }, h("div", { class: "actions" }, copy, pause))));
  }
  $("prs-empty").hidden = prs.length > 0;
  $("prs").hidden = prs.length === 0;
  const sel = $("tl-filter"), cur = state.tlPr;
  const opts = ["", ...prs.map((p) => String(p.number))];
  if (opts.join() !== [...sel.options].map((o) => o.value).join()) {
    clear(sel); opts.forEach((v) => sel.append(h("option", { value: v, text: v ? `PR #${v}` : "Tất cả PR" })));
  }
  sel.value = opts.includes(cur) ? cur : "";
}

// ---- workers -----------------------------------------------------------------------
function renderWorkers(ws) {
  const box = $("workers"); clear(box);
  for (const w of ws) {
    const lr = w.last_run, sess = w.session;
    const row = (k, v) => h("div", { class: "row" }, h("span", { text: k }), h("span", { text: v }));
    const cmd = sess ? `cd ${w.path} && claude --resume ${sess}` : null;
    const copy = h("button", { class: "btn small", type: "button", text: "Copy lệnh", title: cmd ? "Copy lệnh mở lại session" : "Chưa có session", "aria-label": `Copy lệnh resume cho ${w.branch}` });
    if (cmd) copy.onclick = () => copyText(cmd, "lệnh resume"); else copy.disabled = true;
    box.append(h("article", { class: "card worker" },
      h("header", {}, h("span", { class: "mono", text: w.branch }), badge(STATE_VI[w.state] || w.state, STATE_TONE[w.state] || "muted")),
      row("PR", w.pr ? `#${w.pr}` : "chưa có PR"),
      row("Session", sess ? sess.slice(0, 8) : "–"),
      row("Bắt đầu lượt cuối", lr ? fmtDateTime(lr.started_at) : "–"),
      row("Kết thúc lượt cuối", lr && lr.ended_at ? fmtDateTime(lr.ended_at) : (w.state === "working" ? "đang chạy" : "–")),
      row("Chi phí lượt cuối", lr && lr.cost_usd != null ? `$${lr.cost_usd.toFixed(2)}` : "–"),
      row("Số lần chạy lại", lr ? String(lr.attempts) : "–"),
      h("div", { class: "actions", style: "margin-top:8px" }, copy)));
  }
  $("workers-empty").hidden = ws.length > 0;
}

// ---- timeline / digest / backlog ----------------------------------------------------
function renderTimeline(items) {
  const ol = $("timeline"); clear(ol);
  const prUrl = new Map(state.prs.map((p) => [p.number, p.url]));
  for (const t of items) {
    const url = safeUrl(t.url) || safeUrl(prUrl.get(t.pr));
    ol.append(h("li", {}, h("time", { datetime: t.at, text: fmtDateTime(t.at) }),
      h("div", { class: "tl-text" }, badge(t.kind, t.kind === "noop" ? "muted" : t.kind === "incident" || t.kind === "escalated" ? "bad" : "info"), " ",
        t.pr ? (url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: `#${t.pr}` }) : `#${t.pr}`) : "", " ", t.text)));
  }
  $("timeline-empty").hidden = items.length > 0;
}
function digestBlock(el, title, items) {
  clear(el);
  el.append(h("h3", { text: `${title} (${items.length})` }));
  el.append(items.length ? h("ul", {}, items.slice(0, 6).map((t) => h("li", {}, h("span", { class: "muted", text: fmtTime(t.at) + " " }), (t.pr ? `#${t.pr} · ` : "") + t.text)))
    : h("p", { class: "empty", text: "Không có hoạt động." }));
}
function renderDigest(d) {
  $("digest-summary").textContent = d.summary;
  const now = new Date(), cut = new Date(now); cut.setHours(8, 0, 0, 0);
  const start = new Date(cut); start.setDate(start.getDate() - 1); start.setHours(18, 0, 0, 0);
  const at = (t) => new Date(t.at);
  digestBlock($("digest-night"), "Đêm qua", d.recent.filter((t) => at(t) >= start && at(t) < cut));
  digestBlock($("digest-today"), "Hôm nay", d.recent.filter((t) => at(t) >= cut));
}
function renderBacklog(data) {
  if (data) state.backlog = data.entries;
  for (const k of ["spec-gap", "scope"]) {
    const tab = $(`tab-${k}`);
    tab.setAttribute("aria-selected", String(k === state.backlogKind));
    tab.querySelector(".count").textContent = state.backlog.filter((e) => e.kind === k).length;
  }
  const ul = $("backlog"); clear(ul);
  const rows = state.backlog.filter((e) => e.kind === state.backlogKind);
  for (const e of rows) ul.append(h("li", {}, h("span", { class: "muted", text: `PR #${e.pr} · ${e.date} · ` }), e.text));
  if (!rows.length) ul.append(h("li", { class: "empty", text: "Backlog trống." }));
}

// ---- settings ----------------------------------------------------------------------
function renderSettings(vals) {
  const form = $("settings");
  if (!state.settingsLoaded) {
    state.settingsLoaded = true;
    for (const [key, label, type] of SETTINGS) {
      const input = type === "textarea" ? h("textarea", { rows: "3", id: `set-${key}` }) : h("input", { type, id: `set-${key}`, ...(type === "number" ? { min: "1", step: "1", inputmode: "numeric" } : {}) });
      input.dataset.key = key;
      input.addEventListener("input", () => { input.dataset.dirty = "1"; });
      const save = h("button", { class: "btn", type: "button", text: "Lưu", title: `Lưu ${key}`, "aria-label": `Lưu cài đặt ${label}` });
      save.onclick = async () => {
        let v = input.value;
        if (type === "number") { v = Number(v); if (!Number.isInteger(v) || v < 1) { toast("Hãy nhập số nguyên dương.", true); return; } }
        if (await act([save], "/api/settings", { key, value: v }, () => `Đã lưu ${label}.`)) delete input.dataset.dirty;
        save.disabled = false;
      };
      form.append(h("div", { class: "srow" }, h("label", { for: `set-${key}`, text: label }), input, save));
    }
  }
  for (const [key] of SETTINGS) {
    const el = $(`set-${key}`), v = vals[key];
    if (el && !el.dataset.dirty && document.activeElement !== el) el.value = v == null ? "" : String(v);
  }
}

// ---- refresh loop ------------------------------------------------------------------
async function refresh() {
  if (state.busy) return;
  state.busy = true;
  try {
    const tl = state.tlPr ? `/api/timeline?pr=${encodeURIComponent(state.tlPr)}` : "/api/timeline";
    const [ov, items, prs, ws, tls, dg, bl, st] = await Promise.all(
      ["/api/overview", "/api/items", "/api/prs", "/api/workers", tl, "/api/digest", "/api/backlog", "/api/settings"].map((p) => api(p).catch((e) => e)));
    const auth = [ov, items, prs].find((r) => r instanceof ApiErr && r.status === 401);
    if (auth) { setBanner("Phiên hết hạn hoặc thiếu token. Mở lại dashboard bằng link in ra từ `autopilot web`."); return; }
    const down = [ov, items, prs].find((r) => r instanceof ApiErr);
    setBanner(down ? down.message : "");
    if (!(ov instanceof Error)) renderOverview(ov);
    if (!(prs instanceof Error)) renderPrs(prs);
    if (!(items instanceof Error)) renderItems(items);
    if (!(ws instanceof Error)) renderWorkers(ws);
    else { clear($("workers")); $("workers-empty").hidden = false; $("workers-empty").textContent = ws.message; }
    if (!(tls instanceof Error)) renderTimeline(tls);
    if (!(dg instanceof Error)) renderDigest(dg);
    if (!(bl instanceof Error)) renderBacklog(bl);
    if (!(st instanceof Error)) renderSettings(st);
    $("updated").textContent = `Cập nhật lúc ${new Date().toLocaleTimeString("vi-VN")}`;
  } finally { state.busy = false; }
}
function schedule() {
  clearTimeout(state.timer);
  if (document.hidden) return;
  state.timer = setTimeout(async () => { await refresh(); schedule(); }, POLL_MS);
}

async function init() {
  if (new URLSearchParams(location.search).has("token")) history.replaceState(null, "", location.pathname); // drop token from the URL (cookie is set)
  let projects = [];
  try { projects = await api("/api/projects"); } catch (e) { setBanner(e.message); }
  const sel = $("project");
  projects.forEach((p) => sel.append(h("option", { value: p, text: p })));
  let saved = null; try { saved = localStorage.getItem("autopilot.project"); } catch { /* storage unavailable */ }
  state.project = projects.includes(saved) ? saved : projects[0] || null;
  if (state.project) sel.value = state.project;
  sel.onchange = () => {
    state.project = sel.value; state.settingsLoaded = false; clear($("settings")); clear($("decisions-list"));
    try { localStorage.setItem("autopilot.project", state.project); } catch { /* ignore */ }
    refresh();
  };
  $("toggle-router").onclick = (ev) => { const b = ev.currentTarget; const resume = b.dataset.action === "resume";
    act([b], resume ? "/api/resume" : "/api/pause", {}, () => (resume ? "Đã cho router chạy lại." : "Đã tạm dừng router.")); };
  $("copy-all").onclick = () => state.prs.length ? copyText(state.prs.map((p) => p.url).join("\n"), `${state.prs.length} link PR`) : toast("Chưa có PR nào.", true);
  $("tl-filter").onchange = (ev) => { state.tlPr = ev.target.value; refresh(); };
  document.querySelectorAll(".tabs button").forEach((b) => { b.onclick = () => { state.backlogKind = b.dataset.kind; renderBacklog(); }; });
  $("qa-form").onsubmit = async (ev) => { ev.preventDefault();
    const name = $("qa-name").value.trim(), text = $("qa-text").value;
    if (await act([ev.submitter], "/api/qa", { name, text }, () => `Đã lưu QA "${name}".`)) { $("qa-name").value = ""; $("qa-text").value = ""; }
    ev.submitter.disabled = false; };
  const issue = $("push-issue");
  issue.oninput = () => { $("push-btn").textContent = `Đẩy quyết định lên issue #${issue.value || "…"}`; };
  $("push-form").onsubmit = async (ev) => { ev.preventDefault();
    const n = Number(issue.value);
    if (!Number.isInteger(n) || n < 1) { toast("Số issue phải là số nguyên dương.", true); return; }
    if (!confirm(`Đẩy các quyết định mới lên issue #${n} trên GitHub?`)) return;
    await act([ev.submitter], "/api/push-decisions", { issue: n }, (r) => `Đã đẩy quyết định lên issue #${n}.`);
    ev.submitter.disabled = false; };
  document.addEventListener("visibilitychange", () => { if (document.hidden) clearTimeout(state.timer); else { refresh(); schedule(); } });
  await refresh(); schedule();
}
init();
