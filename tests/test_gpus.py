import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from foldjay.gpus import parse_nodes, select
from foldjay.permissions import Policy
from foldjay.storage import Store
from foldjay.tools import Tools


def node(name="n1", state="MIXED", gres="gpu:a40:8(S:0)", used="gpu:a40:4(IDX:0-1,4,7)",
         extra="CPUAlloc=2 CPUEfctv=16 RealMemory=1024 AllocMem=256"):
    return (f"NodeName={name} State={state} Gres={gres} GresUsed={used} GresDrain=N/A "
            f"Partitions=debug,rent {extra}")


class InventoryTest(unittest.TestCase):
    def snapshot(self, *records):
        rows = parse_nodes("\n".join(records), {"debug": "up", "rent": "down"})
        return {"observed_at": "instant", "source": "test", "rows": rows}

    def test_indices_node_totals_and_partition_filters(self):
        data = self.snapshot(node(), node("n2", state="IDLE+DRAIN", used="gpu:a40:0(IDX:N/A)"),
                             node("cpu", gres="(null)", used="gpu:0"))
        report = select(data, free_only=True)
        self.assertEqual((report["total"], report["known_unallocated"], report["candidate_gpus"]), (16, 12, 4))
        self.assertEqual(report["rows"][0]["free_ids"], [2, 3, 5, 6])
        self.assertIsNone(data["rows"][1]["free_ids"])
        self.assertEqual(select(data, free_only=True, partition="rent")["rows"], [])
        self.assertEqual(select(data, model="A40")["models"]["a40"]["total"], 16)

    def test_missing_usage_is_unknown_and_saturated_cpu_is_blocked(self):
        data = self.snapshot(node("n1", used="(null)"), node("n2", extra="CPUAlloc=16 CPUEfctv=16"))
        report = select(data, free_only=True)
        self.assertEqual(report["unknown_capacity"], 8)
        self.assertFalse(report["rows"])

    def test_heterogeneous_models_and_missing_ids(self):
        data = self.snapshot(node(gres="gpu:a40:2,gpu:a800:2", used="gpu:a40:1(IDX:2),gpu:a800:0(IDX:N/A)"))
        report = select(data, free_only=True)
        self.assertEqual((report["total"], report["candidate_gpus"]), (4, 3))
        self.assertTrue(all(row["free_ids"] is None for row in report["rows"]))

    def test_fallback_tres_and_unavailable_indices(self):
        data = self.snapshot(node(gres="(null)", used="(null)",
            extra="CfgTRES=cpu=8,gres/gpu=4,gres/gpu:a40=4 AllocTRES=cpu=2,gres/gpu=1"))
        row = data["rows"][0]
        self.assertEqual((row["model"], row["unallocated"], row["free_ids"]), ("a40", 3, None))

    def test_missing_and_conflicting_counters_are_not_free(self):
        data = self.snapshot(node(used="(null)", extra="CfgTRES=gres/gpu=8 AllocTRES=(null)"),
                             node("n2", extra="CfgTRES=gres/gpu=8 AllocTRES=gres/gpu=7"))
        report = select(data, free_only=True)
        self.assertFalse(report["rows"])
        self.assertEqual(report["unknown_capacity"], 16)

    def test_plan_tool_paginates_one_snapshot(self):
        snapshot = self.snapshot(*(node(f"n{i}") for i in range(15)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = Store(root / ".foldjay")
            self.addCleanup(store.close)
            tools = Tools(Policy(root, store.root, "plan"), store)
            with patch("foldjay.gpus.inventory", return_value=snapshot) as fetch:
                first = tools.call("gpu_inventory", {"free_only": True})
                second = tools.call("gpu_inventory", {"free_only": True, "offset": first["next_offset"],
                                                      "snapshot": first["observed_at"]})
                self.assertEqual(len(first["rows"]) + len(second["rows"]), 15)
                self.assertIsNone(second["next_offset"])
                self.assertLess(len(json.dumps(first)), 16000)
                self.assertIn("error", tools.call("gpu_inventory", {"offset": 12, "snapshot": "expired"}))
                fetch.assert_called_once()
