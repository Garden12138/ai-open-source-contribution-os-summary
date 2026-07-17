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

当前不会修改第三方仓库，不会评论 Issue，也没有 GitHub 写权限。AI 深度分析、方案版本、Docker 执行、独立 Review 和 Draft PR 属于后续阶段。

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

直接运行一次扫描：

```bash
contribos scan
```

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
| `POST` | `/api/v1/scans` | 立即执行一次发现扫描 |
| `GET` | `/api/v1/scans` | 扫描历史 |
| `GET` | `/api/v1/opportunities/daily` | 当日精选榜单 |
| `GET` | `/api/v1/opportunities/{id}` | 完整评分与风险详情 |

FastAPI 交互文档位于 <http://127.0.0.1:8000/docs>。

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

## 验证

```bash
pytest
python -m compileall -q app
```

测试全部使用 Fake GitHub 数据，不会访问网络。

## 下一阶段

1. Issue 详情的结构化 AI 深度分析与评分解释校准。
2. 多轮方案讨论、版本管理、Hash 锁定和审计日志。
3. 不可信代码的 Docker 隔离执行，以及 Explore / Implement / Verify 状态机。
4. 独立 Reviewer、Diff 与风险报告。
5. 仅在用户确认后创建 Draft PR，并同步 PR 状态。
