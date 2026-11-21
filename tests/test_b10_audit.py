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


class TestAuditorPrompt(unittest.TestCase):
    def test_the_prompt_states_the_design(self):
        p = " ".join((ROOT / "orch" / "prompts" / "auditor.md").read_text().lower().split())
        for s in ("role views", "layers above are hidden", "held by design", "checklist", "manager–user conversation",
                  "check-ins", "monitor", "next regular turn", "discussion mode", "golden board", "reset at milestones",
                  "compacted", "retried", "backup", "delivered share"):
            self.assertIn(s, p)


if __name__ == "__main__":
    unittest.main()
