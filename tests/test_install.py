"""Installer and uninstaller tests with fake Codex/Claude CLIs, an isolated HOME and CODEX_HOME."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load("install")
uninstall = load("uninstall")
settings = install.settings  # The same module object the installer uses.

# Mirrors `codex mcp get/add/remove` as observed on codex-cli 0.156: get --json prints the
# transport, a missing name exits 1 with "No MCP server named", add silently replaces.
FAKE_CODEX = f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
home = Path(os.environ["CODEX_HOME"])
store = home / "fake-mcp.json"
servers = json.loads(store.read_text()) if store.exists() else {{}}
with open(home / "calls.log", "a") as log:
    log.write(" ".join(sys.argv[1:3]) + "\\n")
args = sys.argv[1:]
if os.environ.get("FAKE_CODEX_TOUCH") and args[1:2] in (["add"], ["remove"]):
    # Simulates Codex (or a concurrent edit) changing an unrelated setting during our write.
    config = home / "config.toml"
    config.write_text(config.read_text().replace('model = "gpt-6-astra"', 'model = "gpt-6-other"'))
if args[:2] == ["mcp", "get"]:
    name = args[2]
    if name not in servers:
        print(f"Error: No MCP server named '{{name}}' found.", file=sys.stderr)
        sys.exit(1)
    entry = servers[name]
    print(json.dumps({{"name": name, "enabled": True, "disabled_reason": None, "transport": {{"type": "stdio",
        "command": entry["command"], "args": entry["args"], "env": entry["env"] or None, "env_vars": [], "cwd": None}},
        "enabled_tools": None, "disabled_tools": None, "startup_timeout_sec": None, "tool_timeout_sec": None,
        **entry.get("options", {{}})}}))
elif args[:2] == ["mcp", "add"]:
    if os.environ.get("FAKE_CODEX_FAIL_ADD"):
        print("Error: simulated failure", file=sys.stderr)
        sys.exit(1)
    rest = args[3:]
    split = rest.index("--")
    env = dict(v.split("=", 1) for k, v in zip(rest[:split:2], rest[1:split:2]) if k == "--env")
    servers[args[2]] = {{"command": rest[split + 1], "args": rest[split + 2:], "env": env}}
    store.write_text(json.dumps(servers))
    print(f"Added global MCP server '{{args[2]}}'.")
elif args[:2] == ["mcp", "remove"]:
    print(f"Removed global MCP server '{{args[2]}}'." if servers.pop(args[2], None) else f"No MCP server named '{{args[2]}}' found.")
    store.write_text(json.dumps(servers))
else:
    sys.exit(2)
'''

FAKE_CLAUDE = f'''#!{sys.executable}
import json, sys
from pathlib import Path
if sys.argv[1:] == ["--restricted", "auth", "status"]:
    print(Path(__file__).with_name("auth.json").read_text())
    sys.exit()
sys.exit("the installer must never run a model")
'''

READY = {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max"}


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.codex_home = self.home / ".codex"
        self.codex_home.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.codex = self.write_exec(self.bin / "codex", FAKE_CODEX)
        self.claude = self.write_exec(self.bin / "claude", FAKE_CLAUDE)
        (self.bin / "auth.json").write_text(json.dumps(READY))
        self.config_toml = self.codex_home / "config.toml"
        self.config_toml.write_text('model = "gpt-6-astra"\n\n[mcp_servers.claude-bridge]\ncommand = "/old/python"\n')
        self.seed({"claude-bridge": {"command": "/old/python", "args": ["/old/bridge/server.py"], "env": {}},
                   "other": {"command": "/bin/echo", "args": ["hi"], "env": {"A": "1"}}})
        self.env = {"HOME": str(self.home), "CODEX_HOME": str(self.codex_home), "PATH": f"{self.bin}:/usr/bin:/bin"}
        self.dest = self.home / ".local/share/crewview"
        # Never reach the real Codex app bundle from tests.
        self.no_bundle = patch.object(settings, "APP_CODEX", ())
        self.no_bundle.start()

    def tearDown(self):
        self.no_bundle.stop()
        self.tmp.cleanup()

    def write_exec(self, path, text):
        path.write_text(text)
        path.chmod(0o700)
        return path

    def seed(self, servers):
        (self.codex_home / "fake-mcp.json").write_text(json.dumps(servers))

    def servers(self):
        return json.loads((self.codex_home / "fake-mcp.json").read_text())

    def calls(self):
        log = self.codex_home / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def run_install(self, *argv, env=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = install.main(list(argv), environ=env or self.env, system=kw.get("system", "Darwin"),
                                version=kw.get("version", (3, 12, 1)), python=kw.get("python", sys.executable))
        return code, out.getvalue() + err.getvalue()

    def run_uninstall(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = uninstall.main(list(argv), environ=self.env)
        return code, out.getvalue() + err.getvalue()

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.home.rglob("*")
                if p.is_file() and p.name != "calls.log"}

    # ---------- read-only modes ----------

    def test_check_and_dry_run_write_nothing(self):
        before = self.snapshot()
        code, out = self.run_install("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("Claude subscription login (claude.ai, max)", out)
        self.assertIn("'claude-bridge' is also registered and stays unchanged", out)
        code, out = self.run_install("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("Register Codex MCP server 'crewview'", out)
        self.assertIn("Dry run only. Nothing was changed.", out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.dest.exists())
        self.assertTrue(all(call.startswith("mcp get") for call in self.calls()), self.calls())

    def test_missing_claude_is_reported_before_any_write(self):
        self.claude.unlink()
        code, out = self.run_install()
        self.assertEqual(code, 1)
        self.assertIn("Claude Code CLI not found", out)
        self.assertIn("Nothing was changed", out)
        self.assertFalse(self.dest.exists())
        self.assertNotIn("mcp add", self.calls())

    def test_missing_codex_and_explicit_bad_paths_fail(self):
        self.codex.unlink()
        code, out = self.run_install()
        self.assertEqual((code, "Codex CLI not found" in out), (1, True), out)
        code, out = self.run_install("--codex", str(self.root / "nope"), "--claude", str(self.root / "nada"))
        self.assertEqual(code, 1)
        self.assertIn("nope is not an executable file", out)
        self.assertIn("nada is not an executable file", out)
        self.assertFalse(self.dest.exists())

    def test_api_key_or_logged_out_claude_blocks_install(self):
        (self.bin / "auth.json").write_text(json.dumps({**READY, "authMethod": "apiKey"}))
        code, out = self.run_install()
        self.assertEqual(code, 1)
        self.assertIn("claude auth login --claudeai", out)
        self.assertIn("never uses API keys", out)
        self.assertFalse(self.dest.exists())
        (self.bin / "auth.json").write_text("not json")
        code, out = self.run_install()
        self.assertEqual(code, 1)
        self.assertIn("authentication check failed", out)

    def test_skip_auth_check_installs_without_login(self):
        (self.bin / "auth.json").write_text(json.dumps({"loggedIn": False}))
        code, out = self.run_install("--skip-auth-check")
        self.assertEqual(code, 0, out)
        self.assertIn("not checked (--skip-auth-check)", out)
        self.assertIn("crewview", self.servers())

    def test_python_version_and_platform_are_enforced(self):
        code, out = self.run_install("--check", version=(3, 10, 12))
        self.assertEqual(code, 1)
        self.assertIn("needs Python 3.11 or newer", out)
        code, out = self.run_install("--check", system="Linux")
        self.assertEqual(code, 1)
        self.assertIn("macOS only", out)

    # ---------- install ----------

    def test_full_install_copies_allowlisted_files_and_registers(self):
        before_toml = self.config_toml.read_bytes()
        code, out = self.run_install()
        self.assertEqual(code, 0, out)
        for name in ("server.py", "settings.py", "dashboard.py", "activity.py", "codex_history.py", "demo.py",
                     "uninstall.py", "web/index.html", "web/app.js", "web/app.css", "crewview.example.json",
                     settings.MARKER):
            self.assertTrue((self.dest / name).is_file(), name)
        copied = [str(p.relative_to(self.dest)) for p in self.dest.rglob("*")]
        self.assertFalse([p for p in copied if "__pycache__" in p or p.startswith("tests") or "install.py" == p], copied)
        self.assertEqual((self.dest / "crewview.json").read_bytes(), (ROOT / "crewview.example.json").read_bytes())
        self.assertEqual((self.dest / "state").stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.dest / "crewview.json").stat().st_mode & 0o777, 0o600)
        launcher = (self.dest / "launch-dashboard.command").read_text()
        self.assertIn(f"CREWVIEW_PYTHON={sys.executable}", launcher)
        self.assertTrue(os.access(self.dest / "launch-dashboard.command", os.X_OK))
        servers = self.servers()
        self.assertEqual(servers["crewview"], {"command": sys.executable, "args": [str(self.dest / "server.py")],
                                               "env": {"CREWVIEW_STATE": str(self.dest / "state"),
                                                       "CREWVIEW_CONFIG": str(self.dest / "crewview.json"),
                                                       "CREWVIEW_CLAUDE_CLI": str(self.claude)}})
        # Unrelated entries and the legacy bridge are untouched; config.toml is never rewritten by us.
        self.assertEqual(servers["claude-bridge"]["args"], ["/old/bridge/server.py"])
        self.assertEqual(servers["other"]["env"], {"A": "1"})
        self.assertEqual(self.config_toml.read_bytes(), before_toml)
        backups = list((self.dest / "backups").glob("*/codex-config.toml"))
        self.assertEqual([b.read_bytes() for b in backups], [before_toml])
        self.assertIn("Restart Codex", out)

    def test_installed_server_uses_installed_config(self):
        self.assertEqual(self.run_install()[0], 0)
        (self.dest / "crewview.json").write_text(json.dumps({"models": {"worker": "sonnet"}}))
        entry = self.servers()["crewview"]
        requests = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        run = subprocess.run([entry["command"], *entry["args"]], input=requests, text=True, capture_output=True,
                             env={**self.env, **entry["env"]}, timeout=30)
        instructions = json.loads(run.stdout)["result"]["instructions"]
        self.assertIn("Sonnet (sonnet)", instructions)
        self.assertEqual(json.loads(run.stdout)["result"]["serverInfo"]["name"], "crewview")

    def test_rerun_preserves_user_models_and_registration(self):
        self.assertEqual(self.run_install()[0], 0)
        custom = json.dumps({"models": {"worker": "claude-sonnet-5", "reviewer": "gpt-6-sol"}})
        (self.dest / "crewview.json").write_text(custom)
        (self.dest / "state" / "keep.txt").write_text("history")
        code, out = self.run_install()
        self.assertEqual(code, 0, out)
        self.assertIn("keeping", out)
        self.assertIn("already registered as planned; unchanged", out)
        self.assertEqual((self.dest / "crewview.json").read_text(), custom)
        self.assertEqual((self.dest / "state" / "keep.txt").read_text(), "history")
        self.assertEqual(self.calls().count("mcp add"), 1)

    def test_update_backs_up_changed_files_only(self):
        self.assertEqual(self.run_install()[0], 0)
        (self.dest / "server.py").write_text("# locally modified\n")
        self.assertEqual(self.run_install()[0], 0)
        saved = [p for p in (self.dest / "backups").rglob("*") if p.is_file() and p.name != "codex-config.toml"]
        self.assertEqual([p.name for p in saved], ["server.py"])
        self.assertEqual(saved[0].read_text(), "# locally modified\n")
        self.assertEqual((self.dest / "server.py").read_bytes(), (ROOT / "server.py").read_bytes())

    def test_explicit_config_replaces_after_backup(self):
        self.assertEqual(self.run_install()[0], 0)
        custom = self.root / "mine.json"
        custom.write_text(json.dumps({"models": {"worker": "opus", "helper": "gpt-6-mini"}}))
        code, out = self.run_install("--config", str(custom))
        self.assertEqual(code, 0, out)
        self.assertEqual((self.dest / "crewview.json").read_text(), custom.read_text())
        backups = list((self.dest / "backups").glob("*/crewview.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), (ROOT / "crewview.example.json").read_bytes())

    def test_invalid_config_is_refused_without_writes(self):
        bad = self.root / "bad.json"
        bad.write_text('{"models": {"worker": "gpt-6"}}')
        code, out = self.run_install("--config", str(bad))
        self.assertEqual(code, 1)
        self.assertIn("worker must be a Claude model", out)
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.run_install()[0], 0)
        (self.dest / "crewview.json").write_text("{broken")
        code, out = self.run_install()
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", out)
        self.assertIn("Fix it or pass --config PATH", out)
        self.assertEqual((self.dest / "crewview.json").read_text(), "{broken")

    def test_foreign_registration_is_refused(self):
        self.seed({**self.servers(), "crewview": {"command": "/usr/bin/python3", "args": ["/elsewhere/server.py"],
                                                  "env": {}}})
        code, out = self.run_install()
        self.assertEqual(code, 1)
        self.assertIn("will not replace it", out)
        self.assertIn("/elsewhere/server.py", out)
        self.assertFalse(self.dest.exists())
        self.assertNotIn("mcp add", self.calls())
        self.assertEqual(self.servers()["crewview"]["args"], ["/elsewhere/server.py"])

    def test_owned_registration_is_updated_when_python_changes(self):
        self.assertEqual(self.run_install()[0], 0)
        code, out = self.run_install(python="/opt/other/python3")
        self.assertEqual(code, 0, out)
        self.assertIn("Update Codex MCP server 'crewview'", out)
        self.assertEqual(self.servers()["crewview"]["command"], "/opt/other/python3")

    def test_failed_registration_reports_without_rollback(self):
        code, out = self.run_install(env={**self.env, "FAKE_CODEX_FAIL_ADD": "1"})
        self.assertEqual(code, 1)
        self.assertIn("simulated failure", out)
        self.assertIn("no rollback was attempted", out)
        self.assertTrue((self.dest / "server.py").is_file())
        self.assertNotIn("crewview", self.servers())

    def test_unsafe_or_foreign_destinations_are_refused(self):
        stranger = self.root / "stranger"
        stranger.mkdir()
        (stranger / "notes.txt").write_text("mine")
        for target, message in ((stranger, "not a Crewview install"), (self.home, "not allowed"),
                                (ROOT, "not allowed"), (Path("relative/dir"), "absolute path")):
            code, out = self.run_install("--destination", str(target))
            self.assertEqual(code, 1, target)
            self.assertIn(message, out)
        self.assertEqual([p.name for p in stranger.iterdir()], ["notes.txt"])

    def test_dashboard_only_needs_no_clis_or_login(self):
        env = {**self.env, "PATH": str(self.root / "empty")}
        code, out = self.run_install("--dashboard-only", env=env)
        self.assertEqual(code, 0, out)
        self.assertIn("Skip Codex MCP registration", out)
        self.assertTrue((self.dest / "dashboard.py").is_file())
        self.assertTrue(json.loads((self.dest / settings.MARKER).read_text())["dashboard_only"])
        self.assertEqual(self.calls(), [])
        self.assertNotIn("crewview", self.servers())

    def test_launcher_runs_dashboard_with_recorded_python(self):
        if not shutil.which("zsh"):
            self.skipTest("zsh not available")
        self.assertEqual(self.run_install("--dashboard-only")[0], 0)
        (self.dest / "dashboard.py").write_text("import sys\nprint(sys.executable, sys.argv[1:])\n")
        run = subprocess.run(["zsh", str(self.dest / "launch-dashboard.command"), "--port", "9"], capture_output=True,
                             text=True, env={"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), f"{sys.executable} ['--open', '--state', '{self.dest / 'state'}', "
                                             "'--port', '9']")

    def test_custom_destination_viewer_matches_server_state(self):
        custom = self.root / "apps" / "crew"
        self.assertEqual(self.run_install("--destination", str(custom))[0], 0)
        server_state = self.servers()["crewview"]["env"]["CREWVIEW_STATE"]
        self.assertEqual(server_state, str(custom / "state"))
        # The installed dashboard's own default, even with a legacy variable in the caller's shell.
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); import dashboard, codex_history; "
                 "print(dashboard.DEFAULT_STATE)")
        run = subprocess.run([sys.executable, "-c", probe, str(custom)], capture_output=True, text=True, timeout=30,
                             env={**self.env, "CLAUDE_BRIDGE_STATE": "/elsewhere/legacy"})
        self.assertEqual(run.stdout.strip(), server_state, run.stderr)
        if shutil.which("zsh"):
            launcher = custom / "launch-dashboard.command"
            (custom / "dashboard.py").write_text(
                "import argparse, sys\np = argparse.ArgumentParser()\np.add_argument('--open', action='store_true')\n"
                "p.add_argument('--state')\nprint(p.parse_args().state)\n")
            env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
            run = subprocess.run(["zsh", str(launcher)], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(run.stdout.strip(), server_state, run.stderr)
            run = subprocess.run(["zsh", str(launcher), "--state", "/explicit/state"], capture_output=True, text=True,
                                 env=env, timeout=30)
            self.assertEqual(run.stdout.strip(), "/explicit/state")  # The caller's --state wins.

    # ---------- ownership, customization and preservation ----------

    def test_foreign_command_on_same_server_path_is_refused(self):
        self.assertEqual(self.run_install()[0], 0)
        hijacked = {**self.servers()["crewview"], "command": "/evil/python"}
        self.seed({**self.servers(), "crewview": hijacked})
        code, out = self.run_install()
        self.assertEqual(code, 1)
        self.assertIn("not this Crewview install's Python", out)
        code, out = self.run_uninstall("--destination", str(self.dest))
        self.assertEqual(code, 1)
        self.assertIn("is not the Python recorded", out)
        self.assertEqual(self.servers()["crewview"]["command"], "/evil/python")
        self.assertEqual(self.calls().count("mcp add"), 1)
        self.assertNotIn("mcp remove", self.calls())

    def test_customized_entry_is_not_silently_discarded(self):
        self.assertEqual(self.run_install()[0], 0)
        entry = {**self.servers()["crewview"], "options": {"tool_timeout_sec": 900}}
        entry["env"] = {**entry["env"], "HTTPS_PROXY": "http://proxy.example"}
        self.seed({**self.servers(), "crewview": entry})
        code, out = self.run_install()  # Identical registration: still a no-op.
        self.assertEqual(code, 0, out)
        self.assertEqual(self.calls().count("mcp add"), 1)
        code, out = self.run_install(python="/opt/other/python3")  # Needs an update that would drop the timeout.
        self.assertEqual(code, 1)
        self.assertIn("would discard (tool_timeout_sec)", out)
        self.assertEqual(self.servers()["crewview"]["options"], {"tool_timeout_sec": 900})
        del entry["options"]  # Extra env alone is carried over by the update.
        self.seed({**self.servers(), "crewview": entry})
        code, out = self.run_install(python="/opt/other/python3")
        self.assertEqual(code, 0, out)
        self.assertIn("keeps your env HTTPS_PROXY", out)
        updated = self.servers()["crewview"]
        self.assertEqual((updated["command"], updated["env"]["HTTPS_PROXY"]), ("/opt/other/python3", "http://proxy.example"))

    def test_unrelated_config_change_fails_verification_without_restore(self):
        code, out = self.run_install(env={**self.env, "FAKE_CODEX_TOUCH": "1"})
        self.assertEqual(code, 1)
        self.assertIn("Preservation check failed", out)
        self.assertIn("(model)", out)
        backup = next((self.dest / "backups").glob("*/codex-config.toml"))
        self.assertIn(str(backup), out)
        self.assertIn('model = "gpt-6-other"', self.config_toml.read_text())  # Not restored over the edit.
        self.assertIn('model = "gpt-6-astra"', backup.read_text())
        self.assertIn("crewview", self.servers())
        self.config_toml.write_text(backup.read_text())
        code, out = self.run_uninstall("--destination", str(self.dest))
        self.assertEqual(code, 0, out)  # Registration matches, no touch: clean removal.
        self.assertEqual(self.run_install()[0], 0)
        code, out = self.run_uninstall_env({**self.env, "FAKE_CODEX_TOUCH": "1"}, "--destination", str(self.dest))
        self.assertEqual(code, 1)
        self.assertIn("Preservation check failed", out)

    def run_uninstall_env(self, env, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = uninstall.main(list(argv), environ=env)
        return code, out.getvalue() + err.getvalue()

    def test_malformed_codex_config_blocks_before_writes(self):
        self.config_toml.write_text('model = \n[mcp_servers\n')
        code, out = self.run_install()
        self.assertEqual(code, 1)
        self.assertIn("could not be parsed", out)
        self.assertFalse(self.dest.exists())
        self.assertNotIn("mcp add", self.calls())

    def test_preservation_ignores_crewview_and_empty_args(self):
        before = {"model": "x", "mcp_servers": {"a": {"command": "c", "args": []}}}
        after = {"model": "x", "mcp_servers": {"a": {"command": "c"}, "crewview": {"command": "p"}}}
        self.assertEqual(settings.config_changes(before, after), [])
        self.assertEqual(settings.config_changes(before, {**after, "model": "y"}), ["model"])
        self.assertEqual(settings.config_changes({}, {"mcp_servers": {"crewview": {}}}), [])

    # ---------- uninstall ----------

    def test_uninstall_removes_only_owned_registration_and_keeps_files(self):
        self.assertEqual(self.run_install()[0], 0)
        (self.dest / "state" / "history.json").write_text("{}")
        code, out = self.run_uninstall("--destination", str(self.dest), "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("crewview", self.servers())
        code, out = self.run_uninstall("--destination", str(self.dest))
        self.assertEqual(code, 0, out)
        servers = self.servers()
        self.assertNotIn("crewview", servers)
        self.assertEqual(set(servers), {"claude-bridge", "other"})
        for kept in ("server.py", "crewview.json", "state/history.json"):
            self.assertTrue((self.dest / kept).exists(), kept)
        self.assertIn("never deletes them", out)
        self.assertEqual(len(list((self.dest / "backups").glob("*/codex-config.toml"))), 2)
        code, out = self.run_uninstall("--destination", str(self.dest))
        self.assertEqual(code, 0)
        self.assertIn("nothing to remove", out)

    def test_uninstall_refuses_foreign_registration(self):
        self.seed({**self.servers(), "crewview": {"command": "/usr/bin/python3", "args": ["/elsewhere/server.py"],
                                                  "env": {}}})
        code, out = self.run_uninstall("--destination", str(self.dest))
        self.assertEqual(code, 1)
        self.assertIn("left unchanged", out)
        self.assertEqual(self.servers()["crewview"]["args"], ["/elsewhere/server.py"])
        self.assertNotIn("mcp remove", self.calls())

    def test_uninstall_rejects_dangerous_destinations_and_never_suggests_rm(self):
        before = self.snapshot()
        for target in (self.home, Path("/"), self.home.parent):
            code, out = self.run_uninstall("--destination", str(target))
            self.assertEqual(code, 1, target)
            self.assertIn("contains your home folder", out)
            self.assertNotIn("rm ", out)
        self.assertEqual(self.calls(), [])  # Rejected before Codex is even consulted.
        self.assertEqual(self.snapshot(), before)
        code, out = self.run_uninstall("--destination", str(self.root / "never-installed"))
        self.assertEqual(code, 0)
        self.assertIn("No Crewview install was found", out)
        self.assertEqual(self.run_install()[0], 0)
        code, out = self.run_uninstall("--destination", str(self.dest))
        self.assertEqual(code, 0, out)
        self.assertNotIn("rm ", out)
        self.assertIn("move it to the Trash yourself", out)

    def test_uninstall_without_codex_changes_nothing(self):
        env_path = {**self.env, "PATH": str(self.root / "empty")}
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(uninstall.main(["--destination", str(self.dest)], environ=env_path), 1)
        self.assertIn("Codex CLI not found", out.getvalue())


if __name__ == "__main__":
    unittest.main()
