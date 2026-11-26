"""Q: the audit of the new runtime (AUD-1…4, UI-4)."""
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from rt_e2e_helpers import CRITIC, MANAGER, RuntimeCase
from rt_helpers import C, T0, gate, job, message, snapshot
from orch.runtime import audit
from orch.runtime.adapters.mock import MockCLI, Reply

VERSIONS = ["Orch test"]


class FakeAdapter:
    def __init__(self, reply, status=C.TurnStatus.OK):
        self.reply, self.status, self.sent, self.stopped, self.spec = reply, status, [], False, None
        self._events = []

    def start(self, spec, session):
        self.spec = spec

    def send(self, mid, text):
        self.sent.append(text)
        self._events.append(C.TurnEnded(agent="auditor", at=T0, status=self.status, text=self.reply))

    def events(self):
        out, self._events = self._events, []
        return out

    def stop(self):
        self.stopped = True


def spec():
    return C.AgentSpec("auditor", C.Role.AUDITOR, C.Choice(backend="mock"), "/tmp", "At most {points} points.", "/tmp")


class Facts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run = Path(tmp.name)

    def snap(self, *records, **kw):
        info = C.RunInfo(id="r1", run_dir=str(self.run), workspace=str(self.run), started=T0 - 5000)
        return snapshot(*records, run=info, now=T0, **kw)

    def test_AUD_2_exact_facts(self):
        s = self.snap(
            message("xwait", recipient="critic", status="waiting", created=T0 - 900),
            message("xfresh", recipient="critic", status="waiting", created=T0 - 30),
            message("xfail", recipient="worker-1", status="failed", created=T0 - 100, error="no such worker"),
            message("xopen", recipient="critic", needs_answer=True, created=T0 - 4000, delivered_at=T0 - 3990),
            message("xuser", recipient="user", status="waiting", created=T0 - 900),
            job("worker-1/j1", state="running", started=T0 - 9000, last_progress=T0 - 9000),
            paused=True)
        facts = audit.facts(self.run, s, list(s.messages.values()), [], T0 - 5000)
        text = "\n".join(facts)
        self.assertIn("not delivered: xwait", text)
        self.assertNotIn("xfresh", text)
        self.assertNotIn("xuser", text)  # messages to the user end at delivered and count for nothing
        self.assertIn("failed: xfail", text)
        self.assertIn("unanswered: xopen", text)
        self.assertIn("job without end: worker-1/j1", text)
        self.assertIn("missing gate: the run is paused", text)

    def test_AUD_2_a_question_without_its_gate(self):
        turns = [{"turn_id": "r001-t01-manager", "role": "manager", "status": "ok",
                  "output": "<question>Which?</question>"},
                 {"turn_id": "r001-t02-manager", "role": "manager", "status": "ok",
                  "output": "<question>Other?</question>"}]
        s = self.snap(gate("g1", source_turn="r001-t02-manager"))
        facts = audit.facts(self.run, s, [], turns, 0)
        self.assertEqual([f for f in facts if "missing gate" in f],
                         ["missing gate: r001-t01-manager asks a <question> but no question gate came from it"])

    def test_AUD_4_a_window_of_zero_and_the_version(self):
        self.assertEqual(audit.excerpt("text", 0), "(text not included)")
        self.assertEqual(audit.excerpt("", 50), "(empty)")
        s = self.snap(message("xm1", body="Hello there"))
        digest, _ = audit.build(self.run, s, list(s.messages.values()), limit=0, versions=["Orch 9, commit abc"])
        self.assertIn("Orch 9, commit abc", digest)
        self.assertIn("(text not included)", digest)
        self.assertNotIn("nothing written", digest.lower())
        lines = audit.version_lines()
        self.assertTrue(lines[0].startswith("Orch "))

    def test_a_note_to_the_user_is_shown_in_its_words(self):
        note = message("xnote", sender="orch", recipient="user", body="", status="delivered",
                       text=C.TextRef(C.TextKey.NOTE_FINISHED, {}))
        s = self.snap(note)
        digest, seen = audit.build(self.run, s, list(s.messages.values()), versions=VERSIONS)
        self.assertIn("All milestones are done.", digest)
        self.assertNotIn("(empty)", digest.split("## Messages")[1].split("## Turns")[0])
        self.assertIn("All milestones are done.", seen["ids"]["xnote"])

    def test_AUD_1_the_window_starts_at_the_run_and_after_the_last_audit(self):
        s = self.snap(message("xold", created=T0 - 6000), message("xnew", created=T0 - 100))
        _, seen = audit.build(self.run, s, list(s.messages.values()), versions=VERSIONS)
        self.assertEqual(list(seen["ids"]), ["xnew"])  # before the run's start in this runtime: not audited
        (self.run / "audits").mkdir()
        (self.run / "audits" / "a.json").write_text(json.dumps({"status": "ok", "until": T0 - 50}))
        _, seen = audit.build(self.run, s, list(s.messages.values()), versions=VERSIONS)
        self.assertEqual(seen["ids"], {})

    def test_AUD_3_points_are_checked(self):
        s = self.snap(message("xm1", body="Please rerun the whole benchmark suite tonight."))
        _, seen = audit.build(self.run, s, list(s.messages.values()), versions=VERSIONS)
        notes = "- Repeated benchmark instructions to the critic: won't fix, by design"
        points = ("1. Repeated benchmark instructions to the critic\nxm1 says \"rerun the whole benchmark suite\".\n"
                  "2. Invented\nxzzz said \"something\".\n"
                  "3. Misquoted\nxm1 says \"delete every file in the folder\".\n"
                  "4. A real one\nxm1: \"Please rerun the whole benchmark\" took a full turn.")
        kept, dropped = audit.verify(points, seen, notes)
        self.assertEqual(kept, "1. A real one\nxm1: \"Please rerun the whole benchmark\" took a full turn.")
        whys = [w for _, w in dropped]
        self.assertTrue(whys[0].startswith("dismissed in the user's notes"))
        self.assertEqual(whys[1], "cites no message or turn of this stretch")
        self.assertTrue(whys[2].startswith("quote not found"))
        self.assertEqual(audit.verify("Nothing new.", seen, "")[0], "Nothing new.")

    def test_one_audit_writes_its_record_and_the_findings(self):
        notes = self.run / "audit-decisions.md"
        notes.write_text("My notes.\n")
        s = replace(self.snap(message("xm1", body="Status?")),
                    run=C.RunInfo(id="r1", run_dir=str(self.run), workspace=str(self.run), started=T0 - 5000,
                                  audit_notes_path=str(notes)))
        reply = ("<points>\n1. Bare status question\nxm1 asked \"Status?\" only.\n2. Made up, xnone.\n</points>\n"
                 "<findings>\n- [efficiency] status pings · seen 1× since 2026-11-26 · open\n</findings>")
        ad = FakeAdapter(reply)
        rec = audit.run_audit(self.run, s, list(s.messages.values()), ad, spec(), points=3, versions=VERSIONS)
        self.assertEqual(rec["status"], "ok")
        self.assertTrue(ad.stopped)
        self.assertEqual(ad.spec.system_prompt, "At most 3 points.")
        self.assertIn("My notes.", ad.sent[0])
        self.assertEqual(rec["points"], "1. Bare status question\nxm1 asked \"Status?\" only.")
        self.assertEqual(len(rec["not_confirmed"]), 1)
        md = next((self.run / "audits").glob("*.md")).read_text()
        self.assertIn("## Sent", md)
        self.assertIn("## Reply\n\n1. Bare status question", md)
        found = (self.run / "audit.md").read_text()
        self.assertIn("- [efficiency] status pings", found)
        self.assertEqual(audit.findings_lines(self.run / "audit.md"),
                         ["[efficiency] status pings · seen 1× since 2026-11-26 · open"])

    def test_a_failed_or_empty_audit(self):
        s = self.snap(message("xm1"))
        rec = audit.run_audit(self.run, s, list(s.messages.values()), FakeAdapter("", C.TurnStatus.ERROR), spec(),
                              versions=VERSIONS)
        self.assertEqual(rec["status"], "error")
        self.assertFalse((self.run / "audit.md").exists())
        empty = self.snap()
        rec = audit.run_audit(self.run, empty, [], FakeAdapter("x"), spec(), versions=VERSIONS)
        self.assertEqual(rec["status"], "empty")

    def test_the_prompt_is_the_new_one(self):
        text = (audit.Path(audit.__file__).parent / "prompts" / "auditor.md").read_text()
        for gone in ("pipeline", "protocol version", "upper-default-v1", "golden board (standing rules) reaches"):
            self.assertNotIn(gone, text)
        self.assertIn("{points}", text)
        self.assertIn("(text not included)", text)


class EngineAudit(RuntimeCase):
    def test_AuditRun_starts_one_auditor_process_and_records_it(self):
        bank = self.banks["claude"]
        bank["auditor"] = MockCLI(self.clock, "auditor", default=Reply("<points>\nNothing new.\n</points>\n"
                                                                       "<findings>\n</findings>", after=0))
        self.start()
        self.chat(MANAGER, "Begin.")
        self.advance(2)
        reply = self.command(C.AuditRun)
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(self.state.run.audit_running)
        again = self.command(C.AuditRun)
        self.assertFalse(again["ok"])
        self.assertEqual(again["data"]["why"], "an audit is already running")
        for _ in range(100):
            self.advance(1)
            if not self.state.run.audit_running:
                break
            time.sleep(0.02)
        self.assertFalse(self.state.run.audit_running)
        records = audit.records(self.path / "run")
        self.assertEqual([r["status"] for r in records], ["ok"])
        self.assertEqual(len(bank["auditor"].sent), 1)
        self.assertIn("## Facts Orch checked", bank["auditor"].sent[0][1])
        self.assertNotIn("auditor", self.state.agents)  # not one of the agents: never routed to

    def test_automatic_audits_every_n_rounds_and_when_the_run_finishes(self):
        bank = self.banks["claude"]
        bank["auditor"] = MockCLI(self.clock, "auditor", default=Reply("<points>\nNothing new.\n</points>", after=0))
        self.start()
        self.engine.settings["audit_every"] = 2
        self.chat(MANAGER, "Begin.")
        real = self.engine.store.snapshot
        fake = {"rounds": 0, "finished": False}
        self.engine.store.snapshot = lambda: replace(real(), **fake)

        def wait():
            for _ in range(100):
                self.advance(1)
                if not real().run.audit_running:
                    return
                time.sleep(0.02)

        def audits():
            return len(audit.records(self.path / "run"))
        fake["rounds"] = 1
        self.advance(2)
        self.assertEqual(audits(), 0)
        fake["rounds"] = 2
        self.advance(2)
        wait()
        self.assertEqual(audits(), 1)
        self.assertEqual(audit.records(self.path / "run")[0]["rounds_at"], 2)
        self.advance(3)
        self.assertEqual(audits(), 1)  # nothing new
        fake.update(rounds=3, finished=True)
        self.advance(2)
        wait()
        self.assertEqual(audits(), 2)  # the final one
        self.advance(3)
        self.assertEqual(audits(), 2)
        fake["rounds"] = 4  # a model switch after the end: no audit for it alone
        self.advance(3)
        self.assertEqual(audits(), 2)
        self.restart()  # the engine reads "finished" back from the records
        self.engine.settings["audit_every"] = 2
        real = self.engine.store.snapshot
        self.engine.store.snapshot = lambda: replace(real(), **fake)
        self.advance(3)
        self.assertEqual(audits(), 2)
        fake["rounds"] = 5  # a full stretch after the final audit
        self.advance(2)
        wait()
        self.assertEqual(audits(), 3)

    def test_an_audit_cut_off_by_a_restart_is_cleared(self):
        self.start()
        self.engine.store.configure(run=replace(self.state.run, audit_running=True))
        self.restart()
        self.assertFalse(self.state.run.audit_running)


if __name__ == "__main__":
    unittest.main()
