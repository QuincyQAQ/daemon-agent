# 功能与验收入口

以下命令均在仓库根目录运行。完整回归入口为 `./scripts/check.sh`，每组测试使用独立的临时状态。

| 功能 | 运行入口或验收文件 |
| --- | --- |
| CLI、REPL、Plan/Act、会话锁、恢复和子任务 | `./bear.sh run`、[运行时集成测试](../tests/test_harness.py) |
| 预算、重复读取、摘要压缩、超时检查点 | [恢复与预算回归](../tests/test_runtime_repair.py) |
| 列文件、搜索、哈希编辑、Shell、MCP stdio | [工具与权限集成](../tests/test_harness.py)、[工具边界回归](../tests/test_tool_boundaries.py) |
| Memory、BM25、Skills 提取、版本与晋升 | `./bear.sh demo`、[回放与计数回归](../tests/test_skill_roundtrip.py) |
| 模型适配器、工具调用回写 | [SDK 协议测试](../tests/test_sdk_transport.py)、`./campus.sh campus check` |
| 安装、容器与持续集成 | [工作流](../.github/workflows/test.yml)、[Dockerfile](../Dockerfile) |

核心测试使用真实本地文件、SQLite、Shell 和 stdio 子进程。默认测试的模型决策可重复；可选 SDK 测试使用真实客户端和本地 HTTP 模拟响应。校园网关检查才会发出远端推理请求。规则评测和单个演示样本都无法估计通用任务成功率。

## 验收记录

2026-11-24，Python 3.10.12，Linux。从 Git 干净副本运行时，标准库环境执行了 78 项测试，并跳过两个可选 SDK 测试。安装模型依赖后，全部 80 项通过；测试使用 OpenAI SDK 2.54.0 和 Anthropic SDK 0.125.0。

从发布文件构建 wheel 后，将其安装到独立虚拟环境，并在空目录运行 `bear demo`。文件复读、会话恢复、子会话 ID 和选定技能版本均与预期一致。另一次校园端点实测中，模型调用了 `read_file`，准确回传临时文件中的随机文本。Anthropic 的测试覆盖实际 SDK 序列化和工具结果回写，远端 Anthropic 推理尚待配置凭据后验证。

Compose 配置通过解析；本机缺少 Docker daemon 访问权限。容器构建、运行和挂载验证由仓库的 GitHub Actions 工作流执行，运行结果以对应提交的 Actions 页面为准。
