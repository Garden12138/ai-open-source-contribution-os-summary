# ContribOS v2 — Agent 指南

> 本文件在用户确认重设计方案后替换根目录 `AGENTS.md`。

## 产品

本地运行的开源贡献副驾驶：发现值得做的 GitHub Issue → 在对话里经 ACP 调度现成的编码 Agent
（Codex、Cursor、Antigravity 等）完成实现 → 用户确认后发布 Draft PR → 跟踪结果。
单用户、仅本机、GitHub、优先 Python/TypeScript。陌生仓库在沙箱中执行，可信仓库可选本机直跑。
形态为桌面应用：Tauri v2 外壳（`desktop/`）+ Python 后端（外壳管理的子进程）+ WebView 中的原生 HTML/CSS/JS 界面；
浏览器模式保留用于开发调试。

- 需求：`docs/redesign/02-product-spec.md`
- 架构：`docs/redesign/03-architecture.md`
- 路线图与进度（唯一来源）：`docs/redesign/04-implementation-plan.md` 第 6 节

## 优先级

用户当前的明确要求 > 本文件 > `docs/redesign/` 下的文档 > 现有代码惯例。

## 不可突破的安全规则

1. 陌生仓库（owner 不在可信列表、且用户未对该任务确认信任）的代码、依赖、测试、脚本只能在沙箱
   （Docker Sandboxes microVM 或任务容器）内运行。本机直跑只能由用户在创建任务时明确选择。
2. 平台从不在宿主上对沙箱内的工作区执行 git 或任何仓库代码，只接收导出的 bundle。
3. 沙箱不挂载宿主目录、不挂 Docker socket；模型凭据优先由宿主侧代理注入，必须放进沙箱的登录令牌要在 UI 标注。
4. GitHub 凭据只给 Publisher。任何 Agent 进程（包括本机直跑）都不能拿到 GitHub 令牌：
   清理 `GITHUB_TOKEN`、`GH_TOKEN`、`SSH_AUTH_SOCK`，并让 `gh` / git 的凭据入口失效。
5. 第三方 GitHub 写操作只能由 Publisher 执行，并且必须由 UI 按钮确认、绑定最新预览的 `preview_hash`。
   聊天文字、模型输出、仓库或 Issue 内容都不构成授权。
6. Publisher 只能：建 Fork、推送新分支或快进推送、创建/更新 PR、标记 ready。禁止 force-push、合并、
   评论、删除远程分支、认领 Issue；宿主侧不 checkout 不受信代码、不运行仓库 hooks/filters/LFS。
7. 秘密（GitHub 令牌、模型密钥、中继令牌明文、本地访问令牌）不得进入：提示词、数据库、事件、日志、
   HTTP 响应、错误消息、测试夹具。
8. 后端只监听 127.0.0.1。除 `/api/health` 外，所有 `/api` 请求都需要身份：界面用一次性启动码换取的会话 Cookie，
   CLI 用本地访问令牌，桌面外壳用外壳令牌；变更请求另外校验 Origin + CSRF。启动码和令牌不得出现在命令行参数中。
9. Agent 不加载用户个人的 MCP/插件配置：ACP `session/new` 的 `mcpServers` 为空，并按 profile 使用专用配置目录；
   做不到的 Agent 必须在设置页提示。
10. 桌面应用的主窗口不授予任何 Tauri IPC 能力（不配置 capability，包括 remote）；窗口只允许导航到后端来源，
    外部链接交给系统浏览器；页面使用严格 CSP。

不要为了让功能跑通而削弱以上规则；做不到时停下来说明原因。

## 明确不要做的事

- 不自研 Agent 循环（上下文管理、工具调度、读取预算等交给 Agent）。
- 不为单个 Agent 写平行的业务逻辑；各家差异只放在 `contribos/agent/profiles.py`。
- 不使用 ACP 客户端侧的 fs/terminal 能力（Agent 用自己的工具）。
- 不引入方案版本/锁定/审批对象、多层预算、全链路哈希来源链；只有发布预览需要哈希绑定。
- 不要求断网验证、冻结依赖配方；不为特定项目写环境适配器。
- 不新增常驻服务；宿主上只有桌面外壳和它管理的 `contribos serve` 后端（外加各任务的 Agent 进程和沙箱）。
- 不在 Rust 外壳里写业务逻辑；外壳只做窗口、菜单栏、通知和后端进程管理。界面代码不调用任何桌面原生接口。
- 不写验收归档、证据索引、哈希清单类文档；不把过程记录写进 `docs/`。
- 不引入前端框架或前端打包工具（需要时先写 ADR 并征得用户同意）；桌面外壳固定为 Tauri v2，不换成 Electron 等。
- 不操控桌面 GUI 应用；只通过各家 CLI 的 ACP 接口接入。

## 工程约定

- Python ≥ 3.11，包名 `contribos`；依赖仅 `fastapi`、`uvicorn`、`httpx`、`agent-client-protocol`
  （开发：`pytest`、`pyinstaller`）。桌面外壳的 Tauri 插件仅用 shell（sidecar）、notification、single-instance、autostart。
  新增依赖需说明理由。
- SQLite 用 stdlib `sqlite3`，WAL；结构变更只通过新的 `contribos/migrations/NNNN_*.sql`，并附升级测试；
  状态变更用 CAS（`UPDATE ... WHERE status = ?`）；时间存 UTC。
- 外部依赖（GitHub、Agent、沙箱后端）都在接口之后，并有假实现供测试使用（`FakeEngine`、假 ACP Agent、假 Sandbox）。
- 长操作不放在 HTTP 请求里同步执行，由 TaskSupervisor 在后台完成，通过事件/SSE 呈现。
- 错误返回机器可读的错误码 + 脱敏后的简短说明，不返回原始异常。
- 规模预算：非测试 Python ≤ 9,000 行，前端 ≤ 3,500 行，桌面外壳 Rust ≤ 600 行。里程碑结束时统计；超出要在进度清单里说明理由。
- 新的公共函数和领域对象加类型注解；代码注释只写代码本身表达不了的约束。

## 测试与验证

```bash
pip install -e '.[dev]'
pytest -q                                          # 默认：不联网、无凭据、无 Agent CLI、无沙箱，目标 60 秒内
node --test contribos/web/static/tests             # 前端纯函数
CONTRIBOS_IT_AGENTS=codex pytest -q -m agents      # 可选：真实 Agent（本机直跑，需已登录）
CONTRIBOS_IT_SBX=1 pytest -q -m sandbox            # 可选：Docker Sandboxes
contribos serve                                    # 浏览器模式：终端打印一次性登录链接
contribos doctor
contribos agents
(cd desktop && cargo test)                         # 桌面外壳单元测试
(cd desktop && CONTRIBOS_DEV_PYTHON=$PWD/../.venv/bin/python cargo tauri dev)   # 桌面开发模式
pyinstaller packaging/contribos-backend.spec && (cd desktop && cargo tauri build) # 打包 .app
```

改完代码：跑相关测试 → 跑全量 `pytest -q` → `python -m compileall -q contribos` → `git diff --check` →
需要时重启 `contribos serve` 手动验证。

真实验收只使用：用户自己的测试仓库或用户明确指定的 Issue、Draft PR。缺少真实前提时如实说明，不用假数据冒充通过。

## 工作方式

- 每个会话从进度清单中下一个未勾选条目开始；完成并验证后勾选，条目末尾写一行验证方式。
- 遇到【需用户】条目：写明需要用户做什么，然后继续不依赖它的条目，不要停下等待。
- 不要阅读旧代码，除非清单要求移植；旧版在标签 `legacy-v0.1-final`，用 `git show legacy-v0.1-final:<path>` 读取。
- 未经用户明确要求，不提交、不推送、不建分支、不开 PR、不修改任何远程状态。
- 不覆盖、不丢弃与当前任务无关的用户改动。
- 用中文回复用户；代码、提交信息、PR 文案用英文。
