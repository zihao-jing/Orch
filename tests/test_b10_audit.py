"""B10: the auditor knows orch's design; its digest has times, the routing ledger and reminder-free checks."""
import json
import tempfile
import unittest
from pathlib import Path

from orch import audit
from orch import reminders as R
from orch.routing import NEXT_TURN

ROOT = Path(__file__).resolve().parents[1]


def turn(rd, tid, agent, role, start, end, inp, out, purpose="work"):
    rnd = tid[1:4]
    (rd / "rounds" / f"r{rnd}").mkdir(parents=True, exist_ok=True)
    (rd / "rounds" / f"r{rnd}" / f"{tid.split('-', 1)[1]}.json").write_text(json.dumps(
        {"turn_id": tid, "round": int(rnd), "agent": agent, "role": role, "purpose": purpose, "status": "ok",
         "started_at": f"2026-01-01T{start}", "ended_at": f"2026-01-01T{end}", "input": inp, "output": out}))


class TestDigestTimes(unittest.TestCase):
    def setUp(self):
        self.rd = Path(tempfile.mkdtemp()) / "run"
        self.rd.mkdir()
        long = "Run the second arm with the corrected span and report the table rows. " * 6
        turn(self.rd, "r001-t01-crit", "crit", "verifier", "10:00:00", "10:05:00", "Plan the work please. " * 20,
             "Step one: " + long + "\n\n<checklist>\n- [ ] rows\n</checklist>")
        turn(self.rd, "r001-t02-wk", "wk", "worker", "10:40:00", "10:50:00", long, "Done. " * 100)
        turn(self.rd, "r001-t03-boss", "boss", "manager", "11:00:00", "11:02:00", "note " * 40, "ok " * 200)
        state = {
            "messages": [
                {"id": "M1", "from": "crit", "to": "wk", "body": long, "status": "delivered", "turn": "r001-t02-wk",
                 "created_at": "2026-01-01T10:05:00", "delivered_at": "2026-01-01T10:40:00", "how": "prompt"},
                {"id": "M2", "from": "crit", "to": "wk", "body": "- [ ] rows", "status": "held", "by_design": True,
                 "kind": "checklist", "reason": "held by design: the critic's own checklist",
                 "created_at": "2026-01-01T10:05:00"},
                {"id": "M3", "from": "crit", "to": "boss", "body": "FYI the step is going well.", "status": "delivered",
                 "next_turn": True, "reason": NEXT_TURN, "turn": "r001-t03-boss",
                 "created_at": "2026-01-01T10:06:00", "delivered_at": "2026-01-01T11:00:00"},
                {"id": "M4", "from": "boss", "to": "crit", "body": "Old instruction", "status": "superseded",
                 "by": "M5", "created_at": "2026-01-01T10:07:00", "updated_at": "2026-01-01T10:08:00"},
            ],
            "questions": [
                {"id": "Q1", "from": "wk", "to": "crit", "status": "answered", "answered_by": "crit",
                 "asked_at": "2026-01-01T10:45:00", "answered_at": "2026-01-01T10:55:00"},
                {"id": "Q2", "from": "crit", "to": "boss", "status": "open", "asked_at": "2026-01-01T10:58:00"},
            ],
        }
        (self.rd / "state.json").write_text(json.dumps(state))
        self.md, self.stats = audit.build(self.rd, 1, 1)

    def test_turns_have_start_and_end(self):
        self.assertIn("| start–end |", self.md)
        self.assertIn("| r001-t02-wk | 10:40:00–10:50:00 |", self.md)

    def test_messages_show_wait_from_sent_to_delivered(self):
        self.assertIn("M1 crit → wk sent 10:05:00, delivered 10:40:00 in r001-t02-wk, waited 35 min", self.md)
        flag = [f for f in self.stats["flags"] if f["kind"] == "message waited"]
        self.assertEqual([f["line"].split()[0] for f in flag], ["M1"])  # M3's wait was a next-turn hold, by design

    def test_held_by_design_is_not_lost_and_not_counted_against_delivery(self):
        line = next(ln for ln in self.md.splitlines() if ln.startswith("- M2 "))
        self.assertIn("held by design (checklist)", line)
        self.assertIn("not lost", line)
        self.assertIn("M3", self.md)
        self.assertIn("held for the manager's next turn, by design", self.md)
        c = self.stats["ledger"]
        self.assertEqual((c["held by design"], c["superseded"], c["delivered"]), (1, 1, 2))
        self.assertEqual(c["delivered_share"], 1.0)
        self.assertIn("Delivered share of those meant to arrive (not held by design, not superseded, not dropped): 100%", self.md)

    def test_questions_opened_and_closed(self):
        self.assertIn("Q1: wk → crit opened 10:45:00, answered by crit 10:55:00 (open 10 min)", self.md)
        self.assertIn("Q2: crit → boss opened 10:58:00, still open after 4 min", self.md)


class TestRemindersStripped(unittest.TestCase):
    def test_every_check_reads_messages_without_reminders(self):
        rd = Path(tempfile.mkdtemp()) / "run"
        rd.mkdir()
        big = "Write briefly, in plain language. " * 1000  # a reminder alone past the large-message size
        body = "Please rerun the arm with the corrected span; report the rows and the totals afterwards."
        reply = "Done: rows written to results/x.json, totals match the earlier run exactly as expected. " * 5
        turn(rd, "r001-t01-wk", "wk", "worker", "10:00:00", "10:01:00", R.append(body, big), R.append(reply, big))
        turn(rd, "r001-t02-wk", "wk", "worker", "10:02:00", "10:03:00", R.append(body, big), "ok")
        turn(rd, "r001-t03-crit", "crit", "verifier", "10:04:00", "10:05:00", R.append(reply, big),
             R.append("Fine.", "x" * 2000))
        md, stats = audit.build(rd, 1, 1)
        kinds = [f["kind"] for f in stats["flags"]]
        self.assertNotIn("large message", kinds)
        self.assertNotIn("repeated content", kinds)  # the shared reminder is no repeat
        short = [f["turn"] for f in stats["flags"] if f["kind"] == "short or failed reply"]
        self.assertIn("r001-t03-crit", short)  # "Fine." plus a long reminder is still a short reply
        self.assertNotIn("Write briefly, in plain language", md)
        # the relay from the worker's reply is measured on the reply without its reminder
        rc = audit.relay_coverage(audit.trace(audit.load_turns(rd), [])[2],
                                  {t["turn_id"]: t for t in audit.load_turns(rd)})
        self.assertEqual([(r["source"], r["share"]) for r in rc], [("r001-t01-wk", 1.0)])
        text, _ = audit.passable({"agent": "wk", "role": "worker", "output": R.append(reply, big)},
                                 {"agent": "crit", "role": "verifier"})
        self.assertNotIn("system-reminder", text)


class TestShortReplyFlags(unittest.TestCase):
    def flagged(self, purpose, status_out, status="ok"):
        rd = Path(tempfile.mkdtemp()) / "run"
        rd.mkdir()
        turn(rd, "r001-t01-x", "x", "worker", "10:00:00", "10:01:00", "Check it.", status_out, purpose=purpose)
        if status != "ok":
            path = rd / "rounds" / "r001" / "t01-x.json"
            data = json.loads(path.read_text())
            data["status"] = status
            path.write_text(json.dumps(data))
        _, stats = audit.build(rd, 1, 1)
        return [f["turn"] for f in stats["flags"] if f["kind"] == "short or failed reply"]

    def test_short_ok_verify_turn_is_not_flagged(self):
        verdict = "Checked the rows against the spec; all match. <verdict>PASS</verdict>"
        self.assertLess(len(verdict), audit.TINY_REPLY)
        self.assertEqual(self.flagged("verify", verdict), [])

    def test_compact_turn_with_empty_output_is_not_flagged(self):
        self.assertEqual(self.flagged("compact", ""), [])

    def test_short_ok_work_turn_is_still_flagged(self):
        self.assertEqual(self.flagged("work", "Fine."), ["r001-t01-x"])

    def test_verify_turn_with_error_status_is_still_flagged(self):
        self.assertEqual(self.flagged("verify", "Checked.", status="error"), ["r001-t01-x"])


class TestLedgerOrigin(unittest.TestCase):
    def test_a_short_delivered_message_is_traced_to_its_sender(self):
        rd = Path(tempfile.mkdtemp()) / "run"
        rd.mkdir()
        short = "本轮查询修订到此结束。保留已交审的架构稿。"
        self.assertLess(len(short), audit.MIN_PARA)
        turn(rd, "r001-t01-crit", "crit", "verifier", "10:00:00", "10:01:00", "Plan the work please. " * 5,
             f'<message to="wk">{short}</message>')
        turn(rd, "r002-t01-wk", "wk", "worker", "10:02:00", "10:03:00", short + "\n", "Understood, holding. " * 20)
        (rd / "state.json").write_text(json.dumps({"messages": [
            {"id": "M1", "from": "crit", "to": "wk", "body": short, "text": short, "source": "r001-t01-crit",
             "status": "delivered", "turn": "r002-t01-wk", "created_at": "2026-01-01T10:01:00",
             "delivered_at": "2026-01-01T10:02:00", "how": "prompt"}]}))
        md, _ = audit.build(rd, 2, 2)
        self.assertIn(f"| {len(short)} | 0 | 419 | 0 | 0.00 | critic {len(short)} |", md)
        self.assertIn(f"- r002-t01-wk ⇐ r001-t01-crit 100% of {len(short)}", md)
        # without the ledger, short text is not traced (no guessing from a short match)
        self.assertEqual(audit.trace(audit.load_turns(rd), [])[1]["paras"], [])


class TestRetries(unittest.TestCase):
    def setUp(self):
        self.rd = Path(tempfile.mkdtemp()) / "run"
        self.rd.mkdir()
        rep = "The milestone report: everything checked against the spec, rows match. " * 4
        turn(self.rd, "r001-t01-boss", "boss", "manager", "10:00:00", "10:01:00", rep, "Looks fine " * 40,
             purpose="review")
        turn(self.rd, "r001-t02-boss", "boss", "manager", "10:01:00", "10:01:10",
             "Please end your reply with `<decision>ACCEPT</decision>`, `<decision>REVISE</decision>` or "
             "`<decision>PAUSE</decision>`.", "<decision>REVISE</decision>", purpose="review")
        turn(self.rd, "r001-t03-boss", "boss", "manager", "10:01:10", "10:01:20",
             "Nothing in your last reply reached the critic: only `<message to=\"the critic\">…</message>` is "
             "passed on.", "<message to=\"critic\">Redo the rows.</message>", purpose="message")
        turn(self.rd, "r001-t04-boss", "boss", "manager", "10:01:20", "10:01:30",
             "Your overall score of 2/5 is below the 4/5 we agreed on. What should the critic do next?", "Redo.",
             purpose="review")
        turn(self.rd, "r002-t01-boss", "boss", "manager", "10:30:00", "10:31:00", "A new task: " + rep,
             "On it " * 60, purpose="review")
        self.md, self.stats = audit.build(self.rd, 1, 2)

    def test_reasks_are_marked_in_the_turn_table(self):
        self.assertEqual(self.stats["retries"], {
            "r001-t02-boss": ("r001-t01-boss", "invalid decision"),
            "r001-t03-boss": ("r001-t02-boss", "no message for the critic"),
            "r001-t04-boss": ("r001-t03-boss", "score below min_score")})
        self.assertIn("| ok · retry of r001-t01-boss (invalid decision) |", self.md)
        self.assertIn("*retry of X (why)*", self.md)

    def test_retries_are_explained_in_flags_and_not_a_burst(self):
        kinds = [f["kind"] for f in self.stats["flags"]]
        self.assertNotIn("burst of turns", kinds)
        short = [f for f in self.stats["flags"] if f["kind"] == "short or failed reply"]
        self.assertTrue(short and all("it answers orch's retry of" in f["line"] for f in short))

    def test_structural_rule_needs_a_short_orch_only_message_in_the_same_purpose(self):
        tr = audit.trace(audit.load_turns(self.rd), [])
        ids = {x["turn"]["turn_id"]: x["turn"] for x in tr}
        ids["r001-t02-boss"]["input"] = "Something else orch asks, briefly."  # same purpose as r001-t01: review
        ids["r001-t04-boss"]["input"] = "Something else orch asks, briefly."  # after a "message" turn
        got = audit.retries(tr, {})
        self.assertEqual(got["r001-t02-boss"], ("r001-t01-boss", "asked again"))
        self.assertNotIn("r001-t04-boss", got)
        self.assertNotIn("r002-t01-boss", got)  # a long message with a task is no retry
        self.assertNotIn("r001-t02-boss", audit.retries(tr, {"r001-t02-boss": "r001-t01-boss"}))  # a redo


class TestAuditorPrompt(unittest.TestCase):
    def test_the_prompt_states_the_design(self):
        p = " ".join((ROOT / "orch" / "prompts" / "auditor.md").read_text().lower().split())
        for s in ("role views", "layers above are hidden", "held by design", "checklist", "manager–user conversation",
                  "check-ins", "monitor", "next regular turn", "discussion mode", "golden board", "reset at milestones",
                  "compacted", "retried", "backup", "delivered share"):
            self.assertIn(s, p)


if __name__ == "__main__":
    unittest.main()
