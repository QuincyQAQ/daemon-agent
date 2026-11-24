# 学校网关接入

HKUST(GZ) AIGC 网关使用 OpenAI 兼容协议。有效模型由当前账号在 `/api/v2/models` 的返回决定。[学校 API 使用说明](https://docs.hpc.hkust-gz.edu.cn/docs/aigc/api-usage/)

在源码目录安装模型依赖后，选择一种凭据来源：

```bash
# 使用已导出的 OPENAI_API_KEY
./daemon-agent.sh campus setup

# 或在隐藏输入提示中输入密钥
./daemon-agent.sh campus setup --key-prompt

# 或引用已有的 Claude Code Router 学校配置
./daemon-agent.sh campus setup --from-ccr
```

配置保存到本机 `.foldjay/`。选择环境变量或 CCR 时只记录来源；隐藏输入方式将密钥保存为权限 600 的独立文件。连接只允许配置的学校 HTTPS 端点，采用 TLS 校验。

```bash
./campus.sh doctor
./campus.sh campus models
./campus.sh campus select MODEL_ID
./campus.sh campus check
./campus.sh run --mode plan
```

`doctor` 展示 SDK 和凭据状态。`campus check` 在临时工作区生成随机文本，请求真实模型通过工具读取并回传，产生一次有计费可能的验证流程。验证结果写入配置旁的 `campus-check.json`。

校园配置的单次请求超时为 90 秒，关闭 SDK 自动重试。超时后在当前窗口输入 `/retry`，或使用 `./campus.sh run --resume SESSION_ID --retry`。DeepSeek 系列可通过 `FOLDJAY_THINKING=enabled` 启用思考扩展。
