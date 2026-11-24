"""Exercise the optional SDKs' real serializers and response models without network access."""
import importlib.util
import json
import unittest

from agents.providers import AnthropicProvider, OpenAIProvider


TOOLS = [{"type": "function", "function": {"name": "lookup", "description": "Read a sample",
          "parameters": {"type": "object", "properties": {}, "required": []}}}]
TASK = [{"role": "system", "content": "Inspect the sample."}, {"role": "user", "content": "Read it"}]


class SDKTransportTest(unittest.TestCase):
    def round_trip(self, provider):
        first = provider.complete(TASK, TOOLS)
        call = first["tool_calls"][0]
        self.assertEqual((call["id"], call["function"]["name"]), ("call_1", "lookup"))
        self.assertEqual(json.loads(call["function"]["arguments"]), {})
        observation = {"role": "tool", "tool_call_id": call["id"], "content": "sample text"}
        final = provider.complete([*TASK, first, observation], [])
        self.assertEqual(final["content"], "sample text")

    @unittest.skipUnless(importlib.util.find_spec("anthropic"), "install .[models] for the Anthropic SDK test")
    def test_anthropic_tool_result_over_http(self):
        import anthropic
        import httpx

        def endpoint(request):
            self.assertEqual(request.url.path, "/v1/messages")
            payload = json.loads(request.content)
            self.assertEqual(payload["system"], TASK[0]["content"])
            if "tools" in payload:
                self.assertEqual(payload["tools"][0]["input_schema"], TOOLS[0]["function"]["parameters"])
                content = [{"type": "tool_use", "id": "call_1", "name": "lookup", "input": {}}]
                stop = "tool_use"
            else:
                result = payload["messages"][-1]["content"][0]
                self.assertEqual((result["type"], result["tool_use_id"], result["content"]),
                                 ("tool_result", "call_1", "sample text"))
                content = [{"type": "text", "text": result["content"]}]
                stop = "end_turn"
            return httpx.Response(200, json={"id": "msg_1", "type": "message", "role": "assistant",
                "model": "fixture", "content": content, "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1}})

        with anthropic.Anthropic(api_key="fixture", base_url="https://sdk.invalid",
                                 http_client=httpx.Client(transport=httpx.MockTransport(endpoint))) as client:
            self.round_trip(AnthropicProvider("fixture", client=client))

    @unittest.skipUnless(importlib.util.find_spec("openai"), "install .[models] for the OpenAI SDK test")
    def test_openai_tool_result_over_http(self):
        import httpx
        import openai

        def endpoint(request):
            self.assertEqual(request.url.path, "/v1/chat/completions")
            payload = json.loads(request.content)
            if "tools" in payload:
                self.assertEqual(payload["tools"], TOOLS)
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1",
                    "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}
                stop = "tool_calls"
            else:
                observation = payload["messages"][-1]
                self.assertEqual(observation, {"role": "tool", "tool_call_id": "call_1", "content": "sample text"})
                message = {"role": "assistant", "content": observation["content"]}
                stop = "stop"
            return httpx.Response(200, json={"id": "res_1", "object": "chat.completion", "created": 0,
                "model": "fixture", "choices": [{"index": 0, "message": message, "finish_reason": stop}]})

        with openai.OpenAI(api_key="fixture", base_url="https://sdk.invalid/v1",
                           http_client=httpx.Client(transport=httpx.MockTransport(endpoint))) as client:
            self.round_trip(OpenAIProvider("fixture", client=client))
