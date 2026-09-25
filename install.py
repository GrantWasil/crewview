#!/usr/bin/env python3
"""Install Crewview on macOS for Codex desktop / Codex CLI and Claude Code (stdlib only).

A normal run copies the runtime to the destination (default
~/.local/share/crewview) and registers the Codex MCP server "crewview" with
`codex mcp add`. Every prerequisite is checked read-only first; nothing is
written when a check fails. An existing "claude-bridge" registration and other
Codex settings are left unchanged. Nothing here calls a model.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import sys
import time

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import settings  # noqa: E402

MIN_PYTHON = (3, 11)
RUNTIME = ("server.py", "activity.py", "codex_history.py", "dashboard.py", "settings.py", "demo.py", "uninstall.py",
           "crewview.example.json", "web/index.html", "web/app.css", "web/app.js")
LAUNCHER = "launch-dashboard.command"
LAUNCHER_LINE = 'CREWVIEW_PYTHON=""'
OPTIONAL = ("LICENSE", "README.md")
DOCS = "docs"
PRIVATE_FILE, PRIVATE_DIR, EXECUTABLE = 0o600, 0o700, 0o700


class InstallError(RuntimeError):
    pass


class Report:
    """Ordered check results; any 'fail' blocks every write."""

    def __init__(self):
        self.lines = []

    def add(self, level, text):
        self.lines.append((level, text))

    @property
    def problems(self):
        return [text for level, text in self.lines if level == "fail"]

    def render(self):
        return "\n".join(f"  {level:<5} {text}" for level, text in self.lines)


def source_files(source=HERE):
    """Relative paths copied into an install: allowlisted runtime, assets, docs and license."""
    files = list(RUNTIME)
    files += [name for name in OPTIONAL if (source / name).is_file()]
    docs = source / DOCS
    if docs.is_dir():
        files += sorted(str(p.relative_to(source)) for p in docs.rglob("*.md")
                        if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts)
    return files


def desired_registration(destination, python, claude, existing=None):
    """The entry to register; extra env variables on an existing Crewview entry are kept."""
    ours = {"CREWVIEW_STATE": str(destination / "state"),
            "CREWVIEW_CONFIG": str(destination / settings.CONFIG_NAME),
            "CREWVIEW_CLAUDE_CLI": claude}
    extra = {k: v for k, v in (existing or {}).get("env", {}).items() if k not in ours}
    return {"type": "stdio", "command": python, "args": [str(destination / "server.py")], "env": {**extra, **ours}}


def same_registration(entry, wanted):
    return bool(entry) and all(entry.get(key) == wanted[key] for key in ("type", "command", "args", "env"))


def describe_models(models):
    return ", ".join(f"{role} {models[role]}" for role in settings.ROLES)


def resolve_destination(value, environ):
    raw = value or str(settings.default_destination(environ))
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise InstallError(f"--destination must be an absolute path (got {raw})")
    return path.resolve()


def check_destination(destination, environ, report):
    if settings.unsafe_destination(destination, environ) or HERE.is_relative_to(destination) \
            or destination.is_relative_to(HERE):
        report.add("fail", f"Destination {destination} is not allowed; choose a dedicated folder such as "
                           f"{settings.default_destination(environ)}")
        return None
    if destination.exists() and not destination.is_dir():
        report.add("fail", f"Destination {destination} exists and is not a folder")
        return None
    if (destination / settings.MARKER).is_file():
        report.add("info", f"Destination: existing Crewview install at {destination} (will be updated)")
        return "update"
    if destination.is_dir() and any(destination.iterdir()):
        report.add("fail", f"Destination {destination} is not empty and is not a Crewview install; "
                           "choose a new or empty folder")
        return None
    report.add("info", f"Destination: {destination} (new)")
    return "new"


def check_config(args, destination, report):
    """Returns (action, models, source) for crewview.json; never modifies anything."""
    installed = destination / settings.CONFIG_NAME
    try:
        if args.config:
            loaded = settings.load_file(Path(args.config).expanduser())
            action = "replace" if installed.exists() else "copy"
            report.add("ok", f"Config: {loaded.source} ({describe_models(loaded.models)})")
            return action, loaded.models, loaded.source
        if installed.exists():
            loaded = settings.load_file(installed)
            report.add("ok", f"Config: keeping {installed} ({describe_models(loaded.models)})")
            return "keep", loaded.models, installed
        loaded = settings.load_file(HERE / "crewview.example.json")
        report.add("ok", f"Config: defaults ({describe_models(loaded.models)})")
        return "create", loaded.models, loaded.source
    except settings.SettingsError as error:
        hint = " Fix it or pass --config PATH." if not args.config else ""
        report.add("fail", f"Config: {error}.{hint}")
        return None, None, None


def check_cli(kind, explicit, finder, report, missing_hint):
    path = finder()
    if path and settings.executable(path):
        report.add("ok", f"{kind}: {path}")
        return path
    if explicit or path:
        report.add("fail", f"{kind}: {path or explicit} is not an executable file")
    else:
        report.add("fail", f"{kind} not found. {missing_hint}")
    return None


def preflight(args, environ, system, version, python):
    """All read-only checks. Returns (facts, report); callers must not write if report.problems."""
    report = Report()
    facts = {"python": python}
    version_text = ".".join(map(str, version[:3]))
    if tuple(version[:2]) < MIN_PYTHON:
        report.add("fail", f"Python {version_text} at {python}; Crewview needs Python 3.11 or newer. "
                           "Re-run install.py with a newer python3.")
    else:
        report.add("ok", f"Python {version_text} ({python})")
    if system != "Darwin":
        report.add("fail", f"Crewview supports macOS only (this system reports {system})")
    else:
        report.add("ok", "macOS")
    missing = [name for name in RUNTIME + (LAUNCHER,) if not (HERE / name).is_file()]
    if missing:
        report.add("fail", f"Source folder is incomplete; missing {', '.join(missing)}")
    try:
        destination = resolve_destination(args.destination, environ)
    except InstallError as error:
        report.add("fail", str(error))
        return facts, report
    facts["destination"] = destination
    facts["mode"] = check_destination(destination, environ, report)
    facts["config_action"], facts["models"], facts["config_source"] = check_config(args, destination, report)
    if args.dashboard_only:
        report.add("info", "Dashboard only: no MCP registration; Codex CLI and Claude Code are not required")
        return facts, report
    codex = check_cli("Codex CLI", args.codex, lambda: settings.find_codex(args.codex, environ), report,
                      "Install the Codex desktop app or Codex CLI, or pass --codex PATH.")
    claude = check_cli("Claude Code CLI", args.claude, lambda: settings.find_claude(args.claude, environ), report,
                       "Install Claude Code, or pass --claude PATH.")
    facts["codex"], facts["claude"] = codex, claude
    if claude and args.skip_auth_check:
        report.add("info", "Claude subscription login: not checked (--skip-auth-check); "
                           "jobs refuse to start until `claude auth login --claudeai` succeeds")
    elif claude:
        try:
            auth = settings.claude_auth(claude, environ)
        except (RuntimeError, OSError, ValueError) as error:
            report.add("fail", f"Claude subscription login: {error}")
        else:
            if auth["ready"]:
                tier = f", {auth['subscription_type']}" if auth.get("subscription_type") else ""
                report.add("ok", f"Claude subscription login (claude.ai{tier})")
            else:
                report.add("fail", "Claude Code is not logged in with a Claude subscription "
                                   f"(auth method: {auth.get('auth_method') or 'none'}). Run `claude auth login "
                                   "--claudeai` in Terminal, then re-run. Crewview never uses API keys.")
    if codex:
        try:
            settings.read_codex_config(environ)  # Malformed config: stop before any write.
            facts["registration"] = settings.registration(codex, settings.MCP_NAME, environ)
            legacy = settings.registration(codex, settings.LEGACY_MCP_NAME, environ)
        except settings.CodexError as error:
            report.add("fail", str(error))
            return facts, report
        entry = facts["registration"]
        # Ours only if it runs this installer's Python or the one recorded by the previous install.
        pythons = {python, settings.installed_python(destination)}
        if entry is None:
            report.add("info", f"Codex MCP server '{settings.MCP_NAME}': not registered yet")
        elif not settings.owned_by(entry, destination, pythons):
            report.add("fail", f"Codex MCP server '{settings.MCP_NAME}' is already registered and launches "
                               f"{settings.describe_entry(entry)}, which is not this Crewview install's Python and "
                               f"server; Crewview will not replace it. Inspect it with `codex mcp get "
                               f"{settings.MCP_NAME}` and remove it yourself if it is stale, then re-run.")
        elif claude and not same_registration(entry, desired_registration(destination, python, claude, entry)) \
                and entry["custom"]:
            report.add("fail", f"Codex MCP server '{settings.MCP_NAME}' needs updating, but it has settings "
                               f"`codex mcp add` would discard ({', '.join(sorted(entry['custom']))}). Crewview "
                               f"will not drop them: update its command/args/env in "
                               f"{settings.codex_home(environ) / 'config.toml'} yourself, or remove those settings, "
                               "then re-run.")
        else:
            report.add("info", f"Codex MCP server '{settings.MCP_NAME}': registered for this destination")
        if legacy is not None:
            report.add("note", f"'{settings.LEGACY_MCP_NAME}' is also registered and stays unchanged; Codex will "
                               "list both tool sets until you remove one")
    return facts, report


def wanted_registration(facts):
    return desired_registration(facts["destination"], facts["python"], facts["claude"], facts.get("registration"))


def registration_current(facts):
    return same_registration(facts.get("registration"), wanted_registration(facts))


def plan(facts, args, environ):
    destination = facts["destination"]
    steps = [("Update Crewview files in " if facts["mode"] == "update" else "Create ") + str(destination)
             + (" (changed files are backed up to backups/<time>/ first)" if facts["mode"] == "update" else ""),
             f"Copy {len(source_files())} files: runtime, dashboard assets, example config, docs and license",
             f"Write {LAUNCHER} using {facts['python']}",
             f"Create private state folder {destination / 'state'}"]
    steps.append({"keep": f"Keep your existing {settings.CONFIG_NAME}",
                  "create": f"Create {settings.CONFIG_NAME} with the default models",
                  "copy": f"Install {settings.CONFIG_NAME} from {facts['config_source']}",
                  "replace": f"Replace {settings.CONFIG_NAME} with {facts['config_source']} (current file backed up)",
                  }[facts["config_action"]])
    if args.dashboard_only:
        steps.append("Skip Codex MCP registration (--dashboard-only)")
    elif registration_current(facts):
        steps.append(f"Codex MCP server '{settings.MCP_NAME}' is already registered as planned; unchanged")
    else:
        verb = "Update" if facts.get("registration") else "Register"
        home = settings.codex_home(environ)
        extra = sorted(set(wanted_registration(facts)["env"]) - set(desired_registration(
            facts["destination"], facts["python"], facts["claude"])["env"]))
        kept = f"; keeps your env {', '.join(extra)}" if extra else ""
        steps.append(f"{verb} Codex MCP server '{settings.MCP_NAME}' via `codex mcp add` (backs up "
                     f"{home / 'config.toml'} first and verifies no other setting changed{kept})")
    return steps


class Writer:
    """Copies files atomically, backing up any different existing file once per run."""

    def __init__(self, destination, stamp):
        self.destination = destination
        self.backup = destination / "backups" / stamp
        self.backed_up = []

    def keep_copy(self, target, name=None):
        if not target.is_file():
            return None
        copy = self.backup / (name or target.relative_to(self.destination))
        copy.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR)
        shutil.copy2(target, copy)
        self.backed_up.append(copy)
        return copy

    def write(self, relative, data, mode=PRIVATE_FILE, backup=True):
        target = self.destination / relative
        if target.is_file() and target.read_bytes() == data:
            os.chmod(target, mode)
            return
        if backup:
            self.keep_copy(target)
        target.parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR)
        temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        with open(temp, "wb") as stream:
            os.chmod(temp, mode)
            stream.write(data)
        os.replace(temp, target)


def launcher_text(python):
    text = (HERE / LAUNCHER).read_text()
    if LAUNCHER_LINE not in text:
        raise InstallError(f"{LAUNCHER} is missing its {LAUNCHER_LINE} line")
    return text.replace(LAUNCHER_LINE, f"CREWVIEW_PYTHON={shlex.quote(python)}", 1)


def install_files(facts, args, stamp):
    destination = facts["destination"]
    launcher = launcher_text(facts["python"]).encode()  # Fails before anything is written.
    destination.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR)
    writer = Writer(destination, stamp)
    for relative in source_files():
        writer.write(relative, (HERE / relative).read_bytes())
    writer.write(LAUNCHER, launcher, EXECUTABLE)
    if facts["config_action"] in ("copy", "replace"):
        writer.write(settings.CONFIG_NAME, Path(facts["config_source"]).read_bytes())
    elif facts["config_action"] == "create":
        writer.write(settings.CONFIG_NAME, (HERE / "crewview.example.json").read_bytes())
    (destination / "state").mkdir(exist_ok=True, mode=PRIVATE_DIR)
    marker = {"app": "crewview", "version": settings.VERSION, "installed_at": time.time(),
              "python": facts["python"], "dashboard_only": bool(args.dashboard_only)}
    writer.write(settings.MARKER, (json.dumps(marker, indent=2) + "\n").encode(), backup=False)
    return writer


def register(facts, environ, writer):
    """Register via the Codex CLI, which edits only this entry; never rewrites config.toml ourselves."""
    wanted = wanted_registration(facts)
    config = settings.codex_home(environ) / "config.toml"
    before = settings.read_codex_config(environ)
    backup = writer.keep_copy(config, "codex-config.toml")
    command = ["mcp", "add", settings.MCP_NAME]
    for key, value in wanted["env"].items():
        command += ["--env", f"{key}={value}"]
    command += ["--", wanted["command"], *wanted["args"]]
    result = settings.codex_run(facts["codex"], command, environ)
    if result.returncode != 0:
        raise InstallError(f"`codex mcp add {settings.MCP_NAME}` failed: {result.stderr.strip()[-500:]}")
    if not same_registration(settings.registration(facts["codex"], settings.MCP_NAME, environ), wanted):
        raise InstallError(f"Codex did not report the expected '{settings.MCP_NAME}' registration after adding it; "
                           f"inspect it with `codex mcp get {settings.MCP_NAME}`")
    problem = settings.preservation_error(before, settings.read_codex_config(environ), backup)
    if problem:
        raise InstallError(problem)


def parser():
    p = argparse.ArgumentParser(description="Install Crewview (macOS, Python 3.11+). Checks everything read-only "
                                            "before writing; never calls a model.")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="Report prerequisites, login, config and registration "
                                                          "status without installing anything")
    mode.add_argument("--dry-run", action="store_true", help="Show the install plan without writing anything")
    p.add_argument("--dashboard-only", action="store_true",
                   help="Install only the read-only dashboard and demo: no MCP registration, no Claude Code needed")
    p.add_argument("--destination", metavar="PATH",
                   help="Install folder (default: ~/.local/share/crewview)")
    p.add_argument("--config", metavar="PATH", help="Validated crewview.json to install (replaces the installed "
                                                    "one after backing it up); otherwise an existing one is kept")
    p.add_argument("--codex", metavar="PATH", help="Codex CLI executable (default: PATH, then the Codex app bundle)")
    p.add_argument("--claude", metavar="PATH", help="Claude Code CLI executable (default: PATH, then ~/.local/bin/claude)")
    p.add_argument("--skip-auth-check", action="store_true",
                   help="Do not require a Claude subscription login now (jobs still refuse to start without one)")
    return p


def main(argv=None, environ=None, system=None, version=None, python=None):
    args = parser().parse_args(argv)
    environ = os.environ if environ is None else environ
    system = platform.system() if system is None else system
    version = tuple(sys.version_info) if version is None else tuple(version)
    python = os.path.abspath(sys.executable) if python is None else python
    title = "check (read-only)" if args.check else "dry run (no changes)" if args.dry_run else "install"
    print(f"Crewview {settings.VERSION} {title}")
    facts, report = preflight(args, environ, system, version, python)
    print(report.render())
    if report.problems:
        print(f"\n{len(report.problems)} problem(s) found. Nothing was changed.")
        return 1
    if args.check:
        print("\nReady to install. Nothing was changed.")
        return 0
    steps = plan(facts, args, environ)
    print("\nPlan:" if args.dry_run else "\nInstalling:")
    print("\n".join(f"  - {step}" for step in steps))
    if args.dry_run:
        print("\nDry run only. Nothing was changed.")
        return 0
    old_umask = os.umask(0o077)
    try:
        writer = install_files(facts, args, settings.backup_stamp())
        if not args.dashboard_only and not registration_current(facts):
            register(facts, environ, writer)
    except (InstallError, settings.CodexError, OSError) as error:
        print(f"\nInstall stopped: {error}\nFiles already copied remain in {facts['destination']}; "
              "no rollback was attempted. Fix the problem and re-run install.py.", file=sys.stderr)
        return 1
    finally:
        os.umask(old_umask)
    destination = facts["destination"]
    if writer.backed_up:
        print(f"\nBacked up {len(writer.backed_up)} previous file(s) to {writer.backup}")
    print(f"\nInstalled Crewview in {destination}")
    print(f"Dashboard: {shlex.quote(str(destination / LAUNCHER))}  (or: {shlex.quote(python)} "
          f"{shlex.quote(str(destination / 'dashboard.py'))} --open)")
    print(f"Models: edit {destination / settings.CONFIG_NAME}")
    if not args.dashboard_only:
        print(f"Restart Codex so it starts the '{settings.MCP_NAME}' MCP server.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
