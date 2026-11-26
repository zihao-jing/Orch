/* Orch dashboard. Plain JavaScript, no build step, no outside resources. Every text from a user or an agent is put
   into the page as text (h() and textContent only), never as HTML. */
(function () {
  "use strict";
  const L = window.OrchLogic;
  const $ = id => document.getElementById(id);

  let S = null;                 // the last /api/state
  let offset = 0;               // server time minus browser time, seconds
  let old = null;               // an earlier run being read ({id, state})
  let es = null, tries = 0, connectedOnce = false;     // the event stream
  const live = {}, liveN = {}, liveMore = {}, liveLoaded = new Set();
  const openMsgs = new Set(), openAct = new Set(), openFolds = new Set(), pages = new Map();
  const detail = new Map();
  let timeline = { rows: [], remaining: 0, limit: L.MESSAGE_PAGE, busy: false, queued: false };
  let headVersion = "";
  let levels = new Set(Object.keys(L.LEVELS));
  let who = "manager";          // the talk panel's recipient
  let seenSeq = null, modalRefresh = null;
  let goldText = null, auditText = null, auditNotes = null, goldEdit = false, notesEdit = false, lastSide = 0;

  // ---------------------------------------------------------------------------------------------- small tools
  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* no storage: fine */ } },
  };
  let drafts = {};
  try {
    const saved = JSON.parse(store.get("orch-message-levels"));
    if (Array.isArray(saved)) levels = new Set(saved.filter(x => Object.hasOwn(L.LEVELS, x)));
  } catch (e) { /* use all interaction levels */ }
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
      if (reply.queued) toast(S && S.viewer && S.viewer.orch_running ? "Sent to the Orch that runs this project."
        : "No Orch is running this project, so nothing changes now. The request waits in the run folder until Orch runs it again.");
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
  let loading = false, again = false;
  async function loadState() {
    if (loading) { again = true; return; }
    loading = true;
    try {
      const j = await getJson("api/state");
      S = j; offset = (j.now || Date.now() / 1000) - Date.now() / 1000;
      setLost(false);
      if (!old) {
        render();
        const version = JSON.stringify(S.messages);
        if (headVersion !== version) { headVersion = version; loadTimeline(); }
        loadSide(false);
      }
    } catch (e) { setLost(true); }
    loading = false;
    if (again) { again = false; loadState(); }
  }
  function connect() {
    if (es) es.close();
    try { es = new EventSource("api/events"); } catch (e) { setLost(true); setTimeout(connect, L.backoff(tries++)); return; }
    es.onopen = () => {
      const reconn = connectedOnce;
      connectedOnce = true;
      tries = 0;
      setLost(false);
      loadState();
      if (reconn) {
        const seen = new Set(Object.keys(liveN).concat(Array.from(openAct)));
        seen.forEach(id => loadLive(id, null, id in liveN ? liveN[id] + 1 : 0));
      }
    };
    es.addEventListener("state", () => loadState());
    es.addEventListener("live", ev => { try { onLive(JSON.parse(ev.data)); } catch (e) { /* ignore a bad line */ } });
    es.onerror = () => { es.close(); setLost(true); setTimeout(connect, L.backoff(tries++)); };
  }
  function onLive(d) {
    if (typeof d.n === "number") {
      if (d.agent in liveN && d.n <= liveN[d.agent]) return;
      liveN[d.agent] = d.n;
    }
    const list = live[d.agent] || (live[d.agent] = []);
    list.push(d);
    if (list.length > listLimit("activity:" + d.agent)) { list.shift(); liveMore[d.agent] = true; }
    paintActivity(d.agent);
  }

  function paintActivity(id) {
    const list = live[id] || [], last = list[list.length - 1];
    document.querySelectorAll('[data-live="' + id + '"]').forEach(e => { e.textContent = last ? L.preview(last.s, 110) : ""; });
    document.querySelectorAll('[data-livelog="' + id + '"]').forEach(box => {
      const signature = JSON.stringify([list, liveMore[id]]);
      paint(box, signature, () => [
        liveMore[id] ? h("button", { type: "button", class: "link", on: { click: () => {
          const key = "activity:" + id;
          pages.set(key, listLimit(key) + L.LIST_PAGE);
          loadLive(id, "older");
        } } }, "Show more") : null,
        list.length ? h("div", { class: "livelog" }, list.map(x => (x.k === "tool" ? "> " : "") + x.s + "\n"))
          : h("p", { class: "empty" }, "No activity recorded."),
      ]);
    });
  }
  async function loadLive(id, mode, fromN) {
    const paging = mode === "older";
    const list = live[id] || [];
    const from = fromN !== undefined ? fromN : (id in liveN ? liveN[id] + 1 : 0);
    const tail = paging || (mode === "open" && !liveLoaded.has(id));
    const params = tail ? "&tail=" + L.LIST_PAGE + (paging && list.length ? "&before=" + list[0].n : "") : "&from=" + from;
    try {
      const j = await getJson("api/live?agent=" + encodeURIComponent(id) + params);
      const seen = live[id] || [];
      const previous = j.reset || (!tail && from === 0) ? [] :
        tail && !paging ? seen.filter(x => x.n >= j.next) : seen;
      const byIndex = new Map(previous.concat(j.items || []).map(x => [x.n, x]));
      const sorted = [...byIndex.values()].sort((a, b) => a.n - b.n);
      const page = L.latest(sorted, listLimit("activity:" + id));
      live[id] = page.rows;
      if (tail && !paging) liveLoaded.add(id);
      if (!paging && (j.next > 0 || j.reset)) {
        liveN[id] = j.reset ? j.next - 1 : Math.max(liveN[id] ?? -1, j.next - 1);
      }
      if (tail || j.reset) liveMore[id] = !!j.more;
      if (page.remaining) liveMore[id] = true;
      paintActivity(id);
    } catch (e) {
      if (mode) toast("Could not load activity.");
    }
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
      viewing ? h("span", { class: "state-label", title: "Shown with orch serve: no Orch runs it, so buttons only leave requests for the next Orch" },
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
  function gateCard(g) {
    const key = "gate:" + g.id, box = h("div", { class: "gate" });
    box.append(h("div", { class: "k" }, L.GATE_KIND[g.kind] || "Needs you", g.agent ? " - " + nm(g.agent) : ""));
    const w = L.shownOf(g.words);
    if (w) box.append(md(w));
    if (g.text) box.append(md(g.text));
    if (g.summary) box.append(h("div", { class: "rec" }, md(g.summary)));
    if (g.kind === "plan" && (g.new || []).length) box.append(msList(g.new));
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
    return h("button", { type: "button", class: "link", title: command, on: { click: () => copyCommand(command) } }, "Open in terminal");
  }
  function menu(title) {
    const panel = h("div", { class: "menu-items" });
    const summary = h("summary", { class: "icon", "aria-label": title, title }, "⋯");
    const node = h("details", { class: "menu" }, summary, panel);
    panel.addEventListener("click", () => { node.open = false; summary.focus(); });
    return [node, panel];
  }
  // which agents are unfolded, and whether the whole team panel is folded: kept in this browser
  const openAgents = new Set((() => { try { return JSON.parse(store.get("orch-team-open") || "[]"); } catch (e) { return []; } })());
  let teamFolded = store.get("orch-team-folded") === "1";
  function saveOpenAgents() { store.set("orch-team-open", JSON.stringify(Array.from(openAgents))); }
  function agentSummaryState(a, t) {
    // the one word that matters most on the folded line
    if (a.status === "failed") return "Failed";
    if (a.switch && a.switch.step && ["done", "failed"].indexOf(a.switch.step) < 0) return "Switching model";
    if (a.status === "writing") return "Writing";
    if (a.status === "waiting_job") return "Job running";
    if (a.status === "closed") return "Closed";
    return a.last_output ? "Idle " + L.ago(a.last_output, t) : "Idle";
  }
  function agentCard(a) {
    const t = nowSrv(), name = nm(a.id), role = ROLE[a.role] || "", status = L.STATUS[a.status] || "";
    const model = a.actual_model || (a.choice && (a.choice.model || a.choice.label)) || "";
    const box = h("details", { class: "agent status-row-" + a.status + (a.status === "closed" ? " closed" : "") });
    box.open = openAgents.has(a.id);
    box.addEventListener("toggle", () => {
      if (box.open) openAgents.add(a.id); else openAgents.delete(a.id);
      saveOpenAgents();
    });
    box.append(h("summary", { class: "agent-sum", title: name + ": " + status + (model ? " · " + model : "") },
      h("span", { class: "dot status-" + a.status, "aria-hidden": "true" }),
      h("span", { class: "agent-name" }, name),
      role.toLowerCase() !== name.toLowerCase().replace(/[ -]?\d+$/, "") ? h("span", { class: "agent-role" }, role) : null,
      h("span", { class: "agent-state" }, agentSummaryState(a, t)),
      a.waiting ? h("span", { class: "agent-count", title: a.waiting + " message(s) wait for " + name }, String(a.waiting)) : null));
    const body = h("div", { class: "agent-body" });
    box.append(body);
    const [actions, items] = menu("More actions for " + name);
    body.append(h("div", { class: "agent-meta" },
      model ? h("span", { class: "model-name", title: model }, L.shortModel(model)) : null,
      h("span", { class: "agent-when" }, status,
        a.last_output ? [" · ", h("span", { data: { ago: a.last_output }, title: "Last activity" }, L.ago(a.last_output, t))] : null)));
    const tags = L.agentTags(a, S);
    if (tags.length) body.append(h("div", { class: "agent-line tags" }, tags.join(" · ")));
    if (a.assignment && a.assignment.line) body.append(h("div", { class: "agent-line assignment", title: a.assignment.line },
      h("span", { class: "label" }, a.open_report || S.finished ? "Last assignment" : "Assignment"), a.assignment.line));
    if (a.status === "failed" && a.failed_why) body.append(h("div", { class: "agent-line danger-text" }, a.failed_why));
    const last = (live[a.id] || []).slice(-1)[0];
    body.append(h("div", { class: "agent-line live-preview", data: { live: a.id } }, last ? L.preview(last.s, 110) : ""));
    if (a.waiting) {
      const why = a.holds.map(x => ({ terminal: "a terminal is open", "switch": "model is being switched", turn: "it is writing", send_failed: "sending failed", stopped: "stopped by you" }[x])).filter(Boolean);
      body.append(h("div", { class: "agent-line" }, a.waiting + " message(s) waiting", why.length ? " · " + why.join(", ") : ""));
    }
    const jobs = Object.values(S.jobs || {}).filter(j => j.agent === a.id && (j.state === "running" || (!j.woke && !j.stopped_by_user)));
    const jobKey = "jobs:" + a.id, shownJobs = L.latest(jobs, listLimit(jobKey));
    shownJobs.rows.forEach(j => body.append(h("div", { class: "agent-line job" }, h("span", { class: "label" }, "Job"), L.jobLine(j, t))));
    add(body, listMore(jobKey, shownJobs.remaining, renderTeam));
    const controls = h("div", { class: "agent-actions" });
    if (a.role !== "monitor" && a.lifecycle === "open") controls.append(h("button", { type: "button", class: "sm", title: "Talk to " + name, on: { click: () => openTalk(a.id) } }, "Talk"));
    controls.append(h("button", { type: "button", class: "sm", title: "Choose the model for " + name, on: { click: () => openModel(a.id) } }, "Model"));
    if (a.role !== "monitor" && a.lifecycle === "open") controls.append(terminalLink(a));
    controls.append(h("span", { class: "grow" }), actions);
    body.append(controls);
    const activity = h("div", { class: "activity-body", data: { livelog: a.id }, hidden: !openAct.has(a.id) });
    items.append(h("button", { type: "button", title: "Show this agent’s recent output and tools",
      on: { click: ev => {
        activity.hidden = !activity.hidden;
        ev.currentTarget.textContent = activity.hidden ? "Recent activity" : "Hide recent activity";
        if (activity.hidden) openAct.delete(a.id);
        else { openAct.add(a.id); paintActivity(a.id); loadLive(a.id, "open"); }
      } } }, openAct.has(a.id) ? "Hide recent activity" : "Recent activity"));
    body.append(activity);
    if (a.role === "worker") L.workerActions(a).forEach(type => items.append(h("button", { type: "button",
      class: type === "WorkerDelete" ? "danger-text" : "",
      title: { WorkerClose: "Close this worker and keep its session", WorkerReopen: "Reopen this worker in its saved session", WorkerDelete: "Permanently remove this worker from the team" }[type],
      on: { click: ev => workerAct(type, a, ev.currentTarget) } },
      { WorkerClose: "Close", WorkerReopen: "Reopen", WorkerDelete: "Delete" }[type])));
    if (a.lifecycle === "open") items.append(a.stopped_by_user
      ? h("button", { type: "button", title: "Resume this agent in its saved session", on: { click: act("StartAgents", { agents: [a.id] }) } }, "Start")
      : h("button", { type: "button", title: "Stop this agent and its jobs", on: { click: act("StopAgents", { agents: [a.id] }) } }, "Stop"));
    return box;
  }
  function railDot(a) {
    // the folded team panel: one dot per agent, its name and state in the tooltip; a click unfolds that agent
    const name = nm(a.id), st = agentSummaryState(a, nowSrv());
    return h("button", { type: "button", class: "rail-agent", title: name + ": " + st, "aria-label": name + ": " + st,
      on: { click: () => { openAgents.add(a.id); saveOpenAgents(); setTeamFolded(false); } } },
      h("span", { class: "dot status-" + a.status, "aria-hidden": "true" }), h("span", { class: "rail-initial" }, railLabel(a)));
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
    const sig = JSON.stringify([teamFolded, list, S.jobs, S.terminals, S.discussions, S.finished, t, list.map(a => listLimit("jobs:" + a.id))]);
    if (paint($("team"), sig, () => teamFolded ? list.map(railDot) : list.map(agentCard))) openAct.forEach(paintActivity);
    if (!list.length) $("team").textContent = "No agents yet.";
  }

  // progress: milestones and the current checklist
  function renderProgress() {
    const ms = S.milestones || [], ck = S.checklist || {};
    paint($("progress"), JSON.stringify([ms, ck, listLimit("checklist")]), () => {
      if (!ms.length) return h("p", { class: "empty" }, "No milestones yet.");
      const [d, n] = L.progress(ck);
      const out = [h("div", { class: "ms" }, ms.map(m => h("div", { class: "mscard" + (m.status === "current" ? " cur" : "") },
          h("div", { class: "mute small" }, L.MS_STATUS[m.status] || ""), h("div", { class: "t" }, m.title),
          m.status === "current" && n ? [h("div", { class: "bar" }, h("i", { style: "width:" + Math.round(100 * d / n) + "%" })), h("div", { class: "mute small" }, d + " of " + n + " done")] : null,
          m.score !== null && m.score !== undefined && m.status === "done" ? h("div", { class: "mute small" }, "Score " + m.score) : null,
          h("div", { class: "milestone-actions" },
            m.text ? h("details", {}, h("summary", { class: "link" }, "Details"), md(m.text)) : null,
            m.status === "done" ? h("button", { type: "button", class: "link", on: { click: act("ReopenMilestone", { milestone_id: m.id }) } }, "Reopen") : null))))];
      if (ck.error) out.push(h("p", { class: "mute" }, "The critic's checklist is missing or cannot be read."));
      const entries = (ck.steps || []).flatMap(step => (step.items.length ? step.items : [null]).map(item => ({ step, item })));
      const { rows, remaining } = L.latest(entries, listLimit("checklist"));
      if (rows.length) {
        out.push(listMore("checklist", remaining, renderProgress), h("ul", { class: "ck" }, rows.map(({ step, item }, i) =>
          [i === 0 || rows[i - 1].step !== step ? h("li", { class: "stepname" }, step.title) : null,
            item ? h("li", { class: item[0] ? "done" : "open" }, h("span", { class: "bx", title: item[0] ? "Complete" : "To do" }, item[0] ? "✓" : "○"), h("span", {}, item[1])) : null])));
      }
      return out;
    });
  }

  // messages
  const messageFilter = () => ({ q: $("msearch").value, agent: $("mwho").value,
    levels: [...levels], notes: $("orch-notes").checked });

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

  function messageBody(m, sendable, run, named) {
    const hint = L.statusHint(m), meta = m.written_by || {};
    const sized = Number.isFinite(meta.input_chars) && Number.isFinite(meta.output_chars);
    const facts = [meta.model ? "Written by " + L.shortModel(meta.model) : "",
      sized ? L.characterSize(meta.input_chars) + " characters in, " + L.characterSize(meta.output_chars) + " out" : "", hint].filter(Boolean);
    const fb = h("div", { class: "fb" }, facts.length ? h("p", { class: "detail-hint", title: meta.model || "" }, facts.join(" · ")) : null,
      md(L.messageText(m)));
    if (sendable) fb.append(h("button", { type: "button", class: "link", on: {
      click: act("SendToCritic", { message_id: m.id, agent: m.sender }),
    } }, "Send to critic"));
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
    if (!run && named[m.recipient] && !m.drop) {
      const recipient = L.agentName(named, m.recipient);
      fb.append(fold(m.id + ":received", "What " + recipient + " received", async () => {
        const result = await cached("api/message?id=" + encodeURIComponent(m.id));
        return h("pre", {}, (result.message || {}).rendered || "No delivered text was recorded.");
      }, "The message as delivered, with Orch’s additions."));
    }
    return fb;
  }

  function msgRow(m, sendable, run, named = agents()) {
    const level = L.messageLevel(m, named), note = L.isNotice(m), awaiting = L.awaitsAnswer(m);
    const cls = ["msg", "level-" + level, m.recipient === "user" ? "touser" : "", awaiting ? "awaiting" : "", note ? "notice-row" : m.orch_note ? "housekeeping" : ""];
    const meta = m.written_by || {}, exception = L.messageException(m), hint = L.statusHint(m);
    const route = L.agentName(named, m.sender) + " → " + L.agentName(named, m.recipient);
    const size = Number.isFinite(meta.input_chars) && Number.isFinite(meta.output_chars);
    const d = h("details", { class: cls.filter(Boolean).join(" "), data: { message: m.id } },
      h("summary", { title: [route, hint].filter(Boolean).join(" · ") },
        h("span", { class: "tm" }, L.stamp(m.created, nowSrv())),
        h("span", { class: "lv", title: (L.LEVELS[level] || [""])[0], "aria-hidden": "true" }),
        h("span", { class: "route" }, route),
        exception ? h("span", { class: "st " + L.messageTone(m), title: hint }, exception) : null,
        h("span", { class: "pv" }, L.preview(L.messageText(m))),
        size && meta.input_chars > 30000 ? h("span", { class: "sz large", title: meta.input_chars.toLocaleString() +
          " input characters: more than 30,000" }, L.characterSize(meta.input_chars) + " in") : null));
    function expand() {
      if (!d._body) { d._body = messageBody(m, sendable, run, named); d.append(d._body); }
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
    const opts = [["", "All agents"]].concat(agentList().map(a => [a.id, nm(a.id)]));
    const keep = $("mwho").value;
    if (paint($("mwho"), JSON.stringify(opts), () => opts.map(x => h("option", { value: x[0] }, x[1])))) {
      $("mwho").value = opts.some(x => x[0] === keep) ? keep : "";
    }
    const { rows, remaining, busy, ready } = timeline;
    const shown = rows.length, total = shown + remaining;
    $("message-pages").hidden = remaining <= 0;
    $("older").disabled = $("show-all").disabled = busy;
    $("show-all").hidden = remaining <= L.MESSAGE_PAGE;
    $("remaining").textContent = remaining + " older";
    $("message-count").textContent = ready ? (remaining ? shown + " of " + total : shown + (shown === 1 ? " message" : " messages")) : "Loading…";
    if (!ready) return;
    const eligible = new Set(L.sendableToCritic(messages(), agents()));
    patchRows($("messages"), rows, m => msgRow(m, eligible.has(m.id)), m =>
      JSON.stringify([m, eligible.has(m.id), nm(m.sender), nm(m.recipient), L.messageLevel(m, agents())]));
  }

  async function requestTimeline(filter, limit, before) {
    let rows = [], remaining = 0;
    do {
      const page = await getJson("api/messages?" + L.pageQuery({ ...filter, limit: Math.min(limit - rows.length, 500), before }));
      if (!page.messages.length) { remaining = 0; break; }
      rows = L.mergeMessages([rows, page.messages]);
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
      view.rows = before ? L.mergeMessages([result.rows, view.rows]) : result.rows;
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
    Object.entries(L.LEVELS).forEach(([level, [name, title]]) => {
      const b = h("button", { type: "button", class: "level-toggle level-" + level, title,
        "aria-pressed": String(levels.has(level)),
        on: { click: () => {
          if (levels.has(level)) levels.delete(level); else levels.add(level);
          b.setAttribute("aria-pressed", String(levels.has(level)));
          store.set("orch-message-levels", JSON.stringify([...levels]));
          resetTimeline();
        } } }, name);
      $("levels").append(b);
    });
    $("orch-notes").addEventListener("change", e => {
      store.set("orch-message-notes", e.target.checked ? "1" : "0");
      resetTimeline();
    });
    let timer = 0;
    $("msearch").addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(resetTimeline, 250);
    });
    $("mwho").addEventListener("change", () => { clearTimeout(timer); resetTimeline(); });
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
    const all = L.talkLog(messages(), who, mgrId && mgrId.id), elig = L.sendableToCritic(messages(), agents());
    const talkKey = "talk-history:" + who;
    const { rows: log, remaining } = L.latest(all, listLimit(talkKey));
    const sig = JSON.stringify([log.map(m => [m.id, m.status, m.drop]), remaining, who, !!disc, disc && disc.phase, elig]);
    const box = $("talklog"), near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    const changed = paint(box, sig, () => log.length ? [listMore(talkKey, remaining, () => {
      const height = box.scrollHeight, top = box.scrollTop;
      renderTalk();
      box.scrollTop = top + box.scrollHeight - height;
    }), log.map(m => {
      const me = m.sender === "user";
      return h("div", { class: "cm" + (me ? " me" : "") + (L.isNotice(m) ? " note" : "") },
        h("div", { class: "w" }, (me ? "You" : nm(m.sender)) + " - " + L.stamp(m.created, nowSrv()) + (me ? " - " + L.messageStatus(m) : "")),
        h("div", { class: "b" }, md(L.messageText(m)), elig.indexOf(m.id) >= 0 ? h("button", { type: "button", class: "sm", on: { click: act("SendToCritic", { message_id: m.id, agent: m.sender }) } }, "Send to critic") : null));
    })] : h("p", { class: "empty" }, "Nothing yet. Write below."));
    if (changed && near) box.scrollTop = box.scrollHeight;
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
  async function more() {
    const v = await modal("More", h("p", { class: "mute" }, "Orch itself."), [
      { label: "Update and restart", value: "u" }, { label: "Close Orch", value: "c" }, { label: "Close Orch and stop agents", value: "a", cls: "danger" }, { label: "Back", value: "" }]);
    if (v === "u" && await confirmBox("Update Orch?", "Orch takes in the new version and restarts. The agents carry on.", "Update")) cmd("UpdateOrch", {});
    if (v === "c" && await confirmBox("Close Orch?", "Agents and jobs keep running. Start Orch again to see them.", "Close Orch")) cmd("StopOrch", { agents: false });
    if (v === "a" && await confirmBox("Close Orch and stop the agents?", "Agents and their jobs stop.", "Stop everything", "danger")) cmd("StopOrch", { agents: true });
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
    const items = j.messages ? L.sortedMessages(j.messages) :
      (j.tl_items || []).concat((j.chat || []).map(c => ({ ts: c.ts, from: c.role === "human" ? "You" : (c.who || c.role), to: "", text: c.text })))
        .sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
    paint($("team"), "old" + r.id, () => team.map(a => h("div", { class: "agent" },
      h("h3", {}, snapshotAgents ? L.agentName(j.agents, a.id) : L.plainName(a.called || a.name)),
      h("span", { class: "mute small" }, ROLE[a.role === "verifier" ? "critic" : a.role] || a.role || ""))));
    paint($("progress"), "old" + r.id, () => (j.milestones || j.tl_groups || []).map(g => h("div", { class: "line" }, g.title || g.label || g.key)));
    const { rows, remaining } = L.latest(items, listLimit("old-messages"));
    $("message-count").textContent = rows.length + " of " + items.length;
    paint($("messages"), "old" + r.id + ":" + rows.length, () => items.length ? [
      listMore("old-messages", remaining, () => renderOld(j, r)),
      rows.map(it => j.messages ? msgRow(it, false, r.id, j.agents) :
        h("details", { class: "msg" }, h("summary", {}, h("span", { class: "tm" }, String(it.ts || "").slice(11, 16)),
          h("span", { class: "route" }, L.plainName(it.from || "") + (it.to ? " → " + L.plainName(it.to) : "")),
          h("span", { class: "pv" }, L.preview(it.text || it.preview || it.title || ""))),
        h("div", { class: "fb" }, md(it.text || it.preview || it.title || "")))),
    ] : h("p", { class: "empty" }, "Nothing recorded."));
  }

  // ------------------------------------------------------------------------------------------------ wiring
  function wire() {
    $("talkbtn").addEventListener("click", () => openTalk());
    $("talkclose").addEventListener("click", closeTalk);
    $("talksend").addEventListener("click", sendTalk);
    $("talkwho").addEventListener("change", e => { who = e.target.value; renderTalk(true); markSeen(); });
    $("talkin").addEventListener("input", e => { drafts["talk:" + who] = e.target.value; saveDrafts(); });
    $("talkin").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendTalk(); } });
    document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("talk").hidden && !$("dlg").open && !$("dlg2").open) closeTalk(); });
    const runMenu = $("run-menu");
    ["nightbtn", "stopbtn", "morebtn", "runsbtn"].forEach(id => $(id).addEventListener("click", () => { runMenu.open = false; }));
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
    $("morebtn").addEventListener("click", more);
    $("runsbtn").addEventListener("click", runs);
    $("addworker").addEventListener("click", ev => cmd("WorkerAdd", {}, ev.currentTarget));
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
    setInterval(() => { document.querySelectorAll("[data-ago]").forEach(e => { e.textContent = L.ago(Number(e.dataset.ago), nowSrv()); }); }, 15000);
    setInterval(loadState, 20000);
    window.addEventListener("online", () => { connect(); loadState(); });
  }

  wire();
  loadState();
  connect();
})();
