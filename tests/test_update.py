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


if __name__ == "__main__":
    unittest.main()
