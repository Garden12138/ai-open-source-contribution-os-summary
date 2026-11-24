# M2 · Docker Sandboxes Spike

2026-11-23，macOS arm64，sbx v0.54.0，Codex CLI 0.171.0，Codex ACP 2.8.0。测试使用 [Garden12138 的测试 Issue #1](https://github.com/Garden12138/contribos-acceptance-sandbox/issues/1)。

| 项 | 结果 |
| --- | --- |
| 1. 无挂载与命令 | 非交互创建命名沙箱。宿主和运行时挂载均为空。在 VM 内完成部分克隆、Git 状态检查与 PyPI 依赖安装。 |
| 2. ACP stdio | 用 `sbx exec -i` 接现有 ACP 客户端和官方 ACP kit，建立会话、修改词数统计并通过 3 个单测。长命令开始后，ACP 取消在 0.01 秒内返回。 |
| 3. 模型凭据 | 环境、认证文件和配置中只有代理占位值。扫描运行后的 Codex 目录，唯一形似 JWT 的匹配为负载均衡响应 Cookie `__oailb`，其声明无账号身份或授权范围。未发现模型 OAuth 访问或刷新令牌。 |
| 4. 出网与宿主 | 全局默认拒绝，沙箱单独放行包仓库。PyPI 可用，未列入策略的域名被拒绝。VM 无法命中宿主回环、宿主别名和 LAN 上的金丝雀。 |
| 5. 导出与恢复 | 在 VM 内生成 Git bundle，经 `sbx cp` 导出。停止后用 `sbx exec` 唤醒，工作区保留，恢复原 ACP 会话并完成新一轮。 |
| 6. 其他 Agent | Cursor 的官方无挂载 kit 提供 ACP，登录与推理待验收。Antigravity 需自建 kit。见 [接入矩阵](agent-matrix.md)。 |
| 7. xcli | 用独立金丝雀验证，按沙箱仅放行 `localhost:<port>` 后可通过 `host.docker.internal:<port>` 到达，移除规则后重新阻断。M2 未开放 xcli。 |
| 8. 上游代理 | 系统 HTTP/HTTPS 代理为 `127.0.0.1:6738`，sbx 使用 `system`。宿主 daemon 拉取 kit，VM 内 GitHub 克隆、PyPI 安装和 ChatGPT OAuth 推理均成功。Codex 未报地区错误。GitHub REST 匿名请求另有限流。 |

关键项 1–5 通过，采用 Docker Sandboxes。当前 Codex kit 提供 GitHub 格式的占位值。后端须清理 Agent 环境中的 GitHub 变量，并阻断能通过 sbx 注入 GitHub 凭据的配置。共享技能显式关闭，静态 MCP 列表为空。

- [Docker 命令参考](https://docs.docker.com/reference/cli/sbx/) · [ACP kit](https://hub.docker.com/r/sbx/codex-acp) · [凭据与占位值](https://docs.docker.com/learn/content/working-with-sandbox-secrets/)
- [本地策略](https://docs.docker.com/ai/governance/local/network-rules/) · [访问宿主服务](https://docs.docker.com/ai/sandboxes/workflows/development/#accessing-host-services-from-a-sandbox) · [系统代理](https://docs.docker.com/ai/sandboxes/configuration/upstream-proxy/)
