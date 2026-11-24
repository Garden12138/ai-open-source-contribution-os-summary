# Agent 接入矩阵

M1 验收使用 [Garden12138 的测试 Issue #1](https://github.com/Garden12138/contribos-acceptance-sandbox/issues/1)。每家 Agent 使用独立任务和克隆。
“1–8”对应 `03-architecture.md` 第 3.4.8 节。第 6 项在 M1 验证本机直跑，真实沙箱后端留到 M2。第 8 项实际使用空配置验证未登录，限流、网络和额度分类由离线测试覆盖。

| Agent | 隔离 | 版本 | 检查 1–8 | 结果与待处理原因 |
| --- | --- | --- | --- | --- |
| Codex | host | CLI 0.171.0 / ACP 2.8.0 | 1–8 通过 | 集成测试 1 passed。手动实现后 6 项 `unittest` 通过，取消在 10 秒内返回，终止后恢复了原会话，拒绝权限后命令未执行。个人 MCP 6 项，实际 ACP 会话 0 项。个人插件缓存 21 个名称，专用配置禁用插件后已安装列表为空。初次取消时底层长命令仍自行跑完。 |
| Cursor | host | 2026.11.19-f005e9d | 1–8 通过 | 专用文件存储登录成功，集成测试 1 passed。手动修复后 4 项 `unittest` 通过，支持取消、继续、diff，终止进程后恢复原会话。终端 y/n 均验证。个人 MCP 4 项、插件 11 项，专用目录无同名条目，实际 Agent 环境的 MCP 清单为空。空配置认证错误不可重试。 |
| Antigravity | host | 1.5.12 | 1–6、8 通过，7 部分 | 专用目录登录和上下文读取通过。手动确认编辑后 5 项 `unittest` 通过，取消、继续、diff、权限 y/n 和原会话恢复均通过。初测出现 API 地区错误，复跑的非交互测试默认拒绝权限请求，停在无 diff 断言。个人与专用 MCP、插件清单均为空，缺少非空的隔离对照。 |

默认数据目录下的登录命令（使用 `CONTRIBOS_HOME` 时，以 `contribos agents` 打印的路径为准）：

```bash
npm i -g @agentclientprotocol/codex-acp
CODEX_HOME="$HOME/.contribos/agent-homes/codex" codex login
cursor_dir="$HOME/.contribos/agent-homes/cursor"
env HOME="$cursor_dir" USERPROFILE="$cursor_dir" XDG_CONFIG_HOME="$cursor_dir/.config" CURSOR_CONFIG_DIR="$cursor_dir" AGENT_CLI_CREDENTIAL_STORE=file NO_OPEN_BROWSER=1 cursor-agent login
agy_dir="$HOME/.contribos/agent-homes/antigravity"
env HOME="$agy_dir" USERPROFILE="$agy_dir" XDG_CONFIG_HOME="$agy_dir/.config" agy acp --app-data-dir contribos-antigravity --gemini-dir "$agy_dir/gemini" --login
contribos agents
contribos chat --agent codex https://github.com/OWNER/REPO/issues/NUMBER
```

CLI 新任务默认使用沙箱。本机验收时加 `--isolation host`，按提示为该任务确认仓库风险。之后可用 `/continue`、`/diff`，退出后可用 `contribos chat --agent codex --resume TASK_ID`。
可选真实测试需显式设置 `CONTRIBOS_IT_AGENTS=codex`、`CONTRIBOS_IT_ISSUE_URL=<自有Issue>`、`CONTRIBOS_IT_TRUST_REPO=1`，再运行 `pytest -q -m agents`；不会自动批准宿主权限升级，工作区保留供检查。MCP/插件隔离仍须人工核实。

| Agent | 沙箱可行性 | 凭据位置 |
| --- | --- | --- |
| Codex | 官方 kit 与 ACP mixin 通过 M2 编辑、测试、取消和恢复实测。 | 订阅 OAuth 存宿主钥匙串，sbx 代理注入，VM 中为占位值。 |
| Cursor | v2026.11.22-e9042b2 的官方无挂载 kit 提供 ACP 命令。M7 仍待用户在 VM 内完成 OAuth 登录后验证推理、取消与恢复，当前仅开放本机直跑。 | OAuth 登录由沙箱触发，Docker 文档说明令牌交换由宿主代理接管。本次 VM 未登录。 [Docker Cursor 文档](https://docs.docker.com/ai/sandboxes/agents/cursor/) |
| Antigravity | 已用官方 Linux 安装器构建 v3 mixin，在无挂载的 shell VM 内运行了 `agy acp --help`。真实 ACP 返回 `authentication_required`，推理、取消与恢复待验。当前仅开放本机直跑。 | 临时 VM 使用独立配置，未导入宿主账号；登录后的凭据位置待实测。 [Google ACP 文档](https://antigravity.google/docs/cli/acp/) |
