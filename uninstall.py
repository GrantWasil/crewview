#!/usr/bin/env python3
"""Remove Crewview's Codex MCP registration, only if it launches this install.

Files, history (state/) and crewview.json are always kept; this script never
deletes a folder. Other MCP servers, including "claude-bridge", are untouched.
"""
import argparse
import os
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import settings  # noqa: E402


def default_destination(environ):
    """This folder when run from an install; otherwise the standard install location."""
    return HERE if settings.installed(HERE) else settings.default_destination(environ)


def guidance(destination):
    if not settings.installed(destination):
        return f"No Crewview install was found at {destination}; no files were changed."
    return (f"Crewview's files were kept in {destination}: history in state/, your models in "
            f"{settings.CONFIG_NAME}, backups in backups/. Uninstalling never deletes them. When you no longer "
            "need them, review that folder and move it to the Trash yourself.")


def backup_codex_config(destination, environ):
    config = settings.codex_home(environ) / "config.toml"
    if not config.is_file():
        return None
    target = destination / "backups" / settings.backup_stamp() / "codex-config.toml"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(config, target)
    return target


def fail(message):
    print(message, file=sys.stderr)
    return 1


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    p = argparse.ArgumentParser(description="Remove Crewview's Codex MCP registration if it points to this "
                                            "install. Never deletes files or history.")
    p.add_argument("--destination", metavar="PATH", help="Crewview install folder (default: this folder when run "
                                                         "from an install, else ~/.local/share/crewview)")
    p.add_argument("--codex", metavar="PATH", help="Codex CLI executable (default: PATH, then the Codex app bundle)")
    p.add_argument("--dry-run", action="store_true", help="Show what would be removed without changing anything")
    args = p.parse_args(argv)
    destination = Path(args.destination).expanduser().resolve() if args.destination else default_destination(environ)
    unsafe = settings.unsafe_destination(destination, environ)
    if unsafe:
        return fail(f"{unsafe}. Nothing was changed.")
    codex = settings.find_codex(args.codex, environ)
    if not settings.executable(codex):
        return fail(f"Codex CLI not found{f' at {codex}' if codex else ''}; pass --codex PATH. Nothing was changed.")
    try:
        entry = settings.registration(codex, settings.MCP_NAME, environ)
        if entry is None:
            print(f"No '{settings.MCP_NAME}' MCP server is registered with Codex; nothing to remove.")
            print(guidance(destination))
            return 0
        python = settings.installed_python(destination)
        if not settings.owned_by(entry, destination, {python}):
            reason = (f"is not the Python recorded by the install at {destination}" if python else
                      f"cannot be confirmed as Crewview's: no Crewview install record at {destination}")
            return fail(f"The '{settings.MCP_NAME}' MCP server launches {settings.describe_entry(entry)}, which "
                        f"{reason}. It was left unchanged. Pass --destination for the matching install, or inspect "
                        f"it with `codex mcp get {settings.MCP_NAME}` and remove it yourself.")
        if args.dry_run:
            print(f"Would back up the Codex config and run `codex mcp remove {settings.MCP_NAME}`. Nothing was changed.")
            print(guidance(destination))
            return 0
        before = settings.read_codex_config(environ)
        backup = backup_codex_config(destination, environ)
        result = settings.codex_run(codex, ["mcp", "remove", settings.MCP_NAME], environ)
        if result.returncode != 0:
            return fail(f"`codex mcp remove {settings.MCP_NAME}` failed: {result.stderr.strip()[-500:]}")
        if settings.registration(codex, settings.MCP_NAME, environ) is not None:
            return fail(f"Codex still reports a '{settings.MCP_NAME}' MCP server; inspect it with "
                        f"`codex mcp get {settings.MCP_NAME}`.")
        problem = settings.preservation_error(before, settings.read_codex_config(environ), backup)
        if problem:
            return fail(f"Removed the '{settings.MCP_NAME}' MCP server, but: {problem}")
    except settings.CodexError as error:
        return fail(f"{error}. Nothing further was changed.")
    print(f"Removed the '{settings.MCP_NAME}' MCP server from Codex." + (f" Config backup: {backup}" if backup else ""))
    print("Restart Codex to stop the running server. Jobs already started keep running until they finish.")
    print(guidance(destination))
    return 0


if __name__ == "__main__":
    sys.exit(main())
