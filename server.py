#!/usr/bin/env python3
"""Crewview: local, dependency-free MCP bridge to subscription-authenticated Claude Code."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import activity  # noqa: E402  (sibling module, also installed alongside)
import settings  # noqa: E402

try:
    SETTINGS = settings.load()
except settings.SettingsError as error:
    sys.exit(f"crewview: invalid configuration: {error}")
MODEL = SETTINGS.worker
STATE = settings.state_dir()
CLI = settings.find_claude()
ACTIVE = activity.ACTIVE
OUTPUT_LIMIT = 16 * 1024 * 1024
OUTPUT_ERROR = "Claude output exceeded 16 MiB; stopped this job"
RESERVED = ACTIVE | {"interrupted"}
WORKER = settings.worker_label(MODEL)
ORCHESTRATOR = settings.codex_name(SETTINGS.models["orchestrator"]) or "the Codex orchestrator"
REVIEWER = settings.codex_name(SETTINGS.models["reviewer"]) or "the reviewer"
INSTRUCTIONS = f"""Suggested Crewview workflow (configurable in crewview.json):
{SETTINGS.describe("orchestrator")} coordinates and makes decisions; delegate
implementation to {SETTINGS.describe("worker")} through Claude Code with these tools;
use native Codex subagents such as {SETTINGS.describe("reviewer")} for critique/review
and {SETTINGS.describe("helper")} for small bounded tasks. These role models are
workflow suggestions; Crewview does not change the Codex model picker.
Use only when the user authorizes
delegation or an implementation workflow. Pass the exact task, relevant original
user instructions, authorized working directory, exclusions and completion checks.
Never use this bridge to bypass a Codex permission denial or sandbox restriction.
claude_start returns a background job; collect it with claude_status (wait_seconds
up to 25). Use claude_reply with conversation_id for follow-ups; it retains context.
Do not infer completion from job submission. Check status, actual_model, response,
and permission_denials. Return denied operations to the parent without working
around them. Keep concurrent writers in separate worktrees. The parent reviews
the actual diff and verifies results before declaring the user's task complete.
Jobs continue after the MCP client disconnects. claude_cancel cancels only the named
bridge job. Subscription login is required; no API-key fallback is supported.
Pass codex_thread_id to claude_start: the canonical UUID of the originating Codex
thread (the root conversation the user is in), so the local dashboard can show the
whole agent conversation. Omit it if you do not know it exactly; never guess.
"""


def child_instructions(model):
    """Worker system prompt naming the model the job actually requests."""
    return f"""You are {settings.worker_label(model)}, an implementation worker delegated by {ORCHESTRATOR} in Codex.
Follow the user's task and supplied constraints. Read applicable AGENTS.md and
CLAUDE.md in the authorized working directory before implementation. The task
should include any applicable ancestor instructions. Implement and verify the
authorized change. Do not publish, deploy, send messages, or change accounts unless
the original user explicitly authorized that action. Never bypass denied tools,
managed policy or sandbox restrictions. Report a blocked operation precisely.
Your final reply must state what changed, files affected, checks performed and
remaining issues. The parent may send {REVIEWER}'s review back to this same conversation.
"""


CHILD_INSTRUCTIONS = child_instructions(MODEL)


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("x") as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream, ensure_ascii=False)
    os.replace(temp, path)


def read(path):
    return json.loads(path.read_text())


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Expected a canonical bridge UUID")
    return value


def thread_link(args):
    """Optional explicit link to the originating Codex thread (canonical UUID only)."""
    value = args.get("codex_thread_id")
    if value is None:
        return None
    try:
        return identifier(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("codex_thread_id must be a canonical lowercase Codex thread UUID")


def job_path(job_id):
    return STATE / "jobs" / (identifier(job_id) + ".json")


def conversation_path(conversation_id):
    return STATE / "conversations" / (identifier(conversation_id) + ".json")


@contextlib.contextmanager
def locked():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE / "registry.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def environment():
    return settings.claude_environment()


def health():
    return {**settings.claude_auth(CLI), "model": MODEL, "cli": CLI,
            "login_instruction": "If not ready, run claude auth login --claudeai in Terminal."}


def integer(value, name, lower, upper):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f"{name} must be an integer from {lower} to {upper}")
    return value


def validate_task(args):
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 100000:
        raise ValueError("prompt must contain 1 to 100000 characters")
    return {"prompt": prompt,
            "timeout_seconds": integer(args.get("timeout_seconds", 1800), "timeout_seconds", 10, 7200),
            "max_turns": integer(args.get("max_turns", 60), "max_turns", 1, 200)}


def inspect_job(job_id):
    # Never claim a dead worker completed. The heartbeat is persisted for other
    # Codex threads, subsequent MCP processes and the dashboard to detect
    # interrupted runs; activity.effective holds the shared rule.
    return activity.effective(read(job_path(job_id)))


def public_job(job):
    return {key: value for key, value in job.items() if key not in {"prompt", "heartbeat"}}


def submit(args, reply=False):
    task = validate_task(args)
    link = thread_link(args)
    if not reply:
        cwd_arg = args.get("cwd", "")
        if not isinstance(cwd_arg, str) or not Path(cwd_arg).is_absolute():
            raise ValueError("cwd must be an explicit absolute directory")
        cwd = Path(cwd_arg).resolve(strict=True)
        if not cwd.is_dir() or cwd in {Path("/"), Path.home()}:
            raise ValueError("Choose a specific project/worktree, not / or your home directory")
        mode = args.get("mode", "implement")
        if mode not in {"implement", "consult"}:
            raise ValueError("mode must be implement or consult")
        # A conversation keeps the worker model it started with; config changes apply to new ones.
        conversation = {"conversation_id": str(uuid.uuid4()), "cwd": str(cwd), "mode": mode, "model": MODEL}
        if link:
            conversation["codex_thread_id"] = link
    # Check effective restricted-mode auth before admitting any billed work.
    if not health()["ready"]:
        raise RuntimeError("Claude subscription login unavailable. Run claude auth login --claudeai in Terminal. No API fallback was attempted.")
    with locked():
        if reply:
            conversation = read(conversation_path(args["conversation_id"]))
            previous = inspect_job(conversation["latest_job_id"])
            if previous["status"] in RESERVED:
                raise ValueError("This conversation already has an active job; wait or cancel it first")
            if not previous.get("session_id"):
                raise ValueError("The previous turn did not establish a Claude session; start a new conversation")
            if link and conversation.get("codex_thread_id") not in (None, link):
                raise ValueError("codex_thread_id conflicts with this conversation's linked Codex thread")
            if link:
                conversation["codex_thread_id"] = link  # Establishes a missing link; never replaces one.
            conversation["session_id"] = previous["session_id"]
            # Older records carry the model only on jobs; resuming must not switch models.
            conversation["model"] = conversation.get("model") or previous.get("model") or MODEL
        active = [inspect_job(p.stem) for p in (STATE / "jobs").glob("*.json")]
        # An interrupted worker may have left its Claude process alive. Never
        # release that writer slot or resume it based only on stale heartbeats.
        active = [j for j in active if j["status"] in RESERVED]
        if len(active) >= 3:
            raise ValueError("Three bridge jobs are already active; wait or cancel one")
        if conversation["mode"] == "implement" and any(
                j["mode"] == "implement" and j["cwd"] == conversation["cwd"] for j in active):
            raise ValueError("An implementation is active in this directory; use a separate worktree")
        job_id = str(uuid.uuid4())
        job = {**conversation, **task, "job_id": job_id, "model": conversation["model"],
               "status": "queued", "created_at": time.time(), "heartbeat": time.time()}
        atomic(job_path(job_id), job)
        conversation["latest_job_id"] = job_id
        atomic(conversation_path(conversation["conversation_id"]), conversation)
        log_path = STATE / "jobs" / (job_id + ".worker.log")
        try:
            with log_path.open("a") as log:
                os.chmod(log_path, 0o600)
                worker_process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", job_id],
                                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                  start_new_session=True, close_fds=True)
                # Reap while the server is alive without tying worker lifetime
                # to the MCP connection. On disconnect the OS adopts the child.
                threading.Thread(target=worker_process.wait, daemon=True).start()
        except Exception as error:
            job.update(status="failed", error=str(error), finished_at=time.time())
            atomic(job_path(job_id), job)
            raise
    return public_job(job)


def command(job):
    settings = {"disableAllHooks": True,
                "sandbox": {"enabled": True, "autoAllowBashIfSandboxed": True,
                            "allowUnsandboxedCommands": False, "failIfUnavailable": True,
                            "excludedCommands": []}}
    tools = "Read,Glob,Grep" if job["mode"] == "consult" else "Read,Glob,Grep,Edit,Write,Bash"
    # A job keeps the model it was submitted with, even if the configuration changes later.
    model = job.get("model") or MODEL
    cmd = [CLI, "--restricted", "-p", "--model", model,
           "--effort", "high", "--output-format", "stream-json", "--verbose",
           "--permission-mode", "plan" if job["mode"] == "consult" else "acceptEdits",
           "--permission-prompts", "none", "--tools", tools,
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--settings", json.dumps(settings), "--max-turns", str(job["max_turns"]),
           "--append-system-prompt", child_instructions(model)]
    if job.get("session_id"):
        cmd.extend(["--resume", job["session_id"]])
    return cmd


def stop_child(child):
    if child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=5)


def worker(job_id):
    path = job_path(job_id)
    job = read(path)
    cancel_path = path.with_suffix(".cancel")
    out_path = path.with_suffix(".stdout")
    recorder = activity.Recorder(path.with_suffix(".events.jsonl"))
    child = None
    try:
        job.update(status="running", heartbeat=time.time())
        atomic(path, job)
        if not health()["ready"]:
            raise RuntimeError("Subscription authentication unavailable; no API fallback attempted")
        if cancel_path.exists():
            job["status"] = "cancelled"
            return
        # File-backed I/O bounds memory and keeps prompts out of process arguments.
        err_path = path.with_suffix(".stderr")
        with out_path.open("w+") as out, err_path.open("w+") as err, path.with_suffix(".input").open("w+") as inp, \
                out_path.open("rb") as live:
            # `live` is a separate open file description, so reading it never
            # moves the offset the child shares with `out` while writing.
            for p in (out_path, err_path, path.with_suffix(".input")):
                os.chmod(p, 0o600)
            inp.write(job["prompt"])
            inp.seek(0)
            child = subprocess.Popen(command(job), cwd=job["cwd"], env=environment(),
                                     stdin=inp, stdout=out, stderr=err, start_new_session=True)
            deadline = time.monotonic() + job["timeout_seconds"]
            last_heartbeat = time.monotonic()
            while child.poll() is None:
                recorder.feed(live.read(1024 * 1024))
                if cancel_path.exists() or time.monotonic() >= deadline:
                    job["status"] = "cancelled" if cancel_path.exists() else "timed_out"
                    stop_child(child)
                    break
                if out_path.stat().st_size + err_path.stat().st_size > OUTPUT_LIMIT:
                    stop_child(child)
                    raise RuntimeError(OUTPUT_ERROR)
                if time.monotonic() - last_heartbeat > 5:
                    job["heartbeat"] = time.time()
                    atomic(path, job)
                    last_heartbeat = time.monotonic()
                time.sleep(0.2)
            # A fast child can exit between size checks; re-check before any
            # unbounded read of its final output.
            oversized = out_path.stat().st_size + err_path.stat().st_size > OUTPUT_LIMIT
            if not oversized:
                recorder.feed(live.read())
            recorder.finish()
            job["exit_code"] = child.returncode
            if job["status"] in {"cancelled", "timed_out"}:
                job["error"] = "Run stopped; any edits already made remain on disk. Inspect them before continuing."
                return
            if oversized:
                raise RuntimeError(OUTPUT_ERROR)
            out.seek(0)
            raw = out.read()
            err.seek(0)
            stderr = err.read()
        # stream-json ends with a result record; legacy/fake CLIs print one JSON object.
        result = activity.final_result(raw)
        if result is None:
            raise RuntimeError("Claude returned no valid JSON result. " + (stderr or activity.diagnostic_tail(raw))[-3000:])
        if not isinstance(result, dict):
            raise RuntimeError("Claude returned an unexpected JSON result shape")
        job.update(response=result.get("result", ""), session_id=result.get("session_id"),
                   permission_denials=result.get("permission_denials", []),
                   actual_models=list(result.get("modelUsage", {})),
                   model_usage=result.get("modelUsage", {}),
                   num_turns=result.get("num_turns"), usage=result.get("usage"),
                   cost_estimate_usd=result.get("total_cost_usd"),
                   cost_note="Claude's token-cost estimate is not a subscription invoice.")
        job["status"] = "failed" if child.returncode or result.get("is_error") else "completed"
        if job["status"] == "failed":
            job["error"] = result.get("errors") or result.get("subtype") or stderr[-3000:]
        elif job["permission_denials"]:
            job["status"] = "needs_attention"
        requested = job.get("model") or MODEL
        confirmed = [m for m in job["actual_models"] if settings.model_matches(requested, m)]
        other_models = [m for m in job["actual_models"] if m not in confirmed and not m.startswith("claude-haiku-")]
        if not confirmed or other_models:
            if job["status"] == "completed":
                job["status"] = "needs_attention"
            job["model_warning"] = (f"Reported usage includes another non-utility model or does not confirm the "
                                    f"requested worker model {requested}; inspect actual_models and model_usage.")
    except Exception as error:
        if child is not None:
            stop_child(child)
        job.update(status="failed", error=str(error))
    finally:
        recorder.finish()
        if child is not None and child.poll() is not None:
            # Keep the private diagnostic stdout, minus any thinking blocks.
            with contextlib.suppress(OSError, ValueError):
                activity.scrub_stdout(out_path, OUTPUT_LIMIT)
        job.update(finished_at=time.time(), heartbeat=time.time())
        atomic(path, job)


def status(args):
    wait = integer(args.get("wait_seconds", 0), "wait_seconds", 0, 25)
    deadline = time.monotonic() + wait
    while True:
        job = inspect_job(args["job_id"])
        if job["status"] not in ACTIVE or time.monotonic() >= deadline:
            return public_job(job)
        time.sleep(0.2)


def cancel(args):
    job = inspect_job(args["job_id"])
    if job["status"] == "interrupted":
        return {**public_job(job), "cancellation_warning": "Worker unavailable; cancellation cannot be confirmed. Writer slot remains reserved. Inspect the original worker and Claude process before reconciling this record."}
    if job["status"] in ACTIVE:
        atomic(job_path(job["job_id"]).with_suffix(".cancel"), {"requested_at": time.time()})
        return {"job_id": job["job_id"], "status": "cancellation_requested"}
    return public_job(job)


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TASK_PROPS = {"prompt": {"type": "string", "minLength": 1, "maxLength": 100000,
                         "description": "Concrete authorized task, exact user constraints, applicable ancestor instructions, and completion checks."},
              "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 7200, "default": 1800},
              "max_turns": {"type": "integer", "minimum": 1, "maximum": 200, "default": 60},
              "codex_thread_id": {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                                  "description": "Optional canonical UUID of the originating Codex thread, for the local dashboard. Omit if not known exactly; a reply may add a missing link but never change one."}}
TOOLS = [
    {"name": "claude_health", "description": "Check local Claude Code subscription login without sending a model request. Does not return identity or credentials.",
     "inputSchema": schema({}, []), "annotations": {"readOnlyHint": True}},
    {"name": "claude_start", "description": f"Delegate an authorized implementation or consultation to {WORKER} ({MODEL}) through Claude Code on this Mac. Returns a job immediately; collect with claude_status. Implement mode permits local edits and sandboxed Bash; consult has only Read/Glob/Grep. Requires an explicit project/worktree directory. Uses the existing Claude subscription. Never use to bypass parent permission denials.",
     "inputSchema": schema({**TASK_PROPS, "cwd": {"type": "string", "description": "Absolute authorized project/worktree directory on this Mac."},
                            "mode": {"type": "string", "enum": ["implement", "consult"], "default": "implement"}}, ["prompt", "cwd"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
    {"name": "claude_reply", "description": "Send follow-up instructions or review findings to a finished bridge conversation. Retains its Claude session, directory and permission mode. Returns a new background job.",
     "inputSchema": schema({**TASK_PROPS, "conversation_id": {"type": "string"}}, ["prompt", "conversation_id"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}},
    {"name": "claude_status", "description": "Read a Claude worker job's status and final reply, actual model, usage and permission denials. Wait up to 25 seconds for completion. Use the returned conversation_id for follow-ups. Submission is not completion.",
     "inputSchema": schema({"job_id": {"type": "string"}, "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 25, "default": 0}}, ["job_id"]),
     "annotations": {"readOnlyHint": True}},
    {"name": "claude_cancel", "description": "Request cancellation of exactly one bridge job. Existing edits are retained; inspect them before resuming. Does not stop other Claude or Codex sessions.",
     "inputSchema": schema({"job_id": {"type": "string"}}, ["job_id"]),
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
]


def call_tool(name, args):
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if not tool:
        raise ValueError("Unknown tool")
    if not isinstance(args, dict) or set(args) - set(tool["inputSchema"]["properties"]):
        raise ValueError("Unexpected tool arguments")
    if set(tool["inputSchema"]["required"]) - set(args):
        raise ValueError("Missing required arguments")
    return {"claude_health": lambda a: health(), "claude_start": submit,
            "claude_reply": lambda a: submit(a, reply=True),
            "claude_status": status, "claude_cancel": cancel}[name](args)


def serve():
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        if not isinstance(req, dict):
            continue
        if "id" not in req:
            continue
        response = {"jsonrpc": "2.0", "id": req["id"]}
        try:
            method, params = req.get("method"), req.get("params", {})
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": settings.MCP_NAME, "version": settings.VERSION},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    value = call_tool(params["name"], params.get("arguments", {}))
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
                except Exception as error:
                    result = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
            else:
                response["error"] = {"code": -32601, "message": "Method not found"}
                result = None
            if "error" not in response:
                response["result"] = result
        except Exception as error:
            response["error"] = {"code": -32602, "message": str(error)}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(identifier(sys.argv[2]))
    else:
        serve()
