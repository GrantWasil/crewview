# Crewview agent instructions

## If asked to install Crewview

Read `docs/SETUP.md`, then perform its prerequisite checks and install workflow. The user's request authorizes ordinary local installation and verification; do not ask again for each reversible step. Respect actual tool/OS permission prompts and organization policy. Stop for an interactive login or genuinely missing preference; never work around a denied operation.

Preserve existing Codex models, other MCP servers, project files, and user model configuration. Do not treat a successful file copy as proof that Codex loaded the server. Verify the registration, launch the local dashboard, and distinguish a synthetic demo from a real model run. Ask before a real model smoke test unless the user already authorized model usage.

Do not extract, print, copy, or transform subscription tokens. Do not silently use API keys or a different model. Never use this bridge to evade the parent agent's sandbox or approval restrictions.

## If asked to use Crewview

Read `docs/WORKFLOW.md`. Pass the exact originating `codex_thread_id` to the first `claude_start`. If it is unavailable, omit it and report that the dashboard cannot show the parent conversation yet; do not guess based on directories or prompt text. Carry forward the original user's authority and constraints. Keep one implementation writer per directory, use the reviewer for critique, and send fixes back with `claude_reply`.

## If asked to change this repository

Keep the runtime standard-library-only and Python 3.11 compatible. Test changes with isolated temporary state and fake CLIs. Add regressions for meaningful parsing, attribution, installation, or permission behavior; do not use live accounts in unit tests.

The dashboard must remain read-only and bound to loopback, with its Host/Origin protections. Parse only linked task agents' public messages; never expose reasoning, platform permission-review records, credentials, or encrypted message bodies. Do not infer model identity, successful edits, completed commands, or task completion without evidence.

Run the relevant tests and `python3 scripts/release-check.py` before publishing. Never commit private histories, installation backups, local configuration, task prompts, or real screenshots. Do not publish or push unless the user authorized that action.
