"""Read-only adapter for local Codex rollout logs: public agent messages only.

Given one explicitly linked Codex thread UUID, this finds that thread's rollout
file and its explicitly spawned task subagents (bounded recursion through
`parent_thread_id` metadata). It never associates threads by cwd, prompt text,
shared session_id, or UUIDs mentioned in messages.

Only public prose is returned: human requests from the root thread (with known
scaffolding removed), assistant `commentary`/`final_answer` output_text, and
lightweight delegation/tool markers. Reasoning, instructions, developer/system
text, tool arguments and tool outputs, and encrypted inter-agent payloads are
never returned. The log schema is a local implementation detail and may change;
anything unrecognised is skipped and reported as reduced coverage.
"""
import datetime
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import activity  # noqa: E402
import settings  # noqa: E402

DEFAULT_ROOT = settings.codex_sessions()
INDEX_TTL = 30            # seconds between metadata-index refreshes
META_BYTES = 1024 * 1024  # max bytes read to find a file's first (session_meta) line
MAX_INDEX_FILES = 20000
MAX_SESSION_BYTES = 256 * 1024 * 1024
MAX_LINE = 16 * 1024 * 1024
CHUNK = 4 * 1024 * 1024
MAX_TEXT = 40000
MAX_ITEMS = 4000          # per session
MAX_FEED_ITEMS = 2000     # per response (latest kept)
MAX_DEPTH = 4
MAX_SESSIONS = 32
DELEGATION = {"spawn_agent", "send_message", "followup_task", "send_input"}
BRIDGE_CALLS = {"claude_start", "claude_reply"}
TARGET_KEYS = ("target", "agent_path", "task_name", "recipient", "agent_id", "agent", "nickname", "name")
FILE_ID = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
ISO = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$")
SAFE = re.compile(r"^[\w .:/@+()-]{1,120}$")
AMBIENT = re.compile(r"<(in[_-]app[_-]browser[_-]context|browser[_-]context|ide[_-]context|ambient[_-]context)\b[^>]*>"
                     r"[\s\S]*?</\1>\s*", re.I)
SEGMENT = re.compile(r"<([A-Za-z_][\w-]*)\b[^>]*>[\s\S]*?</\1\b[^>]*>")
SCAFFOLD_TAGS = {"environment_context", "user_instructions", "instructions", "permissions", "collaboration_mode",
                 "apps", "skills", "plugins", "recommended_plugins", "turn_aborted", "subagent_notification",
                 "user_shell_command"}
AGENTS_HEADER = re.compile(r"^# AGENTS\.md instructions for [^\n]*\n?", re.M)
REQUEST_MARK = re.compile(r"^##[ \t]*My request(?: for Codex)?:[ \t]*", re.M)
# Hidden renderer metadata inside public replies: drop the element and its content.
CITATION = re.compile(r"<oai-mem-citation\b[^>]*/>|<oai-mem-citation\b[^>]*>[\s\S]*?</oai-mem-citation\s*>"
                      r"|<oai-mem-citation\b[^\n]*", re.I)
PUBLIC_PHASES = {"commentary": "commentary", "final_answer": "final", "final": "final"}


def canonical(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def parse_time(value):
    """ISO-8601 timestamp → epoch seconds within browser-formattable range, else None."""
    match = ISO.match(value) if isinstance(value, str) and len(value) <= 40 else None
    if not match:
        return None
    try:
        moment = datetime.datetime(*map(int, match.groups()[:6]), tzinfo=datetime.timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return None
    zone = match.group(8)
    offset = 0 if zone == "Z" else (1 if zone[0] == "+" else -1) * (int(zone[1:3]) * 3600 + int(zone[4:6]) * 60)
    moment += float("0." + match.group(7)) if match.group(7) else 0.0
    moment -= offset
    return moment if 0 <= moment <= 1e11 else None


def safe_label(value):
    """A short identifier-like string (paths, nicknames, models, tool names); never payloads."""
    return value if isinstance(value, str) and SAFE.match(value) and not value.startswith("gAAAA") else None


def agent_name(model):
    """Display name from the model actually reported for a turn; no model means plain Codex."""
    lowered = (model or "").lower()
    for name in ("astra", "sol", "luna"):
        if re.search(rf"(^|[^a-z]){name}([^a-z]|$)", lowered):
            return name.title()
    return "Codex"


def looks_encrypted(text):
    stripped = text.strip()
    return stripped.startswith("gAAAA") or (len(stripped) > 200 and re.fullmatch(r"[A-Za-z0-9+/=_-]+", stripped) is not None)


def dig(value, *keys):
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def meta_fields(row, file_id):
    """Extract only linkage/identity metadata from a session_meta row (never instructions)."""
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    source = payload.get("source")
    spawn = dig(source, "subagent", "thread_spawn") or {}
    raw_parents = [payload.get("parent_thread_id"), row.get("parent_thread_id"), spawn.get("parent_thread_id")]
    present = [p for p in raw_parents if p is not None]
    parents = sorted({p for p in present if canonical(p)})
    thread_source = safe_label(payload.get("thread_source"))
    blob = " ".join(str(v) for v in (payload.get("agent_path"), payload.get("agent_nickname"), payload.get("agent_role"),
                                     thread_source, json.dumps(source)[:4000] if source is not None else "")).lower()
    return {"id": payload.get("id") if canonical(payload.get("id")) and payload.get("id") == file_id else None,
            "parents": parents, "parent_conflict": len(parents) > 1 or any(not canonical(p) for p in present),
            "subagent": thread_source == "subagent" or isinstance(dig(source, "subagent"), (dict, str)),
            "guardian": "guardian" in blob,
            "path": safe_label(payload.get("agent_path")), "nickname": safe_label(payload.get("agent_nickname")),
            "role": safe_label(payload.get("agent_role") or spawn.get("agent_role") or spawn.get("agent_type")),
            "model": safe_label(dig(payload, "base_instructions", "provenance", "model")),
            "started": parse_time(row.get("timestamp")) or parse_time(payload.get("timestamp"))}


def read_meta(path):
    """Metadata from the first line only; None if unreadable, oversized or not session_meta."""
    match = FILE_ID.search(path.name)
    if not match:
        return None
    try:
        with path.open("rb") as stream:
            # Only the first line is read: never any conversation body.
            first = stream.readline(META_BYTES + 1)
        if len(first) > META_BYTES or not first.endswith(b"\n"):
            return None  # Oversized, or still being written.
        row = activity.loads(first.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(row, dict) or row.get("type") != "session_meta":
        return None
    return meta_fields(row, match.group(1))


def output_text(content):
    if isinstance(content, str):
        return CITATION.sub("", content).strip()
    if not isinstance(content, list):
        return ""
    text = "\n\n".join(c["text"] for c in content if isinstance(c, dict) and c.get("type") == "output_text"
                       and isinstance(c.get("text"), str) and c["text"].strip())
    return CITATION.sub("", text).strip()


def strip_scaffolding(match):
    tag = match.group(1).lower()
    scaffold = tag in SCAFFOLD_TAGS or tag.endswith(("_instructions", "_context", "_notification"))
    return "" if scaffold else match.group(0)


def clean_user(content):
    """The human's actual request from a root user message, minus known scaffolding.

    Known wrappers are removed wherever they appear in a block, so a block that
    combines setup and the request keeps the request. An explicit trailing
    "## My request:" / "## My request for Codex:" section wins when present.
    """
    blocks = [c["text"] for c in content if isinstance(c, dict) and c.get("type") == "input_text"
              and isinstance(c.get("text"), str)] if isinstance(content, list) else []
    kept = []
    for block in blocks:
        text = SEGMENT.sub(strip_scaffolding, AMBIENT.sub("", block))
        marks = list(REQUEST_MARK.finditer(text))
        if marks:
            text = text[marks[-1].end():]
        text = AGENTS_HEADER.sub("", text).strip()
        if text:
            kept.append(text)
    return "\n\n".join(kept)


def phase_of(payload):
    """Only explicitly public phases; missing, unknown, analysis or summary phases are never shown."""
    value = payload.get("phase", payload.get("channel"))
    return PUBLIC_PHASES.get(value) if isinstance(value, str) else None


def stat_key(path):
    info = path.stat()
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def still_valid(hit, key):
    """Readable metadata is immutable for an inode; an unreadable/partial first line is retried on change."""
    if hit is None:
        return False
    return hit[0][0] == key[0] if hit[1] is not None else hit[0] == key


def role_label(meta):
    """Role from explicit metadata, else the agent's own (leaf) path segment; never from ancestors."""
    explicit = meta.get("role")
    if explicit:
        return "Reviewer" if "review" in explicit.lower() else explicit.replace("_", " ").strip().title()
    leaf = (meta.get("path") or "").rstrip("/").rsplit("/", 1)[-1].lower()
    for word, label in (("review", "Reviewer"), ("check", "Checker"), ("help", "Helper")):
        if word in leaf:
            return label
    return "Agent"


def tool_arguments(payload):
    raw = payload.get("arguments", payload.get("input"))
    if isinstance(raw, str) and len(raw) <= 200000:
        try:
            raw = activity.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


class Session:
    """Incremental, bounded parser for one rollout file (append-only JSONL)."""

    def __init__(self, path, thread_id, meta, depth):
        self.path, self.thread_id, self.meta, self.depth = path, thread_id, meta, depth
        self.reset()

    def reset(self):
        self.key = None
        self.offset = 0
        self.buffer = b""
        self.skipping = False
        self.line = 0
        self.items = []
        self.model = self.meta.get("model")
        self.last_at = self.meta.get("started")
        self.malformed = self.long_lines = self.encrypted = 0
        self.truncated = False

    def refresh(self):
        info = self.path.stat()
        if self.key is not None and (self.key != (info.st_dev, info.st_ino) or info.st_size < self.offset):
            self.reset()  # Replaced or rewritten: parse from the start.
        self.key = (info.st_dev, info.st_ino)
        limit = min(info.st_size, MAX_SESSION_BYTES)
        self.truncated = self.truncated or info.st_size > MAX_SESSION_BYTES
        if self.offset >= limit:
            return
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            while self.offset < limit:
                chunk = stream.read(min(CHUNK, limit - self.offset))
                if not chunk:
                    break
                self.offset += len(chunk)
                self.feed(chunk)

    def feed(self, chunk):
        if self.skipping:
            cut = chunk.find(b"\n")
            if cut < 0:
                return
            chunk, self.skipping = chunk[cut + 1:], False
            self.line += 1
        *lines, self.buffer = (self.buffer + chunk).split(b"\n")
        for line in lines:
            self.line += 1
            self.row(line)
        if len(self.buffer) > MAX_LINE:
            self.buffer, self.skipping = b"", True
            self.long_lines += 1

    def row(self, line):
        if not line.strip():
            return
        try:
            row = activity.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.malformed += 1
            return
        if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
            self.malformed += 1
            return
        at = parse_time(row.get("timestamp"))
        self.last_at = at = at if at is not None else self.last_at
        payload = row["payload"]
        if row.get("type") == "turn_context":
            # Each turn states its model; a missing/malformed one means unknown, never the previous agent's.
            self.model = safe_label(payload.get("model"))
        elif row.get("type") == "response_item":
            self.response_item(payload, at)
        # event_msg rows duplicate response_items; compaction/reasoning rows are never shown.

    def add(self, kind, at, **fields):
        if len(self.items) >= MAX_ITEMS:
            self.truncated = True
            return
        text = fields.get("text")
        if text is not None and len(text) > MAX_TEXT:
            fields.update(text=text[:MAX_TEXT], truncated=True)
        self.items.append({"id": f"{self.thread_id}:{self.line}", "at": at, "kind": kind, "thread_id": self.thread_id,
                           "model": self.model, "depth": self.depth, **fields})

    def response_item(self, payload, at):
        kind = payload.get("type")
        if kind == "message":
            role = payload.get("role")
            if role == "assistant":
                phase = phase_of(payload)
                text = output_text(payload.get("content")) if phase else ""
                if text and looks_encrypted(text):
                    self.encrypted += 1
                elif text.strip():
                    self.add("agent_message", at, phase=phase, text=text)
            elif role == "user" and self.depth == 0:
                # A subagent's user turns are delegation scaffolding, not human requests.
                text = clean_user(payload.get("content"))
                if text:
                    self.add("human", at, text=text)
        elif kind in ("function_call", "custom_tool_call"):
            name = safe_label(payload.get("name")) or "tool"
            base = re.split(r"__|\.", name)[-1]
            if base in DELEGATION:
                args = tool_arguments(payload)
                targets = [safe_label(args.get(k)) for k in TARGET_KEYS]
                self.add("handoff", at, tool=base, target=next((t for t in targets if t), None),
                         target_model=safe_label(args.get("model")),
                         encrypted=any(isinstance(v, str) and v.startswith("gAAAA") for v in args.values()))
            elif base in BRIDGE_CALLS:
                self.add("handoff", at, tool=base, target="Claude worker", bridge=True, encrypted=False)
            elif self.items and self.items[-1]["kind"] == "tools":
                counts = self.items[-1]["counts"]
                if base in counts or len(counts) < 30:
                    counts[base] = counts.get(base, 0) + 1
            else:
                self.add("tools", at, counts={base: 1})


class CodexHistory:
    """Cached, thread-safe access to the linked thread tree. Metadata index refreshes every INDEX_TTL."""

    def __init__(self, root=None, clock=time.time, ttl=INDEX_TTL):
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.clock, self.ttl = clock, ttl
        self.lock = threading.Lock()
        self.meta = {}        # path -> (inode key, meta or None)
        self.by_id, self.children = {}, {}
        self.scanned_at, self.since = None, None
        self.roots = {}       # thread id -> path (positive lookups)
        self.sessions = {}    # path -> Session
        self.rejected = []    # parent claims of files with mismatched or duplicate ids
        self.root_meta = {}   # path -> (inode, meta)

    def find_root(self, thread_id):
        path = self.roots.get(thread_id)
        if path is not None and path.exists():
            return path
        try:
            found = sorted(self.root.glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
        except OSError:
            found = []
        if len(found) == 1:
            self.roots[thread_id] = found[0]
            return found[0]
        return None

    def scan(self, since):
        """Index first-line metadata of rollout files in day folders on/after `since` (bounded)."""
        if self.since is None or since < self.since:
            self.since, self.scanned_at = since, None
        if self.scanned_at is not None and self.clock() - self.scanned_at < self.ttl:
            return
        self.scanned_at = self.clock()
        seen, count = set(), 0
        try:
            days = sorted(self.root.glob("*/*/*"))
        except OSError:
            days = []
        for day in days:
            if tuple(day.parts[-3:]) < self.since or not day.is_dir():
                continue
            for path in sorted(day.glob("rollout-*.jsonl")):
                count += 1
                if count > MAX_INDEX_FILES:
                    break
                try:
                    key = stat_key(path)
                except OSError:
                    continue
                seen.add(path)
                if not still_valid(self.meta.get(path), key):
                    self.meta[path] = (key, read_meta(path))
        self.meta = {p: v for p, v in self.meta.items() if p in seen}
        self.by_id, self.children, self.rejected, claimed = {}, {}, [], {}
        for path, (_, meta) in sorted(self.meta.items()):
            if not meta:
                continue
            if not meta["id"]:
                self.rejected.append(meta["parents"])  # File name and payload id disagree.
                continue
            claimed.setdefault(meta["id"], []).append((path, meta))
        for thread_id, entries in claimed.items():
            if len(entries) > 1:
                # Two files claim one thread id: trust neither.
                self.rejected.extend(meta["parents"] for _, meta in entries)
                continue
            self.by_id[thread_id] = entries[0]
            for parent in entries[0][1]["parents"]:
                self.children.setdefault(parent, []).append(thread_id)

    def cached_meta(self, path):
        try:
            key = stat_key(path)
        except OSError:
            return None
        hit = self.root_meta.get(path)
        if not still_valid(hit, key):
            hit = self.root_meta[path] = (key, read_meta(path))
        return hit[1]

    def feed(self, thread_id):
        with self.lock:
            return self._feed(thread_id)

    def _feed(self, thread_id):
        coverage = []
        root_path = self.find_root(thread_id) if canonical(thread_id) else None
        root_meta = self.cached_meta(root_path) if root_path else None
        if not root_meta or root_meta["id"] != thread_id:
            text = ("The linked Codex thread wasn't found in local history, so only the Claude worker history is shown."
                    if not root_path else "The linked Codex thread's log couldn't be read, so only the Claude worker history is shown.")
            return {"available": False, "coverage": [{"level": "warn", "text": text}], "participants": [], "items": []}
        if root_meta["guardian"]:
            # Platform approval/review internals are never a task conversation, even when linked directly.
            return {"available": False, "participants": [], "items": [], "coverage": [{"level": "warn", "text":
                    "The linked Codex thread is an internal review session, so only the Claude worker history is shown."}]}
        day = tuple(root_path.parts[-4:-1])
        self.scan(day)
        tree, queue, visited = [(thread_id, root_path, root_meta, 0)], [(thread_id, 0)], {thread_id}
        excluded = {"guardian": 0, "untrusted": 0, "limit": 0}
        while queue:
            parent, depth = queue.pop(0)
            for child in sorted(self.children.get(parent, [])):
                path, meta = self.by_id[child]
                if child in visited:
                    continue
                if meta["guardian"]:
                    excluded["guardian"] += 1
                elif meta["parent_conflict"] or not meta["subagent"]:
                    excluded["untrusted"] += 1
                elif depth + 1 > MAX_DEPTH or len(tree) >= MAX_SESSIONS:
                    excluded["limit"] += 1
                else:
                    visited.add(child)
                    tree.append((child, path, meta, depth + 1))
                    queue.append((child, depth + 1))
        items, sessions, unreadable = [], [], 0
        for order, (tid, path, meta, depth) in enumerate(tree):
            session = self.sessions.get(path)
            if session is None or session.thread_id != tid:
                session = self.sessions[path] = Session(path, tid, meta, depth)
            try:
                session.refresh()
            except OSError:
                unreadable += 1
                continue
            sessions.append(session)
            items.extend((item, order) for item in session.items)
        live = {path for _, path, _, _ in tree}
        self.sessions = {p: s for p, s in self.sessions.items() if p in live}
        excluded["untrusted"] += sum(1 for parents in self.rejected if visited & set(parents))
        return self.assemble(tree, sessions, items, excluded, unreadable, coverage)

    def assemble(self, tree, sessions, items, excluded, unreadable, coverage):
        meta_by_id = {tid: (meta, depth) for tid, _, meta, depth in tree}
        items.sort(key=lambda pair: (pair[0]["at"] is None, pair[0]["at"] or 0, pair[1], int(pair[0]["id"].rsplit(":", 1)[1])))
        items = [item for item, _ in items]
        participants = {}

        def participant(item):
            meta, depth = meta_by_id[item["thread_id"]]
            if item["kind"] == "human":
                return participants.setdefault("user", {"key": "user", "name": "User", "role": "Human request",
                                                         "models": [], "message_count": 0, "final_count": 0})
            name = agent_name(item["model"])
            key = f"{item['thread_id']}:{name}"
            # Only the agent's own path segment counts: a child of /root/sol_review is not a reviewer.
            role = "Main thread" if depth == 0 else role_label(meta)
            entry = participants.setdefault(key, {"key": key, "name": name, "role": role, "models": [],
                                                  "thread_id": item["thread_id"], "depth": depth,
                                                  "nickname": meta.get("nickname"), "path": meta.get("path"),
                                                  "message_count": 0, "final_count": 0, "handoff_count": 0})
            if item["model"] and item["model"] not in entry["models"]:
                entry["models"].append(item["model"])
            return entry

        by_path = {}
        for tid, _, meta, depth in tree:
            for label in (meta.get("path"), (meta.get("path") or "").rsplit("/", 1)[-1] or None, meta.get("nickname"), tid):
                if label:
                    by_path.setdefault(label, tid)
        unmatched = encrypted_handoffs = 0
        for item in items:
            entry = participant(item)
            item.update(actor=entry["key"], name=entry["name"])
            if item["kind"] in ("agent_message", "human"):
                entry["message_count"] += 1
                entry["final_count"] += item.get("phase") == "final"
            elif item["kind"] == "handoff":
                entry["handoff_count"] = entry.get("handoff_count", 0) + 1
                encrypted_handoffs += bool(item.get("encrypted"))
                target_id = by_path.get(item.get("target")) if not item.get("bridge") else None
                if target_id:
                    first = next((i for i in items if i["thread_id"] == target_id and i["kind"] == "agent_message"), None)
                    item["target_name"] = agent_name(first["model"]) if first else "Codex"
                    item["target_thread_id"] = target_id
                elif not item.get("bridge") and item["tool"] == "spawn_agent":
                    unmatched += 1
        for entry in participants.values():
            if entry["key"] != "user" and entry["depth"] == 0 and entry.get("handoff_count"):
                entry["role"] = "Orchestrator"
        coverage += self.notices(sessions, excluded, unreadable, unmatched, encrypted_handoffs, len(items))
        if len(items) > MAX_FEED_ITEMS:
            coverage.append({"level": "info", "text": f"Showing the latest {MAX_FEED_ITEMS} of {len(items)} history entries."})
            items = items[-MAX_FEED_ITEMS:]
        return {"available": True, "coverage": coverage, "participants": list(participants.values()), "items": items,
                "session_count": len(sessions)}

    def notices(self, sessions, excluded, unreadable, unmatched, encrypted_handoffs, count):
        out = [{"level": "info", "text": "Shows public agent messages from local Codex logs. Reasoning, tool output and "
                                         "encrypted agent-to-agent messages are not shown."}]
        malformed = sum(s.malformed for s in sessions)
        long_lines = sum(s.long_lines for s in sessions)
        encrypted = sum(s.encrypted for s in sessions) + encrypted_handoffs
        if encrypted:
            out.append({"level": "info", "text": f"{encrypted} agent-to-agent message(s) are encrypted in the local log; "
                                                 "handoffs show only who delegated to whom."})
        if unmatched:
            out.append({"level": "warn", "text": f"{unmatched} delegation(s) have no readable local session yet."})
        if unreadable:
            out.append({"level": "warn", "text": f"{unreadable} linked session log(s) couldn't be read."})
        if malformed or long_lines:
            out.append({"level": "info", "text": f"Skipped {malformed + long_lines} unreadable or oversized log line(s)."})
        if any(s.truncated for s in sessions):
            out.append({"level": "warn", "text": "A session log exceeded the reading limit; some entries are omitted."})
        if excluded["untrusted"]:
            out.append({"level": "info", "text": "Some sessions with inconsistent parent metadata were left out."})
        if excluded["limit"]:
            out.append({"level": "warn", "text": f"{excluded['limit']} nested session(s) beyond the display limit were left out."})
        return out
