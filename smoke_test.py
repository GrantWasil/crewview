"""Live two-turn test with synthetic data; uses the existing Claude subscription.

Opt-in only: this sends real requests to the configured Claude Code worker model.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402

WORKER = settings.load().worker
parser = argparse.ArgumentParser()
parser.add_argument("--server", default=str(Path(__file__).with_name("server.py")))
parser.add_argument("--output", required=True)
args = parser.parse_args()
output = Path(args.output).resolve()
output.mkdir(parents=True, exist_ok=True)
fixture = output / ("fixture-" + uuid.uuid4().hex[:8])
fixture.mkdir()
env = os.environ.copy()
env["CREWVIEW_STATE"] = str(output / "state")
server = subprocess.Popen([sys.executable, args.server], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
request_id = 0


def request(method, params):
    global request_id
    request_id += 1
    server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
    server.stdin.flush()
    response = json.loads(server.stdout.readline())
    if "error" in response:
        raise RuntimeError(response["error"])
    return response["result"]


def tool(name, arguments):
    result = request("tools/call", {"name": name, "arguments": arguments})
    if result.get("isError"):
        raise RuntimeError(result["content"])
    return json.loads(result["content"][0]["text"])


def collect(job, allow_denial=False):
    while job["status"] in {"queued", "running"}:
        job = tool("claude_status", {"job_id": job["job_id"], "wait_seconds": 25})
        print(json.dumps({"job_id": job["job_id"], "status": job["status"]}), flush=True)
    if job["status"] != "completed" and not (allow_denial and job["status"] == "needs_attention" and job.get("permission_denials") and not job.get("model_warning")):
        raise RuntimeError(json.dumps(job))
    return job


try:
    init = request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "bridge-smoke", "version": "1"}})
    server.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    server.stdin.flush()
    assert len(request("tools/list", {})["tools"]) == 5
    auth = tool("claude_health", {})
    assert auth["ready"], auth
    print(json.dumps({"health": auth}), flush=True)
    marker = "bridge-" + uuid.uuid4().hex[:12]
    first = collect(tool("claude_start", {"cwd": str(fixture), "mode": "implement", "timeout_seconds": 180, "max_turns": 12,
        "prompt": f"Synthetic bridge verification only. In the current empty working directory, create implementation.txt containing exactly {marker} followed by a newline. Use a file-writing tool. Then use Bash to run python3 to check its exact content, printing VERIFIED on success. Remember the marker for a follow-up; do not create any other files. Report the marker and the check result. If Bash needs approval, report the denial without retrying; the test harness will separately inspect the resulting file. This request authorizes only this fixture file and local verification, no external actions."}), allow_denial=True)
    assert (fixture / "implementation.txt").read_text() == marker + "\n"
    assert any(settings.model_matches(WORKER, model) for model in first["actual_models"]), first["actual_models"]
    second = collect(tool("claude_reply", {"conversation_id": first["conversation_id"], "timeout_seconds": 180, "max_turns": 8,
        "prompt": "Continue our synthetic bridge test. Recall the marker from the preceding request. Change implementation.txt to contain that same marker followed immediately by -resumed and a newline, using Edit or Write. Report what you changed. No shell command is requested in this turn; the test harness independently checks the file. Only this fixture file is authorized."}))
    assert first["session_id"] == second["session_id"]
    assert (fixture / "implementation.txt").read_text() == marker + "-resumed\n"
    evidence = {"passed": True, "scope": "MCP handshake, subscription auth, Claude worker file implementation, response, followup and session continuity; file contents independently verified by harness", "shell_check_passed": not bool(first.get("permission_denials")), "tested_at": time.time(), "fixture": str(fixture), "auth": auth, "first": first, "followup": second}
    (output / "live-smoke.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps({"passed": True, "model": second["actual_models"], "session_retained": True, "evidence": str(output / "live-smoke.json")}), flush=True)
finally:
    server.stdin.close()
    server.wait(timeout=10)
