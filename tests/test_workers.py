import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/delegate-workers/scripts/workers.py"
SPEC = importlib.util.spec_from_file_location("workers", SCRIPT)
workers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workers)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.config = workers.read_json(workers.DEFAULT_CONFIG)

    def test_default_worker_does_not_inherit_main_settings(self):
        result = workers.resolve(self.config)
        self.assertEqual(result["action"], "delegate")
        self.assertEqual(result["worker_request"], {
            "model": "gpt-5.6-luna", "reasoning_effort": "medium"})
        self.assertEqual(result["availability"], "unverified")
        self.assertEqual(result["execution"], "not_started")

    def test_explicit_harder_work_does_not_require_a_failed_attempt(self):
        result = workers.resolve(self.config, profile="complex")
        self.assertEqual(result["worker_request"]["model"], "gpt-5.6-terra")
        self.assertEqual(result["attempt"], 1)

    def test_arbitrary_worker_models_and_profile_names(self):
        self.config["profiles"]["batch"] = {"model": "provider/future-model", "reasoning_effort": "low"}
        self.config["default_profile"] = "batch"
        result = workers.resolve(self.config)
        self.assertEqual(result["worker_request"]["model"], "provider/future-model")

    def test_request_overrides_do_not_mutate_config(self):
        before = copy.deepcopy(self.config)
        result = workers.resolve(self.config, model="another-model", effort="low", max_parallel=2)
        self.assertEqual(result["worker_request"], {"model": "another-model", "reasoning_effort": "low"})
        self.assertEqual(result["limits"]["max_parallel_workers"], 2)
        self.assertEqual(self.config, before)

    def test_model_override_requires_effort(self):
        with self.assertRaises(workers.ConfigError):
            workers.resolve(self.config, model="another-model")

    def test_effort_only_override(self):
        result = workers.resolve(self.config, effort="high")
        self.assertEqual(result["worker_request"]["reasoning_effort"], "high")

    def test_fallback_is_explicit_and_bounded(self):
        result = workers.resolve(self.config, escalate_from="default", attempt=2)
        self.assertEqual(result["action"], "delegate")
        self.assertEqual(result["profile"], "complex")
        result = workers.resolve(self.config, escalate_from="default", attempt=3)
        self.assertEqual(result["reason"], "attempt_limit_reached")
        self.assertNotIn("worker_request", result)

    def test_missing_fallback_returns_to_current_parent(self):
        result = workers.resolve(self.config, escalate_from="complex", attempt=2)
        self.assertEqual(result["action"], "return_to_parent")
        self.assertEqual(result["reason"], "no_fallback_configured")

    def test_fallback_cannot_overwrite_explicit_choices(self):
        for override in ({"model": "new-model", "effort": "low"},
                         {"effort": "high"}, {"profile": "complex"}):
            with self.subTest(override=override), self.assertRaises(workers.ConfigError):
                workers.resolve(self.config, escalate_from="default", attempt=2, **override)

    def test_escalation_needs_previous_attempt(self):
        with self.assertRaises(workers.ConfigError):
            workers.resolve(self.config, escalate_from="default")

    def test_concurrency_and_host_capacity(self):
        for args in ({"active_workers": 3}, {"active_workers": 5},
                     {"available_slots": 0}, {"active_workers": 1, "max_parallel": 1}):
            with self.subTest(args=args):
                self.assertEqual(workers.resolve(self.config, **args)["action"], "wait")
        self.assertEqual(workers.resolve(self.config, active_workers=2, available_slots=1)["action"], "delegate")

    def test_invalid_runtime_counts(self):
        for args in ({"attempt": 0}, {"attempt": True}, {"active_workers": -1},
                     {"available_slots": -1}, {"max_parallel": 0}):
            with self.subTest(args=args), self.assertRaises(workers.ConfigError):
                workers.resolve(self.config, **args)

    def test_unsupported_model_does_not_silently_fallback(self):
        result = workers.resolve(self.config, capabilities={"models": []})
        self.assertEqual(result["action"], "return_to_parent")
        self.assertEqual(result["reason"], "model_unavailable")
        self.assertEqual(result["profile"], "default")

    def test_capability_effort_checks(self):
        capabilities = {"models": [{"model": "gpt-5.6-luna", "reasoning_efforts": ["high"]}]}
        result = workers.resolve(self.config, capabilities=capabilities)
        self.assertEqual(result["reason"], "effort_unavailable")
        result = workers.resolve(self.config, capabilities=capabilities, effort="high")
        self.assertEqual(result["action"], "delegate")
        self.assertEqual(result["availability"], "supported")
        self.assertEqual(result["execution"], "not_started")

    def test_malformed_capabilities(self):
        cases = [None, {"models": {}}, {"models": [{"model": "x", "reasoning_efforts": "high"}]},
                 {"models": [{"model": "x", "reasoning_efforts": []}] * 2}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(workers.ConfigError):
                workers.validate_capabilities(value)

    def test_planner_and_global_config_fields_are_rejected(self):
        for key in ("planner", "controller", "model", "model_reasoning_effort", "sandbox_mode"):
            with self.subTest(key=key), self.assertRaises(workers.ConfigError):
                workers.validate_config({**self.config, key: "unwanted-setting"})

    def test_profile_typos_and_unknown_fallbacks_are_rejected(self):
        for value in ({"model": "x", "reasoning_effort": "medium", "effort": "high"},
                      {"model": "x", "reasoning_effort": "typo"},
                      {"model": "x", "reasoning_effort": "medium", "fallback": "missing"}):
            with self.subTest(value=value), self.assertRaises(workers.ConfigError):
                workers.validate_config({**self.config, "profiles": {"default": value}})

    def test_cycles_are_rejected(self):
        self.config["profiles"]["complex"]["fallback"] = "default"
        with self.assertRaises(workers.ConfigError):
            workers.validate_config(self.config)

    def test_unknown_profile_does_not_default_silently(self):
        with self.assertRaises(workers.ConfigError):
            workers.resolve(self.config, profile="typo")

    def test_malformed_config_types(self):
        cases = [[], {}, {**self.config, "profiles": []}, {**self.config, "profiles": {}},
                 {**self.config, "default_profile": []}, {**self.config, "version": True},
                 {**self.config, "max_attempts_per_task": 0},
                 {**self.config, "max_parallel_workers": True}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(workers.ConfigError):
                workers.validate_config(value)

    def test_cli_works_from_another_cwd_and_preserves_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_dir = root / "codex"
            codex_dir.mkdir()
            session = codex_dir / "config.toml"
            original = b'model = "user-selected-model"\nmodel_reasoning_effort = "xhigh"\n'
            session.write_bytes(original)
            custom = root / "workers.json"
            custom.write_text(json.dumps(self.config), encoding="utf-8")
            config_bytes = custom.read_bytes()
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(custom), "resolve", "--effort", "high"],
                cwd=root, env={**os.environ, "CODEX_HOME": str(codex_dir)},
                capture_output=True, text=True, check=True)
            output = json.loads(result.stdout)
            self.assertEqual(output["worker_request"]["model"], "gpt-5.6-luna")
            self.assertEqual(custom.read_bytes(), config_bytes)
            self.assertEqual(session.read_bytes(), original)
            result = subprocess.run([sys.executable, str(SCRIPT), "resolve"], cwd=root,
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout)["profile"], "default")

    def test_cli_invalid_config_has_nonzero_exit_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workers.json"
            for content in ('{"version": 1, "version": 2}', '{invalid', 'null'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    result = subprocess.run([sys.executable, str(SCRIPT), "--config", str(path), "resolve"],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("error", json.loads(result.stderr))
                    self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
