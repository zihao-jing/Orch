"""Static, self-contained HTML viewers built from the files on disk.

``report.html`` in each run directory shows every round and turn (input, output,
status, session, timing); ``index.html`` in the output root lists all runs.
Both are regenerated after every turn, and auto-refresh while a run is active.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .recorder import read_json

# a Markdown checkbox line: "- [x] item", "* [ ] item", "1. [X] item"
CHECKBOX = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*\[( |x|X|✓|✔)\]", re.M)


def checklist_items(text: str) -> List[Tuple[bool, str]]:
    """[(done, item text)] for every checkbox line in a reply."""
    out = []
    for ln in (text or "").split("\n"):
        m = CHECKBOX.match(ln)
        if m:
            out.append((m.group(1) != " ", ln[m.end():].strip()))
    return out


def _item_key(s: str) -> str:
    s = re.sub(r"[*_`]", "", s.lower())
    s = re.split(r"\s+[—–]\s+", s, maxsplit=1)[0]  # drop a trailing reason ("item — why it fails")
    return re.sub(r"\W+", " ", s).strip()[:90]

CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--mute:#6b6b66;--line:#e3e2dd;--code:#f2f1ed;
--ok:#1f7a4d;--ok-bg:#e3f3ea;--err:#b3261e;--err-bg:#fbe5e3;--warn:#8a5a00;--warn-bg:#fff1d6;--run:#1b5fb4;--run-bg:#e2edfb;--acc:#3b4cca}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--panel:#1d1d1b;--ink:#ecebe6;--mute:#9b9a94;--line:#33322f;--code:#262522;
--ok:#6fd19c;--ok-bg:#16301f;--err:#ff8a80;--err-bg:#3a1916;--warn:#f3c56b;--warn-bg:#352a12;--run:#8ab8ff;--run-bg:#14263f;--acc:#9aa5ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 16px 80px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 10px}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
.mute{color:var(--mute)}.mono,pre,code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin:16px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card .k{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}.card .v{font-size:16px;font-weight:600;margin-top:2px;word-break:break-word}
.goal{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px;white-space:pre-wrap}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:11px;text-transform:uppercase;color:var(--mute);font-weight:600}
tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.ok,.completed,.PASS{color:var(--ok);background:var(--ok-bg)}.error,.failed,.FAIL,.ERROR{color:var(--err);background:var(--err-bg)}
.timeout,.stopped,.max_rounds_reached,.UNKNOWN,.starting{color:var(--warn);background:var(--warn-bg)}.running,.done{color:var(--run);background:var(--run-bg)}
details{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin:8px 0}
details>summary{cursor:pointer;padding:10px 12px;list-style:none;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
details>summary::-webkit-details-marker{display:none}details>summary:before{content:"▸";color:var(--mute)}details[open]>summary:before{content:"▾"}
.body{padding:0 12px 12px}.turn{margin:8px 0 0}.turn>summary{padding:8px 10px}
pre{background:var(--code);border-radius:8px;padding:10px;white-space:pre-wrap;word-break:break-word;max-height:520px;overflow:auto;margin:6px 0 12px}
.label{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);margin-top:8px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;font-size:12.5px;margin:6px 0}
.tag{font-size:12px;color:var(--mute)}.decision{margin:6px 0 4px;font-weight:600}
@media (max-width:600px){.hide-sm{display:none}}
"""

JS = """
(function(){
 var key='orch-open:'+location.pathname, ykey=key+':y', pkey=key+':pre';
 function pres(){return [].slice.call(document.querySelectorAll('.turn pre'))}
 function pid(p,i){var d=p.closest('details[id]');return (d?d.id:'')+'#'+i}
 function load(){try{return JSON.parse(sessionStorage.getItem(key)||'[]')}catch(e){return[]}}
 function save(){try{
   var ids=[].slice.call(document.querySelectorAll('details[open][id]')).map(function(d){return d.id});
   sessionStorage.setItem(key,JSON.stringify(ids));sessionStorage.setItem(ykey,String(window.scrollY));
   var m={};pres().forEach(function(p,i){if(p.scrollTop)m[pid(p,i)]=p.scrollTop});
   sessionStorage.setItem(pkey,JSON.stringify(m))}catch(e){}}
 function restore(){try{
   load().forEach(function(id){var d=document.getElementById(id);if(d)d.open=true});
   var m=JSON.parse(sessionStorage.getItem(pkey)||'{}');
   pres().forEach(function(p,i){var v=m[pid(p,i)];if(v)p.scrollTop=v});
   var y=+sessionStorage.getItem(ykey);if(y)window.scrollTo(0,y)}catch(e){}}
 restore();
 var lastAct=0;function act(){lastAct=Date.now();save()}
 document.addEventListener('toggle',save,true);
 window.addEventListener('scroll',act,{passive:true});
 document.addEventListener('scroll',act,{capture:true,passive:true}); // scrolling inside a <pre>
 ['wheel','keydown','mousedown','touchstart'].forEach(function(ev){document.addEventListener(ev,function(){lastAct=Date.now()},{passive:true})});
 if(document.body.getAttribute('data-live')!=='1')return;
 var base=null;
 function busy(){var sel=window.getSelection&&String(window.getSelection());return Date.now()-lastAct<15000||!!sel}
 function tick(){
   fetch(location.href,{cache:'no-store'}).then(function(r){return r.text()}).then(function(t){
     if(base===null){base=t;return}
     if(t!==base&&!busy()){save();location.reload()}   // only when the report changed and you are idle
   }).catch(function(){})}
 fetch(location.href,{cache:'no-store'}).then(function(r){return r.text()}).then(function(t){base=t}).catch(function(){});
 setInterval(tick,5000);
})();
"""


def e(x: Any) -> str:
    return html.escape("" if x is None else str(x))


def pill(s: Any) -> str:
    return f'<span class="pill {e(s)}">{e(s)}</span>'


def page(title: str, body: str, live: bool) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{e(title)}</title><style>{CSS}</style></head>"
            f'<body data-live="{1 if live else 0}"><main>{body}</main><script>{JS}</script></body></html>')


def _turns_for_round(rdir: Path) -> List[Dict[str, Any]]:
    out = []
    for p in sorted(rdir.glob("t*.json")):
        d = read_json(p)
        if isinstance(d, dict) and "turn_id" in d:
            out.append(d)
    return out


def _turn_html(t: Dict[str, Any]) -> str:
    meta = t.get("meta") or {}
    cost = meta.get("turn_cost_usd", meta.get("total_cost_usd"))
    head = (f'<summary>{pill(t.get("status"))}<b>{e(t.get("agent"))}</b>'
            f'<span class="tag">{e(t.get("role"))} · {e(t.get("purpose"))}</span>'
            f'<span class="tag mono">{e(t.get("turn_id"))}</span>'
            f'<span class="tag">{e(t.get("duration_s") if t.get("duration_s") is not None else "…")}s</span>'
            + (f'<span class="tag">${cost:.4f}</span>' if isinstance(cost, (int, float)) else "")
            + "</summary>")
    kv = [("backend", t.get("backend")), ("session", t.get("session_id")),
          ("session turn", f'{t.get("session_turn")} ({"resumed" if t.get("resumed_session") else "new session"})'),
          ("cwd", t.get("cwd")), ("started", t.get("started_at")), ("ended", t.get("ended_at")),
          ("exit code", t.get("exit_code")), ("attempts", len(t.get("attempts") or []) or None)]
    for k in ("num_turns", "subtype"):
        if k in meta:
            kv.append((k, meta[k]))
    kvh = "".join(f"<div class='mute'>{e(k)}</div><div class='mono'>{e(v)}</div>" for k, v in kv if v not in (None, ""))
    parts = [f"<div class='kv'>{kvh}</div>"]
    if t.get("system_prompt"):
        parts.append(f"<details class='turn' id='{e(t.get('turn_id'))}-sys'><summary class='tag'>system prompt</summary><div class='body'><pre>{e(t['system_prompt'])}</pre></div></details>")
    parts.append(f"<div class='label'>input</div><pre>{e(t.get('input'))}</pre>")
    if t.get("status") == "running":
        parts.append("<div class='label'>output</div><pre class='mute'>running…</pre>")
    else:
        parts.append(f"<div class='label'>output</div><pre>{e(t.get('output') or '(empty)')}</pre>")
    if t.get("stderr"):
        parts.append(f"<div class='label'>stderr</div><pre>{e(t['stderr'])}</pre>")
    if t.get("command"):
        parts.append(f"<div class='label'>command</div><pre>{e(' '.join(map(str, t['command'])))}</pre>")
    return f"<details class='turn' id='{e(t.get('turn_id'))}'>{head}<div class='body'>{''.join(parts)}</div></details>"


def _checklist_html(rdirs: List[Path]) -> str:
    """Item × review matrix built from every reviewer turn that contains checkboxes (oldest first)."""
    cols, rows, order = [], {}, []  # cols: (turn_id, agent, overruled); rows: key -> {col: (done, text)}
    for rdir in rdirs:
        rs = read_json(rdir / "round.json", {}) or {}
        for t in _turns_for_round(rdir):
            items = checklist_items(t.get("output") or "") if t.get("role") == "verifier" else []
            if not items:
                continue
            ci = len(cols)
            cols.append((t.get("turn_id"), t.get("agent"), t.get("agent") in (rs.get("checklist_overruled") or [])))
            for done, text in items:
                k = _item_key(text) or text
                if k not in rows:
                    rows[k] = {}
                    order.append(k)
                rows[k][ci] = (done, text)
    if not cols:
        return ""
    head = "".join(
        f"<th title='{e(tid)}'><a href='#{e(tid)}'>{e(tid.split('-')[0])}</a>"
        + (f"<br><span class='mute' style='font-weight:400'>{e(agent)}</span>" if len({c[1] for c in cols}) > 1 else "")
        + ("<br><span class='pill FAIL' title='said PASS with open items'>overruled</span>" if over else "")
        + "</th>" for tid, agent, over in cols)
    body = []
    for k in order:
        cells = rows[k]
        last = cells[max(cells)]
        label = last[1]
        tds = []
        for ci in range(len(cols)):
            if ci not in cells:
                tds.append("<td class='mute' style='text-align:center'>·</td>")
            else:
                done, text = cells[ci]
                tds.append(f"<td style='text-align:center' title='{e(text)}'>"
                           + ("<span class='pill PASS'>✓</span>" if done else "<span class='pill FAIL'>✗</span>") + "</td>")
        body.append(f"<tr><td>{e(label)}</td>{''.join(tds)}</tr>")
    totals = []
    for ci in range(len(cols)):
        vals = [rows[k][ci][0] for k in order if ci in rows[k]]
        totals.append(f"<td style='text-align:center' class='mono'>{sum(vals)}/{len(vals)}</td>")
    body.append(f"<tr><td class='mute'>done</td>{''.join(totals)}</tr>")
    return ("<h2>Reviewer checklist</h2><p class='mute' style='margin:-4px 0 8px'>Kept by the reviewer only; "
            "the worker gets the prose feedback without it. Hover a cell for that review's wording.</p>"
            "<div style='overflow-x:auto'><table><tr><th>item</th>" + head + "</tr>" + "".join(body) + "</table></div>")


def write_run_report(run_dir: Path) -> Path:
    st = read_json(run_dir / "state.json", {}) or {}
    status = st.get("status", "?")
    tc = st.get("turn_counts") or {}
    cards = [("status", pill(status)), ("mode", e(st.get("mode"))),
             ("round", f"{e(st.get('round'))} / {e(st.get('max_rounds'))}"),
             ("turns", f"{e(tc.get('total', 0))} <span class='mute' style='font-size:12px'>({e(tc.get('ok', 0))} ok · {e(tc.get('error', 0) + tc.get('timeout', 0))} failed)</span>"),
             ("cost", f"${st.get('cost_usd', 0):.4f}" if st.get("cost_usd") else "—"),
             ("started", e((st.get("started_at") or "")[:19].replace("T", " "))),
             ("updated", e((st.get("updated_at") or "")[:19].replace("T", " ")))]
    body = [f"<p class='mute'><a href='../../index.html'>← all runs</a></p>",
            f"<h1>{e(st.get('monitor'))} <span class='mute mono' style='font-size:14px'>{e(st.get('run_id'))}</span></h1>",
            f"<div class='mute mono'>{e(st.get('working_dir'))}</div>",
            "<div class='grid'>" + "".join(f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in cards) + "</div>",
            f"<h2>Goal</h2><div class='goal'>{e(st.get('goal') or '(none)')}</div>"]
    if st.get("final_report"):
        body.append(f"<h2>Final report</h2><div class='goal'>{e(st['final_report'])}</div>")
    if st.get("error"):
        body.append(f"<h2>Error</h2><pre>{e(st['error'])}</pre>")
    sessions = st.get("sessions") or {}
    if sessions:
        rows = "".join(
            f"<tr><td><b>{e(a)}</b></td><td>{e(s.get('role'))}</td><td class='hide-sm'>{e(s.get('backend'))}</td>"
            f"<td class='mono'>{e(s.get('session_id'))}</td><td>{e(s.get('turns'))}</td><td>{pill(s.get('last_status'))}</td></tr>"
            for a, s in sessions.items())
        body.append("<h2>Sessions</h2><table><tr><th>agent</th><th>role</th><th class='hide-sm'>backend</th>"
                    f"<th>session id</th><th>turns</th><th>last</th></tr>{rows}</table>")
    rounds_dir = run_dir / "rounds"
    rdirs = sorted([p for p in rounds_dir.iterdir() if p.is_dir()], reverse=True) if rounds_dir.is_dir() else []
    body.append(_checklist_html(list(reversed(rdirs))))
    body.append("<h2>Rounds</h2>")
    if not rdirs:
        body.append("<p class='mute'>No rounds yet.</p>")
    for i, rdir in enumerate(rdirs):
        rs = read_json(rdir / "round.json", {}) or {}
        turns = _turns_for_round(rdir)
        extra = []
        if rs.get("verdicts"):
            over = rs.get("checklist_overruled") or []
            extra.append("Verdicts: " + " ".join(
                f"{e(k)} {pill(v)}" + (" <span class='tag'>(said PASS with open checklist items)</span>" if k in over else "")
                for k, v in rs["verdicts"].items()))
        d = rs.get("directive") or {}
        if d.get("summary"):
            extra.append(f"Controller: {e(d['summary'])}")
        if rs.get("error"):
            extra.append(f"<span class='pill error'>error</span> {e(rs['error'])}")
        rstatus = rs.get("status", "?")
        open_attr = " open" if i == 0 else ""
        body.append(
            f"<details id='{e(rdir.name)}'{open_attr}><summary><b>Round {e(rs.get('round', rdir.name))}</b>{pill(rstatus)}"
            f"<span class='tag'>{len(turns)} turns</span><span class='tag'>{e(rs.get('decision') or '')}</span></summary>"
            f"<div class='body'>" + "".join(f"<div class='decision'>{x}</div>" for x in extra)
            + "".join(_turn_html(t) for t in turns) + "</div></details>")
    body.append("<p class='mute' style='margin-top:24px'>Files: <code>state.json</code>, <code>events.jsonl</code>, "
                "<code>progress.md</code>, <code>rounds/</code>. Steer with <code>orch inject</code>, stop with <code>orch stop</code>.</p>")
    out = run_dir / "report.html"
    out.write_text(page(f"{st.get('monitor')} · {st.get('run_id')}", "".join(body),
                        live=status in ("running", "starting")), encoding="utf-8")
    return out


def collect_runs(output_root: Path) -> List[Dict[str, Any]]:
    runs = []
    if not output_root.is_dir():
        return runs
    for mdir in sorted(p for p in output_root.iterdir() if p.is_dir()):
        for rdir in sorted((p for p in mdir.iterdir() if (p / "state.json").is_file()), reverse=True):
            st = read_json(rdir / "state.json", {}) or {}
            st["_dir"] = rdir
            runs.append(st)
    return runs


def write_index(output_root: Path) -> Path:
    runs = collect_runs(output_root)
    live = any(r.get("status") in ("running", "starting") for r in runs)
    rows = []
    for r in runs:
        rel = f"{r['_dir'].parent.name}/{r['_dir'].name}/report.html"
        tc = r.get("turn_counts") or {}
        rows.append(f"<tr><td><b>{e(r.get('monitor'))}</b></td><td><a class='mono' href='{e(rel)}'>{e(r.get('run_id'))}</a></td>"
                    f"<td>{pill(r.get('status'))}</td><td>{e(r.get('round'))}/{e(r.get('max_rounds'))}</td>"
                    f"<td>{e(tc.get('total', 0))}</td><td class='hide-sm'>{e(r.get('mode'))}</td>"
                    f"<td class='hide-sm mono'>{e((r.get('updated_at') or '')[:19].replace('T', ' '))}</td></tr>")
    body = ("<h1>Orch runs</h1>"
            f"<p class='mute mono'>{e(output_root)}</p>"
            + ("<table><tr><th>monitor</th><th>run</th><th>status</th><th>round</th><th>turns</th>"
               "<th class='hide-sm'>mode</th><th class='hide-sm'>updated</th></tr>" + "".join(rows) + "</table>"
               if rows else "<p class='mute'>No runs yet.</p>"))
    out = output_root / "index.html"
    output_root.mkdir(parents=True, exist_ok=True)
    out.write_text(page("Orch runs", body, live), encoding="utf-8")
    return out
