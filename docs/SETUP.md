# Install Crewview with an agent

This is the installation runbook for a local agent that has been asked to set up Crewview. Do the work, report concrete results, and reserve questions for missing preferences, interactive authentication, or required permission prompts.

## 1. Inspect before changing anything

- Confirm macOS and Python 3.11 or newer (`python3 --version`).
- Inspect the repository and the installer's help: `python3 install.py --help`.
- Run `python3 install.py --check` and `python3 install.py --dry-run`.
- Identify existing `crewview` or legacy `claude-bridge` registration. Do not replace another installation silently.
- Use the configured Codex home, including `CODEX_HOME` when set. Do not print unrelated settings, account details, environment values, or secrets.

The full installation needs Codex desktop, a usable Codex CLI, Claude Code, and its subscription login. The viewer/demo does not need Claude authentication.

If a prerequisite is missing, explain exactly what is missing. Use the official [Codex installation guidance](https://developers.openai.com/codex/cli/) or [Claude Code setup guidance](https://code.claude.com/docs/en/setup) to help the user install it. Do not download unofficial binaries or change package-manager/system settings without the appropriate authorization.

If Claude Code needs authentication, have the user complete its normal interactive subscription login. Never ask the user to paste a token. Never switch to API billing to get past a login failure. Account and managed-organization restrictions still apply.

## 2. Choose the installation and crew

Default: install the bridge and dashboard, with Astra as orchestrator, Opus as Claude worker, Sol as reviewer, and Luna as helper. These are configurable preferences, not promises of account/model access.

If the user only wants to inspect saved work or run the demo, use `--dashboard-only`.

This skips registration; it does not disconnect an already registered bridge. Use the uninstaller for that separate action.

For custom models, copy `crewview.example.json` to a private local file and edit the `models` values. Pass it using `--config /absolute/path/to/crewview.json`. Do not commit the user's settings. Select the actual orchestrator model in Codex itself. See [WORKFLOW.md](WORKFLOW.md).

Explicit executable overrides are available with `--codex` and `--claude`. Use `--destination` only when the user needs a different installation location. Prefer defaults so the launcher and documentation are predictable.

## 3. Install

```sh
python3 install.py
```

For custom configuration:

```sh
python3 install.py --config /absolute/path/to/crewview.json
```

The installation registers MCP server `crewview`; its tools retain the `claude_` prefix. It does not replace the legacy `claude-bridge` server. Keep only the intended bridge enabled in a task to avoid duplicate tool sets; migrating an existing installation should be deliberate, with histories preserved.

Check the installer's reported destination, registration, and backup. An unrelated configuration change or registration conflict is a failed install, not something to suppress.

## 4. Verify without spending model usage

1. Inspect the `crewview` MCP registration with the Codex CLI.
2. Open a fresh local Codex task so it can load the new server and instructions.
3. Call `claude_health` if the full bridge is installed. A successful health check establishes authentication readiness, not a completed model request.
4. Launch `~/.local/share/crewview/launch-dashboard.command` and confirm the local page loads.
5. An empty page is normal before the first delegation. Optionally create a synthetic demo with `python3 demo.py --output /tmp/crewview-demo-UNIQUE` and follow the printed command. Use a fresh directory and a different port if another dashboard is running.

Do not claim a synthetic demo proves live Claude execution. To test the model path, get authorization for model usage, then run:

```sh
python3 smoke_test.py --output /tmp/crewview-live-check-UNIQUE
```

Inspect reported models, the written synthetic file, the follow-up response, and permission denials. A denied command is not permission to run it through another tool.

## 5. Verify the first real delegation

Include the exact originating Codex task UUID as `codex_thread_id` in `claude_start`. A direct shell in that task may expose `CODEX_THREAD_ID`; never assume a long-running shared MCP process's environment identifies its caller.

Collect the returned job with `claude_status`. Confirm the dashboard's participants include the parent and its actual task agents. If only the worker appears, check the thread link and local-log availability. Do not link by matching project paths or a UUID mentioned in a prompt.

## Updates

Inspect release changes, then rerun the installer from the new checkout. Preserve the user's existing configuration unless they explicitly provide a replacement with `--config`. Keep backups and history outside the repository. Restart the dashboard and open a fresh Codex task after updating. Worker model changes apply to new conversations; existing conversations keep their original model.

An unchanged MCP registration is left alone. Extra environment variables are preserved. If updating the registration would drop custom options such as timeouts or disabled tools, the installer stops and reports the conflict; inspect and deliberately reconcile those options before retrying.

## Uninstall

```sh
python3 uninstall.py
```

The uninstaller disconnects only the owned registration and keeps local files/history. It must refuse to remove a registration pointing elsewhere. Ownership verification needs the installation record. If the installation folder was already deleted, inspect the entry and manually run `codex mcp remove crewview` for the intended registration. Stop the dashboard with Ctrl+C. Removing an MCP registration does not cancel running worker jobs.

After inspecting any active work and saving anything needed, the user may manually remove the installation directory. Do not recursively delete history or backups as an automatic cleanup step.

## Completion report

Tell the user the installation path, dashboard URL, chosen role models, registration result, what was actually tested, and any remaining login/restart step. Keep distinctions between installed, loaded, authenticated, synthetic-tested, and live-tested explicit.
