import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


demo = load("demo")
d = load("dashboard")


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "demo"

    def tearDown(self):
        self.tmp.cleanup()

    def run_demo(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = demo.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_demo_renders_the_whole_team_in_the_dashboard(self):
        code, out, _ = self.run_demo("--output", str(self.out))
        self.assertEqual(code, 0)
        self.assertIn(f"--state {self.out / 'state'} --codex-sessions {self.out / 'codex-sessions'}", out)
        dash = d.Dashboard(self.out / "state", codex_root=self.out / "codex-sessions")
        listing = dash.conversations()
        self.assertEqual((len(listing["conversations"]), listing["skipped_records"]), (2, 0))
        linked = next(c for c in listing["conversations"] if c["linked"])
        self.assertEqual((linked["worker"], linked["request_count"], linked["status"]), ("Opus", 2, "completed"))
        detail = dash.conversation(linked["conversation_id"])
        team = detail["team"]
        self.assertTrue(team["available"])
        names = {p["name"]: p["role"] for p in team["participants"]}
        self.assertEqual(names, {"User": "Human request", "Astra": "Orchestrator", "Sol": "Reviewer",
                                 "Luna": "Checker", "Opus": "Implementer"})
        self.assertEqual([r["name"] for r in team["reviews"]], ["Sol"])
        times = [i["at"] for i in team["items"]]
        self.assertEqual(times, sorted(times))
        kinds = [i["kind"] for i in team["items"]]
        self.assertLess(kinds.index("human"), kinds.index("opus_request"))
        files = [f["path"].rsplit("/", 1)[-1] for f in detail["contributions"]["files"]]
        self.assertEqual(sorted(files), ["cli.py", "format.py", "test_units.py"])
        self.assertEqual(detail["contributions"]["applied_edit_count"], 5)
        self.assertEqual([c["status"] for c in detail["contributions"]["commands"]], ["succeeded", "succeeded"])
        consult = next(c for c in listing["conversations"] if not c["linked"])
        self.assertEqual(consult["mode"], "consult")

    def test_demo_contains_no_real_paths_or_accounts(self):
        self.assertEqual(self.run_demo("--output", str(self.out))[0], 0)
        blob = "".join(p.read_text() for p in self.out.rglob("*") if p.is_file())
        for leaked in (str(Path.home()), os.environ.get("USER") or "\0", "@", "sk-ant", "ANTHROPIC"):
            self.assertNotIn(leaked, blob)
        self.assertIn("/Users/demo/projects/forecast-cli", blob)
        modes = {p.stat().st_mode & 0o777 for p in self.out.rglob("*") if p.is_file()}
        self.assertEqual(modes, {0o600})

    def test_refuses_to_overwrite_existing_output(self):
        self.out.mkdir()
        (self.out / "keep.txt").write_text("mine")
        code, _, err = self.run_demo("--output", str(self.out))
        self.assertEqual(code, 1)
        self.assertIn("Nothing was written", err)
        self.assertEqual([p.name for p in self.out.iterdir()], ["keep.txt"])
        existing_file = Path(self.tmp.name) / "file"
        existing_file.write_text("x")
        self.assertEqual(self.run_demo("--output", str(existing_file))[0], 1)
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(self.run_demo("--output", str(empty))[0], 0)

    def test_linked_ids_and_records_are_well_formed(self):
        self.assertEqual(self.run_demo("--output", str(self.out))[0], 0)
        for path in (self.out / "state" / "jobs").glob("*.json"):
            record = json.loads(path.read_text())
            self.assertEqual(record["job_id"], path.stem)
            self.assertIn(record["status"], {"completed"})
        rollouts = sorted((self.out / "codex-sessions").glob("*/*/*/rollout-*.jsonl"))
        self.assertEqual(len(rollouts), 3)


if __name__ == "__main__":
    unittest.main()
