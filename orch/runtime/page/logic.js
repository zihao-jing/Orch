/* Orch dashboard: pure helpers (no DOM, no network). Used by app.js; tested through node. */
(function (root) {
  "use strict";

  const STATUS = { idle: "Idle", writing: "Writing", waiting_job: "Waiting for job", failed: "Failed", closed: "Closed" };
  const MSG_STATUS = { waiting: "Waiting", delivered: "Delivered", answered: "Answered", failed: "Failed" };
  const HOLD = {
    question: "waits for your answer", gates: "waits for your answers", draft: "draft in a discussion",
    terminal: "a terminal is open", "switch": "model is being switched", turn: "waits for the end of the turn",
    send_failed: "could not be sent", stopped: "stopped by you",
  };
  const DROP = {
    answered_gates: "replaced by your answers", draft_ended: "draft dropped", silent: "kept here only",
    rejected: "no such worker", worker_closed: "worker is closed", replaced: "replaced", merged: "merged into a later one",
    not_needed: "no longer needed",
  };
  const GATE_KIND = {
    question: "Question", pause: "Paused", plan: "Plan to approve", milestone: "Milestone to approve",
    milestone_change: "Milestone change to approve", operational: "Needs your attention",
  };
  const MS_STATUS = { pending: "Later", current: "Now", done: "Done", dropped: "Dropped" };
  // the text keys of notices Orch writes to the user (shown as notices, not as talk)
  const NOTE_KEYS = ["note_agent_failed", "note_wakeup_sent", "note_monitor_failed", "note_switched",
    "note_review_withdrawn", "note_finished", "note_migrated"];

  // an agent id such as "worker-2" is shown as a plain name
  function plainName(id) {
    if (!id) return "";
    if (id === "user") return "You";
    if (id === "orch") return "Orch";
    const s = String(id).replace(/[-_]+/g, " ").trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  function agentName(agents, id) {
    const a = agents && agents[id];
    const d = a && a.display;
    if (d && d !== id && d.toLowerCase() !== "the " + String(id).toLowerCase() && d.toLowerCase() !== "the " + String((a && a.role) || "").toLowerCase()) {
      return d;
    }
    return plainName(id);
  }

  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function clock(t) {
    if (!t) return "";
    const d = new Date(t * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function stamp(t, now) {
    if (!t) return "";
    const d = new Date(t * 1000), n = new Date((now || t) * 1000);
    const same = d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
    return same ? clock(t) : pad(d.getDate()) + "." + pad(d.getMonth() + 1) + ". " + clock(t);
  }
  function ago(t, now) {
    if (!t || t <= 0) return "–";
    const s = Math.max(0, Math.floor(now - t));
    if (s < 45) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " d ago";
  }
  function duration(s) {
    s = Math.max(0, Math.floor(s));
    if (s < 60) return s + " s";
    if (s < 3600) return Math.floor(s / 60) + " min";
    return Math.floor(s / 3600) + " h " + pad(Math.floor((s % 3600) / 60)) + " min";
  }

  // the text of a Orch-worded record: TextRef.shown (added by the server), else nothing
  function shownOf(ref) { return (ref && typeof ref.shown === "string") ? ref.shown : ""; }

  // what a message says: Orch's words around its body, or the body alone
  function messageText(m) {
    const w = shownOf(m.text), b = m.body || "";
    if (!w) return b;
    return (b && w.indexOf(b) < 0) ? w + "\n\n" + b : w;
  }

  function messageStatus(m) {
    if (m.drop) return "Not sent: " + (DROP[m.drop] || "dropped");
    if (m.status === "waiting" && m.holds && m.holds.length) {
      return "Waiting: " + m.holds.map(h => HOLD[h] || h).join(", ");
    }
    return MSG_STATUS[m.status] || "";
  }
  function messageTone(m) {
    if (m.drop || m.status === "failed") return "bad";
    if (m.status === "waiting") return "wait";
    return "ok";
  }
  function isNotice(m) {
    return m.sender === "orch" && m.recipient === "user" && !!(m.text && NOTE_KEYS.indexOf(m.text.key) >= 0);
  }

  const LEVELS = {
    you: ["You ↔ Manager", "Messages to or from you, and between Orch and the manager."],
    critic: ["Manager ↔ Critic", "Messages between the manager and critic, and Orch messages to the critic."],
    workers: ["Critic ↔ Workers", "Messages involving workers, including instructions from Orch."],
    monitor: ["Monitor", "Messages to or from the monitor."],
  };
  const MESSAGE_PAGE = 30;
  const LIST_PAGE = 15;
  function messageLevel(m, agents) {
    const role = id => (agents[id] || {}).role || id;
    const parties = [role(m.sender), role(m.recipient)];
    if (parties.includes("monitor")) return "monitor";
    if (parties.includes("user") || (parties.includes("orch") && parties.includes("manager"))) return "you";
    if (parties.includes("worker")) return "workers";
    return parties.includes("critic") ? "critic" : "you";
  }

  function latest(list, limit = LIST_PAGE) {
    return { rows: limit > 0 ? list.slice(-limit) : [], remaining: Math.max(0, list.length - limit) };
  }

  function pageQuery({ limit = MESSAGE_PAGE, before = 0, q = "", agent = "", levels = Object.keys(LEVELS), notes = true }) {
    const p = new URLSearchParams({ timeline: "1", limit: String(limit), levels: levels.join(",") || "none", notes: notes ? "1" : "0" });
    if (before > 0) p.set("before", String(before));
    if (q.trim()) p.set("q", q.trim());
    if (agent) p.set("agent", agent);
    return p.toString();
  }

  function shortModel(model) { return String(model || "").trim().split("/").pop(); }

  function characterSize(n) {
    return n < 1000 ? String(n) : (n / 1000).toFixed(n < 10000 ? 1 : 0).replace(/\.0$/, "") + "k";
  }

  function messageException(m) {
    if (m.drop) return "Dropped";
    if (m.status === "waiting" && (m.holds || []).length) return "Held";
    if (m.status === "waiting" || m.status === "failed") return messageStatus(m);
    return "";
  }

  function awaitsAnswer(m) {
    return m.needs_answer && !m.drop && (m.status === "waiting" || m.status === "delivered");
  }

  function statusHint(m) {
    if (m.status === "answered" && !m.drop) return "Answered. The recipient replied to this message.";
    if (m.status === "delivered" && !m.drop) {
      return "Delivered. This message reached the recipient." + (awaitsAnswer(m) ? " Waiting for a reply." : "");
    }
    if (m.status === "waiting" && !(m.holds || []).length) return "Waiting. This message is queued to be sent.";
    if (m.status === "failed" && !m.drop) return "Failed. Orch could not send this message.";
    return messageStatus(m);
  }

  // one line about an agent beyond its status
  function agentTags(a, snap) {
    const t = [];
    if (a.stopped_by_user) t.push("stopped by you");
    if (a.retry) t.push("trying again" + (a.retry.until ? " at " + clock(a.retry.until) : ""));
    if (a.switch) t.push(a.switch.step === "pending" ? "switch pending" : "switching model");
    if (snap && snap.terminals && snap.terminals[a.id]) t.push("terminal open");
    const discussion = snap && (snap.discussions || {})[a.id];
    if (discussion) {
      t.push(discussion.by_terminal && !(snap.terminals || {})[a.id] ? "Terminal discussion pending" : "discussing");
    }
    return t;
  }

  function jobLine(j, now) {
    const label = j.label || "job";
    if (j.state === "running") return label + " (running " + duration(now - j.started) + ")";
    if (j.state === "lost") return label + " (lost)";
    return label + " (ended" + (j.exit_code ? ", exit " + j.exit_code : "") + ")";
  }

  // the controls of one worker; a deleted worker shows nothing
  function workerActions(a) {
    if (a.lifecycle === "deleted") return [];
    return a.lifecycle === "closed" ? ["WorkerReopen", "WorkerDelete"] : ["WorkerClose", "WorkerDelete"];
  }

  function sortedMessages(map) {
    return Object.keys(map || {}).map(k => map[k]).sort((a, b) => (a.seq || 0) - (b.seq || 0));
  }
  function mergeMessages(lists) {
    const seen = {}, out = [];
    lists.forEach(l => (l || []).forEach(m => { if (!seen[m.id]) { seen[m.id] = 1; out.push(m); } }));
    return out.sort((a, b) => (a.seq || 0) - (b.seq || 0));
  }

  // messages of a talk with one agent: both directions; notices belong to the manager's talk
  function talkLog(messages, agentId, managerId) {
    return messages.filter(m =>
      (m.sender === "user" && m.recipient === agentId) ||
      (m.sender === agentId && m.recipient === "user") ||
      (agentId === managerId && m.sender === "orch" && m.recipient === "user"));
  }

  // LAB-6: the latest reply of a worker that went to the user can be sent on to the critic
  function openGates(gates) {
    const g = Object.keys(gates || {}).map(k => gates[k]).filter(x => !x.closed);
    const rank = { question: 0, plan: 1, milestone: 1, milestone_change: 1, pause: 2, operational: 3 };
    return g.sort((a, b) => (rank[a.kind] - rank[b.kind]) || (a.opened - b.opened));
  }
  // answered, but the answer waits for the other gates (GATE-4)
  function answeredWaiting(gates) {
    return Object.keys(gates || {}).map(k => gates[k]).filter(x => x.closed && !x.withdrawn && !x.sent_in && x.answer);
  }

  // the body of GateAnswer for each way of answering
  function gateAnswer(gate, how) {
    const b = { type: "GateAnswer", gate_id: gate.id };
    if (how.text) b.text = how.text;
    if (how.option !== undefined && how.option !== null) b.option = how.option;
    if (how.approve !== undefined && how.approve !== null) b.approve = how.approve;
    return b;
  }

  function newId() {
    try { if (root.crypto && root.crypto.randomUUID) return root.crypto.randomUUID(); } catch (e) { /* fall through */ }
    return "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }

  // delay before the n-th try to reconnect, in ms
  function backoff(n) { return Math.min(10000, 1000 * Math.pow(2, Math.max(0, n))); }

  // a command sent again after a choice: the choice's fields set, a new id
  function applyChoice(body, choice, id) {
    return Object.assign({}, body, choice.set || {}, { id: id });
  }

  // the effort levels of a catalog entry for the picker; [] hides it
  function efforts(catalog, modelId) {
    const m = (catalog || []).find(x => x.id === modelId);
    return m ? (m.efforts || []) : [];
  }

  // a small Markdown reading into plain data: blocks of {t:"p"|"ul"|"ol"|"code"|"h", ...}; spans of {t, s}
  // The page turns it into DOM nodes with textContent, never into HTML.
  function spans(line) {
    const out = [];
    // `code`, **bold**, *italic* and [text](a web address); a link to anything else stays text
    const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|((?<![*\w])\*[^*\s](?:[^*]*[^*\s])?\*(?![*\w]))|(\[([^\]]+)\]\((https?:\/\/[^)\s]+)\))/g;
    let i = 0, m;
    while ((m = re.exec(line))) {
      if (m.index > i) out.push({ t: "text", s: line.slice(i, m.index) });
      const tok = m[0];
      out.push(m[1] ? { t: "code", s: tok.slice(1, -1) } : m[2] ? { t: "b", s: tok.slice(2, -2) }
        : m[3] ? { t: "i", s: tok.slice(1, -1) } : { t: "a", s: m[5], href: m[6] });
      i = m.index + tok.length;
    }
    if (i < line.length) out.push({ t: "text", s: line.slice(i) });
    return out;
  }
  // a table row "| a | b |" as its cells; a | inside `code` stays in its cell
  function tableCells(line) {
    let s = line.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const cells = [];
    let cur = "", code = false;
    for (let k = 0; k < s.length; k++) {
      const c = s[k];
      if (c === "\\" && s[k + 1] === "|") { cur += "|"; k++; continue; }
      if (c === "`") code = !code;
      if (c === "|" && !code) { cells.push(cur.trim()); cur = ""; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }
  const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;
  function isTableStart(lines, i) {
    return /\|/.test(lines[i] || "") && TABLE_SEP.test(lines[i + 1] || "") && /\|/.test(lines[i + 1] || "");
  }
  function markdown(src) {
    const L = String(src || "").replace(/\r/g, "").split("\n"), out = [];
    let i = 0;
    const LI = /^\s*([-*+]|\d+[.)])\s+(.*)$/;
    while (i < L.length) {
      const l = L[i];
      if (/^\s*```/.test(l)) {
        const b = []; i++;
        while (i < L.length && !/^\s*```/.test(L[i])) b.push(L[i++]);
        i++; out.push({ t: "code", s: b.join("\n") }); continue;
      }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)) { out.push({ t: "hr" }); i++; continue; }
      if (/^\s*>/.test(l)) {
        const q = [];
        while (i < L.length && /^\s*>/.test(L[i])) q.push(L[i++].replace(/^\s*>\s?/, ""));
        out.push({ t: "quote", blocks: markdown(q.join("\n")) }); continue;
      }
      if (isTableStart(L, i)) {
        const head = tableCells(L[i]);
        const align = tableCells(L[i + 1]).map(c => /^:-+:$/.test(c) ? "center" : /-:$/.test(c) ? "right" : "");
        const rows = [];
        i += 2;
        while (i < L.length && L[i].trim() && /\|/.test(L[i])) {
          const cells = tableCells(L[i++]);
          rows.push(head.map((_, k) => spans(cells[k] || "")));
        }
        out.push({ t: "table", head: head.map(spans), align: head.map((_, k) => align[k] || ""), rows: rows });
        continue;
      }
      const hm = l.match(/^#{1,4}\s+(.*)/);
      if (hm) { out.push({ t: "h", c: spans(hm[1]) }); i++; continue; }
      if (LI.test(l)) {
        const ordered = /^\s*\d/.test(l), items = [];
        while (i < L.length && LI.test(L[i])) items.push(spans(L[i++].match(LI)[2]));
        out.push({ t: ordered ? "ol" : "ul", items: items }); continue;
      }
      if (!l.trim()) { i++; continue; }
      const p = [];
      while (i < L.length && L[i].trim() && !/^\s*```/.test(L[i]) && !/^#{1,4}\s/.test(L[i]) && !LI.test(L[i]) &&
        !isTableStart(L, i) && !/^\s*>/.test(L[i]) && !/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(L[i])) p.push(L[i++]);
      out.push({ t: "p", lines: p.map(spans) });
    }
    return out;
  }

  // the first line of a text, for a folded message
  function preview(text, n) {
    const s = String(text || "").split("\n")
      .filter(l => !/^\s*```/.test(l) && !TABLE_SEP.test(l) && !/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l))
      .map(l => l.replace(/^\s*(#{1,6}|>)\s*/, "").replace(/^\s*\|(.*?)\|?\s*$/, (_, r) => r.split("|").map(c => c.trim()).join(" · ")))
      .join(" ").replace(/\*\*([^*]+)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1")
      .replace(/\s+/g, " ").trim();
    return s.length > (n || 140) ? s.slice(0, n || 140) + "…" : s;
  }

  // progress of the checklist: [done, all]
  function progress(checklist) {
    let d = 0, n = 0;
    ((checklist && checklist.steps) || []).forEach(s => (s.items || []).forEach(it => { n++; if (it[0]) d++; }));
    return [d, n];
  }

  const api = {
    STATUS, GATE_KIND, MS_STATUS, NOTE_KEYS, plainName, agentName, clock, stamp, ago, duration, shownOf, messageText,
    messageStatus, messageTone, isNotice, agentTags, jobLine, workerActions, sortedMessages, mergeMessages, talkLog,
    openGates, answeredWaiting, gateAnswer, newId, backoff, applyChoice, efforts, markdown, preview,
    progress, LEVELS, MESSAGE_PAGE, LIST_PAGE, messageLevel, latest, pageQuery, shortModel, characterSize,
    messageException, awaitsAnswer, statusHint,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.OrchLogic = api;
})(typeof window !== "undefined" ? window : globalThis);
