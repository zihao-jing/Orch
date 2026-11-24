"""B9b: the board's timeline — one-line summaries in the state, full text on unfold, folded steps on opening, search,
a bounded payload on a large run, the log page gone and a cut static report. Synthetic run folders only."""

import json
import sys
import tempfile
import unittest
from orch.timeline import OTHER_KINDS
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch import report as R  # noqa: E402
from orch.board import Board, serve  # noqa: E402
from orch.timeline import ITEMS_MAX, assign, block_of, line_of  # noqa: E402

AGENTS = [{"name": "boss", "role": "manager", "display_name": "Alex"},
          {"name": "critic", "role": "verifier", "display_name": "Jordan"},
          {"name": "writer", "role": "worker", "display_name": "Sam"}]


def ts(i: int) -> str:
    return f"2026-11-19T{10 + i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}+08:00"


def make_run(tmp: Path, milestones: int = 2, rounds: int = 3, big: int = 0, chats: int = 2) -> Path:
    """A run folder: per milestone a kickoff, a brief with a 2-step plan, work/verify rounds (each verify finishing a
    step), a report; then chats, a question for me, an open gate and a retry event."""
    rd = tmp / "out" / "m" / "20261119-100000"
    (rd / "rounds").mkdir(parents=True)
    (rd / "config.json").write_text(json.dumps({"agents": AGENTS}))
    n, r, turns = 0, 0, []

    def turn(agent, role, purpose, out, ms, plan=None, inp="prompt"):
        nonlocal n
        n += 1
        tid = f"r{r:03d}-t{n:02d}-{agent}"
        d = rd / "rounds" / f"r{r:03d}"
        d.mkdir(exist_ok=True)
        t = {"turn_id": tid, "round": r, "seq": n, "agent": agent, "role": role, "purpose": purpose, "status": "ok",
             "started_at": ts(len(turns) * 10), "ended_at": ts(len(turns) * 10 + 5), "milestone": ms, "output": out,
             "input": inp + ("x" * big)}
        if plan:
            t["checklist_file"] = {"plan": plan, "checklist": "- [x] a\n- [ ] b"}
        (d / f"t{n:02d}-{agent}.json").write_text(json.dumps(t))
        (d / f"t{n:02d}-{agent}.output.md").write_text(out)
        turns.append(t)
        return t

    turn("boss", "manager", "plan", "1. Part one\n2. Part two", None)
    for ms in range(milestones):
        r += 1
        n = 0
        turn("boss", "manager", "kickoff", f"<message to=\"critic\">Start milestone {ms + 1}.</message>", ms)
        turn("critic", "verifier", "brief", "Here is the brief.", ms, plan="- [ ] Write the intro\n- [ ] Check the tables")
        for k in range(rounds):
            r += 1
            n = 0
            filler = ("lorem ipsum " * (big // 12)) if big else ""
            turn("writer", "worker", "work", f"Did part {k} of milestone {ms + 1}. NEEDLE-{ms}-{k}\n{filler}", ms)
            done = min(k + 1, 2)
            plan = "\n".join(f"- [{'x' if i < done else ' '}] {s}" for i, s in enumerate(["Write the intro", "Check the tables"]))
            turn("critic", "verifier", "verify", f"Round review {k}.\n<message to=\"boss\">FYI step {done} done</message>",
                 ms, plan=plan)
        turn("critic", "verifier", "report", "Everything holds.", ms)
    chat = []
    for i in range(chats):
        chat.append({"id": f"chat-{i:03d}-1-q", "role": "human", "text": f"How is it going {i}? " + "y" * big,
                     "ts": ts(len(turns) * 10 + i)})
        chat.append({"id": f"chat-{i:03d}-1", "role": "manager", "status": "ok", "text": f"Fine {i}. " + "z" * big,
                     "ts": ts(len(turns) * 10 + i)})
    (rd / "chat.jsonl").write_text("".join(json.dumps(c) + "\n" for c in chat))
    last = ts(len(turns) * 10 + 50)
    (rd / "events.jsonl").write_text(
        json.dumps({"ts": last, "event": "retry", "agent": "writer", "model": "haiku", "reason": "overloaded",
                    "retry": 1, "of": 3, "wait_s": 60}) + "\n"
        + json.dumps({"ts": last, "event": "gate", "gate_kind": "milestone", "title": "Approve milestone?"}) + "\n")
    st = {"run_id": rd.name, "monitor": "m", "status": "awaiting_approval", "round": r, "updated_at": last,
          "managed": {"ms": milestones - 1, "milestones": [{"title": f"Part {i + 1}", "status": "active"} for i in range(milestones)],
                      "gate": {"kind": "milestone", "key": "milestone-1-0", "title": "Approve milestone?", "text": "GATE-TEXT"},
                      "work": {"writer": {"output": "w" * (big * 10)}}},
          "questions": [{"id": "Q1", "from": "boss", "to": "human", "status": "open", "text": "Keep the appendix?",
                         "asked_at": last}],
          "decisions": [{"id": "S1", "text": "D" * 5000, "by": "you", "source": "answer", "at": last}]}
    (rd / "state.json").write_text(json.dumps(st))
    return rd


class TimelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_critic_reply_uses_recorded_recipient_not_worker_default(self):
        rd = make_run(self.tmp, milestones=1, rounds=1, chats=0)
        path = next(rd.glob('rounds/r*/t*-writer.json'))
        w = json.loads(path.read_text())
        critic = dict(w, agent='critic', role='verifier', purpose='incoming', seq=99,
                      turn_id='r099-t99-critic', output='No intervention needed.')
        (path.parent/'t99-critic.json').write_text(json.dumps(critic))
        st = json.loads((rd/'state.json').read_text())
        st['messages'] = [dict(id='M1', source=critic['turn_id'], **{'from': 'critic'},
                               to='boss', status='held', i=None)]
        (rd/'state.json').write_text(json.dumps(st))
        b = Board('m', lambda: rd)
        _d, items, _gs, _cur = b._timeline()
        it = next(i for i in items if i.get('tid') == critic['turn_id'])
        self.assertEqual((it['from'], it['to'], it['block'], it['status']), ('critic', 'boss', 'mc', 'held'))
        self.assertNotIn('lane', it)

    def test_parallel_followup_without_assignment_uses_newest_step(self):
        turns = [
            dict(turn_id='plan', milestone=3, started_at=ts(0), ended_at=ts(1), plan=[37, 38]),
            dict(turn_id='assigned', agent='a', purpose='work', milestone=3, round=10,
                 started_at=ts(2), par_worker='a', par_step=37),
            dict(turn_id='pilot', agent='b', purpose='work', milestone=3, round=11,
                 started_at=ts(3), par_worker='b', par_step=38),
            dict(turn_id='followup', agent='a', purpose='work', milestone=3, round=12,
                 started_at=ts(4), par_worker='a', par_step=None),
            dict(turn_id='review', agent='critic', purpose='par_review', milestone=3, round=12,
                 started_at=ts(5), par_worker='a', par_step=None),
        ]
        items = [dict(tid=t['turn_id'], ts=t['started_at'], ms=3, _turn=True) for t in turns[1:]]
        assign(items, turns)
        self.assertEqual([it['step'] for it in items], [37, 38, 38, 38])
        self.assertTrue(all('unassigned' not in it for it in items))

    def test_critic_messages_to_two_workers_keep_both_recipients(self):
        from orch.timeline import Builder
        d = dict(agents=[dict(name='critic', role='verifier', called='critic'),
                         dict(name='a', role='worker', called='a'), dict(name='b', role='worker', called='b')],
                 turns=[dict(turn_id='review', agent='critic', role='verifier', purpose='par_review', status='ok')],
                 routes={'review': [dict(i=0, to='a', status='read', preview='A task'),
                                    dict(i=1, to='b', status='read', preview='B task')]})
        builder = Builder(self.tmp, d, [], [])
        builder.turns()
        rows = builder.items
        self.assertEqual(rows[0]['to'], '')  # no invented recipient for the whole turn
        self.assertEqual([(r['to'], r['lane']) for r in rows[1:]], [('a', 'a'), ('b', 'b')])

    def test_historical_monitor_report_follows_watched_worker_in_dashboard(self):
        from datetime import datetime, timedelta
        rd = make_run(self.tmp, milestones=1, rounds=1, chats=0)
        path = next(rd.glob('rounds/r*/t*-writer.json'))
        w = json.loads(path.read_text())
        w['par_step'] = 38
        path.write_text(json.dumps(w))
        when = (datetime.fromisoformat(w['started_at']) + timedelta(seconds=1)).isoformat()
        check = dict(turn_id='historical-monitor', agent='monitor-2', role='monitor', purpose='check',
                     watching='writer', watch_kind='turn', status='error', started_at=when, ended_at=when,
                     output='Monitor failed to inspect the logs.', round=w['round'], seq=99)
        saved = path.parent / 't99-monitor-2.json'
        saved.write_text(json.dumps(check))
        original = saved.read_bytes()
        board = Board('m', lambda: rd)
        items = board.items('0|38')['items']
        report = next(it for it in items if it.get('tid') == 'historical-monitor')
        self.assertEqual((report['block'], report['from'], report['to'], report['status']),
                         ('mon', 'monitor-2', 'critic', 'error'))
        self.assertEqual(saved.read_bytes(), original)

    def test_monitor_checks_follow_their_worker_not_another_parallel_round(self):
        turns = [
            dict(turn_id='a', agent='a', purpose='work', milestone=2, started_at=ts(0), par_step=38),
            dict(turn_id='b', agent='b', purpose='work', milestone=2, started_at=ts(2), par_step=37),
            dict(turn_id='check', purpose='check', watching='a', started_at=ts(4)),
        ]
        items = [dict(tid='check', ts=ts(4), ms=-1, _turn=True), dict(ts=ts(5))]
        assign(items, turns)
        self.assertEqual((items[0]['ms'], items[0]['step']), (2, 38))
        self.assertEqual(items[1]['ms'], 2)  # missing check metadata must not move later events before milestone 1

    def test_message_models_come_from_each_turn_and_chat(self):
        rd = make_run(self.tmp, milestones=1, rounds=1, chats=1)
        for path in rd.glob("rounds/r*/t*.json"):
            t = json.loads(path.read_text())
            t["model"] = "historical-backup"
            if t["agent"] == "writer":
                t.update(status="running", model="worker-primary")
            path.write_text(json.dumps(t))
        with (rd / "chat.jsonl").open("a") as f:
            f.write(json.dumps({"id": "chat-000-1", "model": "chat-backup",
                               "text": '<message to="critic">Proceed</message>'}) + "\n")
        b = Board("m", lambda: rd)
        state = b.state()
        self.assertEqual(state["running"][0]["model"], "worker-primary")
        items = [it for g in state["tl_groups"] for it in b.items(g["key"], all_=True)["items"]]
        for it in items:
            if it["id"].startswith("t:"):
                self.assertEqual(it["model"], "worker-primary" if it["from"] == "writer" else "historical-backup")
            elif it["id"].startswith("c:chat-000-1"):
                if it["from"] == "you":
                    self.assertNotIn("model", it)
                else:
                    self.assertEqual(it["model"], "chat-backup")
        self.assertEqual(next(c for c in state["chat"] if c["id"] == "chat-000-1")["model"], "chat-backup")

    def test_helpers(self):
        self.assertEqual(block_of("human", "manager"), "user")
        self.assertEqual(block_of("manager", "verifier"), "mc")
        self.assertEqual(block_of("verifier", "worker"), "cw")
        self.assertEqual(block_of("monitor", "verifier"), "mon")
        self.assertEqual(block_of("human", "verifier"), "mc")  # I talk to the critic as its usual user
        self.assertEqual(line_of("Here is the plan:\n\n**Step one** is `x`\n<message to=\"w\">later</message>"), "Step one is x")

    def test_recovered_transitions_fill_missing_snapshots_without_rewriting_turns(self):
        rd = make_run(self.tmp, milestones=1, rounds=3)
        reviews = sorted(rd.glob("rounds/r*/t*-critic.json"))
        review = reviews[1]  # finishes step 1, but its historical snapshot was lost
        t = json.loads(review.read_text())
        t["checklist_file"] = {"plan": "", "checklist": ""}
        review.write_text(json.dumps(t))
        before = review.read_bytes()
        recovery = {"run_id": rd.name, "transitions": [{"turn": t["turn_id"], "step": 2, "when": "after"}]}
        (rd / "timeline-steps.json").write_text(json.dumps(recovery))
        b = Board("m", lambda: rd)
        self.assertTrue(any(it.get("purpose") == "work" and "NEEDLE-0-1" in it["line"]
                            for it in b.items("0|2")["items"]))
        # The later valid snapshot still takes over: the third work turn is after the last step.
        self.assertTrue(any("NEEDLE-0-2" in it["line"] for it in b.items("0|3")["items"]))
        self.assertEqual(review.read_bytes(), before)
        recovery["run_id"] = "another-run"
        (rd / "timeline-steps.json").write_text(json.dumps(recovery))
        b = Board("m", lambda: rd)
        self.assertTrue(any("NEEDLE-0-1" in it["line"] for it in b.items("0|1")["items"]))

    def test_parallel_work_and_reviews_follow_their_own_assignment(self):
        turns = [
            {"turn_id": "plan", "milestone": 0, "started_at": ts(0), "ended_at": ts(1), "plan": [1, 19]},
            {"turn_id": "a", "agent": "worker-a", "milestone": 0, "round": 3, "started_at": ts(2), "par_step": 12},
            {"turn_id": "b", "agent": "worker-b", "milestone": 0, "round": 4, "started_at": ts(3), "par_step": 9},
            {"turn_id": "rb", "milestone": 0, "round": 4, "started_at": ts(4), "purpose": "par_review", "par_worker": "worker-b"},
            {"turn_id": "ra", "milestone": 0, "round": 3, "started_at": ts(5), "purpose": "par_review", "par_worker": "worker-a"},
        ]
        items = [{"tid": t["turn_id"], "ts": t["started_at"], "ms": 0, "_turn": True} for t in turns[1:]]
        assign(items, turns)
        self.assertEqual([it["step"] for it in items], [12, 9, 9, 12])

    def test_recovery_is_scoped_to_its_turn_milestone(self):
        turns = [{"turn_id": "old", "milestone": 0, "started_at": ts(0), "ended_at": ts(1)},
                 {"turn_id": "new", "milestone": 1, "started_at": ts(2), "ended_at": ts(3)}]
        items = [{"ts": ts(1)}, {"ts": ts(4)}]
        assign(items, turns, [{"turn": "old", "step": 18}, {"turn": "missing", "step": 99},
                             {"turn": "new", "step": "bad"}])
        self.assertEqual([it["step"] for it in items], [18, 0])

    def test_critic_label_uses_recorded_verdict_and_updates_when_round_finishes(self):
        rd = make_run(self.tmp, milestones=1)
        path = sorted(rd.glob("rounds/r*/t*-critic.json"))[1]
        t = json.loads(path.read_text())
        t["output"] = "All done. <verdict>PASS</verdict>"
        t["checklist_file"] = {"checklist": "- [x] checked", "plan": "- [x] finished"}
        path.write_text(json.dumps(t))
        b = Board("m", lambda: rd)
        self.assertEqual(b._turn_entry(path)[0]["verdict"], "PASS")
        # The scheduler overrules a claimed PASS. Only round.json changes, so its cache key must count too.
        summary = {"turns": [t["turn_id"]], "verdicts": {"critic": "FAIL"}}
        (path.parent / "round.json").write_text(json.dumps(summary))
        self.assertEqual(b._turn_entry(path)[0]["verdict"], "FAIL")
        t["output"] = "Please fix the remaining items."  # current protocol has no verdict tag
        path.write_text(json.dumps(t))
        self.assertEqual(b._turn_entry(path)[0]["verdict"], "FAIL")
        summary["turns"] = ["another-turn"]
        (path.parent / "round.json").write_text(json.dumps(summary))
        self.assertEqual(b._turn_entry(path)[0]["verdict"], "PASS")

    def test_untagged_checklist_review_labels_open_items_and_open_plan(self):
        rd = make_run(self.tmp, milestones=1)
        path = sorted(rd.glob("rounds/r*/t*-critic.json"))[1]
        t = json.loads(path.read_text())
        for checklist, plan, status, purpose, verdict in (
            ("- [ ] fix this", "- [ ] step", "ok", "verify", "FAIL"),
            ("- [x] checked", "- [ ] next step", "ok", "verify", "FAIL"),
            ("- [x] checked", "- [x] step", "ok", "verify", "PASS"),
            ("", "", "ok", "verify", None),
            ("- [ ] fix this", "- [ ] step", "running", "verify", None),
            ("- [ ] fix this", "- [ ] step", "ok", "brief", None),
        ):
            with self.subTest(checklist=checklist, plan=plan, status=status, purpose=purpose):
                t.update(output="Review notes", status=status, purpose=purpose,
                         checklist_file={"checklist": checklist, "plan": plan})
                path.write_text(json.dumps(t))
                self.assertEqual(Board("m", lambda: rd)._turn_entry(path)[0].get("verdict"), verdict)

    def test_plan_steps_stay_out_of_the_timeline(self):
        rd = make_run(self.tmp)  # the event stays in events.jsonl; the board no longer shows it
        with open(rd / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": ts(5), "event": "plan_step", "turn": "r001-t02-boss", "done": 1, "total": 2,
                                "next": "Check the tables"}) + "\n")
        b = Board("m", lambda: rd)
        lines = [it["line"] for g in b.state()["tl_groups"] for it in b.items(g["key"])["items"]]
        self.assertTrue(any("waiting for your OK" in x for x in lines), lines)  # the timeline does have events
        self.assertFalse(any("of 2 done" in x or "next: Check" in x for x in lines), lines)

    def test_state_carries_lines_of_the_open_step_only(self):
        rd = make_run(self.tmp)
        b = Board("m", lambda: rd)
        st = b.state()
        self.assertNotIn("turns", st)
        self.assertNotIn("messages", st)
        self.assertNotIn("work", st["managed"])
        keys = [g["key"] for g in st["tl_groups"]]
        self.assertIn("-1|0", keys)  # the plan, before the milestones
        self.assertIn("0|1", keys)
        self.assertIn("1|3", keys)  # after the last step of milestone 2
        cur = st["tl_cur"]
        self.assertEqual(cur, "1|3")  # where the newest item is
        self.assertTrue(st["tl_items"] and all(f"{it['ms']}|{it['step']}" == cur for it in st["tl_items"]))
        for it in st["tl_items"]:
            self.assertLessEqual(len(it["line"]), 160)
            self.assertNotIn("text", it)
        blocks = {it["block"] for it in st["tl_items"]}
        self.assertTrue({"user", "mc", "cw"} <= blocks, blocks)
        # each step counts its lines per block, so the page shows one card per block without loading every step
        g = next(g for g in st["tl_groups"] if g["key"] == cur)
        for blk in blocks:
            mine = [it for it in st["tl_items"] if it["block"] == blk]
            self.assertEqual(g["blocks"][blk][1], len(mine))
            self.assertEqual(g["blocks"][blk][0], sum(it["kind"] not in OTHER_KINDS for it in mine))
        # a turn's reply carries its round and the sizes of its input and output (communication efficiency)
        whole = [it for it in b.items("0|1")["items"] if it.get("tid") and it["id"] == "t:" + it["tid"]]
        self.assertTrue(whole and all("in_len" in it and "round" in it for it in whole), whole)
        gate = next(it for it in st["tl_items"] if it["kind"] == "gate")
        self.assertTrue(gate["needs"])
        q = next(it for it in st["tl_items"] if it["id"] == "q:Q1")
        self.assertTrue(q["needs"])
        retry = next(it for it in st["tl_items"] if it["kind"] == "system")
        self.assertEqual((retry["block"], retry.get("lane")), ("cw", "writer"))
        # A critic reply with a single recipient lives in manager ↔ critic; unfolding
        # the whole reply retains its tagged message without a duplicate timeline row.
        mc = [it for it in b.items("0|1")["items"] if it["block"] == "mc"]
        reply = next(it for it in mc if it.get("purpose") == "verify")
        self.assertEqual((reply["from"], reply["to"]), ("critic", "boss"))
        self.assertNotIn("lane", reply)
        self.assertIn("FYI step 1 done", b.message(reply["id"])["text"])
        self.assertEqual(len(st["decisions"][0]["text"]), 601)  # clipped; the whole text on unfold
        self.assertEqual(len(b.message("d:S1")["text"]), 5000)

    def test_full_text_and_folded_groups_and_search(self):
        rd = make_run(self.tmp)
        b = Board("m", lambda: rd)
        g = b.items("0|1")
        work = next(it for it in g["items"] if it.get("purpose") == "work")
        full = b.message(work["id"])
        self.assertIn("NEEDLE-0-0", full["text"])
        self.assertEqual(full["item"]["id"], work["id"])
        self.assertEqual(b.message("q:Q1")["text"].split("\n")[0], "Keep the appendix?")
        self.assertIn("GATE-TEXT", b.message(next(it["id"] for it in b.state()["tl_items"] if it["kind"] == "gate"))["text"])
        self.assertIn("Fine 1", b.message("c:chat-001-1")["text"])
        self.assertIn("error", b.message("t:r999-t01-nobody"))
        hits = b.search("needle-1-2")["hits"]  # case-insensitive, over full texts
        self.assertEqual([h["id"] for h in hits], [next(it["id"] for it in b.items("1|3")["items"] if it.get("purpose") == "work"
                                                        and "1-2" in b.message(it["id"])["text"])])
        self.assertIn("NEEDLE-1-2", hits[0]["snip"])
        self.assertEqual(b.search("x")["hits"], [])  # too short

    def test_payload_stays_small_on_a_large_run(self):
        rd = make_run(self.tmp, milestones=4, rounds=40, big=60_000, chats=40)
        b = Board("m", lambda: rd)
        full = len(json.dumps(b.data(), default=str))
        slim = len(json.dumps(b.state(), default=str))
        self.assertGreater(full, 1_000_000)  # what the page used to poll
        self.assertLess(slim, 250_000, slim)  # chat tail in full is the largest part
        self.assertLessEqual(len(b.state()["tl_items"]), ITEMS_MAX)

    def test_http_endpoints_disable_report_and_log_pages_but_keep_file_links(self):
        rd = make_run(self.tmp)
        (rd / "report.html").write_text("<html>Saved report</html>")
        (rd / "review.md").write_text("Review comments")
        b = Board("m", lambda: rd)
        srv, url = serve({"m": b}, port=8930)
        try:
            get = lambda path: json.loads(urllib.request.urlopen(url + path).read())
            st = get("api/state?m=m&since=")
            self.assertIn("tl_items", st["set"])
            self.assertTrue(get("api/items?m=m&g=0%7C1")["items"])
            self.assertIn("NEEDLE", json.dumps(get("api/search?m=m&q=needle")))
            first = get("api/items?m=m&g=0%7C1")["items"][0]["id"]
            self.assertIn("text", get("api/message?m=m&id=" + urllib.request.quote(first)))
            import http.client
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1])
            for path in ("/report?m=m", "/log", "/log/", "/log/index.html", "/log/run?m=m",
                         f"/log/m/{rd.name}/report.html"):
                c.request("GET", path)
                r = c.getresponse()
                self.assertEqual(r.status, 404, path)
                r.read()
            c.close()
            with urllib.request.urlopen(url + f"log/m/{rd.name}/review.md") as r:
                self.assertEqual(r.read().decode(), "Review comments")
            page = urllib.request.urlopen(url).read().decode()
            self.assertNotIn("📜 Log", page)
            self.assertNotIn('id="replink"', page)
            self.assertIn('id="timeline"', page)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_report_cuts_long_texts_on_a_large_run(self):
        small = R.render_run_report(make_run(self.tmp / "a"))
        self.assertNotIn("more characters", small)
        rd = make_run(self.tmp / "b", milestones=2, rounds=40, big=60_000, chats=0)
        html_ = R.render_run_report(rd)
        self.assertIn("report cuts replies", html_)
        self.assertIn("more characters: the whole text is in rounds/", html_)
        self.assertLess(len(html_), 8_000_000)


if __name__ == "__main__":
    unittest.main()


class GoldenEditTest(unittest.TestCase):
    """I edit the golden board on the page: saved, refused on a newer file, and the manager is told what changed."""

    def test_edit_golden(self):
        from orch import boards as B
        tmp = Path(tempfile.mkdtemp())
        rd = make_run(tmp)
        gp = tmp / "golden.md"
        gp.write_text(B.GOLDEN_HEAD + "- Python 3 only.\n", encoding="utf-8")
        st = json.loads((rd / "state.json").read_text())
        st["golden"] = {"path": str(gp), "limit": 8000}
        (rd / "state.json").write_text(json.dumps(st))
        b = Board("m", lambda: rd)
        g = b.state()["golden"]
        self.assertEqual(g["raw"], gp.read_text())
        # the manager edited it after I opened the editor: refused, nothing written
        gp.write_text(g["raw"] + "- Tests use unittest.\n", encoding="utf-8")
        r = b.edit_golden(g["raw"] + "- Mine.\n", g["hash"])
        self.assertTrue(r.get("conflict"))
        self.assertNotIn("Mine", gp.read_text())
        # from the current text: saved, and one note to the manager with my change
        g = b.state()["golden"]
        r = b.edit_golden(g["raw"].replace("- Python 3 only.", "- Python 3.11 only."), g["hash"])
        self.assertTrue(r.get("ok"))
        self.assertIn("Python 3.11 only.", gp.read_text())
        self.assertIn("Tests use unittest.", gp.read_text())
        notes = [json.loads(p.read_text()) for p in (rd / "inbox").glob("*-board-note.json")]
        self.assertEqual(len(notes), 1)
        self.assertIn("+- Python 3.11 only.", notes[0]["message"])
        self.assertIn("-- Python 3 only.", notes[0]["message"])
        self.assertNotIn("to", notes[0])  # to the manager, as from its usual user
        # saving the same text again changes nothing and sends nothing
        self.assertTrue(b.edit_golden(gp.read_text(), B.digest(gp.read_text())).get("unchanged"))
        self.assertEqual(len(list((rd / "inbox").glob("*-board-note.json"))), 1)
