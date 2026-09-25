# Troubleshooting

## The dashboard is empty

No bridge job may have been started yet, or the dashboard may be reading a different state directory. Check the startup message and the bridge's configured state path. Run the synthetic demo to separate viewer problems from model/login problems.

## I see the worker but not the orchestrator or reviewer

Check the coverage note. The bridge conversation must carry the exact `codex_thread_id` of the task that submitted it. A working-directory match or a UUID mentioned in a prompt is not enough.

Old bridge jobs may have no link. After verifying the original job-submission record, a finished conversation can accept its missing link through `claude_reply`; the normal reply also starts a new worker turn, so do not use it merely to edit metadata without authorization for that work. For a display-only repair, a local agent can back up and update only the verified conversation record under the bridge registry lock. Never rewrite job status or infer the originating thread from a title.

The linked task's logs must be present on the same Mac. Remote/cloud tasks whose local logs are unavailable cannot be reconstructed by this viewer. Newly created subagents may take up to 30 seconds to appear. Unsupported or unreadable records are reported in coverage notices.

## Opus has zero messages but appears to be working

The participant's message count counts final bridge replies, not individual tool calls. Open the worker detail view for live public activity. A running job has no final reply yet; a failed turn-limit run may also finish without one. Read status and errors before deciding to continue.

Older jobs created before activity recording was added may have only their saved final reply. Crewview does not automatically import arbitrary historical Claude transcripts.

## The requested model is unavailable

Check the model in the installed client and account. Configure an available model explicitly; do not silently substitute it. Native Codex role settings are suggestions, not automatic model-picker changes. The bridge's reported model usage is evidence separate from the requested model.

## Claude authentication is not ready

Complete Claude Code's normal interactive subscription login. Run the installer's check again or call `claude_health`. Do not paste credentials into the chat or add an API key to bypass a subscription-login failure.

## A command was denied

Inspect the denial and the exact authorized task. Respect the host's sandbox and managed policy. Do not retry through another tool as a workaround. The parent can handle an independently authorized operation through its normal permission system only when doing so does not bypass the denial.

## Codex cannot see the tools

Confirm `crewview` is registered with the correct absolute Python/server paths. Open a fresh Codex task, and restart the client if needed. A running task may still have an older server/tool schema loaded. A successful installer run alone does not prove tools loaded in that task.

## Port 8767 is occupied

The launcher reuses only a service it recognizes as this dashboard with the same state directory. If another service uses the port, choose another with `--port 8768`. Do not kill an unfamiliar process. Stop your own dashboard with Ctrl+C before restarting it.

## Two bridge tool sets appear

Crewview uses MCP registration `crewview`; the earlier private prototype used `claude-bridge`. It does not remove that older registration or migrate its history automatically. Decide which installation to use and disable only the verified older registration when authorized. Existing histories remain on disk.
