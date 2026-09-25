"""Crewview settings and local runtime discovery (stdlib only).

Model names are workflow suggestions for the Codex-native roles and the model
Claude Code is asked to use for the worker. Crewview never changes the Codex
model picker, never configures Codex subagent models, and never falls back to
API-key authentication for Claude.

Configuration is optional. It is read from $CREWVIEW_CONFIG when set, else from
crewview.json next to this file (the installed copy). A missing file means the
defaults; an invalid file is an error, never a silent fallback.
"""
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

HERE = Path(__file__).resolve().parent
VERSION = "0.1.0"
MCP_NAME = "crewview"
LEGACY_MCP_NAME = "claude-bridge"
CONFIG_NAME = "crewview.json"
MARKER = ".crewview-install.json"
ROLES = ("orchestrator", "worker", "reviewer", "helper")
DEFAULT_MODELS = {"orchestrator": "gpt-6-astra", "worker": "claude-opus-5-5",
                  "reviewer": "gpt-6-sol", "helper": "gpt-6-luna"}
MAX_CONFIG_BYTES = 64 * 1024
MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}(\[[A-Za-z0-9]{1,16}\])?")  # Always fullmatch.
CLAUDE_ALIASES = {"opus", "sonnet", "haiku"}
FAMILY = re.compile(r"^(?:claude-)?(opus|sonnet|haiku)(?![a-z])(?:-(\d{1,2})(?!\d)(?:-(\d{1,2})(?!\d))?)?")
CODEX_NAMES = ("astra", "sol", "luna")
# Codex desktop ships its CLI inside the app bundle; names differ between releases.
APP_CODEX = (Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
             Path("/Applications/Codex.app/Contents/Resources/codex"))


class SettingsError(ValueError):
    """The configuration file exists but cannot be used as written."""


class Settings:
    def __init__(self, models, source=None):
        self.models = dict(models)
        self.source = source

    @property
    def worker(self):
        return self.models["worker"]

    def describe(self, role):
        """'Sol (gpt-6-sol)' for a known Codex name, else a generic role with the model."""
        model = self.models[role]
        if role == "worker":
            return f"{worker_label(model)} ({model})"
        generic = {"orchestrator": "the main Codex agent", "reviewer": "a reviewer",
                   "helper": "a helper"}[role]
        name = codex_name(model)
        return f"{name} ({model})" if name else f"{generic} ({model})"


# ---------- Configuration ----------

def validate(data, source="configuration"):
    """Return the complete models dict for parsed JSON, or raise SettingsError."""
    if not isinstance(data, dict):
        raise SettingsError(f"{source}: expected a JSON object with a \"models\" object")
    unknown = sorted(set(data) - {"models", "$comment"})
    if unknown:
        raise SettingsError(f"{source}: unknown key(s) {', '.join(unknown)}; allowed: models")
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise SettingsError(f"{source}: \"models\" must be an object")
    unknown = sorted(set(models) - set(ROLES))
    if unknown:
        raise SettingsError(f"{source}: unknown model role(s) {', '.join(unknown)}; allowed: {', '.join(ROLES)}")
    out = dict(DEFAULT_MODELS)
    for role, value in models.items():
        if not isinstance(value, str) or not MODEL.fullmatch(value):
            raise SettingsError(f"{source}: models.{role} must be a model name of 1-100 letters, digits "
                                "or . _ : / - (optionally with a [suffix])")
        out[role] = value
    worker = out["worker"].split("[", 1)[0]
    if not (worker.startswith("claude-") or worker in CLAUDE_ALIASES):
        raise SettingsError(f"{source}: models.worker must be a Claude model (claude-...) or a Claude Code "
                            "alias (opus, sonnet, haiku); the worker always runs through Claude Code")
    return out


def load_file(path):
    path = Path(path)
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            raise SettingsError(f"{path}: larger than {MAX_CONFIG_BYTES // 1024} KiB")
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise SettingsError(f"{path}: not UTF-8 text") from None
    except OSError as error:
        raise SettingsError(f"{path}: cannot be read ({error.strerror or error})") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise SettingsError(f"{path}: invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}") from None
    return Settings(validate(data, str(path)), path)


def config_path(environ=None, here=HERE):
    """(path, explicit) — explicit paths must exist; the installed sibling is optional."""
    environ = os.environ if environ is None else environ
    if environ.get("CREWVIEW_CONFIG"):
        return Path(environ["CREWVIEW_CONFIG"]).expanduser(), True
    return Path(here) / CONFIG_NAME, False


def load(path=None, environ=None, here=HERE):
    if path is not None:
        return load_file(path)
    path, explicit = config_path(environ, here)
    if not explicit and not path.exists():
        return Settings(DEFAULT_MODELS)
    if explicit and not path.exists():
        raise SettingsError(f"CREWVIEW_CONFIG points to {path}, which does not exist")
    return load_file(path)


# ---------- Display names ----------

def worker_family(model):
    """'Opus', 'Sonnet' or 'Haiku' from a Claude model string; otherwise 'Claude'."""
    match = FAMILY.match((model or "").lower())
    return match.group(1).title() if match else "Claude"


def worker_label(model):
    """Human label with version when the model name carries one, e.g. 'Opus 5.5'."""
    match = FAMILY.match((model or "").lower())
    if not match:
        return "Claude"
    version = ".".join(part for part in match.groups()[1:] if part)
    return match.group(1).title() + (" " + version if version else "")


def codex_name(model):
    lowered = (model or "").lower()
    for name in CODEX_NAMES:
        if re.search(rf"(^|[^a-z]){name}([^a-z]|$)", lowered):
            return name.title()
    return None


def model_matches(requested, actual):
    """True when a reported model is the requested one (aliases match their family)."""
    if not isinstance(requested, str) or not isinstance(actual, str):
        return False
    base = requested.split("[", 1)[0]
    if actual in (requested, base):
        return True
    return base in CLAUDE_ALIASES and actual.startswith(f"claude-{base}-")


# ---------- Local paths ----------

def home(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ.get("HOME") or Path.home())


def default_destination(environ=None):
    return home(environ) / ".local/share/crewview"


def installed(here=HERE):
    return (Path(here) / MARKER).is_file()


def state_dir(environ=None, here=HERE):
    """CREWVIEW_STATE, else an installed copy's own state/ (the folder its MCP server uses), else the
    legacy CLAUDE_BRIDGE_STATE, else the default install location."""
    environ = os.environ if environ is None else environ
    if environ.get("CREWVIEW_STATE"):
        return Path(environ["CREWVIEW_STATE"]).expanduser()
    if installed(here):
        return Path(here) / "state"
    if environ.get("CLAUDE_BRIDGE_STATE"):
        return Path(environ["CLAUDE_BRIDGE_STATE"]).expanduser()
    return default_destination(environ) / "state"


def codex_home(environ=None):
    environ = os.environ if environ is None else environ
    return Path(environ["CODEX_HOME"]).expanduser() if environ.get("CODEX_HOME") else home(environ) / ".codex"


def codex_sessions(environ=None):
    environ = os.environ if environ is None else environ
    value = environ.get("CREWVIEW_CODEX_SESSIONS") or environ.get("CLAUDE_BRIDGE_CODEX_SESSIONS")
    return Path(value).expanduser() if value else codex_home(environ) / "sessions"


def unsafe_destination(destination, environ=None):
    """Why a folder can never be a Crewview install (/, your home, or a folder containing it), else None."""
    destination, user_home = Path(destination), home(environ).resolve()
    if destination == Path(destination.anchor) or user_home.is_relative_to(destination):
        return f"{destination} is / or contains your home folder; use a dedicated Crewview folder"
    return None


def backup_stamp():
    """Folder name for one run's backups; sub-second so consecutive runs never share one."""
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def executable(path):
    return bool(path) and Path(path).is_file() and os.access(path, os.X_OK)


def find_claude(explicit=None, environ=None):
    """Claude Code CLI: explicit/env path as given, else PATH, else ~/.local/bin/claude."""
    environ = os.environ if environ is None else environ
    given = explicit or environ.get("CREWVIEW_CLAUDE_CLI") or environ.get("CLAUDE_BRIDGE_CLI")
    if given:
        return str(Path(given).expanduser())
    found = shutil.which("claude", path=environ.get("PATH"))
    if found:
        return found
    candidate = home(environ) / ".local/bin/claude"
    return str(candidate) if executable(candidate) else None


def find_codex(explicit=None, environ=None):
    """Codex CLI: explicit path as given, else PATH, else the macOS app-bundled CLI."""
    environ = os.environ if environ is None else environ
    given = explicit or environ.get("CREWVIEW_CODEX_CLI")
    if given:
        return str(Path(given).expanduser())
    found = shutil.which("codex", path=environ.get("PATH"))
    if found:
        return found
    return next((str(path) for path in APP_CODEX if executable(path)), None)


# ---------- Claude subscription check (no model request) ----------

def claude_environment(environ=None):
    env = dict(os.environ if environ is None else environ)
    # Leave normal Keychain subscription auth intact; never reuse API credentials,
    # alternate providers, or an enclosing agent's runtime/session variables.
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_")) or key in {"CLAUDECODE", "CLAUDE_CONFIG_DIR"}:
            env.pop(key)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def claude_auth(cli, environ=None):
    """Subscription login status from `claude auth status`; never returns identity or credentials."""
    if not executable(cli):
        raise RuntimeError("Claude Code CLI not found. Install Claude Code or set CREWVIEW_CLAUDE_CLI "
                           "to its absolute path. No API fallback was attempted.")
    result = subprocess.run([cli, "--restricted", "auth", "status"], capture_output=True, text=True,
                            env=claude_environment(environ), timeout=20)
    try:
        auth = json.loads(result.stdout)
    except json.JSONDecodeError:
        auth = None
    if not isinstance(auth, dict):
        raise RuntimeError("Claude authentication check failed; run claude auth status in Terminal")
    return {"ready": bool(auth.get("loggedIn") and auth.get("authMethod") == "claude.ai"
                          and auth.get("apiProvider") == "firstParty"),
            "auth_method": auth.get("authMethod"), "subscription_type": auth.get("subscriptionType")}


# ---------- Codex MCP registration (read-only helpers) ----------

class CodexError(RuntimeError):
    pass


def codex_run(codex, args, environ=None, timeout=60):
    try:
        return subprocess.run([codex, *args], capture_output=True, text=True, timeout=timeout,
                              env=dict(os.environ if environ is None else environ), stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodexError(f"Could not run the Codex CLI at {codex}: {error}") from None


def registration(codex, name, environ=None):
    """The named MCP server as reported by `codex mcp get --json`, or None if absent."""
    result = codex_run(codex, ["mcp", "get", name, "--json"], environ)
    if result.returncode != 0:
        if "No MCP server named" in result.stderr:
            return None
        detail = result.stderr.strip().splitlines()[-1:] or ["no error output"]
        raise CodexError(f"`codex mcp get {name}` failed: {detail[0][:300]}")
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise CodexError(f"`codex mcp get {name} --json` returned unexpected output") from None
    transport = value.get("transport") if isinstance(value, dict) else None
    if not isinstance(transport, dict):
        raise CodexError(f"`codex mcp get {name} --json` returned an unexpected shape")
    # Options `codex mcp add` cannot express; a later add would silently drop them.
    custom = {key: item for key, item in value.items() if key not in ENTRY_DEFAULTS_IGNORED
              and item != ENTRY_DEFAULTS.get(key) and item not in (None, [], {})}
    custom.update({f"transport.{key}": item for key, item in transport.items()
                   if key not in {"type", "command", "args", "env"} and item not in (None, [], {})})
    return {"type": transport.get("type"), "command": transport.get("command"),
            "args": transport.get("args") or [], "env": transport.get("env") or {}, "custom": custom}


# Top-level `codex mcp get --json` fields and their values for a plain stdio entry.
ENTRY_DEFAULTS = {"name": None, "enabled": True, "disabled_reason": None, "transport": None, "enabled_tools": None,
                  "disabled_tools": None, "startup_timeout_sec": None, "tool_timeout_sec": None}
ENTRY_DEFAULTS_IGNORED = {"name", "transport"}


def installed_python(destination):
    """The interpreter recorded by install.py for this destination, or None."""
    try:
        value = json.loads((Path(destination) / MARKER).read_text())
    except (OSError, ValueError):
        return None
    python = value.get("python") if isinstance(value, dict) else None
    return python if isinstance(python, str) and python else None


def owned_by(entry, destination, pythons):
    """True only for a stdio entry running an expected Crewview interpreter on exactly this server.py."""
    return (bool(entry) and entry.get("type") == "stdio" and entry.get("command") in set(pythons) - {None}
            and entry.get("args") == [str(Path(destination) / "server.py")])


def describe_entry(entry):
    return " ".join([str(entry.get("command") or entry.get("type"))] + [str(a) for a in entry.get("args", [])])[:300]


# ---------- Codex config preservation check (read-only) ----------

def read_codex_config(environ=None):
    """Parsed config.toml ({} if absent); raises CodexError if it is not valid TOML."""
    path = codex_home(environ) / "config.toml"
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise CodexError(f"{path} could not be parsed ({error}); fix it before installing") from None


def _normalized(config, name):
    """Config without the named MCP entry; empty args/env count as absent (Codex may drop them)."""
    config = json.loads(json.dumps(config, default=str))
    servers = config.get("mcp_servers")
    if isinstance(servers, dict):
        servers.pop(name, None)
        for entry in servers.values():
            if isinstance(entry, dict):
                for key in ("args", "env"):
                    if entry.get(key) in ([], {}):
                        entry.pop(key)
        if not servers:
            config.pop("mcp_servers")
    return config


def config_changes(before, after, name=MCP_NAME):
    """Dotted keys that differ between two parsed configs, ignoring only the named MCP entry."""
    changed = []

    def walk(a, b, path):
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                walk(a.get(key, _MISSING), b.get(key, _MISSING), path + [str(key)])
        elif a != b:
            changed.append(".".join(path) or "(whole file)")
    walk(_normalized(before, name), _normalized(after, name), [])
    return changed


_MISSING = object()


def preservation_error(before, after, backup):
    """A report if anything besides the Crewview entry changed; the file is never restored automatically."""
    changes = config_changes(before, after)
    if not changes:
        return None
    where = f"The copy from before the change is {backup}." if backup else "No config.toml existed before."
    return (f"Preservation check failed: Codex config settings other than '{MCP_NAME}' changed "
            f"({', '.join(changes[:20])}). Crewview did not restore anything automatically, because another "
            f"program may have edited the file at the same time. {where} Review the difference and fix any "
            "unintended change yourself.")
