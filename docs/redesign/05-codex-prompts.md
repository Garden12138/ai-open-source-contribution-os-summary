# 05 · Codex 提示词（逐里程碑复制使用）

使用方法：每次新开一个 Codex 会话，先粘贴"通用前缀"，再粘贴对应里程碑的提示词。
一个里程碑做不完就开新会话，用同样的提示词，Codex 会从清单中下一个未勾选的条目继续。

## 通用前缀

```text
你在 ContribOS v2 仓库中工作。开始前只读这些文件：
AGENTS.md、docs/redesign/02-product-spec.md、docs/redesign/03-architecture.md、
docs/redesign/04-implementation-plan.md。不要阅读旧代码，除非下面明确列出（用 git show legacy-v0.1-final:<path>）。

工作方式：
- 从 04-implementation-plan.md 第 6 节中本里程碑下一个未勾选的条目开始，按顺序做。
- 每完成一个条目：跑 `pytest -q`（涉及前端时再跑 `node --test contribos/web/static/tests`，
  涉及 desktop/ 时再跑 `cargo test`），通过后在清单中勾选，并在条目末尾写一行验证方式。
- 遇到【需用户】条目：写清楚需要用户提供或操作什么，跳过它继续做后面不依赖它的条目。
- 不要写验收归档/证据索引/哈希清单类文档；不要引入方案版本、审批对象、多层预算、自研 Agent 循环、
  断网验证、为特定项目写的适配，或为单个 Agent 写的平行业务逻辑（差异只放在 agent/profiles.py）。
- 结束时用中文简要汇报：完成了哪些条目、怎么验证的、还剩什么、有哪些需要用户做的事。
```

## M0 清场与新骨架

```text
目标：完成 04 文档清单 M0（M0-1 由用户授权后执行）。

用户已授权（若未授权则跳过 M0-1、M0-2，只做 M0-3 起的新骨架，放在新目录 contribos/ 下且不删除旧代码）：
1. git switch -c redesign/v2；git tag legacy-v0.1-final <当前 HEAD>。
2. 用 docs/redesign/AGENTS.proposed.md 覆盖根目录 AGENTS.md，并删除 AGENTS.proposed.md。
3. git rm 旧内容：app/ tests/ scripts/ containers/ 以及 docs/ 下除 redesign/ 之外的全部；
   不要动未跟踪的 data/ .local-demo/ .env。新建 docs/legacy.md（≤20 行）说明旧版位置。
4. 重写 README.md（≤80 行）。

然后实现新骨架（03 文档第 5、6、8、10 节）：
- pyproject.toml：包 contribos，依赖 fastapi/uvicorn/httpx，dev 依赖 pytest，入口 contribos=contribos.cli:main。
- contribos/db.py：stdlib sqlite3，WAL，busy_timeout=5000，按文件名顺序执行 contribos/migrations/*.sql，
  记录 schema_migrations；0001_init.sql 包含 03 第 5 节全部表和 CHECK 约束。
- contribos/security.py：脱敏（GitHub 令牌、OpenAI 风格密钥、Authorization 头、URL 中的凭据）、
  一次性启动码（60 秒有效、只能用一次）与会话 Cookie（HttpOnly、SameSite=Strict）、Origin 校验、双提交 CSRF、
  本地访问令牌（~/.contribos/local-token，0600）。可参考 git show legacy-v0.1-final:app/security.py。
- contribos/web/app.py：/api/health 免鉴权，其余 /api 一律需要会话 Cookie 或本地令牌；GET /desktop/bootstrap?code=
  换取会话后重定向到首页；静态文件；仅回环地址启动（否则报错退出）；变更请求的 Origin/CSRF 中间件。
- contribos/cli.py：serve（默认 127.0.0.1:8765，启动时在终端打印一次性登录链接）、doctor（python、git、gh 登录）。
- 测试：迁移空库建表且可重复执行；脱敏用例；无会话的 /api 请求被拒绝；启动码只能用一次且会过期；
  Origin/CSRF 拒绝与通过；非回环启动被拒绝；health。
完成标准：pytest 通过；contribos serve 可启动；contribos doctor 输出各项检查。
```

## M1 ACP 接入 + 本机直跑

```text
目标：完成清单 M1，最终能用 `contribos chat --agent codex|cursor|antigravity <issue_url>`
在本机独立工作区里和 Agent 对话改代码（03 第 3.4、3.5.1、3.5.3 节）。

要点：
- 依赖加入 agent-client-protocol（官方 Python SDK，1.x）。先读它的 quickstart 和 examples，
  用 spawn 方式（或在给定 stdin/stdout 上建立连接）实现客户端。
- agent/acp_engine.py：initialize 时不声明 fs 和 terminal 能力；session/new 传 cwd=<仓库目录>、
  additionalDirectories=[<上下文目录>]、mcpServers=[]；Agent 支持时用 session/resume 或 session/load；
  session/prompt 返回 stopReason；session/cancel；session/request_permission 和结构化提问转为事件并等待
  respond() 应答；03 第 3.4.5 节的事件规范化（消息与思考片段合并后落库，编辑类工具完成后触发 diff 刷新回调）。
- tests/fake_acp_agent.py：用 SDK 写一个脚本化假 Agent（按环境变量选择场景：读文件、编辑、执行命令、
  请求权限、返回错误、慢轮可取消、中途退出），所有组件测试都用它，不依赖真实 Agent。
- agent/profiles.py：codex（命令 codex-acp，安装提示 npm i -g @agentclientprotocol/codex-acp）、
  cursor（cursor-agent acp）、antigravity（agy acp）。每条含检测/版本/登录检查命令、登录指引、
  本机直跑参数（尽量开启 Agent 自带沙箱）、配置隔离方式（如专用 CODEX_HOME、agy --app-data-dir，
  做不到的写明）、可重试错误模式。`contribos agents` 打印状态；doctor 汇总。
- sandbox/host.py：工作区 ~/.contribos/workspaces/<task>/{repo,context}；git clone --filter=blob:none（无凭据）；
  建分支；写 TASK.md / issue.md / linked_prs.json；Agent 子进程环境去掉 GITHUB_TOKEN、GH_TOKEN、SSH_AUTH_SOCK，
  设置 GH_CONFIG_DIR=<空目录>、GIT_CONFIG_GLOBAL=<平台最小配置>、GIT_TERMINAL_PROMPT=0；
  平台自己的 git 命令加 -c core.hooksPath=/dev/null -c core.fsmonitor=false。写测试验证环境清理。
- contribos chat：建任务行、准备工作区、启动 Agent、发首轮指令；逐行读输入；Ctrl-C 取消当前轮；
  /continue；/diff；权限请求在终端里问 y/n。
- M1-9 需要用户登录各 Agent 并提供一个自己的小仓库/Issue；按 03 第 3.4.8 节逐项验收，结果写
  docs/redesign/agent-matrix.md。某个 Agent 不达标就记录原因继续，不要为它改业务逻辑。
完成标准：组件测试通过（离线）；至少 Codex 在本机直跑下完成"方案 → 实现 → 跑测试 → 取消 → 继续"。
```

## M1-9 准备与真实验收（由 Codex 代办准备工作）

```text
你在 ContribOS v2 仓库（分支 redesign/v2）中工作。目标：替我完成 M1-9 的全部准备工作，再完成 M1-9 真实验收，
并更新 docs/redesign/agent-matrix.md。先读：AGENTS.md、docs/redesign/03-architecture.md 第 3.4.6 与 3.4.8 节、
docs/redesign/04-implementation-plan.md 的 M1、docs/redesign/agent-matrix.md。

【我的授权，仅限本次任务】
- 允许全局安装 npm 包 @agentclientprotocol/codex-acp。
- 允许创建并写入 ContribOS 数据目录 ${CONTRIBOS_HOME:-$HOME/.contribos} 下的 agent-homes/ 与 workspaces/。
- 允许用 gh 在我的 GitHub 账号 Garden12138 下新建一个公开测试仓库 contribos-acceptance-sandbox，推送代码并创建 Issue；
  除此之外不对 GitHub 做任何写操作。
- 我确认：该仓库是我的测试仓库，允许为本次验收在本机独立工作区执行其代码和测试，可以使用 --trust-repo 和
  CONTRIBOS_IT_TRUST_REPO=1。
- 不授权：提交或推送 ContribOS 仓库、修改其他任何仓库、复制我的个人配置。
需要在沙箱外运行、写工作区外目录、联网或打开浏览器的命令，逐条申请批准并说明用途。

【安全要求】
- 不读取、打印、复制任何令牌或凭据文件（auth.json、Cursor/Antigravity 登录文件、gh 令牌、钥匙串）；
  登录状态只用各工具的 status 类命令或 contribos agents 判断；汇报中的错误信息要脱敏。
- 不把 ~/.codex、~/.cursor、~/.gemini 等个人配置复制进专用目录，专用目录必须单独登录
  （复制 Codex 的 auth.json 还会因刷新令牌轮换导致两边掉线）。
- 测试仓库中不能包含任何秘密。

【第一步：环境】
1. source .venv-v2/bin/activate；contribos --version。
2. npm i -g @agentclientprotocol/codex-acp；codex-acp --version。若找不到命令，检查 `npm prefix -g` 的 bin 是否在 PATH 中并告诉我。
3. contribos_data_dir="${CONTRIBOS_HOME:-$HOME/.contribos}"；mkdir -p "$contribos_data_dir/agent-homes/codex"
   "$contribos_data_dir/agent-homes/cursor" "$contribos_data_dir/agent-homes/antigravity/gemini"。

【第二步：登录（浏览器授权由我本人完成）】
做法：在后台启动登录命令并把输出写入临时日志，只把其中的登录链接或设备码发给我；之后每 15 秒检查一次登录状态，
最多等 10 分钟，完成后再做下一个。若命令需要交互终端、无法在你的环境中完成，就给我一条可直接粘贴到我自己终端的命令，
等我回复"好了"再检查。
- Codex：CODEX_HOME="$contribos_data_dir/agent-homes/codex" codex login --device-auth
  （若提示账号未开启设备码授权，改用不带 --device-auth 的浏览器方式）；用同样 CODEX_HOME 的 codex login status 确认。
- Cursor：NO_OPEN_BROWSER=1 CURSOR_CONFIG_DIR="$contribos_data_dir/agent-homes/cursor" cursor-agent login；
  用同样 CURSOR_CONFIG_DIR 的 cursor-agent status --format json 确认。
- Antigravity：agy acp --app-data-dir "$contribos_data_dir/agent-homes/antigravity"
  --gemini-dir "$contribos_data_dir/agent-homes/antigravity/gemini" --login。它多半需要交互终端，优先让我在自己的终端执行。
- 最后运行 contribos agents：预期 Codex、Cursor 为 ok；Antigravity 为 unknown 可以继续。

【第三步：测试仓库与 Issue】
1. gh auth status；未登录或令牌无效时，让我在自己的终端执行 gh auth login -h github.com -p https -w，等我回复后继续。
2. 在临时目录（不要放在 ContribOS 仓库内）创建小型 Python 项目：只用标准库、无第三方依赖，测试用 unittest
   （命令 python -m unittest -v），包含 README、MIT LICENSE、.gitignore。
3. 植入一个真实、可复现、修复范围小的缺陷（例如文本统计函数对连续空格或首尾空白处理错误）；现有测试全部通过，但没有覆盖该缺陷。
4. gh repo create Garden12138/contribos-acceptance-sandbox --public 并推送到默认分支；若同名仓库已存在，先告诉我，不要覆盖。
5. 创建 Issue：标题清楚；正文写当前行为、期望行为、复现步骤（可直接运行的命令和实际输出）、测试命令；不要写修复方案或代码。
6. 把仓库和 Issue 链接发给我。

【第四步：M1-9 真实验收（先 Codex，再 Cursor，最后 Antigravity）】
注意：macOS 不允许嵌套 Seatbelt 沙箱。真实 Agent 的验收命令要申请在你的沙箱之外运行，否则 Agent 自带的沙箱会启动失败；
不要据此判定 Agent 不可用。
对每个 Agent，在本机直跑下按 03 第 3.4.8 节 8 项检查逐项验收；同一个 Issue，每个 Agent 用独立任务和工作区：
- 先跑：CONTRIBOS_IT_AGENTS=<agent> CONTRIBOS_IT_ISSUE_URL=<Issue> CONTRIBOS_IT_TRUST_REPO=1 pytest -q -m agents。
- 再用 contribos chat --agent <agent> --trust-repo <Issue> 手动走一遍：方案 → 回复"按这个方案开始实现" → 实现 → 跑测试 →
  中途取消 → /continue → /diff；然后结束 Agent 进程，用 contribos chat --agent <agent> --resume <TASK_ID> 验证能恢复。
  权限请求按 03 第 3.4.6 节处理，不要自动批准超出工作区的操作。
- 第 7 项配置隔离要有可观察证据：确认我个人配置中的 MCP 服务器和插件，在专用目录启动的 Agent 中不可见
  （只比较条目名称，不打印配置中的密钥或值）。
- 第 8 项错误映射：可以用一个未登录的临时空配置目录验证"需要登录"被正确识别且不重试；不要为了制造限流而大量请求。
- 某个 Agent 不达标：记录现象和脱敏错误，继续下一个。只有确属平台缺陷时才修 ContribOS 代码，修完补测试并跑全量 pytest；
  不要为单个 Agent 写平行业务逻辑。

【收尾】
- 更新 agent-matrix.md：每个 Agent 一行，写版本、1–8 项结果、待处理原因（说明 ≤ 5 行），不写任何凭据。
- 当 Codex 的 8 项全部通过时，勾选 04 清单的 M1-9，并在条目末尾写一行验证方式；否则保持未勾选并写明阻塞原因。
- 跑 pytest -q、python -m compileall -q contribos、git diff --check。不要提交或推送 ContribOS 仓库。
- 用中文汇报：完成了什么、每个 Agent 的结论、测试仓库与 Issue 链接、还需要我做什么。
```

## M2 沙箱后端

```text
目标：完成清单 M2（03 第 3.5.4–3.5.6 节）。

- M2-1 需要用户：brew install docker/tap/sbx、sbx login、sbx secret set openai --oauth。
- 先查阅 sbx 当前版本的 CLI 帮助和官方文档（创建无挂载沙箱、在沙箱内执行命令、网络策略、
  宿主服务访问、文件导出、停止/启动），再逐项完成 03 第 3.5.6 节的 Spike，结论写 docs/redesign/spike-sandbox.md
  （≤60 行），并在 agent-matrix.md 补充每个 Agent 的沙箱列（可行性、凭据位置）。
- 通过：实现 sandbox/sbx.py，满足 sandbox/base.py 的接口：prepare（无挂载沙箱 + VM 内 clone + 上下文文件 +
  出网策略）、exec、spawn_agent（沙箱内以 stdio 运行 profile 的 ACP 命令）、export_file、stop、destroy。
- 不通过：实现 sandbox/container.py（03 第 3.5.5 节的镜像与参数）、relay/server.py（每任务令牌、预算、
  只存令牌哈希）和出网代理；此时只允许 API Key 型 Agent 使用该级别。
- 写后端无关的 Sandbox 契约测试（假后端）；可选集成测试（CONTRIBOS_IT_SBX=1 / CONTRIBOS_IT_DOCKER=1）：
  准备、Codex 一轮产生 diff、导出 bundle、停止后恢复；探针：VM/容器内搜不到真实模型令牌，
  宿主回环上的金丝雀 HTTP 服务不可达。
- contribos chat --isolation sbx|container 手动验证。
```

## M3 任务运行时 + 对话 API

```text
目标：完成清单 M3。实现 tasks/store.py、tasks/events.py、tasks/supervisor.py、web/routes_tasks.py、
GET /api/agents（03 第 3.3、4、5、6、9 节）。

必须覆盖的测试（FakeEngine 或假 ACP Agent，离线）：
- 消息开新轮；运行中消息默认排队，delivery=interrupt 时先 cancel 再发；interrupt；continue。
- client_message_id 幂等；并发两条消息只产生一个运行中的轮（CAS）。
- 重试：匹配 profile 可重试模式的错误最多 3 次退避重试（替换时钟），之后 agent_state=error；
  stopReason=max_tokens/max_turn_requests 自动继续一次；认证/额度错误不重试。
- 恢复：Agent 进程退出 → 重启并 resume/load；不支持时新建会话并发送"指令 + 最近对话回顾 + diff 摘要"，
  写 system.session_recreated；服务启动时扫描 running 任务并恢复。
- 权限/提问：生成 permission.requested / question.asked，经 POST /api/tasks/{id}/requests/{rid}/respond 应答；
  进程重启后旧请求标记失效。
- SSE：Last-Event-ID 补发；增量事件不带 id；客户端断开不影响后台。
- 脱敏：命令输出中的金丝雀令牌不出现在 task_events、SSE、API 响应中。
- 新建任务的隔离校验：owner 不在可信列表且 isolation=host 时，没有 trust_confirmed=true 一律拒绝。

M1 验收发现的问题（见 docs/redesign/agent-matrix.md），本里程碑一并解决：
- 强制取消（03 第 3.3 节）：Agent 以独立进程组启动；session/cancel 后 10 秒内轮未结束或该轮启动的子进程仍在，
  就终止整个进程组、重启 Agent 并恢复会话，写 system.cancel_forced。用假 Agent 覆盖"不响应取消""遗留子进程"两种情况。
- 地区不可用类错误（Antigravity 出现过 API 地区错误）：在 profiles 的错误分类中新增不可重试的 agent_region_unavailable，
  提示检查网络出口或代理；用脱敏后的错误文本写测试。
- 本机直跑的权限请求必须等待用户应答（经 requests/{id}/respond），不能像非交互测试那样默认拒绝；超时不自动处理，只在 UI 中保持待处理。
完成标准：测试通过；手动用 curl 在真实 Issue 上驱动对话，轮进行中杀掉服务再启动，"继续"后接着干；
对一个正在跑长测试的轮执行停止，确认测试进程在 10 秒内停止。
```

## M4 桌面应用（第一段：界面，M4-1 至 M4-8）

```text
目标：完成清单 M4-1 至 M4-8。原生 HTML/CSS/JS，不引入前端框架和构建工具（02 第 2、4 节，03 第 3.1 节）。
先在浏览器模式（contribos serve + 登录链接）下开发；界面只通过 HTTP/SSE 访问后端，不调用任何桌面原生接口。

- 布局：左侧任务列表 + 新建；中间对话流；右侧面板（改动 / Issue / 用量，后续加审查、发布）；
  任务头部显示 Agent 和隔离级别（本机直跑用醒目标识）。
- 新建任务面板：Issue 链接；Agent 下拉（显示安装/登录状态，不可用的置灰并给出登录指引）；
  隔离级别（非可信仓库默认沙箱；选本机直跑需勾选"我信任这个仓库"并显示风险说明，可选加入可信列表）。
- chat.js：EventSource 订阅 /api/tasks/{id}/stream；渲染 message.*、reasoning.*（默认折叠）、
  command.*（命令、退出码、输出末尾，可展开）、files.changed、tool.activity（折叠）、plan.updated、
  system.*（重试提示、会话重建提示、错误 + 继续按钮）。
- 权限卡片（工具、命令、路径；允许 / 拒绝 / 本任务内总是允许）与提问卡片（可点选项或自填）；失效后置灰。
- Markdown 渲染：先转义 HTML 再处理代码块、行内代码、粗体、列表、链接（仅 http/https）；写 XSS 测试。
- 输入框：Enter 发送、Shift+Enter 换行；运行中发送默认排队，旁边提供"打断并发送"；停止、继续；快捷短语只是预填文本。
- 改动面板：调用 /api/tasks/{id}/diff，文件列表 + unified diff 渲染，收到 diff.updated 自动刷新。
- 设置页：/api/doctor、/api/agents（状态与登录指引）、可信 owner 列表、沙箱后端状态、默认 Agent、审查 Agent、
  首轮策略、资源上限、各命令行工具路径、各类通知开关。
- 外部链接（PR、Issue）使用普通 <a target="_blank">，由桌面外壳拦截后交给系统浏览器。
- contribos serve --demo：FakeEngine + 示例任务，离线可演示全流程。
- 纯函数测试放在 contribos/web/static/tests/，用 node --test 运行。
完成标准：测试通过；浏览器模式下完成粘贴链接 → 选 Agent 和隔离级别 → 对话 → 看改动；刷新后历史和进行中输出完整恢复。
```

## M4 桌面应用（第二段：外壳，M4-9 至 M4-14）

```text
目标：完成清单 M4-9 至 M4-14（03 第 3.10 节是规格，逐条实现）。

后端（contribos/desktop_mode.py）：
- contribos serve --desktop --port <p>：从环境变量读取启动码和外壳令牌（不接受命令行传入），就绪后在 stdout 输出一行
  CONTRIBOS_READY port=<p>；GET /api/notifications/stream（SSE）与 GET /api/desktop/status 只接受外壳令牌。
- 通知事件：turn 结束、permission/question 待处理、任务出错停下、发布成功/失败、每日推荐就绪；按设置开关过滤。
- 桌面模式启动时用 $SHELL -l -c 读取 PATH（超时 5 秒，失败则用默认值并在 doctor 中提示）；设置中的工具绝对路径优先。
- 收到 SIGTERM：取消运行中的轮并标记 interrupted、停止 Agent 进程、关闭数据库、退出。
- 测试：令牌鉴权、通知过滤、PATH 解析（用假 shell 脚本）、SIGTERM 后的状态。

外壳（desktop/，Tauri v2，Rust ≤ 600 行，不放业务逻辑）：
- 【需用户】安装 Rust（rustup）与 tauri-cli。
- 启动：生成随机端口、启动码、外壳令牌；开发模式用 CONTRIBOS_DEV_PYTHON 指向的 venv 运行
  python -m contribos serve --desktop，发布模式用 sidecar（externalBin）；等待就绪行和 /api/health（20 秒超时，
  失败显示错误窗口和日志路径）；主窗口加载 http://127.0.0.1:<p>/desktop/bootstrap?code=<启动码>。
- 安全：主窗口不配置任何 capability（包括 remote），界面拿不到任何 IPC；on_navigation 只放行后端来源，
  其他 http(s) 链接用系统浏览器打开；写测试读取 tauri.conf.json 与 capabilities/ 断言主窗口无权限。
- 插件只用：shell（仅 Rust 侧启动 sidecar）、notification、single-instance、autostart。
- 菜单栏：打开主窗口、运行中任务数、待处理请求数、开机启动（勾选）、退出；数据来自 /api/desktop/status 和通知事件流。
- 关闭主窗口只隐藏；退出时若有运行中的轮弹确认，确认后 SIGTERM 后端，15 秒未退出则强制结束；
  后端意外退出时自动重启一次并发通知。
- 通知：Rust 侧订阅 /api/notifications/stream，用系统通知展示；点击通知打开主窗口并导航到对应任务
  （不支持点击回调时，激活应用后跳到最近一条通知的任务）。
- cargo test：就绪行解析、导航过滤。

打包：packaging/contribos-backend.spec（PyInstaller，先单文件）；cargo tauri build 生成 .app；
打包产物启动后自动跑一次 /api/health 与 doctor 作为冒烟检查。
M4-14 需要用户从 Finder 启动 .app 按 04 文档 M4 验收逐项确认。
```

## M5 发布 Draft PR + 跨 Agent 审查

```text
目标：完成清单 M5（03 第 3.6、3.7 节是规格，逐条实现）。

发布的关键约束：
- 打包在任务的执行环境内完成（经 Sandbox.exec），bundle 经 Sandbox.export_file 导出；宿主从不对沙箱工作区执行 git。
- 宿主侧只操作 bare 仓库对象：不 checkout、core.hooksPath=/dev/null、不跑 LFS/filters；
  基线 P 必须是上游默认分支的祖先（merge-base --is-ancestor）；用候选提交的 tree 在宿主
  git commit-tree 重建提交，作者信息来自设置。
- 令牌来自 gh auth token 或 CONTRIBOS_GITHUB_TOKEN，经环境变量注入 http.extraHeader，
  不得出现在命令行参数、URL、日志、事件、响应中；写测试检查子进程参数。
- 三个步骤都"先查后做"：Fork、推送（远程同名分支同 SHA 跳过、不同 SHA 失败，绝不 force）、
  建 PR（先查 head 对应 PR）。异常 → unknown → 对账重跑。
- 只有最新且状态为 previewed 的预览、且 preview_hash 匹配时才能确认。
- PR 文案：向实现 Agent 发固定提示，要求最后输出 JSON 代码块 {title, body, commit_message}；解析失败用模板兜底。

审查：
- review/service.py：默认用设置中的审查 Agent（与实现 Agent 不同；不可用时用同一 Agent 新会话），
  在同一执行环境新开会话、Agent 支持时切只读模式；要求输出 JSON 代码块 {verdict, summary, findings[]}；
  审查前后各算一次 diff 哈希，改动了工作区则标记 invalid。结果卡片；"按审查意见修改"把发现项发给实现会话。
测试：本地 bare 仓库夹具（含会写哨兵文件的 hooks，断言哨兵不存在）；httpx.MockTransport 模拟 GitHub；
假 ACP Agent 模拟审查输出（含 JSON 损坏、审查改动工作区两种情况）。
完成标准：测试通过；真实发布到用户的测试仓库（需要用户确认目标仓库和 gh 登录），至少一次跨 Agent 审查。
```

## M6 发现模块（可与 M1–M5 并行）

```text
目标：完成清单 M6（03 第 3.9 节，02 第 2 节 J1）。

- 移植：git show legacy-v0.1-final:app/domain.py、app/scoring.py、app/github.py 到 contribos/discovery/，
  去掉 app.provenance 依赖（canonical_json 用 json.dumps(sort_keys=True, separators=(',',':')) 替代）。
- 重写 discovery/service.py 的持久化，使用 0001_init.sql 中的发现相关表。
- 移植旧测试中仍适用的用例：git show legacy-v0.1-final:tests/test_scoring.py、tests/test_github.py、tests/test_service.py。
- routes_discovery.py 与发现视图：今日推荐（3/3/2/2）、全部机会（筛选、收藏/忽略）、扫描进度、"开始贡献"
  （打开 M4 的新建任务面板并预填）。
- 每日定时扫描（服务运行时，时间可配置）；新信号：Issue 已有关联 PR、仓库近 90 天外部 PR 合并情况。
完成标准：测试通过（全部假数据）；手动真实扫描一次，首页出现推荐，点"开始贡献"进入任务对话。
```

## M7 安全加固 + 真实验收

```text
目标：完成清单 M7（03 第 7 节威胁表）。

- 对照 T1–T11 逐项确认有测试覆盖，缺的补上。
- 沙箱出网策略：默认放行 GitHub、PyPI/npm 等包仓库和所选 Agent 的模型端点，其余拒绝；设置页可追加域名。
  探针（集成测试）：宿主回环和局域网地址各起一个金丝雀 HTTP 服务，沙箱内访问必须失败；包仓库可访问；
  沙箱内 env 和常见凭据文件路径中找不到真实令牌。doctor 汇总这些检查。
- 本机直跑护栏：可信列表校验、逐任务风险确认、环境清理回归测试、任务头部常驻隔离标识。
- 资源与磁盘监控（超阈值告警并暂停任务）。
- 威胁 T12：确认主窗口无 IPC 权限的配置测试仍然通过；CSP 生效；外部链接不在应用窗口内打开。
- 产品级验收需要用户参与：协助用户在 3 个真实 Issue 上、只通过桌面应用走完全流程（≥1 Python、≥1 TypeScript、
  ≥1 在沙箱中、≥2 家 Agent 参与，至少一次关窗后通过通知回到任务），结果写 docs/redesign/acceptance-mvp.md（每个任务 ≤15 行）。
```

## M8 v1 功能

```text
目标：完成清单 M8（02 第 2 节 J5，03 第 3.4.7、3.4.9、3.6、3.8 节）。
- PR Tracker：每 30 分钟刷新未关闭 PR；写 pull_requests；发 pr.updated 事件。贡献记录页：列表、月度统计、热力图、赏金标记。
- 处理 review 意见：把 PR 审查评论带入新一轮；追加提交走 kind=push_followup 预览 + 确认，快进推送；
  基线变化需要 rebase 时提示无法追加，不 force-push。更新 PR 标题/正文、标记 Ready for review：各自预览 + 确认。
- OpenCode、OpenHands profile：按 03 第 3.4.8 节验收；配置它们使用 xcli（OpenAI 兼容端点）或 DeepSeek；
  沙箱中访问宿主 xcli 必须走 spike-sandbox.md 中确定的受控方式，不能直接暴露端口。
- 换 Agent 继续：新会话 + 指令 + 对话回顾 + 当前 diff，写 system.session_recreated。
- 若 M7 验收证明 ACP 缺口明显影响 Codex 体验，实现 agent/codex_appserver.py（turn/steer、review/start、
  outputSchema、tokenUsage、codexErrorInfo），接口与 AcpEngine 一致。
- contribos import-legacy --db <旧库路径>：只读打开旧 SQLite，导入仓库/机会、最新偏好、收藏忽略、已发布 PR；
  旧任务、方案、执行不导入。写测试（小型夹具库）。
- Linux amd64：跑集成测试和探针，生成并试用桌面包（AppImage / deb），结果追加到 acceptance-mvp.md。
```
