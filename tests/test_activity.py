import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("activity", ROOT / "activity.py")
activity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activity)

STREAM = [
    {"type": "system", "subtype": "init", "model": "claude-opus-5-5", "session_id": "s", "tools": ["Read"]},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "SECRET"}, {"type": "text", "text": "Voilà ✓"}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/p/.env"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "r1", "content": [{"type": "text", "text": "TOKEN=abc\nB=2"}]}]}},
    {"type": "user", "message": {"content": [{"type": "text", "text": "<system-reminder>not ours</system-reminder>"}]}},
    {"type": "result", "subtype": "success", "result": "ok", "session_id": "s", "num_turns": 2, "permission_denials": []},
]


def stream_bytes():
    return ("\n".join(json.dumps(r, ensure_ascii=False) for r in STREAM) + "\n").encode()


class ActivityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, chunks, name="events.jsonl"):
        recorder = activity.Recorder(self.dir / name)
        for chunk in chunks:
            recorder.feed(chunk)
        recorder.finish()
        events, malformed, truncated = activity.read_events((self.dir / name).read_bytes())
        return [{k: v for k, v in e.items() if k != "at"} for e in events], malformed, truncated

    def test_byte_split_stream_matches_whole_stream(self):
        data = stream_bytes()
        whole, _, _ = self.record([data], "whole.jsonl")
        split, _, _ = self.record([data[i:i + 1] for i in range(len(data))], "split.jsonl")
        self.assertEqual(whole, split)
        self.assertEqual([e["kind"] for e in whole], ["session", "text", "tool_use", "tool_result", "result"])
        self.assertEqual(whole[1]["text"], "Voilà ✓")

    def test_hidden_content_and_read_output_are_not_recorded(self):
        self.record([stream_bytes()])
        stored = (self.dir / "events.jsonl").read_text()
        for secret in ("SECRET", "TOKEN=abc", "system-reminder"):
            self.assertNotIn(secret, stored)
        self.assertIn("2 lines read", stored)

    def test_malformed_and_final_partial_lines(self):
        events, _, _ = self.record([b"garbage\n[1,2]\n", json.dumps(STREAM[1]).encode()])  # No trailing newline.
        self.assertEqual([e["kind"] for e in events], ["text", "notice"])
        self.assertIn("2 unreadable", events[-1]["text"])

    def test_event_log_is_bounded(self):
        with unittest.mock.patch.object(activity, "MAX_EVENTS", 5):
            line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}) + "\n"
            events, _, truncated = self.record([(line * 20).encode()])
        self.assertTrue(truncated)
        self.assertEqual(len(events), 5)
        big = "y" * (activity.CONTENT + 10)
        events, _, _ = self.record([(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "w", "name": "Write", "input": {"file_path": "/a", "content": big}}]}}) + "\n").encode()], "big.jsonl")
        self.assertEqual(len(events[0]["input"]["content"]), activity.CONTENT)
        self.assertEqual(events[0]["truncated"], ["content"])

    def test_read_events_ignores_in_progress_and_unknown_lines(self):
        data = b'{"seq":0,"kind":"text","text":"a"}\n{"seq":1,"kind":"thinking","text":"no"}\nnope\n{"seq":2,"kind":"te'
        events, malformed, truncated = activity.read_events(data)
        self.assertEqual([e["text"] for e in events], ["a"])
        self.assertEqual((malformed, truncated), (2, False))

    def test_final_result_legacy_stream_and_invalid(self):
        self.assertEqual(activity.final_result('{"result": "x"}')["result"], "x")
        self.assertEqual(activity.final_result(stream_bytes().decode())["result"], "ok")
        self.assertIsNone(activity.final_result("not json"))
        self.assertIsNone(activity.final_result(json.dumps(STREAM[1]) + "\n"))
        self.assertEqual(activity.final_result("[1]"), [1])  # Worker reports unexpected shape.
        self.assertNotIn("SECRET", activity.diagnostic_tail(json.dumps(STREAM[1]) + "\n"))

    def test_only_result_envelopes_are_final(self):
        for singleton in (STREAM[0], STREAM[1], {"type": "user", "message": {}}, {"unrelated": 1}):
            self.assertIsNone(activity.final_result(json.dumps(singleton)), singleton)
            self.assertIsNone(activity.final_result(json.dumps(singleton, indent=2)), singleton)
        self.assertEqual(activity.final_result(json.dumps(STREAM[-1]))["result"], "ok")
        error = activity.final_result('{"is_error": true, "errors": ["boom"]}')
        self.assertEqual(error["errors"], ["boom"])
        self.assertIsNone(activity.final_result('{"type": "result", "result": NaN}'))

    def test_scrub_never_keeps_thinking_or_raw_fragments(self):
        cases = {
            "singleton": json.dumps(STREAM[1]),
            "pretty singleton": json.dumps(STREAM[1], indent=2),
            "partial stream": json.dumps(STREAM[0]) + "\n" + json.dumps(STREAM[1])[:60],
            "nested": json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "x", "name": "Bash", "input": {"items": [{"type": "redacted_thinking", "data": "SECRET"}]}}]}}),
            "stream_event": json.dumps({"type": "stream_event", "event": {"delta": {"thinking": "SECRET"}}}),
        }
        for name, raw in cases.items():
            path = self.dir / "case.stdout"
            path.write_text(raw + "\n" + '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"SECRET')
            activity.scrub_stdout(path)
            text = path.read_text()
            self.assertNotIn("SECRET", text, name)
            self.assertNotIn('"thinking"', text, name)
            self.assertIn("unreadable output line", text, name)
            for line in text.splitlines():
                json.loads(line)  # Only re-serialized records and notes remain.
        big = self.dir / "big.stdout"
        big.write_text("SECRET" * 10)
        activity.scrub_stdout(big, limit=20)
        self.assertNotIn("SECRET", big.read_text())

    def test_diagnostic_tail_never_echoes_partial_stream(self):
        partial = json.dumps(STREAM[1])[:70]
        self.assertNotIn("SECRET", activity.diagnostic_tail(partial))
        self.assertEqual(activity.diagnostic_tail("plain failure text"), "plain failure text")

    def test_read_events_validates_field_types(self):
        lines = [
            {"seq": 0, "kind": "tool_use", "id": "a", "name": "Write", "input": "bad"},
            {"seq": 1, "kind": "tool_use", "id": "b", "name": "Bash", "input": {"command": 5, "timeout": 10}},
            {"seq": 2, "kind": "tool_result", "tool_use_id": "a", "is_error": "no"},
            {"seq": 3, "kind": "text", "text": ["x"]},
            {"seq": 4, "kind": "result", "denials": "bad", "num_turns": "3"},
        ]
        data = "\n".join(json.dumps(line) for line in lines) + '\n{"seq":5,"at":1e999,"kind":"text","text":"ok"}\n'
        data += '{"seq":6,"at":NaN,"kind":"text","text":"nan"}\n'
        events, malformed, _ = activity.read_events(data.encode())
        self.assertEqual(malformed, 4)  # Input "bad", non-bool is_error, list text, NaN literal.
        self.assertEqual(events[0]["input"], {"timeout": 10})  # Non-string command dropped.
        self.assertEqual((events[1]["denials"], events[1]["num_turns"]), ([], None))
        self.assertEqual((events[2]["text"], events[2]["at"]), ("ok", None))
        json.dumps(events, allow_nan=False)

    def test_scrub_stdout_removes_thinking(self):
        path = self.dir / "job.stdout"
        path.write_bytes(stream_bytes() + b"partial")
        activity.scrub_stdout(path)
        text = path.read_text()
        self.assertNotIn("SECRET", text)
        self.assertIn("Voilà", text)
        self.assertEqual(activity.final_result(text)["result"], "ok")
        legacy = self.dir / "legacy.stdout"
        legacy.write_text('{\n "result": "x"\n}')
        activity.scrub_stdout(legacy)
        self.assertEqual(legacy.read_text(), '{\n "result": "x"\n}')

    def test_outcomes_require_matching_success(self):
        events = [{"kind": "tool_use", "id": i, "name": "Edit"} for i in "abcd"] + [
            {"kind": "tool_result", "tool_use_id": "a", "is_error": False},
            {"kind": "tool_result", "tool_use_id": "b", "is_error": True},
            {"kind": "tool_result", "tool_use_id": "c", "is_error": True},
            {"kind": "result", "denials": [{"tool_use_id": "c"}]}]
        self.assertEqual(activity.tool_outcomes(events, [], active=True),
                         {"a": "succeeded", "b": "failed", "c": "denied", "d": "pending"})
        self.assertEqual(activity.tool_outcomes(events, [{"tool_use_id": "a"}], active=False)["a"], "denied")

    def test_stale_heartbeat_rule(self):
        job = {"status": "running", "created_at": 0, "heartbeat": 1000}
        self.assertEqual(activity.effective(job, now=1050)["status"], "running")
        self.assertEqual(activity.effective(job, now=1091)["status"], "interrupted")
        self.assertEqual(job["status"], "running")
        self.assertEqual(activity.effective({"status": "completed", "created_at": 0}, now=10 ** 9)["status"], "completed")
        self.assertEqual(activity.effective({"status": "queued", "heartbeat": "bad", "created_at": 0}, now=100)["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
