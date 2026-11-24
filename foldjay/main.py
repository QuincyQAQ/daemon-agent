from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

from .agent import Agent
from .config import Config
from .context import compact
from .mcp import MCPClient
from .memory import Memory
from .permissions import Policy
from .providers import create_provider
from .skills import Skills
from .storage import Store, atomic_write, dumps


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value,str) else value)


def approval(action, detail):
    print("\n审批 " + action + ":\n" + json.dumps(detail, ensure_ascii=False, indent=2), file=sys.stderr)
    if not sys.stdin.isatty():
        print("非交互模式默认拒绝写入、命令和外部工具。", file=sys.stderr)
        return False
    try:
        return input("允许本次操作？[y/N] ").strip().lower() == "y"
    except EOFError:
        return False


def events(kind, data):
    print("[" + kind + "] " + dumps(data), file=sys.stderr)


def error_message(exc):
    from .providers import is_timeout
    if is_timeout(exc):
        return "模型请求等待超时；这不等于密钥无效。会话已保存，可恢复后重试。"
    if type(exc).__module__.startswith(("openai", "anthropic", "httpx")):
        status = getattr(exc, "status_code", None)
        detail = str(getattr(exc, "body", ""))
        if status and status >= 500 and "ascii" in detail and "encode" in detail:
            return "模型服务端返回 HTTP " + str(status) + "：字符编码失败（ascii）。请联系服务管理员，或稍后重试。"
        return "模型请求失败：" + type(exc).__name__ + (" HTTP " + str(status) if status else "") + "；请检查端点、模型和凭据。"
    return "错误：" + str(exc)


def parser():
    p = argparse.ArgumentParser(description="Foldjay Agent — 本地 Agent、Memory、Skills、MCP")
    p.add_argument("--workspace", default=".")
    p.add_argument("--state", help="State directory (default: WORKSPACE/.foldjay, or existing .bear)")
    p.add_argument("--profile", type=Path, help="Explicit school API profile; use campus.sh for the default")
    sub = p.add_subparsers(dest="command")
    run = sub.add_parser("run", help="One-shot task or interactive REPL")
    run.add_argument("task", nargs="?")
    run.add_argument("--resume", metavar="SESSION_ID")
    run.add_argument("--retry", action="store_true", help="Retry a saved interrupted model request; requires --resume")
    run.add_argument("--mode", choices=["act", "plan"], default="act")
    run.add_argument("--plain", action="store_true", help="Use the plain interactive terminal and event log")
    run.add_argument("--max-steps", type=int, default=24)
    run.add_argument("--context-chars", type=int, default=128000)
    run.add_argument("--no-evolution", action="store_true")
    run.add_argument("--mcp-config", type=Path)
    sub.add_parser("demo", help="Run a deterministic offline end-to-end demo")
    sub.add_parser("doctor", help="Check local configuration without displaying credentials")
    campus = sub.add_parser("campus", help="Configure school API, discover models and verify tool calls")
    campus_actions = campus.add_subparsers(dest="action", required=True)
    setup = campus_actions.add_parser("setup")
    source = setup.add_mutually_exclusive_group()
    source.add_argument("--from-ccr", action="store_true")
    source.add_argument("--key-prompt", action="store_true")
    setup.add_argument("--model")
    setup.add_argument("--thinking", choices=["enabled", "disabled"], default="disabled")
    campus_actions.add_parser("models")
    campus_actions.add_parser("check")
    select = campus_actions.add_parser("select")
    select.add_argument("model")
    sub.add_parser("sessions", help="List sessions")
    audit = sub.add_parser("audit", help="Inspect recent tool/evolution/recovery events")
    audit.add_argument("--limit", type=int, default=30)
    skills = sub.add_parser("skills")
    skill_actions = skills.add_subparsers(dest="action", required=True)
    for action in ["list", "show", "search", "add", "merge", "discard", "history", "replays", "promote", "export", "archive", "activate"]:
        item = skill_actions.add_parser(action)
        item.set_defaults(value=None, file=None, title="Reusable workflow")
        if action not in {"list", "add"}:
            item.add_argument("value")
        if action in {"add", "merge"}:
            item.add_argument("--file", type=Path, required=True, help="UTF-8 workflow body")
            item.add_argument("--title", default="Reusable workflow")
    memory = sub.add_parser("memory")
    memory_actions = memory.add_subparsers(dest="action", required=True)
    for action in ["add", "search", "list", "delete"]:
        item = memory_actions.add_parser(action)
        item.set_defaults(value=None, kind="fact")
        if action != "list":
            item.add_argument("value")
        if action == "add":
            item.add_argument("--kind", choices=["fact", "preference"], default="fact")
    evaluate = sub.add_parser("eval")
    evaluate.add_argument("skill")
    evaluate.add_argument("--cases", type=Path, help="JSON list with query and expected_terms; defaults to labelled replays")
    evaluate.add_argument("--candidate", type=Path)
    evaluate.add_argument("--judge", action="store_true", help="Use configured live model as additional judge")
    evaluate.add_argument("--output", type=Path)
    return p


def repl(agent, view=None):
    show = view.detail if view else output
    answer = view.answer if view else output
    if view:
        view.welcome(agent)
    else:
        show("Foldjay Agent · /help 查看命令 · Ctrl-D 退出")
    if agent.retry_checkpoint() is not None:
        show("上次请求尚未完成。输入 /retry 可沿用已保存的任务和工具结果。")
    while True:
        try:
            text = (view.read(agent.config.mode) if view else input("foldjay> ")).strip()
        except EOFError:
            return
        if not text:
            continue
        command, _, arg = text.partition(" ")
        try:
            if command in {"/exit", "/quit"}:
                return
            elif command == "/help":
                if view:
                    view.help()
                    continue
                show("/plan /act /compact /retry /skills [query] /memory query /feedback text /extract_now text /session /exit")
            elif command in {"/plan", "/act"}:
                agent.config.mode = agent.policy.mode = command[1:]
                show("mode=" + command[1:])
            elif command == "/compact":
                show(compact(agent.session, agent.store, agent.provider, max_chars=agent.config.context_chars))
            elif command == "/retry":
                answer(agent.retry())
            elif command == "/skills":
                show(agent.skills.search(arg) if arg else agent.skills.list())
            elif command == "/memory":
                show(agent.memory.search(arg))
            elif command in {"/feedback", "/extract_now"}:
                show(agent.feedback(arg))
            elif command == "/session":
                show(agent.session["id"])
            elif command.startswith("/"):
                show("未知命令，使用 /help")
            else:
                answer(agent.run(text))
        except Exception as exc:
            if view:
                view.stop()
            print(error_message(exc), file=sys.stderr)
            agent.session = agent.store.load(agent.session["id"], agent.config.workspace)


def run_agent(args, config, store):
    provider = create_provider(config)
    session = {"id": args.resume} if args.resume else store.new(config.workspace)
    clients = []
    with store.session_lock(session["id"]):
        if args.resume:
            session = store.load(args.resume, config.workspace)
        view = None
        if (not args.task and not args.retry and not args.plain
                and all(stream.isatty() for stream in (sys.stdin, sys.stdout, sys.stderr))):
            try:
                from .terminal import TerminalChat
            except ModuleNotFoundError as exc:
                if exc.name != "rich":
                    raise
            else:
                view = TerminalChat()
        agent = Agent(config, store, provider, approval, session, event=view.event if view else events)
        store.save(session)
        if not view:
            print("session=" + session["id"], file=sys.stderr)
        try:
            if args.mcp_config:
                settings = json.loads(args.mcp_config.read_text())
                for name, entry in settings.get("servers", {}).items():
                    agent.policy.check("mcp.start", {"server": name, "command": entry["command"]}, "execute")
                    # Values are environment variable names, not secrets stored in the config file.
                    env = {key: os.environ[value] for key, value in entry.get("env_from", {}).items()}
                    client = MCPClient(entry["command"], config.workspace, env, config.timeout)
                    clients.append(client)
                    client.register(agent.tools, name)
            if args.retry:
                output(agent.retry())
            elif args.task:
                output(agent.run(args.task))
            else:
                repl(agent, view)
        finally:
            if view:
                view.stop()
            for client in reversed(clients):
                client.close()
            if hasattr(provider, "close"):
                provider.close()


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if getattr(args, "retry", False) and (not args.resume or args.task):
        p.error("--retry requires --resume SESSION_ID and no new task text")
    if args.command is None:
        p.print_help()
        return 0
    store = None
    try:
        if args.command == "campus":
            from .campus import command
            output(command(args))
            return 0
        config = Config.from_env(args.workspace, args.state,
            mode=getattr(args,"mode","act"), max_steps=getattr(args,"max_steps",24),
            context_chars=getattr(args,"context_chars",48000), evolution=not getattr(args,"no_evolution",False))
        if args.profile:
            from .campus import apply_profile
            config = apply_profile(config, args.profile)
        if args.command == "doctor":
            output({"python": sys.version.split()[0], "workspace": str(config.workspace), "state": str(config.state),
                    "provider": config.provider, "model": config.model or "(unset)",
                    "base_url": config.api_base_url or os.getenv("OPENAI_BASE_URL"),
                    "thinking": config.thinking, "model_timeout": config.model_timeout or config.timeout,
                    "model_retries": config.model_retries,
                    "profile_credential_present": bool(config.api_key),
                    "sdk": {k: importlib.util.find_spec(k) is not None for k in ("openai", "anthropic")},
                    "credentials_present": {k: bool(os.getenv(k)) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")},
                    "network_checked": False})
            return 0
        store = Store(config.state)
        skills, memory = Skills(store), Memory(store)
        if args.command == "run":
            run_agent(args, config, store)
        elif args.command == "demo":
            from .demo import run_demo
            output(run_demo(config.state / "demo"))
        elif args.command == "sessions":
            sessions = []
            for file in sorted((config.state / "sessions").glob("*.json")):
                s = json.loads(file.read_text())
                sessions.append({k: s.get(k) for k in ("id", "workspace", "parent", "updated")})
            output(sessions)
        elif args.command == "audit":
            if not 1 <= args.limit <= 1000:
                raise ValueError("Audit limit must be 1–1000")
            output([dict(r) for r in store.db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (args.limit,))])
        elif args.command == "memory":
            if args.action not in {"list"} and not args.value:
                raise ValueError("This action requires a value")
            if args.action == "add":
                output({"id": memory.add(args.value, args.kind)})
            elif args.action == "search":
                output(memory.search(args.value))
            elif args.action == "delete":
                memory.delete(args.value)
                output("deleted")
            else:
                output([dict(r) for r in store.db.execute("SELECT * FROM memories")])
        elif args.command == "skills":
            if args.action == "list":
                output(skills.list())
            elif args.action in {"show", "search", "promote", "export", "history", "replays", "archive", "activate"}:
                if not args.value:
                    raise ValueError("This action requires a skill ID or query")
                if args.action == "show":
                    output(skills.get(args.value))
                elif args.action == "search":
                    output(skills.search(args.value))
                elif args.action == "promote":
                    output(skills.promote(args.value))
                elif args.action == "export":
                    skills.materialize(args.value)
                    output(str(store.root / "skills" / args.value / "SKILL.md"))
                elif args.action in {"archive", "activate"}:
                    output(skills.set_status(args.value, "archived" if args.action == "archive" else "active"))
                elif args.action == "replays":
                    output(skills.replays(args.value))
                else:
                    output([dict(r) for r in store.db.execute("SELECT * FROM history WHERE skill_id=?", (args.value,))])
            else:
                if args.action != "discard" and args.file is None:
                    raise ValueError("add/merge requires --file")
                candidate = {"title": args.title, "body": args.file.read_text() if args.file else args.value or ""}
                output(skills.maintain(candidate, {"origin": "user-cli"}, args.action, args.value))
        elif args.command == "eval":
            cases = json.loads(args.cases.read_text()) if args.cases else skills.replays(args.skill)
            if args.judge and config.provider == "demo":
                raise ValueError("LLM judge requires a live provider; demo is not an LLM")
            report = skills.evaluate(args.skill, cases, create_provider(config) if args.judge else None,
                                     args.candidate.read_text() if args.candidate else None)
            if args.output:
                atomic_write(args.output, json.dumps(report, ensure_ascii=False, indent=2))
            output(report)
        return 0
    except KeyboardInterrupt:
        print("已中断；已保存的会话可用 --resume 恢复。", file=sys.stderr)
        return 130
    except Exception as exc:
        # SDK exceptions can contain request material: avoid printing them or API keys.
        print(error_message(exc), file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
