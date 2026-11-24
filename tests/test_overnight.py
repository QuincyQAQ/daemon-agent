import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foldjay.main import main


class OvernightTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / "examples/overnight", self.root / "experiment")
        self.folder = self.root / ".state/overnight/test"
        self.submit_count = 0
        self.history = ""
        self.live_queue = ""
        self.drop_reply = False
        self.start = ["start", "test", "experiment", "--script", "experiment/replay.sh", "--partition", "cpu"]
        self.addCleanup(patch.stopall)
        patch("foldjay.overnight.slurm", side_effect=self.scheduler).start()
        patch("foldjay.overnight.shutil.which", return_value="/usr/bin/sbatch").start()

    def scheduler(self, argv, *, script=None):
        if argv[0] == "scontrol":
            return "ClusterName = test-cluster"
        if argv[0] == "squeue":
            return self.live_queue
        if argv[0] == "sacct":
            return self.history
        self.assertEqual(argv[0], "sbatch")
        self.submit_count += 1
        self.token = next(arg.split("=")[1] for arg in argv if arg.startswith("--job-name="))
        self.assertEqual(json.loads((self.folder / "submission.json").read_text())["token"], self.token)
        self.assertFalse(any("--gpus" in arg for arg in argv))
        self.assertIn("--cpus-per-task=1", argv)
        self.assertIn("exec bash", script)
        if self.drop_reply:
            raise TimeoutError("response lost")
        return "123;test-cluster"

    def cli(self, *args, expected=0):
        result, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(result), contextlib.redirect_stderr(errors):
            code = main(["--workspace", str(self.root), "--state", str(self.root / ".state"), "overnight", *args])
        self.assertEqual(code, expected, errors.getvalue())
        return json.loads(result.getvalue()) if result.getvalue() else None

    def test_recover_after_submission_before_receipt(self):
        self.drop_reply = True
        self.cli(*self.start, expected=1)
        self.assertEqual(self.cli(*self.start)["state"], "AWAITING_CONFIRMATION")
        self.live_queue = f"123|{self.token}|RUNNING"
        self.assertEqual(self.cli("status", "test")["job_id"], "123")
        for group in ["A", "B"]:
            shutil.copytree(self.root / "experiment" / group, self.folder / "output" / group)
        self.live_queue = ""
        self.history = f"123|{self.token}|COMPLETED|0:0"
        result = self.cli("watch", "test")
        self.assertTrue(Path(result["report"]).is_file())
        report = json.loads((self.folder / "report.json").read_text())
        self.assertEqual([p["seed"] for p in report["pairs"]], [0, 1])
        self.assertEqual(report["slurm"]["job_id"], "123")
        self.assertEqual(self.submit_count, 1)

    def test_conflict_and_changed_authorization(self):
        self.cli(*self.start)
        self.cli(*self.start, "--cpus", "2", expected=1)
        self.history = f"123|{self.token}|RUNNING|0:0\n456|{self.token}|RUNNING|0:0"
        self.cli("status", "test", expected=1)
        self.assertEqual(self.submit_count, 1)

    def test_failed_job_has_no_comparable_results(self):
        self.cli(*self.start)
        for group in ["A", "B"]:
            shutil.copytree(self.root / "experiment" / group, self.folder / "output" / group)
        self.history = f"123|{self.token}|FAILED|1:0"
        self.cli("watch", "test", expected=1)
        self.assertFalse(json.loads((self.folder / "report.json").read_text())["pairs"])

    def test_workload_cannot_replace_authorized_plan(self):
        self.cli(*self.start)
        (self.folder / "output/experiment.json").write_text("{}")
        self.history = f"123|{self.token}|COMPLETED|0:0"
        result = self.cli("status", "test", expected=1)
        self.assertIn("report_error", result)
        self.assertEqual(json.loads((self.folder / "report.json").read_text())["pairs"], [])

    def test_unknown_id_cannot_submit(self):
        self.cli("status", "unknown", expected=1)
        self.assertEqual(self.submit_count, 0)
