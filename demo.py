#!/usr/bin/env python3
"""Write synthetic Crewview demo data: bridge state plus linked Codex session logs.

Everything is invented (a fictional forecast-cli project under /Users/demo);
no real paths, accounts or model calls are involved. View the result with the
dashboard command this prints.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import settings  # noqa: E402

CWD = "/Users/demo/projects/forecast-cli"
MODELS = settings.DEFAULT_MODELS


def iso(epoch):
    moment = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


class Events:
    """Builds an activity log in the format activity.Recorder writes."""

    def __init__(self, start):
        self.at, self.rows = start, []

    def add(self, kind, step=4.0, **fields):
        self.at += step
        self.rows.append({"seq": len(self.rows), "at": round(self.at, 3), "kind": kind, **fields})

    def tool(self, tool_id, name, tool_input, result=None, summary=None, step=6.0):
        self.add("tool_use", step, id=tool_id, name=name, input=tool_input)
        extra = {"summary": summary} if summary is not None else {"text": result or ""}
        self.add("tool_result", 2.0, tool_use_id=tool_id, is_error=False, **extra)

    def text(self):
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.rows)


def job_record(conversation_id, job_id, prompt, response, created, finished, mode="implement", link=None,
               session_id=None, turns=6):
    record = {"conversation_id": conversation_id, "cwd": CWD, "mode": mode, "prompt": prompt,
              "timeout_seconds": 1800, "max_turns": 60, "job_id": job_id, "model": MODELS["worker"],
              "status": "completed", "created_at": created, "heartbeat": finished, "finished_at": finished,
              "exit_code": 0, "response": response, "session_id": session_id,
              "permission_denials": [], "actual_models": [MODELS["worker"]],
              "model_usage": {MODELS["worker"]: {"inputTokens": 18000, "outputTokens": 2400}},
              "num_turns": turns, "usage": {"input_tokens": 18000, "output_tokens": 2400,
                                            "cache_read_input_tokens": 52000},
              "cost_estimate_usd": None, "cost_note": "Synthetic demo data."}
    if link:
        record["codex_thread_id"] = link
    return record


def implementation_events(start, worker):
    e = Events(start)
    e.add("session", 1.0, model=worker, session_id="demo-session", permission_mode="acceptEdits",
          tools=["Read", "Glob", "Grep", "Edit", "Write", "Bash"])
    e.add("text", text="I'll read the CLI entry point and the formatter before changing anything.")
    e.tool("demo-read-1", "Read", {"file_path": f"{CWD}/forecast/cli.py"}, summary="84 lines read")
    e.tool("demo-edit-1", "Edit", {"file_path": f"{CWD}/forecast/cli.py",
                                   "old_string": 'parser.add_argument("city")\n',
                                   "new_string": 'parser.add_argument("city")\nparser.add_argument("--units", '
                                                 'choices=("metric", "imperial"), default="metric")\n'},
           "The file forecast/cli.py has been updated successfully.")
    e.tool("demo-edit-2", "Edit", {"file_path": f"{CWD}/forecast/format.py",
                                   "old_string": "def format_temperature(celsius):\n    return f\"{round(celsius)}°C\"\n",
                                   "new_string": "def format_temperature(celsius, units=\"metric\"):\n"
                                                 "    value = round(celsius)\n"
                                                 "    if units == \"imperial\":\n"
                                                 "        return f\"{round(value * 9 / 5 + 32)}°F\"\n"
                                                 "    return f\"{value}°C\"\n"},
           "The file forecast/format.py has been updated successfully.")
    e.tool("demo-write-1", "Write", {"file_path": f"{CWD}/tests/test_units.py",
                                     "content": "from forecast.format import format_temperature\n\n\n"
                                                "def test_metric_default():\n    assert format_temperature(21.4) == \"21°C\"\n\n\n"
                                                "def test_imperial():\n    assert format_temperature(21.4, \"imperial\") == \"70°F\"\n"},
           "File created successfully at: tests/test_units.py")
    e.tool("demo-bash-1", "Bash", {"command": "python3 -m pytest -q tests/test_units.py", "description": "Run unit tests"},
           "..                                                    [100%]\n2 passed in 0.08s")
    e.add("result", 3.0, subtype="success", is_error=False, num_turns=9, duration_ms=212000, denials=[])
    return e


def followup_events(start, worker):
    e = Events(start)
    e.add("session", 1.0, model=worker, session_id="demo-session", permission_mode="acceptEdits",
          tools=["Read", "Glob", "Grep", "Edit", "Write", "Bash"])
    e.add("text", text="Sol is right: rounding before converting loses precision. Converting first.")
    e.tool("demo-edit-3", "Edit", {"file_path": f"{CWD}/forecast/format.py",
                                   "old_string": "    value = round(celsius)\n    if units == \"imperial\":\n"
                                                 "        return f\"{round(value * 9 / 5 + 32)}°F\"\n"
                                                 "    return f\"{value}°C\"\n",
                                   "new_string": "    if units == \"imperial\":\n"
                                                 "        return f\"{round(celsius * 9 / 5 + 32)}°F\"\n"
                                                 "    return f\"{round(celsius)}°C\"\n"},
           "The file forecast/format.py has been updated successfully.")
    e.tool("demo-edit-4", "Edit", {"file_path": f"{CWD}/tests/test_units.py",
                                   "old_string": "    assert format_temperature(21.4, \"imperial\") == \"70°F\"\n",
                                   "new_string": "    assert format_temperature(21.4, \"imperial\") == \"71°F\"\n\n\n"
                                                 "def test_half_degree_converts_before_rounding():\n"
                                                 "    assert format_temperature(-17.5, \"imperial\") == \"0°F\"\n"},
           "The file tests/test_units.py has been updated successfully.")
    e.tool("demo-bash-2", "Bash", {"command": "python3 -m pytest -q", "description": "Run the full test suite"},
           "......                                                [100%]\n6 passed in 0.11s")
    e.add("result", 3.0, subtype="success", is_error=False, num_turns=5, duration_ms=98000, denials=[])
    return e


def consult_events(start, worker):
    e = Events(start)
    e.add("session", 1.0, model=worker, session_id="demo-consult", permission_mode="plan", tools=["Read", "Glob", "Grep"])
    e.tool("demo-grep-1", "Grep", {"pattern": "cache", "path": f"{CWD}/forecast"},
           "forecast/client.py\nforecast/cache.py")
    e.tool("demo-read-2", "Read", {"file_path": f"{CWD}/forecast/cache.py"}, summary="41 lines read")
    e.add("result", 3.0, subtype="success", is_error=False, num_turns=3, duration_ms=31000, denials=[])
    return e


def row(epoch, kind, payload):
    return {"timestamp": iso(epoch), "type": kind, "payload": payload}


def say(epoch, text, phase="final_answer"):
    return row(epoch, "response_item", {"type": "message", "role": "assistant", "phase": phase,
                                        "content": [{"type": "output_text", "text": text}]})


def call(epoch, name, arguments):
    return row(epoch, "response_item", {"type": "function_call", "name": name, "call_id": "call-" + uuid.uuid4().hex[:8],
                                        "arguments": json.dumps(arguments)})


def meta(epoch, thread_id, root, model, **fields):
    return row(epoch, "session_meta", {"id": thread_id, "session_id": root, "cwd": CWD, "timestamp": iso(epoch),
                                       "base_instructions": {"provenance": {"model": model}}, **fields})


def child_meta(epoch, thread_id, root, parent, path, model):
    return meta(epoch, thread_id, root, model, parent_thread_id=parent, thread_source="subagent", agent_path=path,
                source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}})


def write_session(sessions, thread_id, started, rows):
    """A rollout file in Codex's day-folder layout for the session's start time."""
    start = datetime.datetime.fromtimestamp(started, datetime.timezone.utc)
    name = f"rollout-{start.strftime('%Y-%m-%dT%H-%M-%S')}-{thread_id}.jsonl"
    write_private(sessions / start.strftime("%Y/%m/%d") / name, "".join(json.dumps(r) + "\n" for r in rows))


def build(output, now=None):
    """Write the demo under output (which must be new or empty); returns (state, sessions)."""
    now = time.time() if now is None else now
    state, sessions = output / "state", output / "codex-sessions"
    base = now - 45 * 60
    root, sol, luna = (str(uuid.uuid4()) for _ in range(3))
    conversation, first, second = (str(uuid.uuid4()) for _ in range(3))
    session_id = str(uuid.uuid4())
    orch, reviewer, helper, worker = MODELS["orchestrator"], MODELS["reviewer"], MODELS["helper"], MODELS["worker"]
    name = settings.worker_label(worker)

    request = "Add a --units flag to the forecast CLI so people can choose metric or imperial output."
    write_session(sessions, root, base, [
        meta(base, root, root, orch, thread_source="user", source="vscode"),
        row(base + 1, "turn_context", {"model": orch, "cwd": CWD}),
        row(base + 2, "response_item", {"type": "message", "role": "user",
                                        "content": [{"type": "input_text", "text": request}]}),
        say(base + 10, f"Planning: this touches the argument parser and the formatter. I'll delegate the "
                       f"implementation to {name} and ask Sol to review the diff.", "commentary"),
        call(base + 20, "mcp__crewview__claude_start", {"mode": "implement"}),
        say(base + 270, f"{name} reports the flag is implemented with tests. Asking Sol to review.", "commentary"),
        call(base + 275, "spawn_agent", {"target": "sol_review", "model": reviewer}),
        say(base + 410, f"Sol found one rounding issue; sending it back to {name}.", "commentary"),
        call(base + 415, "mcp__crewview__claude_reply", {}),
        say(base + 560, "Done. `--units metric|imperial` is implemented and tested, and Sol's rounding finding is "
                        "fixed.\n\n- forecast/cli.py: new `--units` option\n- forecast/format.py: converts before "
                        "rounding\n- tests/test_units.py: metric, imperial and half-degree cases"),
    ])
    write_session(sessions, sol, base + 276, [
        child_meta(base + 276, sol, root, root, "/root/sol_review", reviewer),
        row(base + 277, "turn_context", {"model": reviewer, "cwd": CWD}),
        say(base + 290, "Reviewing the diff in forecast/cli.py, forecast/format.py and the new tests.", "commentary"),
        call(base + 300, "spawn_agent", {"agent_path": "luna_check", "model": helper}),
        say(base + 400, "Review: one finding.\n- `format_temperature` rounds before converting, so half-degree "
                        "values drift in imperial. Convert first, then round.\n- The option wiring and defaults "
                        "look right; Luna confirmed the CLI behaviour."),
    ])
    write_session(sessions, luna, base + 301, [
        child_meta(base + 301, luna, root, sol, "/root/sol_review/luna_check", helper),
        row(base + 302, "turn_context", {"model": helper, "cwd": CWD}),
        say(base + 350, "Checked: `--units` defaults to metric and rejects unknown values with a clear argparse error."),
    ])

    jobs = state / "jobs"
    one = implementation_events(base + 21, worker)
    write_private(jobs / (first + ".events.jsonl"), one.text())
    write_private(jobs / (first + ".json"), json.dumps(job_record(
        conversation, first, f"Implement a --units flag (metric|imperial, default metric) in {CWD}.\n\n"
        "Update the argument parser and format_temperature, add tests, and run them. Report files changed "
        "and checks run.", f"Implemented `--units`.\n\n- **forecast/cli.py**: `--units {{metric,imperial}}`, "
        "default metric\n- **forecast/format.py**: `format_temperature(celsius, units)`\n- **tests/test_units.py**: "
        "two new tests\n\nChecks: `python3 -m pytest -q tests/test_units.py` → 2 passed.",
        base + 21, one.at + 1, link=root, session_id=session_id, turns=9)))
    two = followup_events(base + 416, worker)
    write_private(jobs / (second + ".events.jsonl"), two.text())
    write_private(jobs / (second + ".json"), json.dumps(job_record(
        conversation, second, "Sol review finding: format_temperature rounds before converting, so half-degree "
        "values drift in imperial. Convert first, then round; add a regression test and re-run the suite.",
        "Fixed: `format_temperature` now converts before rounding.\n\n- **forecast/format.py**: round after "
        "conversion\n- **tests/test_units.py**: half-degree regression test\n\nChecks: `python3 -m pytest -q` → "
        "6 passed.", base + 416, two.at + 1, link=root, session_id=session_id, turns=5)))
    write_private(state / "conversations" / (conversation + ".json"), json.dumps(
        {"conversation_id": conversation, "cwd": CWD, "mode": "implement", "codex_thread_id": root,
         "latest_job_id": second, "session_id": session_id}))

    consult, consult_job = str(uuid.uuid4()), str(uuid.uuid4())
    three = consult_events(base - 3600, worker)
    write_private(jobs / (consult_job + ".events.jsonl"), three.text())
    write_private(jobs / (consult_job + ".json"), json.dumps(job_record(
        consult, consult_job, "Which files handle response caching, and how long are entries kept?",
        "Caching lives in **forecast/cache.py** (a JSON file cache keyed by city) and is used from "
        "**forecast/client.py**. Entries expire after 10 minutes (`TTL_SECONDS = 600`).",
        base - 3600, three.at + 1, mode="consult", session_id=str(uuid.uuid4()), turns=3)))
    write_private(state / "conversations" / (consult + ".json"), json.dumps(
        {"conversation_id": consult, "cwd": CWD, "mode": "consult", "latest_job_id": consult_job}))
    return state, sessions


def main(argv=None, now=None):
    p = argparse.ArgumentParser(description="Write synthetic Crewview demo data (no model calls, no real data).")
    p.add_argument("--output", required=True, metavar="DIR", help="New or empty folder to write the demo into")
    args = p.parse_args(argv)
    output = Path(args.output).expanduser().absolute()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        print(f"{output} already exists and is not an empty folder; choose a new one. Nothing was written.",
              file=sys.stderr)
        return 1
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    state, sessions = build(output, now)
    command = [sys.executable, str(HERE / "dashboard.py"), "--state", str(state), "--codex-sessions", str(sessions),
               "--open"]
    print(f"Wrote synthetic demo data to {output}\nView it (read-only) with:\n  {shlex.join(command)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
