# AI Open Source Contribution OS

面向独立开发者的开源贡献工作台：发现值得做的 GitHub Issue，按个人目标筛选和比较候选，再跟进真实贡献进度。

当前版本实现了产品闭环的第一个纵向切片：

```text
GitHub Search → 候选去重 → 仓库元数据缓存 → 硬规则过滤
              → 七维评分 → 个性化推荐 → 批量 AI 筛选 → 候选比较
              → Vibe Coding 工作台 → Review → Draft PR 意图 → 贡献进度
```

## 已实现

- 单用户、只读 GitHub Issue 发现。
- 默认搜索 Python、TypeScript、`help wanted`、`good first issue` 和 bounty 机会。
- 候选跨查询去重，仓库元数据按 24 小时缓存。
- 排除已归档、无可识别许可证、已分配、描述过短或长期不活跃的任务。
- 按奖励可靠性、接受概率、技术匹配、项目影响力、清晰度、竞争度和学习价值计算 0～100 分。
- 保存每个维度的分数、风险扣分和原因，结果可审计。
- 每日榜单优先组合 3 个赏金、3 个高影响力、2 个技术匹配和 2 个战略机会；不足时按总分补位。
- 首次使用可设置贡献目标、偏好语言、每周投入与最低赏金；推荐会对完整合格候选池重新排序，但不会覆盖原始规则分。
- 原生 Studio 工作台：侧栏导航／任务历史、卡片机会列表、点击展开详情、独立任务对话和可编辑方案，支持候选比较、浅色／深色和窄屏布局。
- 模型设置支持 MiniMax 国内官方 API、NVIDIA 与 OpenAI 兼容服务、自定义模型、默认及四阶段覆盖；API Key 由浏览器加密并仅保存到独立 Gateway，任务冻结模型版本。
- 支持每日自动扫描、候选提醒，以及新增高匹配机会和候选内容/评分变化的站内通知；需持续运行 Worker。
- 默认发现结果明确使用条件查询、硬筛选和规则/偏好排序，不会暗中调用模型；Provider 就绪时可对当前规则 Top 30 中最匹配的 5 个候选批量运行有预算上限的 AI 筛选。
- AI 结论只调整独立的决策排序，不覆盖原始规则分；旧 Snapshot 的分析不会用于新扫描候选，也不能启动新的贡献任务。
- 分析先从冻结仓库快照生成中文项目介绍，再从冻结 Issue 生成中文需求概括，后续匹配度、工作量、竞争、风险和结论都基于这两部分；详情以精简 Markdown 报告展示并可直接下载，结构化 JSON 仅保留给内部校验、评分和不可变溯源。
- “我的候选”可直接进入 AI 评估与 Vibe Coding 工作台；“贡献进度”可继续已有任务，完成计划、批准、执行、Review 和发布意图流程。
- FastAPI、SQLite、原生 Web 产品和可供 cron 调用的 CLI。

默认禁止 GitHub 写入。“制定贡献方案”支持 AI 阅读代码、提问、可编辑方案和批准后的自动执行；验证与审查通过后停在人工验收。配置独立 Publisher 并再次确认精确发布内容后，才允许 Fork、Push 和 Draft PR。使用、隔离配置及当前验收边界见 [贡献方案工作台](docs/contribution-workbench.md)。

## 库完成 vs 生产可点

| 环节 | 库 / API / UI | 默认 `serve` + `worker` |
| --- | --- | --- |
| GitHub 扫描与 3/3/2/2 榜单 | 已完成 | **可点** |
| AI 深析与批量筛选 | 已完成 | MiniMax-M3 可通过国内官方 API 直连并作为四阶段默认；其他 Profile 可手动切换 |
| AI 规划、编辑版本、批准 | 已实现 | 需 AnalysisVersion、实现模型、固定 Runner 摘要和独立 Sandbox Worker；对话与方案同屏 |
| 隔离执行 Explore/Implement/Verify | 引擎与状态机已完成 | 采集归档后，`SANDBOX_STAGE_RUNTIME=fake` 可离线跑通 Explore → ChangeSet → Implement → Verify |
| 从计划生成代码 / ChangeSet | 已接线 | DeepSeek V4 Pro 支持冻结上下文、多轮对话、结构化提案和精确哈希确认 |
| 独立 Review / 有界修复 | 已完成 | Fake 可离线演示；MiniMax M3 可审查精确 diff 与测试 Artifact |
| 人工验收 / Draft PR | 本地实现与契约测试通过 | 默认关闭；`PUBLISHER_MODE=gh` 使用独立 Publisher，最终确认才 Fork／Push／Draft；真实账号验收待完成 |
| PR 事件 / 生命周期 / 热力图 | 已完成（本地观测） | 看板可点；真实 GitHub 轮询未启用 |

## 快速开始

完整的安装、配置、双进程启动步骤和分层测试案例见
[部署、启动与自测指南](docs/deployment-and-self-test.md)。
NVIDIA Build 的模型分工、密钥隔离、Compose Profile 和完整五进程启动方式见
[NVIDIA Provider 接入指南](docs/nvidia-provider.md)。
新界面、通用模型配置和独立密钥卷的升级要求见 [Studio 使用说明](docs/studio-workbench.md)。
在模型设置页录入 MiniMax API Key 并选择“配置并设为四阶段默认”，即可通过
`https://api.minimax.cn/v1` 使用官方 `MiniMax-M3`；密钥不会进入业务数据库。

要求 Python 3.11+。推荐使用 `uv`：

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e '.[dev]'
export GITHUB_TOKEN='your-read-only-token'
contribos serve
```

打开 <http://127.0.0.1:8000>。GitHub Token 不是硬性要求，但未认证请求的额度很低；建议使用只读 fine-grained token。

Web 发起的扫描会进入可恢复的持久任务队列。请在另一个终端启动 Worker：

```bash
source .venv/bin/activate
export GITHUB_TOKEN='your-read-only-token'
contribos worker
```

若设置了 `LOCAL_ACCESS_TOKEN`，所有写 API 还必须携带
`Authorization: Bearer <token>` 或 `X-ContribOS-Token`。浏览器写请求同时
校验同源 `Origin` 和由 `/api/v1/meta` 返回的 CSRF Token；访问令牌不会写入
页面、数据库或日志。

直接运行一次扫描：

```bash
contribos scan
```

检查 Phase 5 Sandbox Worker 的本机运行前置条件（镜像必须使用本地精确摘要，
不能使用可变 Tag）：

```bash
contribos doctor --runner-image sha256:<64 位小写十六进制摘要>
```

该命令不会读取 GitHub 或应用 Token；它以 JSON 报告 Python、Docker/buildx、
OrbStack 或 Linux 架构、隔离原语、磁盘、无网络能力、Runner 镜像和真实受限
容器探针，任一检查失败都会返回非零状态。

使用自定义查询：

```bash
contribos scan \
  --query 'is:issue is:open no:assignee label:"help wanted" language:Python' \
  --query 'is:issue is:open no:assignee bounty in:title,body'
```

若使用 `.env.example`，本项目不自动读取 `.env`；请由 shell 或进程管理器注入：

```bash
cp .env.example .env
set -a
source .env
set +a
```

该共享注入方式只适合 `none`/`fake` 快速体验。启用 NVIDIA 时不要把同一份完整
`.env` 注入所有进程；按 NVIDIA 指南分别给 Gateway、Provider Worker 和 Sandbox
Worker 最小权限配置。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/v1/meta` | 当前搜索与偏好配置 |
| `POST` | `/api/v1/scans` | 幂等创建一个发现扫描 Job |
| `GET` | `/api/v1/scans` | 扫描历史 |
| `GET` | `/api/v1/jobs/{id}` | 查询 Job 状态、进度与结果 |
| `POST` | `/api/v1/jobs/{id}/cancel` | 请求取消 Job |
| `POST` | `/api/v1/jobs/{id}/retry` | 重试可重试的终态 Job |
| `GET` | `/api/v1/opportunities/daily` | 当日精选榜单 |
| `GET` | `/api/v1/recommendations` | 按本地偏好重排后的完整合格候选池 |
| `POST` | `/api/v1/recommendations/analyses` | 为最多 5 个当前 Top-30 候选排队有总预算上限的分析 Job |
| `GET/POST` | `/api/v1/preferences/current` | 读取或创建不可变的新一版推荐偏好 |
| `POST` | `/api/v1/opportunities/{id}/dispositions` | 加入/移出候选、忽略或设置提醒 |
| `GET` | `/api/v1/shortlist` | 当前候选清单 |
| `GET` | `/api/v1/opportunities/compare` | 并排比较 2～3 个机会 |
| `GET` | `/api/v1/scan-changes/latest` | 最近扫描带来的新匹配和候选变化 |
| `GET` | `/api/v1/notifications` | 站内通知与已读状态 |
| `GET` | `/api/v1/opportunities/{id}` | 完整评分与风险详情 |
| `POST` | `/api/v1/opportunities/{id}/analyses` | 在 Provider 就绪时排队一次分析 Job |
| `GET` | `/api/v1/opportunities/{id}/analyses` | 不可变分析版本历史 |
| `GET` | `/api/v1/opportunities/{id}/analyses/{version}/document` | 查看或下载包含项目介绍、需求内容和关联判断的 Markdown 分析报告 |
| `GET` | `/api/v1/opportunities/{id}/analyses/compare/document` | 查看两个版本的重要内容中文差异 |
| `POST` | `/api/v1/tasks` | 从精确 AnalysisVersion 创建贡献任务 |
| `GET` | `/api/v1/tasks/{id}` | 任务、计划、批准与执行摘要 |
| `POST` | `/api/v1/tasks/{id}/plan-versions` | 创建或修订结构化计划 |
| `POST` | `/api/v1/plan-versions/{id}/approve` | 锁定 base SHA 并批准计划 |
| `POST` | `/api/v1/plan-versions/{id}/archives` | 为已批准计划排队只读仓库归档采集 Job |
| `POST` | `/api/v1/plan-versions/{id}/executions` | 以精确审批、base SHA、归档和 Runner digest 幂等创建执行 |
| `POST` | `/api/v1/executions/{id}/change-sets` | 在 Explore 成功后接受 ChangeSet 并排队 Implement |
| `POST` | `/api/v1/executions/{id}/coding-context` | 在 Sandbox Worker 中冻结批准路径的编码上下文 |
| `GET` | `/api/v1/executions/{id}/coding-session` | 读取经哈希链复验的多轮编码会话与提案 |
| `POST` | `/api/v1/executions/{id}/coding/messages` | 排队一次 DeepSeek 编码对话 |
| `POST` | `/api/v1/executions/{id}/coding/proposals` | 从当前对话生成不可变 ChangeSet 提案 |
| `POST` | `/api/v1/change-set-proposals/{id}/accept` | 按精确 ChangeSet 哈希确认并排队 Implement |
| `POST` | `/api/v1/executions/{id}/review-jobs` | 排队一次 MiniMax 独立 Review |
| `GET` | `/api/v1/executions/{id}` | 查询经溯源复验的执行、阶段、Job 与 Artifact 清单 |
| `GET` | `/api/v1/executions/{id}/artifacts` | 列出属于该执行的经复验 Artifact |
| `GET` | `/api/v1/executions/{id}/artifacts/{artifact_id}` | 按 SHA-256 复验并读取 Artifact 内容 |

FastAPI 交互文档位于 <http://127.0.0.1:8000/docs>。

## 数据库迁移

服务启动和 `contribos scan` 会自动执行有版本、带 checksum 的数据库迁移。
既有 Phase 1 SQLite 数据库只有在完整 schema 校验通过后才会被标记为基线版本；
部分或未知 schema 会拒绝启动，不会通过删库重建来绕过。

升级和恢复约定见
[docs/database-migrations.md](docs/database-migrations.md)。
Explore → Implement → Verify 的不可变执行记录、重启恢复边界和 CAS 规则见
[docs/execution-state.md](docs/execution-state.md)。
可选依赖准备的内部网络、固定包代理、哈希锁和只读 Verify 约束见
[docs/sandbox-dependencies.md](docs/sandbox-dependencies.md)。
Sandbox 内统一禁用 Git hooks、credential helper、LFS/filter、submodule 与
传输协议的规则见 [docs/sandbox-git-safety.md](docs/sandbox-git-safety.md)。
命令、UTC 时间、资源使用、输出哈希与脱敏日志的执行证据约定见
[docs/execution-evidence.md](docs/execution-evidence.md)。
完整文件清单、确定性 unified diff、diff hash 与规范化测试结果也由同一份
执行证据约定覆盖。
Stage 成功前的内容寻址固化、Artifact manifest 与崩溃恢复规则见
[docs/execution-artifacts.md](docs/execution-artifacts.md)。
Verify Artifact 固化后的 workspace 销毁、Worker 隔离与重试规则见
[docs/workspace-disposal.md](docs/workspace-disposal.md)。

## 评分模型

```text
总分 = 奖励可靠性 × 22%
     + 接受概率 × 20%
     + 技术匹配 × 18%
     + 项目影响力 × 15%
     + Issue 清晰度 × 10%
     + 竞争度 × 10%
     + 学习价值 × 5%
     - 风险扣分
```

这是不会被 AI 或个人偏好覆盖的基础规则分。产品推荐会按用户目标调整七个维度的权重，并应用语言、时间和最低赏金偏好；AI 对当前 Snapshot 的 `pursue / consider / skip / insufficient_evidence` 结论只形成独立、可解释的决策排序调整。`GET /api/v1/recommendations?analysis_filter=recommended` 可只读取 AI 建议投入或继续确认的机会。所有评分都只是决策辅助，不承诺 PR 一定合并或赏金一定兑现。

## 配置

常用环境变量见 [.env.example](.env.example)：

- `GITHUB_TOKEN`：GitHub 只读 Token，也兼容 `GH_TOKEN`。
- `GITHUB_QUERIES`：以分号分隔的 GitHub Search 查询。
- `PREFERRED_LANGUAGES`：技术偏好，默认 `Python;TypeScript`。
- `STRATEGIC_KEYWORDS`：战略项目关键词。
- `DATABASE_URL`：默认 `sqlite:///./data/contribos.db`。
- `APP_TIMEZONE`：每日榜单的日期时区，默认 `Asia/Shanghai`。
- `ANALYSIS_PROVIDER`：`none`、`fake` 或 `nvidia_nim`；NVIDIA 默认使用 `nvidia/nemotron-3.5-lightning-30b-a3b`。
- `IMPLEMENTATION_PROVIDER`：`none`、`fake` 或 `nvidia_nim`；NVIDIA 默认使用 `deepseek-ai/deepseek-v4-pro-0813`。
- `REVIEW_PROVIDER`：`none`、`fake` 或 `nvidia_nim`；NVIDIA 默认使用 `minimaxai/minimax-m3`。
- `NVIDIA_API_KEY`：只允许注入 `contribos model-gateway`，不得注入 API、Provider 或 Sandbox Worker。
- `MODEL_GATEWAY_SIGNING_KEY`：至少 32 字节十六进制，用于签发短期、限次、任务绑定的 Gateway 凭据。
- `SANDBOX_JOB_SPEC_KEY_ID`：JobSpec 签名密钥 ID，默认 `local-v1`。
- `SANDBOX_JOB_SPEC_SIGNING_KEY`：至少 32 字节的十六进制 HMAC 密钥。未设置时执行只创建 `explore/pending`，不会入队沙箱 Job。
- `SANDBOX_STAGE_RUNTIME`：`none`、`fake` 或 `docker`。Docker runtime 只在 `contribos sandbox-worker` 装配，API/Provider 不读取 Docker 配置。
- `WORKBENCH_RUNNER_IMAGE`：规划和自动执行使用的固定 Runner 镜像摘要。
- `PUBLISHER_MODE`：`none`（默认）、`fake`（显式离线演示）或 `gh`（独立真实 Publisher）。
- `GITHUB_ARCHIVE_HOSTS`：允许接收无凭证归档下载的 HTTPS 主机，默认 `codeload.github.com`。GitHub Token 不会发往这些主机。

## 验证

```bash
pytest
python -m compileall -q app
```

测试全部使用 Fake GitHub 数据，不会访问网络。

## 下一阶段

1. 完成原生 Linux amd64 + Docker Engine 恶意套件验收（P5-G06）。
2. 在用户提供的 Linux 环境完成同一 Runner 与恶意套件验收。
3. 为 NVIDIA 分析、编码和 Review 运行受控的真实账户验收并记录预算证据。
4. 在精确用户确认后接通真实 Draft PR，并同步远端 PR 状态。
