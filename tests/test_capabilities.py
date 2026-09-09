import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "skills/delegate-workers/scripts/capabilities.py"
SPEC = importlib.util.spec_from_file_location("capabilities_under_test", MODULE)
capabilities = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capabilities)


class CapabilityTests(unittest.TestCase):
    def test_builtin_snapshot_is_exact_and_dated(self):
        value = capabilities.load_builtin_capabilities()
        self.assertIn("2026-09-09", value["source"])
        self.assertEqual(value["models"], {
            "gpt-6-astra": ["low", "medium", "high", "xhigh", "max", "ultra"],
            "gpt-5.6-sol": ["low", "medium", "high", "xhigh", "max", "ultra"],
            "gpt-5.6-terra": ["low", "medium", "high", "xhigh", "max", "ultra"],
            "gpt-5.6-luna": ["low", "medium", "high", "xhigh", "max"],
            "gpt-5.5": ["low", "medium", "high", "xhigh"],
        })

    def test_validation_returns_a_copy_and_rejects_invalid_shapes(self):
        source = {"version": 1, "source": "caller catalog", "models": {"custom/model": ["low", "max"]}}
        normalized = capabilities.validate_capabilities(source)
        self.assertEqual(normalized, source)
        self.assertIsNot(normalized, source)
        self.assertIsNot(normalized["models"], source["models"])
        self.assertIsNot(normalized["models"]["custom/model"], source["models"]["custom/model"])
        invalid = [
            [],
            {"version": True, "source": "x", "models": {"m": ["low"]}},
            {"version": 2, "source": "x", "models": {"m": ["low"]}},
            {"version": 1, "source": "", "models": {"m": ["low"]}},
            {"version": 1, "source": "   ", "models": {"m": ["low"]}},
            {"version": 1, "source": "x", "models": {}},
            {"version": 1, "source": "x", "models": {"m": "low"}},
            {"version": 1, "source": "x", "models": {"m": []}},
            {"version": 1, "source": "x", "models": {"m": ["low", "low"]}},
            {"version": 1, "source": "x", "models": {"m": [True]}},
            {"version": 1, "source": "x", "models": {"m": ["unsupported"]}},
            {"version": 1, "source": "x", "models": {"bad model": ["low"]}},
            {"version": 1, "source": "x", "models": {"m": ["low"]}, "extra": True},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(capabilities.ConfigError):
                capabilities.validate_capabilities(value)

    def test_duplicate_json_fields_are_rejected_at_read_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capabilities.json"
            for content in (
                '{"version": 1, "version": 1, "source": "x", "models": {"m": ["low"]}}',
                '{"version": 1, "source": "x", "models": {"m": ["low"], "m": ["max"]}}',
                '{"version": 1, "source": "x", "models": {"m": ["low", "low"]}}',
            ):
                path.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(capabilities.ConfigError):
                    capabilities.validate_capabilities(capabilities.read_json(path))


if __name__ == "__main__":
    unittest.main()
