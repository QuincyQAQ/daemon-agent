import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foldjay.config import Config
from foldjay.memory import Memory
from foldjay.permissions import Denied, Policy
from foldjay.storage import Store


class WorkspaceUpgradeTest(unittest.TestCase):
    def test_existing_database_and_session_survive_rebrand(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            legacy = workspace / ".bear"
            old = Store(legacy)
            try:
                memory_id = Memory(old).add("remember this workspace")
                session = old.new(workspace)
                old.save(session)
            finally:
                old.close()
            (legacy / "foldjay.sqlite3").rename(legacy / "bear.sqlite3")
            upgraded = Config.from_env(workspace)
            self.assertEqual(upgraded.state, legacy)
            with self.subTest("resume"):
                store = Store(upgraded.state)
                try:
                    self.assertEqual(store.load(session["id"], workspace)["id"], session["id"])
                    self.assertEqual(Memory(store).search("workspace")[0]["id"], memory_id)
                    self.assertFalse((legacy / "foldjay.sqlite3").exists())
                finally:
                    store.close()

    def test_new_workspace_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BEAR_MODEL": "old"}, clear=True):
            workspace = Path(directory)
            config = Config.from_env(workspace)
            self.assertEqual(config.state, workspace / ".foldjay")
            self.assertEqual(config.model, "old")
            os.environ["FOLDJAY_MODEL"] = "new"
            self.assertEqual(Config.from_env(workspace).model, "new")
            policy = Policy(workspace, workspace / "custom-state")
            for dirname in (".foldjay", ".bear"):
                with self.subTest(dirname=dirname), self.assertRaises(Denied):
                    policy.path(dirname + "/campus.json")
