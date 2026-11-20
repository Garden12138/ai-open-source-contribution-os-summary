# ContribOS v2 重新设计方案

**状态：提案，已确认两项关键取舍（经 ACP 接入多个 Agent、分级隔离）。** 用户最终确认前不改动现有代码和根目录 `AGENTS.md`。

## 一句话结论

现有系统失败的根源是**自研了一个被严格限额的 Coding Agent，再在外面套了一层全链路哈希授权**：
同一个 Issue 连续 7 次真实执行（Plan10–Plan16），补丁数为 0，任何小故障都变成"终态 + 重新审批"。
v2 改为**调度现成的编码 Agent**：通过 ACP（Agent Client Protocol）接入本机已有的 Codex、Cursor、Antigravity，
后续再接 OpenCode、OpenHands 等开源 Agent（可用 xcli、DeepSeek 等模型）。陌生仓库在沙箱中执行（优先 Docker Sandboxes），
可信仓库可以本机直跑。产品形态是桌面应用（Tauri 外壳 + Python 后端 + 原生 HTML/JS 界面），
常驻菜单栏，关窗任务不中断，需要你处理时发系统通知。
平台只负责发现、任务与对话、隔离、事件流、跨 Agent 审查和唯一的"确认发布 Draft PR"。
保留发现与评分模块，其余重建，规模目标约为现在的十分之一。

## 新旧对比

| 方面 | 现状 | v2 |
| --- | --- | --- |
| 编码 Agent | 自研循环：单次请求 32 KB、输出 4,096 token、每轮 ≤ 4 个工具 | 经 ACP 调度 Codex / Cursor / Antigravity 等现成 Agent，平台不自研 |
| 执行隔离 | 自研 Sandbox Worker + 断网验证 | 分级：陌生仓库在沙箱（Docker Sandboxes microVM，备选自管容器）；可信仓库可本机直跑 |
| 人工确认 | 方案批准、执行授权、精确预览、各类扩展确认，任何变化都让授权失效 | 只有发布（及发布后的写操作）需要按钮确认；本机直跑非可信仓库需确认风险 |
| 失败处理 | 终态，需新 Plan + 新授权 | 自动重试可恢复错误；任何时候都可以"继续" |
| 独立审查 | 另一个模型调用，由平台自研流程驱动 | 默认交给另一家 Agent（如 Codex 实现、Cursor 审查） |
| 产品形态 | 浏览器访问的本地服务 | 桌面应用 ContribOS.app（Tauri 外壳）：常驻菜单栏、系统通知、关窗任务不中断 |
| 运行形态 | 6 个服务、多组管理密钥 | 桌面外壳 + 1 个后端进程 + 每任务 1 个 Agent 进程（本机或沙箱内） |
| 代码规模 | Python 7.8 万行、文档 3.5 万行 | 非测试 Python ≤ 9,000 行、前端 ≤ 3,500 行 |

## 文档

| 文件 | 内容 |
| --- | --- |
| [01-diagnosis.md](01-diagnosis.md) | 需求重述、现状数据、六个根因、保留/丢弃清单 |
| [02-product-spec.md](02-product-spec.md) | 用户旅程、功能优先级、对话交互规范、确认点、验收标准 |
| [03-architecture.md](03-architecture.md) | ACP 接入与 Agent 配置、分级隔离与沙箱后端、发布、审查、桌面外壳、数据模型、API、安全、可靠性、ADR |
| [04-implementation-plan.md](04-implementation-plan.md) | M0–M8 里程碑、测试策略、风险、**进度清单（唯一路线图）** |
| [05-codex-prompts.md](05-codex-prompts.md) | 每个里程碑可直接粘贴给 Codex 的提示词 |
| [AGENTS.proposed.md](AGENTS.proposed.md) | 新的 Agent 指南，确认后替换根目录 `AGENTS.md` |

实施过程中会新增三份短文档：`agent-matrix.md`（每个 Agent × 隔离级别的验收结果）、
`spike-sandbox.md`（沙箱后端验证结论）、`acceptance-mvp.md`（真实验收记录）。

## 如何启用

1. 阅读 01 与 02，确认需求理解和取舍；重点看 03 第 11 节"与现行 AGENTS.md 的冲突"。
2. 确认后，在 Codex 中粘贴 05 的"通用前缀 + M0"提示词，并在对话中明确授权：
   建分支 `redesign/v2`、打标签 `legacy-v0.1-final`、替换 `AGENTS.md`、删除旧代码。
3. 之后按 M1（本机直跑跑通三个 Agent）→ M2（沙箱）→ M3–M5 推进核心链路；M6 发现模块可并行。
   M7 完成前，陌生仓库只在沙箱里做。

## 需要你提前准备的

- 各 Agent 登录：Codex（ChatGPT 订阅）、Cursor（`cursor-agent login`）、Antigravity（`agy` 登录）；
  Codex 经 ACP 接入需要安装适配器 `npm i -g @agentclientprotocol/codex-acp`。
- 沙箱（M2）：`brew install docker/tap/sbx`、`sbx login`（需要 Docker 账号）、`sbx secret set openai --oauth`；
  要求 macOS 14 以上、Apple 芯片。若验证不通过，备选方案使用现有 OrbStack。
- 桌面应用（M4）：Rust 工具链（`rustup`）和 `cargo install tauri-cli`。
- 发布（M5）：`gh auth login`，以及一个用于首次发布验收的测试仓库。
- 一个你自己的小仓库或 Issue，用于 M1 的本机直跑验收。
