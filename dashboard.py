#!/usr/bin/env python3
"""Crewview: read-only local dashboard for Codex → Claude Code worker conversations.

Serves a static UI and a small JSON API on 127.0.0.1 only. It reads the bridge's
own job records and sanitized activity logs; it never starts, cancels or replies
to jobs, calls a model, or checks the subscription.
"""
import argparse
import errno
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid
import webbrowser

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import activity  # noqa: E402
import codex_history  # noqa: E402
import settings  # noqa: E402

APP = "crewview-dashboard"
HOST = "127.0.0.1"
DEFAULT_PORT = 8767
DEFAULT_STATE = settings.state_dir()
WEB = HERE / "web"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8")}
DETAIL = re.compile(r"^/api/conversations/([0-9a-f-]{36})$")
MAX_JOB_BYTES = 32 * 1024 * 1024
MAX_EVENT_BYTES = activity.MAX_LOG_BYTES + 1024 * 1024
INVALID = object()
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def canonical(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def state_id(state):
    return hashlib.sha256(str(Path(state).expanduser().resolve()).encode()).hexdigest()[:16]


def text(value, limit):
    if value is None:
        return None
    return activity.clip(value, limit)[0]


def integer(value):
    return value if type(value) is int else None


def string(value, limit):
    return value[:limit] if isinstance(value, str) else None


def timestamp(value):
    """Finite epoch seconds in a range browsers can format; otherwise None."""
    value = activity.number(value)
    return value if value is not None and 0 <= value <= 1e11 else None


def parse_json(data):
    try:
        value = activity.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return INVALID
    return value if isinstance(value, dict) else INVALID


JOB_STRINGS = {"cwd": 4000, "mode": 40, "prompt": 200000, "response": 400000, "model_warning": 1000,
               "model": 200, "session_id": 200}


def normalize_job(value, stem):
    """Validate a job record at the read boundary; every field has a known type afterwards."""
    if (value is INVALID or value.get("job_id") != stem or not canonical(value.get("conversation_id"))
            or not isinstance(value.get("status"), str)):
        return INVALID
    job = {"job_id": stem, "conversation_id": value["conversation_id"], "status": value["status"][:40]}
    job.update({key: string(value.get(key), limit) for key, limit in JOB_STRINGS.items()})
    job.update({key: timestamp(value.get(key)) for key in ("created_at", "heartbeat", "finished_at")})
    job.update({key: integer(value.get(key)) for key in ("num_turns", "exit_code", "timeout_seconds", "max_turns")})
    error = value.get("error")
    job["error"] = text(error, 6000) if isinstance(error, (str, list, dict)) and error else None
    models = value.get("actual_models")
    job["actual_models"] = [m[:200] for m in models[:10] if isinstance(m, str)] if isinstance(models, list) else []
    denials = value.get("permission_denials")
    job["permission_denials"] = [d for d in denials[:100] if isinstance(d, dict)] if isinstance(denials, list) else []
    job["usage"] = value["usage"] if isinstance(value.get("usage"), dict) else {}
    job["cost_estimate_usd"] = activity.number(value.get("cost_estimate_usd"))
    job["codex_thread_id"] = value["codex_thread_id"] if canonical(value.get("codex_thread_id")) else None
    return job


def thread_link(record, jobs):
    """The explicit Codex thread link: conversation record first (may be backfilled), then jobs."""
    if canonical(record.get("codex_thread_id")):
        return record["codex_thread_id"]
    return next((j["codex_thread_id"] for j in jobs if j["codex_thread_id"]), None)


class Store:
    """Thread-safe, stat-keyed cache over the bridge state directory (read-only)."""

    def __init__(self, state):
        self.state = Path(state)
        self.lock = threading.Lock()
        self.cache = {}

    def cached(self, path, limit, loader):
        try:
            info = path.stat()
        except OSError:
            self.cache.pop(path, None)
            return None
        key = (info.st_mtime_ns, info.st_size, info.st_ino)
        hit = self.cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        try:
            value = INVALID if info.st_size > limit else loader(path.read_bytes())
        except OSError:
            return None
        self.cache[path] = (key, value)
        return value

    def jobs(self):
        """Return ({conversation_id: [jobs]}, skipped_count)."""
        grouped, skipped = {}, 0
        try:
            paths = sorted((self.state / "jobs").glob("*.json"))
        except OSError:
            paths = []
        live = set()
        for path in paths:
            if not canonical(path.stem):
                continue
            live.add(path)
            job = self.cached(path, MAX_JOB_BYTES, lambda data, stem=path.stem: normalize_job(parse_json(data), stem))
            if job is None:
                continue  # Removed between listing and reading.
            if job is INVALID:
                skipped += 1
                continue
            grouped.setdefault(job["conversation_id"], []).append(job)
        for path in [p for p in self.cache if p.suffix == ".json" and p.parent.name == "jobs" and p not in live]:
            self.cache.pop(path, None)
        for jobs in grouped.values():
            jobs.sort(key=lambda j: (j["created_at"] or 0, j["job_id"]))
        return grouped, skipped

    def events(self, job_id):
        path = self.state / "jobs" / (job_id + ".events.jsonl")
        value = self.cached(path, MAX_EVENT_BYTES, lambda data: activity.read_events(data))
        if value is None:
            return None
        if value is INVALID:
            return [], 0, True
        return value

    def conversation_record(self, conversation_id):
        value = self.cached(self.state / "conversations" / (conversation_id + ".json"), 1024 * 1024, parse_json)
        return value if isinstance(value, dict) else {}


def title_of(prompt):
    for line in (prompt or "").splitlines():
        line = line.strip().lstrip("#>*- ").strip()
        if line:
            return line[:120] + ("…" if len(line) > 120 else "")
    return "Untitled request"


def timestamps(job):
    return [t for t in (activity.number(job.get(k)) for k in ("created_at", "heartbeat", "finished_at")) if t]


def denials_view(denials):
    out = []
    for d in (denials if isinstance(denials, list) else [])[:100]:
        if not isinstance(d, dict):
            continue
        tool_input = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
        out.append({"tool_name": string(d.get("tool_name"), 100) or "unknown",
                    "tool_use_id": string(d.get("tool_use_id"), 200),
                    "input": {str(k)[:100]: v[:2000] for k, v in list(tool_input.items())[:8] if isinstance(v, str)}})
    return out


def usage_view(usage):
    usage = usage if isinstance(usage, dict) else {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: usage[k] for k in keys if integer(usage.get(k)) is not None}


def worker_name(jobs, default_model):
    """Display name from the models these jobs requested; mixed families read as 'Claude'."""
    names = {settings.worker_family(j["model"] or default_model) for j in jobs}
    return names.pop() if len(names) == 1 else "Claude"


def summary(conversation_id, jobs, record, now, store, default_model=settings.DEFAULT_MODELS["worker"]):
    first = jobs[0]
    latest = activity.effective(jobs[-1], now)
    cwd = latest["cwd"] or string(record.get("cwd"), 4000) or ""
    models = sorted({m for j in jobs for m in j["actual_models"]})
    stamps = [t for j in jobs for t in timestamps(j)]
    return {"conversation_id": conversation_id, "title": title_of(first["prompt"]),
            "snippet": (first["prompt"] or "")[:300], "project": Path(cwd).name if cwd else "",
            "cwd": cwd, "mode": latest["mode"] or string(record.get("mode"), 40) or "",
            "status": latest["status"], "created_at": first["created_at"],
            "updated_at": max(stamps) if stamps else None, "request_count": len(jobs), "actual_models": models,
            "worker": worker_name(jobs, default_model), "latest_job_id": latest["job_id"], "linked": thread_link(record, jobs) is not None,
            "has_activity": any(store.events(j["job_id"]) is not None for j in jobs)}


def job_view(job, now, store):
    job = activity.effective(job, now)
    status = job["status"]
    logged = store.events(job["job_id"])
    events, malformed, truncated = logged if logged is not None else ([], 0, False)
    outcomes = activity.tool_outcomes(events, job["permission_denials"], status in activity.ACTIVE)
    return {"job_id": job["job_id"], "status": status,
            "created_at": job["created_at"], "finished_at": job["finished_at"], "heartbeat": job["heartbeat"],
            "prompt": job["prompt"] or "", "response": job["response"],
            "error": job["error"], "model_warning": job["model_warning"], "requested_model": job["model"],
            "actual_models": job["actual_models"], "num_turns": job["num_turns"], "exit_code": job["exit_code"],
            "timeout_seconds": job["timeout_seconds"], "max_turns": job["max_turns"],
            "session_id": job["session_id"], "mode": job["mode"],
            "usage": usage_view(job["usage"]), "cost_estimate_usd": job["cost_estimate_usd"],
            "permission_denials": denials_view(job["permission_denials"]),
            "activity": {"available": logged is not None, "events": events, "outcomes": outcomes,
                         "malformed": malformed, "truncated": truncated}}


def contributions(turns):
    """Recorded edits/commands across a conversation; only matched successes are 'applied'."""
    edits, commands, other, tools = [], [], [], {}
    for index, turn in enumerate(turns, 1):
        outcomes = turn["activity"]["outcomes"]
        results = {e.get("tool_use_id"): e for e in turn["activity"]["events"] if e.get("kind") == "tool_result"}
        for event in turn["activity"]["events"]:
            if event.get("kind") != "tool_use":
                continue
            name, status = event.get("name"), outcomes.get(event.get("id"), "no_result")
            tools[name] = tools.get(name, 0) + 1
            base = {"request": index, "job_id": turn["job_id"], "tool_use_id": event.get("id"), "tool": name,
                    "status": status, "at": event.get("at"), "input": event.get("input") or {},
                    "truncated": event.get("truncated") or []}
            result = results.get(event.get("id")) or {}
            if name in activity.EDIT_TOOLS:
                (edits if status == "succeeded" else other).append(base)
            elif name == "Bash":
                commands.append({**base, "output": result.get("text"), "output_truncated": bool(result.get("truncated"))})
    denials = [{**d, "request": i} for i, t in enumerate(turns, 1) for d in t["permission_denials"]]
    files = {}
    for edit in edits:
        files.setdefault(edit["input"].get("file_path") or "(unknown file)", []).append(edit)
    return {"files": [{"path": p, "edits": e} for p, e in sorted(files.items(), key=lambda kv: kv[1][0]["at"] or 0)],
            "applied_edit_count": len(edits), "not_applied": other, "commands": commands, "tool_counts": tools,
            "denials": denials, "requests_with_activity": sum(1 for t in turns if t["activity"]["available"]),
            "request_count": len(turns)}


def team_view(turns, head, link, feed):
    """Merge linked Codex history (public messages only) with the bridge's Claude worker turns.

    The worker keeps the internal key "opus" (and opus_* item kinds) for API
    compatibility; its display name comes from the models its jobs requested.
    """
    coverage, items, participants = [], [], []
    worker = head["worker"]
    if not link:
        coverage.append({"level": "info", "text": "Codex history not linked; showing Claude worker history."})
    else:
        coverage += feed["coverage"]
        items += [{**i, "target_name": worker} if i.get("bridge") else i for i in feed["items"]]
        participants += feed["participants"]
    replies = sum(1 for t in turns if t["response"])
    participants.append({"key": "opus", "name": worker, "role": "Consultant" if head["mode"] == "consult" else "Implementer",
                         "models": sorted({m for t in turns for m in t["actual_models"]}), "via": "Claude Code",
                         "message_count": replies, "final_count": replies})
    for index, turn in enumerate(turns, 1):
        base = {"actor": "opus", "name": worker, "job_id": turn["job_id"], "request": index, "status": turn["status"]}
        items.append({**base, "id": f"opus:{turn['job_id']}:request", "at": turn["created_at"], "kind": "opus_request"})
        # A running request's reply slot stays next to its request so the feed doesn't shift while live.
        finished = turn["finished_at"] if turn["status"] not in activity.ACTIVE else None
        items.append({**base, "id": f"opus:{turn['job_id']}:reply", "at": finished or turn["created_at"], "kind": "opus_reply"})
    items.sort(key=lambda i: (i["at"] is None, i["at"] or 0))  # Stable: equal times keep source order.
    roles = {p["key"]: p for p in participants}
    # A review is a final answer from an agent whose own role/path says review; the model name alone isn't enough.
    reviews = [{"id": i["id"], "name": i["name"], "at": i["at"], "excerpt": i["text"][:240]} for i in items
               if i["kind"] == "agent_message" and i.get("phase") == "final" and roles[i["actor"]]["role"] == "Reviewer"]
    main = {p["key"] for p in participants if p.get("depth") == 0}
    coordination = {"handoffs": sum(1 for i in items if i["kind"] == "handoff" and i["actor"] in main),
                    "finals": sum(1 for i in items if i["kind"] == "agent_message" and i.get("phase") == "final"
                                  and i["actor"] in main),
                    "names": sorted({roles[k]["name"] for k in main})}
    return {"linked": bool(link), "codex_thread_id": link, "available": bool(feed and feed["available"]),
            "coverage": coverage, "participants": participants, "items": items, "reviews": reviews,
            "coordination": coordination}


class Dashboard:
    def __init__(self, state, codex_root=None, history=None, worker_model=settings.DEFAULT_MODELS["worker"]):
        self.state = Path(state)
        self.store = Store(self.state)
        self.history = history or codex_history.CodexHistory(codex_root)
        # Only labels jobs that predate per-job model records; each job's own requested model wins.
        self.worker_model = worker_model

    def feed(self, link):
        if not link:
            return None
        try:
            return self.history.feed(link)
        except Exception as error:  # noqa: BLE001 - history problems never break the worker view
            print(f"dashboard: Codex history unavailable: {type(error).__name__}", file=sys.stderr, flush=True)
            return {"available": False, "participants": [], "items": [],
                    "coverage": [{"level": "warn", "text": "Codex history couldn't be read; showing Claude worker history."}]}

    def conversations(self):
        now = time.time()
        with self.store.lock:
            grouped, skipped = self.store.jobs()
            items = []
            for cid, jobs in grouped.items():
                # Defense in depth: one unexpected record never hides the others.
                try:
                    items.append(summary(cid, jobs, self.store.conversation_record(cid), now, self.store, self.worker_model))
                except Exception as error:  # noqa: BLE001
                    print(f"dashboard: skipped conversation {cid}: {type(error).__name__}", file=sys.stderr, flush=True)
                    skipped += len(jobs)
        items.sort(key=lambda c: c["updated_at"] or 0, reverse=True)
        return {"state_dir": str(self.state), "state_exists": self.state.is_dir(), "conversations": items,
                "skipped_records": skipped,
                "active_count": sum(1 for c in items if c["status"] in activity.ACTIVE)}

    def conversation(self, conversation_id):
        now = time.time()
        with self.store.lock:
            grouped, _ = self.store.jobs()
            jobs = grouped.get(conversation_id)
            if not jobs:
                return None
            record = self.store.conversation_record(conversation_id)
            turns = [job_view(job, now, self.store) for job in jobs]
            head = summary(conversation_id, jobs, record, now, self.store, self.worker_model)
            link = thread_link(record, jobs)
        head["session_id"] = turns[-1]["session_id"] or text(record.get("session_id"), 200)
        head["codex_thread_id"] = link
        return {"conversation": head, "turns": turns, "contributions": contributions(turns),
                "team": team_view(turns, head, link, self.feed(link))}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Crewview/1"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # Quiet by default; errors are reported below.

    def send(self, status, body=b"", ctype="application/json; charset=utf-8", extra=None, head=False):
        self.send_response(status)
        headers = {"Content-Type": ctype, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                   "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                   "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                   "Cross-Origin-Resource-Policy": "same-origin", "Cross-Origin-Opener-Policy": "same-origin",
                   **(extra or {})}
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body and not head:
            self.wfile.write(body)

    def fail(self, status, message, head=False):
        self.send(status, json.dumps({"error": message}).encode(), head=head)

    def permitted(self):
        port = self.server.server_address[1]
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0] not in {f"{HOST}:{port}", f"localhost:{port}"}:
            return "Unexpected Host header"
        origin = self.headers.get("Origin")
        if origin is not None and origin != "http://" + hosts[0]:
            return "Cross-origin requests are not allowed"
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in {"same-origin", "none"}:
            return "Cross-site requests are not allowed"
        return None

    def do_GET(self, head=False):  # noqa: N802 - stdlib naming
        problem = self.permitted()
        if problem:
            return self.fail(403, problem, head)
        if not self.path.startswith("/"):
            return self.fail(400, "Bad request path", head)
        path = urlsplit(self.path).path
        try:
            if path in ASSETS:
                name, ctype = ASSETS[path]
                return self.send(200, (WEB / name).read_bytes(), ctype, head=head)
            if path == "/api/health":
                return self.json({"app": APP, "version": 1, "state_id": self.server.state_id}, head)
            if path == "/api/conversations":
                return self.json(self.server.dashboard.conversations(), head)
            match = DETAIL.match(path)
            if match and canonical(match.group(1)):
                detail = self.server.dashboard.conversation(match.group(1))
                if detail is None:
                    return self.fail(404, "Conversation not found", head)
                return self.json(detail, head)
            return self.fail(404, "Not found", head)
        except Exception as error:  # noqa: BLE001 - never leak a traceback to the page
            print(f"dashboard: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            return self.fail(500, "Internal error", head)

    def do_HEAD(self):  # noqa: N802
        self.do_GET(head=True)

    def json(self, value, head=False):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        tag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        if self.headers.get("If-None-Match") == tag:
            return self.send(304, extra={"ETag": tag}, head=True)
        self.send(200, body, extra={"ETag": tag}, head=head)

    def refuse(self):
        self.send(405, json.dumps({"error": "This dashboard is read-only"}).encode(), extra={"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = refuse  # noqa: N815


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # Allow prompt restarts after TCP TIME_WAIT. SO_REUSEPORT remains disabled,
    # so an active listener is still detected and handled by the health probe.
    allow_reuse_address = True


def make_server(state, port, codex_root=None, worker_model=settings.DEFAULT_MODELS["worker"]):
    server = Server((HOST, port), Handler)
    server.dashboard = Dashboard(state, codex_root, worker_model=worker_model)
    server.state_id = state_id(state)
    return server


def probe(port):
    """Return the /api/health payload of whatever is on the port, or None."""
    try:
        conn = http.client.HTTPConnection(HOST, port, timeout=2)
        conn.request("GET", "/api/health", headers={"Host": f"{HOST}:{port}"})
        response = conn.getresponse()
        data = json.loads(response.read(4096)) if response.status == 200 else None
        conn.close()
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Crewview: read-only local dashboard for Codex and Claude Code worker conversations.")
    parser.add_argument("--state", help="Bridge state directory (default: %(default)s)", default=str(DEFAULT_STATE))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port on 127.0.0.1 (default: %(default)s)")
    parser.add_argument("--codex-sessions", default=str(codex_history.DEFAULT_ROOT),
                        help="Codex local session logs, read only for explicitly linked threads (default: %(default)s)")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        worker_model = settings.load().worker
    except settings.SettingsError as error:
        print(f"crewview: invalid configuration: {error}", file=sys.stderr)
        return 2
    state = Path(args.state).expanduser().resolve()
    try:
        server = make_server(state, args.port, Path(args.codex_sessions).expanduser(), worker_model)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        info = probe(args.port)
        url = f"http://{HOST}:{args.port}/"
        if info and info.get("app") == APP and info.get("state_id") == state_id(state):
            print(f"Crewview dashboard is already running at {url}", flush=True)
            if args.open:
                webbrowser.open(url)
            return 0
        if info and info.get("app") == APP:
            print(f"A Crewview dashboard for a different state directory is using port {args.port}. "
                  f"Choose another with --port.", file=sys.stderr)
        else:
            print(f"Port {args.port} is in use by another program; it was left alone. "
                  f"Choose another with --port.", file=sys.stderr)
        return 1
    url = f"http://{HOST}:{server.server_address[1]}/"
    print(f"Crewview dashboard (read-only): {url}\nState: {state}\nPress Ctrl+C to stop.", flush=True)
    if args.open:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    os.umask(0o077)
    sys.exit(main())
