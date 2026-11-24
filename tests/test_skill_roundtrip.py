import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from foldjay.retrieval import rank
from foldjay.skills import Skills
from foldjay.storage import Store


class SkillRoundtripTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.store = Store(self.workspace / ".foldjay")
        self.addCleanup(self.store.close)

    def cli(self, *args, expected_status=0):
        result = subprocess.run(
            [sys.executable, "-m", "foldjay.main", "--workspace", str(self.workspace), *args],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "FOLDJAY_PROVIDER": "demo"},
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, expected_status, result.stderr)
        return result.stdout

    def test_feedback_export_labels_and_evaluation_roundtrip(self):
        self.cli("run", "Review the release checklist")
        session = json.loads(self.cli("sessions"))[0]["id"]
        self.cli("run", "--resume", session, "经验：Always verify the release checklist before publishing.")
        sid = json.loads(self.cli("skills", "list"))[0]["id"]

        cases = json.loads(self.cli("skills", "replays", sid))
        self.assertEqual(len(cases), 1)
        case = cases[0]
        self.assertEqual(case["query"], "Review the release checklist")
        self.assertEqual(case["source"]["session"], session)
        self.assertEqual(case["expected_terms"], [])
        self.assertNotIn("payload", case)
        self.cli("eval", sid, expected_status=1)

        case["expected_terms"] = ["verify", "release"]
        path = self.workspace / "cases.json"
        path.write_text(json.dumps(cases))
        report = json.loads(self.cli("eval", sid, "--cases", str(path)))
        self.assertEqual(report["score"], 1)
        self.assertEqual(report["cases"], cases)
        self.assertEqual(json.loads(self.cli("skills", "promote", report["id"]))["skill"], sid)

        legacy = dict(self.store.db.execute("SELECT * FROM replays WHERE skill_id=?", (sid,)).fetchone())
        payload = json.loads(legacy["payload"])
        for outer_labels in (True, False):
            with self.subTest(outer_labels=outer_labels):
                if outer_labels:
                    old_case = {**legacy, "expected_terms": case["expected_terms"]}
                else:
                    payload["expected_terms"] = case["expected_terms"]
                    old_case = {**legacy, "payload": json.dumps(payload)}
                path.write_text(json.dumps([old_case]))
                report = json.loads(self.cli("eval", sid, "--cases", str(path)))
                self.assertEqual(report["cases"], cases)
                self.assertEqual(report["score"], 1)

    def test_overlapping_retrievals_report_their_own_committed_count(self):
        sid = Skills(self.store).maintain(
            {"title": "Release", "body": "Verify the release checklist before publishing."}, {})["id"]
        barrier = threading.Barrier(2, timeout=5)

        def rank_together(*args):
            ordered = rank(*args)
            barrier.wait()
            return ordered

        def search():
            store = Store(self.store.root)
            try:
                return Skills(store).search("release")[0]["retrieved"]
            finally:
                store.close()

        with patch("foldjay.skills.rank", side_effect=rank_together), ThreadPoolExecutor(max_workers=2) as pool:
            searches = [pool.submit(search) for _ in range(2)]
            self.assertEqual(sorted(job.result() for job in searches), [1, 2])
        self.assertEqual(Skills(self.store).get(sid)["retrieved"], 2)
