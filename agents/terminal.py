"""Rich presentation for interactive terminals. The runtime does not depend on it."""
import json
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule
from rich.table import Table
from rich.text import Text


def visible(value):
    """Allow text and indentation, but never interpret terminal controls from a model."""
    return "".join(ch for ch in str(value) if ch in "\n\t" or (ord(ch) >= 32 and not 127 <= ord(ch) <= 159))


class TerminalChat:
    def __init__(self):
        self.out = Console(highlight=False)
        self.err = Console(file=sys.stderr, highlight=False)
        self.status = None

    def welcome(self, agent):
        config = agent.config
        self.out.print()
        self.out.print(Rule(Text("Bear Harness", style="bold cyan"), align="left", style="cyan"))
        self.out.print(Text.assemble((visible(config.model or config.provider), "bold"),
                                     ("  ·  ", "dim"), visible(config.workspace.name or config.workspace),
                                     ("  ·  " + agent.session["id"][:8], "dim")))
        self.out.print("/help 命令    /session 会话    Ctrl-D 退出", style="dim", markup=False)
        self.out.print()

    def read(self, mode):
        color = "cyan" if mode == "plan" else "yellow"
        return self.out.input(Text.assemble((mode.upper(), "bold " + color), (" › ", color)))

    def answer(self, text):
        self.out.print()
        self.out.print(Text("Bear", style="bold cyan"))
        self.out.print(Markdown(visible(text), hyperlinks=False))
        self.out.print()

    def help(self):
        table = Table.grid(padding=(0, 2))
        table.add_column(style="cyan")
        table.add_column()
        for command, description in [
            ("/plan  /act", "切换只读规划或执行模式"),
            ("/retry", "接续上次中断的模型请求"),
            ("/compact", "压缩当前会话上下文"),
            ("/skills [关键词]", "列出或搜索技能"),
            ("/memory 关键词", "搜索项目记忆"),
            ("/feedback 反馈", "从上一轮反馈提取技能，也可用 /extract_now"),
            ("/session  /exit", "显示会话 ID 或退出"),
        ]:
            table.add_row(command, description)
        self.out.print(table)

    def stop(self):
        if self.status is not None:
            self.status.stop()
            self.status = None

    def event(self, kind, data):
        if kind == "model_start":
            self.stop()
            label = "正在整理回答" if data["finalizing"] else "正在思考"
            self.status = self.err.status(label, spinner="dots", spinner_style="cyan")
            self.status.start()
        elif kind == "model_wait" and self.status is not None:
            self.status.update(f"等待模型 · {data['seconds']:.0f} 秒")
        elif kind in {"model_end", "model_timeout"}:
            self.stop()
        elif kind == "tool_start":
            self.err.print(Text.assemble(("  ↳ ", "dim cyan"), (visible(data["name"]), "cyan"),
                                         ("  " + visible(data.get("path", "")), "dim")))
        elif kind == "tool_end" and not data["ok"]:
            self.err.print(Text(f"  × {visible(data['name'])} 执行失败", style="yellow"))
        elif kind == "read_reused":
            self.err.print("    已确认内容相同，沿用上次读取", style="dim")
        elif kind == "compact_start":
            self.err.print("  ↳ 正在整理上下文", style="dim")
        elif kind == "compact":
            self.err.print(f"    已保留 {data['retained_tool_results']} 条工具结果", style="dim")
        elif kind == "evolution":
            self.err.print(Text("  技能反馈 · " + visible(data.get("decision", "")), style="dim"))
        elif kind == "evolution_error":
            self.err.print(Text("  技能反馈处理失败 · " + visible(data.get("type", "")), style="yellow"))

    def detail(self, value):
        if isinstance(value, str):
            self.out.print(Text(visible(value)))
        else:
            self.out.print_json(json.dumps(value, ensure_ascii=False))
