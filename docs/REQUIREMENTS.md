# 功能与验收入口

以下命令均在仓库根目录运行。完整回归入口为 `./scripts/check.sh`，每组测试使用独立的临时状态。

| 功能 | 运行入口或验收文件 |
| --- | --- |
| CLI、REPL、Plan/Act、会话锁、恢复和子任务 | `./daemon-agent.sh run`、[运行时集成测试](../tests/test_harness.py) |
| 预算、重复读取、摘要压缩、超时检查点 | [恢复与预算回归](../tests/test_runtime_repair.py) |
| 列文件、搜索、哈希编辑、Shell、MCP stdio | [工具与权限集成](../tests/test_harness.py)、[工具边界回归](../tests/test_tool_boundaries.py) |
| Memory、BM25、Skills 提取、版本与晋升 | `./daemon-agent.sh demo`、[回放与计数回归](../tests/test_skill_roundtrip.py) |
| 模型适配器、工具调用回写 | [SDK 协议测试](../tests/test_sdk_transport.py)、`./campus.sh campus check` |
| 两组科研实验晨报、缺失评测与证据哈希 | `./daemon-agent.sh morning-report examples/overnight --json`、[晨报核验测试](../tests/test_morning.py) |
| Slurm 单次授权、提交中断恢复、终态晨报 | `overnight start/status/watch`、[作业恢复回归](../tests/test_overnight.py) |
| 安装、容器与持续集成 | [工作流](../.github/workflows/test.yml)、[Dockerfile](../Dockerfile) |

核心测试使用真实本地文件、SQLite、Shell 和 stdio 子进程。默认测试的模型决策可重复；可选 SDK 测试使用真实客户端和本地 HTTP 模拟响应。校园网关检查才会发出远端推理请求。规则评测和单个演示样本都无法估计通用任务成功率。

对可能重新运行的同一组配置，在每次运行的 `job.json` 和 `eval.json` 中记录相同的 `run_id`（非空字符串或整数）。一方缺失或两者不同会使这一项从配对中排除。两文件都沿用无 `run_id` 的旧格式时，按 group、seed、scene 匹配。JSON 晨报记录计划文件和已解析产物的 SHA-256，可据此核对输入版本。

## 复现验证

在安装了 `.[models,ui]` 的环境运行 `./scripts/check.sh` 可覆盖全部测试。仅使用标准库也可以测试核心，缺少可选依赖时对应的测试会标记为跳过。[升级兼容测试](../tests/test_upgrade.py) 检查旧数据库、会话、环境变量优先级以及状态目录的权限保护。

[GitHub Actions](https://github.com/QuincyQAQ/daemon-agent/actions/workflows/test.yml) 在干净环境中测试三个 Python 版本、安装后的 CLI、两个 SDK，以及 Docker 的 core、live 和 Compose 入口。点击对应提交的运行记录可查看各项日志。
