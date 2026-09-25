import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("settings", ROOT / "settings.py")
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


def executable(path, text="#!/bin/sh\nexit 0\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o700)
    return path


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, value, name="crewview.json"):
        path = self.root / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def test_defaults_when_no_config_exists(self):
        loaded = s.load(environ={}, here=self.root)
        self.assertEqual(loaded.models, s.DEFAULT_MODELS)
        self.assertEqual(loaded.models, {"orchestrator": "gpt-6-astra", "worker": "claude-opus-5-5",
                                         "reviewer": "gpt-6-sol", "helper": "gpt-6-luna"})
        self.assertIsNone(loaded.source)

    def test_example_config_matches_defaults(self):
        self.assertEqual(s.load_file(ROOT / "crewview.example.json").models, s.DEFAULT_MODELS)

    def test_installed_sibling_and_env_override_are_read(self):
        self.write({"models": {"worker": "sonnet"}})
        self.assertEqual(s.load(environ={}, here=self.root).models,
                         {**s.DEFAULT_MODELS, "worker": "sonnet"})  # Partial config keeps other defaults.
        other = self.write({"models": {"reviewer": "gpt-6-custom-review"}}, "other.json")
        loaded = s.load(environ={"CREWVIEW_CONFIG": str(other)}, here=self.root)
        self.assertEqual((loaded.worker, loaded.models["reviewer"]), ("claude-opus-5-5", "gpt-6-custom-review"))

    def test_explicit_missing_config_is_an_error(self):
        with self.assertRaisesRegex(s.SettingsError, "does not exist"):
            s.load(environ={"CREWVIEW_CONFIG": str(self.root / "absent.json")}, here=self.root)

    def test_invalid_configs_are_rejected_clearly(self):
        cases = {
            '{"models": {"worker": "sonnet",}}': "invalid JSON at line 1",
            "[]": "expected a JSON object",
            '{"model": {}}': "unknown key",
            '{"models": []}': "must be an object",
            '{"models": {"planner": "x"}}': "unknown model role",
            '{"models": {"reviewer": 5}}': "models.reviewer",
            '{"models": {"reviewer": "has spaces"}}': "models.reviewer",
            '{"models": {"helper": "' + "x" * 101 + '"}}': "models.helper",
            '{"models": {"worker": "gpt-6-astra"}}': "worker must be a Claude model",
            '{"models": {"worker": "claude-opus-5-5\\n"}}': "models.worker",  # A trailing newline is not a name.
            '{"models": {"reviewer": "gpt-6-sol\\n"}}': "models.reviewer",
        }
        for text, message in cases.items():
            path = self.write(text)
            with self.assertRaisesRegex(s.SettingsError, message, msg=text) as caught:
                s.load(environ={}, here=self.root)
            self.assertIn(str(path), str(caught.exception))  # The error names the file to fix.
        (self.root / "crewview.json").write_bytes(b"\xff\xfe{}")
        with self.assertRaisesRegex(s.SettingsError, "UTF-8"):
            s.load(environ={}, here=self.root)
        (self.root / "crewview.json").write_text(" " * (s.MAX_CONFIG_BYTES + 1))
        with self.assertRaisesRegex(s.SettingsError, "larger than"):
            s.load(environ={}, here=self.root)

    def test_comment_and_claude_worker_forms_are_accepted(self):
        for worker in ("claude-opus-5-5", "claude-sonnet-5", "opus", "sonnet[1m]", "haiku"):
            self.write({"$comment": "notes", "models": {"worker": worker}})
            self.assertEqual(s.load(environ={}, here=self.root).worker, worker)


class LabelTest(unittest.TestCase):
    def test_worker_labels(self):
        cases = {"claude-opus-5-5": ("Opus 5.5", "Opus"), "claude-sonnet-5": ("Sonnet 5", "Sonnet"),
                 "claude-haiku-4-5-20251001": ("Haiku 4.5", "Haiku"), "claude-sonnet-4-20250514": ("Sonnet 4", "Sonnet"),
                 "sonnet": ("Sonnet", "Sonnet"), "opus[1m]": ("Opus", "Opus"), "opusplan": ("Claude", "Claude"),
                 "claude-custom": ("Claude", "Claude"), None: ("Claude", "Claude")}
        for model, (label, family) in cases.items():
            self.assertEqual((s.worker_label(model), s.worker_family(model)), (label, family), model)

    def test_model_matching(self):
        self.assertTrue(s.model_matches("claude-opus-5-5", "claude-opus-5-5"))
        self.assertTrue(s.model_matches("sonnet", "claude-sonnet-5"))
        self.assertTrue(s.model_matches("claude-sonnet-5[1m]", "claude-sonnet-5"))
        self.assertFalse(s.model_matches("claude-opus-5-5", "claude-opus-5-5-preview"))
        self.assertFalse(s.model_matches("sonnet", "claude-opus-5-5"))
        self.assertFalse(s.model_matches("claude-opus-5-5", None))

    def test_role_descriptions(self):
        custom = s.Settings({**s.DEFAULT_MODELS, "reviewer": "gpt-6", "worker": "sonnet"})
        self.assertEqual(custom.describe("orchestrator"), "Astra (gpt-6-astra)")
        self.assertEqual(custom.describe("reviewer"), "a reviewer (gpt-6)")
        self.assertEqual(custom.describe("worker"), "Sonnet (sonnet)")


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_state_and_session_paths(self):
        env = {"HOME": str(self.home)}
        self.assertEqual(s.state_dir(env), self.home / ".local/share/crewview/state")
        self.assertEqual(s.state_dir({**env, "CLAUDE_BRIDGE_STATE": "/x/legacy"}), Path("/x/legacy"))
        self.assertEqual(s.state_dir({**env, "CLAUDE_BRIDGE_STATE": "/x/legacy", "CREWVIEW_STATE": "/x/new"}), Path("/x/new"))
        # An installed copy uses its own state/ (what its MCP server writes), over the legacy variable only.
        install = self.root / "install"
        install.mkdir()
        (install / s.MARKER).write_text("{}")
        self.assertEqual(s.state_dir({**env, "CLAUDE_BRIDGE_STATE": "/x/legacy"}, here=install), install / "state")
        self.assertEqual(s.state_dir({**env, "CREWVIEW_STATE": "/x/new"}, here=install), Path("/x/new"))
        self.assertEqual(s.VERSION, "0.1.0")
        self.assertEqual(s.codex_sessions(env), self.home / ".codex/sessions")
        self.assertEqual(s.codex_sessions({**env, "CODEX_HOME": "/x/codex"}), Path("/x/codex/sessions"))
        self.assertEqual(s.codex_sessions({**env, "CLAUDE_BRIDGE_CODEX_SESSIONS": "/x/s"}), Path("/x/s"))

    def test_claude_discovery(self):
        env = {"HOME": str(self.home), "PATH": str(self.root / "empty")}
        self.assertIsNone(s.find_claude(environ=env))
        fallback = executable(self.home / ".local/bin/claude")
        self.assertEqual(s.find_claude(environ=env), str(fallback))
        on_path = executable(self.root / "bin/claude")
        self.assertEqual(s.find_claude(environ={**env, "PATH": str(on_path.parent)}), str(on_path))
        self.assertEqual(s.find_claude(environ={**env, "CLAUDE_BRIDGE_CLI": "/x/legacy"}), "/x/legacy")
        self.assertEqual(s.find_claude("/x/explicit", environ={**env, "CREWVIEW_CLAUDE_CLI": "/x/env"}), "/x/explicit")

    def test_codex_discovery_falls_back_to_app_bundle(self):
        env = {"HOME": str(self.home), "PATH": str(self.root / "empty")}
        bundled = executable(self.root / "App.app/Contents/Resources/codex")
        with patch.object(s, "APP_CODEX", (self.root / "missing", bundled)):
            self.assertEqual(s.find_codex(environ=env), str(bundled))
            on_path = executable(self.root / "bin/codex")
            self.assertEqual(s.find_codex(environ={**env, "PATH": str(on_path.parent)}), str(on_path))
        with patch.object(s, "APP_CODEX", ()):
            self.assertIsNone(s.find_codex(environ=env))

    def test_auth_requires_subscription_and_strips_api_settings(self):
        record = self.root / "env.json"
        cli = executable(self.root / "claude", f"#!{sys.executable}\nimport json, os, sys\n"
                         f"json.dump(dict(os.environ), open({str(record)!r}, 'w'))\n"
                         "print(json.dumps({'loggedIn': True, 'authMethod': 'apiKey', 'apiProvider': 'firstParty'}))\n")
        auth = s.claude_auth(str(cli), {"PATH": "/usr/bin:/bin", "ANTHROPIC_API_KEY": "secret"})
        self.assertFalse(auth["ready"])
        self.assertNotIn("ANTHROPIC_API_KEY", json.loads(record.read_text()))
        with self.assertRaisesRegex(RuntimeError, "not found"):
            s.claude_auth(str(self.root / "missing"), {})


class ServerConfigTest(unittest.TestCase):
    """The MCP server's instructions follow the configuration and never claim a fixed Opus worker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Path(self.tmp.name) / "crewview.json"

    def tearDown(self):
        self.tmp.cleanup()

    def load_server(self):
        server_spec = importlib.util.spec_from_file_location("configured_server", ROOT / "server.py")
        module = importlib.util.module_from_spec(server_spec)
        with patch.dict(os.environ, {"CREWVIEW_CONFIG": str(self.config), "CREWVIEW_STATE": self.tmp.name}):
            server_spec.loader.exec_module(module)
        return module

    def test_default_instructions(self):
        self.config.write_text(json.dumps({"models": {}}))
        server = self.load_server()
        self.assertEqual(server.MODEL, "claude-opus-5-5")
        self.assertTrue(server.CHILD_INSTRUCTIONS.startswith("You are Opus 5.5, an implementation worker delegated by Astra"))
        self.assertIn("Sol's review", server.CHILD_INSTRUCTIONS)
        self.assertIn("Astra (gpt-6-astra) coordinates", server.INSTRUCTIONS)
        self.assertNotIn("Grant", server.INSTRUCTIONS)

    def test_custom_worker_and_roles(self):
        self.config.write_text(json.dumps({"models": {"worker": "claude-sonnet-5", "orchestrator": "gpt-6",
                                                      "reviewer": "gpt-6-review"}}))
        server = self.load_server()
        self.assertEqual(server.MODEL, "claude-sonnet-5")
        self.assertTrue(server.CHILD_INSTRUCTIONS.startswith("You are Sonnet 5, an implementation worker delegated by the Codex orchestrator"))
        combined = server.INSTRUCTIONS + server.CHILD_INSTRUCTIONS + json.dumps(server.TOOLS)
        self.assertNotIn("Opus", combined)
        self.assertIn("Sonnet 5 (claude-sonnet-5)", combined)
        self.assertIn("does not change the Codex model picker", server.INSTRUCTIONS)

    def test_invalid_config_stops_server_with_clear_error(self):
        self.config.write_text('{"models": {"worker": "gpt-6"}}')
        run = subprocess.run([sys.executable, str(ROOT / "server.py")], input="", text=True, capture_output=True,
                             env={**os.environ, "CREWVIEW_CONFIG": str(self.config)}, timeout=30)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("invalid configuration", run.stderr)
        self.assertIn("worker must be a Claude model", run.stderr)
        self.assertEqual(run.stdout, "")


if __name__ == "__main__":
    unittest.main()
