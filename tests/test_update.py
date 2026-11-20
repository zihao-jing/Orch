"""Taking in a newer orch from origin/main (orch/update.py) and when `orch run` may restart for it (cli.py)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from orch import update  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from unittest.mock import patch  # noqa: E402

from orch import cli  # noqa: E402
from orch.cli import _update_ready  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.up = self.tmp / "upstream"  # where new commits are made and pushed
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.up)], check=True, capture_output=True)
        for r in (self.up,):
            git(r, "config", "user.email", "t@example.com")
            git(r, "config", "user.name", "t")
        self.commit(self.up, "a.py", "1")
        self.commit(self.up, "pyproject.toml", "deps = []")
        self.repo = self.tmp / "orch"  # the checkout orch runs from
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True, capture_output=True)
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        self.ok = self.fake_python(0)
        self.bad = self.fake_python(1)

    def fake_python(self, code):
        p = self.tmp / f"python{code}"
        p.write_text(f"#!/bin/sh\necho validate says {code} >&2\nexit {code}\n")
        p.chmod(0o755)
        return str(p)

    def commit(self, repo, name, text, push=True):
        (repo / name).write_text(text)
        git(repo, "add", name)
        git(repo, "commit", "-q", "-m", f"{name}={text}")
        if push:
            git(repo, "push", "-q", "origin", "HEAD:main")
        return git(repo, "rev-parse", "HEAD")

    def apply(self, python=None):
        return update.apply("cfg", repo=self.repo, python=python or self.ok)

    def test_up_to_date_does_nothing(self):
        res = self.apply()
        self.assertEqual((res["ok"], res["updated"]), (True, False))

    def test_a_newer_main_is_taken_in(self):
        new = self.commit(self.up, "a.py", "2")
        res = self.apply()
        self.assertEqual((res["ok"], res["updated"], res["new"]), (True, True, new[:7]))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), new)

    def test_new_code_that_fails_validate_is_rolled_back(self):
        old = git(self.repo, "rev-parse", "HEAD")
        self.commit(self.up, "a.py", "broken")
        res = self.apply(self.bad)
        self.assertFalse(res["ok"])
        self.assertIn("validate says 1", res["error"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), old)
        self.assertEqual((self.repo / "a.py").read_text(), "1")

    def test_a_dependency_change_is_left_to_me(self):
        old = git(self.repo, "rev-parse", "HEAD")
        self.commit(self.up, "pyproject.toml", "deps = ['x']")
        res = self.apply()
        self.assertFalse(res["ok"])
        self.assertIn("pyproject.toml", res["error"])
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), old)

    def test_not_on_main_dirty_or_ahead_is_left_alone(self):
        self.commit(self.up, "a.py", "2")
        (self.repo / "a.py").write_text("my edit")
        self.assertIn("uncommitted", self.apply()["error"])
        git(self.repo, "checkout", "-q", "--", "a.py")
        git(self.repo, "checkout", "-q", "-b", "mine")
        self.assertIn("not on main", self.apply()["error"])
        git(self.repo, "checkout", "-q", "main")
        self.commit(self.repo, "b.py", "local", push=False)
        self.assertIn("fast-forward", self.apply()["error"])

    def test_restart_keeps_the_arguments_and_resumes(self):
        argv = update.restart_argv(["/bin/orch", "run", ".", "--serve", "--host", "0.0.0.0"])
        self.assertEqual(argv[1:], ["-m", "orch", "run", "--resume", ".", "--serve", "--host", "0.0.0.0"])
        self.assertEqual(update.restart_argv(["orch", "run", "--resume", "."]).count("--resume"), 1)
        moved = update.restart_argv(["orch", "run", ".", "--port", "8899", "--serve"], port=8900)
        self.assertEqual(moved[3:], ["run", "--resume", ".", "--serve", "--port", "8900"])
        self.assertEqual(update.restart_argv(["orch", "run", "--port=8899"], port=8900)[-2:], ["--port", "8900"])


class TestReady(unittest.TestCase):
    def runner(self, status="awaiting_approval", busy=(), auditing=False):
        rec = SimpleNamespace(state={"status": status, **({"auditing": {"lo": 1}} if auditing else {})},
                              agent_busy=lambda name: name in busy)
        return SimpleNamespace(rec=rec, mon=SimpleNamespace(agents=[SimpleNamespace(name="boss"),
                                                                    SimpleNamespace(name="writer")]))

    def test_only_when_every_run_waits_with_nothing_running(self):
        self.assertTrue(_update_ready([self.runner(), self.runner("waiting_job")]))
        self.assertFalse(_update_ready([self.runner(), self.runner("running")]))
        self.assertFalse(_update_ready([self.runner(busy=("boss",))]))  # a board chat at the gate
        self.assertFalse(_update_ready([self.runner(auditing=True)]))
        self.assertFalse(_update_ready([self.runner("completed")]))  # nothing to carry on
        self.assertFalse(_update_ready([SimpleNamespace(rec=None, mon=None)]))  # not started yet
        self.assertFalse(_update_ready([]))


class TestCheckAtEachStop(unittest.TestCase):
    """The watcher fetches once per stop (a gate, a job), not on a timer, and restarts only while nothing runs."""

    def setUp(self):
        self.busy = set()
        self.state = {"status": "running", "managed": {"gate": None}, "round": 1}
        rec = SimpleNamespace(state=self.state, agent_busy=lambda n: n in self.busy, event=lambda *a, **k: None,
                              progress=lambda *a, **k: None)
        self.runners = [SimpleNamespace(rec=rec, mon=SimpleNamespace(agents=[SimpleNamespace(name="boss")]))]
        self.alive = threading.Event()
        self.alive.set()
        self.thread = SimpleNamespace(is_alive=self.alive.is_set)
        self.stop, self.restart, self.applied = threading.Event(), {}, []
        self.heads = ["aaaaaaa1"]

    def watch(self, results):
        def apply(config):
            self.applied.append(dict(self.state, gate=(self.state["managed"]["gate"] or {}).get("key")))
            res = results.pop(0) if results else {"ok": True, "updated": False}
            if res.get("updated"):
                self.heads[0] = "bbbbbbb2"
            return res
        a = SimpleNamespace(config=".")
        for p in (patch.object(cli, "POLL_STOP", 0.01), patch("orch.update.apply", apply),
                  patch("orch.update.head", lambda repo=None: self.heads[0]),
                  patch("orch.update.validate_code", lambda config, repo=None, python=None: "")):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.stop.set)  # the watcher thread ends with the test, before the patches go
        th = threading.Thread(target=cli._auto_update,
                              args=(a, self.runners, [self.thread], self.stop, self.restart, lambda s: None))
        th.start()
        return th

    def at_gate(self, key):
        self.state.update(status="awaiting_approval", managed={"gate": {"key": key}})

    def settle(self):
        time.sleep(0.15)

    def test_one_fetch_per_stop_and_none_while_running(self):
        th = self.watch([])
        self.settle()
        self.assertEqual(self.applied, [])  # running: no fetch
        self.at_gate("plan-0-0")
        self.settle()
        self.settle()
        self.assertEqual(len(self.applied), 1)  # the same stop: once
        self.state.update(status="running")
        self.settle()
        self.at_gate("milestone-1-1")
        self.settle()
        self.assertEqual(len(self.applied), 2)  # the next stop: again
        self.stop.set()
        th.join(2)
        self.assertEqual(self.restart, {})

    def test_a_stop_reached_during_a_chat_is_checked_when_it_ends(self):
        th = self.watch([{"ok": True, "updated": True, "old": "aaaaaaa", "new": "bbbbbbb"}])
        self.busy.add("boss")
        self.at_gate("plan-0-0")
        self.settle()
        self.assertEqual(self.applied, [])
        self.busy.clear()
        th.join(2)
        self.assertEqual(len(self.applied), 1)
        self.assertEqual(self.restart, {"old": "aaaaaaa", "new": "bbbbbbb"})
        self.assertTrue(self.stop.is_set())

    def test_new_code_taken_in_just_before_a_chat_restarts_after_it(self):
        th = self.watch([{"ok": True, "updated": True, "old": "aaaaaaa", "new": "bbbbbbb"}])
        real = cli._update_ready
        calls = []

        def ready(runners):  # quiet for the stop check, then a chat starts before the restart, then it ends
            calls.append(1)
            return real(runners) and len(calls) != 2
        with patch.object(cli, "_update_ready", ready):
            self.at_gate("plan-0-0")
            th.join(2)
        self.assertEqual(len(self.applied), 1)  # no second fetch for the same stop
        self.assertEqual(self.restart["new"], "bbbbbbb")


if __name__ == "__main__":
    unittest.main()
