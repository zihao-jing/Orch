/* Orch dashboard. Plain JavaScript, no build step, no outside resources. Every text from a user or an agent is put
   into the page as text (h() and textContent only), never as HTML. */
(function () {
  "use strict";
  const L = window.OrchLogic;
  const LOST_AFTER = 4000;  // ms without the board before the lost banner
  const $ = id => document.getElementById(id);

  let S = null;                 // the last /api/state
  let offset = 0;               // server time minus browser time, seconds
  let old = null;               // an earlier run being read ({id, state})
  let es = null, tries = 0, connectedOnce = false;     // the event stream
  const writing = new Map();     // one live reply per agent; never part of the message ledger
  const turnActivity = new Map(); // latest turn for each agent
  const openDeliveries = new Map();  // keep a reply open when its live row is replaced
  const openMsgs = new Set(), openFolds = new Set(), pages = new Map();
  const detail = new Map();
  let timeline = { rows: [], remaining: 0, limit: L.MESSAGE_PAGE, busy: false, queued: false };
  let headVersion = "";
  let who = "manager";          // the talk panel's recipient
  let seenSeq = null, modalRefresh = null;
  let goldText = null, auditText = null, auditNotes = null, goldEdit = false, notesEdit = false, lastSide = 0;

  // ---------------------------------------------------------------------------------------------- small tools
  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* no storage: fine */ } },
  };
  // agents whose lines are hidden; new agents show
  let hiddenWho = new Set((() => { try { return JSON.parse(store.get("orch-message-hidden") || "[]"); } catch (e) { return []; } })());
  let drafts = {};
  try { drafts = JSON.parse(window.sessionStorage.getItem("orch-drafts") || "{}"); } catch (e) { drafts = {}; }
  function saveDrafts() { try { window.sessionStorage.setItem("orch-drafts", JSON.stringify(drafts)); } catch (e) { /* ignore */ } }
  function clearDraft(k) { delete drafts[k]; saveDrafts(); }
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const nowSrv = () => Date.now() / 1000 + offset;

  function add(e, kids) {
    [].concat(kids).forEach(k => {
      if (Array.isArray(k)) add(e, k);
      else if (k !== null && k !== undefined && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
    });
  }
  // a nested list of nodes, text and empties as a flat list of nodes
  function flat(list) {
    const out = [];
    [].concat(list).forEach(k => {
      if (Array.isArray(k)) out.push(...flat(k));
      else if (k !== null && k !== undefined && k !== false) out.push(k.nodeType ? k : document.createTextNode(String(k)));
    });
    return out;
  }
  // h("div", {class: "x", on: {click: f}, data: {a: 1}}, children...)  text only, never HTML
  function h(tag, a, ...kids) {
    const e = document.createElement(tag);
    Object.keys(a || {}).forEach(k => {
      const v = a[k];
      if (k === "class") e.className = v;
      else if (k === "on") Object.keys(v).forEach(ev => e.addEventListener(ev, v[ev]));
      else if (k === "data") Object.keys(v).forEach(d => { e.dataset[d] = v[d]; });
      else if (k === "value") e.value = v;
      else if (v === true) e.setAttribute(k, "");
      else if (v !== false && v !== null && v !== undefined) e.setAttribute(k, String(v));
    });
    add(e, kids);
    return e;
  }

  // the text of a user or an agent as small Markdown, built as nodes
  function spanNodes(list) {
    return list.map(s => s.t === "code" ? h("code", {}, s.s) : s.t === "b" ? h("b", {}, s.s) : s.t === "i" ? h("i", {}, s.s)
      : s.t === "a" ? h("a", { href: s.href, target: "_blank", rel: "noopener noreferrer" }, s.s) : s.s);
  }
  function md(text) {
    const box = h("div", { class: "md" });
    mdInto(box, L.markdown(text));
    return box;
  }
  function mdInto(box, blocks) {
    blocks.forEach(b => {
      if (b.t === "code") box.append(h("pre", {}, b.s));
      else if (b.t === "hr") box.append(h("hr"));
      else if (b.t === "quote") { const q = h("blockquote"); mdInto(q, b.blocks); box.append(q); }
      else if (b.t === "h") box.append(h("h4", {}, spanNodes(b.c)));
      else if (b.t === "ul" || b.t === "ol") box.append(h(b.t, {}, b.items.map(i => h("li", {}, spanNodes(i)))));
      else if (b.t === "table") {
        const cell = (tag, c, k) => h(tag, b.align[k] ? { style: "text-align:" + b.align[k] } : {}, spanNodes(c));
        box.append(h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, b.head.map((c, k) => cell("th", c, k)))),
          h("tbody", {}, b.rows.map(r => h("tr", {}, r.map((c, k) => cell("td", c, k))))))));
      }
      else {
        const p = h("p");
        b.lines.forEach((ln, i) => { if (i) p.append(h("br")); add(p, spanNodes(ln)); });
        box.append(p);
      }
    });
  }

  // rebuild a part of the page only when its data changed; keep the focus and the typed text
  function paint(el, sig, build) {
    if (el._sig === sig) return false;
    el._sig = sig;
    const ctl = "button,input,textarea,select,summary";
    const a = document.activeElement, all = Array.prototype.slice.call(el.querySelectorAll(ctl));
    const at = a && el.contains(a) ? all.indexOf(a) : -1;
    const sel = at >= 0 && typeof a.selectionStart === "number" ? [a.selectionStart, a.selectionEnd] : null;
    el.replaceChildren(...flat(build()));
    if (at >= 0) {
      const n = el.querySelectorAll(ctl)[at];
      if (n && n.tagName === a.tagName) { n.focus(); if (sel) { try { n.setSelectionRange(sel[0], sel[1]); } catch (e) { /* ignore */ } } }
    }
    return true;
  }

  // a text field whose content is kept in `drafts`, so an update never loses it
  function draft(key, tag, attrs) {
    const e = h(tag, attrs);
    e.value = drafts[key] || "";
    e.addEventListener("input", () => { drafts[key] = e.value; saveDrafts(); });
    return e;
  }
  function draftSelect(key, options, dflt) {
    const e = h("select", {}, options.map(o => h("option", { value: o[0] }, o[1])));
    const v = key in drafts ? drafts[key] : dflt;
    e.value = options.some(o => o[0] === v) ? v : (options[0] ? options[0][0] : "");
    e.addEventListener("change", () => { drafts[key] = e.value; saveDrafts(); });
    return e;
  }
  function draftCheck(key, label) {
    const c = h("input", { type: "checkbox" });
    c.checked = !!drafts[key];
    c.addEventListener("change", () => { drafts[key] = c.checked; saveDrafts(); });
    return h("label", { class: "chk" }, c, label);
  }

  function toast(text) {
    if (!text) return;
    const t = h("div", {}, text);
    $("toast").append(t);
    setTimeout(() => t.remove(), 6000);
  }
  const listLimit = key => pages.get(key) || L.LIST_PAGE;
  function listMore(key, remaining, render) {
    if (!remaining) return null;
    return h("button", { type: "button", class: "link more-list", title: remaining + " more items",
      on: { click: () => { pages.set(key, listLimit(key) + L.LIST_PAGE); render(); } } }, "Show more");
  }
  function busy(b, on) { if (b) { b.disabled = on; if (on) b.setAttribute("aria-busy", "true"); else b.removeAttribute("aria-busy"); } }

  // ------------------------------------------------------------------------------------------------ dialogs
  function dismissOnBackdrop(layer, close) {
    let startedOutside = false;
    function outside(ev) {
      if (ev.target !== layer) return false;
      if (layer.tagName !== "DIALOG") return true; // Talk's backdrop is a separate element.
      const rect = layer.getBoundingClientRect();
      return ev.clientX < rect.left || ev.clientX >= rect.right || ev.clientY < rect.top || ev.clientY >= rect.bottom;
    }
    layer.addEventListener("pointerdown", ev => { startedOutside = ev.button === 0 && outside(ev); });
    layer.addEventListener("pointercancel", () => { startedOutside = false; });
    layer.addEventListener("click", ev => {
      const dismiss = startedOutside && outside(ev);
      startedOutside = false;
      if (dismiss) close();
    });
  }
  // buttons: [{label, value, cls}] -> the value pressed, or the value a body control gave to close(); null when
  // closed otherwise. body may be a function(close). A second dialog opens over the first (a question in a window).
  function modal(title, body, buttons, refresh) {
    const sfx = $("dlg").open ? "2" : "", d = $("dlg" + sfx);
    return new Promise(resolve => {
      $("dlg-h" + sfx).textContent = title;
      const close = v => d.close(String(v));
      $("dlg-b" + sfx).replaceChildren(...flat(typeof body === "function" ? body(close) : body));
      $("dlg-a" + sfx).replaceChildren(...buttons.map(b => h("button", { type: "button", class: b.cls || "", on: { click: () => close(b.value) } }, b.label)));
      if (!sfx) modalRefresh = refresh || null;
      d.returnValue = "";
      const done = () => {
        d.removeEventListener("close", done);
        if (!sfx) modalRefresh = null;
        const hit = buttons.find(b => String(b.value) === d.returnValue);
        resolve(hit ? hit.value : (d.returnValue || null));
      };
      d.addEventListener("close", done);
      d.showModal();
    });
  }
  const confirmBox = (title, text, ok, cls) =>
    modal(title, h("p", {}, text), [{ label: "Cancel", value: false }, { label: ok, value: true, cls: cls || "pri" }]);
  async function choose(reply) {
    const bs = reply.choices.map((c, i) => ({ label: c.label, value: i, cls: i === 0 ? "pri" : "" }));
    bs.unshift({ label: "Cancel", value: -1 });
    const v = await modal("Please choose", h("p", {}, reply.shown || ""), bs);
    return v === null || v < 0 ? null : reply.choices[v];
  }

  // ------------------------------------------------------------------------------------------------ commands
  async function submit(body, btn) {
    busy(btn, true);
    try {
      let reply = null;
      for (let i = 0; i < 4 && !reply; i++) {   // the same id each time: a repeated command does nothing
        try {
          const r = await fetch("api/command", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
          const j = await r.json().catch(() => ({}));
          if (r.status >= 500 && i < 3) { await sleep(L.backoff(i)); continue; }
          reply = r.ok || j.ok !== undefined ? j : { ok: false, shown: j.error || "Orch did not accept that." };
        } catch (e) { if (i < 3) await sleep(L.backoff(i)); }
      }
      if (!reply) { toast("Orch cannot be reached. What you typed is kept. Try again."); return null; }
      if (reply.choices && reply.choices.length) {
        const c = await choose(reply);
        busy(btn, false);
        return c ? submit(L.applyChoice(body, c, L.newId()), btn) : reply;
      }
      if (reply.queued) toast("Sent to the Orch that runs this project.");
      else if (reply.shown) toast(reply.shown); else if (reply.ok === false) toast(reply.error || "That did not work.");
      loadState();
      return reply;
    } finally { busy(btn, false); }
  }
  function cmd(type, fields, btn) { return submit(Object.assign({ type: type, id: L.newId() }, fields || {}), btn); }
  const act = (type, fields) => ev => cmd(type, fields, ev.currentTarget);

  // ---------------------------------------------------------------------------------------------- loading
  async function getJson(path) {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  }
  function setLost(on) { $("lost").hidden = !on; }
  // the banner only when the board stays unreachable for a few seconds: a stream that ends and reopens is not a loss
  let lostTimer = 0;
  function lostSoon() { if (!lostTimer) lostTimer = setTimeout(() => { lostTimer = 0; setLost(true); }, LOST_AFTER); }
  function found() { clearTimeout(lostTimer); lostTimer = 0; setLost(false); }
  let loading = false, again = false;
  async function loadState() {
    if (loading) { again = true; return; }
    loading = true;
    try {
      const j = await getJson("api/state");
      S = j; offset = (j.now || Date.now() / 1000) - Date.now() / 1000;
      found();
      if (!old) {
        render();
        const version = JSON.stringify(S.messages);
        if (headVersion !== version) { headVersion = version; loadTimeline(); }
        loadSide(false);
      }
    } catch (e) { lostSoon(); }
    loading = false;
    if (again) { again = false; loadState(); }
  }
  function connect() {
    if (es) es.close();
    try { es = new EventSource("api/events"); } catch (e) { lostSoon(); setTimeout(connect, L.backoff(tries++)); return; }
    es.onopen = () => {
      const reconn = connectedOnce;
      connectedOnce = true;
      tries = 0;
      found();
      loadState();
      if (reconn && !old && S) syncWriting(true);
    };
    es.addEventListener("state", () => loadState());
    es.addEventListener("live", ev => { try { onLive(JSON.parse(ev.data)); } catch (e) { /* ignore a bad line */ } });
    es.addEventListener("bye", () => connect());  // the server ends each stream after a while: open the next one now
    es.onerror = () => { es.close(); lostSoon(); setTimeout(connect, L.backoff(tries++)); };
  }
  function onLive(d) {
    if (d.k !== "text" && d.k !== "tool") return;
    const reply = writing.get(d.agent);
    if (reply) addWritingEvents(reply, [d]);
  }

  // --------------------------------------------------------------------------------------------- rendering
  const agents = () => (S ? S.agents : {});
  const nm = id => L.agentName(agents(), id);
  const ROLE = { manager: "Manager", critic: "Critic", worker: "Worker", monitor: "Monitor" };

  function agentList() {
    const rank = { manager: 0, critic: 1, worker: 2, monitor: 3 };
    return Object.values(agents()).filter(a => a.lifecycle !== "deleted" && a.role !== "auditor")
      .sort((x, y) => ((rank[x.role] ?? 9) - (rank[y.role] ?? 9)) || x.id.localeCompare(y.id, undefined, { numeric: true }));
  }
  const messages = () => L.sortedMessages(S.messages);

  function render() {
    if (!S) return;
    $("oldnote").hidden = true;
    ["header-actions", "message-filters", "resources", "changems", "addworker", "notices"].forEach(id => { $(id).hidden = false; });
    syncWriting();
    renderHeader(); renderNotices(); renderNeeds(); renderTeam(); renderProgress(); renderMessages();
    renderTalk(); renderSide();
    if (modalRefresh) modalRefresh();
  }

  function renderHeader() {
    const name = (S.run && S.run.project) || "Orch board";
    $("title").textContent = name; document.title = name + " - Orch board";
    const stopped = agentList().some(a => a.stopped_by_user && a.lifecycle === "open");
    const bits = (status, label) => h("span", { class: "state-label" }, h("span", { class: "dot status-" + status, "aria-hidden": "true" }), label);
    const viewing = !!(S.viewer && !S.viewer.orch_running);
    paint($("runstate"), JSON.stringify([S.finished, S.paused, stopped, viewing]), () => [
      viewing ? h("span", { class: "state-label", title: "Shown with orch serve and no Orch runs it: it can be read; start it with orch run --resume to change it" },
        h("span", { class: "dot status-closed", "aria-hidden": "true" }), S.finished ? "Finished · saved run, not running" : "Saved run, not running")
      : S.finished ? bits("finished", "Finished") : S.paused ? bits("waiting_job", "Paused") : bits("writing static", "Running"),
      stopped ? bits("failed", "Agents stopped") : null]);
    const nb = $("nightbtn"); nb.classList.toggle("on", !!S.night); nb.setAttribute("aria-pressed", S.night ? "true" : "false");
    $("stopbtn").textContent = stopped ? "Start agents" : "Stop agents";
    $("stopbtn").dataset.mode = stopped ? "start" : "stop";
    $("stopbtn").classList.toggle("danger-text", !stopped);
    $("stopbtn").setAttribute("title", stopped ? "Resume agents in their saved sessions" : "Stop all agents and their jobs after confirmation");
  }

  // Notices stay in the timeline. Toast fresh ones once, including just-finished runs.
  const seenNotices = new Set();
  function renderNotices() {
    const notes = messages().filter(L.isNotice);
    notes.forEach(m => {
      if (seenNotices.has(m.id)) return;
      seenNotices.add(m.id);
      if (nowSrv() - m.created < 60) toast(L.messageText(m));
    });
    const terms = Object.keys(S.terminals || {});
    const migration = notes.find(m => m.text.key === "note_migrated");
    paint($("notices"), JSON.stringify([terms.map(a => [a, (agents()[a] || {}).waiting]), migration && migration.id]), () => [
      terms.map(a => h("div", { class: "banner info terminal-banner" }, h("div", { class: "t" },
        "A terminal is open on " + nm(a) + ". " + ((agents()[a] || {}).waiting || 0) + " message(s) are waiting for it."),
        h("button", { type: "button", class: "link", on: { click: act("TerminalTakeBack", { agent: a }) } }, "Take back"))),
      migration ? h("div", { class: "banner info terminal-banner" }, h("div", { class: "t" }, L.messageText(migration)),
        h("button", { type: "button", class: "link", on: { click: act("ConfirmMigration", {}) } }, "Remove old files")) : null]);
  }

  // gates: everything that waits for you
  function msList(list) {
    return h("ol", {}, list.map(m => h("li", {}, h("b", {}, m.title), m.text ? " - " + L.preview(m.text, 200) : "")));
  }
  function planText(text, milestones) {
    const box = h("div", { class: "md" });
    const blocks = L.markdown(text), titles = new Set(milestones.map(m => m.title));
    for (const block of blocks) {
      if (block.t !== "ol") continue;
      block.items = block.items.map(item => {
        const [first, ...rest] = item;
        if (first?.t !== "text") return item;
        const title = first.s.split(/\s+[—–-]\s+|:\s+/, 1)[0].trimEnd();
        if (!titles.has(title)) return item;
        return [{ t: "b", s: title }, { t: "text", s: first.s.slice(title.length) }, ...rest];
      });
    }
    mdInto(box, blocks);
    return box;
  }
  function reviewText(text) {
    const box = h("div", { class: "md" }), blocks = L.markdown(text), latest = new Map();
    const words = parts => parts.map(x => x.s).join("").trim().toLowerCase();
    const keys = blocks.map((b, i) => {
      if (b.t !== "table" || !b.rows.length) return null;
      const head = b.head.map(words), score = head.findIndex(x => /^(score|rating|评分|得分|分数)$/.test(x));
      if (score < 1) return null;
      const key = JSON.stringify([head, b.rows.map(row => [words(row[0]), words(row[score])])]);
      latest.set(key, i);
      return key;
    });
    mdInto(box, blocks.filter((_, i) => keys[i] === null || latest.get(keys[i]) === i));
    return box;
  }
  function gateCard(g) {
    const key = "gate:" + g.id, box = h("div", { class: "gate" });
    box.append(h("div", { class: "k" }, L.GATE_KIND[g.kind] || "Needs you", g.agent ? " - " + nm(g.agent) : ""));
    const w = L.shownOf(g.words);
    if (g.kind === "plan") {
      // The original reply, parsed milestones, and generated notice describe the same plan.
      if (g.text) box.append(planText(g.text, g.new || []));
      else if ((g.new || []).length) box.append(msList(g.new));
      else if (w) box.append(md(w));
    } else {
      if (w) box.append(md(w));
      if (g.text) box.append(g.kind === "milestone" ? reviewText(g.text) : md(g.text));
    }
    if (g.summary) box.append(h("div", { class: "rec" }, md(g.summary)));
    if (g.kind === "milestone_change") {
      box.append(h("div", { class: "side2" }, h("div", {}, h("h3", {}, "Now"), msList(g.old || [])), h("div", {}, h("h3", {}, "Proposed"), msList(g.new || []))));
    }
    const send = (how, ev) => cmdGate(g, how, ev.currentTarget, key);
    if (g.options.length) {
      box.append(h("div", { class: "opts" }, g.options.map((o, i) => h("button", { type: "button", on: { click: ev => send({ option: i }, ev) } }, inlineCode(o)))));
    }
    const text = g.kind === "operational" ? null : draft(key, "textarea", { rows: 1, "aria-label": "Your answer", placeholder: g.kind === "question" ? "Or write your own answer" : "Your comment (needed to ask for changes)" });
    if (text) box.append(h("div", { class: "row" }, text));
    const acts = h("div", { class: "row" });
    if (g.kind === "question") acts.append(h("button", { type: "button", class: "pri", on: { click: ev => { if (!(drafts[key] || "").trim()) { toast("Write an answer or pick an option."); return; } send({ text: drafts[key].trim() }, ev); } } }, "Send answer"));
    else if (["plan", "milestone", "milestone_change"].indexOf(g.kind) >= 0) {
      acts.append(h("button", { type: "button", class: "ok", on: { click: ev => send({ approve: true, text: (drafts[key] || "").trim() }, ev) } }, "Approve"),
        h("button", { type: "button", on: { click: ev => { if (!(drafts[key] || "").trim()) { toast("Write what should change."); return; } send({ approve: false, text: drafts[key].trim() }, ev); } } }, "Ask for changes"));
    } else acts.append(h("button", { type: "button", class: "pri", on: { click: ev => send({ text: (drafts[key] || "").trim() }, ev) } }, g.kind === "pause" ? "Continue" : "Close"));
    box.append(acts);
    return box;
  }
  async function cmdGate(g, how, btn, key) {
    const r = await submit(Object.assign(L.gateAnswer(g, how), { id: L.newId() }), btn);
    if (r && r.ok) clearDraft(key);
  }
  function renderNeeds() {
    const open = L.openGates(S.gates), sec = $("needs");
    sec.hidden = !open.length;
    paint(sec, JSON.stringify(open), () => open.length ? [h("h2", {}, "Needs you"), open.map(gateCard)] : []);
  }

  // team
  function termCommand(a) { return "orch terminal " + ((S.run && S.run.project) || "") + " " + a.id; }
  async function copyCommand(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied: " + text + ". Paste it in a terminal.");
    } catch (e) {
      toast("Copy this command and paste it in a terminal: " + text);
    }
  }
  function terminalLink(a) {
    const command = termCommand(a);
    return h("button", { type: "button", class: "link term", title: command,
      "aria-label": "Copy the terminal command for " + nm(a.id), on: { click: () => copyCommand(command) } }, "Terminal");
  }
  const openAgents = new Set((() => { try { return JSON.parse(store.get("orch-team-open") || "[]"); } catch (e) { return []; } })());
  let teamFolded = store.get("orch-team-folded") === "1";
  function agentSummaryState(a) {
    if (a.status === "failed") return "Failed";
    if (a.switch && a.switch.step && ["done", "failed"].indexOf(a.switch.step) < 0) return "Switching model";
    if (a.status === "writing") return "Writing";
    if (a.status === "waiting_job") return "Job running";
    if (a.status === "closed") return "Closed";
    return "Idle";
  }
  function activityAge(at, now) {
    return now - at < 60 ? L.duration(now - at) + " ago" : L.ago(at, now);
  }
  function lastActivity(a) {
    const at = a.last_output || a.writing_since;
    return at ? h("span", { class: "agent-last-active", data: { ago: at }, title: "Last activity" },
      activityAge(at, nowSrv())) : null;
  }
  function assignmentText(a) {
    if (!a.assignment) return "";
    const original = (S.messages || {})[a.assignment.message_id];
    return (original && L.messageText(original)) || a.assignment.line;
  }
  function assignmentTag() {
    return h("span", { class: "assignment-tag", role: "img", "aria-label": "Last assignment", title: "Last assignment" }, "→");
  }
  function activeJobs(id) {
    return Object.values(S.jobs || {}).filter(j => j.agent === id &&
      (j.state === "running" || (j.state === "lost" && !j.woke && !j.stopped_by_user)))
      .sort((x, y) => x.started - y.started);
  }
  function agentStatusDot(a, jobs) {
    const running = a.status === "waiting_job" ||
      ((a.status === "writing" || a.status === "idle") && jobs.some(j => j.state === "running"));
    return h("span", { class: "dot status-" + a.status + (running ? " job-running" : ""), "aria-hidden": "true" });
  }
  function agentModel(a) { return a.actual_model || (a.choice && (a.choice.model || a.choice.label)) || ""; }
  function agentCard(a) {
    const name = nm(a.id), role = ROLE[a.role] || "", status = L.STATUS[a.status] || "", model = agentModel(a);
    const jobs = activeJobs(a.id), job = jobs[jobs.length - 1];
    const opened = openAgents.has(a.id), panelId = "agent-summary-" + a.id;
    const panel = h("div", { class: "agent-body", id: panelId });
    panel.hidden = !opened;
    const fold = h("button", { type: "button", class: "agent-sum", title: name + ": " + status + (model ? " · " + model : ""),
      "aria-expanded": String(opened), "aria-controls": panelId, on: { click: () => {
        const open = panel.hidden;
        if (open) openAgents.add(a.id); else openAgents.delete(a.id);
        store.set("orch-team-open", JSON.stringify(Array.from(openAgents)));
        fold.setAttribute("aria-expanded", String(open));
        panel.hidden = !open;
      } } },
        h("span", { class: "agent-fold-mark", "aria-hidden": "true" }),
        agentStatusDot(a, jobs),
        h("span", { class: "agent-name" }, name),
        role.toLowerCase() !== name.toLowerCase().replace(/[ -]?\d+$/, "") ? h("span", { class: "agent-role" }, role) : null,
        h("span", { class: "agent-state" }, agentSummaryState(a), lastActivity(a)),
        a.waiting ? h("span", { class: "agent-count", title: a.waiting + " message(s) wait for " + name }, String(a.waiting)) : null);
    const tags = L.agentTags(a, S), assignment = assignmentText(a);
    panel.append(h("div", { class: "agent-meta" },
      model ? h("span", { class: "model-name", title: model }, L.shortModel(model)) : null,
      h("span", { class: "agent-when" }, status)));
    if (tags.length) panel.append(h("div", { class: "agent-line tags" }, tags.join(" · ")));
    if (assignment) panel.append(h("div", { class: "agent-line inline-assignment" },
      assignmentTag(), h("span", { class: "assignment-preview" }, assignment)));
    if (a.status === "failed" && a.failed_why) panel.append(h("div", { class: "agent-line danger-text" }, a.failed_why));
    panel.append(agentActions(a));
    return h("div", { class: "agent status-row-" + a.status + (a.status === "closed" ? " closed" : "") },
      fold,
      job ? h("div", { class: "agent-job-preview" }, h("span", { class: "label" }, jobs.length === 1 ? "Job" : jobs.length + " jobs"),
        h("span", { class: "job-preview" }, job.command || job.label || job.id)) : null,
      h("button", { type: "button", class: "icon agent-details-button", title: "Open " + name + " details",
        "aria-label": "Open " + name + " details", "aria-haspopup": "dialog", on: { click: () => openAgent(a.id) } }, "…"),
      panel);
  }
  function agentDetails(a, assignment, jobs, refresh, close) {
    const t = nowSrv(), status = L.STATUS[a.status] || "", model = agentModel(a);
    const body = h("div");
    body.append(h("div", { class: "agent-meta" },
      model ? h("span", { class: "model-name" }, model) : null,
      h("span", { class: "agent-when" }, status, (a.last_output || a.writing_since) ? [" · ", lastActivity(a)] : null)));
    const tags = L.agentTags(a, S);
    if (tags.length) body.append(h("div", { class: "agent-line tags" }, tags.join(" · ")));
    if (assignment) body.append(h("div", { class: "agent-line assignment" },
      assignmentTag(), md(assignment)));
    if (a.status === "failed" && a.failed_why) body.append(h("div", { class: "agent-line danger-text" }, a.failed_why));
    if (a.waiting) {
      const why = (a.holds || []).map(x => ({ terminal: "a terminal is open", "switch": "model is being switched", turn: "it is writing", send_failed: "sending failed", stopped: "stopped by you" }[x])).filter(Boolean);
      body.append(h("div", { class: "agent-line" }, a.waiting + " message(s) waiting", why.length ? " · " + why.join(", ") : ""));
    }
    const jobKey = "jobs:" + a.id, shownJobs = L.latest(jobs, listLimit(jobKey));
    shownJobs.rows.forEach(j => body.append(h("div", { class: "job-detail" },
      h("h4", {}, "Job · ", h("span", { data: j.state === "running" ? { jobstart: j.started } : null }, L.jobStatus(j, t))),
      h("p", { class: "detail-hint" }, "Job ID: ", h("code", {}, j.id),
        j.started ? " · Started " + new Date(j.started * 1000).toLocaleString() : ""),
      h("pre", {}, j.command || j.label || "No command recorded."),
      !j.command && j.label && j.label.length >= 160 ?
        h("p", { class: "detail-hint" }, "This job has only a saved preview; the original command may be longer.") : null)));
    add(body, listMore(jobKey, shownJobs.remaining, refresh));
    body.append(agentActions(a, close));
    return body;
  }
  function agentActions(a, close) {
    const name = nm(a.id), controls = h("div", { class: "agent-actions" });
    if (a.role !== "monitor" && a.lifecycle === "open") controls.append(h("button", { type: "button", class: "link", title: "Talk to " + name,
      on: { click: () => close ? close("talk") : openTalk(a.id) } }, "Talk"));
    controls.append(h("button", { type: "button", class: "link", title: "Choose the model for " + name,
      on: { click: () => close ? close("model") : openModel(a.id) } }, "Model"));
    if (a.role !== "monitor" && a.lifecycle === "open") controls.append(terminalLink(a));
    if (a.role === "worker") L.workerActions(a).forEach(type => controls.append(h("button", { type: "button",
      class: type === "WorkerDelete" ? "link danger-link" : "link",
      title: { WorkerClose: "Close this worker and keep its session", WorkerReopen: "Reopen this worker in its saved session", WorkerDelete: "Permanently remove this worker from the team" }[type],
      on: { click: ev => workerAct(type, a, ev.currentTarget) } },
      { WorkerClose: "Close worker", WorkerReopen: "Reopen worker", WorkerDelete: "Delete worker" }[type])));
    if (a.lifecycle === "open") controls.append(a.stopped_by_user
      ? h("button", { type: "button", class: "link", title: "Resume this agent in its saved session", on: { click: act("StartAgents", { agents: [a.id] }) } }, "Start")
      : h("button", { type: "button", class: "link danger-link", title: "Stop this agent and its jobs", on: { click: act("StopAgents", { agents: [a.id] }) } }, "Stop"));
    return controls;
  }
  async function openAgent(id) {
    const box = h("div", { class: "agent-detail" });
    let close;
    const refresh = () => {
      const a = agents()[id], jobs = activeJobs(id), assignment = a && assignmentText(a);
      paint(box, JSON.stringify([a, assignment, jobs, listLimit("jobs:" + id), a && L.agentTags(a, S)]), () => a && a.lifecycle !== "deleted"
        ? agentDetails(a, assignment, jobs, refresh, close) : h("p", {}, "This agent is no longer on the team."));
    };
    const result = await modal(nm(id) + " details", done => { close = done; refresh(); return box; },
      [{ label: "Close", value: "" }], refresh);
    if (result === "talk") openTalk(id);
    if (result === "model") openModel(id);
  }
  function railDot(a) {
    const at = a.last_output || a.writing_since;
    const name = nm(a.id), st = agentSummaryState(a) + (at ? " · " + activityAge(at, nowSrv()) : "");
    return h("button", { type: "button", class: "rail-agent", title: name + ": " + st, "aria-label": name + ": " + st,
      "aria-haspopup": "dialog", on: { click: () => openAgent(a.id) } },
      agentStatusDot(a, activeJobs(a.id)), h("span", { class: "rail-initial" }, railLabel(a)));
  }
  function railLabel(a) {
    const n = (a.id.match(/(\d+)$/) || [])[1];
    if (a.role === "worker") return "W" + (n || "1");
    return { manager: "Mgr", critic: "Cri", monitor: "Mon" }[a.role] || a.id.slice(0, 3);
  }
  function inlineCode(text) {
    // `code` in a short label (a gate's option) as code, the rest as text
    return String(text).split(/(`[^`]+`)/).map(x => x.length > 2 && x[0] === "`" && x[x.length - 1] === "`" ? h("code", {}, x.slice(1, -1)) : x);
  }
  function setTeamFolded(on) {
    teamFolded = on;
    store.set("orch-team-folded", on ? "1" : "0");
    $("team")._sig = null;
    renderTeam();
  }
  async function workerAct(type, a, btn) {
    if (type === "WorkerDelete" && !(await confirmBox("Delete " + nm(a.id) + "?", "It cannot be opened again. Its messages stay in the record.", "Delete", "danger"))) return;
    cmd(type, { agent: a.id }, btn);
  }
  function renderTeam() {
    const list = agentList(), t = Math.floor(nowSrv() / 60);
    $("workspace").classList.toggle("team-folded", teamFolded);
    const fb = $("teamfold");
    fb.setAttribute("aria-expanded", teamFolded ? "false" : "true");
    fb.title = teamFolded ? "Show the team" : "Fold the team into a narrow strip";
    fb.setAttribute("aria-label", fb.title);
    fb.textContent = teamFolded ? "‹" : "›";
    const sig = JSON.stringify([teamFolded, list, S.jobs, t, teamFolded ? [] : list.map(a => [assignmentText(a), L.agentTags(a, S)])]);
    paint($("team"), sig, () => teamFolded ? list.map(railDot) : list.map(agentCard));
    if (!list.length) $("team").textContent = "No agents yet.";
  }

  // progress: milestones and the current checklist
  // the checklist shown under the milestones: the current one's, or the one picked on a card
  let ckPick = null;
  function checklistList(ck) {
    if (ck.error === "missing") return h("p", { class: "mute" }, "The critic has not written this checklist yet.");
    if (ck.error) return h("p", { class: "mute" }, "The critic's checklist cannot be read.");
    if (!(ck.steps || []).length) return h("p", { class: "mute" }, "The checklist has no steps yet.");
    return h("ul", { class: "ck" }, ck.steps.map(step => [h("li", { class: "stepname" }, step.title),
      step.items.map(item => h("li", { class: item[0] ? "done" : "open" },
        h("span", { class: "bx", title: item[0] ? "Complete" : "To do" }, item[0] ? "✓" : "○"), h("span", {}, spanNodes(L.markdown(item[1])[0] ? L.markdown(item[1])[0].lines[0] : []))))]));
  }
  let ckOpen = store.get("orch-checklist-open") === "1";
  let msOpen = store.get("orch-msdetails-open") === "1";
  function foldBox(cls, open, store_key, title, note, setOpen) {
    const box = h("details", { class: "ck-box " + cls, open }, h("summary", { class: "ck-head" }, h("h3", {}, title), note ? h("span", { class: "mute small" }, note) : null));
    box.addEventListener("toggle", () => { setOpen(box.open); store.set(store_key, box.open ? "1" : "0"); });
    return box;
  }
  function renderProgress() {
    const ms = S.milestones || [], ck = S.checklist || {};
    const cur = ms.find(m => m.status === "current");
    const shown = ms.find(m => m.id === ckPick) || cur || ms.filter(m => m.status === "done").pop() || null;
    paint($("progress"), JSON.stringify([ms, ck, shown && shown.id]), () => {
      if (!ms.length) return h("p", { class: "empty" }, "No milestones yet.");
      const [d, n] = L.progress(ck);
      const pick = m => { ckPick = m.id; renderProgress(); };
      const out = [h("ol", { class: "ms" }, ms.map((m, i) => h("li", { class: "mscard ms-" + m.status + (shown && shown.id === m.id ? " picked" : ""),
          tabindex: "0", role: "button", "aria-pressed": shown && shown.id === m.id ? "true" : "false", title: "Show this milestone's details and checklist below",
          on: { click: ev => { if (!ev.target.closest("button")) pick(m); }, keydown: ev => { if ((ev.key === "Enter" || ev.key === " ") && ev.target === ev.currentTarget) { ev.preventDefault(); pick(m); } } } },
          h("div", { class: "ms-top" }, h("span", { class: "ms-num", "aria-hidden": "true" }, m.status === "done" ? "✓" : String(i + 1)),
            h("span", { class: "ms-status" }, L.MS_STATUS[m.status] || ""),
            h("span", { class: "grow" }),
            m.status === "done" ? h("button", { type: "button", class: "link ms-reopen", title: "Open this milestone again",
              on: { click: act("ReopenMilestone", { milestone_id: m.id }) } }, "Reopen") : null,
            m.score !== null && m.score !== undefined && m.status === "done" ? h("span", { class: "ms-score", title: "Score out of 5" }, m.score + " / 5") : null),
          h("div", { class: "t" }, m.title),
          m.status === "current" && n ? [h("div", { class: "bar" }, h("i", { style: "width:" + Math.round(100 * d / n) + "%" })), h("div", { class: "mute small" }, d + " of " + n + " done")] : null,
          null)))];
      if (shown) {
        if (shown.text || (shown.rubric || []).length) {
          const box = foldBox("ms-details", msOpen, "orch-msdetails-open", "Details", shown.title, v => { msOpen = v; });
          box.append(h("div", { class: "ck-body" }, shown.text ? md(shown.text) : null,
            (shown.rubric || []).length ? [h("div", { class: "ms-sub" }, "Rubric"), h("ul", {}, shown.rubric.map(r => h("li", {}, r)))] : null));
          out.push(box);
        }
        const box = foldBox("ck-list", ckOpen, "orch-checklist-open", "Checklist",
          shown.title + (shown.status === "current" && n ? " · " + d + " of " + n + " done" : ""), v => { ckOpen = v; });
        if (shown.status === "current") box.append(checklistList(ck));
        else if (shown.checklist_path) {
          const holder = h("div", {}, h("p", { class: "mute" }, "Loading…"));
          box.append(holder);
          cached("api/checklist?milestone=" + encodeURIComponent(shown.id))
            .then(c => holder.replaceChildren(checklistList(c)))
            .catch(() => holder.replaceChildren(h("p", { class: "mute" }, "Could not load this checklist.")));
        } else box.append(h("p", { class: "mute" }, "No checklist yet."));
        out.push(box);
      }
      return out;
    });
  }


  // messages
  const messageFilter = () => ({ q: $("msearch").value, off: [...hiddenWho], notes: $("orch-notes").checked });

  function syncWriting(reconnect = false) {
    const current = new Set();
    for (const a of agentList()) {
      const turn = (S.turns || {})[a.id];
      if (!turn?.id) continue;
      current.add(a.id);
      let reply = turnActivity.get(a.id);
      if (!reply || reply.turn !== turn.id) {
        if (writing.has(a.id)) finishWriting(writing.get(a.id));
        reply = { id: "writing:" + turn.id, turn: turn.id, sender: a.id, created: turn.started || a.writing_since,
          chunks: new Map(), tools: new Map(), toolLines: [], body: "",
          timelineView: { open: true, inputOpen: true }, talkView: { open: true, inputOpen: true } };
        turnActivity.set(a.id, reply);
      }
      if (a.status === "writing" && turn.status === "running" && !reply.delivered) {
        writing.set(a.id, reply);
        if (reply.input === undefined) loadWritingInput(reply);
        if (reconnect || !reply.loaded) loadWritingEvents(reply);
      } else if (writing.has(a.id)) {
        finishWriting(reply);
      }
      reply.model = a.actual_model || (a.choice || {}).label;
    }
    for (const id of turnActivity.keys()) {
      if (current.has(id)) continue;
      if (writing.has(id)) finishWriting(writing.get(id));
      turnActivity.delete(id);
    }
  }

  function finishWriting(reply) {
    writing.delete(reply.sender);
    if (reply.timelineView.paint && reply.timelineView.open && reply.body) openDeliveries.set(reply.sender, reply.turn);
  }

  async function loadWritingInput(reply) {
    if (reply.inputLoading) return;
    reply.inputLoading = true;
    try {
      const turn = await getJson("api/turn?id=" + encodeURIComponent(reply.turn));
      reply.input = turn.input || "";
    } catch (e) { /* retry when the state next updates */ }
    finally { reply.inputLoading = false; }
    if (!old && writing.get(reply.sender) === reply) {
      paintWritingViews(reply, "paintInput");
      if ($("msearch").value.trim()) renderMessages();
    }
  }

  async function loadWritingEvents(reply) {
    if (reply.loading) return;
    reply.loading = true;
    try {
      const path = "api/live?agent=" + encodeURIComponent(reply.sender) + "&tail=500";
      const items = [];
      let before = 0;
      do {
        const page = await getJson(path + (before ? "&before=" + before : ""));
        if (writing.get(reply.sender) !== reply) return;
        items.push(...page.items);
        const oldest = page.items[0];
        before = page.more && oldest?.turn === reply.turn ? oldest.n : 0;
      } while (before > 0);
      addWritingEvents(reply, items);
      reply.loaded = true;
    } catch (e) { /* incoming live fragments still work; retry on the next state refresh */ }
    finally { reply.loading = false; }
  }

  function writingToolLine(label, call) {
    let line = call || label || "Tool call";
    // Most adapters save the tool name and structured arguments; Codex saves the command itself.
    const split = call.indexOf("\n");
    if (split > 0) {
      try {
        const args = JSON.parse(call.slice(split + 1));
        if (args && typeof args === "object") {
          const name = call.slice(0, split);
          const fields = args.command || [args.pattern || args.query, args.filePath || args.file_path || args.path || args.url].filter(Boolean).join(" · ");
          const what = fields || (Object.keys(args).length ? JSON.stringify(args) : "");
          line = what ? name + ": " + what : label || name;
        }
      } catch (e) { /* a multiline command has no JSON arguments to decode */ }
    }
    line = line.replace(/\s+/g, " ").trim();
    return line.length > 2000 ? line.slice(0, 2000) + "…" : line;
  }

  function addWritingEvents(reply, items) {
    if (writing.get(reply.sender) !== reply) return;
    let textChanged = false, toolsChanged = false;
    for (const item of items) {
      if (item.turn !== reply.turn || !Number.isInteger(item.n)) continue;
      if (item.k === "text" && !reply.chunks.has(item.n)) {
        reply.chunks.set(item.n, item.s);
        textChanged = true;
      } else if (item.k === "tool") {
        const id = item.tool_id ? "id:" + item.tool_id : item.n, prev = reply.tools.get(id);
        const first = Math.min(item.n, prev?.first ?? item.n);
        if (!prev || item.n > prev.n || first < prev.first) {
          const newer = !prev || item.n >= prev.n;
          reply.tools.set(id, { first, n: newer ? item.n : prev.n, label: (newer ? item.s : prev.label) || item.s,
            call: (newer ? item.call || prev?.call : prev.call || item.call) || "" });
          toolsChanged = true;
        }
      }
    }
    if (!textChanged && !toolsChanged) return;
    if (textChanged) {
      reply.body = [...reply.chunks].sort((a, b) => a[0] - b[0]).map(x => x[1]).join("");
    }
    if (toolsChanged) {
      reply.toolLines = [...reply.tools.values()].sort((a, b) => a.first - b.first).map(x => writingToolLine(x.label, x.call));
    }
    if (!old) {
      paintWritingViews(reply, "paint");
      if ($("msearch").value.trim()) renderMessages();
    }
  }

  function paintWritingViews(reply, method) {
    reply.timelineView[method]?.();
    if ($("talk").hidden || who !== reply.sender) return;
    const log = $("talklog"), near = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    reply.talkView[method]?.();
    if (near) log.scrollTop = log.scrollHeight;
  }

  function writingRow(reply, view = reply.timelineView) {
    const preview = h("span", { class: "pv" });
    const tools = h("div", { class: "writing-tools" }), body = h("div", { class: "writing-text" });
    const row = h("details", { class: "msg writing-msg", data: { message: reply.id } },
      h("summary", {}, h("span", { class: "tm", title: "Started writing" }, L.stamp(reply.created, nowSrv())),
        h("span", { class: "route" }, h("b", {}, nm(reply.sender))), preview,
        h("span", { class: "st-slot" }, h("span", { class: "st wait" }, "Writing")),
        reply.model ? h("span", { class: "by", title: reply.model }, L.shortModel(reply.model)) : null),
      h("div", { class: "fb" }, writingInput(reply, view), tools, body));
    row.open = view.open;
    let rendered = null, renderedTools = null;
    view.paint = () => {
      preview.textContent = row.open ? "" : L.preview(reply.body || reply.toolLines[reply.toolLines.length - 1]) || "Waiting for text…";
      if (row.open && renderedTools !== reply.toolLines) {
        const top = tools.scrollTop, follow = tools.scrollHeight - top - tools.clientHeight < 20;
        tools.replaceChildren(...reply.toolLines.map(line => h("div", { class: "writing-tool-line", title: line },
          h("span", {}, "Tool"), line)));
        tools.hidden = !reply.toolLines.length;
        tools.scrollTop = follow ? tools.scrollHeight : top;
        renderedTools = reply.toolLines;
      }
      if (row.open && rendered !== reply.body) {
        body.replaceChildren(reply.body ? md(reply.body) : h("p", { class: "empty" }, "Waiting for text…"));
        rendered = reply.body;
      }
    };
    row.addEventListener("toggle", () => { view.open = row.open; view.paint(); });
    view.paint();
    return row;
  }

  function writingInput(reply, view) {
    const body = h("div");
    const section = h("details", { class: "writing-input" }, h("summary", {}, "Input"), body);
    section.open = view.inputOpen;
    view.paintInput = () => {
      const empty = reply.input === undefined ? "Loading input…" : "No input was recorded for this turn.";
      body.replaceChildren(reply.input ? h("pre", {}, reply.input) : h("p", { class: "empty" }, empty));
    };
    section.addEventListener("toggle", () => { view.inputOpen = section.open; });
    view.paintInput();
    return section;
  }

  function cached(path) {
    if (!detail.has(path)) {
      detail.set(path, getJson(path).catch(error => { detail.delete(path); throw error; }));
      if (detail.size > 180) detail.delete(detail.keys().next().value);
    }
    return detail.get(path);
  }

  function fold(key, label, build, help) {
    const body = h("div", { class: "fold-body" });
    const node = h("details", { class: "message-fold" }, h("summary", { class: "link" }, label), body);
    let loaded = false, loading = false;
    async function fill() {
      if (loaded || loading) return;
      loading = true;
      body.textContent = "Loading…";
      try {
        body.replaceChildren(...flat([help ? h("p", { class: "detail-hint" }, help) : null, await build()]));
        loaded = true;
      } catch (e) { body.textContent = "Could not load this. Close and reopen to try again."; }
      loading = false;
    }
    node.addEventListener("toggle", () => {
      if (node.open) { openFolds.add(key); fill(); }
      else openFolds.delete(key);
    });
    if (openFolds.has(key)) { node.open = true; fill(); }
    return node;
  }

  const toNames = (named, list) => list.map(r => L.agentName(named, r)).join(", ");
  function messageBody(m, run, named) {
    const meta = m.written_by || {};
    const sized = Number.isFinite(meta.input_chars) && Number.isFinite(meta.output_chars);
    const facts = [meta.model ? "Written by " + L.shortModel(meta.model) : "",
      sized ? L.characterSize(meta.input_chars) + " characters in, " + L.characterSize(meta.output_chars) + " out" : ""].filter(Boolean);
    const renderText = !run && m.turn && Object.values(S.gates || {}).some(g => g.kind === "milestone" && g.source_turn === m.turn)
      ? reviewText : md;
    // one output that wrote several messages: each with whom it went to and how it stands
    const texts = m.parts ? m.parts.map(p => h("div", { class: "part" },
        h("div", { class: "part-head" }, h("span", {}, "→ " + toNames(named, [p.recipient].concat(p.also || []))),
          h("span", { class: "st " + L.messageTone(p) + (L.messageException(p) ? "" : " plain") }, L.messageException(p) || L.messageStatus(p))),
        renderText(L.messageText(p))))
      : [renderText(L.messageText(m))];
    const fb = h("div", { class: "fb" }, texts,
      facts.length ? h("p", { class: "detail-hint", title: meta.model || "" }, facts.join(" · ")) : null);
    if (m.turn) {
      const getTurn = () => cached("api/turn?id=" + encodeURIComponent(m.turn) + (run ? "&run=" + encodeURIComponent(run) : ""));
      const sender = L.agentName(named, m.sender);
      const possessive = ((named[m.sender] || {}).display || "").startsWith("the ") ? "the " + sender.toLowerCase() + "'s" : sender + "'s";
      fb.append(fold(m.id + ":input", "Input to " + possessive + " turn that wrote this", async () => {
        const turn = await getTurn();
        return [h("pre", {}, turn.input || "No input was recorded."),
          fold(m.id + ":system", "System instructions for " + sender, () => h("pre", {}, turn.system_prompt || "No instructions were recorded."))];
      }), fold(m.id + ":output", possessive[0].toUpperCase() + possessive.slice(1) + " whole reply", async () => {
        const turn = await getTurn();
        return md(turn.output || turn.text || "Nothing was recorded.");
      }, "Everything that turn wrote, including text sent in other messages."));
    }
    return fb;
  }

  function msgRow(m, run, named = agents()) {
    const parts = m.parts || [m];
    const level = L.messageLevel(m, named), note = L.isNotice(m);
    const awaitsYou = parts.some(p => p.recipient === "user" && L.awaitsAnswer(p));
    const cls = ["msg", "level-" + level, parts.some(p => p.recipient === "user") ? "touser" : "", awaitsYou ? "awaiting" : "", note ? "notice-row" : m.orch_note ? "housekeeping" : ""];
    const meta = m.written_by || {}, odd = parts.find(p => L.messageException(p)) || m;
    const exception = L.messageException(odd), hint = parts.length > 1 ? "" : L.statusHint(m);
    const rank = r => ({ user: 0, manager: 1, critic: 2 })[r] ?? 3;
    const to = [...new Set([m.recipient].concat(parts.flatMap(p => [p.recipient].concat(p.also || []))))]
      .sort((a, b) => rank(a) - rank(b));
    const allWorkers = to.length > 1 && to.every(r => (named[r] || {}).role === "worker");
    const toText = allWorkers ? to.length + " workers" : to.length > 2 ? L.agentName(named, to[0]) + " +" + (to.length - 1) : toNames(named, to);
    const fullRoute = L.agentName(named, m.sender) + " → " + toNames(named, to) + (parts.length > 1 ? " (" + parts.length + " messages)" : "");
    const size = Number.isFinite(meta.input_chars) && Number.isFinite(meta.output_chars);
    const d = h("details", { class: cls.filter(Boolean).join(" "), data: { message: m.id } },
      h("summary", { title: [fullRoute, hint].filter(Boolean).join(" · ") },
        h("span", { class: "tm", title: m.ended ? "Finished" : "Sent" }, L.stamp(m.ended || m.created, nowSrv())),
        h("span", { class: "route" }, h("b", {}, L.agentName(named, m.sender)), h("span", { class: "to" }, " → " + toText)),
        h("span", { class: "pv" }, parts.length > 1 ? h("span", { class: "count", title: parts.length + " messages from this output" }, parts.length) : null,
          L.preview(L.messageText(m))),
        h("span", { class: "st-slot" }, exception || L.messageStatus(odd) ? h("span", { class: "st " + L.messageTone(odd) + (exception ? "" : " plain"),
          title: hint }, exception || L.messageStatus(odd)) : null),
        h("span", { class: "by", title: meta.model ? "Written by " + meta.model : "" }, meta.model ? L.shortModel(meta.model) : ""),
        size && meta.input_chars > 30000 ? h("span", { class: "sz large", title: meta.input_chars.toLocaleString() +
          " input characters: more than 30,000" }, L.characterSize(meta.input_chars) + " in") : null));
    function expand() {
      if (!d._body) { d._body = messageBody(m, run, named); d.append(d._body); }
    }
    if (openMsgs.has(m.id)) { d.open = true; expand(); }
    d.addEventListener("toggle", () => {
      if (d.open) { openMsgs.add(m.id); expand(); }
      else openMsgs.delete(m.id);
    });
    return d;
  }

  function patchRows(box, list, build, signature) {
    if (!list.length) { paint(box, "empty", () => h("p", { class: "empty" }, "No messages match.")); return; }
    box._sig = null;
    const previous = new Map(Array.from(box.children).map(e => [e.dataset.message, e]));
    list.forEach((m, i) => {
      const sig = signature(m), previousRow = previous.get(m.id);
      const existing = previousRow && previousRow._signature === sig;
      const row = existing ? previousRow : build(m);
      row._signature = sig;
      if (row !== box.children[i]) box.insertBefore(row, box.children[i] || null);
    });
    Array.from(box.children).slice(list.length).forEach(row => row.remove());
  }

  function renderMessages() {
    const who = [["user", "You", "user"]].concat(agentList().map(a => [a.id, nm(a.id), a.role]));
    paint($("who"), JSON.stringify([who, [...hiddenWho]]), () => who.map(([id, name, role]) => h("button", { type: "button",
      class: "level-toggle who-" + role, "aria-pressed": String(!hiddenWho.has(id)),
      title: (id === "user" ? "Your messages and Orch's notes to you" : "What " + name + " wrote, and Orch's messages to it"),
      on: { click: () => {
        if (hiddenWho.has(id)) hiddenWho.delete(id); else hiddenWho.add(id);
        store.set("orch-message-hidden", JSON.stringify([...hiddenWho]));
        $("who")._sig = null; renderMessages(); resetTimeline();
      } } }, name)));
    const { rows, remaining, busy, ready } = timeline;
    const shown = rows.length, total = shown + remaining;
    $("message-pages").hidden = remaining <= 0;
    $("older").disabled = $("show-all").disabled = busy;
    $("show-all").hidden = remaining <= L.MESSAGE_PAGE;
    $("remaining").textContent = remaining + " older";
    $("message-count").textContent = ready ? (remaining ? shown + " of " + total : shown + (shown === 1 ? " message" : " messages")) : "Loading…";
    // the newest on top; older pages load below
    const stored = L.groupReplies(rows.slice().reverse());
    for (const m of stored) {
      const reply = writing.get(m.sender);
      if (reply && reply.turn === m.turn) {
        reply.delivered = true;
        finishWriting(reply);
      }
      if (m.turn && openDeliveries.get(m.sender) === m.turn) {
        openMsgs.add(m.id);
        openDeliveries.delete(m.sender);
      }
    }
    const q = $("msearch").value.trim().toLowerCase();
    const matches = text => !q || String(text || "").toLowerCase().includes(q);
    const extra = [];
    for (const reply of writing.values()) {
      if (hiddenWho.has(reply.sender)) continue;
      if (matches(reply.body) || matches(reply.input) || reply.toolLines.some(matches)) extra.push(reply);
    }
    if (!ready && !extra.length) return;
    const list = extra.length ? extra.concat(stored).sort((a, b) => (b.ended || b.created || 0) - (a.ended || a.created || 0)) : stored;
    patchRows($("messages"), list, m => m.chunks ? writingRow(m) : msgRow(m), m =>
      m.chunks ? JSON.stringify([m.id, nm(m.sender), m.model]) :
        JSON.stringify([m, nm(m.sender), nm(m.recipient), L.messageLevel(m, agents())]));
  }

  async function requestTimeline(filter, limit, before) {
    let rows = [], remaining = 0;
    do {
      const page = await getJson("api/messages?" + L.pageQuery({ ...filter, limit: Math.min(limit - rows.length, 500), before }));
      if (!page.messages.length) { remaining = 0; break; }
      rows = [...page.messages, ...rows];
      remaining = Number(page.remaining) || 0;
      before = rows[0].seq;
    } while (remaining && rows.length < limit);
    return { rows, remaining };
  }

  async function loadTimeline(olderCount = 0) {
    const view = timeline;
    if (old || !S) return;
    if (view.busy) { view.queued = true; return; }
    view.busy = true;
    renderMessages();
    try {
      const before = olderCount && view.rows.length ? view.rows[0].seq : 0;
      const result = await requestTimeline(messageFilter(), olderCount || view.limit, before);
      if (timeline !== view || old) return;
      view.rows = before ? [...result.rows, ...view.rows] : result.rows;
      view.remaining = result.remaining;
      if (olderCount) view.limit += olderCount;
      view.ready = true;
    } catch (e) { if (timeline === view && !old) toast("Could not load messages."); }
    finally {
      view.busy = false;
      if (timeline === view && !old) {
        renderMessages();
        if (view.queued) { view.queued = false; loadTimeline(); }
      }
    }
  }

  function resetTimeline() {
    if (old) return;
    timeline = { rows: [], remaining: 0, limit: L.MESSAGE_PAGE, busy: false, queued: false };
    $("messages").replaceChildren();
    $("messages")._sig = null;
    loadTimeline();
  }

  function wireFilters() {
    $("orch-notes").checked = store.get("orch-message-notes") !== "0";
    $("orch-notes").addEventListener("change", e => {
      store.set("orch-message-notes", e.target.checked ? "1" : "0");
      resetTimeline();
    });
    $("msearch").value = store.get("orch-message-search") || "";
    let timer = 0;
    $("msearch").addEventListener("input", () => {
      store.set("orch-message-search", $("msearch").value);
      clearTimeout(timer);
      timer = setTimeout(resetTimeline, 250);
    });
    $("older").addEventListener("click", () => loadTimeline(Math.min(L.MESSAGE_PAGE, timeline.remaining)));
    $("show-all").addEventListener("click", () => loadTimeline(timeline.remaining));
  }

  // the talk panel
  function talkable() {
    return agentList().filter(a => a.role !== "monitor" && a.lifecycle === "open").map(a => a.id);
  }
  function openTalk(id) {
    if (id) who = id;
    $("talk").hidden = false;
    $("talkbtn").setAttribute("aria-expanded", "true");
    renderTalk(true);
    markSeen();
    $("talkin").focus();
  }
  function closeTalk() { $("talk").hidden = true; $("talkbtn").setAttribute("aria-expanded", "false"); $("talkbtn").focus(); }
  function markSeen() { const l = messages(); if (l.length) { seenSeq = l[l.length - 1].seq; store.set("orch-seen", String(seenSeq)); } renderBadge(); }
  function renderBadge() {
    if (!S) return;
    if (seenSeq === null) { const v = store.get("orch-seen"); const l = messages(); seenSeq = v !== null ? Number(v) : (l.length ? l[l.length - 1].seq : 0); }
    const n = messages().filter(m => m.recipient === "user" && m.seq > seenSeq && m.sender !== "orch").length;
    paint($("talkbadge"), String(n) + $("talk").hidden, () => n && $("talk").hidden ? h("span", { class: "badge" }, String(n)) : []);
  }
  function renderTalk(force) {
    const ids = talkable();
    if (ids.indexOf(who) < 0) who = ids[0] || "manager";
    paint($("talkwho"), JSON.stringify(ids.map(nm)), () => ids.map(i => h("option", { value: i }, nm(i))));
    if ($("talkwho").value !== who) $("talkwho").value = who;
    if (force || $("talkin").dataset.who !== who) { $("talkin").dataset.who = who; $("talkin").value = drafts["talk:" + who] || ""; }
    renderBadge();
    if ($("talk").hidden) return;
    const a = S.agents[who] || {}, disc = S.discussions[who];
    const mgrId = Object.values(S.agents).find(x => x.role === "manager");
    const all = L.talkLog(messages(), who, mgrId && mgrId.id);
    const talkKey = "talk-history:" + who;
    const { rows: log, remaining } = L.latest(all, listLimit(talkKey));
    const current = writing.get(who);
    const live = current && !all.some(m => m.sender === who && m.turn === current.turn) ? current : null;
    const sig = JSON.stringify([log.map(m => [m.id, m.status, m.drop]), remaining, who, !!disc, disc && disc.phase, live?.id, live?.model]);
    const box = $("talklog"), near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    const changed = paint(box, sig, () => log.length || live ? [listMore(talkKey, remaining, () => {
      const height = box.scrollHeight, top = box.scrollTop;
      renderTalk();
      box.scrollTop = top + box.scrollHeight - height;
    }), log.map(m => {
      const me = m.sender === "user";
      return h("div", { class: "cm" + (me ? " me" : "") + (L.isNotice(m) ? " note" : "") },
        h("div", { class: "w" }, (me ? "You" : nm(m.sender)) + " - " + L.stamp(m.created, nowSrv()) + (me ? " - " + L.messageStatus(m) : "")),
        h("div", { class: "b" }, md(L.messageText(m))));
    }), live ? writingRow(live, live.talkView) : null] : h("p", { class: "empty" }, "Nothing yet. Write below."));
    if (force && live && !changed) {
      live.talkView.paintInput();
      live.talkView.paint();
    }
    const fresh = force || box._who !== who;   // just opened, or another agent: start at the newest
    box._who = who;
    if (changed && (near || fresh)) box.scrollTop = box.scrollHeight;
    if (fresh) setTimeout(() => { box.scrollTop = box.scrollHeight; }, 0);   // again once the window is laid out
    $("talk-h").textContent = "Talk to";
    const isLead = a.role === "manager" || a.role === "critic";
    paint($("talkacts"), JSON.stringify([isLead, !!disc, disc && disc.phase, who, S.run && S.run.project]), () => [
      isLead && disc ? [h("button", { type: "button", class: "sm ok", on: { click: applyDiscussion } }, "Apply discussion"),
        h("button", { type: "button", class: "sm", on: { click: endDiscussion } }, "End without applying")] : null,
      isLead && !disc ? h("button", { type: "button", class: "sm", title: "Messages to other agents wait until you apply the discussion", on: { click: act("DiscussionStart", { agent: who }) } }, "Discuss") : null,
      a.id && (isLead || a.role === "worker") ? terminalLink(a) : null,
    ]);
    paint($("talkinfo"), JSON.stringify([!!disc, disc && disc.phase, who]), () =>
      disc ? h("div", { class: "banner info" }, disc.phase === "applying" ? "Applying the discussion. The agent is answering."
        : "A discussion is open. What " + nm(who) + " writes to others is a draft until you apply it.") : []);
  }
  async function applyDiscussion() {
    const t = draft("apply:" + who, "textarea", { rows: 3, "aria-label": "Your words with it" });
    const v = await modal("Apply the discussion", [h("p", {}, "The agent sends what you decided. You can add a few words."), t], [{ label: "Cancel", value: false }, { label: "Apply", value: true, cls: "ok" }]);
    if (v) { const r = await cmd("DiscussionApply", { agent: who, text: t.value.trim() }); if (r && r.ok) clearDraft("apply:" + who); }
  }
  async function endDiscussion() {
    if (await confirmBox("End the discussion?", "Drafts are dropped. Messages that came in stay.", "End it")) cmd("DiscussionEnd", { agent: who });
  }
  async function sendTalk() {
    const t = $("talkin").value.trim();
    if (!t) return;
    const r = await cmd("Chat", { agent: who, text: t }, $("talksend"));
    if (r && r.ok !== false) { clearDraft("talk:" + who); $("talkin").value = ""; }
  }

  // model window
  // a new worker: like the first one, or on a model of the catalog
  async function addWorker() {
    const cat = S.catalog || [], like = agentList().find(a => a.role === "worker" && a.lifecycle !== "deleted");
    const likeModel = like ? L.shortModel(like.actual_model || like.choice.model || like.choice.label || "") : "";
    const msel = h("select", { "aria-label": "Model" }, [h("option", { value: "" }, "Same as " + (like ? nm(like.id) : "the first worker") + (likeModel ? " (" + likeModel + ")" : ""))]
      .concat(cat.map(m => h("option", { value: m.id }, m.label || m.model))));
    const effWrap = h("div", { class: "field" });
    const fillEff = () => {
      const ef = msel.value ? L.efforts(cat, msel.value) : [];
      const def = (cat.find(m => m.id === msel.value) || {}).default_effort;
      const es = h("select", { "aria-label": "Effort" }, ef.map(e => h("option", { value: e }, e)));
      if (def && ef.indexOf(def) >= 0) es.value = def;
      effWrap.replaceChildren(...(ef.length ? [h("label", {}, "Effort"), es] : []));
    };
    msel.addEventListener("change", fillEff);
    fillEff();
    const body = h("div", {}, h("div", { class: "field" }, h("label", {}, "Model"), msel), effWrap,
      cat.length ? null : h("p", { class: "mute small" }, "No other models are listed, so it starts like the first worker."));
    const v = await modal("Add a worker", body, [{ label: "Add worker", value: "add", cls: "pri" }, { label: "Cancel", value: "" }]);
    if (v !== "add") return;
    const eff = effWrap.querySelector("select");
    cmd("WorkerAdd", msel.value ? { model_id: msel.value, effort: eff ? eff.value : "" } : {}, $("addworker"));
  }
  function openModel(id) {
    const build = () => {
      const a = S.agents[id]; if (!a) return h("p", {}, "This agent is gone.");
      const k = "model:" + id + ":", cat = S.catalog || [];
      const current = a.actual_model || a.choice.model || a.choice.label || "";
      const body = [h("p", {}, h("b", {}, "Current model "), h("span", { title: current }, L.shortModel(current)),
        a.choice.effort ? " · " + a.choice.effort + " effort" : "")];
      if (a.switch) {
        body.push(h("div", { class: "banner note" }, h("div", { class: "t" }, (a.switch.step === "pending" ? "Switch pending to " : "Switching to ") + (a.switch.target.label || "the new model") +
          (a.switch.step === "pending" ? ". It happens when the agent is idle." : ".") + (a.switch.error ? " " + a.switch.error : "")),
          a.switch.step === "pending" ? h("button", { type: "button", class: "sm", on: { click: act("ForceSwitch", { agent: id }) } }, "Switch now") : null));
      }
      if (cat.length) {
        const msel = draftSelect(k + "model", cat.map(m => [m.id, m.label || m.model]), a.choice.model_id);
        const effWrap = h("div", { class: "field" });
        const fillEff = () => {
          const ef = L.efforts(cat, msel.value);
          effWrap.replaceChildren(...flat(ef.length ? [h("label", {}, "Effort"), draftSelect(k + "effort", ef.map(e => [e, e]), a.choice.effort || (cat.find(m => m.id === msel.value) || {}).default_effort)] : []));
        };
        msel.addEventListener("change", () => { delete drafts[k + "effort"]; fillEff(); });
        fillEff();
        body.push(h("div", { class: "field" }, h("label", {}, "Model"), msel), effWrap,
          h("div", { class: "field" }, draftCheck(k + "force", "Switch now (current work and jobs stop)")),
          h("button", { type: "button", class: "pri", on: { click: async ev => { const r = await cmd("ChooseModel", { agent: id, model_id: msel.value, effort: (effWrap.querySelector("select") || {}).value || "", force: !!drafts[k + "force"] }, ev.currentTarget); if (r && r.ok) { ["model", "effort", "force"].forEach(x => delete drafts[k + x]); saveDrafts(); refresh(); } } } }, "Switch"));
      } else body.push(h("p", { class: "mute" }, "No models are listed yet."));
      if (a.choices.length > 1 || a.choice.name) {
        body.push(h("h3", { style: "margin-top:14px" }, "Backups"));
        a.choices.forEach(c => {
          const cur = ["backend", "provider", "model", "effort", "name"].every(key => (c[key] || "") === (a.choice[key] || ""));
          const name = c.label || L.shortModel(c.model), source = [c.backend, c.provider].filter(Boolean).join(" / ");
          body.push(h("div", { class: "opt" + (cur ? " cur" : "") },
            h("span", { class: "grow", title: c.model }, c.name ? name : "Main · " + name, source ? h("small", { class: "mute model-source" }, source) : null),
            cur ? h("span", { class: "mute small" }, "in use") :
            h("button", { type: "button", class: "sm", on: { click: ev => cmd("ChooseModel", { agent: id, backup: c.name || "", model_id: "", force: !!drafts[k + "force"] }, ev.currentTarget) } }, "Use")));
        });
      }
      return body;
    };
    const holder = h("div");
    const refresh = () => paint(holder, JSON.stringify([S.agents[id], S.catalog]), build);
    modal("Model of " + nm(id), holder, [{ label: "Close", value: true }], refresh);
    refresh();
  }

  // the side cards: golden board, audit
  async function loadSide(force) {
    const t = Date.now();
    if (!force && t - lastSide < 8000) return;
    lastSide = t;
    try {
      if ($("goldcard").open && !goldEdit) goldText = await getJson("api/golden");
      if ($("auditcard").open) {
        if (!notesEdit) auditNotes = await getJson("api/audit_notes");
        auditText = await getJson("api/audit?history=1");
      }
    } catch (e) { /* the connection banner speaks for itself */ }
    renderSide();
  }
  function editor(key, label, getBase, command, done, stop) {
    const ta = draft(key, "textarea", { rows: 12, "aria-label": label });
    if (!(key in drafts)) { ta.value = getBase().text || ""; drafts[key] = ta.value; }
    return h("div", {}, ta, h("div", { class: "row" },
      h("button", { type: "button", class: "pri", on: { click: async ev => { const r = await cmd(command, { text: ta.value, base: getBase().digest || "" }, ev.currentTarget); if (r && r.ok) { clearDraft(key); done(); } } } }, "Save"),
      h("button", { type: "button", on: { click: () => { clearDraft(key); stop(); } } }, "Cancel")));
  }
  function renderSide() {
    paint($("golden"), JSON.stringify([goldText, goldEdit]), () => !goldText ? h("p", { class: "mute" }, "Loading...") : goldEdit
      ? editor("golden", "Golden board", () => goldText, "EditGolden", () => { goldEdit = false; loadSide(true); }, () => { goldEdit = false; renderSide(); })
      : [h("p", { class: "mute small" }, "The manager keeps the rules and decisions every agent works by. Your edit goes to all agents."),
        goldText.text ? md(goldText.text) : h("p", { class: "empty" }, "Empty."),
        h("button", { type: "button", class: "sm", on: { click: () => { goldEdit = true; renderSide(); } } }, "Edit")]);
    paint($("audit"), JSON.stringify([auditText, auditNotes, notesEdit, listLimit("audits")]), () => {
      if (!auditText) return h("p", { class: "mute" }, "Loading...");
      return [h("div", { class: "acts" }, h("button", { type: "button", class: "sm", disabled: !!auditText.running, on: { click: act("AuditRun", {}) } }, auditText.running ? "Audit is running" : "Run audit")),
        auditText.report ? md(auditText.report) : h("p", { class: "empty" }, "No audit yet."),
        (auditText.records || []).slice(1, listLimit("audits")).map(a => fold(
          "audit:" + a.id, "Audit from " + L.stamp(a.at, nowSrv()),
          async () => md((await cached("api/audit?id=" + encodeURIComponent(a.id))).report || "No reply was recorded."))),
        listMore("audits", Math.max(0, (auditText.records || []).length - listLimit("audits")), renderSide),
        h("h3", { style: "margin-top:14px" }, "Your notes on the audit"),
        !auditNotes ? null : notesEdit
          ? editor("notes", "Your notes", () => auditNotes, "AuditNotes", () => { notesEdit = false; loadSide(true); }, () => { notesEdit = false; renderSide(); })
          : [auditNotes.text ? md(auditNotes.text) : h("p", { class: "empty" }, "No notes."), h("button", { type: "button", class: "sm", on: { click: () => { notesEdit = true; renderSide(); } } }, "Edit notes")]];
    });
  }

  // other dialogs
  async function updateOrch() {
    if (await confirmBox("Update Orch?", "Orch takes in the new version and restarts. The agents carry on.", "Update")) cmd("UpdateOrch", {});
  }
  async function changeMilestones() {
    const t = draft("changems", "textarea", { rows: 5, "aria-label": "What should change", placeholder: "What should change in the milestones?" });
    const v = await modal("Change milestones", [h("p", {}, "The manager gets the current list and your words, and proposes a change. You approve it before anything changes."), t],
      [{ label: "Cancel", value: false }, { label: "Ask", value: true, cls: "pri" }]);
    if (v && t.value.trim()) { const r = await cmd("ChangeMilestones", { text: t.value.trim() }); if (r && r.ok) clearDraft("changems"); }
  }
  async function stopStart() {
    if ($("stopbtn").dataset.mode === "start") { cmd("StartAgents", { agents: [] }, $("stopbtn")); return; }
    if (await confirmBox("Stop all agents?", "The agents and their jobs stop. Orch stays open. Start them again when you want.", "Stop agents", "danger")) cmd("StopAgents", { agents: [] }, $("stopbtn"));
  }

  // earlier runs (read only)
  async function runs() {
    let list = [];
    try {
      const current = S && S.run ? S.run.project + "/" + S.run.id : "";
      list = ((await getJson("api/runs")).runs || []).filter(r => r.id !== current);
    } catch (e) { toast("Could not load the runs."); return; }
    const box = h("div");
    const draw = close => {
      const limit = listLimit("earlier-runs");
      box.replaceChildren(...flat(list.length ? [
        list.slice(0, limit).map((r, i) => h("div", { class: "opt" },
          h("span", { class: "grow" }, (r.project || "Run") + " · " + (r.status || "Saved") + " · " + L.stamp(r.started, nowSrv())),
          h("button", { type: "button", class: "sm", on: { click: () => close("run" + i) } }, "Open"))),
        listMore("earlier-runs", list.length - Math.min(limit, list.length), () => draw(close)),
      ] : h("p", { class: "empty" }, "No earlier runs.")));
      return box;
    };
    const v = await modal("Earlier runs", draw, [{ label: "Close", value: "" }]);
    if (typeof v === "string" && /^run\d+$/.test(v)) viewOld(list[Number(v.slice(3))]);
  }
  async function viewOld(r) {
    try { old = await getJson("api/run?id=" + encodeURIComponent(r.id)); } catch (e) { toast("Could not open that run."); return; }
    pages.delete("old-messages");
    renderOld(old, r);
  }
  function renderOld(j, r) {
    const note = $("oldnote"); note.hidden = false;
    note.replaceChildren(document.createTextNode("You are reading an earlier run (" + (r.project || "run") + "). "),
      h("button", { type: "button", class: "link", on: { click: () => {
        old = null;
        ["needs", "team", "progress", "messages", "runstate"].forEach(i => { $(i)._sig = null; });
        render(); loadTimeline();
      } } }, "Back to the current run"));
    $("needs").hidden = true;
    ["header-actions", "message-filters", "resources", "changems", "addworker", "notices", "message-pages"].forEach(id => { $(id).hidden = true; });
    $("title").textContent = r.project || "Earlier run";
    $("runstate").textContent = "Read only";
    const snapshotAgents = j.agents && !Array.isArray(j.agents);
    const team = snapshotAgents ? Object.values(j.agents) : (j.agents || []);
    const items = j.messages ? Object.values(j.messages).sort((a, b) => (a.ended || a.created) - (b.ended || b.created) || a.seq - b.seq) :
      (j.tl_items || []).concat((j.chat || []).map(c => ({ ts: c.ts, from: c.role === "human" ? "You" : (c.who || c.role), to: "", text: c.text })))
        .sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
    paint($("team"), "old" + r.id, () => team.map(a => h("div", { class: "agent" },
      h("h3", {}, snapshotAgents ? L.agentName(j.agents, a.id) : L.plainName(a.called || a.name)),
      h("span", { class: "mute small" }, ROLE[a.role === "verifier" ? "critic" : a.role] || a.role || ""))));
    paint($("progress"), "old" + r.id, () => (j.milestones || j.tl_groups || []).map(g => h("div", { class: "line" }, g.title || g.label || g.key)));
    const { rows, remaining } = L.latest(items, listLimit("old-messages"));
    $("message-count").textContent = rows.length + " of " + items.length;
    paint($("messages"), "old" + r.id + ":" + rows.length, () => items.length ? [
      rows.reverse().map(it => j.messages ? msgRow(it, r.id, j.agents) :
        h("details", { class: "msg" }, h("summary", {}, h("span", { class: "tm" }, String(it.ts || "").slice(11, 16)),
          h("span", { class: "route" }, L.plainName(it.from || "") + (it.to ? " → " + L.plainName(it.to) : "")),
          h("span", { class: "pv" }, L.preview(it.text || it.preview || it.title || ""))),
        h("div", { class: "fb" }, md(it.text || it.preview || it.title || "")))),
      listMore("old-messages", remaining, () => renderOld(j, r)),
    ] : h("p", { class: "empty" }, "Nothing recorded."));
  }

  // ------------------------------------------------------------------------------------------------ wiring
  function wire() {
    for (const d of [$("dlg"), $("dlg2")]) dismissOnBackdrop(d, () => d.close());
    dismissOnBackdrop($("talk"), closeTalk);
    $("talkbtn").addEventListener("click", () => openTalk());
    $("talkclose").addEventListener("click", closeTalk);
    $("talksend").addEventListener("click", sendTalk);
    $("talkwho").addEventListener("change", e => { who = e.target.value; renderTalk(true); markSeen(); });
    $("talkin").addEventListener("input", e => { drafts["talk:" + who] = e.target.value; saveDrafts(); });
    $("talkin").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendTalk(); } });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("talk").hidden && !$("dlg").open && !$("dlg2").open) closeTalk(); });
    document.addEventListener("click", e => document.querySelectorAll("details.menu[open]").forEach(menu => {
      if (!menu.contains(e.target)) menu.open = false;
    }));
    document.addEventListener("keydown", e => {
      if (e.key !== "Escape") return;
      document.querySelectorAll("details.menu[open]").forEach(menu => {
        if (menu.contains(document.activeElement)) menu.querySelector("summary").focus();
        menu.open = false;
      });
    });
    $("nightbtn").addEventListener("click", ev => cmd("SetNight", { on: !(S && S.night) }, ev.currentTarget));
    $("stopbtn").addEventListener("click", stopStart);
    $("updatebtn").addEventListener("click", updateOrch);
    $("runsbtn").addEventListener("click", runs);
    $("addworker").addEventListener("click", addWorker);
    $("teamfold").addEventListener("click", () => setTeamFolded(!teamFolded));
    $("changems").addEventListener("click", changeMilestones);
    $("themebtn").addEventListener("click", () => {
      const r = document.documentElement, dark = r.dataset.theme ? r.dataset.theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
      r.dataset.theme = dark ? "light" : "dark"; store.set("orch-theme", r.dataset.theme);
    });
    const th = store.get("orch-theme"); if (th) document.documentElement.dataset.theme = th;
    wireFilters();
    $("goldcard").addEventListener("toggle", () => { if ($("goldcard").open) loadSide(true); });
    $("auditcard").addEventListener("toggle", () => { if ($("auditcard").open) loadSide(true); });
    setInterval(() => {
      const t = nowSrv();
      document.querySelectorAll("[data-ago]").forEach(e => { e.textContent = activityAge(Number(e.dataset.ago), t); });
    }, 1000);
    setInterval(() => {
      const t = nowSrv();
      document.querySelectorAll("[data-jobstart]").forEach(e => { e.textContent = L.jobStatus({ state: "running", started: Number(e.dataset.jobstart) }, t); });
    }, 15000);
    setInterval(loadState, 20000);
    window.addEventListener("online", () => { connect(); loadState(); });
  }

  wire();
  loadState();
  connect();
})();
