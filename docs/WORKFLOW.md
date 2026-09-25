# Configure and use your crew

Crewview's default roles are:

| Role | Suggested model | How it runs |
|---|---|---|
| Orchestrator | `gpt-6-astra` | Your selected Codex task model |
| Worker | `claude-opus-5-5` | Claude Code through the bridge |
| Reviewer | `gpt-6-sol` | Native Codex task agent |
| Helper | `gpt-6-luna` | Native Codex task agent, when useful |

Change the `models` entries in a copy of `crewview.example.json`, then install using `--config`. The worker must be a model usable through your Claude Code account. Changing the other entries changes the suggested delegation instructions; select the orchestrator in Codex and explicitly request the configured model when creating reviewer/helper agents. Crewview does not provide a cross-provider model picker or substitute unsupported models.

Worker model changes apply to new bridge conversations after reloading the MCP server. Existing conversations keep the model they started with, including follow-up fixes.

Model names and availability can change. Verify them in the actual installed clients and accounts. If a requested model is unavailable, report it and ask for an alternative rather than silently substituting one.

## One task, multiple contributors

1. The orchestrator receives the user's task and decides what to delegate.
2. It calls `claude_start` with the authorized absolute project/worktree path, concrete prompt, mode, and exact `codex_thread_id`.
3. The worker implements. `claude_status` returns progress and, eventually, a reply or failure/attention state.
4. A native Codex reviewer examines the actual changes and tests.
5. The orchestrator sends relevant findings to the same worker conversation with `claude_reply`.
6. The orchestrator verifies the result and reports what actually works.

A handoff should include the original user requirements, authorized scope, relevant project instructions, completion checks, and exclusions. Don't turn an agent's guess into user approval. Keep simultaneous implementation writers in separate worktrees/directories.

The helper is optional. Use it for a bounded check when useful; don't create agents solely to fill every role.

## Bridge tools

| Tool | Purpose |
|---|---|
| `claude_health` | Check subscription authentication without a model request |
| `claude_start` | Start authorized worker implementation or consultation |
| `claude_status` | Read progress/result; bounded waiting is supported |
| `claude_reply` | Continue a finished worker conversation |
| `claude_cancel` | Stop the named job; already-made edits remain |

`implement` permits the configured file tools and sandboxed Bash. `consult` permits read/search tools. Restricted mode excludes external MCP servers and personal/project Claude settings/hooks; provide relevant instructions in the task. Managed policy still applies.

## Link the whole conversation

`codex_thread_id` is the UUID of the Codex task that actually called the bridge. It is not the Claude session ID, bridge job ID, bridge conversation ID, a shared session ID, or the original brainstorming task that created the implementation task.

A reply retains an existing link. It can establish a missing link by explicitly providing the verified originating UUID; it cannot change an existing link. If a job is running, wait until it finishes before using a reply to establish a link. A manually repaired historical link requires verified original submission evidence and a backup; do not guess.

Local logs provide parent/child task ancestry. Crewview follows that ancestry to show public messages and actual per-turn model labels. Missing logs, encrypted bodies, and unsupported formats are coverage gaps, not invented conversation content.
