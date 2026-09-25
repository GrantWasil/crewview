import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

SERVER = Path(__file__).resolve().parents[1] / "server.py"
spec = importlib.util.spec_from_file_location("bridge", SERVER)
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)

FAKE = '''#!/usr/bin/env python3
import json, os, sys, time, uuid
from pathlib import Path
if "auth" in sys.argv:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}))
    sys.exit()
if os.environ.get("FAKE_ARGV_LOG"):
    with open(os.environ["FAKE_ARGV_LOG"], "a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
prompt = sys.stdin.read()
if prompt == "sleep":
    time.sleep(20)
if prompt == "bad-json":
    print("not JSON")
    sys.exit(1)
if prompt == "failed-json":
    print(json.dumps({"is_error": True, "errors": ["fixture failure"]}))
    sys.exit(1)
sid = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else str(uuid.uuid4())
if prompt == "init-only":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-opus-5-5"}))
    sys.exit()
if prompt == "flood":
    sys.stdout.write("x" * (17 * 1024 * 1024))
    sys.exit()
if prompt.startswith("stream"):
    def assistant(*blocks):
        return {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": list(blocks)}}
    def result(tool_id, content, error=False):
        return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id, "content": content, "is_error": error}]}}
    first = [{"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": sid, "tools": ["Write", "Edit", "Bash"]},
             assistant({"type": "thinking", "thinking": "HIDDEN-REASONING", "signature": "sig"}, {"type": "text", "text": "Starting — café"}),
             assistant({"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/w/a.txt", "content": "héllo\\n"}}),
             result("t1", "File created successfully")]
    rest = [assistant({"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/w/b.txt", "old_string": "x", "new_string": "y"}}),
            result("t2", "String to replace not found", True),
            assistant({"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "rm -rf build"}}),
            result("t3", "Permission to use Bash has been denied", True),
            assistant({"type": "redacted_thinking", "data": "HIDDEN-REDACTED"},
                      {"type": "tool_use", "id": "t4", "name": "Write", "input": {"file_path": "/w/c.txt", "content": "never"}}),
            {"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": sid,
             "modelUsage": {"claude-opus-5-5": {}}, "num_turns": 3,
             "permission_denials": [{"tool_name": "Bash", "tool_use_id": "t3", "tool_input": {"command": "rm -rf build"}}]}]
    def emit(records, extra=""):
        data = "\\n".join(json.dumps(r) for r in records) + "\\n" + extra
        for i in range(0, len(data), 37):  # Deliberately split mid-line and mid-character.
            sys.stdout.write(data[i:i + 37])
            sys.stdout.flush()
            time.sleep(0.003)
    emit(first, "not valid json\\n")
    if prompt == "stream-slow":
        time.sleep(3)
    emit(rest)
    sys.exit()
print(json.dumps({"result": prompt, "session_id": sid, "modelUsage": {"claude-opus-5-5": {}},
 "permission_denials": [{"tool_name": "Bash"}] if prompt == "denied" else [], "num_turns": 1}))
'''


class BridgeTest(unittest.TestCase):
    def test_mixed_model_usage_requires_attention(self):
        usage = {"claude-opus-5-5": {"outputTokens": 1}, "claude-sonnet-4-6": {"outputTokens": 1000}}
        self.fake.write_text(FAKE.replace('{"claude-opus-5-5": {}}', repr(usage)))
        job = b.status({"job_id": self.start()["job_id"], "wait_seconds": 10})
        self.assertEqual(job["status"], "needs_attention")
        self.assertEqual(job["model_usage"], usage)
        self.assertIn("model_warning", job)

    def test_opus_with_haiku_utility_usage_is_allowed(self):
        usage = {"claude-opus-5-5": {}, "claude-haiku-4-5": {}}
        self.fake.write_text(FAKE.replace('{"claude-opus-5-5": {}}', repr(usage)))
        job = b.status({"job_id": self.start()["job_id"], "wait_seconds": 10})
        self.assertEqual(job["status"], "completed")
        self.assertNotIn("model_warning", job)

    def test_configured_worker_alias_is_requested_and_confirmed(self):
        # The worker subprocess re-reads configuration, so the job's recorded model is what counts.
        self.fake.write_text(FAKE.replace('{"claude-opus-5-5": {}}', repr({"claude-sonnet-5": {}})))
        with patch.object(b, "MODEL", "sonnet"):
            job = self.collect(self.start())
            self.assertEqual((job["model"], job["status"]), ("sonnet", "completed"))
            self.assertNotIn("model_warning", job)
            cmd = b.command({"mode": "implement", "max_turns": 3, "model": "sonnet"})
            self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")

    def test_conversation_keeps_its_worker_model_across_config_changes(self):
        log = self.root / "argv.jsonl"
        config = self.root / "crewview.json"
        config.write_text(json.dumps({"models": {"worker": "claude-sonnet-5"}}))
        # The worker subprocess loads this Sonnet config; the job was submitted under Opus.
        with patch.dict(os.environ, {"CREWVIEW_CONFIG": str(config), "FAKE_ARGV_LOG": str(log)}):
            first = self.collect(self.start())
            with patch.object(b, "MODEL", "claude-sonnet-5"):  # Config changed before the reply.
                second = self.collect(b.submit({"conversation_id": first["conversation_id"], "prompt": "more"}, reply=True))
                path = b.conversation_path(first["conversation_id"])
                record = b.read(path)
                del record["model"]  # Records from before this field existed fall back to the previous job.
                b.atomic(path, record)
                third = self.collect(b.submit({"conversation_id": first["conversation_id"], "prompt": "again"}, reply=True))
                fresh = self.collect(self.start())
        self.assertEqual([j["model"] for j in (first, second, third)], ["claude-opus-5-5"] * 3)
        self.assertEqual([j["status"] for j in (first, second, third)], ["completed"] * 3)
        self.assertEqual(fresh["model"], "claude-sonnet-5")  # New conversations follow the new config.
        runs = [json.loads(line) for line in log.read_text().splitlines()]
        runs = [r for r in runs if "--model" in r]
        self.assertEqual([r[r.index("--model") + 1] for r in runs],
                         ["claude-opus-5-5"] * 3 + ["claude-sonnet-5"])
        prompts = [r[r.index("--append-system-prompt") + 1] for r in runs]
        self.assertTrue(all(p.startswith("You are Opus 5.5,") for p in prompts[:3]), prompts)
        self.assertTrue(prompts[3].startswith("You are Sonnet 5,"))

    def test_configured_worker_flags_a_different_reported_model(self):
        with patch.object(b, "MODEL", "claude-sonnet-5"):
            job = self.collect(self.start())  # The fake reports claude-opus-5-5.
        self.assertEqual(job["status"], "needs_attention")
        self.assertIn("claude-sonnet-5", job["model_warning"])
        self.assertNotIn("Opus 5.5", job["model_warning"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = self.root / "claude"
        self.fake.write_text(FAKE)
        self.fake.chmod(0o700)
        self.state = self.root / "state"
        # Both the Crewview names and the legacy bridge names, so a developer's shell can't redirect tests.
        self.env = patch.dict(os.environ, {"CREWVIEW_STATE": str(self.state), "CREWVIEW_CLAUDE_CLI": str(self.fake),
                                           "CLAUDE_BRIDGE_STATE": str(self.state), "CLAUDE_BRIDGE_CLI": str(self.fake)})
        self.env.start()
        os.environ.pop("CREWVIEW_CONFIG", None)
        self.globals = patch.multiple(b, STATE=self.state, CLI=str(self.fake))
        self.globals.start()

    def tearDown(self):
        for p in (self.state / "jobs").glob("*.json"):
            if b.inspect_job(p.stem)["status"] in b.ACTIVE:
                b.cancel({"job_id": p.stem})
                b.status({"job_id": p.stem, "wait_seconds": 10})
        self.globals.stop()
        self.env.stop()
        self.tmp.cleanup()

    def start(self, prompt="hello", **kw):
        return b.submit({"prompt": prompt, "cwd": str(self.root), **kw})

    def collect(self, job):
        return b.status({"job_id": job["job_id"], "wait_seconds": 10})

    def test_result_and_followup_preserve_session(self):
        first = self.collect(self.start())
        self.assertEqual(first["status"], "completed")
        followup = b.submit({"conversation_id": first["conversation_id"], "prompt": "followup"}, reply=True)
        second = self.collect(followup)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(second["response"], "followup")
        self.assertNotIn("prompt", second)
        self.assertEqual(second["cwd"], first["cwd"])

    def test_cancel_and_writer_conflict(self):
        first = self.start("sleep")
        with self.assertRaisesRegex(ValueError, "implementation is active"):
            self.start()
        with self.assertRaisesRegex(ValueError, "active job"):
            b.submit({"conversation_id": first["conversation_id"], "prompt": "followup"}, reply=True)
        self.assertEqual(b.cancel({"job_id": first["job_id"]})["status"], "cancellation_requested")
        self.assertEqual(self.collect(first)["status"], "cancelled")

    def test_permission_denial_and_invalid_output_are_not_success(self):
        self.assertEqual(self.collect(self.start("denied"))["status"], "needs_attention")
        self.assertEqual(self.collect(self.start("bad-json"))["status"], "failed")
        self.assertEqual(self.collect(self.start("failed-json"))["status"], "failed")

    def test_interrupted_job_keeps_writer_slot(self):
        first = self.collect(self.start())
        path = b.job_path(first["job_id"])
        record = b.read(path)
        record.update(status="running", heartbeat=time.time() - 100)
        b.atomic(path, record)
        try:
            self.assertEqual(b.inspect_job(first["job_id"])["status"], "interrupted")
            with self.assertRaisesRegex(ValueError, "implementation is active"):
                self.start()
            with self.assertRaisesRegex(ValueError, "active job"):
                b.submit({"conversation_id": first["conversation_id"], "prompt": "followup"}, reply=True)
            self.assertEqual(b.cancel({"job_id": first["job_id"]})["status"], "interrupted")
        finally:
            record["status"] = "completed"
            b.atomic(path, record)

    def test_api_auth_cannot_start_work(self):
        self.fake.write_text(FAKE.replace('"claude.ai"', '"apiKey"'))
        with self.assertRaisesRegex(RuntimeError, "No API fallback"):
            self.start()
        self.assertFalse((self.state / "jobs").exists())

    def test_environment_and_restricted_tools(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "secret", "ANTHROPIC_BASE_URL": "https://invalid", "CLAUDE_CODE_USE_BEDROCK": "1"}):
            env = b.environment()
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ANTHROPIC_BASE_URL", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
        cmd = b.command({"mode": "consult", "max_turns": 3})
        self.assertIn("--restricted", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Glob,Grep")
        self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_bad_arguments_and_traversal(self):
        for bad in ("../../settings", "not-a-uuid", "", None):
            with self.assertRaises((ValueError, AttributeError)):
                b.job_path(bad)
        with self.assertRaises(ValueError):
            b.call_tool("claude_start", {"cwd": str(self.root), "prompt": "x", "bypass": True})
        for cwd in ("relative", "/", str(Path.home())):
            with self.assertRaises(ValueError):
                b.submit({"cwd": cwd, "prompt": "x"})

    def test_mcp_handshake_and_errors(self):
        requests = ["not-json", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}),
                    json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                    json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
                    json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "claude_health", "arguments": {}}})]
        run = subprocess.run([sys.executable, str(SERVER)], input="\n".join(requests) + "\n", text=True, capture_output=True, check=True)
        responses = [json.loads(line) for line in run.stdout.splitlines()]
        self.assertEqual(len(responses), 4)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(len(responses[2]["result"]["tools"]), 5)
        self.assertTrue(json.loads(responses[3]["result"]["content"][0]["text"])["ready"])

    def events(self, job):
        path = b.job_path(job["job_id"]).with_suffix(".events.jsonl")
        return path, [json.loads(line) for line in path.read_text().splitlines()]

    def test_stream_json_records_sanitized_activity(self):
        job = self.collect(self.start("stream"))
        # Final fields keep their existing shape and priority rules.
        self.assertEqual(job["status"], "needs_attention")
        self.assertEqual(job["response"], "done")
        self.assertEqual(job["actual_models"], ["claude-opus-5-5"])
        self.assertEqual(job["permission_denials"][0]["tool_use_id"], "t3")
        self.assertTrue(job["session_id"])
        path, events = self.events(job)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds, ["session", "text", "tool_use", "tool_result", "tool_use", "tool_result",
                                 "tool_use", "tool_result", "tool_use", "result", "notice"])
        self.assertEqual(events[1]["text"], "Starting — café")
        self.assertEqual(events[2]["input"]["content"], "héllo\n")
        stored = path.read_text() + b.job_path(job["job_id"]).with_suffix(".stdout").read_text() + json.dumps(job)
        self.assertNotIn("HIDDEN-REASONING", stored)
        self.assertNotIn("HIDDEN-REDACTED", stored)
        outcomes = b.activity.tool_outcomes(events, job["permission_denials"], active=False)
        self.assertEqual(outcomes, {"t1": "succeeded", "t2": "failed", "t3": "denied", "t4": "no_result"})

    def test_activity_is_visible_while_running(self):
        job = self.start("stream-slow")
        path = b.job_path(job["job_id"]).with_suffix(".events.jsonl")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (path.exists() and '"tool_result"' in path.read_text()):
            time.sleep(0.1)
        self.assertEqual(b.status({"job_id": job["job_id"]})["status"], "running")
        _, events = self.events(job)
        self.assertEqual([e["kind"] for e in events], ["session", "text", "tool_use", "tool_result"])
        self.assertEqual(self.collect(job)["status"], "needs_attention")

    def test_legacy_single_json_output_still_completes(self):
        job = self.collect(self.start("legacy"))
        self.assertEqual((job["status"], job["response"]), ("completed", "legacy"))
        _, events = self.events(job)
        self.assertEqual([e["kind"] for e in events], ["result"])

    def test_singleton_stream_record_is_not_a_result(self):
        job = self.collect(self.start("init-only"))
        self.assertEqual(job["status"], "failed")
        self.assertIn("no valid JSON result", job["error"])

    def test_fast_oversized_output_is_bounded(self):
        job = self.collect(self.start("flood"))
        self.assertEqual(job["status"], "failed")
        self.assertIn("exceeded 16 MiB", job["error"])
        stdout = b.job_path(job["job_id"]).with_suffix(".stdout")
        self.assertLess(stdout.stat().st_size, 1024)  # Oversized diagnostic replaced by a note.

    def test_codex_thread_link_is_validated_stored_and_preserved(self):
        thread = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"  # Synthetic; has letters so .upper() is invalid.
        for tool in ("claude_start", "claude_reply"):
            spec = next(t for t in b.TOOLS if t["name"] == tool)["inputSchema"]["properties"]["codex_thread_id"]
            self.assertEqual(spec["type"], "string")
        self.assertIn("codex_thread_id", b.INSTRUCTIONS)
        for bad in (thread.upper(), "not-a-uuid", 5, ""):
            with self.assertRaisesRegex(ValueError, "codex_thread_id"):
                self.start(codex_thread_id=bad)
        first = self.collect(self.start(codex_thread_id=thread))
        self.assertEqual(first["codex_thread_id"], thread)
        conversation = b.read(b.conversation_path(first["conversation_id"]))
        self.assertEqual(conversation["codex_thread_id"], thread)
        # A reply without the field keeps the link; the same link is fine; a different one is refused.
        second = self.collect(b.submit({"conversation_id": first["conversation_id"], "prompt": "more"}, reply=True))
        self.assertEqual(second["codex_thread_id"], thread)
        self.collect(b.submit({"conversation_id": first["conversation_id"], "prompt": "same", "codex_thread_id": thread}, reply=True))
        with self.assertRaisesRegex(ValueError, "conflicts"):
            b.submit({"conversation_id": first["conversation_id"], "prompt": "x", "codex_thread_id": str(uuid.uuid4())}, reply=True)
        self.assertEqual(b.read(b.conversation_path(first["conversation_id"]))["codex_thread_id"], thread)

    def test_reply_can_establish_a_missing_link(self):
        thread = str(uuid.uuid4())
        first = self.collect(self.start())
        self.assertNotIn("codex_thread_id", first)
        second = self.collect(b.submit({"conversation_id": first["conversation_id"], "prompt": "x", "codex_thread_id": thread}, reply=True))
        self.assertEqual(second["codex_thread_id"], thread)
        self.assertEqual(b.read(b.conversation_path(first["conversation_id"]))["codex_thread_id"], thread)

    def test_job_survives_mcp_disconnect(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "claude_start", "arguments": {"cwd": str(self.root), "prompt": "hello"}}}
        run = subprocess.run([sys.executable, str(SERVER)], input=json.dumps(request) + "\n", text=True, capture_output=True, check=True)
        job = json.loads(json.loads(run.stdout)["result"]["content"][0]["text"])
        self.assertEqual(self.collect(job)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
