"""A labelled offline integration scenario, with actual filesystem/tool execution."""
import json
import uuid
from .agent import Agent
from .config import Config
from .context import compact
from .providers import DemoProvider, tool_call
from .storage import Store, atomic_write


class Scenario(DemoProvider):
    def __init__(self):
        self.step = 0

    def complete(self, messages, tools):
        self.step += 1
        if self.step == 1:
            name, args = "write_file", {"path": "demo-note.txt", "content": "Hello Foldjay\n", "expected_hash": "NEW"}
        elif self.step == 2:
            name, args = "read_file", {"path": "demo-note.txt"}
        elif self.step == 3:
            previous = json.loads(messages[-1]["content"])
            name, args = "edit_file", {"path": "demo-note.txt", "old": "Hello", "new": "Verified", "expected_hash": previous["sha256"]}
        elif self.step == 4:
            name, args = "read_file", {"path": "demo-note.txt"}
        else:
            return {"role": "assistant", "content": "已通过真实工具创建、读取、校验哈希、编辑并复读 demo-note.txt。此流程由离线脚本驱动。"}
        return {"role": "assistant", "content": "", "tool_calls": [tool_call(name, args)]}


def run_demo(base):
    workspace = base / uuid.uuid4().hex[:8]
    workspace.mkdir(parents=True)
    config = Config(workspace, workspace / ".foldjay")
    store = Store(config.state)
    try:
        agent = Agent(config, store, Scenario(), approve=lambda action, detail: detail.get("path") == "demo-note.txt")
        with store.session_lock(agent.session["id"]):
            answer = agent.run("创建并验证一个文本文件")
            learned = agent.feedback("经验：编辑文本文件时，先读取文件获取 sha256，确认旧文本只匹配一次，修改后重新读取并检查预期内容。")
            skill = learned["id"]
            evaluation = agent.skills.evaluate(skill, [{"query": "如何可靠编辑文本文件", "expected_terms": ["sha256", "重新读取", "匹配一次"]}])
            champion = agent.skills.promote(evaluation["id"])
            folded = compact(agent.session, store, DemoProvider())
            resumed = store.load(agent.session["id"], workspace)
            child = Agent(config, store, DemoProvider(), session=resumed).subagent("检查文件", "explore")
        report = {"mode": "offline scripted integration, not a model benchmark", "workspace": str(workspace),
                  "session": agent.session["id"], "answer": answer,
                  "file_content": (workspace / "demo-note.txt").read_text(), "learned": learned,
                  "evaluation": evaluation, "champion": champion, "compaction": folded,
                  "resumed": resumed["id"] == agent.session["id"], "child": child}
        atomic_write(base / "latest-report.json", json.dumps(report, ensure_ascii=False, indent=2))
        return report
    finally:
        store.close()
