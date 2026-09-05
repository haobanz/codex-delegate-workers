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
sys.path.insert(0, str(SCRIPT.parent))
SPEC.loader.exec_module(workers)
sys.path.pop(0)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.config = workers.read_json(workers.DEFAULT_CONFIG)

    def test_default_selection_preserves_main_settings(self):
        result = workers.resolve(self.config)
        self.assertEqual(result["worker_request"], {"model": "gpt-5.6-luna", "reasoning_effort": "medium"})
        self.assertEqual(result["main_session"], "unchanged")
        self.assertEqual(result["execution"], "not_started")

    def test_arbitrary_presets_are_selectable(self):
        self.config["profiles"]["batch"] = {"model": "provider/future-model", "reasoning_effort": "low"}
        self.assertEqual(workers.resolve(self.config, profile="batch")["worker_request"]["model"], "provider/future-model")

    def test_overrides_do_not_mutate_presets(self):
        previous = copy.deepcopy(self.config)
        result = workers.resolve(self.config, model="another-model", effort="high")
        self.assertEqual(result["worker_request"], {"model": "another-model", "reasoning_effort": "high"})
        self.assertEqual(self.config, previous)

    def test_model_override_needs_explicit_effort(self):
        with self.assertRaises(workers.ConfigError):
            workers.resolve(self.config, model="another-model")

    def test_legacy_migration_preserves_user_model_choices(self):
        legacy = {"version": 1, "default_profile": "custom", "max_parallel_workers": 1,
                  "max_attempts_per_task": 2, "profiles": {
                      "custom": {"model": "gpt-5.6-luna", "reasoning_effort": "max", "fallback": "other"},
                      "other": {"model": "gpt-5.6-terra", "reasoning_effort": "xhigh"}}}
        original = copy.deepcopy(legacy)
        converted = workers.validate_config(legacy)
        self.assertEqual(converted, {"version": 2, "default_profile": "custom", "profiles": {
            "custom": {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
            "other": {"model": "gpt-5.6-terra", "reasoning_effort": "xhigh"}}})
        self.assertEqual(legacy, original)

    def test_unknown_fields_and_invalid_models_are_rejected(self):
        cases = [{**self.config, "planner": "x"}, {**self.config, "version": True},
                 {**self.config, "default_profile": "missing"}, {**self.config, "profiles": {}},
                 {**self.config, "max_parallel_workers": 3},
                 {**self.config, "profiles": {"default": {"model": "", "reasoning_effort": "high"}}},
                 {**self.config, "profiles": {"default": {"model": "x", "reasoning_effort": "typo"}}}]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(workers.ConfigError):
                workers.validate_config(value)

    def test_unknown_preset_does_not_silently_fallback(self):
        with self.assertRaises(workers.ConfigError):
            workers.resolve(self.config, profile="missing")

    def test_cli_does_not_read_or_write_main_session_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "config.toml"
            original = b'model = "user-choice"\nmodel_reasoning_effort = "xhigh"\n'
            session.write_bytes(original)
            result = subprocess.run([sys.executable, str(SCRIPT), "resolve"], cwd=root,
                                    env={**os.environ, "CODEX_HOME": str(root)},
                                    capture_output=True, text=True, encoding="utf-8", check=True)
            self.assertEqual(json.loads(result.stdout)["worker_request"]["model"], "gpt-5.6-luna")
            self.assertEqual(session.read_bytes(), original)

    def test_invalid_json_returns_structured_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workers.json"
            for content in ('{"version": 1, "version": 2}', '{invalid', 'null'):
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    result = subprocess.run([sys.executable, str(SCRIPT), "--config", str(path), "show"],
                                            capture_output=True, text=True, encoding="utf-8")
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("error", json.loads(result.stderr))


if __name__ == "__main__":
    unittest.main()
