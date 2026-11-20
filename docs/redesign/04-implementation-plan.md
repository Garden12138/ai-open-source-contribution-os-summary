# 04 · 实施计划（交给 Codex 执行）

本文件是 v2 的**唯一路线图和进度来源**（第 6 节清单）。架构细节以 `03-architecture.md` 为准，
产品行为以 `02-product-spec.md` 为准。

## 1. 交接方式

1. 用户确认本方案后：创建分支 `redesign/v2`，给当前 HEAD 打标签 `legacy-v0.1-final`，
   用 `docs/redesign/AGENTS.proposed.md` 替换根目录 `AGENTS.md`（M0 第一步）。
2. 每个里程碑开一个或多个 Codex 会话，直接粘贴 `05-codex-prompts.md` 中对应的提示词。
3. 每个会话只做清单里**下一个未勾选的条目**（或一组紧密相关的条目），结束时：
   测试通过 → 勾选条目并在条目后用一行写明验证方式 → 按用户授权提交。
4. 需要用户参与的事项（登录各 Agent、安装 `sbx`、真实发布）在清单里标 `【需用户】`，
   Codex 遇到时写明需要什么，然后继续做其他不依赖它的条目，不要停下等待。

### Codex 会话规则（同样写入新 AGENTS.md）

- 不要阅读旧代码，除非清单明确列出要移植的文件；读取方式：`git show legacy-v0.1-final:<path>`。
- 不要写验收归档、证据索引、哈希清单之类的文档；每个里程碑最多更新本清单和一份相关说明。
- 不要引入：方案版本/锁定/审批对象、多层预算、自研 Agent 循环、断网验证、为特定项目写的适配、
  为单个 Agent 写的平行业务逻辑（差异只放在 `agent/profiles.py`）。
- 完成后只需重启 `contribos serve` 验证，不需要 Docker Compose 重部署流程。

## 2. 里程碑总览

| 里程碑 | 目标 | 可演示结果 | 依赖 |
| --- | --- | --- | --- |
| M0 | 清场与新骨架 | `contribos serve` 启动、`/api/health`、`contribos doctor` | 用户确认方案 |
| M1 | ACP 接入 + 本机直跑（终端可用） | `contribos chat --agent codex <issue_url>` 在本机独立工作区里和 Agent 对话改代码；Cursor、Antigravity 同样可用 | M0；【需用户】各 Agent 登录 |
| M2 | 沙箱后端（Spike + 实现） | `contribos chat --isolation sbx …` 在沙箱里完成同样流程 | M1；【需用户】安装并登录 `sbx` |
| M3 | 任务运行时 + 对话 API | 用 HTTP 驱动完整对话；重启服务后能继续 | M1 |
| M4 | 桌面应用：界面 + 外壳 | 从 Finder 打开 ContribOS.app，完成"粘贴链接 → 选 Agent 和隔离级别 → 对话 → 看改动"；关窗后任务继续，完成时收到系统通知 | M3；【需用户】安装 Rust 工具链 |
| M5 | 发布 Draft PR + 跨 Agent 审查 | 在应用里确认后，测试仓库出现 Draft PR | M4；【需用户】gh 登录、测试仓库 |
| M6 | 发现模块移植 + 首页 | 今日推荐、筛选、一键开始贡献 | M0（可与 M1–M5 并行） |
| M7 | 安全加固 + 真实验收 | 隔离与凭据探针通过；3 个真实 Issue 端到端 | M2、M5、M6 |
| M8 | v1 功能 | PR 跟踪、贡献记录、追加提交、开源 Agent + xcli、换 Agent 继续、旧数据导入、Linux 验证 | M7 |

M2 可以和 M3 并行。M7 完成前，陌生仓库只能在沙箱里做；本机直跑只用于你自己的仓库。

## 3. 各里程碑说明

### M0 清场与新骨架

范围：
- 打标签、建分支、替换 `AGENTS.md`（用户已授权的前提下）。
- 删除主线上的旧内容：`app/`、`tests/`、`scripts/`、`containers/`、`docs/` 下除 `redesign/` 外的全部内容。
  未被 Git 跟踪的本地数据（`data/`、`.local-demo/`、`.env`）保持不动。新建 `docs/legacy.md`（≤ 20 行）
  说明旧版在标签 `legacy-v0.1-final` 中、如何查看。
- 重写 `README.md`（≤ 80 行）：一句话定位、快速开始、架构图链接、文档链接。
- 新骨架：`pyproject.toml`（包名 `contribos`，依赖 `fastapi`、`uvicorn`、`httpx`，开发依赖 `pytest`），
  `contribos/{__init__,__main__,cli,config,db,security}.py`、`contribos/migrations/0001_init.sql`
  （包含 03 第 5 节全部表）、`contribos/web/app.py`（`/api/health` 免鉴权、静态文件、回环检查、
  会话鉴权：一次性启动码 `/desktop/bootstrap` 换取 HttpOnly Cookie，`contribos serve` 启动时在终端打印登录链接；
  CLI 本地令牌；变更请求的 Origin/CSRF 中间件）、`contribos/web/static/index.html` 占位页。
- `contribos doctor`：Python 版本、`git`、`gh` 登录状态（各 Agent 与沙箱检查在 M1/M2 补充）。

验收：`pytest` 通过（迁移从空库建表且可重复执行、脱敏、无会话的 `/api` 请求被拒绝、启动码只能用一次且会过期、
Origin/CSRF、非回环启动被拒绝）；`contribos serve` 在 `127.0.0.1:8765` 启动；`curl /api/health` 返回 ok；
`contribos doctor` 输出各项结果。

### M1 ACP 接入 + 本机直跑（终端可用）

范围（03 第 3.4、3.5.1、3.5.3 节）：
- 依赖加入 `agent-client-protocol`。
- `agent/engine.py`：`AgentEngine` 协议、`AgentCapabilities`、规范化事件类型。
- `agent/acp_engine.py`：基于 SDK 的通用 ACP 客户端（不声明 fs/terminal 能力；会话新建/恢复；一轮；取消；
  权限与提问请求转为事件并等待应答）+ 03 第 3.4.5 节事件规范化。
- `agent/profiles.py`：`codex`（`codex-acp`）、`cursor`（`cursor-agent acp`）、`antigravity`（`agy acp`）三条配置，
  含检测/版本/登录检查/登录指引/配置隔离/可重试错误模式；`contribos agents` 列出状态；doctor 接入。
- `agent/fake.py`：`FakeEngine`；`tests/fake_acp_agent.py`：用 SDK 写的脚本化假 ACP Agent（读、改、执行、
  权限请求、错误、慢轮、进程退出），供测试使用。
- `sandbox/base.py`（`Sandbox` 接口）+ `sandbox/host.py`（独立克隆到 `~/.contribos/workspaces/<task>/repo`、
  上下文目录、环境清理、Agent 自带沙箱参数、平台 git 命令）。
- `tasks/instructions.py`：与 Agent 无关的指令模板（03 第 3.4.4 节）。
- `contribos chat [--agent codex|cursor|antigravity] <issue_url>`：终端调试入口（建任务行、准备本机工作区、
  启动 Agent、发首轮指令；逐行读输入作为消息；`Ctrl-C` 取消当前轮；`/continue`；`/diff`；权限请求在终端里问 y/n）。

验收：
- 单元/组件（用假 ACP Agent 子进程，离线）：初始化与能力读取、会话新建与恢复、事件规范化、取消、
  权限请求往返、Agent 进程退出检测、环境清理（`GITHUB_TOKEN`/`GH_TOKEN`/`SSH_AUTH_SOCK` 不在子进程环境中）。
- 【需用户】按 03 第 3.4.8 节对三个 Agent 做本机直跑验收（在你自己的一个小仓库或 Issue 上），
  结果写入 `docs/redesign/agent-matrix.md`。某个 Agent 不达标就记录原因，不阻塞其他条目。

### M2 沙箱后端（Spike + 实现）

范围（03 第 3.5.4–3.5.6 节）：
- 【需用户】安装 `sbx`（`brew install docker/tap/sbx`）、`sbx login`、为 Codex 设置订阅 OAuth（`sbx secret set openai --oauth`）。
- 按 03 第 3.5.6 节逐项做 Spike，结论写入 `docs/redesign/spike-sandbox.md`，并在 `agent-matrix.md` 补充沙箱列。
- 通过 → 实现 `sandbox/sbx.py`（创建无挂载沙箱、clone、出网策略、在沙箱内以 stdio 启动 ACP 命令、导出文件、停止/恢复/销毁）。
- 不通过 → 实现 `sandbox/container.py` + `relay/server.py` + 出网代理（03 第 3.5.5 节），只支持 API Key 型 Agent。
- `contribos chat --isolation sbx|container …`。

验收：后端无关的 `Sandbox` 契约测试（用假后端）；可选集成测试（`CONTRIBOS_IT_SBX=1` 或 `CONTRIBOS_IT_DOCKER=1`）：
准备、在沙箱内跑 Codex 一轮并产生 diff、导出 bundle、停止后恢复；探针：沙箱内找不到真实模型令牌、访问不了宿主回环服务。

### M3 任务运行时 + 对话 API

范围：`tasks/{store,events,supervisor}.py`、`web/routes_tasks.py`、`GET /api/agents`
（03 第 3.3、4、5、6、9 节）。

验收（全部基于 FakeEngine 或假 ACP Agent，离线）：
- 消息开新轮；运行中消息默认排队、`interrupt` 方式先取消再发；停止；继续。
- 强制取消：假 Agent 模拟"不响应取消"和"取消后遗留子进程"两种情况，10 秒内轮结束、子进程全部停止，会话随后可继续。
- 地区不可用类错误不重试，并给出检查网络出口的提示。
- `client_message_id` 幂等；并发两条消息只产生一个运行中的轮（CAS）。
- 重试：匹配可重试模式的错误最多 3 次退避重试（替换时钟），之后 `agent_state=error`；
  `max_tokens` / `max_turn_requests` 自动继续一次；认证/额度错误不重试。
- 恢复：Agent 进程退出后重启并恢复会话；不支持恢复的 Agent 新建会话并发送回顾（`system.session_recreated`）；
  服务启动时扫描 `running` 任务并恢复。
- 权限/提问请求：生成事件、经 `requests/{id}/respond` 应答；进程重启后旧请求标记失效。
- SSE：`Last-Event-ID` 补发；增量事件不带 id；客户端断开不影响后台。
- 脱敏：命令输出中的金丝雀令牌不出现在 `task_events`、SSE、API 响应中。

手动：用 curl 在真实 Issue 上驱动对话；轮进行中杀掉服务再启动，发"继续"后接着干。

### M4 桌面应用：界面 + 外壳

分两段做：先在浏览器模式下把界面做完（迭代快），再套上桌面外壳。

**界面**（03 第 3.1 节）：页面框架与导航；任务列表；新建任务面板（Issue 链接、Agent 选择及状态、隔离级别与可信确认）；
`chat.js`（SSE、消息与活动卡片、权限卡片、提问卡片、Markdown 安全渲染、排队/打断发送/停止/继续、快捷短语）；
右侧面板"改动 / Issue / 用量"；设置页（各 Agent 状态与登录指引、可信 owner、沙箱后端、默认 Agent、审查 Agent、
首轮策略、资源上限、工具路径、通知开关）；`contribos serve --demo`（FakeEngine 离线演示）。界面不调用任何桌面原生接口。

**后端桌面模式**（03 第 3.10 节）：`contribos/desktop_mode.py`：`--desktop` 启动参数、从环境变量读取启动码与外壳令牌、
就绪输出、`/api/notifications/stream`、`/api/desktop/status`、登录 shell `PATH` 解析与工具路径设置、SIGTERM 优雅退出。

**桌面外壳**（03 第 3.10 节）：`desktop/` Tauri v2 项目：启动后端（开发用 venv，发布用 sidecar）并等待就绪；
主窗口加载启动链接，**不授予任何 IPC 能力**；导航拦截（外部链接用系统浏览器打开）；单实例；菜单栏图标与菜单；
关窗隐藏；退出确认；订阅通知事件流发系统通知；开机启动开关。打包：PyInstaller sidecar + `cargo tauri build`。

验收：
- `node --test` 跑纯函数测试（Markdown XSS 用例、diff 解析、事件归并）；加载/空/错误状态齐全；
  键盘可操作（Enter 发送、Shift+Enter 换行、Esc 关闭面板）。
- `pytest`：桌面模式的启动码/外壳令牌鉴权、通知事件流、状态接口、PATH 解析（假 shell）、SIGTERM 后轮被标记 `interrupted`。
- `cargo test`：就绪行解析、导航过滤（只放行后端来源）。
- 检查 `tauri.conf.json` 和 capabilities：主窗口没有任何 IPC 权限（写一个测试读配置断言）。
- 【需用户】从 Finder 启动打包后的 `.app`：工具路径正确解析；完成 J2、J3；关闭主窗口后任务继续并收到通知，
  点击通知回到任务；有运行中任务时退出会弹确认，重新打开后"继续"可接着干。

### M5 发布 Draft PR + 跨 Agent 审查

范围：`publish/{git_transfer,github_writer,service}.py`、`routes_publish.py`、PR 文案轮（JSON 代码块解析 + 模板兜底）、
发布面板 UI；`review/service.py`（默认用审查 Agent 新开会话、只读模式、审查前后 diff 哈希、结果卡片、
"按审查意见修改"）（03 第 3.6、3.7 节）。

验收：
- `git_transfer` 用本地 bare 仓库真实测试（不联网）：基线祖先校验、父提交校验、宿主重建提交的作者信息、
  diff 哈希、密钥扫描阻止、带 hooks 的夹具仓库其 hooks 不被执行、workflow 改动标记。
- `github_writer` 用 `httpx.MockTransport`：Fork 已存在/不存在、PR 已存在/不存在、远程分支同 SHA 跳过/不同 SHA 失败、
  中途异常后对账不重复创建。
- 预览哈希不匹配、非最新、已被替代一律拒绝；令牌不出现在子进程参数、日志、事件、响应中。
- 审查：审查改动工作区时标记无效；JSON 解析失败时保留原文并提示。
- 【需用户】真实发布到你的测试仓库，生成 Draft PR；至少一次审查由另一家 Agent 完成。

### M6 发现模块移植 + 首页（可并行）

范围：从标签移植 `app/scoring.py`、`app/domain.py`、`app/github.py`（去掉 provenance 依赖）；重写
`discovery/service.py` 持久化；`routes_discovery.py`；发现视图（今日推荐、全部机会、筛选、收藏/忽略、扫描进度、
"开始贡献"）；每日定时扫描；新增"已有关联 PR"和"外部 PR 合并情况"两个信号。

验收：移植并改写旧版 `tests/test_scoring.py`、`tests/test_github.py`、`tests/test_service.py` 中仍适用的用例；
GitHub 读取全部用假数据；手动：真实扫描一次，首页显示推荐，点"开始贡献"进入任务。

### M7 安全加固 + 真实验收

范围：对照 03 第 7 节逐项补测试；沙箱出网策略（放行列表可在设置中修改）与探针（宿主回环/局域网金丝雀服务不可达、
包仓库可达、VM 内无真实凭据）；本机直跑护栏（可信列表校验、风险确认、环境清理、任务头部常驻隔离标识）；
资源与磁盘监控；doctor 汇总这些检查。

验收：
- 探针与威胁表测试通过（集成测试按环境变量开启）。
- 【需用户】产品级验收（02 第 7.1 节）：3 个真实未解决 Issue（≥1 Python、≥1 TypeScript；≥1 在沙箱中；
  ≥2 家 Agent 参与），只通过桌面应用完成到 Draft PR，每个任务至少一次人为中断后成功继续，
  且至少一次关闭主窗口后通过系统通知回到任务。
  结果写入 `docs/redesign/acceptance-mvp.md`（每个任务 ≤ 15 行：Issue、Agent、隔离级别、轮数、工具调用数、
  用量（如有）、耗时、重试/中断次数、PR 链接、问题）。

### M8 v1 功能

PR Tracker 与贡献记录页；处理 review 意见并追加提交；更新 PR 文案、标记 Ready for review（各自预览 + 确认）；
OpenCode、OpenHands profile 及通过它们使用 xcli / DeepSeek（本机直跑与沙箱两种接入方式）；
任务中途换 Agent 继续；可选 `CodexAppServerEngine`（若 M7 验收证明 ACP 缺口影响体验）；
`contribos import-legacy`；Linux amd64 上跑集成测试与探针，并生成和验证 Linux 桌面包（AppImage / deb）。

## 4. 测试策略

| 层次 | 内容 | 默认运行 |
| --- | --- | --- |
| 单元 | 评分、事件规范化、脱敏、环境清理、预览哈希、状态 CAS | 是 |
| 组件 | ACP 客户端 + 假 ACP Agent 子进程、Supervisor + FakeEngine、Sandbox 契约（假后端）、git_transfer + 本地 bare 仓库、github_writer + MockTransport | 是 |
| 前端纯函数 | `node --test` | 是 |
| 桌面外壳 | `cargo test`（就绪解析、导航过滤）+ 配置断言（主窗口无 IPC 权限） | 是（需 Rust 工具链） |
| 集成（可选） | `CONTRIBOS_IT_AGENTS=codex,cursor,antigravity`（真实 Agent，本机直跑）、`CONTRIBOS_IT_SBX=1`、`CONTRIBOS_IT_DOCKER=1`、`CONTRIBOS_IT_GITHUB=1`（测试仓库） | 否 |
| 产品级 | 02 第 7.1 节 | M7 手动 |

默认测试套件不联网、不需要任何凭据、不需要 Agent CLI 或沙箱，目标在 60 秒内跑完。

## 5. 风险与预案

| 风险 | 预案 |
| --- | --- |
| 各家 Agent 的 ACP 实现差异大（恢复、权限、模式、错误） | 差异集中在 profile；每个 Agent 按 03 第 3.4.8 节验收；不达标的 Agent 暂不在 UI 中提供 |
| ACP 缺少插话、结构化错误、用量上报 | 排队/打断发送替代插话；按消息模式识别可重试错误；Codex 可在 M8 启用原生适配器 |
| ACP v2 变化 | 通过官方 SDK 接入，固定版本；不使用 v2 将移除的客户端 fs/terminal 能力 |
| Docker Sandboxes 不能被程序驱动或有缺陷 | M2 Spike 决定；不行则用自管容器（只支持 API Key 型 Agent） |
| Cursor / Antigravity 的登录令牌只能放在沙箱内 | UI 标注"凭据在沙箱内"；出网默认拒绝限制外传；用户可选只在本机直跑中使用它们 |
| 本机直跑的残余风险 | 只对可信仓库开放；逐任务风险确认；环境清理；任务头部常驻隔离标识 |
| 订阅额度与各家条款 | 只使用各家官方 CLI 的非交互/ACP 入口；用量和限流错误如实展示 |
| 大仓库克隆慢、占磁盘 | `--filter=blob:none`；磁盘监控；任务删除时清理 |
| 从 Finder 启动的应用找不到命令行工具 | 登录 shell 解析 `PATH` + 设置中可填绝对路径；doctor 显示解析结果 |
| PyInstaller 打包后端出问题（启动慢、漏打依赖） | 先单文件，慢再改目录形式；打包后跑一遍 doctor 与健康检查作为冒烟测试 |
| 未签名应用的系统通知或首次打开受限 | 个人使用按系统提示手动允许；签名与公证列为后续 |
| WebView 与 Chrome 行为差异 | 界面只用标准 DOM/EventSource；手动在桌面应用和浏览器模式各走一遍 |
| 代码再次膨胀 | 规模预算（03 P7）；每个里程碑结束时统计行数，超出需在清单里说明理由 |

## 6. 进度清单

> 勾选规则：实现完成且有对应的自动测试或明确的手动验证后才勾选；在条目末尾用一行写验证方式。

### M0 清场与新骨架
- [x] M0-1 【需用户】授权：建分支 `redesign/v2`、打标签 `legacy-v0.1-final`、替换 `AGENTS.md`
  验证：Python 3.12 `pytest -q`（临时 Git/文件断言）1 passed；分支与旧版标签均指向原 HEAD，新指南已替换。
- [x] M0-2 删除旧内容、新建 `docs/legacy.md`、重写 `README.md`
  验证：Python 3.12 `pytest -q`（临时 Git/文件断言）2 passed；确认旧目录已移除、redesign 保留、README/legacy 行数合规。
- [x] M0-3 `pyproject.toml` 与 `contribos/` 包骨架、CLI 入口
  验证：Python 3.12 可编辑安装成功；`python -m pytest -q` 3 passed，覆盖 CLI 帮助、模块入口与独立数据目录。
- [x] M0-4 `db.py` 迁移执行器 + `0001_init.sql` + 迁移测试
  验证：`python -m pytest -q` 15 passed；空库建表、重复迁移、顺序升级、失败事务回滚与约束均有测试。
- [x] M0-5 `security.py`（脱敏、启动码与会话 Cookie、Origin/CSRF、本地令牌）+ 测试
  验证：`python -m pytest -q` 50 passed；覆盖令牌/头/URL 脱敏、60 秒过期与并发单次兑换、会话/CSRF 绑定、本地令牌 0600 与符号链接拒绝。
- [x] M0-6 `web/app.py`（health、静态文件、回环检查、会话鉴权中间件、终端打印登录链接）+ 测试
  验证：`python -m pytest -q` 94 passed；`node --test contribos/web/static/tests` 2 passed；真实 serve/curl、登录与重放拒绝、SIGTERM 关闭通过。8765 被既有进程占用，实测使用空闲回环端口；默认 8765 有自动测试。
- [x] M0-7 `contribos doctor`（基础检查）

  验证：`python -m pytest -q` 102 passed；真实 `contribos doctor` 与鉴权后的 `/api/doctor` 均输出 Python/git/gh 正常；缺失、未登录、超时与脱敏由假进程测试覆盖。

  M0 规模：非测试 Python 516 行；前端 20 行；无 Rust。`compileall`、`git diff --check` 与新增文件凭据模式检查通过；下一项 M1-1。

### M1 ACP 接入 + 本机直跑
- [x] M1-1 `agent/engine.py` 协议、能力与事件类型
  验证：Python 3.12 `pytest -q` 104 passed；事件持久化边界与任务上下文默认隔离有测试，依赖固定官方 SDK 1.0.0。
- [x] M1-2 `tests/fake_acp_agent.py` 脚本化假 ACP Agent
  验证：Python 3.12 `pytest -q` 105 passed；SDK 子进程真实读上下文、写文件和执行 Python 测试；权限、提问、错误、慢轮和退出场景供后续组件测试调用。
- [x] M1-3 `agent/acp_engine.py` + 事件规范化 + 组件测试
  验证：Python 3.12 `pytest -q` 120 passed；假 ACP 子进程覆盖初始化、load/resume/重建、事件归并与截断、权限/提问往返、取消后继续、退出检测、fs 拒绝与编辑完成/轮结束 diff 回调。
- [x] M1-4 `agent/profiles.py`（codex / cursor / antigravity）+ `contribos agents` + doctor
  验证：Python 3.12 `pytest -q` 132 passed；三条配置、隔离目录/沙箱参数、自检输出和认证/额度/网络错误分类有测试；本机 CLI help 核实参数。Antigravity 无可靠非交互登录检查，状态明确为 unknown。
- [x] M1-5 `agent/fake.py`（FakeEngine）
  验证：Python 3.12 `pytest -q` 133 passed；脚本回放、请求应答、取消后继续、会话复用与事件流关闭通过。
- [x] M1-6 `sandbox/base.py` + `sandbox/host.py`（含环境清理测试）
  验证：Python 3.12 `pytest -q` 140 passed；本地临时仓库真实 clone/建分支/上下文/diff/导出，hooks 不执行；假 ACP 子进程验证 GitHub 凭据清理、空 GH_CONFIG_DIR；信任与路径护栏有测试。
- [x] M1-7 `tasks/instructions.py`
  验证：Python 3.12 `pytest -q` 141 passed；首轮方案等待/直接实现、上下文路径、非可信内容与禁止发布规则均由通用模板测试覆盖。
- [x] M1-8 `contribos chat` 终端入口
  验证：Python 3.12 `pytest -q` 163 passed、3 skipped（真实 Agent 显式启用）；假 ACP + 本地仓库覆盖 CLI stdin/stdout、快照分页、持久化归并、排队、权限/提问、真实 SIGINT 取消与继续、退出后 resume；`compileall`、`git diff --check` 通过。
- [x] M1-9 【需用户】三个 Agent 本机直跑验收，写 `agent-matrix.md`
  验证：在用户确认信任的 `Garden12138/contribos-acceptance-sandbox#1` 上，Codex 与 Cursor 本机直跑各通过 1–8 项和真实集成测试。Antigravity 手动完成修复、5 项单测、取消、继续与原会话恢复。自动集成和配置隔离的限制见 `agent-matrix.md`。

  M1 当前规模：非测试 Python 2,051 行，前端 20 行，无 Rust。全量离线测试 168 passed、3 skipped，`compileall` 和 `git diff --check` 通过。

### M2 沙箱后端
- [ ] M2-1 【需用户】安装并登录 `sbx`，设置 Codex 订阅 OAuth
- [ ] M2-2 Spike（03 第 3.5.6 节），写 `spike-sandbox.md`，补 `agent-matrix.md` 沙箱列
- [ ] M2-3 `sandbox/sbx.py`（或备选 `sandbox/container.py` + `relay/` + 出网代理）
- [ ] M2-4 Sandbox 契约测试 + 可选集成测试 + 凭据/宿主访问探针
- [ ] M2-5 `contribos chat --isolation …` 手动验证

### M3 任务运行时 + 对话 API
- [ ] M3-1 `tasks/store.py`（CRUD、CAS）+ 测试
- [ ] M3-2 `tasks/events.py`（规范化后处理、脱敏、落库、广播）+ 测试
- [ ] M3-3 `tasks/supervisor.py`（队列、并发、排队/打断、取消与强制取消、权限请求）+ 测试
- [ ] M3-4 自动重试、截断续跑 + 测试
- [ ] M3-5 进程/服务/沙箱重启恢复、会话重建 + 测试
- [ ] M3-6 `routes_tasks.py` 全部接口 + `GET /api/agents` + SSE + 契约测试
- [ ] M3-7 手动：真实 Issue 上 HTTP 驱动、中途重启后继续

### M4 桌面应用：界面 + 外壳
- [ ] M4-1 页面框架、导航、任务列表、新建任务面板（Agent 与隔离级别）
- [ ] M4-2 `chat.js`：SSE、消息与活动卡片、Markdown 安全渲染 + `node --test`
- [ ] M4-3 权限卡片、提问卡片
- [ ] M4-4 输入框：排队/打断发送、停止、继续、快捷短语
- [ ] M4-5 右侧面板：改动、Issue、用量
- [ ] M4-6 设置与自检页（Agent 状态、可信 owner、沙箱后端、默认/审查 Agent、工具路径、通知开关）
- [ ] M4-7 `--demo` 离线演示模式
- [ ] M4-8 手动：浏览器模式完成 J2/J3，刷新后完整恢复
- [ ] M4-9 `desktop_mode.py`：启动参数、启动码/外壳令牌、通知事件流、状态接口、PATH 解析、优雅退出 + 测试
- [ ] M4-10 【需用户】安装 Rust 工具链与 `tauri-cli`；`desktop/` 外壳：启动后端与就绪等待、主窗口（无 IPC）、导航拦截、单实例 + `cargo test`
- [ ] M4-11 菜单栏图标与菜单、关窗隐藏、退出确认、开机启动开关
- [ ] M4-12 系统通知（订阅通知事件流、点击定位任务）
- [ ] M4-13 打包：PyInstaller sidecar + `cargo tauri build` 生成 `.app`
- [ ] M4-14 【需用户】从 Finder 启动 `.app` 完成验收（工具路径、J2/J3、关窗通知、退出后继续）

### M5 发布 Draft PR + 跨 Agent 审查
- [ ] M5-1 PR 文案轮（JSON 代码块 + 模板兜底）
- [ ] M5-2 `publish/git_transfer.py` + 本地 bare 仓库测试（含 hooks 不执行、基线祖先校验）
- [ ] M5-3 `publish/github_writer.py` + MockTransport 测试
- [ ] M5-4 `publish/service.py`：预览、哈希、确认、步骤幂等、对账 + 测试
- [ ] M5-5 发布面板 UI
- [ ] M5-6 `review/service.py` + 审查卡片 + 预览中显示审查状态
- [ ] M5-7 【需用户】真实发布到测试仓库，含一次跨 Agent 审查

### M6 发现模块
- [ ] M6-1 移植 `domain.py`、`scoring.py` 及测试
- [ ] M6-2 移植 `github.py` 及测试
- [ ] M6-3 `discovery/service.py` 持久化重写 + 测试
- [ ] M6-4 `routes_discovery.py` + 契约测试
- [ ] M6-5 发现视图 UI、每日定时扫描
- [ ] M6-6 新信号：关联 PR、外部 PR 合并情况

### M7 安全加固 + 真实验收
- [ ] M7-1 威胁表（03 第 7 节）逐项测试补齐
- [ ] M7-2 沙箱出网策略设置 + 探针 + doctor 检查
- [ ] M7-3 本机直跑护栏（可信列表、风险确认、隔离标识）
- [ ] M7-4 资源与磁盘监控
- [ ] M7-5 【需用户】3 个真实 Issue 产品级验收，写 `acceptance-mvp.md`

### M8 v1 功能
- [ ] M8-1 PR Tracker
- [ ] M8-2 贡献记录页
- [ ] M8-3 处理 review 意见与追加提交
- [ ] M8-4 更新 PR 文案、标记 Ready for review
- [ ] M8-5 OpenCode / OpenHands profile + xcli / DeepSeek 模型接入
- [ ] M8-6 任务中途换 Agent 继续
- [ ] M8-7 （按需）`CodexAppServerEngine`
- [ ] M8-8 `import-legacy`
- [ ] M8-9 【需用户】Linux amd64 验证（含桌面包）
