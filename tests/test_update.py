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


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()



class TestSafePoint(unittest.TestCase):
    def test_an_ended_run_is_a_safe_point(self):
        """A completed run's board said "The run is busy" and never checked for an update."""
        from orch import update
        for st in ("completed", "failed", "max_rounds_reached", "needs_human", "stopped", "awaiting_approval"):
            self.assertTrue(update.safe_point(st), st)
        for st in ("running", "starting"):
            self.assertFalse(update.safe_point(st), st)

class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
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

    def check(self):
        with patch.object(update, "REPO", self.repo):
            return update.check("completed")

    def test_check_tells_local_commits_and_offers_only_a_fast_forward(self):
        """Local commits not pushed showed as "up to date"; with main moved on too, the board offered an update that
        apply could only refuse."""
        j = self.check()
        self.assertEqual((j["behind"], j["ahead"], j["update"]), (0, 0, False))
        self.commit(self.repo, "b.py", "local", push=False)
        j = self.check()
        self.assertEqual((j["behind"], j["ahead"], j["update"]), (0, 1, False))
        self.commit(self.up, "a.py", "2")
        j = self.check()
        self.assertEqual((j["behind"], j["ahead"], j["update"]), (1, 1, False))  # diverged: no Update button
        git(self.repo, "reset", "-q", "--hard", "HEAD~1")
        j = self.check()
        self.assertEqual((j["behind"], j["ahead"], j["update"]), (1, 0, True))

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




class TestUpdateButton(unittest.TestCase):
    """The dashboard's Update button (UpdateOrch): Orch takes in the new code and restarts with --resume at once; the
    agents keep running meanwhile, so there is no waiting for a quiet moment."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.engine = SimpleNamespace(run_dir=self.tmp, update_requested=True)
        self.heads = ["aaaaaaa1"]
        self.applied = 0
        p = patch.object(cli, "_STARTED_HEAD", "aaaaaaa1")
        p.start()
        self.addCleanup(p.stop)

    def take(self, result, valid=""):
        def apply(config):
            self.applied += 1
            if result.get("updated"):
                self.heads[0] = "bbbbbbb2"
            return result
        with patch("orch.update.apply", apply), patch("orch.update.head", lambda repo=None: self.heads[0]), \
                patch("orch.update.validate_code", lambda config, repo=None, python=None: valid), \
                patch.object(cli.sys, "argv", ["orch", "run", "proj", "--serve"]):
            return cli._take_update(SimpleNamespace(config="."), [self.engine], 8766)

    def test_the_button_updates_and_restarts_with_resume_on_the_same_port(self):
        argv, new = self.take({"ok": True, "updated": True, "old": "aaaaaaa", "new": "bbbbbbb"})
        self.assertEqual((new, argv[-6:]), ("bbbbbbb", ["run", "--resume", "proj", "--serve", "--port", "8766"]))
        self.assertEqual(update.read_status(self.tmp).get("state"), "restarting")
        self.assertFalse(self.engine.update_requested)  # asked once, done once

    def test_a_failed_update_is_shown_and_nothing_restarts(self):
        self.assertIsNone(self.take({"ok": False, "updated": False, "error": "the orch checkout is not on main"}))
        self.assertEqual(update.read_status(self.tmp).get("state"), "failed")

    def test_already_current_and_code_pulled_by_hand_that_fails_validate(self):
        self.assertIsNone(self.take({"ok": True, "updated": False}))
        self.assertEqual(update.read_status(self.tmp).get("state"), "current")
        self.engine.update_requested = True
        self.heads[0] = "ccccccc3"  # pulled by hand since this process started
        self.assertIsNone(self.take({"ok": True, "updated": False}, valid="bad config"))
        self.assertEqual(update.read_status(self.tmp).get("state"), "failed")

    def test_the_engine_flags_the_request_from_the_dashboard(self):
        from orch.runtime import contracts as C
        from rt_helpers import snapshot
        from orch.runtime.adapters.mock import FakeClock
        from orch.runtime.engine import Engine
        eng = Engine(self.tmp / "run", FakeClock(1000.0), {}, {"tick_s": 1})
        self.addCleanup(eng.close)
        eng.start(snapshot())
        reply = eng.submit(C.UpdateOrch("", 1000.0, "u1"))
        self.assertTrue(reply.get("ok", True))
        self.assertTrue(eng.update_requested)

