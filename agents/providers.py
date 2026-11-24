from __future__ import annotations

import json
import os
import uuid


class ModelTimeoutError(RuntimeError):
    pass


def is_timeout(exc):
    return isinstance(exc, TimeoutError) or type(exc).__name__ in {
        "APITimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"}


def tool_call(name, arguments):
    return {"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class DemoProvider:
    """Deterministic integration fixture, deliberately not an LLM."""
    def complete(self, messages, tools):
        last = messages[-1]
        if last["role"] == "tool":
            return {"role": "assistant", "content": "离线演示完成。工具结果：\n" + last["content"]}
        names = {t["function"]["name"] for t in tools}
        text = last.get("content", "")
        if "list_files" in names:
            return {"role": "assistant", "content": "离线演示：查看工作区文件。",
                    "tool_calls": [tool_call("list_files", {"path": "."})]}
        return {"role": "assistant", "content": "离线演示（非模型推理）：" + text[:200]}


class OpenAIProvider:
    def __init__(self, model, timeout=60, client=None, *, api_key=None, base_url=None,
                 direct=False, max_output_tokens=None, max_retries=2, thinking=None):
        if not model:
            raise ValueError("Set BEAR_MODEL to a model supported by your endpoint")
        self.model = model
        self.max_output_tokens = max_output_tokens
        self.thinking = thinking
        self.owns_client = client is None
        if client is None:
            from openai import OpenAI
            options = {}
            if direct:
                import httpx
                options["http_client"] = httpx.Client(trust_env=False, follow_redirects=False)
            client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"],
                            base_url=base_url or os.getenv("OPENAI_BASE_URL") or None,
                            timeout=timeout, max_retries=max_retries, **options)
        self.client = client

    def complete(self, messages, tools):
        args = {"model": self.model, "messages": messages}
        if self.max_output_tokens is not None:
            args["max_completion_tokens" if self.model.lower().startswith("gpt-") else "max_tokens"] = self.max_output_tokens
        if tools:
            args["tools"] = tools
        if self.thinking is not None:
            args["extra_body"] = {"thinking": {"type": self.thinking}}
        response = self.client.chat.completions.create(**args)
        message = response.choices[0].message
        finish = getattr(response.choices[0], "finish_reason", None)
        if not (message.content or "").strip() and not message.tool_calls:
            if finish == "length":
                raise ValueError("模型输出额度已用尽，但尚未生成正文；请提高 max_output_tokens 后恢复会话。")
            raise ValueError("模型返回空回答且没有工具调用；本轮未完成，请重试或切换模型。")
        result = {"role": "assistant", "content": message.content or ""}
        if message.tool_calls:
            result["tool_calls"] = [c.model_dump(include={"id", "type", "function"}) for c in message.tool_calls]
        return result

    def close(self):
        if self.owns_client:
            self.client.close()


def anthropic_messages(messages):
    system, translated = [], []
    for m in messages:
        if m["role"] == "system":
            system.append(m["content"])
            continue
        blocks = []
        role = m["role"]
        if role == "tool":
            role = "user"
            try:
                error = bool(json.loads(m["content"]).get("error"))
            except (ValueError, AttributeError):
                error = False
            blocks.append({"type": "tool_result", "tool_use_id": m["tool_call_id"],
                           "content": m["content"], "is_error": error})
        else:
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for call in m.get("tool_calls", []):
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                               "input": json.loads(call["function"]["arguments"])})
        if not blocks:
            blocks = [{"type": "text", "text": "(empty response)"}]
        if translated and translated[-1]["role"] == role:
            translated[-1]["content"].extend(blocks)
        else:
            translated.append({"role": role, "content": blocks})
    return "\n\n".join(system), translated


class AnthropicProvider:
    def __init__(self, model, timeout=60, client=None):
        if not model:
            raise ValueError("Set BEAR_MODEL")
        self.model = model
        if client is None:
            from anthropic import Anthropic
            client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"],
                               base_url=os.getenv("ANTHROPIC_BASE_URL") or None,
                               timeout=timeout, max_retries=2)
        self.client = client

    def complete(self, messages, tools):
        system, translated = anthropic_messages(messages)
        args = dict(model=self.model, system=system, messages=translated, max_tokens=4096)
        if tools:
            args["tools"] = [{"name": t["function"]["name"], "description": t["function"]["description"],
                              "input_schema": t["function"]["parameters"]} for t in tools]
        response = self.client.messages.create(**args)
        result = {"role": "assistant", "content": "\n".join(b.text for b in response.content if b.type == "text")}
        calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                 for b in response.content if b.type == "tool_use"]
        if calls:
            result["tool_calls"] = calls
        return result


def create_provider(config):
    factories = {"demo": lambda: DemoProvider(),
                 "openai": lambda: OpenAIProvider(config.model, config.model_timeout or config.timeout, api_key=config.api_key,
                     base_url=config.api_base_url, direct=config.direct_connection, max_output_tokens=config.max_output_tokens,
                     max_retries=config.model_retries, thinking=config.thinking),
                 "anthropic": lambda: AnthropicProvider(config.model, config.timeout)}
    if config.provider not in factories:
        raise ValueError("BEAR_PROVIDER must be demo, openai or anthropic")
    return factories[config.provider]()


def json_completion(provider, instruction, payload):
    answer = provider.complete([{"role": "system", "content": instruction + " Return ONLY a JSON object."},
                                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], [])
    text = answer.get("content", "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value
