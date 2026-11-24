import sys
import tempfile
import unittest
from pathlib import Path

from agents.mcp import MCPClient
from agents.permissions import Policy
from agents.storage import Store
from agents.tools import Tools


# A stdio fixture with a JSON Schema union that accepts a string or null.
NULLABLE_MCP_SERVER = """
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": sys.argv[1], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "nullable", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "inputSchema": {"type": "object",
                  "required": ["value"], "properties": {"value": {"type": ["string", "null"]}}}}]}
    elif method == "tools/call":
        value = request["params"]["arguments"]["value"]
        if value is None or isinstance(value, str):
            result = {"content": [{"type": "text", "text": json.dumps(value)}]}
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "Expected string or null"}]}
    else:
        raise ValueError(method)
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
"""


class ToolBoundaryTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / ".bear")
        self.addCleanup(self.store.close)
        self.policy = Policy(self.root, self.store.root, approve=lambda *args: True)
        self.tools = Tools(self.policy, self.store, timeout=2)

    def test_search_match_limit(self):
        fixture = self.root / "lines.txt"
        for count in (49, 50, 51):
            with self.subTest(count=count):
                fixture.write_text("Needle\n" * count)
                result = self.tools.call("search", {"query": "needle", "path": fixture.name})
                self.assertEqual([item["line"] for item in result["matches"]], list(range(1, min(count, 50) + 1)))
                self.assertEqual(result["truncated"], count > 50)

    def test_search_file_limit(self):
        for index in range(1, 5000):
            (self.root / f"{index:04}.txt").touch()
        (self.root / "5000.txt").write_text("last scanned")
        result = self.tools.call("search", {"query": "last scanned"})
        self.assertEqual([match["path"] for match in result["matches"]], ["5000.txt"])
        self.assertFalse(result["truncated"])
        (self.root / "5001.txt").write_text("outside scan")
        self.assertEqual(self.tools.call("search", {"query": "outside scan"}), {"matches": [], "truncated": True})

    def test_shell_limits_decoded_characters(self):
        for output in ("中" * 6000, "😀中\r\n" * 4000, "😀" * 16001):
            with self.subTest(length=len(output)):
                (self.root / "output.txt").write_bytes(output.encode("utf-8"))
                result = self.tools.call("shell", {"command": "cat output.txt"})
                self.assertEqual(result["exit_code"], 0)
                self.assertEqual(result["output"], output[:16000])
                self.assertEqual(result["truncated"], len(output) > 16000)

    def test_mcp_nullable_schema_with_approvals_and_server_validation(self):
        for version in ("2024-11-05", "2025-06-18"):
            with self.subTest(version=version):
                client = MCPClient([sys.executable, "-c", NULLABLE_MCP_SERVER, version], self.root, timeout=2)
                self.addCleanup(client.close)
                tools = Tools(self.policy, self.store)
                client.register(tools, "test")
                tool = "mcp_test_echo"
                for value, expected in [(None, "null"), ("hello", '"hello"')]:
                    result = tools.call(tool, {"value": value})
                    self.assertEqual(result["content"][0]["text"], expected)
                invalid = tools.call(tool, {"value": 42})
                self.assertEqual(invalid["content"][0]["text"], "Expected string or null")
                self.assertIn("error", invalid)

                self.policy.approve = lambda *args: False
                self.assertIn("Approval denied", tools.call(tool, {"value": None})["error"])
                self.policy.approve = lambda *args: True
                self.policy.mode = "plan"
                self.assertIn("Plan Mode", tools.call(tool, {"value": None})["error"])
                self.policy.mode = "act"
                self.assertIn("invalid type", tools.call("read_file", {"path": None})["error"])
