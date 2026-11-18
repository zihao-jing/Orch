"""B1: each agent sees only its own user; no layer above leaks into the critic's or the worker's prompts."""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch.backends import MockBackend, TurnResult  # noqa: E402
from orch.config import load_config  # noqa: E402
from role_view_checks import assert_no_role_leaks, leaks, role_leaks  # noqa: E402
from test_orch import make_cfg, managed, run, turns  # noqa: E402

CHAT = "USERCHAT"  # marks the manager's text for the real user: must never reach the critic or the worker


def _leaky_run(tmp: Path):
    mgr = {"milestones": 2, "replies": {
        "kickoff": [f"{CHAT}-1 You asked me to hurry, so I am starting the critic now.\n\n"
                    '<message to="Jordan">Edit the intro of the plan.</message>\n<state>intro edit</state>'],
        "answer": [f"{CHAT}-2 I would pick A, since you said you prefer short tables."],  # no tag: asked again
        "review": [f"| depth | 2 |\n\n{CHAT}-3 Not done yet; I will push the critic.\n\n"
                   '<message to="Jordan">Add the missing table.</message>\n<score>2</score>\n<decision>REVISE</decision>',
                   "<score>5</score>\n<decision>ACCEPT</decision>"]}}
    critic = {"replies": {"verify": [
        '<message to="Taylor">hello</message>\n<question>Short or long tables?\nA) short\nB) long</question>\n'
        "Fix the headings.\n<checklist>- [ ] headings</checklist><verdict>FAIL</verdict>",
        "Done.\n<checklist>- [x] headings</checklist><verdict>PASS</verdict>"]}}
    worker = {"replies": {"work": ['Edited the intro.\n\n<message to="Jordan">ping</message>', "Done."]}}
    cfg = make_cfg(tmp, [managed(mgr=mgr, critic=critic, worker=worker)])
    status, rec = run(cfg)
    return status, rec, load_config(cfg).monitor("m")


class TestRoleViews(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_the_helper_finds_leaks_and_allows_tag_syntax_and_paths(self):
        self.assertTrue(leaks("Your manager wants this.", "verifier"))
        self.assertTrue(leaks("The critic said so.", "worker"))
        self.assertTrue(leaks("Sent by orch.", "verifier"))
        self.assertTrue(leaks("Ask Alex.", "verifier", ["Alex"]))
        self.assertFalse(leaks("The critic said so.", "verifier"))  # the critic may know its own role
        self.assertFalse(leaks('Write <message to="manager">…</message> for that.', "verifier"))
        self.assertFalse(leaks("Notes in `/p/.orch/agents/critic/state.md`.", "worker"))

    def test_no_layer_above_and_no_manager_chat_reach_the_critic_or_the_worker(self):
        status, rec, mon = _leaky_run(self.tmp)
        self.assertEqual(status, "completed")
        assert_no_role_leaks(self, rec, mon, also=[CHAT])
        ts = turns(rec)
        brief = next(t for t in ts if t["purpose"] == "brief")
        self.assertIn("Edit the intro of the plan.", brief["input"])  # only the explicit message
        guidance = next(t for t in ts if t["purpose"] == "guidance")
        self.assertIn("Add the missing table.", guidance["input"])
        # an answer without <message to="critic"> is asked for again; the untagged text for me is never relayed
        seq = [(t["agent"], t["purpose"]) for t in ts]
        i = seq.index(("boss", "answer"))
        self.assertEqual(seq[i + 1], ("boss", "message"))
        self.assertIn("Nothing in your last reply reached Jordan", ts[i + 1]["input"])
        amend = next(t for t in ts if t["purpose"] == "amend")
        self.assertIn("Please get the first part of the goal done", amend["input"])

    def test_an_invalid_address_gets_a_one_line_correction(self):
        status, rec, _mon = _leaky_run(self.tmp)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        crit = [t for t in ts if t["agent"] == "critic"]
        i = next(n for n, t in enumerate(crit) if '<message to="Taylor">' in (t["output"] or ""))
        later = "\n".join(t["input"] for t in crit[i + 1:])
        self.assertIn('Not delivered: <message to="Taylor">.', later)
        self.assertIn('to me (`<message to="you">`)', later)
        work = [t for t in ts if t["agent"] == "writer"]
        self.assertIn("was not delivered. Write to me directly", work[1]["input"])
        self.assertNotIn("not delivered", work[0]["input"])
        events = [json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines()]
        self.assertEqual(sorted(e["agent"] for e in events if e["event"] == "route_problem"), ["critic", "writer"])
        self.assertIn('<message to="Taylor"> names no one it can write to; not sent',
                      (rec.run_dir / "progress.md").read_text())

    def test_the_decisions_the_critic_reads_skip_the_managers_text_for_me(self):
        from orch.managed import ManagedRunner
        status, rec, mon = _leaky_run(self.tmp)
        r = ManagedRunner(mon, threading.Event(), resume=True, log=lambda s: None)
        r.rec = rec
        r.decide(f"Q9 short tables?: {CHAT}-night I chose A for you", "manager (night mode)", "Q9")
        r.decide(f"{CHAT}-note please hurry", "you", "note to all")
        r.decide("Keep tables to one page.", "you", "note to critic")
        critic = mon.agent("critic")
        text = r.decisions_text(critic)
        self.assertNotIn(CHAT, text)
        self.assertIn("Keep tables to one page.", text)
        self.assertFalse(leaks(text, "verifier", ["Alex", "boss"]))
        self.assertIn(CHAT, r.decisions_text())  # the manager still reads everything
        self.assertNotRegex((rec.run_dir / "decisions.md").read_text(), r"(?i)\b(manager|boss|Alex)\b")
        handover = r.handover(critic)
        self.assertFalse(leaks(handover, "verifier", ["Alex", "boss"]), handover)
        self.assertNotIn(CHAT, handover)

    def _chat(self, rec, mon, who, message, reply):
        from orch.board import Board
        seen = []

        def spy(backend, req, should_stop):
            seen.append(req)
            return TurnResult("ok", reply, req.session_id or "mock-chat", 0, "", ["mock"], {})
        b = Board("m", lambda: rec.run_dir, mon)
        with mock.patch.object(MockBackend, "run", spy):
            cid = b.chat(message, agent=who)["id"]
            done = []
            for _ in range(100):
                rows = [json.loads(x) for x in (rec.run_dir / "chat.jsonl").read_text().splitlines()]
                done = [x for x in rows if x.get("id") == cid and x.get("status") not in (None, "running")]
                if done:
                    break
                time.sleep(0.05)
        self.assertTrue(done, "the chat reply did not arrive")
        return seen[0], done[0]

    def test_board_chat_reaches_the_critic_and_worker_as_from_their_usual_user(self):
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})])).monitor("m")
        inbox = rec.run_dir / "inbox"
        before = set(inbox.glob("*.json")) if inbox.exists() else set()
        req, row = self._chat(rec, mon, "critic", "How is it going?", "Fine.\n<message to=\"Sam\">Use short tables.</message>")
        self.assertRegex(req.prompt, r"Answer my message")
        self.assertIn('<message to="Sam">', req.prompt)
        self.assertNotRegex(req.prompt, r"(?i)\b(board|user|manager|orch|Alex)\b")
        sent = [json.loads(p.read_text()) for p in set(inbox.glob("*.json")) - before]
        self.assertEqual([(x["to"], x["message"]) for x in sent], [(["writer"], "Use short tables.")])
        before = set(inbox.glob("*.json"))
        req, row = self._chat(rec, mon, "writer", "What did you do?", 'Edited.\n<message to="Jordan">ping</message>')
        self.assertRegex(req.prompt, r"Answer my message")
        self.assertNotRegex(req.prompt, r"(?i)\b(board|user|manager|critic|orch|Alex|Jordan)\b|<message")
        notes = [json.loads(p.read_text()) for p in set(inbox.glob("*.json")) - before]
        self.assertEqual([x for x in notes if not x.get("kind")], [])  # a worker addresses no one
        self.assertEqual([x.get("kind") for x in notes], ["correction"])  # corrected at its next turn
        self.assertTrue(row.get("problems"))

    def test_the_managers_chat_with_me_passes_only_its_message_to_the_critic(self):
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})])).monitor("m")
        inbox = rec.run_dir / "inbox"
        before = set(inbox.glob("*.json")) if inbox.exists() else set()
        self._chat(rec, mon, None, "Tell the critic to use short tables.",
                   f"{CHAT}-chat Sure, I'll pass it on.\n<message to=\"Jordan\">Use short tables.</message>")
        sent = [json.loads(p.read_text()) for p in set(inbox.glob("*.json")) - before]
        self.assertEqual([x["message"] for x in sent], ["Use short tables."])
        self.assertNotIn(CHAT, json.dumps(sent))

    def test_default_prompts_keep_each_role_view(self):
        status, rec = run(make_cfg(self.tmp, [managed(mgr={"milestones": 1})]))
        mon = load_config(make_cfg(self.tmp, [managed(mgr={"milestones": 1})])).monitor("m")
        self.assertEqual(role_leaks(rec, mon), [])
        sp = {t["agent"]: t["system_prompt"] for t in turns(rec)}
        self.assertIn('to="Sam"` is the address of the person you instruct', sp["critic"])
        self.assertIn("Possible, but not suggested", sp["boss"])
        self.assertIn("does not know that I exist", sp["boss"])
        self.assertIn("there is nobody\nelse to address", sp["writer"])


if __name__ == "__main__":
    unittest.main()
