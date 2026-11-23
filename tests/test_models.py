"""B8: models and reliability (mock backend only; no credentials).

1. a project-level model list (`models:`) that agent settings pick from by id (`model`, `backup`, `backup2`), with
   options on top; the inline backend/backend_options/fallback form keeps working; hot reload sees both files;
2. a failed turn is retried on the same model after 1, 2, 4 min (fake clock), then the backups take over; each
   retry and switch is in the turn record, the events, state and the board; billing problems stop the run;
3. a change of the worker's model is told to the critic once, in one neutral line, through the routing ledger;
4. the stored reply is the final (or cut-off) assistant message, never the raw event stream; sizes only warn;
5. agents' prompts carry no cost accounting.
"""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orch.backends import ClaudeBackend, TurnRequest, _claude_stream_text  # noqa: E402
from orch.config import ConfigError, load_config  # noqa: E402
from orch.controller import MonitorRunner  # noqa: E402
from role_view_checks import assert_no_role_leaks  # noqa: E402
from test_orch import make_cfg, managed, pipeline, run, turns  # noqa: E402

FAKE_ENV = {"ANTHROPIC_AUTH_TOKEN": "fake-token-for-tests", "ANTHROPIC_BASE_URL": "https://glm.example"}
MODELS = {
    "opus": {"backend": "mock", "backend_options": {"model": "opus", "effort": "high"}},
    "glm": {"backend": "mock", "backend_options": {"model": "glm-4.6"}, "env": FAKE_ENV},
    "sonnet": {"backend": "mock", "backend_options": {"model": "sonnet"}},
}


def events(rec, kind):
    return [e for e in (json.loads(x) for x in (rec.run_dir / "events.jsonl").read_text().splitlines())
            if e.get("event") == kind]


class FakeClock:
    """Replaces MonitorRunner.pause: records each wait instead of sleeping."""

    def __init__(self):
        self.waits = []

    def __enter__(self):
        self.real = MonitorRunner.pause
        clock = self
        MonitorRunner.pause = lambda self, secs, marker=None: clock.waits.append(secs)
        return self

    def __exit__(self, *a):
        MonitorRunner.pause = self.real


def with_models(cfg_path: str, models=None, **top) -> str:
    raw = json.loads(Path(cfg_path).read_text())
    raw["models"] = models or MODELS
    raw.update(top)
    Path(cfg_path).write_text(json.dumps(raw))
    return cfg_path


class TestModelList(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def test_agent_picks_models_by_id_and_overrides_options(self):
        m = pipeline()
        m["agents"][0].update(model="opus", backup="glm", backup2="sonnet", backend_options={"effort": "low"},
                              backend="mock")
        a = load_config(with_models(make_cfg(self.tmp, [m]))).monitor("m").agent("w1")
        v = a.variants()
        self.assertEqual([x.label for x in v], ["opus", "glm", "sonnet"])
        self.assertEqual([x.model_id for x in v], ["opus", "glm", "sonnet"])
        self.assertEqual(v[0].options, {"model": "opus", "effort": "low"})  # the agent's option on top
        self.assertEqual(v[1].options, {"model": "glm-4.6", "effort": "low"})  # its own model, the agent's effort
        self.assertEqual(v[1].env, FAKE_ENV)
        self.assertEqual(v[0].env, {})
        self.assertNotEqual(v[1].session_key, v[0].session_key)  # another endpoint
        self.assertNotIn("fake-token-for-tests", json.dumps(a.public()))  # env values never shown

    def test_unknown_id_and_mixed_forms_are_config_errors(self):
        m = pipeline()
        m["agents"][0].update(model="nope")
        with self.assertRaises(ConfigError):
            load_config(with_models(make_cfg(self.tmp, [m])))
        m["agents"][0].update(model="opus", backup="glm", fallback=["x"])
        with self.assertRaises(ConfigError):
            load_config(with_models(make_cfg(self.tmp, [m])))
        m["agents"][0].pop("fallback")
        m["agents"][0].pop("backup")
        m["agents"][0]["backup2"] = "glm"
        with self.assertRaises(ConfigError):
            load_config(with_models(make_cfg(self.tmp, [m])))

    def test_inline_settings_keep_working(self):
        m = pipeline()
        m["agents"][0].update(backend_options={"model": "opus"}, fallback=["sonnet"], retries=2)
        a = load_config(make_cfg(self.tmp, [m])).monitor("m").agent("w1")
        self.assertEqual([x.label for x in a.variants()], ["mock:opus", "mock:sonnet"])
        self.assertEqual(a.retry_waits, [60.0, 120.0, 240.0])
        self.assertEqual(a.fallback_cooldown, 5 * 3600)

    def project(self, settings: str, worker: str) -> Path:
        d = self.tmp / "proj" / ".orch"
        for name, body in (("worker", worker), ("critic", "role: verifier\nbackend: mock\n")):
            (d / "agents" / name).mkdir(parents=True, exist_ok=True)
            (d / "agents" / name / "agent.yaml").write_text(body)
        (d / "settings.yaml").write_text(settings)
        return d.parent

    def test_project_settings_file_and_hot_reload_of_both_files(self):
        settings = ("models:\n  main: {backend: mock, backend_options: {model: m1}}\n"
                    "  spare: {backend: mock, backend_options: {model: s1}, env: {ANTHROPIC_AUTH_TOKEN: fake}}\n"
                    "retry_waits: [5, 10]\nfallback_cooldown: 600\n")
        proj = self.project(settings, "role: worker\ntask: do it\nmodel: main\nbackup: spare\n")
        cfg = load_config(str(proj))
        mon = cfg.monitors[0]
        w = mon.agent("worker")
        self.assertEqual((w.retry_waits, w.fallback_cooldown), ([5.0, 10.0], 600.0))
        self.assertEqual([v.label for v in w.variants()], ["main", "spare"])
        r = MonitorRunner(mon, threading.Event(), log=lambda s: None)
        from test_config_reload import FakeRec
        r.rec = FakeRec()
        r.rec.state = {"sessions": {}, "fwd": {}}
        # the model list changes: the agent's next turn uses the new options
        (proj / ".orch" / "settings.yaml").write_text(settings.replace("model: m1", "model: m2"))
        r.refresh_agent(w)
        self.assertEqual(w.backend_options, {"model": "m2"})
        # the agent file changes: another model id, and another wait schedule
        (proj / ".orch" / "agents" / "worker" / "agent.yaml").write_text(
            "role: worker\ntask: do it\nmodel: spare\nretry_waits: [1]\n")
        r.refresh_agent(w)
        self.assertEqual((w.model_id, w.backend_options, w.retry_waits, w.fallback), ("spare", {"model": "s1"}, [1.0], []))
        self.assertNotIn("fake", " ".join(r.rec.progress_lines))  # env keys only, never values

    def test_unknown_key_in_settings_file_is_an_error(self):
        proj = self.project("modles: {}\n", "role: worker\ntask: x\nbackend: mock\n")
        with self.assertRaises(ConfigError):
            load_config(str(proj))


class TestRetries(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def cfg(self, worker_models, fail, **agent):
        ms = {k: dict(v) for k, v in MODELS.items()}
        for mid, turns_ in fail.items():
            ms[mid] = {**ms[mid], "backend_options": {**ms[mid]["backend_options"], "fail_turns": turns_,
                                                       "error_text": "API Error: 529 overloaded_error"}}
        m = pipeline()
        m["agents"][0].update(**worker_models, **agent)
        m["agents"][1]["backend_options"] = {"pass_after": 3}
        return with_models(make_cfg(self.tmp, [m]), ms)

    def test_same_model_after_1_2_4_minutes_then_backup_then_backup2(self):
        # turn 2 of the session fails on opus at every try (the retries are turns 3-5 of the same session), then glm
        # (its first turn in its own session) fails too, and sonnet answers
        cfg = self.cfg({"model": "opus", "backup": "glm", "backup2": "sonnet"},
                       {"opus": [2, 3, 4, 5], "glm": [1, 2, 3, 4]}, retry_waits=[60, 120, 240])
        with FakeClock() as clock:
            status, rec = run(cfg)
        self.assertEqual(status, "completed")
        self.assertEqual(clock.waits[:7], [60, 120, 240, 60, 120, 240])  # opus x3, then glm x3
        w = [t for t in turns(rec) if t["agent"] == "w1"]
        t2 = w[1]
        self.assertEqual(t2["status"], "ok")
        self.assertEqual([a["model"] for a in t2["attempts"]], ["opus"] * 4 + ["glm"] * 4 + ["sonnet"])
        self.assertEqual([(x["model"], x["retry"], x["wait_s"]) for x in t2["retries"]],
                         [("opus", 1, 60), ("opus", 2, 120), ("opus", 3, 240),
                          ("glm", 1, 60), ("glm", 2, 120), ("glm", 3, 240)])
        self.assertEqual([(f["from"], f["to"]) for f in t2["fallbacks"]], [("opus", "glm"), ("glm", "sonnet")])
        self.assertEqual(len(events(rec, "retry")), 6)
        self.assertEqual([e["backup"] for e in events(rec, "fallback")], [1, 2])
        # state: still on the second backup (5 h cooldown before the primary again)
        self.assertEqual(rec.state["fallback"]["w1"]["label"], "sonnet")
        self.assertEqual(rec.state["reliability"]["w1"]["state"], "backup")
        self.assertEqual(w[2]["model"], "sonnet")
        # the board: a system line for each retry and switch
        from orch.board import Board
        lines = [c for c in Board("m", lambda: rec.run_dir).data().get("messages", []) if c.get("label") in ("retry", "model switch")]
        self.assertEqual(len(lines), 8)

    def test_board_skips_only_the_selected_wait(self):
        from orch.board import Board
        cfg = self.cfg({"model": "opus", "backup": "glm", "backup2": "sonnet"},
                       {"opus": [2], "glm": [1]}, retry_waits=[60, 120])
        previous_ids = []
        pause = MonitorRunner.pause

        def click(runner, secs, marker=None):
            board = Board("m", lambda: runner.rec.run_dir)
            wait = board.data()["reliability"]["w1"]
            wait_id = wait["wait_id"]
            self.assertEqual(wait["next_model"], ["glm", "sonnet"][len(previous_ids)])
            self.assertFalse(board.skip_retry("v1", wait_id))
            for previous in previous_ids:
                self.assertFalse(board.skip_retry("w1", previous))
            self.assertTrue(board.skip_retry("w1", wait_id))
            self.assertFalse(board.skip_retry("w1", wait_id))
            previous_ids.append(wait_id)
            pause(runner, secs, marker)

        with patch.object(MonitorRunner, "pause", click):
            status, rec = run(cfg)
        self.assertEqual(status, "completed")
        turn = next(t for t in turns(rec) if t.get("fallbacks"))
        self.assertEqual([a["model"] for a in turn["attempts"]], ["opus", "glm", "sonnet"])
        self.assertTrue(all("retry skipped from board" in f["reason"] for f in turn["fallbacks"]))
        self.assertFalse(list(rec.run_dir.glob(".retry-*")))

    def test_waits_are_configurable_and_a_success_clears_the_state(self):
        cfg = self.cfg({"model": "opus"}, {"opus": [2]}, retry_waits=[7])
        with FakeClock() as clock:
            status, rec = run(cfg)
        self.assertEqual(status, "completed")
        self.assertEqual(clock.waits, [7])
        self.assertEqual(rec.state.get("reliability"), {})  # nothing waiting, no backup in use

    def test_billing_problem_stops_the_run_without_retry_or_backup(self):
        ms = {k: dict(v) for k, v in MODELS.items()}
        ms["opus"] = {"backend": "mock", "backend_options": {
            "fail_turns": [2], "error_text": "API Error: 400 Your credit balance is too low to access the API."}}
        m = pipeline()
        m["agents"][0].update(model="opus", backup="glm")
        cfg = with_models(make_cfg(self.tmp, [m]), ms)
        with FakeClock() as clock:
            status, rec = run(cfg)
        self.assertEqual(clock.waits, [])
        self.assertEqual(status, "stopped")
        t2 = [t for t in turns(rec) if t["agent"] == "w1"][1]
        self.assertEqual([a["model"] for a in t2["attempts"]], ["opus"])
        self.assertIn("credit balance", t2["billing_error"])
        self.assertIn("billing", rec.state["stop_reason"])
        self.assertEqual(len(events(rec, "billing_error")), 1)

    def test_a_cut_off_reply_that_mentions_billing_is_not_a_billing_problem(self):
        from orch.backends import TurnResult
        from orch.controller import billing_error
        res = TurnResult("error", "I fixed the billing module.", "s", 1, "crashed", [], {"partial": True})
        self.assertEqual(billing_error(res), "")


class TestCriticToldOfModelChange(unittest.TestCase):
    NOTE = "The person you instruct is now on a different model; keep instructions short and explicit."

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def test_switch_to_backup_is_told_once_neutrally(self):
        ms = {"main": {"backend": "mock", "backend_options": {"fail_turns": [2]}},
              "spare": {"backend": "mock", "backend_options": {}}}
        m = managed(worker={"replies": {"work": ["Changed the intro.", "Changed the tables.", "Done."]}},
                    critic={"pass_after": 3})
        m["agents"][2].update(model="main", backup="spare", retry_waits=[])
        cfg = with_models(make_cfg(self.tmp, [m]), ms)
        status, rec = run(cfg)
        self.assertEqual(status, "completed")
        ts = turns(rec)
        switched = next(i for i, t in enumerate(ts) if t["agent"] == "writer" and t.get("fallbacks"))
        after = [t for t in ts[switched:] if t["agent"] == "critic"]
        self.assertIn(self.NOTE, after[0]["input"])
        self.assertEqual(sum(t["input"].count(self.NOTE) for t in ts if t["agent"] == "critic"), 1)
        for t in ts:
            if t["agent"] == "critic":
                self.assertNotIn("spare", t["input"])  # no model names, nothing about why
        rows = [x for x in rec.state["messages"] if x.get("kind") == "model_change"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["to"], rows[0]["old_model"], rows[0]["new_model"]), ("critic", "main", "spare"))
        self.assertEqual(len(events(rec, "model_note")), 1)
        assert_no_role_leaks(self, rec)

    def test_user_edit_of_the_worker_model_is_told_too(self):
        from orch.managed import ManagedRunner
        m = managed()
        m["agents"][2].update(model="opus")
        path = with_models(make_cfg(self.tmp, [m]))
        mon = load_config(path).monitor("m")
        r = ManagedRunner(mon, threading.Event(), log=lambda s: None)
        from orch.recorder import Recorder
        r.rec = Recorder.create(load_config(path).output_dir, "m", mon.snapshot())
        r.mg.update(worker_models={"writer": "opus"})
        raw = json.loads(Path(path).read_text())
        raw["monitors"][0]["agents"][2]["model"] = "sonnet"
        Path(path).write_text(json.dumps(raw))
        r.check_worker_models()
        r.check_worker_models()  # no second note for the same change
        self.assertEqual(r.st["fwd"]["critic"], [self.NOTE])


class TestFinalMessageOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    @staticmethod
    def stream(final=True, cut=False, filler=200000):
        ev = [{"type": "system", "subtype": "init", "session_id": "s-9"},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking at the files."}]}},
              {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * filler}]}},
              {"type": "assistant", "parent_tool_use_id": "t1",
               "message": {"content": [{"type": "text", "text": "subagent chatter"}]}},
              {"type": "assistant", "message": {"content": [{"type": "text", "text": "FINAL: tables updated."}]}}]
        if cut:
            ev += [{"type": "stream_event", "event": {"type": "message_start"}},
                   {"type": "stream_event", "event": {"type": "content_block_delta",
                                                      "delta": {"type": "text_delta", "text": "Now I am half"}}},
                   {"type": "stream_event", "event": {"type": "content_block_delta",
                                                      "delta": {"type": "text_delta", "text": " way through"}}}]
        if final:
            ev.append({"type": "result", "result": "FINAL: tables updated.", "session_id": "s-9", "is_error": False})
        return "\n".join(json.dumps(e) for e in ev) + "\n"

    def test_parse_last_message_and_cut_off_text(self):
        self.assertEqual(_claude_stream_text(self.stream(final=False)), ("FINAL: tables updated.", "s-9"))
        self.assertEqual(_claude_stream_text(self.stream(final=False, cut=True)), ("Now I am half way through", "s-9"))
        self.assertEqual(_claude_stream_text("Error: something broke\n"), ("Error: something broke", None))

    def fake_claude(self, body: str, rc: int) -> str:
        data = self.tmp / "stream.jsonl"
        data.write_text(body)
        exe = self.tmp / "claude"
        exe.write_text(f"#!/bin/sh\ncat > /dev/null\ncat '{data}'\nexit {rc}\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        return str(exe)

    def test_claude_backend_error_stream_is_not_the_reply(self):
        body = self.stream(final=False, cut=True)
        exe = self.fake_claude(body, 1)
        req = TurnRequest("w", "worker", "hi", "", None, str(self.tmp), 20,
                          {"executable": exe}, env={}, on_event=lambda e: None)
        res = ClaudeBackend().run(req, lambda: False)
        self.assertEqual(res.status, "error")
        self.assertEqual(res.output, "Now I am half way through")
        self.assertEqual(res.session_id, "s-9")
        self.assertEqual(res.raw, body)
        ok = ClaudeBackend().run(TurnRequest("w", "worker", "hi", "", None, str(self.tmp), 20,
                                             {"executable": self.fake_claude(self.stream(), 0)}, env={}),
                                 lambda: False)
        self.assertEqual((ok.status, ok.output, ok.raw), ("ok", "FINAL: tables updated.", ""))

    def test_run_stores_the_message_and_keeps_the_stream_apart_with_a_warning(self):
        body = self.stream(final=False, filler=300000)
        m = pipeline()
        m["agents"][0].update(retry_waits=[], backend_options={"fail_turns": [1], "stream_on_fail": body})
        m["controller"]["large_text"] = 100000
        status, rec = run(make_cfg(self.tmp, [m]))
        t1 = [t for t in turns(rec) if t["agent"] == "w1"][0]
        self.assertEqual(t1["output"], "FINAL: tables updated.")
        out_md = next((rec.run_dir / "rounds").glob("r*/t*-w1.output.md")).read_text()
        self.assertLess(len(out_md), 1000)
        raw = rec.run_dir / "rounds" / "r001" / t1["raw_stream"]["file"]
        self.assertEqual(raw.read_text(), body)  # kept whole, apart from the reply
        warn = events(rec, "size_warning")
        self.assertEqual(len(warn), 1)
        self.assertIn("raw event stream", warn[0]["problem"])

    def test_a_large_reply_warns_but_is_never_cut(self):
        big = "word " * 30000
        m = pipeline(max_rounds=1)
        m["agents"][0]["backend_options"] = {"script": [big]}
        m["controller"]["large_text"] = 50000
        status, rec = run(make_cfg(self.tmp, [m]))
        t1 = [t for t in turns(rec) if t["agent"] == "w1"][0]
        self.assertEqual(t1["output"].strip(), big.strip())  # whole: no cap, no truncation
        self.assertTrue(any("large reply" in e["problem"] for e in events(rec, "size_warning")))


class TestNoCostAccounting(unittest.TestCase):
    def test_prompts_do_not_ask_agents_to_account_for_cost(self):
        for p in list((ROOT / "orch" / "prompts").rglob("*.md")):
            text = p.read_text().lower()
            for phrase in ("cost so far", "spent so far", "track the cost", "cost accounting"):
                self.assertNotIn(phrase, text, p.name)
        # spend caps still apply
        self.assertIn("hard cap", (ROOT / "orch" / "prompts" / "protocol" / "worker.md").read_text())


if __name__ == "__main__":
    unittest.main()
