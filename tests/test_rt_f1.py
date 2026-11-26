"""F1 models (docs/redesign.md 2.9): MOD-1…5, 7."""
from rt_helpers import *  # noqa: F401,F403

import os
import tempfile
from pathlib import Path

import yaml

from orch import config
from orch.runtime import models
from orch.runtime.models import module

SECRET = "sk-test-not-a-key"
SETTINGS = {"models": {
    "opus": {"backend": "claude", "backend_options": {"model": "opus"}},
    "glm": {"backend": "claude", "backend_options": {"model": "glm-5"},
            "env": {"ANTHROPIC_BASE_URL": "https://api.example.test/anthropic", "ANTHROPIC_AUTH_TOKEN": SECRET}},
    "gpt": {"backend": "codex", "backend_options": {"model": "gpt-5.5", "effort": "high"}},
    "kimi": {"backend": "opencode", "backend_options": {"model": "moon/kimi-k3#max"}},
    "flash": {"backend": "agy", "backend_options": {"model": "gemini-3.8-flash-low"}},
    "pro": {"backend": "agy", "backend_options": {"model": "gemini-3.8-pro"}},
    "qwen": {"backend": "qwen", "backend_options": {"model": "qwen4-coder"}},
    "qoder": {"backend": "qoder", "backend_options": {"model": "auto"}},
}}

AGENT_YAML = """\
# the worker of the training runs
model: opus   # the main one
backup: glm
backend_options:
  extra_args: [--verbose]   # keep
  effort: high
env:
  HTTP_PROXY: http://proxy.test:3128
  ANTHROPIC_BASE_URL: https://old.example.test
  ANTHROPIC_AUTH_TOKEN: old-not-a-key
system_prompt: worker.md
"""


class TestChoices(unittest.TestCase):
    def y(self):
        return yaml.safe_load(AGENT_YAML)

    def test_MOD_1_main_and_backups_from_the_list(self):
        main, backup = models.choices(SETTINGS, self.y())
        self.assertEqual((main.backend, main.model, main.effort, main.model_id, main.name), ("claude", "opus", "high", "opus", ""))
        self.assertEqual(main.options, {"extra_args": ["--verbose"]})
        self.assertEqual(main.env, {"HTTP_PROXY": "http://proxy.test:3128"})  # no hidden override of the address
        self.assertEqual((backup.name, backup.model), ("glm", "glm-5"))
        self.assertEqual(backup.env["ANTHROPIC_AUTH_TOKEN"], SECRET)
        self.assertNotEqual(main.session_key, backup.session_key)

    def test_MOD_1_keys_are_never_stored_or_shown(self):
        backup = models.choices(SETTINGS, self.y())[1]
        self.assertNotIn(SECRET, repr(to_dict(backup)))
        self.assertNotIn(SECRET, repr(backup.public()))
        self.assertNotIn(SECRET, repr(models.catalog(SETTINGS, {})))

    def test_MOD_1_opencode_provider_model_variant(self):
        c = models.choices(SETTINGS, {"model": "kimi"})[0]
        self.assertEqual((c.provider, c.model, c.effort), ("moon", "kimi-k3", "max"))

    def test_MOD_1_inline_agent_and_its_fallback(self):
        y = {"backend": "claude", "backend_options": {"model": "sonnet"},
             "fallback": {"label": "codex", "backend": "codex", "backend_options": {"model": "gpt-5.5"}}}
        main, fb = models.choices({}, y)
        self.assertEqual((main.model, main.model_id), ("sonnet", ""))
        self.assertEqual((fb.backend, fb.model, fb.name), ("codex", "gpt-5.5", "codex"))

    def test_MOD_1_unknown_model_is_a_config_error(self):
        with self.assertRaises(config.ConfigError):
            models.choices(SETTINGS, {"model": "nope"})

    def test_REC_8_resolve_fills_in_a_stored_choice_and_keeps_an_old_one(self):
        y = self.y()
        stored = from_dict(Choice, to_dict(models.choices(SETTINGS, y)[1]))
        self.assertEqual(stored.env, {"ANTHROPIC_BASE_URL": "https://api.example.test/anthropic"})
        self.assertEqual(models.resolve(stored, SETTINGS, y).env["ANTHROPIC_AUTH_TOKEN"], SECRET)
        gone = Choice("codex", model="gpt-4", model_id="old")  # no longer listed: its own model still
        r = models.resolve(gone, SETTINGS, y)
        self.assertEqual((r.backend, r.model, r.options), ("codex", "gpt-4", {}))


class TestCatalog(unittest.TestCase):
    def test_MOD_2_effort_levels_per_cli(self):
        listed = {"codex": [ModelInfo("gpt-5.5", "codex", model="gpt-5.5", efforts=["low", "high"], default_effort="low")],
                  "opencode": [ModelInfo("moon/kimi-k3", "opencode", "moon", "kimi-k3", efforts=["max", "fast"])]}
        got = {m.id: m for m in models.catalog(SETTINGS, listed)}
        self.assertEqual(got["opus"].efforts, ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual((got["gpt"].efforts, got["gpt"].default_effort), (["low", "high"], "high"))
        self.assertEqual(got["kimi"].efforts, ["max", "fast"])
        self.assertEqual(got["flash"].efforts, [])  # the level is in the id: never both
        self.assertEqual(got["pro"].efforts[0], "low")
        self.assertEqual(got["qwen"].efforts, [])
        self.assertIn("ultracode", got["qoder"].efforts)


def _snap(**kw):
    a = agent("worker-1", choice=models.choices(SETTINGS, {"model": "opus"})[0], **kw)
    a.choices = models.choices(SETTINGS, {"model": "opus", "backup": "glm"})
    a.choice = a.choices[0]
    return snapshot(a, catalog=models.catalog(SETTINGS, {}))


class TestChoose(unittest.TestCase):
    def test_MOD_1_choose_a_listed_model_starts_a_switch(self):
        s = _snap()
        acts = decide(module(SETTINGS), s, event(ChooseModel, s, "worker-1", model_id="gpt", effort=""))
        sw = assert_has(self, acts, StartSwitch).switch
        self.assertEqual((sw.target.model, sw.step, sw.reason, sw.new_session), ("gpt-5.5", SwitchStep.PENDING,
                                                                                SwitchReason.USER, True))
        assert_has(self, acts, CommandReply, ok=True)

    def test_MOD_1_backup_by_name_and_unknown_ones(self):
        s = _snap()
        acts = decide(module(SETTINGS), s, event(ChooseModel, s, "worker-1", backup="glm"))
        self.assertEqual(assert_has(self, acts, StartSwitch).switch.target.name, "glm")
        for kw in ({"backup": "nope"}, {"model_id": "nope"}, {"model_id": "opus", "effort": "huge"}):
            acts = decide(module(SETTINGS), s, event(ChooseModel, s, "worker-1", **kw))
            self.assertEqual(assert_has(self, acts, CommandReply, ok=False).text.values["why"], Refusal.UNKNOWN)

    def test_MOD_6_pending_while_busy_and_refused_once_opening(self):
        s = _snap()
        s.jobs["worker-1/j1"] = job()
        acts = decide(module(SETTINGS), s, event(ChooseModel, s, "worker-1", model_id="gpt"))
        self.assertEqual(assert_has(self, acts, CommandReply).text.key, TextKey.SWITCH_PENDING)
        s.agents["worker-1"].switch = Switch("worker-1", s.agents["worker-1"].choices[1], step=SwitchStep.OPENING)
        acts = decide(module(SETTINGS), s, event(ChooseModel, s, "worker-1", model_id="gpt"))
        self.assertEqual(assert_has(self, acts, CommandReply).text.values["why"], Refusal.SWITCHING)

    def test_MOD_6_force_asks_first(self):
        s = _snap()
        s.jobs["worker-1/j1"] = job()
        acts = decide(module(SETTINGS), s, event(ChooseModel, s, "worker-1", model_id="gpt", force=True))
        r = assert_has(self, acts, CommandReply, ok=False)
        self.assertEqual((r.text.key, r.text.values["jobs"]), (TextKey.ASK_FORCE_SWITCH, ["train.py"]))
        acts = decide(module(SETTINGS), s, event(ChooseModel, s, "worker-1", model_id="gpt", force=True, confirmed=True))
        self.assertTrue(assert_has(self, acts, StartSwitch).switch.forced)

    def test_MOD_6_choosing_the_current_one_ends_a_pending_switch(self):
        s = _snap()
        acts = decide(module(SETTINGS), s, event(ChooseModel, s, "worker-1"))
        self.assertEqual(assert_has(self, acts, CommandReply).text.values["why"], Refusal.ALREADY)
        a = s.agents["worker-1"]
        a.switch = Switch("worker-1", a.choices[1])
        acts = decide(module(SETTINGS), s, event(ChooseModel, s, "worker-1"))
        self.assertTrue(assert_has(self, acts, StartSwitch).switch.target.same(a.choice))

    def test_MOD_3_saved_only_after_the_smoke_test_and_never_a_backup(self):
        s = _snap()
        a = s.agents["worker-1"]
        target = models.choices(SETTINGS, {"model": "gpt"})[0]
        a.switch = Switch("worker-1", target, step=SwitchStep.TESTING)
        ok = event(SmokeResult, s, "worker-1", choice=target, ok=True)
        assert_has(self, decide(module(SETTINGS), s, ok), SaveChoice, choice=target)
        self.assertEqual(decide(module(SETTINGS), s, replace(ok, ok=False)), [])
        a.switch = Switch("worker-1", a.choices[1], step=SwitchStep.TESTING)
        self.assertEqual(decide(module(SETTINGS), s, replace(ok, choice=a.choices[1])), [])


class TestSaveChoice(unittest.TestCase):
    def path(self, text):
        d = Path(tempfile.mkdtemp(dir="/Users/fancy/.claude/jobs/4b93f1df/tmp/f"))
        p = d / "agent.yaml"
        p.write_text(text)
        os.chmod(p, 0o640)
        return p

    def test_MOD_3_replaces_model_and_effort_keeping_comments(self):
        p = self.path(AGENT_YAML)
        models.save_choice(p, Choice("codex", model="gpt-5.5", effort="low", model_id="gpt"))
        text = p.read_text()
        self.assertIn("model: gpt   # the main one", text)
        self.assertIn("# the worker of the training runs", text)
        self.assertIn("extra_args: [--verbose]   # keep", text)
        self.assertIn("effort: low", text)
        self.assertEqual(oct(p.stat().st_mode & 0o777), "0o640")
        y = yaml.safe_load(text)
        self.assertEqual((y["model"], y["backup"], y["system_prompt"]), ("gpt", "glm", "worker.md"))

    def test_MOD_3_inline_agent_gets_a_reference(self):
        p = self.path("backend: claude  # old\nbackend_options:\n  model: sonnet\n  effort: high\ncwd: /work\n")
        models.save_choice(p, Choice("claude", model="opus", model_id="opus"))
        y = yaml.safe_load(p.read_text())
        self.assertEqual(y, {"model": "opus", "cwd": "/work"})

    def test_MOD_3_flow_style_options(self):
        p = self.path("model: opus\nbackend_options: {model: x, effort: high, extra_args: [-v]}\n")
        models.save_choice(p, Choice("claude", model="opus", model_id="glm", effort="max"))
        self.assertEqual(yaml.safe_load(p.read_text()),
                         {"model": "glm", "backend_options": {"effort": "max", "extra_args": ["-v"]}})

    def test_MOD_3_a_choice_without_an_id_is_refused(self):
        p = self.path("model: opus\n")
        with self.assertRaises(config.ConfigError):
            models.save_choice(p, Choice("claude", model="opus"))
        self.assertEqual(p.read_text(), "model: opus\n")


class TestMigrate(unittest.TestCase):
    def test_MOD_4_inline_models_become_the_list(self):
        agents = {"worker-1": {"backend": "claude", "backend_options": {"model": "glm-5", "extra_args": ["-v"]},
                               "env": {"ANTHROPIC_BASE_URL": "https://api.example.test", "ANTHROPIC_AUTH_TOKEN": SECRET,
                                       "HTTP_PROXY": "http://p.test"}},
                  "critic": {"model": "opus"}}
        added, refs = models.list_models(SETTINGS, agents)
        self.assertEqual(list(refs), ["worker-1"])
        mid = refs["worker-1"]["model_id"]
        self.assertEqual(added[mid]["env"]["ANTHROPIC_AUTH_TOKEN"], SECRET)
        text = models.with_choice("# w\nbackend: claude\nbackend_options:\n  model: glm-5\n  extra_args: [-v]\nenv:\n"
                                  f"  ANTHROPIC_BASE_URL: https://api.example.test\n  ANTHROPIC_AUTH_TOKEN: {SECRET}\n"
                                  "  HTTP_PROXY: http://p.test\n", mid, drop_env=refs["worker-1"]["drop_env"])
        self.assertNotIn(SECRET, text)
        self.assertTrue(text.startswith("# w\n"))
        self.assertEqual(yaml.safe_load(text), {"model": mid, "backend_options": {"extra_args": ["-v"]},
                                                "env": {"HTTP_PROXY": "http://p.test"}})
        settings = models.with_models("# settings\nretry_waits: [1, 2]\nmodels:\n  opus:\n    backend: claude\n", added)
        self.assertTrue(settings.startswith("# settings"))
        self.assertIn(mid, yaml.safe_load(settings)["models"])
        y = yaml.safe_load(text)
        self.assertEqual(models.choices({"models": yaml.safe_load(settings)["models"]}, y)[0].env["ANTHROPIC_AUTH_TOKEN"],
                         SECRET)


class TestSmoke(unittest.TestCase):
    def run_smoke(self, choice, reply):
        clock = FakeClock()
        cli = MockCLI(clock, "worker-1").script(reply)
        ad = MockAdapter(clock, cli)
        r = models.smoke_test(ad, AgentSpec("worker-1", choice=choice, cwd="/work"), clock, clock.advance)
        self.assertFalse(cli.alive)  # always stopped
        self.assertEqual(len(cli.sent), 1)  # a real turn
        return r

    def test_MOD_7_a_real_turn_passes(self):
        r = self.run_smoke(Choice("claude", model="opus"), CliReply("OK", model="claude-opus-5-5"))
        self.assertTrue(r.ok, r.why)

    def test_MOD_7_MOD_5_another_reported_model_fails(self):
        r = self.run_smoke(Choice("qwen", model="qwen4-coder"), CliReply("OK", model="qwen3-plus"))
        self.assertEqual((r.ok, r.why), (False, "qwen4-coder -> qwen3-plus"))

    def test_MOD_7_an_error_fails_with_its_class(self):
        r = self.run_smoke(Choice("claude", model="opus"), CliReply(error=ErrorClass.BILLING, detail="pay"))
        self.assertEqual((r.ok, r.error), (False, ErrorClass.BILLING))

    def test_MOD_7_no_answer_times_out(self):
        r = self.run_smoke(Choice("claude", model="opus"), CliReply(ends=False))
        self.assertFalse(r.ok)

    def test_MOD_5_aliases(self):
        same = models.same_model
        self.assertTrue(same(Choice("claude", model="haiku"), "claude-haiku-5-5"))
        self.assertTrue(same(Choice("claude", model="opus[1m]"), "claude-opus-5-5[1m]"))
        self.assertTrue(same(Choice("claude", model="glm-5", env={"ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5"}), "glm-5"))
        self.assertTrue(same(Choice("agy", model="gemini-3.8-pro", effort="high"), "gemini-3.8-pro-high"))
        self.assertTrue(same(Choice("opencode", "moon", "kimi-k3"), "moon/kimi-k3"))
        self.assertTrue(same(Choice("codex", model="gpt-5.5"), "gpt-5.5-2026-01-01"))
        self.assertFalse(same(Choice("claude", model="opus"), "claude-sonnet-5-5"))
        self.assertFalse(same(Choice("qoder", model="ultimate"), "auto"))


if __name__ == "__main__":
    unittest.main()
