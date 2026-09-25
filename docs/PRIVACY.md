# Privacy and permissions

## What stays local

Crewview's dashboard reads local job records and the public messages of explicitly linked Codex tasks and their task agents. It runs on loopback, serves a fixed set of assets and read-only endpoints, and rejects foreign Host/Origin requests and cross-site fetches. It has no hosted backend, telemetry service, or account system.

Local records can contain sensitive prompts, public responses, file content excerpts, tool inputs, and command results. Private file permissions reduce accidental exposure; they do not make the dashboard an authenticated service for multiple users on a shared computer. Other local processes with sufficient access can read the files or contact the loopback service.

Do not expose the server with a public tunnel, bind it to a public interface, or upload histories and installation backups to GitHub. Use `demo.py` to create synthetic material for screenshots and bug reports.

## What is sent to model providers

The orchestrator and native Codex agents use the accounts and tools configured in Codex. The bridge launches Claude Code with the delegated prompt and permitted project access. Claude Code sends relevant context to Anthropic through the user's normal authenticated account.

A ChatGPT subscription does not pay for Claude usage. Each provider's access rules, usage limits, billing terms, and organization policies still apply. Crewview does not extract subscription tokens or convert subscription credentials into general API credentials. It strips ambient alternate-provider/API authentication settings from its Claude child environment and requires the supported subscription authentication path.

## Permission boundaries

The worker uses Claude Code restricted mode with a bounded tool set. Implementation mode allows file tools and requires sandboxed Bash. Consultation mode is read/search only. There is no automatic authority to deploy, publish, send messages, or modify accounts.

A working directory is not a filesystem security boundary. The host's sandbox, Claude permissions, and organization policy govern access; scope the prompt and inspect the result. An unattended permission request may fail and return an attention state. A denial is never authorization to retry the operation through an alternative tool.

Stopping a job does not undo edits. Removing the bridge registration does not stop a detached worker or remove local history.

## What the timeline leaves out

Private reasoning, analysis, developer/system instructions, platform permission-review sessions, encrypted message bodies, and raw Codex tool outputs are not exposed in the team timeline. Readable assistant updates/finals and cleaned human requests are included. Some handoff bodies are encrypted in Codex's files; Crewview shows a marker instead of trying to decrypt them.

The adapter depends on a local Codex log format, which is not a stable public API. Unreadable, partial, oversized, or unsupported records produce incomplete coverage. Never interpret a missing message as proof that no work occurred.

## Reporting a problem

Report a minimal reproduction using synthetic data, the Python/macOS/client versions, and the error message with private details removed. Never attach your state directory, Codex rollout files, Claude transcript, installation backup, tokens, or a screenshot containing private project content.
