# Bear Harness

一个可在本地运行的 Python Agent。模型决定下一步调用哪个工具，运行时负责参数检查、操作审批、预算和持久化。每次读取、编辑与重试都有记录可供检查。核心仅依赖 Python 3.10+ 标准库，支持 Linux 和 macOS。

![Bear Harness 在 Plan 模式下显示模型和快捷命令](docs/terminal-preview.png)

## 三分钟演示

在仓库根目录运行：

```bash
./bear.sh demo
cat .bear/demo/latest-report.json
./scripts/check.sh
```

演示会在独立目录中真实创建一个文件，读取 SHA-256，按哈希编辑并复读。报告中的 `file_content` 应为 `"Verified Harness\n"`，`resumed` 应为 `true`。同一份报告还包含从反馈生成的技能、评测与选定版本，以及独立探索子会话的 ID。演示的模型决策由确定性脚本提供，面试现场无需账号、网络或密钥。

## 三个值得跟踪的执行路径

| 问题 | 从哪里开始读 | 可以验证什么 |
| --- | --- | --- |
| 多个智能体如何共享有限步数？ | [主循环与委派](agents/agent.py) | 父任务派发工具、子任务执行、父任务总结都消耗预算。Plan 模式和递归深度限制沿委派生效。 |
| 工具已执行，模型超时怎么办？ | [会话检查点](agents/storage.py) · [上下文折叠](agents/context.py) | 工具结果逐个落盘；重试沿用这些结果。若进程恰好在执行和保存之间中断，会话将结果标记为未知，留待检查。 |
| 用户反馈怎样变成可复用的操作步骤？ | [技能生命周期](agents/skills.py) · [检索](agents/retrieval.py) | 上一轮任务、回答和反馈生成候选技能。技能有版本与来源，有标签的回放样本可以用于规则或模型评分。 |

[架构图与设计取舍](docs/ARCHITECTURE.md) · [功能与测试对照](docs/REQUIREMENTS.md) · [权限与故障排查](docs/OPERATIONS.md)

## 使用模型和工具

从源码运行时使用 `./bear.sh`。要安装 `bear` 命令和模型适配器，在虚拟环境中运行：

```bash
python -m pip install '.[models,ui]'
bear doctor
```

OpenAI 或 Chat Completions 兼容端点使用 `BEAR_PROVIDER=openai`、`BEAR_MODEL`、`OPENAI_API_KEY`。自定义地址由 `OPENAI_BASE_URL` 指定。Anthropic Messages 使用 `BEAR_PROVIDER=anthropic`、`BEAR_MODEL`、`ANTHROPIC_API_KEY`，可选 `ANTHROPIC_BASE_URL`。环境设置见 [.env.example](.env.example)。学校网关有单独的[接入指南](docs/CAMPUS_API.md)。

```bash
./bear.sh run --mode plan "从 README 和入口代码说明程序如何启动"
./bear.sh run --mode act
./bear.sh sessions
./bear.sh run --resume SESSION_ID --retry
```

安装 `ui` 后，交互终端会显示模型、当前模式和工具进度，并排版 Markdown 回复。可用 `./campus.sh run --plain` 切回文本界面；管道和单次任务沿用文本输出。

Plan 允许只读检查。Act 在每次写入、运行命令或调用外部 MCP 工具前请求批准。REPL 支持 `/plan`、`/act`、`/compact`、`/retry`、`/memory`、`/skills` 和 `/help`。使用 `--workspace`、`--state` 时将它们放在子命令前。会话、工具输出和记忆保存在本地 `.bear/`；使用远端模型时，任务及相关上下文会发送到所配置的服务。

## 记忆、技能和 MCP

```bash
./bear.sh memory add --kind preference '项目文档使用中文'
./bear.sh memory search '文档'
./bear.sh skills add --title '可靠文件编辑' --file examples/editing-skill.md
./bear.sh eval SKILL_ID --cases examples/replay-cases.json
./bear.sh skills promote EVALUATION_ID
./bear.sh run --mcp-config examples/mcp.json
```

用 `skills replays SKILL_ID > cases.json` 导出反馈样本，填写其中的 `expected_terms` 后可传给 `eval --cases cases.json`。晋升要求评测对应当前已保存版本，分数至少为 0.8。该分数衡量工作流对样本的覆盖。MCP 示例通过 stdio 子进程提供词数统计；交互时需分别批准服务启动和工具调用。

## 容器和自动化验证

```bash
docker build --target core -t bear-harness:local .
docker run --rm bear-harness:local demo
```

[GitHub Actions](.github/workflows/test.yml) 定义了 Python 版本矩阵、安装包冒烟测试、模型 SDK 协议测试和 Docker 构建运行检查。Compose 使用非特权用户、只读根文件系统和可写工作区。Shell 和 MCP 运行在当前操作系统用户的权限下，部署不受信任的任务时应使用专用用户或容器。
