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
from orch.timeline import ITEMS_MAX, block_of, line_of  # noqa: E402

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

    def test_helpers(self):
        self.assertEqual(block_of("human", "manager"), "user")
        self.assertEqual(block_of("manager", "verifier"), "mc")
        self.assertEqual(block_of("verifier", "worker"), "cw")
        self.assertEqual(block_of("monitor", "verifier"), "mon")
        self.assertEqual(block_of("human", "verifier"), "mc")  # I talk to the critic as its usual user
        self.assertEqual(line_of("Here is the plan:\n\n**Step one** is `x`\n<message to=\"w\">later</message>"), "Step one is x")

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
        # the critic's <message to="boss"> in a verify turn shows in the manager ↔ critic block too
        mc = [it for it in b.items("0|1")["items"] if it["block"] == "mc"]
        self.assertTrue(any(it["id"].count(":") == 2 and "FYI step 1 done" in it["line"] for it in mc), mc)
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

    def test_http_endpoints_log_page_gone_and_report(self):
        rd = make_run(self.tmp)
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
            c.request("GET", "/log/")
            r = c.getresponse()
            self.assertEqual((r.status, r.getheader("Location")), (302, "../"))
            r.read()
            page = urllib.request.urlopen(url).read().decode()
            self.assertNotIn("📜 Log", page)
            self.assertIn('id="timeline"', page)
        finally:
            srv.shutdown()

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
