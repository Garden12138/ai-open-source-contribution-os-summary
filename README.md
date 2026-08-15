# AI Open Source Contribution OS

面向独立开发者的开源机会工作台：发现值得做的 GitHub Issue，先用确定性规则过滤，再用可解释的评分模型生成每日榜单。

当前版本实现了产品闭环的第一个纵向切片：

```text
GitHub Search → 候选去重 → 仓库元数据缓存 → 硬规则过滤
              → 七维评分 → 3/3/2/2 每日榜单 → Web 面板
```

## 已实现

- 单用户、只读 GitHub Issue 发现。
- 默认搜索 Python、TypeScript、`help wanted`、`good first issue` 和 bounty 机会。
- 候选跨查询去重，仓库元数据按 24 小时缓存。
- 排除已归档、无可识别许可证、已分配、描述过短或长期不活跃的任务。
- 按奖励可靠性、接受概率、技术匹配、项目影响力、清晰度、竞争度和学习价值计算 0～100 分。
- 保存每个维度的分数、风险扣分和原因，结果可审计。
- 每日榜单优先组合 3 个赏金、3 个高影响力、2 个技术匹配和 2 个战略机会；不足时按总分补位。
- FastAPI、SQLite、轻量 Web 面板和可供 cron 调用的 CLI。

当前不会修改第三方仓库，不会评论 Issue，也没有真实 GitHub 写权限。独立 Review、发布意图和 Draft PR 可在 Fake 运行时离线走通；真实 Draft PR 仍未启用。

## 库完成 vs 生产可点

| 环节 | 库 / API / UI | 默认 `serve` + `worker` |
| --- | --- | --- |
| GitHub 扫描与 3/3/2/2 榜单 | 已完成 | **可点** |
| AI 深析 | 已完成 | 需 `ANALYSIS_PROVIDER=fake`；真实 Codex 仍未接线 |
| 计划版本、对话、批准 | 已完成 | 可点（需先有 AnalysisVersion） |
| 隔离执行 Explore/Implement/Verify | 引擎与状态机已完成 | 采集归档后，`SANDBOX_STAGE_RUNTIME=fake` 可离线跑通 Explore → ChangeSet → Implement → Verify |
| 从计划生成代码 / ChangeSet | Fake 提议已接线 | 需先 Explore 成功；真实 Implementer Provider 未接线 |
| 独立 Review / 有界修复 | 已完成（Fake） | Verify 成功后可点；`fake_blocking` 用于离线修复回路 |
| 发布意图 / Fake Draft PR | 已完成（Fake） | 需 Review 通过；未确认不会产生记录；真实 `gh` 写未启用 |
| PR 事件 / 生命周期 / 热力图 | 已完成（本地观测） | 看板可点；真实 GitHub 轮询未启用 |

## 快速开始

要求 Python 3.11+。推荐使用 `uv`：

```bash
uv venv
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
| `GET` | `/api/v1/opportunities/{id}` | 完整评分与风险详情 |
| `POST` | `/api/v1/opportunities/{id}/analyses` | 在 Provider 就绪时排队一次分析 Job |
| `GET` | `/api/v1/opportunities/{id}/analyses` | 不可变分析版本历史 |
| `POST` | `/api/v1/tasks` | 从精确 AnalysisVersion 创建贡献任务 |
| `GET` | `/api/v1/tasks/{id}` | 任务、计划、批准与执行摘要 |
| `POST` | `/api/v1/tasks/{id}/plan-versions` | 创建或修订结构化计划 |
| `POST` | `/api/v1/plan-versions/{id}/approve` | 锁定 base SHA 并批准计划 |
| `POST` | `/api/v1/plan-versions/{id}/archives` | 为已批准计划排队只读仓库归档采集 Job |
| `POST` | `/api/v1/plan-versions/{id}/executions` | 以精确审批、base SHA、归档和 Runner digest 幂等创建执行 |
| `POST` | `/api/v1/executions/{id}/change-sets` | 在 Explore 成功后接受 ChangeSet 并排队 Implement |
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

当前评分是透明、确定性的启发式模型，适合承担大模型分析之前的低成本漏斗；它不是对 PR 一定合并或赏金一定兑现的承诺。

## 配置

常用环境变量见 [.env.example](.env.example)：

- `GITHUB_TOKEN`：GitHub 只读 Token，也兼容 `GH_TOKEN`。
- `GITHUB_QUERIES`：以分号分隔的 GitHub Search 查询。
- `PREFERRED_LANGUAGES`：技术偏好，默认 `Python;TypeScript`。
- `STRATEGIC_KEYWORDS`：战略项目关键词。
- `DATABASE_URL`：默认 `sqlite:///./data/contribos.db`。
- `APP_TIMEZONE`：每日榜单的日期时区，默认 `Asia/Shanghai`。
- `ANALYSIS_PROVIDER`：`none`（默认，规则榜单回退）或 `fake`（离线分析）。`codex` 尚未接线。
- `SANDBOX_JOB_SPEC_KEY_ID`：JobSpec 签名密钥 ID，默认 `local-v1`。
- `SANDBOX_JOB_SPEC_SIGNING_KEY`：至少 32 字节的十六进制 HMAC 密钥。未设置时执行只创建 `explore/pending`，不会入队沙箱 Job。
- `SANDBOX_STAGE_RUNTIME`：`none`（默认）或 `fake`（离线 Fake Explore/Implement/Verify，不解压仓库）。Docker runtime 仍只属于 Sandbox Worker。
- `GITHUB_ARCHIVE_HOSTS`：允许接收无凭证归档下载的 HTTPS 主机，默认 `codeload.github.com`。GitHub Token 不会发往这些主机。

## 验证

```bash
pytest
python -m compileall -q app
```

测试全部使用 Fake GitHub 数据，不会访问网络。

## 下一阶段

1. 将 Fake 阶段 runtime 换成真实 Docker Explore/Implement/Verify（API 进程仍不得持有 Docker socket）。
2. 真实 Implementer Provider：从锁定计划提议 ChangeSet，仍须哈希绑定与用户接受。
3. 原生 Linux amd64 + Docker Engine 恶意套件验收（P5-G06）。
4. 独立 Reviewer、Diff 与风险报告。
5. 仅在用户确认后创建 Draft PR，并同步 PR 状态。
