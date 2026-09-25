"""Sanitized, bounded activity capture shared by the bridge worker and dashboard.

The worker feeds Claude's stream-json stdout into Recorder, which appends a
private per-job event log. Only visible assistant text, tool calls and matching
tool results are kept; thinking blocks and user/system text are never recorded.
"""
import contextlib
import json
import math
import os
import time

ACTIVE = {"queued", "running"}
STALE_SECONDS = 90
STALE_ERROR = "Worker heartbeat stopped. Inspect local changes before resuming; no success was recorded."

MAX_EVENTS = 4000
MAX_LOG_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 17 * 1024 * 1024
KINDS = {"session", "text", "tool_use", "tool_result", "result", "notice", "truncated"}
EDIT_TOOLS = {"Write", "Edit"}
FIELD, TEXT, CONTENT, EDIT, COMMAND = 2000, 32000, 64000, 32000, 8000
INPUT_FIELDS = {
    "Write": {"file_path": FIELD, "content": CONTENT},
    "Edit": {"file_path": FIELD, "old_string": EDIT, "new_string": EDIT, "replace_all": 0},
    "Bash": {"command": COMMAND, "description": FIELD, "timeout": 0, "run_in_background": 0},
    "Read": {"file_path": FIELD, "offset": 0, "limit": 0},
    "Glob": {"pattern": FIELD, "path": FIELD},
    "Grep": {"pattern": FIELD, "path": FIELD, "glob": FIELD, "type": FIELD, "output_mode": FIELD},
}
# Read output is file content already on disk; keep only its size.
RESULT_LIMITS = {"Read": 0, "Write": 600, "Edit": 600, "Bash": 6000, "Glob": 1500, "Grep": 1500}


def number(value):
    """A finite int/float, else None (JSON may carry NaN, Infinity or 1e999)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def _reject_constant(name):
    raise ValueError("non-finite JSON constant " + name)


def loads(text):
    """json.loads that rejects NaN/Infinity literals."""
    return json.loads(text, parse_constant=_reject_constant)


def effective(job, now=None):
    """Apply the bridge's stale-heartbeat rule without mutating the stored record."""
    now = time.time() if now is None else now
    beat = number(job.get("heartbeat"))
    if beat is None:
        beat = number(job.get("created_at")) or 0
    if job.get("status") in ACTIVE and now - beat > STALE_SECONDS:
        return {**job, "status": "interrupted", "error": STALE_ERROR}
    return job


def clip(value, limit):
    """Return (text, truncated) for a bounded string representation."""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    return (value, False) if len(value) <= limit else (value[:limit], True)


def tail_clip(value, limit):
    """Keep the head and the (usually more useful) tail of long command output."""
    if len(value) <= limit:
        return value, False
    head = limit // 3
    return value[:head] + "\n…\n" + value[-(limit - head):], True


def sanitize_input(name, raw):
    raw = raw if isinstance(raw, dict) else {}
    fields = INPUT_FIELDS.get(name) or {key: 500 for key in sorted(raw)[:12]}
    out, cut = {}, []
    for key, limit in fields.items():
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str) or limit:
            out[key], truncated = clip(value, limit or FIELD)
            if truncated:
                cut.append(key)
    return out, cut


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
    return ""


def denial_ids(denials):
    return {d.get("tool_use_id") for d in denials or [] if isinstance(d, dict) and isinstance(d.get("tool_use_id"), str)}


class Recorder:
    """Incrementally parse stream-json bytes into a bounded, append-only event log."""

    def __init__(self, path):
        self.path = path
        self.buffer = b""
        self.seq = 0
        self.size = 0
        self.malformed = 0
        self.closed = False
        self.names = {}
        self.stream = None

    def emit(self, kind, **fields):
        if self.closed:
            return
        try:
            if self.stream is None:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                os.fchmod(fd, 0o600)
                self.stream = os.fdopen(fd, "a", encoding="utf-8")
            line = json.dumps({"seq": self.seq, "at": round(time.time(), 3), "kind": kind, **fields},
                              ensure_ascii=False) + "\n"
            if self.seq >= MAX_EVENTS - 1 or self.size + len(line.encode()) > MAX_LOG_BYTES - 512:
                self.stream.write(json.dumps({"seq": self.seq, "at": round(time.time(), 3), "kind": "truncated",
                                              "text": "Activity log limit reached; later events were not recorded."}) + "\n")
                self.closed = True
            else:
                self.stream.write(line)
                self.seq += 1
                self.size += len(line.encode())
            self.stream.flush()
        except OSError:
            # Activity capture is best effort and must never fail the job.
            self.closed = True

    def feed(self, data):
        if not data:
            return
        self.buffer += data
        *lines, self.buffer = self.buffer.split(b"\n")
        for line in lines:
            self.line(line)
        if len(self.buffer) > MAX_LINE_BYTES:
            self.buffer = b""
            self.malformed += 1

    def finish(self):
        if self.buffer.strip():
            self.line(self.buffer)
        self.buffer = b""
        if self.malformed:
            self.emit("notice", text=f"Skipped {self.malformed} unreadable output line(s).")
        if self.stream is not None:
            with contextlib.suppress(OSError):
                self.stream.close()
        self.closed = True

    def line(self, raw):
        raw = raw.strip()
        if not raw:
            return
        try:
            record = loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self.malformed += 1
            return
        try:
            if not isinstance(record, dict):
                raise TypeError("not an object")
            self.record(record)
        except Exception:  # noqa: BLE001 - an odd record must never fail the job
            self.malformed += 1

    def record(self, record):
        kind = record.get("type")
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        if kind == "system" and record.get("subtype") == "init":
            tools = record.get("tools") if isinstance(record.get("tools"), list) else []
            self.emit("session", model=clip(record.get("model") or "", 200)[0],
                      session_id=clip(record.get("session_id") or "", 200)[0],
                      permission_mode=clip(record.get("permissionMode") or "", 100)[0],
                      tools=[clip(t, 100)[0] for t in tools[:50] if isinstance(t, str)])
        elif kind == "assistant":
            # Only visible text and tool calls; thinking/redacted_thinking are dropped.
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                    text, cut = clip(block["text"], TEXT)
                    self.emit("text", text=text, **({"truncated": ["text"]} if cut else {}))
                elif block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                    name = clip(block.get("name") or "unknown", 100)[0]
                    self.names[block["id"]] = name
                    data, cut = sanitize_input(name, block.get("input"))
                    self.emit("tool_use", id=clip(block["id"], 200)[0], name=name, input=data,
                              **({"truncated": cut} if cut else {}))
        elif kind == "user":
            # User-role records carry tool results; other user/system text is not ours to show.
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                    name = self.names.get(block["tool_use_id"], "")
                    text = result_text(block.get("content"))
                    limit = RESULT_LIMITS.get(name, 1500)
                    fields = {"tool_use_id": clip(block["tool_use_id"], 200)[0], "is_error": block.get("is_error") is True}
                    if limit:
                        fields["text"], cut = tail_clip(text, limit)
                        if cut:
                            fields["truncated"] = ["text"]
                    else:
                        fields["summary"] = f"{text.count(chr(10)) + 1 if text else 0} lines read"
                    self.emit("tool_result", **fields)
        elif is_envelope(record):
            denials = record.get("permission_denials") if isinstance(record.get("permission_denials"), list) else []
            self.emit("result", subtype=clip(record.get("subtype") or "", 100)[0],
                      is_error=record.get("is_error") is True, num_turns=number(record.get("num_turns")),
                      duration_ms=number(record.get("duration_ms")),
                      denials=[{"tool_use_id": clip(d.get("tool_use_id") or "", 200)[0],
                                "tool_name": clip(d.get("tool_name") or "", 100)[0]}
                               for d in denials[:100] if isinstance(d, dict)])


LEGACY_KEYS = {"result", "is_error", "errors", "session_id", "modelUsage"}
THINKING = {"thinking", "redacted_thinking"}


def is_envelope(value):
    """A stream `result` record, or a legacy/fake single-object result or error envelope."""
    if not isinstance(value, dict):
        return False
    if "type" in value:
        return value["type"] == "result"
    return bool(LEGACY_KEYS & value.keys())


def final_result(raw):
    """Return the final result envelope, or None if the output has none.

    Non-object JSON (e.g. a list) is returned as-is so the worker reports an
    unexpected result shape, as before.
    """
    try:
        value = loads(raw)
    except ValueError:
        pass
    else:
        if is_envelope(value) or not isinstance(value, dict):
            return value
        return None  # A lone init/assistant/other stream record is not a result.
    last = None
    for line in raw.splitlines():
        try:
            record = loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "result":
            last = record
    return last


def diagnostic_tail(raw):
    """A failure excerpt that never echoes JSON-looking output (which may hold thinking)."""
    if any(line.lstrip().startswith(("{", "[")) for line in raw.splitlines()):
        return f"Output ended without a final result record ({len(raw)} characters; raw stream not echoed)."
    return raw[-3000:]


def without_thinking(value):
    """Recursively drop thinking/redacted_thinking blocks from a parsed record."""
    if isinstance(value, list):
        return [without_thinking(v) for v in value if not (isinstance(v, dict) and v.get("type") in THINKING)]
    if isinstance(value, dict):
        return {k: without_thinking(v) for k, v in value.items() if k not in THINKING}
    return value


def scrub_stdout(path, limit=MAX_LINE_BYTES):
    """Rewrite a finished stdout diagnostic without thinking or unparseable raw text.

    Only a genuine legacy result/error envelope is kept byte-for-byte. Stream
    records are re-serialized without thinking blocks; unreadable lines (which
    could be fragments of a thinking block) are replaced by a single note.
    """
    if path.stat().st_size > limit:
        lines, dropped = [], "all (output exceeded the size limit)"
    else:
        raw = path.read_text(errors="replace")
        try:
            if is_envelope(loads(raw)):
                return
        except ValueError:
            pass
        lines, count = [], 0
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                record = loads(line)
            except ValueError:
                record = None
            if not isinstance(record, dict) or record.get("type") == "stream_event":
                count += 1
                continue
            lines.append(json.dumps(without_thinking(record), ensure_ascii=False))
        dropped = str(count) if count else ""
    if dropped:
        lines.append(json.dumps({"type": "bridge_note", "text": f"Removed {dropped} unreadable output line(s)."}))
    temp = path.with_name(path.name + ".scrub.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write("\n".join(lines) + ("\n" if lines else ""))
    os.replace(temp, path)


def _string(value, limit):
    return value[:limit] if isinstance(value, str) else None


def _strings(value, limit=100, count=50):
    return [v[:limit] for v in value[:count] if isinstance(v, str)] if isinstance(value, list) else []


STRING_INPUTS = {key for fields in INPUT_FIELDS.values() for key, limit in fields.items() if limit}


def clean_input(value):
    """Tool input from a log: string-typed keys must be strings; others scalar and finite."""
    if not isinstance(value, dict):
        return None
    out = {}
    for key, item in list(value.items())[:24]:
        if isinstance(item, str):
            out[key] = item[:CONTENT]
        elif key not in STRING_INPUTS and (isinstance(item, bool) or number(item) is not None):
            out[key] = item
    return out


def clean_event(event):
    """Validate one event-log record; returns a normalized copy or None if malformed."""
    if not isinstance(event, dict) or event.get("kind") not in KINDS:
        return None
    kind = event["kind"]
    out = {"seq": number(event.get("seq")), "at": number(event.get("at")), "kind": kind}
    if out["at"] is not None and not 0 <= out["at"] <= 1e11:
        out["at"] = None  # Keep timestamps inside what browsers can format.
    if kind in {"text", "notice", "truncated"}:
        if not isinstance(event.get("text"), str):
            return None
        out["text"] = event["text"][:CONTENT]
    elif kind == "tool_use":
        tool_input = clean_input(event.get("input"))
        if not isinstance(event.get("id"), str) or not isinstance(event.get("name"), str) or tool_input is None:
            return None
        out.update(id=event["id"][:200], name=event["name"][:100], input=tool_input)
    elif kind == "tool_result":
        if not isinstance(event.get("tool_use_id"), str) or not isinstance(event.get("is_error", False), bool):
            return None
        out.update(tool_use_id=event["tool_use_id"][:200], is_error=event.get("is_error") is True)
        for key in ("text", "summary"):
            if event.get(key) is not None:
                if not isinstance(event[key], str):
                    return None
                out[key] = event[key][:CONTENT]
    elif kind == "session":
        out.update(model=_string(event.get("model"), 200), session_id=_string(event.get("session_id"), 200),
                   permission_mode=_string(event.get("permission_mode"), 100), tools=_strings(event.get("tools")))
    elif kind == "result":
        denials = event.get("denials") if isinstance(event.get("denials"), list) else []
        out.update(subtype=_string(event.get("subtype"), 100), is_error=event.get("is_error") is True,
                   num_turns=number(event.get("num_turns")), duration_ms=number(event.get("duration_ms")),
                   denials=[{"tool_use_id": d["tool_use_id"][:200], "tool_name": _string(d.get("tool_name"), 100)}
                            for d in denials[:100] if isinstance(d, dict) and isinstance(d.get("tool_use_id"), str)])
    if "truncated" in event and kind != "truncated":
        out["truncated"] = _strings(event.get("truncated"))
    return out


def read_events(data):
    """Parse complete event-log lines; an in-progress trailing line is ignored."""
    body, newline, _ = data.rpartition(b"\n")
    events, malformed, truncated = [], 0, False
    for line in (body.split(b"\n") if newline else []):
        if not line.strip():
            continue
        try:
            event = clean_event(loads(line.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            event = None
        if event is None:
            malformed += 1
            continue
        truncated = truncated or event["kind"] == "truncated"
        events.append(event)
    return events, malformed, truncated


def tool_outcomes(events, denials, active):
    """Classify each tool call. Only a matching non-error result counts as success."""
    results = {e.get("tool_use_id"): e for e in events if e.get("kind") == "tool_result"}
    denied = denial_ids(denials)
    for event in events:
        if event.get("kind") == "result":
            denied |= denial_ids(event.get("denials"))
    outcomes = {}
    for event in events:
        if event.get("kind") != "tool_use":
            continue
        result = results.get(event.get("id"))
        if event.get("id") in denied:
            outcomes[event["id"]] = "denied"
        elif result is None:
            outcomes[event["id"]] = "pending" if active else "no_result"
        elif result.get("is_error"):
            outcomes[event["id"]] = "failed"
        else:
            outcomes[event["id"]] = "succeeded"
    return outcomes
