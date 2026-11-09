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
from typing import Any, Dict, List, Optional, Tuple

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
.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0;margin:12px 0 4px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;border-bottom:1px solid var(--line)}
.bar button{font:inherit;font-size:12px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer}
.bar button.on{background:var(--acc);border-color:var(--acc);color:#fff}
.bar input{font:inherit;font-size:12.5px;padding:3px 10px;border-radius:999px;border:1px solid var(--line);background:var(--panel);color:var(--ink);min-width:180px;flex:1;max-width:320px}
.ms>summary{font-size:15px}.ms>.body>details{margin-left:0}
.prev{color:var(--mute);font-size:12.5px;flex-basis:100%;margin-left:18px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sub{margin:6px 0;border-radius:8px}.sub>summary{padding:5px 10px;font-size:12px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
.sub>.body{padding:0 10px 4px}.sub pre{margin:0 0 8px}
.hidden{display:none!important}mark{background:#ffe58a;color:inherit;border-radius:2px}
@media (max-width:600px){.hide-sm{display:none}}
"""

JS = """
(function(){
 var key='orch-open:'+location.pathname, ykey=key+':y', pkey=key+':pre';
 function pres(){return [].slice.call(document.querySelectorAll('.turn pre'))}
 function pid(p,i){var d=p.closest('details[id]');return (d?d.id:'')+'#'+i}
 function load(){try{var v=JSON.parse(sessionStorage.getItem(key)||'{}');return Array.isArray(v)?{}:v}catch(e){return{}}}
 function save(){try{
   var ids={};[].slice.call(document.querySelectorAll('details[id]')).forEach(function(d){if(d.open!==d.hasAttribute('data-def'))ids[d.id]=d.open});
   sessionStorage.setItem(key,JSON.stringify(ids));sessionStorage.setItem(ykey,String(window.scrollY));
   var m={};pres().forEach(function(p,i){if(p.scrollTop)m[pid(p,i)]=p.scrollTop});
   sessionStorage.setItem(pkey,JSON.stringify(m))}catch(e){}}
 function restore(){try{
   var o=load();Object.keys(o).forEach(function(id){var d=document.getElementById(id);if(d)d.open=o[id]});
   var m=JSON.parse(sessionStorage.getItem(pkey)||'{}');
   pres().forEach(function(p,i){var v=m[pid(p,i)];if(v)p.scrollTop=v});
   var y=+sessionStorage.getItem(ykey);if(y)window.scrollTo(0,y)}catch(e){}}
 [].slice.call(document.querySelectorAll('details[open][id]')).forEach(function(d){d.setAttribute('data-def','')});
 restore();
 var bar=document.querySelector('.bar');
 if(bar){
   var who='all',q=document.getElementById('q');
   function apply(){var term=(q.value||'').trim().toLowerCase();
     [].slice.call(document.querySelectorAll('details.turn[data-agent]')).forEach(function(t){
       var ok=(who==='all'||t.getAttribute('data-agent')===who)&&(!term||t.textContent.toLowerCase().indexOf(term)>=0);
       t.classList.toggle('hidden',!ok);if(term&&ok){t.open=true;var o=t.querySelector('.sub.out');if(o)o.open=true}});
     [].slice.call(document.querySelectorAll('details.rnd,details.ms')).forEach(function(r){
       var n=r.querySelectorAll('details.turn[data-agent]:not(.hidden)').length;r.classList.toggle('hidden',!n);
       if(term&&n)r.open=true})}
   bar.addEventListener('click',function(ev){var b=ev.target.closest('button');if(!b)return;
     if(b.hasAttribute('data-who')){who=b.getAttribute('data-who');
       [].slice.call(bar.querySelectorAll('[data-who]')).forEach(function(x){x.classList.toggle('on',x===b)});apply()}
     else if(b.hasAttribute('data-all')){var open=b.getAttribute('data-all')==='1';
       [].slice.call(document.querySelectorAll(b.getAttribute('data-sel'))).forEach(function(d){if(!d.closest('.hidden'))d.open=open})}
     save()});
   var tm;q.addEventListener('input',function(){clearTimeout(tm);tm=setTimeout(apply,250)});
 }
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


def turn_costs(items: List[Dict[str, Any]], key: str = "cost") -> Optional[float]:
    """Each item's own cost, set in place under ``key``, and the total. Claude reports a resumed session's
    running total, so a turn costs the increase over that session's previous report."""
    ordered = sorted((x for x in items if x.get("status") != "running"),
                     key=lambda x: x.get("ended_at") or x.get("ts") or x.get("started_at") or "")
    last: Dict[str, float] = {}
    total, seen = 0.0, False
    for x in ordered:
        c = x.get(key)
        if not isinstance(c, (int, float)):
            continue
        sid = x.get("session_id") or ""
        prev = last.get(sid, 0.0) if sid else 0.0
        x[key] = round(c - prev if c >= prev else c, 6)
        if sid:
            last[sid] = c
        total += x[key]
        seen = True
    return round(total, 4) if seen else None


def _sub(tid: str, part: str, label: str, text: Any, open_: bool = False, cls: str = "") -> str:
    return (f"<details class='sub {cls}' id='{e(tid)}-{part}'{' open' if open_ else ''}><summary>{e(label)}</summary>"
            f"<div class='body'><pre>{e(text)}</pre></div></details>")


def _preview(text: str) -> str:
    """The opening words of a reply, joined across short or heading lines, for a folded row."""
    out = ""
    for ln in (text or "").splitlines():
        ln = re.sub(r"\s+", " ", re.sub(r"<[^>]+>|[#*`>|_]+", " ", ln)).strip()
        if len(ln) > 3:
            out = f"{out} {ln}".strip()
            if len(out) > 60 and not ln.endswith(":"):
                break
    return out[:200]


def _turn_html(t: Dict[str, Any]) -> str:
    tid = str(t.get("turn_id"))
    cost = t.get("_cost")
    out = t.get("output") or ""
    verdict = ""
    if t.get("role") == "verifier":
        m = re.findall(r"<verdict>\s*(PASS|FAIL)|VERDICT:\s*\**\s*(PASS|FAIL)", out, re.I)
        if m:
            v = [a or b for a, b in m][-1].upper()
            verdict = pill(v)
    head = (f'<summary>{pill(t.get("status"))}<b>{e(t.get("agent"))}</b>'
            f'<span class="tag">{e(t.get("purpose"))}</span>{verdict}'
            + (f'<span class="tag">❓ {e(t.get("asked"))}</span>' if t.get("asked") else "")
            + f'<span class="tag mono">{e(tid)}</span>'
            f'<span class="tag">{e((t.get("started_at") or "")[5:16].replace("T", " "))}</span>'
            f'<span class="tag">{_dur(t.get("duration_s"))}</span>'
            + (f'<span class="tag">${cost:.2f}</span>' if isinstance(cost, (int, float)) else "")
            + (f'<span class="prev">{e(_preview(out))}</span>' if out else "")
            + "</summary>")
    meta = t.get("meta") or {}
    kv = [("backend", t.get("backend")), ("model", (t.get("options") or {}).get("model")),
          ("session", t.get("session_id")),
          ("session turn", f'{t.get("session_turn")} ({"resumed" if t.get("resumed_session") else "new session"})'),
          ("cwd", t.get("cwd")), ("started", t.get("started_at")), ("ended", t.get("ended_at")),
          ("exit code", t.get("exit_code")), ("attempts", len(t.get("attempts") or []) or None)]
    for k in ("num_turns", "subtype"):
        if k in meta:
            kv.append((k, meta[k]))
    kvh = "".join(f"<div class='mute'>{e(k)}</div><div class='mono'>{e(v)}</div>" for k, v in kv if v not in (None, ""))
    parts = []
    if t.get("status") == "running":
        parts.append("<div class='label'>output</div><pre class='mute'>running…</pre>")
    else:
        parts.append(_sub(tid, "out", "output", out or "(empty)", open_=True, cls="out"))
    parts.append(_sub(tid, "in", f"input · {len(t.get('input') or ''):,} chars", t.get("input")))
    if t.get("system_prompt"):
        parts.append(_sub(tid, "sys", "system prompt", t["system_prompt"]))
    if t.get("stderr"):
        parts.append(_sub(tid, "err", "stderr", t["stderr"]))
    if t.get("command"):
        parts.append(_sub(tid, "cmd", "command", " ".join(map(str, t["command"]))))
    parts.append(f"<details class='sub' id='{e(tid)}-meta'><summary>details</summary><div class='body'><div class='kv'>{kvh}</div></div></details>")
    return (f"<details class='turn' id='{e(tid)}' data-agent='{e(t.get('agent'))}'>{head}"
            f"<div class='body'>{''.join(parts)}</div></details>")


def _dur(s: Any) -> str:
    if not isinstance(s, (int, float)):
        return "…"
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else (f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s")


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
    return ("<p class='mute' style='margin:0 0 8px'>Kept by the reviewer only; "
            "the worker gets the prose feedback without it. Hover a cell for that review's wording.</p>"
            "<div style='overflow-x:auto'><table><tr><th>item</th>" + head + "</tr>" + "".join(body) + "</table></div>")


def render_run_report(run_dir: Path) -> str:
    st = read_json(run_dir / "state.json", {}) or {}
    status = st.get("status", "?")
    tc = st.get("turn_counts") or {}
    rounds_dir = run_dir / "rounds"
    rdirs = sorted([p for p in rounds_dir.iterdir() if p.is_dir()]) if rounds_dir.is_dir() else []
    rounds = [(rdir, read_json(rdir / "round.json", {}) or {}, _turns_for_round(rdir)) for rdir in rdirs]
    every = [t for _, _, ts in rounds for t in ts]
    for t in every:
        t["_cost"] = (t.get("meta") or {}).get("total_cost_usd")  # running session totals; turn_costs takes the steps
    total = turn_costs(every, "_cost")
    agents = sorted({str(t.get("agent")) for t in every})
    cards = [("status", pill(status)), ("mode", e(st.get("mode"))),
             ("round", f"{e(st.get('round'))} / {e(st.get('max_rounds'))}"),
             ("turns", f"{e(tc.get('total', 0))} <span class='mute' style='font-size:12px'>({e(tc.get('ok', 0))} ok · {e(tc.get('error', 0) + tc.get('timeout', 0))} failed)</span>"),
             ("cost (reported)", f"${total:,.2f}" if total else "—"),
             ("started", e((st.get("started_at") or "")[:16].replace("T", " "))),
             ("updated", e((st.get("updated_at") or "")[:16].replace("T", " ")))]
    body = [f"<p class='mute'><a href='../../index.html'>← all runs</a></p>",
            f"<h1>{e(st.get('monitor'))} <span class='mute mono' style='font-size:14px'>{e(st.get('run_id'))}</span></h1>",
            f"<div class='mute mono'>{e(st.get('working_dir'))}</div>",
            "<div class='grid'>" + "".join(f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in cards) + "</div>",
            f"<details id='goal'><summary><b>Goal</b><span class='prev'>{e(_preview(st.get('goal') or ''))}</span></summary>"
            f"<div class='body'><div class='goal'>{e(st.get('goal') or '(none)')}</div></div></details>"]
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
        body.append("<details id='sessions'><summary><b>Sessions</b></summary><div class='body'><table><tr><th>agent</th><th>role</th>"
                    f"<th class='hide-sm'>backend</th><th>session id</th><th>turns</th><th>last</th></tr>{rows}</table></div></details>")
    ck = _checklist_html(rdirs)
    if ck:
        body.append(f"<details id='checklist'><summary><b>Reviewer checklist</b><span class='tag'>item × review</span></summary>"
                    f"<div class='body'>{ck}</div></details>")

    # milestones -> rounds -> turns, newest first; only the newest milestone and round start open
    titles = [m.get("title") or m.get("name") or "" for m in ((st.get("managed") or {}).get("milestones") or [])]
    groups: List[Tuple[Any, List[Any]]] = []
    for r in rounds:
        ms = next((t.get("milestone") for t in r[2] if t.get("milestone") is not None), None)
        if groups and groups[-1][0] == ms:
            groups[-1][1].append(r)
        else:
            groups.append((ms, [r]))
    who = "".join(f"<button data-who='{e(a)}'>{e(a)}</button>" for a in agents)
    body.append("<h2>Rounds</h2><div class='bar'><button class='on' data-who='all'>everyone</button>" + who +
                "<input id='q' type='search' placeholder='Search inputs and outputs…'>"
                "<button data-all='1' data-sel='details.ms,details.rnd'>expand rounds</button>"
                "<button data-all='0' data-sel='details.ms,details.rnd,details.turn'>collapse all</button></div>")
    if not rounds:
        body.append("<p class='mute'>No rounds yet.</p>")
    for gi, (ms, rs_) in enumerate(reversed(groups)):
        if ms is None:
            name = "Planning" if rs_[0][0].name == "r000" else "Rounds"
        else:
            i = int(ms)
            name = f"Milestone {i + 1}" + (f" · {titles[i]}" if i < len(titles) and titles[i] else "")
        nturns = sum(len(x[2]) for x in rs_)
        rn = [str(x[1].get("round", int(x[0].name[1:]) if x[0].name[1:].isdigit() else "?")) for x in rs_]
        span = f"rounds {rn[0]}–{rn[-1]}" if len(rn) > 1 else f"round {rn[0]}"
        inner = []
        for ri, (rdir, rs, turns) in enumerate(reversed(rs_)):
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
            rcost = sum(t["_cost"] for t in turns if isinstance(t.get("_cost"), (int, float)))
            verd = " ".join(pill(v) for v in (rs.get("verdicts") or {}).values())
            first = gi == 0 and ri == 0
            inner.append(
                f"<details class='rnd' id='{e(rdir.name)}'{' open' if first else ''}><summary><b>{'Plan' if rdir.name == 'r000' else 'Round ' + e(rs.get('round', rdir.name))}</b>"
                f"{pill(rs.get('status', '?'))}{verd}<span class='tag'>{len(turns)} turns</span>"
                + (f"<span class='tag'>${rcost:.2f}</span>" if rcost else "")
                + f"<span class='tag'>{e(rs.get('decision') or '')}</span></summary>"
                f"<div class='body'>" + "".join(f"<div class='decision'>{x}</div>" for x in extra)
                + "".join(_turn_html(t) for t in turns) + "</div></details>")
        body.append(f"<details class='ms' id='ms-{e(ms)}-{e(rs_[0][0].name)}'{' open' if gi == 0 else ''}><summary><b>{e(name)}</b>"
                    f"<span class='tag'>{span} · {nturns} turns</span></summary><div class='body'>{''.join(inner)}</div></details>")
    body.append("<p class='mute' style='margin-top:24px'>Costs are what each agent's CLI reports for the turn "
                "(the increase in its session's total). Files: <code>state.json</code>, <code>events.jsonl</code>, "
                "<code>progress.md</code>, <code>rounds/</code>.</p>")
    return page(f"{st.get('monitor')} · {st.get('run_id')}", "".join(body), live=status in ("running", "starting"))


def write_run_report(run_dir: Path) -> Path:
    out = run_dir / "report.html"
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(render_run_report(run_dir), encoding="utf-8")
    tmp.replace(out)
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


def render_index(output_root: Path) -> str:
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
    return page("Orch runs", body, live)


def write_index(output_root: Path) -> Path:
    out = output_root / "index.html"
    output_root.mkdir(parents=True, exist_ok=True)
    out.write_text(render_index(output_root), encoding="utf-8")
    return out
