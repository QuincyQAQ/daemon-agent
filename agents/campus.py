"""HKUST(GZ) AIGC profile. Credentials are resolved only from an explicit source."""
from __future__ import annotations

import getpass
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from .config import Config
from .providers import OpenAIProvider
from .storage import atomic_write, dumps


BASE_URL = "https://aigc-api.hkust-gz.edu.cn/api/v2"
PROFILE = Path(__file__).resolve().parents[1] / ".bear" / "campus.json"
MAX_OUTPUT_TOKENS = 16384
MODEL_TIMEOUT = 90


def normalize_base_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "aigc-api.hkust-gz.edu.cn"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("Campus credentials may only be sent to the HTTPS school endpoint")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    if path != "/api/v2":
        raise ValueError("Campus profile requires the /api/v2 endpoint")
    return BASE_URL


def load_key(profile):
    normalize_base_url(profile["base_url"])
    source = profile["credential"]
    if source["kind"] == "ccr":
        data = json.loads(Path(source["path"]).expanduser().read_text())
        matches = [p for p in data.get("Providers", []) if p.get("name") == source["provider"]]
        if len(matches) != 1:
            raise ValueError("Expected exactly one configured CCR provider")
        provider = matches[0]
        normalize_base_url(provider["api_base_url"])
        key = provider.get("api_key", "")
    elif source["kind"] == "file":
        path = Path(source["path"]).expanduser()
        if path.stat().st_mode & 0o077:
            raise ValueError("Campus key file must have permissions 600")
        key = path.read_text().strip()
    elif source["kind"] == "env":
        key = os.getenv("OPENAI_API_KEY", "")
    else:
        raise ValueError("Unknown campus credential source")
    if not isinstance(key,str) or not key.strip() or "\n" in key or "\r" in key:
        raise ValueError("School API key is missing or invalid")
    return key


def read_profile(path=PROFILE):
    if not path.exists():
        raise ValueError("Campus profile is not configured. Run ./bear.sh campus setup --from-ccr")
    profile = json.loads(path.read_text())
    normalize_base_url(profile["base_url"])
    return profile


def chat_models(ids):
    return sorted({m for m in ids if isinstance(m,str) and m.strip()
                   and not any(t in m.lower() for t in ("embedding", "audio", "voice", "whisper", "tts", "image", "rerank"))})


def thinking_mode(profile, model):
    mode = os.getenv("BEAR_THINKING") or profile.get("thinking", "disabled")
    if mode not in {"enabled", "disabled"}:
        raise ValueError("BEAR_THINKING / profile thinking must be enabled or disabled")
    return mode if "deepseek" in model.lower() or model.lower() in {"dsp", "dsf"} else None


def provider_for(profile, model=None, key=None):
    selected = model or profile["model"]
    return OpenAIProvider(selected, timeout=MODEL_TIMEOUT,
                          api_key=key or load_key(profile), base_url=normalize_base_url(profile["base_url"]),
                          direct=True, max_output_tokens=MAX_OUTPUT_TOKENS, max_retries=0,
                          thinking=thinking_mode(profile, selected))


def discover(profile, key=None):
    provider = provider_for(profile, model="catalog-only", key=key)
    try:
        return chat_models([m.id for m in provider.client.models.list().data])
    finally:
        provider.close()


def choose_model(models, requested=None, preferred=None):
    if not models:
        raise ValueError("No supported chat models found in live catalog")
    if requested:
        if requested not in models:
            raise ValueError("Model is not in the current filtered school catalog")
        return requested
    if preferred in models:
        return preferred
    # Preferences only: every choice MUST exist in the live catalog.
    return next((m for m in ("GLM-5.3-Flash", "Qwen3.8-27B", "gpt-6-luna") if m in models), models[0])


def setup(path=PROFILE, *, from_ccr=False, key_prompt=False, model=None, ccr_path=None, thinking="disabled"):
    profile = {"version": 1, "base_url": BASE_URL, "thinking": thinking}
    key = None
    preferred = None
    if from_ccr:
        ccr_path = (ccr_path or Path.home()/".claude-code-router/config.json").expanduser().resolve()
        data = json.loads(ccr_path.read_text())
        providers = []
        for item in data.get("Providers", []):
            try:
                normalize_base_url(item.get("api_base_url", ""))
                providers.append(item)
            except ValueError:
                continue
        if len(providers) != 1:
            raise ValueError("Expected one school provider in CCR configuration")
        profile["credential"] = {"kind": "ccr", "path": str(ccr_path), "provider": providers[0]["name"]}
        route_provider, _, route_model = data.get("Router", {}).get("default", "").partition(",")
        if route_provider == providers[0]["name"]:
            preferred = route_model
    elif key_prompt:
        key = getpass.getpass("School API key (hidden): ").strip()
        if not key or "\n" in key or "\r" in key:
            raise ValueError("Missing or invalid API key")
        profile["credential"] = {"kind": "file", "path": str(path.parent / ".env.campus-key")}
    else:
        profile["credential"] = {"kind": "env"}
    models = discover(profile, key=key)
    profile["model"] = choose_model(models, model, preferred)
    if key_prompt:
        key_path = Path(profile["credential"]["path"])
        atomic_write(key_path, key + "\n")
        key_path.chmod(0o600)
    atomic_write(path, json.dumps(profile, ensure_ascii=False, indent=2))
    path.chmod(0o600)
    return {"configured": True, "profile": str(path), "base_url": BASE_URL,
            "model": profile["model"], "credential_source": profile["credential"]["kind"], "models": models}


def apply_profile(config, path=PROFILE):
    profile = read_profile(path)
    model = os.getenv("BEAR_MODEL") or profile["model"]
    if not chat_models([model]):
        raise ValueError("Selected model is excluded by the campus chat-model filter")
    return replace(config, provider="openai", model=model, api_base_url=BASE_URL,
                   api_key=load_key(profile), direct_connection=True,
                   max_output_tokens=MAX_OUTPUT_TOKENS, model_timeout=MODEL_TIMEOUT,
                   model_retries=0, thinking=thinking_mode(profile, model))


def check(path=PROFILE):
    """Run a full read-only Agent tool round trip with synthetic local data only."""
    from .agent import Agent
    from .storage import Store
    profile = read_profile(path)
    provider = provider_for(profile)
    try:
        with tempfile.TemporaryDirectory(prefix="bear-campus-check-") as directory:
            workspace = Path(directory)
            marker = "HARNESS_CAMPUS_VERIFIED_" + os.urandom(6).hex()
            (workspace / "probe.txt").write_text(marker + "\n")
            config = Config(workspace, workspace/".bear", mode="plan", evolution=False, max_steps=12)
            store = Store(config.state)
            try:
                agent = Agent(config, store, provider)
                try:
                    answer = agent.run("Use read_file to read probe.txt. Reply with its exact contents only. Do not guess.")
                except Exception as exc:
                    # Persist only controlled diagnostic fields, never SDK bodies/headers/credentials.
                    detail = str(getattr(exc, "body", ""))
                    report = {"model": profile["model"], "base_url": BASE_URL, "verified": False,
                              "error_type": type(exc).__name__, "http_status": getattr(exc,"status_code",None),
                              "server_ascii_encoding_error": "ascii" in detail and "encode" in detail,
                              "scope": "live model + Harness read-only tools + synthetic file"}
                    atomic_write(path.parent/"campus-check.json", json.dumps(report, ensure_ascii=False, indent=2))
                    raise
                messages = agent.session["messages"]
                tools = [c["function"]["name"] for m in messages for c in m.get("tool_calls", [])]
                actual = any(m["role"] == "tool" and marker in m["content"] for m in messages)
                report = {"model": profile["model"], "base_url": BASE_URL,
                          "tool_names": tools, "verified": "read_file" in tools and actual and marker in answer,
                          "answer": answer, "scope": "live model + Harness read-only tools + synthetic file"}
                atomic_write(path.parent/"campus-check.json", json.dumps(report, ensure_ascii=False, indent=2))
                if not report["verified"]:
                    raise RuntimeError("Live tool verification failed; inspect .bear/campus-check.json")
                return report
            finally:
                store.close()
    finally:
        provider.close()


def command(args):
    path = args.profile or PROFILE
    if args.action == "setup":
        return setup(path, from_ccr=args.from_ccr, key_prompt=args.key_prompt, model=args.model, thinking=args.thinking)
    if args.action == "check":
        return check(path)
    profile = read_profile(path)
    models = discover(profile)
    if args.action == "select":
        profile["model"] = choose_model(models, args.model)
        atomic_write(path, json.dumps(profile, ensure_ascii=False, indent=2))
    return {"model": profile["model"], "models": models}
