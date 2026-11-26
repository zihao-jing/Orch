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
  const live = {}, liveN = {};  // agent -> last live lines, and last seen n
  const openMsgs = new Set(), openAct = new Set(), detail = {};
  let older = [], olderMore = null, found = null, foundMore = false, findSeq = 0;
  let who = "manager";          // the talk panel's recipient
  let seenSeq = null, modalRefresh = null;
  let goldText = null, auditText = null, auditNotes = null, goldEdit = false, notesEdit = false, lastSide = 0;

  // ---------------------------------------------------------------------------------------------- small tools
  const store = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* no storage: fine */ } },
  };
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
    return list.map(s => s.t === "code" ? h("code", {}, s.s) : s.t === "b" ? h("b", {}, s.s) : s.s);
  }
  function md(text) {
    const box = h("div", { class: "md" });
    L.markdown(text).forEach(b => {
      if (b.t === "code") box.append(h("pre", {}, b.s));
      else if (b.t === "h") box.append(h("h4", {}, spanNodes(b.c)));
      else if (b.t === "ul" || b.t === "ol") box.append(h(b.t, {}, b.items.map(i => h("li", {}, spanNodes(i)))));
      else {
        const p = h("p");
        b.lines.forEach((ln, i) => { if (i) p.append(h("br")); add(p, spanNodes(ln)); });
        box.append(p);
      }
    });
    return box;
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
      if (reply.shown) toast(reply.shown); else if (reply.ok === false) toast(reply.error || "That did not work.");
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
      if (!old) { render(); loadSide(false); }
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
    list.push({ n: d.n, at: d.at, k: d.k, s: d.s });
    if (list.length > 60) list.shift();
    document.querySelectorAll('[data-live="' + d.agent + '"]').forEach(e => { e.textContent = L.preview(d.s, 110); });
    document.querySelectorAll('[data-livelog="' + d.agent + '"]').forEach(e => { e.append(liveLine(list[list.length - 1])); });
  }
  const liveLine = x => document.createTextNode((x.k === "tool" ? "> " : "") + x.s + "\n");
  async function loadLive(id, box, fromN) {
    const from = fromN !== undefined ? fromN : (id in liveN ? liveN[id] + 1 : 0);
    try {
      const j = await getJson("api/live?agent=" + encodeURIComponent(id) + "&from=" + from);
      if (j.reset || from === 0) live[id] = [];
      const list = live[id] || (live[id] = []);
      (j.items || []).forEach(it => {
        if (typeof it.n === "number") {
          if (!j.reset && from > 0 && id in liveN && it.n <= liveN[id]) return;
          liveN[id] = it.n;
        }
        list.push({ n: it.n, at: it.at, k: it.k, s: it.s });
      });
      if (list.length > 60) live[id] = list.slice(-60);
      const boxes = box ? [box] : Array.prototype.slice.call(document.querySelectorAll('[data-livelog="' + id + '"]'));
      boxes.forEach(b => b.replaceChildren(...live[id].map(liveLine)));
      const last = live[id][live[id].length - 1];
      if (last) document.querySelectorAll('[data-live="' + id + '"]').forEach(e => { e.textContent = L.preview(last.s, 110); });
      else if (j.reset) document.querySelectorAll('[data-live="' + id + '"]').forEach(e => { e.textContent = ""; });
    } catch (e) { if (box) box.textContent = "Not available."; }
  }

  // --------------------------------------------------------------------------------------------- rendering
  const agents = () => (S ? S.agents : {});
  const nm = id => L.agentName(agents(), id);
  const ROLE = { manager: "Manager", critic: "Critic", worker: "Worker", monitor: "Monitor" };

  function agentList() {
    const rank = { manager: 0, critic: 1, worker: 2, monitor: 3 };
    return Object.values(agents()).filter(a => a.lifecycle !== "deleted" && a.role !== "auditor")
      .sort((x, y) => ((rank[x.role] || 9) - (rank[y.role] || 9)) || x.id.localeCompare(y.id, undefined, { numeric: true }));
  }
  const messages = () => L.sortedMessages(S.messages);

  function render() {
    if (!S) return;
    $("oldnote").hidden = true;
    renderHeader(); renderNotices(); renderNeeds(); renderTeam(); renderProgress(); renderMessages();
    renderTalk(); renderSide();
    if (modalRefresh) modalRefresh();
  }

  function renderHeader() {
    const name = (S.run && S.run.project) || "Orch board";
    $("title").textContent = name; document.title = name + " - Orch board";
    const stopped = agentList().some(a => a.stopped_by_user && a.lifecycle === "open");
    paint($("runstate"), JSON.stringify([S.finished, S.paused, stopped]), () => [
      S.finished ? h("span", { class: "pill p-good" }, "Finished") : S.paused ? h("span", { class: "pill p-warn" }, "Paused") : h("span", { class: "pill p-run" }, "Running"),
      stopped ? h("span", { class: "pill p-crit" }, "Agents stopped") : null]);
    const nb = $("nightbtn"); nb.classList.toggle("on", !!S.night); nb.setAttribute("aria-pressed", S.night ? "true" : "false");
    $("stopbtn").textContent = stopped ? "Start agents" : "Stop agents";
    $("stopbtn").dataset.mode = stopped ? "start" : "stop";
  }

  // notices: what Orch tells you, an open terminal
  function dismissed() { try { return JSON.parse(store.get("orch-dismissed") || "[]"); } catch (e) { return []; } }
  function renderNotices() {
    const gone = dismissed(), t = nowSrv();
    const notes = messages().filter(m => L.isNotice(m) && gone.indexOf(m.id) < 0 && t - m.created < 21600).slice(-5);
    const terms = Object.keys(S.terminals || {});
    const sig = JSON.stringify([notes.map(m => m.id), terms.map(a => [a, (S.agents[a] || {}).waiting])]);
    paint($("notices"), sig, () => [
      terms.map(a => h("div", { class: "banner note" }, h("div", { class: "t" },
        "A terminal is open on " + nm(a) + ". " + ((S.agents[a] || {}).waiting || 0) + " message(s) are waiting for it."),
        h("button", { type: "button", class: "sm", on: { click: act("TerminalTakeBack", { agent: a }) } }, "Take back"))),
      notes.map(m => h("div", { class: "banner note" }, h("div", { class: "t" }, L.messageText(m)),
        m.text && m.text.key === "note_migrated" ? h("button", { type: "button", class: "sm", on: { click: act("ConfirmMigration", {}) } }, "Remove old files") : null,
        h("button", { type: "button", class: "sm", on: { click: () => { gone.push(m.id); store.set("orch-dismissed", JSON.stringify(gone.slice(-200))); $("notices")._sig = ""; renderNotices(); } } }, "Dismiss")))]);
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
      box.append(h("div", { class: "opts" }, g.options.map((o, i) => h("button", { type: "button", on: { click: ev => send({ option: i }, ev) } }, o))));
    }
    const text = g.kind === "operational" ? null : draft(key, "textarea", { rows: 2, "aria-label": "Your answer", placeholder: g.kind === "question" ? "Or write your own answer" : "Your comment (needed to ask for changes)" });
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
    const open = L.openGates(S.gates), waiting = L.answeredWaiting(S.gates), sec = $("needs");
    sec.hidden = !(open.length || waiting.length);
    paint(sec, JSON.stringify([open, waiting.map(g => g.id)]), () => [
      h("h2", {}, "Needs you"), open.map(gateCard),
      waiting.length ? h("p", { class: "mute small" }, "You answered " + waiting.length + " question(s). The answers go to the manager when all questions are answered.") : null]);
  }

  // team
  function termCommand(a) { return "orch terminal " + ((S.run && S.run.project) || "") + " " + a.id; }
  function copy(text) {
    try { navigator.clipboard.writeText(text).then(() => toast("Copied"), () => toast("Copy it by hand")); } catch (e) { toast("Copy it by hand"); }
  }
  function agentCard(a) {
    const t = nowSrv(), tone = { writing: "p-run", waiting_job: "p-warn", failed: "p-crit" }[a.status] || "";
    const box = h("div", { class: "agent" + (a.status === "closed" ? " closed" : "") });
    box.append(h("div", { class: "top" }, h("h3", {}, nm(a.id)), h("span", { class: "mute small" }, ROLE[a.role] || ""),
      h("span", { class: "pill " + tone }, a.status === "writing" ? h("span", { class: "dot live" }) : null, L.STATUS[a.status] || ""),
      L.agentTags(a, S).map(x => h("span", { class: "pill p-warn" }, x))));
    const model = a.actual_model || (a.choice && a.choice.label) || "";
    const chosen = a.choice && a.choice.label;
    box.append(h("div", { class: "line" }, h("b", {}, "Model: "), model, a.actual_model && chosen && a.actual_model !== chosen ? " (chosen: " + chosen + ")" : "",
      a.choice && a.choice.effort ? ", effort " + a.choice.effort : "", a.choice && a.choice.name ? " (backup: " + a.choice.name + ")" : ""));
    if (a.assignment && a.assignment.line) box.append(h("div", { class: "line" }, h("b", {}, "Task: "), a.assignment.line));
    if (a.status === "failed" && a.failed_why) box.append(h("div", { class: "line" }, h("b", {}, "Why: "), a.failed_why));
    const last = (live[a.id] || []).slice(-1)[0];
    box.append(h("div", { class: "line" }, h("b", {}, "Last activity: "), a.last_output ? h("span", { data: { ago: a.last_output } }, L.ago(a.last_output, t)) : "–",
      " ", h("span", { class: "mute", data: { live: a.id } }, last ? L.preview(last.s, 110) : "")));
    if (a.waiting) {
      const why = a.holds.map(x => ({ terminal: "a terminal is open", "switch": "model is being switched", turn: "it is writing", send_failed: "sending failed", stopped: "stopped by you" }[x])).filter(Boolean);
      box.append(h("div", { class: "line" }, h("b", {}, "Waiting messages: "), String(a.waiting), why.length ? " (" + why.join(", ") + ")" : ""));
    }
    const jobs = Object.values(S.jobs || {}).filter(j => j.agent === a.id && (j.state === "running" || (!j.woke && !j.stopped_by_user))).slice(0, 8);
    jobs.forEach(j => box.append(h("div", { class: "line" }, h("b", {}, "Job: "), L.jobLine(j, t))));
    const acts = h("div", { class: "acts" });
    if (a.role !== "monitor") acts.append(h("button", { type: "button", class: "sm", on: { click: () => openTalk(a.id) } }, "Talk"));
    acts.append(h("button", { type: "button", class: "sm", on: { click: () => openModel(a.id) } }, "Model"));
    if (a.role === "worker") L.workerActions(a).forEach(type => acts.append(h("button", { type: "button", class: "sm" + (type === "WorkerDelete" ? " danger" : ""), on: { click: ev => workerAct(type, a, ev.currentTarget) } },
      { WorkerClose: "Close", WorkerReopen: "Reopen", WorkerDelete: "Delete" }[type])));
    if (a.lifecycle === "open") acts.append(a.stopped_by_user
      ? h("button", { type: "button", class: "sm", on: { click: act("StartAgents", { agents: [a.id] }) } }, "Start")
      : h("button", { type: "button", class: "sm", on: { click: act("StopAgents", { agents: [a.id] }) } }, "Stop"));
    box.append(acts);
    if (a.role !== "monitor") box.append(h("div", { class: "term" }, h("span", { class: "mute" }, "Terminal: "), h("code", {}, termCommand(a)), " ",
      h("button", { type: "button", class: "sm", on: { click: () => copy(termCommand(a)) } }, "Copy")));
    const act2 = h("details", { class: "term", data: { id: a.id } }, h("summary", {}, "Recent activity"));
    const log = h("div", { class: "livelog", data: { livelog: a.id } }, (live[a.id] || []).map(liveLine));
    act2.append(log);
    if (openAct.has(a.id)) act2.open = true;
    act2.addEventListener("toggle", () => { if (act2.open) { openAct.add(a.id); loadLive(a.id, log); } else openAct.delete(a.id); });
    box.append(act2);
    return box;
  }
  async function workerAct(type, a, btn) {
    if (type === "WorkerDelete" && !(await confirmBox("Delete " + nm(a.id) + "?", "It cannot be opened again. Its messages stay in the record.", "Delete", "danger"))) return;
    cmd(type, { agent: a.id }, btn);
  }
  function renderTeam() {
    const list = agentList(), t = Math.floor(nowSrv() / 60);
    const sig = JSON.stringify([list, S.jobs, S.terminals, S.discussions, t]);
    paint($("team"), sig, () => list.map(agentCard));
    if (!list.length) $("team").textContent = "No agents yet.";
  }

  // progress: milestones and the current checklist
  function renderProgress() {
    const ms = S.milestones || [], ck = S.checklist || {};
    paint($("progress"), JSON.stringify([ms, ck, S.finished]), () => {
      if (!ms.length) return h("p", { class: "empty" }, "No milestones yet.");
      const [d, n] = L.progress(ck);
      const out = [S.finished ? h("p", { class: "ink2" }, "All milestones are done. The run stays open.") : null,
        h("div", { class: "ms" }, ms.map(m => h("div", { class: "mscard" + (m.status === "current" ? " cur" : "") },
          h("div", { class: "mute small" }, L.MS_STATUS[m.status] || ""), h("div", { class: "t" }, m.title),
          m.status === "current" && n ? [h("div", { class: "bar" }, h("i", { style: "width:" + Math.round(100 * d / n) + "%" })), h("div", { class: "mute small" }, d + " of " + n + " done")] : null,
          m.score !== null && m.score !== undefined && m.status === "done" ? h("div", { class: "mute small" }, "Score " + m.score) : null,
          m.text ? h("details", {}, h("summary", { class: "small" }, "Details"), md(m.text)) : null,
          m.status === "done" ? h("button", { type: "button", class: "sm", style: "margin-top:6px", on: { click: act("ReopenMilestone", { milestone_id: m.id }) } }, "Reopen") : null)))];
      if (ck.error) out.push(h("p", { class: "mute" }, "The critic's checklist is missing or cannot be read."));
      (ck.steps || []).forEach(s => out.push(h("div", { class: "stepname" }, (s.done ? "Done: " : "") + s.title),
        h("ul", { class: "ck" }, (s.items || []).map(it => h("li", { class: it[0] ? "done" : "open" }, h("span", { class: "bx" }, it[0] ? "x" : "o"), h("span", {}, it[1]))))));
      return out;
    });
  }

  // messages
  function msgRow(m) {
    const touser = m.recipient === "user", d = h("details", { class: "msg" + (touser ? " touser" : "") });
    d.append(h("summary", {}, h("span", { class: "tm" }, L.stamp(m.created, nowSrv())), h("b", {}, nm(m.sender)), "to", h("b", {}, nm(m.recipient)),
      h("span", { class: "st " + L.messageTone(m) }, L.messageStatus(m)), h("span", { class: "pv" }, L.preview(L.messageText(m)))));
    if (openMsgs.has(m.id)) d.open = true;
    const fb = h("div", { class: "fb" }, md(L.messageText(m)));
    d.append(fb);
    if (L.sendableToCritic(messages()).indexOf(m.id) >= 0) fb.append(h("button", { type: "button", class: "sm", on: { click: act("SendToCritic", { message_id: m.id, agent: m.sender }) } }, "Send to critic"));
    const more = h("div", { class: "acts", style: "margin-top:6px" }), area = h("div");
    if (m.turn || m.reply_turn || m.sender !== "orch") more.append(h("button", { type: "button", class: "sm", on: { click: () => showTurn(m, area) } }, "Show the whole reply"));
    more.append(h("button", { type: "button", class: "sm", on: { click: () => showSent(m, area) } }, "Show the text as sent"));
    fb.append(more, area);
    d.addEventListener("toggle", () => { if (d.open) openMsgs.add(m.id); else openMsgs.delete(m.id); });
    return d;
  }
  async function showTurn(m, area) {
    try { const j = await getJson("api/turn?id=" + encodeURIComponent(m.turn || m.reply_turn || m.id)); area.replaceChildren(h("pre", {}, j.output || j.text || "Nothing written.")); }
    catch (e) { area.textContent = "Not available."; }
  }
  async function showSent(m, area) {
    try { const j = await getJson("api/message?id=" + encodeURIComponent(m.id)); area.replaceChildren(h("pre", {}, (j.message && j.message.rendered) || "Not sent yet.")); }
    catch (e) { area.textContent = "Not available."; }
  }
  function shownMessages() {
    return found !== null ? found : L.mergeMessages([older, messages()]);
  }
  function renderMessages() {
    const list = shownMessages(), filt = $("msearch").value.trim() || $("mwho").value;
    const opts = [["", "Everyone"]].concat(agentList().map(a => [a.id, nm(a.id)]));
    const keep = $("mwho").value;
    paint($("mwho"), JSON.stringify(opts), () => opts.map(x => h("option", { value: x[0] }, x[1])));
    $("mwho").value = keep;
    const more = found !== null ? foundMore : (olderMore !== null ? olderMore : Object.keys(S.messages).length >= 300);
    $("older").hidden = !more;
    const sig = JSON.stringify([list.map(m => [m.id, m.status, m.drop, m.holds, m.delivered_at]), filt, S.agents && Object.keys(S.agents).length]);
    paint($("messages"), sig, () => list.length ? list.map(msgRow) : h("p", { class: "empty" }, filt ? "No message matches." : "No messages yet."));
  }
  async function loadMessages(before) {
    const q = $("msearch").value.trim(), ag = $("mwho").value, mine = ++findSeq;
    const p = new URLSearchParams({ limit: "50" });
    if (q) p.set("q", q); if (ag) p.set("agent", ag); if (before) p.set("before", String(before));
    try {
      const j = await getJson("api/messages?" + p.toString());
      if (mine !== findSeq) return;
      if (before) { if (q || ag) { found = L.mergeMessages([found, j.messages]); foundMore = !!j.more; } else { older = L.mergeMessages([older, j.messages]); olderMore = !!j.more; } }
      else if (q || ag) { found = j.messages; foundMore = !!j.more; }
      renderMessages();
    } catch (e) { toast("Could not load messages."); }
  }
  function firstSeq() { const l = shownMessages(); return l.length ? l[0].seq : 0; }

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
    const log = L.talkLog(messages(), who, mgrId && mgrId.id), elig = L.sendableToCritic(messages());
    const sig = JSON.stringify([log.map(m => [m.id, m.status, m.drop]), who, !!disc, disc && disc.phase, elig]);
    const box = $("talklog"), near = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
    const changed = paint(box, sig, () => log.length ? log.map(m => {
      const me = m.sender === "user";
      return h("div", { class: "cm" + (me ? " me" : "") + (L.isNotice(m) ? " note" : "") },
        h("div", { class: "w" }, (me ? "You" : nm(m.sender)) + " - " + L.stamp(m.created, nowSrv()) + (me ? " - " + L.messageStatus(m) : "")),
        h("div", { class: "b" }, md(L.messageText(m)), elig.indexOf(m.id) >= 0 ? h("button", { type: "button", class: "sm", on: { click: act("SendToCritic", { message_id: m.id, agent: m.sender }) } }, "Send to critic") : null));
    }) : h("p", { class: "empty" }, "Nothing yet. Write below."));
    if (changed && near) box.scrollTop = box.scrollHeight;
    $("talk-h").textContent = "Talk to";
    const isLead = a.role === "manager" || a.role === "critic";
    paint($("talkacts"), JSON.stringify([isLead, !!disc, disc && disc.phase, who]), () => !isLead ? [] : disc
      ? [h("button", { type: "button", class: "sm ok", on: { click: applyDiscussion } }, "Apply discussion"),
        h("button", { type: "button", class: "sm", on: { click: endDiscussion } }, "End without applying")]
      : [h("button", { type: "button", class: "sm", title: "Talk it over first. What the agent writes to others waits until you apply it.", on: { click: act("DiscussionStart", { agent: who }) } }, "Discuss")]);
    paint($("talkinfo"), JSON.stringify([!!disc, disc && disc.phase, who, S.run && S.run.project]), () => [
      disc ? h("div", { class: "banner info" }, disc.phase === "applying" ? "Applying the discussion. The agent is answering." : "A discussion is open. What " + nm(who) + " writes to others is a draft until you apply it.") : null,
      a.id && (isLead || a.role === "worker") ? h("p", { class: "mute small" }, "In a terminal: ", h("code", {}, termCommand(a))) : null]);
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
      const body = [h("p", {}, h("b", {}, "Now: "), a.actual_model || a.choice.label || "", a.choice.effort ? ", effort " + a.choice.effort : "")];
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
          const cur = c.label === a.choice.label;
          body.push(h("div", { class: "opt" + (cur ? " cur" : "") }, h("span", { class: "grow" }, (c.name ? c.name + ": " : "Main: ") + (c.label || c.model)), cur ? h("span", { class: "mute small" }, "in use") :
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
        auditText = await getJson("api/audit");
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
    paint($("audit"), JSON.stringify([auditText, auditNotes, notesEdit]), () => {
      if (!auditText) return h("p", { class: "mute" }, "Loading...");
      return [h("div", { class: "acts" }, h("button", { type: "button", class: "sm", disabled: !!auditText.running, on: { click: act("AuditRun", {}) } }, auditText.running ? "Audit is running" : "Run audit")),
        auditText.report ? md(auditText.report) : h("p", { class: "empty" }, "No audit yet."),
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
    try { list = (await getJson("api/runs")).runs || []; } catch (e) { toast("Could not load the runs."); return; }
    const v = await modal("Earlier runs", close => list.length ? list.map((r, i) => h("div", { class: "opt" },
      h("span", { class: "grow" }, (r.project || "Run") + " - " + (r.status || "") + " - " + L.stamp(r.started, nowSrv())),
      h("button", { type: "button", class: "sm", on: { click: () => close("run" + i) } }, "Open"))) : h("p", { class: "empty" }, "No earlier runs."), [{ label: "Close", value: "" }]);
    if (typeof v === "string" && /^run\d+$/.test(v)) viewOld(list[Number(v.slice(3))]);
  }
  async function viewOld(r) {
    try { old = await getJson("api/run?id=" + encodeURIComponent(r.id)); } catch (e) { toast("Could not open that run."); return; }
    renderOld(old, r);
  }
  function renderOld(j, r) {
    const note = $("oldnote"); note.hidden = false;
    note.replaceChildren(document.createTextNode("You are reading an earlier run (" + (r.project || "run") + "). Nothing here can be changed. "),
      h("button", { type: "button", class: "sm", on: { click: () => { old = null; note.hidden = true; ["needs", "team", "progress", "messages"].forEach(i => { $(i)._sig = ""; }); render(); } } }, "Back to the current run"));
    $("needs").hidden = true;
    const items = (j.tl_items || []).concat((j.chat || []).map(c => ({ ts: c.ts, from: c.role === "human" ? "You" : (c.who || c.role), to: "", text: c.text })));
    paint($("team"), "old" + r.id, () => (j.agents || []).map(a => h("div", { class: "agent" }, h("h3", {}, L.agentName({ [a.name]: { display: a.called, role: a.role } }, a.name)), h("span", { class: "mute small" }, ROLE[a.role === "verifier" ? "critic" : a.role] || a.role || ""))));
    paint($("progress"), "old" + r.id, () => (j.tl_groups || []).map(g => h("div", { class: "line" }, g.label || g.key)));
    paint($("messages"), "old" + r.id, () => items.length ? items.map(it => h("details", { class: "msg" }, h("summary", {}, h("span", { class: "tm" }, String(it.ts || "").slice(11, 16)),
      h("b", {}, L.plainName(it.from || "")), it.to ? ["to", h("b", {}, L.plainName(it.to))] : null, h("span", { class: "pv" }, L.preview(it.text || it.preview || it.title || ""))),
      h("div", { class: "fb" }, md(it.text || it.preview || it.title || "")))) : h("p", { class: "empty" }, "Nothing recorded."));
    $("older").hidden = true;
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
    $("nightbtn").addEventListener("click", ev => cmd("SetNight", { on: !(S && S.night) }, ev.currentTarget));
    $("stopbtn").addEventListener("click", stopStart);
    $("morebtn").addEventListener("click", more);
    $("runsbtn").addEventListener("click", runs);
    $("addworker").addEventListener("click", ev => cmd("WorkerAdd", {}, ev.currentTarget));
    $("changems").addEventListener("click", changeMilestones);
    $("themebtn").addEventListener("click", () => {
      const r = document.documentElement, dark = r.dataset.theme ? r.dataset.theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
      r.dataset.theme = dark ? "light" : "dark"; store.set("orch-theme", r.dataset.theme);
    });
    const th = store.get("orch-theme"); if (th) document.documentElement.dataset.theme = th;
    let timer = 0;
    const again2 = () => { clearTimeout(timer); timer = setTimeout(() => { found = null; if ($("msearch").value.trim() || $("mwho").value) loadMessages(0); else { findSeq++; renderMessages(); } }, 300); };
    $("msearch").addEventListener("input", again2);
    $("mwho").addEventListener("change", again2);
    $("older").addEventListener("click", () => loadMessages(firstSeq()));
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
