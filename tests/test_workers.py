import copy
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
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
        self.assertEqual(result["compatibility"]["status"], "compatible")
        self.assertFalse(result["compatibility"]["runtime_verified"])
        self.assertIn("2026-09-09", result["compatibility"]["source"])
        self.assertEqual(result["main_session"], "unchanged")
        self.assertEqual(result["execution"], "not_started")

    def test_arbitrary_presets_are_selectable(self):
        self.config["profiles"]["batch"] = {"model": "provider/future-model", "reasoning_effort": "low"}
        result = workers.resolve(self.config, profile="batch")
        self.assertEqual(result["worker_request"]["model"], "provider/future-model")
        self.assertEqual(result["compatibility"]["status"], "unverified")
        self.assertIn("provider/future-model", result["compatibility"]["warning"])

    def test_overrides_do_not_mutate_presets(self):
        previous = copy.deepcopy(self.config)
        result = workers.resolve(self.config, model="another-model", effort="high")
        self.assertEqual(result["worker_request"], {"model": "another-model", "reasoning_effort": "high"})
        self.assertEqual(self.config, previous)

    def test_model_override_needs_explicit_effort(self):
        with self.assertRaises(workers.ConfigError):
            workers.resolve(self.config, model="another-model")

    def test_luna_ultra_is_rejected_but_max_is_accepted(self):
        with self.assertRaises(workers.ConfigError):
            workers.resolve(self.config, model="gpt-5.6-luna", effort="ultra")
        result = workers.resolve(self.config, model="gpt-5.6-luna", effort="max")
        self.assertEqual(result["compatibility"]["status"], "compatible")

    def test_every_snapshot_combination_is_checked_without_downgrade(self):
        snapshot = workers.validate_capabilities(workers.read_json(workers.DEFAULT_CAPABILITIES))
        for model, supported in snapshot["models"].items():
            for effort in workers.EFFORTS:
                worker = {"model": model, "reasoning_effort": effort}
                if effort in supported:
                    self.assertEqual(workers.validate_worker(worker)["status"], "compatible")
                else:
                    with self.subTest(model=model, effort=effort), self.assertRaises(workers.ConfigError):
                        workers.validate_worker(worker)

    def test_external_capabilities_are_complete_and_override_snapshot(self):
        catalog = {"version": 1, "source": "test catalog 2026-09-09",
                   "models": {"gpt-5.6-luna": ["ultra"]}}
        result = workers.resolve(self.config, model="gpt-5.6-luna", effort="ultra",
                                 capabilities=catalog)
        self.assertEqual(result["compatibility"]["source"], "test catalog 2026-09-09")
        with self.assertRaises(workers.ConfigError):
            workers.resolve(self.config, model="gpt-5.6-luna", effort="max", capabilities=catalog)
        with self.assertRaises(workers.ConfigError):
            workers.resolve(self.config, model="gpt-5.5", effort="high", capabilities=catalog)

    def test_cli_validate_loads_explicit_capabilities_once_for_all_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capabilities.json"
            path.write_text(json.dumps({
                "version": 1,
                "source": "one read catalog",
                "models": {
                    "gpt-5.6-luna": ["medium", "max"],
                    "gpt-5.6-terra": ["high"],
                },
            }), encoding="utf-8")
            with patch.object(workers, "_load_capabilities", wraps=workers._load_capabilities) as load, \
                    patch.object(workers, "validate_capabilities", wraps=workers.validate_capabilities) as validate, \
                    contextlib.redirect_stdout(io.StringIO()) as stdout, \
                    contextlib.redirect_stderr(io.StringIO()):
                code = workers.main(["--capabilities", str(path), "validate"])
            self.assertEqual(code, 0)
            load.assert_called_once_with(path)
            validate.assert_called_once()
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["compatibility"]["default"]["source"], "one read catalog")

    def test_nonselected_incompatible_profile_does_not_block_resolve(self):
        config = copy.deepcopy(self.config)
        config["profiles"]["broken"] = {"model": "gpt-5.6-luna", "reasoning_effort": "ultra"}
        result = workers.resolve(config, profile="complex")
        self.assertEqual(result["profile"], "complex")
        self.assertEqual(result["compatibility"]["status"], "compatible")

    def test_final_effort_override_repairs_selected_incompatible_profile(self):
        config = copy.deepcopy(self.config)
        config["profiles"]["default"]["reasoning_effort"] = "ultra"
        original = copy.deepcopy(config)
        result = workers.resolve(config, effort="max")
        self.assertEqual(result["worker_request"], {"model": "gpt-5.6-luna", "reasoning_effort": "max"})
        self.assertEqual(result["compatibility"]["status"], "compatible")
        self.assertEqual(config, original)

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

    def test_show_reads_incompatible_config_but_validate_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "workers.json"
            config = {"version": 2, "default_profile": "default", "profiles": {
                "default": {"model": "gpt-5.6-luna", "reasoning_effort": "ultra"}}}
            original = json.dumps(config, ensure_ascii=False).encode()
            config_path.write_bytes(original)
            show = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config_path), "show"],
                                  capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(show.returncode, 0)
            self.assertEqual(json.loads(show.stdout), config)
            validate = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config_path), "validate"],
                                      capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(validate.returncode, 2)
            self.assertIn("error", json.loads(validate.stderr))
            self.assertEqual(config_path.read_bytes(), original)

    def test_codex_config_is_read_only_and_keeps_toml_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "workers.json"
            config_path.write_text(json.dumps(self.config), encoding="utf-8")
            config_original = config_path.read_bytes()
            session = root / "config.toml"
            original = b'model = "user-choice"\nmodel_reasoning_effort = "xhigh"\n'
            session.write_bytes(original)
            result = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config_path),
                                     "codex-config", "--profile", "complex", "--effort", "xhigh"],
                                    cwd=root, env={**os.environ, "CODEX_HOME": str(root)},
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout,
                             '[agents]\n'
                             'default_subagent_model = "gpt-5.6-terra"\n'
                             'default_subagent_reasoning_effort = "xhigh"\n')
            self.assertNotRegex(result.stdout, r"(?m)^model\s*=")
            self.assertEqual(config_path.read_bytes(), config_original)
            self.assertEqual(session.read_bytes(), original)

    def test_codex_config_unknown_model_warns_on_stderr_only(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "codex-config", "--model", "provider/future-model", "--effort", "low"],
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout,
                         '[agents]\n'
                         'default_subagent_model = "provider/future-model"\n'
                         'default_subagent_reasoning_effort = "low"\n')
        self.assertIn("警告", result.stderr)
        self.assertIn("provider/future-model", result.stderr)
        self.assertNotIn("警告", result.stdout)

    def test_explicit_capabilities_cli_overrides_snapshot_and_rejects_missing_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "workers.json"
            config_path.write_text(json.dumps(self.config), encoding="utf-8")
            catalog_path = root / "capabilities.json"
            catalog_path.write_text(json.dumps({
                "version": 1,
                "source": "CLI catalog",
                "models": {"gpt-5.6-luna": ["ultra"]},
            }), encoding="utf-8")
            override = subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(config_path),
                 "--capabilities", str(catalog_path), "codex-config",
                 "--model", "gpt-5.6-luna", "--effort", "ultra"],
                capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(override.returncode, 0)
            self.assertIn('default_subagent_reasoning_effort = "ultra"', override.stdout)
            self.assertNotIn("error", override.stderr)
            missing = subprocess.run(
                [sys.executable, str(SCRIPT), "--config", str(config_path),
                 "--capabilities", str(catalog_path), "resolve", "--profile", "complex"],
                capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(missing.returncode, 2)
            self.assertEqual(missing.stdout, "")
            self.assertIn("error", json.loads(missing.stderr))

    def test_capabilities_cli_errors_are_structured(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capabilities.json"
            path.write_text('{"version": 1, "source": "bad", "models": {}}', encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPT), "--capabilities", str(path), "validate"],
                                    capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stderr))


if __name__ == "__main__":
    unittest.main()
