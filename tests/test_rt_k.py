"""Package K: the dashboard page (orch/runtime/page). No browser: static checks of the files, small logic tests and a
smoke run of the whole script against a small fake DOM, both through `node` (skipped when it is not installed).

Requirement ids: UI-1 (status board), UI-2 (messages, steps panel), UI-3 (controls), UI-4 (notes), UI-5 (no review file),
UI-6 (old runs read)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from rt_helpers import *  # noqa: F401,F403  (puts the repository on the path)
from orch.runtime import contracts as C

PAGE = Path(__file__).resolve().parents[1] / "orch" / "runtime" / "page"
FILES = ["index.html", "style.css", "logic.js", "app.js"]
NODE = shutil.which("node")


def read(name):
    return (PAGE / name).read_text(encoding="utf-8")


def all_text():
    return "\n".join(read(f) for f in FILES)


def user_commands():
    out, todo = [], list(C.UserCommand.__subclasses__())
    while todo:
        c = todo.pop()
        out.append(c.__name__)
        todo += c.__subclasses__()
    return sorted(out)


class TestPageFiles(unittest.TestCase):
    def test_UI_3_page_files_exist(self):
        for f in FILES:
            self.assertTrue((PAGE / f).is_file(), f)

    def test_page_uses_only_api_paths_of_the_contract(self):
        src = Path(C.__file__).read_text(encoding="utf-8")
        listed = set(re.findall(r"^# (?:GET|POST)\s+/api/(\w+)", src, re.M))
        listed |= set(re.findall(r"/api/(\w+)", src[src.index("# GET  /api/state"):src.index("def public_state")]))
        self.assertTrue({"state", "command", "events", "messages", "live", "run", "runs"} <= listed, listed)
        used = set(re.findall(r"""["'`]/?api/(\w+)""", read("app.js") + read("logic.js") + read("index.html")))
        self.assertTrue(used, "the page uses no API")
        self.assertEqual(used - listed, set())

    CLI_ONLY = {"TerminalOpen", "TerminalClose"}  # they come from `orch terminal`; the page shows its command
    # SendToCritic is refused (a chat with a worker stays with the user); StopOrch has no button so it cannot be hit by
    # accident: Orch is closed from the terminal
    NO_CONTROL = {"SendToCritic", "StopOrch"}

    def test_UI_3_every_user_command_has_a_control(self):
        js = read("app.js") + read("logic.js")
        for name in user_commands():
            if name in self.CLI_ONLY or name in self.NO_CONTROL:
                continue
            self.assertRegex(js, r"""["']%s["']""" % name, f"no control sends {name}")

    def test_UI_3_terminal_command_is_shown_to_copy(self):
        self.assertIn("orch terminal", read("app.js"))

    def test_no_text_is_inserted_as_html(self):
        bad = r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|createContextualFragment|DOMParser|srcdoc|\beval\(|new Function|javascript:|setAttribute\(\s*[\"']on"
        for f in FILES:
            self.assertIsNone(re.search(bad, read(f)), f)

    def test_works_offline_no_outside_resources(self):
        text = all_text()
        self.assertIsNone(re.search(r"https?://|@import|//cdn", text))
        html = read("index.html")
        for ref in re.findall(r"""(?:src|href)="([^"]+)""", html):
            self.assertTrue(ref.startswith("#") or ref.startswith("data:") or (PAGE / ref).is_file(), ref)

    def test_UI_5_no_review_file(self):
        self.assertNotRegex(all_text().lower(), r"review file|review_file|api/review")

    def test_light_and_dark_phone_and_keyboard(self):
        css = read("style.css")
        self.assertIn("prefers-color-scheme:dark", css.replace(" ", ""))
        self.assertIn('[data-theme="dark"]', css)
        self.assertIn("max-width:600px", css.replace(" ", ""))
        self.assertIn("focus-visible", css)
        self.assertIn('name="viewport"', read("index.html"))

    def test_UI_4_notes_are_saved_with_the_digest_they_were_edited_from(self):
        js = read("app.js")
        self.assertIn("AuditNotes", js)
        self.assertIn("base:", js)


# ----------------------------------------------------------------------------------------------- node tests

LOGIC_JS = r"""
const L = require(process.argv[2]);
const assert = require('assert');
const T = {
  names() {
    assert.strictEqual(L.plainName('worker-2'), 'Worker 2');
    assert.strictEqual(L.agentName({'worker-2': {display: 'Ada'}}, 'worker-2'), 'Ada');
    assert.strictEqual(L.plainName('user'), 'You');
  },
  status() {
    const m = {status: 'waiting', holds: ['terminal'], drop: null};
    assert.ok(L.messageStatus(m).includes('terminal'));
    assert.ok(L.messageStatus({status: 'failed', drop: 'rejected'}).startsWith('Not sent'));
    assert.strictEqual(L.messageStatus({status: 'answered', holds: []}), 'Answered');
    assert.strictEqual(L.STATUS.waiting_job, 'Waiting for job');
  },
  text() {
    assert.strictEqual(L.messageText({body: 'hi', text: null}), 'hi');
    assert.strictEqual(L.messageText({body: 'hi', text: {key: 'k', shown: 'Orch says: hi'}}), 'Orch says: hi');
    assert.strictEqual(L.messageText({body: 'hi', text: {key: 'k', shown: 'Orch says'}}), 'Orch says\n\nhi');
  },
  markdown() {
    const b = L.markdown('Hello **you** and `x`\n\n- a\n- b\n\n```\n<b>raw</b>\n```');
    assert.strictEqual(b[0].t, 'p'); assert.strictEqual(b[1].t, 'ul'); assert.strictEqual(b[2].t, 'code');
    assert.strictEqual(b[2].s, '<b>raw</b>');   // kept as plain text; the page puts it in as text
    const t = L.markdown('Scores:\n| Criterion | Score |\n|---|---:|\n| greets `a|b` | **5** |\n| tests | 4.5 |\nAfter.');
    assert.deepStrictEqual(t.map(x => x.t), ['p', 'table', 'p']);
    assert.deepStrictEqual(t[1].align, ['', 'right']);
    assert.strictEqual(t[1].rows.length, 2);
    assert.deepStrictEqual(t[1].rows[0][0], [{t: 'text', s: 'greets '}, {t: 'code', s: 'a|b'}]);
    assert.strictEqual(L.markdown('a | b is not a table')[0].t, 'p');
    const o = L.markdown('> said\n---\n*it* x*y* [d](https://e.org) [x](javascript:1)');
    assert.deepStrictEqual(o.map(x => x.t), ['quote', 'hr', 'p']);
    assert.deepStrictEqual(o[2].lines[0].map(x => x.t), ['i', 'text', 'a', 'text']);
    assert.strictEqual(L.preview('| A | B |\n|---|---|\n| `x` | **2** |\n# Next'), 'A · B x · 2 Next');   // no Markdown symbols in a folded row
  },
  gateAnswer() {
    const g = {id: 'g1'};
    assert.deepStrictEqual(L.gateAnswer(g, {option: 0}), {type: 'GateAnswer', gate_id: 'g1', option: 0});
    assert.deepStrictEqual(L.gateAnswer(g, {approve: false, text: 'no'}), {type: 'GateAnswer', gate_id: 'g1', text: 'no', approve: false});
    assert.deepStrictEqual(L.gateAnswer(g, {}), {type: 'GateAnswer', gate_id: 'g1'});
  },
  gates() {
    const gs = {a: {id: 'a', kind: 'pause', opened: 1, closed: 0}, b: {id: 'b', kind: 'question', opened: 2, closed: 0},
                c: {id: 'c', kind: 'plan', opened: 0, closed: 5, answer: {text: 'x'}, sent_in: '', withdrawn: false}};
    assert.deepStrictEqual(L.openGates(gs).map(g => g.id), ['b', 'a']);
    assert.deepStrictEqual(L.answeredWaiting(gs).map(g => g.id), ['c']);
  },
  choice() {
    const b = L.applyChoice({type: 'WorkerClose', id: 'one', agent: 'worker-1'}, {label: 'x', set: {jobs: 'stop'}}, 'two');
    assert.deepStrictEqual(b, {type: 'WorkerClose', id: 'two', agent: 'worker-1', jobs: 'stop'});
  },
  backoff() {
    assert.strictEqual(L.backoff(0), 1000); assert.strictEqual(L.backoff(2), 4000); assert.strictEqual(L.backoff(20), 10000);
  },
  talk() {
    const ms = [{id: '1', sender: 'user', recipient: 'manager'}, {id: '2', sender: 'critic', recipient: 'user'},
                {id: '3', sender: 'orch', recipient: 'user'}, {id: '4', sender: 'worker-1', recipient: 'user'}];
    assert.deepStrictEqual(L.talkLog(ms, 'manager', 'manager').map(m => m.id), ['1', '3']);
  },
  workerActions() {
    assert.deepStrictEqual(L.workerActions({lifecycle: 'open'}), ['WorkerClose', 'WorkerDelete']);
    assert.deepStrictEqual(L.workerActions({lifecycle: 'closed'}), ['WorkerReopen', 'WorkerDelete']);
    assert.deepStrictEqual(L.workerActions({lifecycle: 'deleted'}), []);
  },
  efforts() {
    assert.deepStrictEqual(L.efforts([{id: 'a', efforts: ['low', 'high']}], 'a'), ['low', 'high']);
    assert.deepStrictEqual(L.efforts([{id: 'a', efforts: []}], 'a'), []);
  },
  progress() {
    assert.deepStrictEqual(L.progress({steps: [{items: [[true, 'a'], [false, 'b']]}]}), [1, 2]);
  },
  timeline() {
    const roster = {lead: {role: 'manager'}, reviewer: {role: 'critic'}, helper: {role: 'worker'}, lookout: {role: 'monitor'}};
    const level = (sender, recipient) => L.messageLevel({sender, recipient}, roster);
    assert.strictEqual(level('user', 'helper'), 'you');
    assert.strictEqual(level('orch', 'lead'), 'you');
    assert.strictEqual(level('lead', 'reviewer'), 'critic');
    assert.strictEqual(level('orch', 'reviewer'), 'critic');
    assert.strictEqual(level('helper', 'reviewer'), 'workers');
    assert.strictEqual(level('orch', 'helper'), 'workers');
    assert.strictEqual(level('lookout', 'reviewer'), 'monitor');
    assert.strictEqual(level('orch', 'lookout'), 'monitor');
    assert.strictEqual(L.MESSAGE_PAGE, 30);
    assert.deepStrictEqual(L.latest([1, 2, 3, 4], 2), {rows: [3, 4], remaining: 2});
    assert.deepStrictEqual(L.latest([1, 2], 0), {rows: [], remaining: 2});
    const p = new URLSearchParams(L.pageQuery({before: 12, q: ' sample text ', agent: 'helper', levels: ['you', 'workers'], notes: false}));
    assert.deepStrictEqual(Object.fromEntries(p), {timeline: '1', limit: '30', levels: 'you,workers', notes: '0', before: '12', q: 'sample text', agent: 'helper'});
    assert.strictEqual(new URLSearchParams(L.pageQuery({levels: []})).get('levels'), 'none');
    assert.strictEqual(L.shortModel('zenmux/deepseek/deepseek-v4-pro'), 'deepseek-v4-pro');
    assert.strictEqual(L.shortModel('claude-haiku'), 'claude-haiku');
    assert.strictEqual(L.shortModel(null), '');
    assert.deepStrictEqual([12000, 1400, 1000, 39].map(L.characterSize), ['12k', '1.4k', '1k', '39']);
    assert.strictEqual(L.messageException({status: 'answered'}), '');
    assert.strictEqual(L.messageException({status: 'waiting', holds: ['turn']}), 'Held');
    assert.strictEqual(L.messageException({status: 'failed', drop: 'rejected'}), 'Dropped');
    assert.ok(L.awaitsAnswer({status: 'delivered', needs_answer: true}));
    assert.ok(!L.awaitsAnswer({status: 'answered', needs_answer: true}));
  },
  groupReplies() {
    const m = (id, to, body, extra) => ({id, sender: 'critic', recipient: to, body, turn: 't1', status: 'delivered', ...extra});
    const rows = L.groupReplies([m('a', 'manager', 'Report.'), m('b', 'worker-2', 'Same task.'), m('c', 'worker-1', 'Same task.'),
                                 {id: 'd', sender: 'critic', recipient: 'worker-1', body: 'Same task.', turn: 't0', status: 'delivered'}]);
    // one line per output: the turn t1 is one item, its messages in the order written, the same text merged
    assert.deepStrictEqual(rows.map(r => [r.id, (r.parts || []).map(p => [p.id, p.also || []])]),
      [['c', [['c', ['worker-2']], ['a', []]]], ['d', []]]);
  },
  time() {
    assert.strictEqual(L.ago(0, 130), '–'); assert.strictEqual(L.ago(null, 130), '–');
    assert.strictEqual(L.ago(100, 130), 'just now'); assert.strictEqual(L.ago(100, 100 + 600), '10 min ago');
    assert.strictEqual(L.duration(125), '2 min');
  },
};
T[process.argv[3]]();
"""


@unittest.skipUnless(NODE, "node is not installed")
class TestLogic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        (cls.tmp / "logic_test.js").write_text(LOGIC_JS, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_js(self, name):
        r = subprocess.run([NODE, str(self.tmp / "logic_test.js"), str(PAGE / "logic.js"), name], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_UI_1_names_are_plain(self):
        self.run_js("names")

    def test_UI_2_message_status_words(self):
        self.run_js("status")

    def test_UI_2_message_text(self):
        self.run_js("text")

    def test_markdown_keeps_markup_as_text(self):
        self.run_js("markdown")

    def test_UI_3_gate_answer_bodies(self):
        self.run_js("gateAnswer")

    def test_UI_3_gates_order_and_waiting_answers(self):
        self.run_js("gates")

    def test_UI_3_choice_resends_with_new_id(self):
        self.run_js("choice")

    def test_lost_connection_waits_longer_each_time(self):
        self.run_js("backoff")

    def test_UI_3_talk_log_and_send_to_critic(self):
        self.run_js("talk")

    def test_UI_3_worker_controls(self):
        self.run_js("workerActions")

    def test_UI_3_effort_levels_hidden_when_none(self):
        self.run_js("efforts")

    def test_UI_2_progress_count(self):
        self.run_js("progress")

    def test_UI_2_timeline_levels_paging_and_short_models(self):
        self.run_js("timeline")

    def test_UI_1_times(self):
        self.run_js("time")

    def test_one_reply_to_several_is_grouped(self):
        self.run_js("groupReplies")


# ------------------------------------------------------------------------------------------------ smoke run

DOM_JS = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert'), path = require('path');
const dir = process.argv[2], scenario = process.argv[4];
const fixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const created = new Set();
class Txt { constructor(t) { this.nodeType = 3; this.text = String(t); } get textContent() { return this.text; } }
class El {
  constructor(tag) {
    created.add(String(tag).toUpperCase());
    this.tagName = String(tag).toUpperCase(); this.nodeType = 1; this.children = []; this.attrs = {}; this.dataset = {};
    this.listeners = {}; this.parent = null; this.hidden = false; this.value = ''; this.checked = false; this.open = false;
    this.className = ''; this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; this.returnValue = ''; this.disabled = false;
  }
  append(...k) { k.forEach(x => { if (typeof x === 'string') x = new Txt(x); x.parent = this; this.children.push(x); }); }
  replaceChildren(...k) { this.children.forEach(x => { x.parent = null; }); this.children = []; this.append(...k); }
  insertBefore(x, next) {
    if (x.parent) x.remove();
    x.parent = this;
    const at = this.children.indexOf(next);
    if (at === -1) this.children.push(x); else this.children.splice(at, 0, x);
  }
  setAttribute(k, v) { this.attrs[k] = v; if (k === 'disabled') this.disabled = true; }
  removeAttribute(k) { delete this.attrs[k]; }
  get textContent() { return this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this.children = []; this.append(String(v)); }
  get classList() { const s = this; return {
    toggle(c, on) { const l = s.className.split(' ').filter(Boolean).filter(x => x !== c); if (on) l.push(c); s.className = l.join(' '); },
    add(c) { this.toggle(c, true); }, remove(c) { this.toggle(c, false); } }; }
  addEventListener(e, f) { (this.listeners[e] = this.listeners[e] || []).push(f); }
  removeEventListener(e, f) { this.listeners[e] = (this.listeners[e] || []).filter(x => x !== f); }
  fire(e, ev) { (this.listeners[e] || []).slice().forEach(f => f(Object.assign({currentTarget: this, target: this, key: '', shiftKey: false, preventDefault() {}}, ev || {}))); }
  contains(n) { for (let x = n; x; x = x.parent) if (x === this) return true; return false; }
  all() { const out = []; const go = n => { if (n.nodeType === 1) { out.push(n); n.children.forEach(go); } }; go(this); return out; }
  querySelectorAll(sel) {
    const alts = sel.split(',').map(s => s.trim());
    return this.all().slice(1).filter(e => alts.some(a => {
      const m = a.match(/^\[data-(\w+)(?:="([^"]*)")?\]$/);
      if (m) return m[1] in e.dataset && (m[2] === undefined || String(e.dataset[m[1]]) === m[2]);
      return e.tagName === a.toUpperCase();
    }));
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
  focus() { doc.activeElement = this; }
  showModal() { this.open = true; }
  close(v) { this.open = false; this.returnValue = v === undefined ? '' : v; this.fire('close'); }
  setSelectionRange() {}
}
const registry = {};
const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
for (const m of html.matchAll(/<(\w+)([^>]*?)\bid="([^"]+)"([^>]*)>/g)) {
  const e = new El(m[1]); if (/\bhidden\b/.test(m[2] + m[4])) e.hidden = true; registry[m[3]] = e;
}
created.clear();
const doc = {
  activeElement: null, title: '', documentElement: new El('html'),
  getElementById: id => registry[id] || null,
  createElement: t => new El(t), createTextNode: t => new Txt(t),
  querySelector: s => { for (const r of Object.values(registry)) { const f = r.querySelector(s); if (f) return f; } return null; },
  querySelectorAll: s => Object.values(registry).flatMap(r => r.querySelectorAll(s)),
  addEventListener(e, f) { (this.l = this.l || {}); (this.l[e] = this.l[e] || []).push(f); },
};
const posts = [], gets = [];
let state = JSON.parse(JSON.stringify(fixture.state));
let commandReply = () => ({ok: true, shown: '', text: null, choices: [], data: {}});
let liveReply = () => ({items: [{n: 1, at: 1, k: 'text', s: 'thinking'}], next: 2});
let failState = false;
const json = (o, ok = true, status = 200) => Promise.resolve({ok, status, json: () => Promise.resolve(o)});
const sources = [];
class ES { constructor(u) { this.url = u; this.l = {}; sources.push(this); } addEventListener(e, f) { this.l[e] = f; } close() { this.closed = true; } }
async function fetchStub(url, opt) {
  if (opt && opt.method === 'POST') { const b = JSON.parse(opt.body); posts.push(b); return json(commandReply(b)); }
  gets.push(url);
  if (url.startsWith('api/state')) { if (failState) throw new Error('down'); return json(state); }
  if (url.startsWith('api/golden')) return json({text: 'Always be kind.', digest: 'd1', path: '/x'});
  if (url.startsWith('api/audit_notes')) return json({text: 'my notes', digest: 'n1', path: '/y'});
  if (url.startsWith('api/audit')) return json({report: 'All fine.', running: false});
  if (url.startsWith('api/runs')) return json({runs: [{id: 'r0', project: 'Old one', status: 'done', started: 1, old: true}]});
  if (url.startsWith('api/run?')) return json({tl_items: [{ts: '2026-01-01T10:00:00', from: 'manager', to: 'critic', text: 'old words'}], chat: [], agents: [{name: 'manager', role: 'manager', called: 'Manager'}, {name: 'worker-1', role: 'worker'}], tl_groups: []});
  if (url.startsWith('api/live')) return json(liveReply(url));
  if (url.startsWith('api/message?')) return json({message: {rendered: 'SENT TEXT'}});
  if (url.startsWith('api/turn')) return json({input: 'TURN INPUT\nSecond paragraph', output: 'WHOLE REPLY', system_prompt: 'SYSTEM INSTRUCTIONS'});
  if (url.startsWith('api/messages')) {
    const p = new URLSearchParams(url.split('?')[1]);
    const levels = (p.get('levels') || '').split(',');
    const q = (p.get('q') || '').toLowerCase(), agent = p.get('agent'), before = Number(p.get('before')), limit = Number(p.get('limit'));
    const off = (p.get('off') || '').split(',').filter(Boolean);
    const matching = win.OrchLogic.sortedMessages(state.messages).filter(m =>
      (!before || m.seq < before) && (!agent || m.sender === agent || m.recipient === agent) &&
      (!q || m.body.toLowerCase().includes(q)) && levels.includes(win.OrchLogic.messageLevel(m, state.agents)) &&
      (p.get('notes') !== '0' || !win.OrchLogic.isNotice(m)) && !off.includes(m.sender === 'orch' ? m.recipient : m.sender));
    const {rows, remaining} = win.OrchLogic.latest(matching, limit);
    return json({messages: rows, remaining, more: remaining > 0});
  }
  return json({}, false, 404);
}
const store = scenario === "restoredFilters" ? {"orch-message-hidden": '["critic"]', "orch-message-notes": "0", "orch-message-search": "Hello manager"} : {}, clipboard = [];
const win = {
  OrchLogic: require(path.join(dir, 'logic.js')),
  localStorage: {getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }},
  sessionStorage: {getItem: () => null, setItem: () => {}},
  matchMedia: () => ({matches: false}), addEventListener() {},
};
const sandbox = {window: win, document: doc, fetch: fetchStub, EventSource: ES, navigator: {clipboard: {writeText: text => { clipboard.push(text); return Promise.resolve(); }}},
  setTimeout: (f, ms) => setTimeout(f, Math.min(ms || 0, 5)), clearTimeout, setInterval: () => 0, URLSearchParams, console, Date, JSON, Math,
  encodeURIComponent, Promise};
sandbox.window.window = win;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(dir, 'app.js'), 'utf8'), sandbox);
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)); };
const $ = id => registry[id];
const everything = () => Object.values(registry).flatMap(r => r.all());
const buttons = root => (root ? root.all() : everything()).filter(e => e.tagName === 'BUTTON');
const byText = (root, t) => buttons(root).find(b => b.textContent === t);
const click = async b => { assert.ok(b, 'button missing'); b.fire('click'); await settle(); };
const lastPost = () => posts[posts.length - 1];
const type = (el, v) => { el.value = v; el.fire('input'); };
const textParts = e => e.nodeType === 3 ? e.textContent : e.children.map(textParts).join(' ');
const pageText = () => Object.values(registry).map(textParts).join('\n');
const refresh = async () => { sources[0].l.state({data: '{}'}); await settle(); };
const ctx = {assert, $, clipboard, byText, click, lastPost, posts, gets, type, settle, pageText, refresh, buttons, everything, sources, doc,
  created, fixture, win, store,
  setState: s => { state = s; }, getState: () => state, setReply: f => { commandReply = f; }, setLiveReply: f => { liveReply = f; }, setFail: v => { failState = v; }};
(async () => {
  if (sources[0] && sources[0].onopen) sources[0].onopen();
  await settle();
  await SC[scenario](ctx);
  process.exit(0);
})().catch(e => { console.error(e.stack || e); process.exit(1); });
"""

SCENARIOS_JS = r"""
global.SC = {
  async board({assert, $, created, getState, refresh, byText, click}) {
    const t = $('team').textContent;
    assert.ok(t.includes('Worker 1') && t.includes('Writing'), t);       // UI-1: status of each agent
    assert.ok(t.includes('Waiting for job') || t.includes('Closed'), t);
    assert.ok($('title').textContent === 'demo');
    assert.ok(!created.has('IMG') && !created.has('SCRIPT'), 'no element made from text');
    const s = getState(), assignment = s.agents['worker-1'].assignment;
    s.messages[assignment.message_id] = {id: assignment.message_id, sender: 'critic', recipient: 'worker-1',
      seq: 10, created: s.now - 600, status: 'answered', body: 'Fix the parser.\n\nPreserve the last comparison in the report.'};
    await refresh();
    const card = $('team').all().find(el => el.className.startsWith('agent status-row-') && el.textContent.includes('Worker 1'));
    const fold = card.all().find(el => el.className === 'agent-sum');
    const panel = card.all().find(el => el.className === 'agent-body');
    const buttons = card.all().filter(el => el.tagName === 'BUTTON' && el.textContent === '…');
    assert.strictEqual(buttons.length, 1);
    assert.ok(panel.hidden);
    await click(buttons[0]);
    assert.ok($('dlg').open && $('dlg-h').textContent.includes('Worker 1'));
    assert.ok(panel.hidden, 'opening details leaves the sidebar folded');
    assert.ok($('dlg-b').textContent.includes('1.0 actual'), 'the model the CLI reports');
    for (const view of [card, $('dlg-b')]) {
      const tag = view.all().find(el => el.className === 'assignment-tag');
      assert.ok(tag && tag.textContent === '→' && tag.attrs['aria-label'] === 'Last assignment');
    }
    assert.ok($('dlg-b').textContent.includes('Preserve the last comparison in the report.'));
    await click(byText($('dlg-a'), 'Close'));
    assert.ok(!$('dlg').open);
    await click(fold);
    assert.ok(!panel.hidden && !$('dlg').open && panel.textContent.includes('1.0 actual'));
    await click(buttons[0]);
    assert.ok(!panel.hidden && $('dlg').open, 'the window also opens from an unfolded card');
    await click(byText($('dlg-a'), 'Close'));
    await click(fold);
    assert.ok(panel.hidden);
  },
  async messages({assert, $, pageText, created}) {
    const m = $('messages').textContent;                                 // UI-2
    assert.ok(m.includes('<img src=x onerror=alert(1)>'), 'shown as text');
    assert.ok(m.includes('Held'), 'status shown');
    assert.ok($('progress').textContent.includes('Build it'));           // steps panel
    assert.ok(!created.has('IMG'));
  },
  async nothingInternal({assert, pageText, fixture}) {
    const t = pageText();
    for (const id of fixture.internal) assert.ok(!t.includes(id), 'internal id shown: ' + id);
    assert.ok(!/\b(MSG|GATE|SIL|REC|MOD|JOB)-\d/.test(t));
    assert.ok(!t.includes('model_id') && !t.includes('session'));
  },
  async gateOption({assert, $, byText, click, lastPost}) {
    assert.ok(!$('needs').hidden);
    await click(byText($('needs'), 'Yes'));
    assert.deepStrictEqual(lastPost().type, 'GateAnswer');
    assert.strictEqual(lastPost().option, 0); assert.strictEqual(lastPost().gate_id, 'gq');
    assert.ok(lastPost().id);
  },
  async gateText({assert, $, byText, click, lastPost, everything, type}) {
    const ta = $('needs').all().find(e => e.tagName === 'TEXTAREA');
    type(ta, 'my own answer');
    await click(byText($('needs'), 'Send answer'));
    assert.strictEqual(lastPost().text, 'my own answer');
  },
  async approveGates({assert, $, byText, click, lastPost, posts}) {
    await click(byText($('needs'), 'Approve'));
    assert.strictEqual(lastPost().approve, true);
    await click(byText($('needs'), 'Ask for changes'));      // nothing written: not sent
    assert.strictEqual(posts.length, 1);
    assert.ok($('needs').textContent.includes('Proposed'));  // old and new side by side
  },
  async pauseClose({assert, $, byText, click, lastPost}) {
    await click(byText($('needs'), 'Continue'));
    assert.strictEqual(lastPost().type, 'GateAnswer'); assert.strictEqual(lastPost().gate_id, 'gp');
    assert.strictEqual(lastPost().option, undefined);
  },
  async typedTextSurvivesUpdate({assert, $, type, refresh, getState, setState, doc}) {
    const ta = () => $('needs').all().find(e => e.tagName === 'TEXTAREA');
    type(ta(), 'half written');
    ta().focus();
    const s = getState(); s.rev = (s.rev || 0) + 1; s.rounds = 99; s.gates.gq.text = 'A changed question';
    setState(s); await refresh();
    assert.strictEqual(ta().value, 'half written');
    assert.ok($('needs').textContent.includes('A changed question'));
    assert.strictEqual(doc.activeElement, ta(), 'focus kept');
  },
  async lostConnection({assert, $, sources, settle, setFail}) {
    assert.ok($('lost').hidden);
    setFail(true); sources[0].onerror(); await settle();
    assert.ok($('lost').hidden, 'not at once: a reconnect may follow');
    await new Promise(r => setTimeout(r, 20)); await settle();
    assert.ok(!$('lost').hidden, 'shown');
    setFail(false); await new Promise(r => setTimeout(r, 30)); await settle();
    const es = sources[sources.length - 1]; assert.ok(sources.length > 1, 'reconnects by itself');
    es.onopen(); await settle();
    assert.ok($('lost').hidden, 'recovered');
  },
  async lostWhenStateFails({assert, $, setFail, refresh}) {
    setFail(true); await refresh(); await new Promise(r => setTimeout(r, 20)); assert.ok(!$('lost').hidden);
  },
  async plannedStreamEnd({assert, $, sources, settle}) {
    const n = sources.length;
    sources[0].l.bye({data: '{}'}); await settle();
    assert.ok(sources[0].closed, 'old stream closed');
    assert.strictEqual(sources.length, n + 1, 'a new stream at once');
    await new Promise(r => setTimeout(r, 20)); await settle();
    assert.ok($('lost').hidden, 'no lost banner');
    sources[sources.length - 1].onerror(); sources[sources.length - 1].onopen(); await settle();
    await new Promise(r => setTimeout(r, 20)); await settle();
    assert.ok($('lost').hidden, 'a reconnect that works in time shows nothing');
  },
  async header({assert, $, byText, click, lastPost, posts}) {
    await click($('nightbtn')); assert.deepStrictEqual([lastPost().type, lastPost().on], ['SetNight', true]);
    await click($('addworker')); assert.ok($('dlg').open);   // the model to start it on
    await click(byText($('dlg-a'), 'Add worker')); assert.strictEqual(lastPost().type, 'WorkerAdd');
    await click($('stopbtn')); assert.ok($('dlg').open);
    await click(byText($('dlg-a'), 'Stop agents')); assert.deepStrictEqual([lastPost().type, lastPost().agents], ['StopAgents', []]);
    await click($('updatebtn')); await click(byText($('dlg-a'), 'Update')); assert.strictEqual(lastPost().type, 'UpdateOrch');
    assert.ok(!$('header-actions').textContent.includes('Close'));   // no Close Orch on the page: too easy to hit
  },
  async addWorkerModel({assert, $, byText, click, lastPost}) {
    await click($('addworker'));
    const sel = $('dlg-b').all().find(e => e.tagName === 'SELECT');   // the first: the model
    sel.value = 'alpha'; sel.fire('change');
    await click(byText($('dlg-a'), 'Add worker'));
    assert.deepStrictEqual([lastPost().type, lastPost().model_id, lastPost().effort], ['WorkerAdd', 'alpha', 'low']);
  },
  async startAgents({assert, $, click, lastPost, getState, setState, refresh}) {
    const s = getState(); s.agents['worker-1'].stopped_by_user = true; setState(s); await refresh();
    assert.strictEqual($('stopbtn').textContent, 'Start agents');
    await click($('stopbtn')); assert.deepStrictEqual([lastPost().type, lastPost().agents], ['StartAgents', []]);
  },
  async workerControls({assert, $, byText, click, lastPost}) {
    const card = $('team').all().filter(e => e.className.split(' ').includes('agent')).find(c => c.textContent.includes('Worker 1'));
    await click(byText(card, '…'));
    await click(byText($('dlg-b'), 'Close worker')); assert.deepStrictEqual([lastPost().type, lastPost().agent], ['WorkerClose', 'worker-1']);
    await click(byText($('dlg-a'), 'Close'));
    const card2 = $('team').all().filter(e => e.className.startsWith('agent')).find(c => c.textContent.includes('Worker 2'));
    await click(byText(card2, '…'));
    await click(byText($('dlg-b'), 'Reopen worker')); assert.strictEqual(lastPost().type, 'WorkerReopen');
    await click(byText($('dlg-b'), 'Delete worker')); await click(byText($('dlg-a2'), 'Delete')); assert.strictEqual(lastPost().type, 'WorkerDelete');
  },
  async jobChoice({assert, $, byText, click, lastPost, posts, setReply}) {
    let n = 0;
    setReply(b => n++ === 0 ? {ok: false, shown: 'A job is running.', text: null, data: {}, choices: [{label: 'Let it finish', set: {jobs: 'finish'}}, {label: 'Stop it', set: {jobs: 'stop'}}]}
                            : {ok: true, shown: '', choices: [], data: {}});
    const card = $('team').all().filter(e => e.className.split(' ').includes('agent')).find(c => c.textContent.includes('Worker 1'));
    await click(byText(card, '…'));
    await click(byText($('dlg-b'), 'Close worker'));
    assert.ok($('dlg2').open && $('dlg-b2').textContent.includes('A job is running.'));
    await click(byText($('dlg-a2'), 'Stop it'));
    assert.strictEqual(posts.length, 2); assert.strictEqual(lastPost().jobs, 'stop');
    assert.notStrictEqual(posts[0].id, posts[1].id);
  },
  async talk({assert, $, click, lastPost, type, byText, clipboard}) {
    await click($('talkbtn')); assert.ok(!$('talk').hidden);
    assert.ok($('talklog').textContent.includes('Hello manager'));
    type($('talkin'), 'please continue'); await click($('talksend'));
    assert.deepStrictEqual([lastPost().type, lastPost().agent, lastPost().text], ['Chat', 'manager', 'please continue']);
    assert.strictEqual($('talkin').value, '');
    await click(byText($('talkacts'), 'Discuss')); assert.deepStrictEqual([lastPost().type, lastPost().agent], ['DiscussionStart', 'manager']);
    const terminal = byText($('talkacts'), 'Terminal');
    assert.strictEqual(terminal.attrs.title, 'orch terminal demo manager');
    await click(terminal);
    assert.deepStrictEqual(clipboard, ['orch terminal demo manager']);
  },
  async enterSends({assert, $, click, lastPost, type, settle}) {
    await click($('talkbtn')); type($('talkin'), 'by key');
    $('talkin').fire('keydown', {key: 'Enter'}); await settle();
    assert.strictEqual(lastPost().text, 'by key');
  },
  async discussion({assert, $, click, lastPost, byText, type, settle, buttons}) {
    await click($('talkbtn')); $('talkwho').value = 'critic'; $('talkwho').fire('change'); await settle();
    assert.ok($('talkinfo').textContent.includes('discussion is open'));
    await click(byText($('talkacts'), 'Apply discussion'));
    type($('dlg-b').all().find(e => e.tagName === 'TEXTAREA'), 'go ahead');
    await click(byText($('dlg-a'), 'Apply'));
    assert.deepStrictEqual([lastPost().type, lastPost().agent, lastPost().text], ['DiscussionApply', 'critic', 'go ahead']);
    await click(byText($('talkacts'), 'End without applying')); await click(byText($('dlg-a'), 'End it'));
    assert.deepStrictEqual([lastPost().type, lastPost().agent], ['DiscussionEnd', 'critic']);
  },
  async terminalAndNotices({assert, $, click, byText, lastPost, posts}) {
    assert.ok($('notices').textContent.includes('A terminal is open on Critic'));
    await click(byText($('notices'), 'Take back'));
    assert.deepStrictEqual([lastPost().type, lastPost().agent], ['TerminalTakeBack', 'critic']);
    assert.ok(!$('notices').textContent.includes('Worker 1 stopped'));
    assert.ok($('messages').all().some(e => e.className.includes('notice-row') && e.textContent.includes('Worker 1 stopped')));
    await click(byText($('notices'), 'Remove old files')); assert.strictEqual(lastPost().type, 'ConfirmMigration');
  },
  async milestones({assert, $, click, byText, lastPost, type}) {
    await click(byText($('progress'), 'Reopen')); assert.deepStrictEqual([lastPost().type, lastPost().milestone_id], ['ReopenMilestone', 'm0']);
    await click($('changems')); type($('dlg-b').all().find(e => e.tagName === 'TEXTAREA'), 'add a docs step');
    await click(byText($('dlg-a'), 'Ask'));
    assert.deepStrictEqual([lastPost().type, lastPost().text], ['ChangeMilestones', 'add a docs step']);
  },
  async models({assert, $, click, byText, lastPost, type}) {
    const card = $('team').all().filter(e => e.className.split(' ').includes('agent')).find(c => c.textContent.includes('Worker 1'));
    await click(byText(card, '…'));
    await click(byText($('dlg-b'), 'Model'));
    assert.ok($('dlg').open); const body = $('dlg-b');
    assert.ok(body.textContent.includes('Alpha') && body.textContent.includes('Backups'));
    const sels = body.all().filter(e => e.tagName === 'SELECT'); assert.strictEqual(sels.length, 2, 'model and effort');
    sels[0].value = 'beta'; sels[0].fire('change'); await new Promise(r => setTimeout(r, 5));
    const eff = body.all().filter(e => e.tagName === 'SELECT')[1]; eff.value = 'high'; eff.fire('change');
    await click(byText(body, 'Switch'));
    assert.deepStrictEqual([lastPost().type, lastPost().agent, lastPost().model_id, lastPost().effort], ['ChooseModel', 'worker-1', 'beta', 'high']);
    await click(byText(body, 'Use'));
    assert.deepStrictEqual([lastPost().type, lastPost().backup], ['ChooseModel', 'slow']);
  },
  async switchPending({assert, $, click, byText, lastPost}) {
    const card = $('team').all().filter(e => e.className.split(' ').includes('agent')).find(c => c.textContent.includes('Worker 1'));
    assert.ok(card.textContent.includes('Switching model'));
    await click(byText(card, '…'));
    await click(byText($('dlg-b'), 'Model'));
    assert.ok($('dlg-b').textContent.includes('Switch pending'));
    await click(byText($('dlg-b'), 'Switch now')); assert.deepStrictEqual([lastPost().type, lastPost().agent], ['ForceSwitch', 'worker-1']);
  },
  async teamFold({assert, $, click}) {
    const cards = () => $('team').all().filter(e => e.className.split(' ').includes('agent'));
    const n = cards().length;
    assert.ok(n >= 3);
    await click($('teamfold'));
    assert.ok($('workspace').className.includes('team-folded'));
    const rail = $('team').all().filter(e => e.className === 'rail-agent');
    assert.strictEqual(rail.length, n);
    assert.strictEqual(cards().length, 0);
    await click(rail[2]);
    assert.ok($('workspace').className.includes('team-folded'));
    assert.ok($('dlg').open && $('dlg-h').textContent.includes('Worker 1'));
  },
  async golden({assert, $, click, byText, lastPost, type, settle}) {
    const card = $('goldcard'); card.open = true; card.fire('toggle'); await settle();
    assert.ok($('golden').textContent.includes('Always be kind.'));
    await click(byText($('golden'), 'Edit'));
    const ta = $('golden').all().find(e => e.tagName === 'TEXTAREA'); type(ta, 'Always be kind and brief.');
    await click(byText($('golden'), 'Save'));
    assert.deepStrictEqual([lastPost().type, lastPost().text, lastPost().base], ['EditGolden', 'Always be kind and brief.', 'd1']);
  },
  async audit({assert, $, click, byText, lastPost, type, settle}) {
    const card = $('auditcard'); card.open = true; card.fire('toggle'); await settle();
    assert.ok($('audit').textContent.includes('All fine.'));
    await click(byText($('audit'), 'Run audit')); assert.strictEqual(lastPost().type, 'AuditRun');
    await click(byText($('audit'), 'Edit notes'));
    type($('audit').all().find(e => e.tagName === 'TEXTAREA'), 'new notes');
    await click(byText($('audit'), 'Save'));
    assert.deepStrictEqual([lastPost().type, lastPost().text, lastPost().base], ['AuditNotes', 'new notes', 'n1']);
  },
  async messageDetail({assert, $, gets, refresh, getState, settle}) {
    const detail = id => $('messages').all().find(e => e.dataset.message === id);
    const open = async node => { assert.ok(node); node.open = true; node.fire('toggle'); await settle(); };
    const fold = (row, label) => row.all().find(e => e.tagName === 'DETAILS' && e.children[0].textContent === label);
    const row = detail('mu');
    await open(row);
    assert.ok(!gets.some(u => u.startsWith('api/message?')));
    assert.ok(!fold(row, 'What Manager received'));   // what the recipient got is not shown
    assert.ok(!row.children[1].textContent.includes('Delivered.'));   // the status sits in the head line, not before the text
    const written = detail('mr');
    await open(written);
    assert.ok(!gets.some(u => u.startsWith('api/turn')));
    const input = fold(written, "Input to the manager's turn that wrote this");
    await open(input);
    assert.ok(input.textContent.includes('TURN INPUT'));
    assert.ok(!input.textContent.includes('SYSTEM INSTRUCTIONS'));
    await open(fold(input, 'System instructions for Manager'));
    assert.ok(input.textContent.includes('SYSTEM INSTRUCTIONS'));
    await open(fold(written, "The manager's whole reply"));
    assert.ok(written.textContent.includes('WHOLE REPLY'));
    const state = getState();
    state.messages.mr.status = 'delivered';
    await refresh();
    assert.ok(detail('mr').open && fold(detail('mr'), "The manager's whole reply").open);
    assert.strictEqual(gets.filter(u => u.startsWith('api/turn')).length, 1);
  },
  async restoredFilters({assert, $, byText, gets}) {
    assert.strictEqual(byText($('who'), 'Critic').attrs['aria-pressed'], 'false');
    assert.strictEqual($('msearch').value, 'Hello manager');
    assert.strictEqual($('orch-notes').checked, false);
    const first = new URLSearchParams(gets.find(u => u.startsWith('api/messages?')).split('?')[1]);
    assert.deepStrictEqual([first.get('off'), first.get('q'), first.get('notes')], ['critic', 'Hello manager', '0']);
  },
  async timelinePage({assert, $, byText, click, gets, refresh, getState, settle, store, type}) {
    const state = getState();
    for (let seq = 6; seq <= 83; seq++) {
      const m = {id: 'typed-' + seq, seq, sender: 'manager', recipient: 'critic', body: 'Record ' + seq, status: 'answered'};
      state.messages[m.id] = m;
    }
    await refresh();
    const ids = () => $('messages').children.map(row => row.dataset.message);
    assert.strictEqual(ids().length, 30);
    assert.strictEqual(ids()[0], 'typed-83');   // the newest on top
    assert.strictEqual(ids().at(-1), 'typed-54');
    assert.strictEqual($('remaining').textContent, '53 older');
    assert.ok(!$('show-all').hidden);
    await click($('older'));
    assert.strictEqual(ids().length, 60);
    assert.ok(gets.some(url => url.includes('before=54')));
    assert.strictEqual(ids()[0], 'typed-83');   // older ones go below
    assert.strictEqual(ids().at(-1), 'typed-24');
    assert.ok($('show-all').hidden);
    await click($('older'));
    assert.strictEqual(ids().length, 83);
    assert.ok($('message-pages').hidden);
    await click(byText($('who'), 'Critic'));   // one chip per agent: the critic's lines go
    assert.ok(ids().length < 83 && ids().every(id => getState().messages[id].sender !== 'critic'));
    assert.deepStrictEqual(JSON.parse(store['orch-message-hidden']), ['critic']);
    await click(byText($('who'), 'Critic')); assert.strictEqual(ids().length, 30);   // back on; a filter change starts at the first page
    $('orch-notes').checked = false;
    $('orch-notes').fire('change');
    await settle();
    assert.ok(ids().length < 83);
    assert.strictEqual(store['orch-message-notes'], '0');
    type($('msearch'), 'Hello manager');
    assert.strictEqual(store['orch-message-search'], 'Hello manager');
    await new Promise(resolve => setTimeout(resolve, 12));
    await settle();
    assert.deepStrictEqual(ids(), ['mu']);
    await click(byText($('who'), 'You'));
    assert.ok($('messages').textContent.includes('No messages match.'));
  },
  async answeredWithoutGates({assert, $, getState, refresh}) {
    const gates = getState().gates;
    Object.values(gates).forEach(g => { g.closed = 1; g.answer = {text: 'yes'}; });
    await refresh();
    assert.ok($('needs').hidden);
  },
  async caps({assert, $, byText, click, getState, refresh}) {
    const state = getState();
    for (let seq = 6; seq <= 27; seq++) {
      const m = {id: 'talk-' + seq, seq, sender: 'manager', recipient: 'user', body: 'Conversation ' + seq, status: 'delivered'};
      state.messages[m.id] = m;
    }
    state.checklist.steps[0].items = Array.from({length: 22}, (_, i) => [false, 'Task number ' + (i + 1)]);
    await refresh();
    assert.ok($('progress').textContent.includes('Task number 1'));   // the whole checklist, every step, is shown
    assert.ok($('progress').textContent.includes('Task number 22'));
    await click($('talkbtn'));
    assert.ok(!$('talklog').textContent.includes('Hello manager'));
    assert.ok($('talklog').textContent.includes('Conversation 27'));
    await click(byText($('talklog'), 'Show more'));
    assert.ok($('talklog').textContent.includes('Hello manager'));
  },
  async oldRuns({assert, $, click, byText, settle}) {
    await click($('runsbtn'));
    await click(byText($('dlg-b'), 'Open')); await settle();
    assert.ok($('oldnote').textContent.includes('earlier run')); assert.ok($('messages').textContent.includes('old words'));
    assert.ok($('needs').hidden);
    await click(byText($('oldnote'), 'Back to the current run'));
    assert.ok($('team').textContent.includes('Worker 1'));
  },
  async toolsInsideWriting({assert, $, sources, settle, refresh, getState, setLiveReply}) {
    const state = getState(), turn = 'r001-t02-manager';
    state.agents.manager.status = 'writing';
    state.turns.manager = {id: turn, status: 'running', started: state.now - 2};
    const event = (n, k, s, extra) => ({agent: 'manager', turn, n, at: state.now - 1, k, s, ...extra});
    setLiveReply(() => ({items: [event(1, 'tool', 'Recorded read label')], more: false}));
    await refresh();
    const row = kind => $('messages').children.find(el => el.dataset.message === kind + ':' + turn);
    const tools = () => row('writing').all().filter(e => e.className === 'writing-tool-line');
    assert.ok(!row('input'));
    const input = row('writing').all().find(el => el.className === 'writing-input');
    assert.ok(input.open);
    assert.strictEqual(input.querySelector('pre').textContent, 'TURN INPUT\nSecond paragraph');
    assert.ok(row('writing').children[1].textContent.includes('Waiting for text'));
    assert.deepStrictEqual(tools().map(e => e.attrs.title), ['Recorded read label']);
    assert.ok(!$('team').all().some(el => el.tagName === 'BUTTON' && el.textContent === 'Activity'));

    const stream = (...args) => sources[0].l.live({data: JSON.stringify(event(...args))});
    stream(2, 'tool', 'Read', {tool_id: 'read-1'});
    stream(3, 'tool', 'Read', {tool_id: 'read-1', call: 'read\n{"path":"/tmp/the plan.md"}'});
    await settle();
    assert.ok(!$('messages').children.some(el => el.dataset.message?.startsWith('tool:')));
    assert.deepStrictEqual(tools().map(e => e.attrs.title), ['Recorded read label', 'read: /tmp/the plan.md']);
    stream(4, 'text', 'Draft reply');
    await settle();
    assert.ok(input.open, 'input stays readable while the reply streams');
    assert.strictEqual(row('writing').all().find(e => e.className === 'writing-text').textContent, 'Draft reply');
  },
  async milestoneReviewTables({assert, $, refresh, getState}) {
    const state = getState();
    const table = (criterion, finding) => '| Criterion | Score | Finding |\n|---|---:|---|\n| ' + criterion + ' | 5/5 | ' + finding + ' |';
    state.gates.review = {id: 'review', kind: 'milestone', agent: 'manager', opened: state.now, closed: 0, options: [],
      text: [table('Quality', 'Earlier finding'), 'Recommendation: start Q1 only.',
        table('Quality', 'Revised finding'), table('Readiness', 'Independent check')].join('\n\n'),
      summary: 'Review complete.'};
    await refresh();
    assert.strictEqual($('needs').all().filter(el => el.tagName === 'TABLE').length, 2);
    const text = $('needs').textContent;
    assert.ok(text.includes('Revised finding') && !text.includes('Earlier finding'));
    assert.ok(text.includes('Recommendation: start Q1 only.') && text.includes('Independent check') && text.includes('Review complete.'));
  },
  async jobDetails({assert, $, byText, click, refresh, getState, posts}) {
    const state = getState(), job = state.jobs['worker-1/j7'];
    job.label = 'long command preview';
    job.command = '# ' + 'x'.repeat(230) + '\nnpm test';
    await refresh();
    const preview = () => $('team').all().find(e => e.className === 'job-preview');
    assert.ok(preview() && preview().textContent.includes('npm test'));
    const card = () => $('team').all().find(e => e.className.startsWith('agent status-row-') && e.textContent.includes('Worker 1'));
    await click(byText(card(), '…'));
    assert.ok($('dlg').open && $('dlg-h').textContent.includes('Worker 1'));
    assert.strictEqual($('dlg-b').querySelector('pre').textContent, job.command);

    job.state = 'ended'; job.ended = state.now; job.exit_code = 0;
    await refresh();
    assert.ok($('dlg').open && !$('dlg-b').querySelector('pre'), 'ended jobs disappear from the window');
    assert.ok(!preview(), 'a finished job is removed from the worker card');
    await click(byText($('dlg-a'), 'Close'));
    assert.strictEqual(posts.length, 0, 'opening details never controls the job');
  },
  async liveReplyInMessages({assert, $, sources, gets, settle, refresh, getState, setLiveReply, click}) {
    const state = getState(), turn = 'r001-t02-manager';
    const line = (n, s, extra) => ({agent: 'manager', n, k: 'text', s, turn, ...extra});
    state.agents.manager.status = 'writing';
    state.turns.manager = {id: turn, status: 'running', started: state.now - 1};
    setLiveReply(url => url.endsWith('&before=5') ?
      {items: [line(3, 'unrelated prior turn', {turn: 'r001-t01-manager'}), line(4, 'Draft')], more: false} :
      {items: [line(5, ' plan')], more: true});
    await refresh();
    const writing = () => $('messages').children.find(row => row.dataset.message === 'writing:' + turn);
    const row = writing();
    const body = () => row.all().find(e => e.className === 'writing-text');
    assert.ok(row && row.open);
    assert.strictEqual($('messages').children[0], row);
    assert.ok(row.children[0].textContent.includes('Writing'));
    assert.strictEqual(body().textContent, 'Draft plan');
    assert.ok(gets.includes('api/live?agent=manager&tail=500&before=5'));
    await click($('talkbtn'));
    const talkWriting = () => $('talklog').all().find(el => el.dataset.message === 'writing:' + turn);
    assert.ok(talkWriting().textContent.includes('Draft plan') && talkWriting().textContent.includes('TURN INPUT'));

    const stream = item => sources[0].l.live({data: JSON.stringify(item)});
    stream(line(6, ' evolving'));
    stream(line(6, ' evolving'));
    stream(line(7, 'tool label', {k: 'tool'}));
    await settle();
    assert.strictEqual(writing(), row, 'streaming preserves the open row');
    assert.strictEqual(body().textContent, 'Draft plan evolving');
    assert.ok(row.textContent.includes('tool label'));
    assert.ok(talkWriting().textContent.includes('Draft plan evolving') && talkWriting().textContent.includes('tool label'));
    await click($('talkclose'));

    setLiveReply(url => url.includes('tail=500') ?
      {items: [line(4, 'Draft'), line(5, ' plan'), line(6, ' evolving'), line(8, ' again')], more: false} :
      {items: [], next: 9});
    sources[0].l.bye();
    sources[1].onopen();
    await settle();
    assert.strictEqual(body().textContent, 'Draft plan evolving again');
    await click($('talkbtn'));
    assert.ok(talkWriting().textContent.includes('Draft plan evolving again'));
    $('talkwho').value = 'critic'; $('talkwho').fire('change');
    assert.ok(!talkWriting());
    $('talkwho').value = 'manager'; $('talkwho').fire('change');
    assert.ok(talkWriting());

    state.agents.manager.status = 'idle';
    state.turns.manager.status = 'ok';
    state.messages.final = {id: 'final', seq: 50, created: state.now, sender: 'manager', recipient: 'user',
      turn, status: 'delivered', body: 'Final plan for review.'};
    await refresh();
    assert.strictEqual(writing(), undefined);
    const final = $('messages').children.find(el => el.dataset.message === 'final');
    assert.ok(final && final.open && final.children[1].textContent.includes('Final plan for review.'));
    assert.ok(!$('messages').textContent.includes('tool label') && !$('messages').textContent.includes('Draft plan evolving'));
    assert.ok(!talkWriting() && !$('talklog').textContent.includes('tool label'));
    assert.strictEqual($('talklog').textContent.split('Final plan for review.').length, 2);
  },
  async commandRetriesWithSameId({assert, $, click, posts, setReply}) {
    let n = 0;
    setReply(() => { if (n++ === 0) throw new Error('network'); return {ok: true, shown: 'Done.', choices: [], data: {}}; });
    await click($('addworker')); await click(byText($('dlg-a'), 'Add worker')); await new Promise(r => setTimeout(r, 30));
    assert.ok(posts.length >= 2 && posts[0].id === posts[1].id, 'same id again');
  },
};
"""


def state_fixture():
    t = 1_800_000_000.0
    snap = snapshot(
        agent("worker-1", status="writing", assignment=Assignment("m5", "fix the parser", t - 600), actual_model="1.0 actual",
              last_output=t - 90),
        agent("worker-2", lifecycle="closed", status="closed"),
        milestone("m0", "done", title="Earlier work", score=4.0),
        milestone("m1", "current", title="Build it", text="Make the thing."),
        message("mu", "manager", USER, "Hello manager", trigger="user_chat"),
        message("mr", USER, "manager", "Hi there. <img src=x onerror=alert(1)>", needs_answer=False, status="waiting",
                holds=["question"]),
        message("mw", USER, "worker-1", "I finished the parser.", needs_answer=False, trigger="worker", status="delivered"),
        message("mn", USER, ORCH, "Worker 1 stopped.", needs_answer=False, status="delivered",
                text=TextRef(TextKey.NOTE_AGENT_FAILED, {"agent": "worker-1", "why": "x"})),
        message("mm", USER, ORCH, "Old files can go.", needs_answer=False, status="delivered",
                text=TextRef(TextKey.NOTE_MIGRATED, {"files": []})),
        gate("gq", "question", text="Shall we ship?", options=["Yes", "No"], agent="manager", opened=t - 50),
        gate("gp", "pause", words=TextRef(TextKey.GATE_SILENCE, {"attempts": 3}), opened=t - 40),
        gate("gc", "milestone_change", words=TextRef(TextKey.GATE_MILESTONE_CHANGE, {}), opened=t - 30,
             old=[milestone("m1", title="Old title")], new=[milestone("m1", title="New title")]),
        job("worker-1/j7", agent="worker-1", label="npm test", started=t - 300),
        Discussion("critic", started=t - 10),
        Terminal("critic", opened=t - 20),
        now=t, run=RunInfo(id="r1", project="demo", started=t - 900),
        catalog=[ModelInfo("alpha", "claude", model="a", label="Alpha", efforts=["low", "high"], default_effort="low"),
                 ModelInfo("beta", "claude", model="b", label="Beta", efforts=["low", "high"], default_effort="low")],
    )
    # the notice messages are from Orch; the contract has `sender` ORCH
    for mid in ("mn", "mm"):
        snap.messages[mid].sender, snap.messages[mid].recipient, snap.messages[mid].created = ORCH, USER, t - 60
    for mid in ("mu", "mr", "mw"):
        snap.messages[mid].created = t - 100
    snap.messages["mu"].sender, snap.messages["mu"].recipient = USER, "manager"
    snap.messages["mr"].sender, snap.messages["mr"].recipient = "manager", USER
    snap.messages["mr"].turn = "r001-t01-manager"
    snap.messages["mw"].sender, snap.messages["mw"].recipient = "worker-1", USER
    for aid, ag in snap.agents.items():
        if aid != "worker-1":
            ag.last_output = 0.0
    w = snap.agents["worker-1"]
    w.choices = [Choice("claude", model="a", model_id="alpha", effort="low"), Choice("claude", model="s", name="slow", model_id="slow")]
    w.choice = w.choices[0]
    w.proc = ProcRef(pid=1)
    snap.agents["manager"].choice = Choice("claude", model="a", model_id="alpha")
    snap.milestones[0].status = MilestoneStatus.DONE
    snap.checklist = Checklist(path="x", steps=[ChecklistStep("Parser", False, [(True, "read"), (False, "write")])])
    d = public_state(snap)
    d["rev"] = 1
    for g in d["gates"].values():
        if g["words"]:
            g["words"]["shown"] = "Orch words for " + g["id"][:0] + "this gate."
    for m in d["messages"].values():
        if m["text"]:
            m["text"]["shown"] = m["body"]
    return d, snap


@unittest.skipUnless(NODE, "node is not installed")
class TestSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        # one file: the scenarios are declared before the runner starts
        src = SCENARIOS_JS.replace("global.SC =", "var SC =") + "\n" + DOM_JS
        (cls.tmp / "dom.js").write_text(src, encoding="utf-8")
        d, snap = state_fixture()
        cls.state = d
        cls.fixture = {"state": d, "internal": ["gq", "gp", "gc", "worker-1/j7", "mu", "mr", "mw", "r1"]}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_scenario(self, name, mutate=None):
        fx = json.loads(json.dumps(self.fixture))
        if mutate:
            mutate(fx["state"])
        (self.tmp / "fx.json").write_text(json.dumps(fx), encoding="utf-8")
        r = subprocess.run([NODE, str(self.tmp / "dom.js"), str(PAGE), str(self.tmp / "fx.json"), name],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2500:])

    def test_state_has_the_shape_the_page_reads(self):
        d = self.state
        for k in ("agents", "messages", "gates", "milestones", "checklist", "catalog", "jobs", "terminals", "discussions", "now"):
            self.assertIn(k, d)
        self.assertIn("waiting", d["agents"]["worker-1"])
        self.assertNotIn("rendered", next(iter(d["messages"].values())))

    def test_UI_1_status_board(self):
        self.run_scenario("board")

    def test_UI_2_messages_in_time_order_shown_as_text(self):
        self.run_scenario("messages")

    def test_UI_3_nothing_internal_is_shown(self):
        self.run_scenario("nothingInternal")

    def test_UI_3_question_option(self):
        self.run_scenario("gateOption")

    def test_UI_3_question_free_text(self):
        self.run_scenario("gateText")

    def test_UI_3_approval_gates(self):
        self.run_scenario("approveGates")

    def test_UI_3_pause_and_operational_gates_close(self):
        self.run_scenario("pauseClose")

    def test_nothing_typed_is_lost_on_update(self):
        self.run_scenario("typedTextSurvivesUpdate")

    def test_lost_connection_is_shown_and_recovers(self):
        self.run_scenario("lostConnection")

    def test_a_planned_stream_end_reconnects_without_the_lost_banner(self):
        self.run_scenario("plannedStreamEnd")

    def test_lost_connection_when_the_state_cannot_be_read(self):
        self.run_scenario("lostWhenStateFails")

    def test_UI_3_header_controls(self):
        self.run_scenario("header")

    def test_UI_3_add_worker_on_a_chosen_model(self):
        self.run_scenario("addWorkerModel")


    def test_UI_3_start_agents_after_stop(self):
        def m(d):
            pass
        self.run_scenario("startAgents")

    def test_UI_3_worker_controls_close_reopen_delete(self):
        self.run_scenario("workerControls")

    def test_UI_3_close_with_a_job_asks_and_sends_again(self):
        self.run_scenario("jobChoice")

    def test_UI_3_talk_and_discuss(self):
        self.run_scenario("talk")

    def test_UI_3_enter_sends(self):
        self.run_scenario("enterSends")

    def test_UI_3_apply_and_end_a_discussion(self):
        self.run_scenario("discussion")


    def test_UI_3_terminal_notice_take_back_and_notices(self):
        self.run_scenario("terminalAndNotices")

    def test_UI_2_milestone_buttons(self):
        self.run_scenario("milestones")

    def test_UI_3_model_window(self):
        self.run_scenario("models")

    def test_UI_3_pending_switch(self):
        def m(d):
            from orch.runtime.contracts import to_dict
            d["agents"]["worker-1"]["switch"] = {"reason": "user", "forced": False, "step": "pending", "step_at": 0, "created": 0,
                                                  "new_session": False, "handover_by": "", "error": "",
                                                  "target": {"label": "Beta", "backend": "claude", "model": "b", "provider": "",
                                                             "effort": "", "model_id": "beta", "name": ""}}
        self.run_scenario("switchPending", m)

    def test_UI_1_team_folds(self):
        self.run_scenario("teamFold")

    def test_UI_4_golden_board_edit(self):
        self.run_scenario("golden")

    def test_UI_4_audit_notes_and_run(self):
        self.run_scenario("audit")

    def test_UI_5_UI_2_message_details(self):
        self.run_scenario("messageDetail")

    def test_UI_2_restores_filters_before_fetching(self):
        self.run_scenario("restoredFilters")

    def test_UI_2_timeline_controls(self):
        self.run_scenario("timelinePage")

    def test_UI_3_answered_gates_do_not_need_you(self):
        self.run_scenario("answeredWithoutGates")

    def test_UI_2_checklist_and_talk_are_capped(self):
        self.run_scenario("caps")

    def test_UI_6_old_runs_are_read_only(self):
        self.run_scenario("oldRuns")

    def test_tools_stream_inside_the_writing_row(self):
        self.run_scenario("toolsInsideWriting")

    def test_milestone_review_replaces_an_earlier_copy_of_the_same_score_table(self):
        self.run_scenario("milestoneReviewTables")

    def test_job_details_show_the_command_and_follow_the_running_job(self):
        self.run_scenario("jobDetails")

    def test_writing_reply_streams_in_the_message_list_and_becomes_the_final_reply(self):
        self.run_scenario("liveReplyInMessages")

    def test_a_command_is_sent_again_with_the_same_id(self):
        self.run_scenario("commandRetriesWithSameId")


if __name__ == "__main__":
    unittest.main()
