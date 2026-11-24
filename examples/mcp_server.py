#!/usr/bin/env python3
"""Minimal stdio MCP fixture: deterministic text tools, never logs on stdout."""
import json
import sys


def main():
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if "id" not in request:
            continue
        error = None
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "bear-example", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "word_count", "description": "Count whitespace-separated words in text",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}}]}
        elif method == "tools/call":
            params = request.get("params", {})
            text = params.get("arguments", {}).get("text")
            if params.get("name") != "word_count" or not isinstance(text, str):
                result = {"isError": True, "content": [{"type": "text", "text": "Invalid tool or text"}]}
            else:
                result = {"content": [{"type": "text", "text": str(len(text.split()))}]}
        elif method == "ping":
            result = {}
        else:
            error = {"code": -32601, "message": "Method not found"}
        response = {"jsonrpc": "2.0", "id": request["id"]}
        response.update({"error": error} if error else {"result": result})
        print(json.dumps(response), flush=True)


if __name__ == "__main__":
    main()
