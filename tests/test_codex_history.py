import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

ROOT_DIR = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("codex_history", ROOT_DIR / "codex_history.py")
ch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch)

# Synthetic thread IDs; they contain letters so the uppercase form is a distinct, invalid ID.
ROOT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SOL = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LUNA, OTHER, GUARD, SPOOF, MISMATCH, NOTSUB, DUP = (str(uuid.uuid4()) for _ in range(7))
SECRETS = ("SECRET", "UNRELATED", "GUARDIAN", "SPOOFED", "MISMATCHED", "NOTSUB", "DUPLICATE", "UNPHASED",
           "oai-mem-citation", "gAAAA")


def at(clock):
    return f"2026-09-24T{clock}Z"


def row(clock, kind, payload, **top):
    return {"timestamp": at(clock), "type": kind, "payload": payload, **top}


def say(clock, text, phase="final_answer", role="assistant", block="output_text"):
    payload = {"type": "message", "role": role, "content": [{"type": block, "text": text}]}
    if phase:
        payload["phase"] = phase
    return row(clock, "response_item", payload)


def call(clock, name, arguments, kind="function_call"):
    return row(clock, "response_item", {"type": kind, "name": name, "call_id": "c" + clock, "arguments": json.dumps(arguments)})


def meta(clock, thread_id, **fields):
    return row(clock, "session_meta", {"id": thread_id, "session_id": ROOT, "cwd": "/work", **fields})


def child(clock, thread_id, parent, path, **fields):
    return meta(clock, thread_id, parent_thread_id=parent, thread_source="subagent", agent_path=path,
                source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}}, **fields)


class Fixture:
    def __init__(self, base):
        self.root = base / "sessions"
        self.day = self.root / "2026/09/24"
        self.day.mkdir(parents=True)

    def write(self, stamp, file_id, rows, tail=""):
        path = self.day / f"rollout-2026-09-24T{stamp}-{file_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows) + tail)
        return path

    def build(self):
        self.root_path = self.write("16-56-00", ROOT, [
            meta("16:56:00.000", ROOT, thread_source="user", source="vscode",
                 base_instructions={"text": "SECRET-INSTRUCTIONS", "provenance": {"model": "gpt-6-astra"}}),
            row("16:56:01", "turn_context", {"model": "gpt-6-astra", "cwd": "/work"}),
            say("16:56:02", None, None, "user") | {"payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions for /work\n\n<INSTRUCTIONS>SECRET-AGENTS</INSTRUCTIONS>"},
                {"type": "input_text", "text": "<environment_context>\n  <cwd>/work</cwd>\n</environment_context>"},
                {"type": "input_text", "text": "<recommended_plugins>\n- SECRET-PLUGIN\n</recommended_plugins>"},
                {"type": "input_text", "text": "<in_app_browser_context>SECRET-BROWSER</in_app_browser_context>\nPlease add the team view"}]}},
            say("16:56:03", "SECRET-DEVELOPER", None, "developer", "input_text"),
            row("16:56:04", "response_item", {"type": "reasoning", "summary": [{"type": "summary_text", "text": "SECRET-REASONING"}],
                                              "encrypted_content": "gAAAAAreasoning"}),
            say("16:56:05", "Planning the work.", "commentary"),
            say("16:56:05.5", "SECRET-ANALYSIS", "analysis"),
            row("16:56:05.6", "event_msg", {"type": "agent_message", "message": "Planning the work."}),
            call("16:56:06", "exec", {"cmd": "SECRET-SHELL"}),
            row("16:56:06.5", "response_item", {"type": "function_call_output", "call_id": "c1", "output": "SECRET-OUTPUT"}),
            call("16:56:07", "apply_patch", {"patch": "SECRET-PATCH"}, kind="custom_tool_call"),
            call("17:16:13", "spawn_agent", {"target": "sol_review", "model": "gpt-6-sol", "message": "gAAAAAencrypted"}),
            call("17:20:00", "mcp__claude-bridge__claude_start", {"prompt": "SECRET-PROMPT", "cwd": "/work"}),
            say("17:29:00", "UNPHASED prose without a public phase", None),
            say("17:30:00", 'All done.<oai-mem-citation source="SECRET-MEMORY">SECRET-CITATION</oai-mem-citation> Sol approved.'),
            row("17:31:00", "turn_context", {"model": "gpt-7-mystery"}),
            say("17:32:00", "Reply after a model switch."),
        ])
        self.sol_path = self.write("17-16-14", SOL, [
            child("17:16:14", SOL, ROOT, "/root/sol_review", agent_nickname="Pasteur"),
            row("17:16:15", "turn_context", {"model": "gpt-6-sol"}),
            say("17:16:16", "SECRET-CHILD-SCAFFOLD task text", None, "user", "input_text"),
            say("17:17:00", "Reviewing the diff.", "commentary"),
            say("17:17:30", "gAAAAA" + "x" * 300, "final_answer"),
            call("17:18:00", "spawn_agent", {"agent_path": "luna_helper", "model": "gpt-6-luna"}),
            say("17:25:00", "Review: two findings.\n- Fix A\n- Fix B"),
        ], tail='not json\n{"timestamp": NaN}\n{"timestamp":"2026-09-24T17:26:00Z","type":"response_item","payload":{"type":"message",'
                '"role":"assistant","phase":"final_answer","content":[{"type":"output_text","text":"Late ')
        self.write("17-18-00", LUNA, [child("17:18:00", LUNA, SOL, "/root/sol_review/luna_helper"),
                                       row("17:18:01", "turn_context", {"model": "gpt-6-luna"}),
                                       say("17:22:00", "Luna checked the edge case."),
                                       row("17:22:30", "turn_context", {"model": 5}),  # Malformed: model unknown.
                                       say("17:23:00", "Follow-up from an unknown model.")])
        self.write("17-00-00", OTHER, [meta("17:00:00", OTHER, thread_source="user"), say("17:01:00", "UNRELATED same cwd")])
        self.write("17-19-00", GUARD, [child("17:19:00", GUARD, ROOT, "/root/guardian_review",
                                             agent_nickname="guardian"), say("17:19:30", "GUARDIAN verdict")])
        self.write("17-19-10", SPOOF, [meta("17:19:10", SPOOF, parent_thread_id=ROOT, thread_source="subagent",
                                            source={"subagent": {"thread_spawn": {"parent_thread_id": OTHER}}}),
                                       say("17:19:20", "SPOOFED parent")])
        self.write("17-19-40", MISMATCH, [child("17:19:40", LUNA, ROOT, "/root/fake"), say("17:19:41", "MISMATCHED id")])
        self.write("17-19-50", NOTSUB, [meta("17:19:50", NOTSUB, parent_thread_id=ROOT, thread_source="user"),
                                        say("17:19:51", "NOTSUB root claims parent")])
        return self


class CodexHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name)).build()
        self.now = [1000.0]
        self.history = ch.CodexHistory(self.fx.root, clock=lambda: self.now[0])

    def tearDown(self):
        os.chmod(self.fx.sol_path, 0o600)
        self.tmp.cleanup()

    def people(self, feed):
        return {p["key"].split(":")[-1] if p["key"] == "user" else (p["thread_id"], p["name"]): p for p in feed["participants"]}

    def test_tree_participants_and_model_attribution(self):
        feed = self.history.feed(ROOT)
        self.assertTrue(feed["available"])
        self.assertEqual(feed["session_count"], 3)  # Root, Sol child, Luna grandchild only.
        people = self.people(feed)
        astra, sol, luna = people[(ROOT, "Astra")], people[(SOL, "Sol")], people[(LUNA, "Luna")]
        self.assertEqual((astra["role"], astra["models"]), ("Orchestrator", ["gpt-6-astra"]))
        self.assertEqual((sol["role"], sol["nickname"], sol["models"], sol["message_count"]), ("Reviewer", "Pasteur", ["gpt-6-sol"], 2))
        # Luna's path is under /root/sol_review but its own leaf is luna_helper: not a reviewer.
        self.assertEqual((luna["role"], luna["models"], luna["message_count"]), ("Helper", ["gpt-6-luna"], 1))
        unknown = people[(LUNA, "Codex")]  # A turn with a malformed model is not attributed to Luna.
        self.assertEqual((unknown["models"], unknown["message_count"]), ([], 1))
        switched = people[(ROOT, "Codex")]  # Unknown model family: plain Codex, never an invented role name.
        self.assertEqual(switched["models"], ["gpt-7-mystery"])
        self.assertEqual(people["user"]["message_count"], 1)
        last = [i for i in feed["items"] if i["kind"] == "agent_message"][-1]
        self.assertEqual((last["name"], last["model"], last["text"]), ("Codex", "gpt-7-mystery", "Reply after a model switch."))

    def test_only_public_prose_and_clean_human_request(self):
        feed = self.history.feed(ROOT)
        dumped = json.dumps(feed)
        for secret in SECRETS:
            self.assertNotIn(secret, dumped, secret)
        humans = [i["text"] for i in feed["items"] if i["kind"] == "human"]
        self.assertEqual(humans, ["Please add the team view"])
        texts = [i["text"] for i in feed["items"] if i["kind"] == "agent_message"]
        self.assertEqual(texts.count("Planning the work."), 1)  # event_msg duplicate ignored.
        self.assertIn("Review: two findings.\n- Fix A\n- Fix B", texts)
        self.assertIn("All done. Sol approved.", texts)  # Citation metadata stripped, prose kept.
        tools = [i for i in feed["items"] if i["kind"] == "tools"]
        self.assertEqual(tools[0]["counts"], {"exec": 1, "apply_patch": 1})

    def test_handoffs_are_markers_without_payloads(self):
        feed = self.history.feed(ROOT)
        handoffs = [(i["name"], i.get("target_name"), i["tool"], i["encrypted"]) for i in feed["items"] if i["kind"] == "handoff"]
        self.assertEqual(handoffs, [("Astra", "Sol", "spawn_agent", True), ("Sol", "Luna", "spawn_agent", False),
                                    ("Astra", None, "claude_start", False)])
        bridge = [i for i in feed["items"] if i.get("bridge")][0]
        self.assertEqual(bridge["target"], "Claude worker")
        coverage = " ".join(n["text"] for n in feed["coverage"])
        self.assertIn("encrypted", coverage)
        self.assertIn("inconsistent parent metadata", coverage)
        self.assertIn("Skipped 2 unreadable", coverage)

    def test_chronological_and_stable_ids(self):
        feed = self.history.feed(ROOT)
        times = [i["at"] for i in feed["items"]]
        self.assertEqual(times, sorted(times))
        ids = [i["id"] for i in feed["items"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [i["id"] for i in self.history.feed(ROOT)["items"]])

    def test_partial_trailing_line_is_read_once_complete(self):
        before = self.history.feed(ROOT)
        self.assertNotIn("Late note", json.dumps(before))
        with self.fx.sol_path.open("a") as stream:
            stream.write('note"}]}}\n')
        after = self.history.feed(ROOT)
        self.assertEqual([i for i in after["items"] if i["kind"] == "agent_message"][-1]["text"], "Reply after a model switch.")
        late = [i for i in after["items"] if i.get("text") == "Late note"]
        self.assertEqual((len(late), late[0]["name"]), (1, "Sol"))
        self.assertTrue({i["id"] for i in before["items"]} <= {i["id"] for i in after["items"]})

    def test_metadata_index_refreshes_on_interval(self):
        self.history.feed(ROOT)
        new = str(uuid.uuid4())
        self.fx.write("17-40-00", new, [child("17:40:00", new, ROOT, "/root/luna_task"),
                                         row("17:40:01", "turn_context", {"model": "gpt-6-luna"}), say("17:41:00", "Late helper")])
        self.assertNotIn("Late helper", json.dumps(self.history.feed(ROOT)))
        self.now[0] += ch.INDEX_TTL + 1
        self.assertIn("Late helper", json.dumps(self.history.feed(ROOT)))

    def test_duplicate_thread_ids_are_not_trusted(self):
        self.fx.write("17-50-00", SOL, [child("17:50:00", SOL, ROOT, "/root/sol_review"), say("17:50:01", "DUPLICATE")])
        feed = self.history.feed(ROOT)
        self.assertNotIn("DUPLICATE", json.dumps(feed))
        self.assertNotIn(SOL, {i["thread_id"] for i in feed["items"]})

    def test_bounds_and_unreadable_logs(self):
        with mock.patch.object(ch, "MAX_LINE", 200), mock.patch.object(ch, "CHUNK", 64):
            feed = ch.CodexHistory(self.fx.root).feed(ROOT)
        self.assertIn("oversized", " ".join(n["text"] for n in feed["coverage"]))
        with mock.patch.object(ch, "MAX_ITEMS", 3):
            feed = ch.CodexHistory(self.fx.root).feed(ROOT)
        self.assertIn("reading limit", " ".join(n["text"] for n in feed["coverage"]))
        self.history.feed(ROOT)
        with self.fx.sol_path.open("a") as stream:
            stream.write("\n")
        os.chmod(self.fx.sol_path, 0)  # Read-restricted after indexing.
        feed = self.history.feed(ROOT)
        self.assertTrue(feed["available"])
        self.assertIn("Please add the team view", json.dumps(feed))
        if not os.access(self.fx.sol_path, os.R_OK):
            self.assertIn("couldn't be read", " ".join(n["text"] for n in feed["coverage"]))

    def test_missing_or_invalid_root(self):
        for thread in (str(uuid.uuid4()), "not-a-uuid", ROOT.upper()):
            feed = self.history.feed(thread)
            self.assertFalse(feed["available"])
            self.assertIn("wasn't found", feed["coverage"][0]["text"])

    def test_partial_first_lines_are_retried_on_the_same_inode(self):
        late = str(uuid.uuid4())
        first = json.dumps(child("17:45:00", late, ROOT, "/root/luna_check")) + "\n"
        path = self.fx.write("17-45-00", late, [], tail=first[:40])  # Metadata still being written.
        self.history.feed(ROOT)
        inode = path.stat().st_ino
        with path.open("a") as stream:
            stream.write(first[40:] + json.dumps(row("17:45:01", "turn_context", {"model": "gpt-6-luna"})) + "\n"
                         + json.dumps(say("17:46:00", "Live check result")) + "\n")
        self.assertEqual(path.stat().st_ino, inode)
        self.now[0] += ch.INDEX_TTL + 1
        feed = self.history.feed(ROOT)
        found = [p for p in feed["participants"] if p.get("thread_id") == late]
        self.assertEqual((found[0]["name"], found[0]["role"]), ("Luna", "Checker"))
        new_root = str(uuid.uuid4())
        line = json.dumps(meta("18:00:00", new_root, thread_source="user")) + "\n"
        root_path = self.fx.write("18-00-00", new_root, [], tail=line[:30])
        self.assertFalse(self.history.feed(new_root)["available"])
        with root_path.open("a") as stream:
            stream.write(line[30:] + json.dumps(say("18:00:01", "Root is live")) + "\n")
        self.assertIn("Root is live", json.dumps(self.history.feed(new_root)))

    def test_linked_guardian_root_is_rejected(self):
        feed = self.history.feed(GUARD)
        self.assertFalse(feed["available"])
        self.assertIn("internal review", feed["coverage"][0]["text"])
        self.assertNotIn("GUARDIAN", json.dumps(feed))

    def test_metadata_read_is_first_line_only_and_bounded(self):
        path = self.fx.write("18-10-00", OTHER, [meta("18:10:00", OTHER, thread_source="user")], tail="x" * 5000)
        with mock.patch.object(Path, "open", wraps=path.open) as opened:
            self.assertEqual(ch.read_meta(path)["id"], OTHER)
        opened.assert_called_once()
        with mock.patch.object(ch, "META_BYTES", 40):
            self.assertIsNone(ch.read_meta(path))  # First line longer than the limit is rejected.
        no_newline = self.fx.write("18-11-00", NOTSUB, [], tail=json.dumps(meta("18:11:00", NOTSUB)))
        self.assertIsNone(ch.read_meta(no_newline))

    def test_role_labels_come_from_the_agents_own_metadata(self):
        self.assertEqual(ch.role_label({"path": "/root/sol_review"}), "Reviewer")
        self.assertEqual(ch.role_label({"path": "/root/sol_review/luna_check"}), "Checker")
        self.assertEqual(ch.role_label({"path": "/root/sol_review/worker"}), "Agent")
        self.assertEqual(ch.role_label({"path": "/root/x", "role": "code_writer"}), "Code Writer")
        self.assertEqual(ch.role_label({"path": "/root/x", "role": "reviewer"}), "Reviewer")

    def test_public_phase_allowlist(self):
        for phase, expected in (("commentary", "commentary"), ("final_answer", "final"), ("final", "final"),
                                (None, None), ("analysis", None), ("summary", None), (5, None)):
            payload = {"phase": phase} if phase is not None else {}
            self.assertEqual(ch.phase_of(payload), expected, phase)
        self.assertEqual(ch.phase_of({"channel": "final"}), "final")
        self.assertEqual(ch.output_text([{"type": "output_text", "text": 'Done<oai-mem-citation id="1"/>.'}]), "Done.")

    def test_user_scaffolding_and_timestamps(self):
        content = [{"type": "input_text", "text": "# Context from my IDE setup:\n## Open tabs: x\n\n## My request for Codex:\nShip it"},
                   {"type": "input_text", "text": "<permissions instructions>SECRET</permissions instructions>"},
                   {"type": "input_image", "image_url": "data:"}]
        self.assertEqual(ch.clean_user(content), "Ship it")
        self.assertEqual(ch.clean_user([{"type": "input_text", "text": "<b>keep</b> this"}]), "<b>keep</b> this")
        # One block combining AGENTS.md, environment, plugins, browser context and the actual request.
        combined = ("# AGENTS.md instructions for /work\n\n<INSTRUCTIONS>\nSECRET rules\n</INSTRUCTIONS>\n"
                    "<environment_context>\n  <cwd>/work</cwd>\n</environment_context>\n"
                    "<recommended_plugins>SECRET plugin list</recommended_plugins>\n"
                    "<in_app_browser_context>SECRET page</in_app_browser_context>\n\n"
                    "## My request:\nInclude Astra and Sol in the dashboard.")
        self.assertEqual(ch.clean_user([{"type": "input_text", "text": combined}]), "Include Astra and Sol in the dashboard.")
        no_marker = combined.replace("## My request:\n", "")
        self.assertEqual(ch.clean_user([{"type": "input_text", "text": no_marker}]), "Include Astra and Sol in the dashboard.")
        self.assertAlmostEqual(ch.parse_time("2026-09-24T17:16:14.5Z"), ch.parse_time("2026-09-24T19:16:14.5+02:00"))
        for bad in ("2026-13-40T00:00:00Z", "yesterday", 5, float("nan"), "9999-12-31T23:59:59Z"):
            self.assertIsNone(ch.parse_time(bad), bad)
        self.assertEqual(ch.agent_name("gpt-6-sol"), "Sol")
        self.assertEqual(ch.agent_name("console-model"), "Codex")
        self.assertIsNone(ch.safe_label("gAAAAAsecret"))


if __name__ == "__main__":
    unittest.main()
