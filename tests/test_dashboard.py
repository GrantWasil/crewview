import contextlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import types
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dashboard", ROOT / "dashboard.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

A, B, LEGACY = (str(uuid.uuid4()) for _ in range(3))


def ev(seq, kind, **fields):
    return json.dumps({"seq": seq, "at": 100.0 + seq, "kind": kind, **fields}) + "\n"


class Fixture:
    def __init__(self, root):
        self.state = root / "state"
        (self.state / "jobs").mkdir(parents=True)
        (self.state / "conversations").mkdir()
        self.now = time.time()

    def job(self, conversation, created, status="completed", events=None, **fields):
        job_id = str(uuid.uuid4())
        record = {"job_id": job_id, "conversation_id": conversation, "cwd": "/work/project", "mode": "implement",
                  "prompt": f"Task at {created}\nDetails", "status": status, "created_at": created,
                  "heartbeat": created + 5, "finished_at": created + 5, "response": f"reply {created}",
                  "actual_models": ["claude-opus-5-5"], "num_turns": 2, "permission_denials": [], **fields}
        (self.state / "jobs" / (job_id + ".json")).write_text(json.dumps(record))
        if events is not None:
            (self.state / "jobs" / (job_id + ".events.jsonl")).write_text(events)
        return job_id


class DashboardDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(Path(self.tmp.name))
        now = self.fx.now
        events = (ev(0, "session", model="claude-opus-5-5") + ev(1, "text", text="Looking")
                  + ev(2, "tool_use", id="w1", name="Write", input={"file_path": "/work/project/a.py", "content": "print(1)\n"})
                  + ev(3, "tool_result", tool_use_id="w1", is_error=False, text="ok")
                  + ev(4, "tool_use", id="e1", name="Edit", input={"file_path": "/work/project/b.py", "old_string": "a", "new_string": "b"})
                  + ev(5, "tool_result", tool_use_id="e1", is_error=True, text="not found")
                  + ev(6, "tool_use", id="x1", name="Bash", input={"command": "pytest"})
                  + ev(7, "tool_result", tool_use_id="x1", is_error=False, text="3 passed")
                  + ev(8, "tool_use", id="d1", name="Write", input={"file_path": "/etc/hosts", "content": "no"})
                  + ev(9, "tool_result", tool_use_id="d1", is_error=True, text="denied")
                  + ev(10, "thinking", text="HIDDEN")
                  + '{"seq": 11, "kind": "text", "te')  # Being written right now.
        # Filenames sort differently from creation order; grouping must use timestamps.
        self.second = self.fx.job(A, now - 100, "needs_attention", events,
                                  permission_denials=[{"tool_name": "Write", "tool_use_id": "d1", "tool_input": {"file_path": "/etc/hosts"}}])
        self.first = self.fx.job(A, now - 200)
        self.fx.job(B, now - 50, "running", heartbeat=now - 500, finished_at=None, response=None)
        self.fx.job(LEGACY, now - 1000)
        (self.fx.state / "jobs" / (str(uuid.uuid4()) + ".json")).write_text('{"job_id": "trunc')
        (self.fx.state / "jobs" / (str(uuid.uuid4()) + ".json")).write_text("[]")
        (self.fx.state / "jobs" / "not-a-uuid.json").write_text("{}")
        (self.fx.state / "jobs" / (self.first + ".json.abc.tmp")).write_text("{}")
        self.dash = d.Dashboard(self.fx.state)

    def tearDown(self):
        self.tmp.cleanup()

    def test_grouped_chronological_history(self):
        listing = self.dash.conversations()
        self.assertEqual([c["conversation_id"] for c in listing["conversations"]], [B, A, LEGACY])
        self.assertEqual(listing["skipped_records"], 2)
        by_id = {c["conversation_id"]: c for c in listing["conversations"]}
        self.assertEqual(by_id[A]["request_count"], 2)
        self.assertEqual(by_id[A]["status"], "needs_attention")  # Latest job, not first.
        self.assertEqual(by_id[A]["title"], f"Task at {self.fx.now - 200}")
        self.assertEqual(by_id[B]["status"], "interrupted")  # Shared stale-heartbeat rule.
        self.assertTrue(by_id[A]["has_activity"])
        self.assertFalse(by_id[LEGACY]["has_activity"])
        detail = self.dash.conversation(A)
        self.assertEqual([t["job_id"] for t in detail["turns"]], [self.first, self.second])
        self.assertFalse(detail["turns"][0]["activity"]["available"])

    def test_contributions_count_only_successful_edits(self):
        detail = self.dash.conversation(A)
        k = detail["contributions"]
        self.assertEqual([f["path"] for f in k["files"]], ["/work/project/a.py"])
        self.assertEqual(k["applied_edit_count"], 1)
        self.assertEqual(sorted((e["tool_use_id"], e["status"]) for e in k["not_applied"]), [("d1", "denied"), ("e1", "failed")])
        self.assertEqual([(c["status"], c["output"]) for c in k["commands"]], [("succeeded", "3 passed")])
        self.assertEqual(k["denials"][0]["request"], 2)
        activity = detail["turns"][1]["activity"]
        self.assertEqual(activity["malformed"], 1)  # Unknown "thinking" kind is dropped.
        self.assertNotIn("HIDDEN", json.dumps(detail))

    def test_missing_state_is_empty_and_untouched(self):
        missing = Path(self.tmp.name) / "absent"
        listing = d.Dashboard(missing).conversations()
        self.assertEqual((listing["conversations"], listing["state_exists"]), ([], False))
        self.assertIsNone(d.Dashboard(missing).conversation(A))
        self.assertFalse(missing.exists())

    def add_corrupt(self):
        """Valid JSON with wrong field types, huge/non-finite numbers and a bad event."""
        bad = str(uuid.uuid4())
        job_id = self.fx.job(bad, self.fx.now - 20, actual_models=1, prompt=5, permission_denials="x", usage=[1],
                             num_turns="4", error={"nested": True}, cwd=["x"],
                             events=ev(0, "tool_use", id="w9", name="Write", input="bad")
                             + ev(1, "tool_result", tool_use_id="w9", is_error=False)
                             + ev(2, "tool_use", id="w8", name="Write", input={"file_path": 7, "content": "c"})
                             + ev(3, "tool_result", tool_use_id="w8", is_error=False))
        path = self.fx.state / "jobs" / (job_id + ".json")
        path.write_text(path.read_text().replace(f'"created_at": {self.fx.now - 20}', '"created_at": 1e999')
                        .replace('"heartbeat": ', '"heartbeat": -1, "x": ', 1))
        nan = str(uuid.uuid4())
        (self.fx.state / "jobs" / (nan + ".json")).write_text(json.dumps(
            {"job_id": nan, "conversation_id": bad, "status": "completed"}).replace('"completed"', '"completed", "finished_at": NaN'))
        return bad

    def test_corrupt_field_types_do_not_break_the_api(self):
        bad = self.add_corrupt()
        listing = self.dash.conversations()
        json.dumps(listing, allow_nan=False)
        ids = [c["conversation_id"] for c in listing["conversations"]]
        self.assertTrue({A, B, LEGACY, bad} <= set(ids))  # Healthy conversations still listed.
        self.assertEqual(listing["skipped_records"], 3)  # Two earlier fixtures plus the NaN record.
        entry = next(c for c in listing["conversations"] if c["conversation_id"] == bad)
        self.assertEqual((entry["actual_models"], entry["title"], entry["cwd"], entry["created_at"]),
                         ([], "Untitled request", "", None))
        detail = self.dash.conversation(bad)
        json.dumps(detail, allow_nan=False)
        turn = detail["turns"][0]
        self.assertEqual((turn["num_turns"], turn["permission_denials"], turn["usage"], turn["heartbeat"]), (None, [], {}, None))
        self.assertEqual(turn["activity"]["malformed"], 1)  # input="bad" dropped, its result is orphaned.
        k = detail["contributions"]
        self.assertEqual(k["applied_edit_count"], 1)
        self.assertEqual(k["files"][0]["path"], "(unknown file)")  # Non-string file_path discarded.
        self.assertEqual(self.dash.conversation(A)["contributions"]["applied_edit_count"], 1)

    def test_prompt_is_not_parsed_or_altered(self):
        self.fx.job(str(uuid.uuid4()), self.fx.now, prompt="<script>alert(1)</script>")
        detail = [c for c in self.dash.conversations()["conversations"] if c["title"].startswith("<script>")]
        self.assertEqual(detail[0]["title"], "<script>alert(1)</script>")


hspec = importlib.util.spec_from_file_location("history_fixture", Path(__file__).with_name("test_codex_history.py"))
hf = importlib.util.module_from_spec(hspec)
hspec.loader.exec_module(hf)


class DashboardTeamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.history = hf.Fixture(base).build()
        self.fx = Fixture(base)
        self.dash = d.Dashboard(self.fx.state, codex_root=self.history.root)
        self.at = hf.ch.parse_time("2026-09-24T17:20:05Z")

    def tearDown(self):
        self.tmp.cleanup()

    def conversation(self, record=None, **job):
        cid = str(uuid.uuid4())
        self.fx.job(cid, self.at, finished_at=self.at + 300, heartbeat=self.at + 300, **job)
        if record is not None:
            (self.fx.state / "conversations" / (cid + ".json")).write_text(json.dumps({"conversation_id": cid, **record}))
        return cid, self.dash.conversation(cid)

    def test_backfilled_record_link_merges_whole_team_chronologically(self):
        cid, detail = self.conversation({"codex_thread_id": hf.ROOT})
        team = detail["team"]
        self.assertTrue(team["linked"] and team["available"])
        self.assertEqual(detail["conversation"]["codex_thread_id"], hf.ROOT)
        names = {p["name"] for p in team["participants"]}
        self.assertTrue({"User", "Astra", "Sol", "Luna", "Opus"} <= names)
        opus = next(p for p in team["participants"] if p["key"] == "opus")
        self.assertEqual((opus["role"], opus["models"], opus["message_count"]), ("Implementer", ["claude-opus-5-5"], 1))
        kinds = [(i["kind"], i["name"]) for i in team["items"]]
        # Planning context before the bridge job, then Opus request/reply in time order among the agents.
        self.assertLess(kinds.index(("human", "User")), kinds.index(("opus_request", "Opus")))
        self.assertLess(kinds.index(("opus_request", "Opus")), kinds.index(("opus_reply", "Opus")))
        times = [i["at"] for i in team["items"]]
        self.assertEqual(times, sorted(times))
        self.assertEqual([r["name"] for r in team["reviews"]], ["Sol"])
        self.assertTrue(team["reviews"][0]["excerpt"].startswith("Review: two findings"))
        self.assertEqual((team["coordination"]["names"], team["coordination"]["handoffs"]), (["Astra", "Codex"], 2))
        dumped = json.dumps(detail, allow_nan=False)
        for secret in hf.SECRETS:
            self.assertNotIn(secret, dumped)
        self.assertTrue(self.dash.conversations()["conversations"][0]["linked"])

    def test_reviews_follow_review_role_not_model_name(self):
        impl = str(uuid.uuid4())
        self.history.write("17-21-00", impl, [hf.child("17:21:00", impl, hf.ROOT, "/root/sol_impl"),
                                              hf.row("17:21:01", "turn_context", {"model": "gpt-6-sol"}),
                                              hf.say("17:24:00", "Implemented the patch.")])
        _, detail = self.conversation({"codex_thread_id": hf.ROOT})
        team = detail["team"]
        implementer = next(p for p in team["participants"] if p.get("thread_id") == impl)
        self.assertEqual((implementer["name"], implementer["role"]), ("Sol", "Agent"))
        self.assertEqual([r["excerpt"].split(".")[0] for r in team["reviews"]], ["Review: two findings"])

    def test_job_level_link_is_used_when_record_has_none(self):
        _, detail = self.conversation({}, codex_thread_id=hf.ROOT)
        self.assertTrue(detail["team"]["available"])

    def test_configured_worker_is_attributed_by_requested_model(self):
        cid, detail = self.conversation({"codex_thread_id": hf.ROOT}, model="claude-sonnet-5",
                                        actual_models=["claude-sonnet-5"])
        team = detail["team"]
        self.assertEqual(detail["conversation"]["worker"], "Sonnet")
        worker = next(p for p in team["participants"] if p["key"] == "opus")
        self.assertEqual((worker["name"], worker["via"], worker["models"]), ("Sonnet", "Claude Code", ["claude-sonnet-5"]))
        self.assertEqual({i["name"] for i in team["items"] if i["kind"].startswith("opus_")}, {"Sonnet"})
        bridge = [i for i in team["items"] if i.get("bridge")]
        self.assertTrue(bridge and all(i["target_name"] == "Sonnet" for i in bridge))
        self.assertNotIn("Opus", {p["name"] for p in team["participants"]})
        listed = next(c for c in self.dash.conversations()["conversations"] if c["conversation_id"] == cid)
        self.assertEqual(listed["worker"], "Sonnet")

    def test_worker_name_defaults_to_configuration_and_mixed_models_read_as_claude(self):
        dash = d.Dashboard(self.fx.state, codex_root=self.history.root, worker_model="sonnet")
        cid, _ = self.conversation()  # Record without a requested model (older bridge versions).
        self.assertEqual(dash.conversation(cid)["conversation"]["worker"], "Sonnet")
        self.fx.job(cid, self.at + 400, model="claude-opus-5-5")
        self.assertEqual(dash.conversation(cid)["conversation"]["worker"], "Claude")
        custom, _ = self.conversation(model="claude-custom-worker")
        self.assertEqual(self.dash.conversation(custom)["conversation"]["worker"], "Claude")

    def test_unlinked_conversation_keeps_opus_view(self):
        _, detail = self.conversation()
        team = detail["team"]
        self.assertFalse(team["linked"])
        self.assertEqual(team["coverage"][0]["text"], "Codex history not linked; showing Claude worker history.")
        self.assertEqual([i["kind"] for i in team["items"]], ["opus_request", "opus_reply"])
        self.assertEqual(detail["turns"][0]["response"].split()[0], "reply")

    def test_missing_or_failing_history_never_breaks_detail(self):
        _, detail = self.conversation({"codex_thread_id": str(uuid.uuid4())})
        self.assertFalse(detail["team"]["available"])
        self.assertIn("wasn't found", detail["team"]["coverage"][0]["text"])
        broken = types.SimpleNamespace(feed=lambda link: 1 / 0)
        dash = d.Dashboard(self.fx.state, history=broken)
        cid, _ = self.conversation({"codex_thread_id": hf.ROOT})
        with contextlib.redirect_stderr(io.StringIO()):
            detail = dash.conversation(cid)
        self.assertIn("couldn't be read", detail["team"]["coverage"][0]["text"])
        self.assertEqual(len(detail["turns"]), 1)


class Raw:
    def __init__(self, data):
        self.data = data

    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.data)


class DashboardHttpTest(unittest.TestCase):
    """Drives the real request handler over a socketpair, so no port is needed."""

    port = 8767

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.fx = Fixture(Path(cls.tmp.name))
        cls.fx.job(A, time.time() - 10)
        cls.server = types.SimpleNamespace(server_address=("127.0.0.1", cls.port), dashboard=d.Dashboard(cls.fx.state),
                                           state_id=d.state_id(cls.fx.state))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def request(self, path, method="GET", headers=None, host=None):
        lines = [f"{method} {path} HTTP/1.1", f"Host: {host or f'127.0.0.1:{self.port}'}"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        client, served = socket.socketpair()
        client.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        def run():
            try:
                d.Handler(served, ("127.0.0.1", 50000), self.server)
            finally:
                served.close()
        thread = threading.Thread(target=run)
        thread.start()
        chunks = []
        while chunk := client.recv(65536):
            chunks.append(chunk)
        thread.join(5)
        client.close()
        response = http.client.HTTPResponse(Raw(b"".join(chunks)), method=method)
        response.begin()
        return response, response.read()

    def test_pages_and_api(self):
        response, body = self.request("/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"<h1>Crewview</h1>", body)
        csp = response.getheader("Content-Security-Policy")
        self.assertIn("script-src 'self'", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        for asset in ("/app.js", "/app.css"):
            self.assertEqual(self.request(asset)[0].status, 200)
        response, body = self.request("/api/conversations", headers={"Origin": f"http://127.0.0.1:{self.port}", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["conversations"][0]["conversation_id"], A)
        tag = response.getheader("ETag")
        self.assertEqual(self.request("/api/conversations", headers={"If-None-Match": tag})[0].status, 304)
        response, body = self.request(f"/api/conversations/{A}", host=f"localhost:{self.port}")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body)["turns"][0]["response"].split()[0], "reply")
        health = json.loads(self.request("/api/health")[1])
        self.assertEqual((health["app"], health["state_id"]), (d.APP, d.state_id(self.fx.state)))

    def test_corrupt_record_does_not_break_http_api(self):
        bad = str(uuid.uuid4())
        self.fx.job(bad, time.time(), actual_models=1, created_at="soon")
        try:
            for path in ("/api/conversations", f"/api/conversations/{bad}", f"/api/conversations/{A}"):
                self.assertEqual(self.request(path)[0].status, 200, path)
        finally:
            for p in (self.fx.state / "jobs").glob("*.json"):
                if json.loads(p.read_text())["conversation_id"] == bad:
                    p.unlink()

    def test_host_origin_and_fetch_metadata_checks(self):
        for host in ("evil.example", f"127.0.0.1:{self.port + 1}", "127.0.0.1", f"attacker.test:{self.port}"):
            self.assertEqual(self.request("/api/conversations", host=host)[0].status, 403, host)
        for origin in ("http://evil.example", f"https://127.0.0.1:{self.port}", "null", f"http://localhost:{self.port}"):
            self.assertEqual(self.request("/api/conversations", headers={"Origin": origin})[0].status, 403, origin)
        self.assertEqual(self.request("/", headers={"Sec-Fetch-Site": "cross-site"})[0].status, 403)
        self.assertEqual(self.request("/", headers={"Sec-Fetch-Site": "same-site"})[0].status, 403)
        self.assertEqual(self.request("/", headers={"Sec-Fetch-Site": "none"})[0].status, 200)

    def test_paths_and_methods(self):
        for path in ("/api/conversations/../../etc/passwd", "/api/conversations/" + A.upper(), "/api/conversations/" + A + "/",
                     "/web/app.js", "/../server.py", "/server.py", "/api/conversations/not-a-uuid", "/%2e%2e/activity.py",
                     "/api/jobs/" + A, "/index.html"):
            self.assertEqual(self.request(path)[0].status, 404, path)
        self.assertEqual(self.request("/api/conversations/" + str(uuid.uuid4()))[0].status, 404)
        self.assertEqual(self.request("http://127.0.0.1/")[0].status, 400)
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            response, _ = self.request("/api/conversations", method=method)
            self.assertEqual(response.status, 405, method)
            self.assertEqual(response.getheader("Allow"), "GET, HEAD")



class DashboardSocketTest(unittest.TestCase):
    """Real loopback listener; skipped where the sandbox forbids binding ports."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Fixture(Path(self.tmp.name)).state
        try:
            self.server = d.make_server(self.state, 0)
        except PermissionError as error:
            self.tmp.cleanup()
            self.skipTest(f"local port binding not permitted here: {error}")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_loopback_listener_and_second_instance(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/health")
        self.assertEqual(json.loads(conn.getresponse().read())["app"], d.APP)
        conn.close()
        # A second launch reuses only this dashboard for this state; it never takes over the port.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(d.main(["--state", str(self.state), "--port", str(self.port)]), 0)
            self.assertEqual(d.main(["--state", str(Path(self.tmp.name) / "other"), "--port", str(self.port)]), 1)


if __name__ == "__main__":
    unittest.main()
