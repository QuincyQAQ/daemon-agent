import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from foldjay.morning import morning_report, render_report
from foldjay.permissions import Policy
from foldjay.storage import Store
from foldjay.tools import Tools


class MorningReportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "runs"
        shutil.copytree(Path(__file__).resolve().parents[1] / "examples/overnight", self.data)
        self.policy = Policy(self.root, self.root / ".state", "plan")

    def test_plan_tool_reports_missing_and_paired(self):
        store = Store(self.policy.state)
        self.addCleanup(store.close)
        tools = Tools(self.policy, store)
        result = tools.call("morning_report", {"path": "runs"})
        self.assertEqual([p["seed"] for p in result["pairs"]], [0, 1])
        reference = (self.data / "A/0/job.json").read_bytes()
        self.assertEqual(result["runs"][0]["sha256"]["job"], hashlib.sha256(reference).hexdigest())
        self.assertNotIn("evaluation", result["runs"][-1]["sha256"])
        self.assertEqual(result["runs"][-1]["issues"], ["缺少 runs/B/2/eval.json"])

    def test_invalid_records_excluded_individually(self):
        for file, field, value in [
            ("A/0/job.json", "status", "RUNNING"), ("B/1/eval.json", "scene", "another"),
            ("A/2/eval.json", "score", True),
        ]:
            path = self.data / file
            record = json.loads(path.read_text())
            record[field] = value
            path.write_text(json.dumps(record))
        result = morning_report(self.policy, "runs")
        self.assertFalse(result["pairs"])
        self.assertEqual(sum(bool(r["issues"]) for r in result["runs"]), 4)

    def test_malformed_record_and_missing_run_leave_other_pair_intact(self):
        (self.data / "A/1/eval.json").write_text("{")
        shutil.rmtree(self.data / "A/2")
        result = morning_report(self.policy, "runs")
        self.assertEqual([p["seed"] for p in result["pairs"]], [0])
        self.assertEqual(len(result["runs"][4]["issues"]), 2)

    def test_restarted_run_requires_matching_provenance(self):
        paths = [self.data / "A/0" / name for name in ("job.json", "eval.json")]
        for path, run_id in zip(paths, ("new-attempt", "old-attempt")):
            record = json.loads(path.read_bytes())
            path.write_text(json.dumps(dict(record, run_id=run_id)))
        result = morning_report(self.policy, "runs")
        self.assertEqual([p["seed"] for p in result["pairs"]], [1])
        self.assertIn("run_id", result["runs"][0]["issues"][0])
        record = json.loads(paths[1].read_bytes())
        del record["run_id"]
        paths[1].write_text(json.dumps(record))
        self.assertEqual([p["seed"] for p in morning_report(self.policy, "runs")["pairs"]], [1])
        paths[1].write_text(json.dumps(dict(record, run_id="new-attempt")))
        self.assertEqual([p["seed"] for p in morning_report(self.policy, "runs")["pairs"]], [0, 1])

    def test_ambiguous_and_deep_records_do_not_lose_valid_pairs(self):
        score = self.data / "A/0/eval.json"
        score.write_text(score.read_text().rstrip()[:-1] + ', "score": 99}')
        (self.data / "A/2/eval.json").write_text('[' * 1100 + '0' + ']' * 1100)
        result = morning_report(self.policy, "runs")
        self.assertEqual([p["seed"] for p in result["pairs"]], [1])
        self.assertIn("字段重复", result["runs"][0]["issues"][0])
        self.assertIn("嵌套层数", result["runs"][4]["issues"][0])

    def test_state_file_is_protected_and_rendering_preserves_lines(self):
        self.policy.state.mkdir()
        source = self.data / "B/0/eval.json"
        target = self.policy.state / "eval.json"
        source.rename(target)
        source.symlink_to(target)
        result = morning_report(self.policy, "runs")
        self.assertEqual([p["seed"] for p in result["pairs"]], [1])
        self.assertNotIn("evaluation", result["runs"][1]["sha256"])
        result["scene"] = "first\nsecond"
        self.assertIn("first\\nsecond", render_report(result).splitlines()[0])
