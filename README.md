# AI Open Source Contribution OS

单用户、本地优先的开源贡献工作台：发现 GitHub Issue，筛选机会，保存分析、方案、执行、测试与 Review 记录。

**当前成熟度：可本地试用；真实 AI 自主贡献全链路尚未完成产品验收。** 默认试用使用合成数据和 Fake Provider，无需 Token、模型额度或 Docker。Fake Patch、Verify 和 Review 只演示业务链路。

## 核心功能

- GitHub 只读发现、硬过滤、七维评分、每日 3/3/2/2 推荐。
- 贡献偏好、候选管理、比较、提醒、扫描与任务历史。
- 不可变分析版本、方案批准、执行及内容寻址 Artifact。
- 原生 Web、FastAPI、SQLite、持久 Job 与 contribos CLI。
- 已有模型设置、隔离 Coding/Verify、Review 和精确确认 PR 工作流实现；真实使用依赖独立 Gateway、Worker、镜像和凭据，部分验收仍阻断。

## 快速启动

要求 Python 3.11+；本轮验证 macOS arm64 / Python 3.12.11。已有可用 .venv 时跳过环境创建和安装。

```bash
cd ai-open-source-contribution-os-summary
uv venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
./scripts/local-start.sh
./scripts/local-smoke.sh
open http://127.0.0.1:18765
```

没有 uv 时：`python3 -m venv .venv`，再 `.venv/bin/python -m pip install -e '.[dev]'`。

“发现机会”显示合成 Issue；“贡献概览”及侧栏“最近任务”显示 smoke 创建的方案、执行和审查。终端输出精确结果 API 地址，详细记录保存在 `.local-demo/last-smoke.json`。

```bash
./scripts/local-stop.sh
```

停止保留数据，重新 start 即可继续。脚本固定监听 127.0.0.1，不加载 .env，不重启已有服务。端口冲突可指定 `--port 18766`；三个脚本均支持 `--data-dir /absolute/private/path`。

## 架构

```text
原生 HTML/CSS/JS → FastAPI → SQLite + Artifact + 持久 Job
                              ├─ Discovery Worker（GitHub 只读）
                              ├─ Provider Worker → 独立 Model Gateway
                              ├─ Sandbox Worker → Docker / OrbStack
                              └─ 独立 Publisher（精确确认后才允许外部写入）
```

离线试用只启动 API 和 Fake Worker，不执行外来仓库代码、不发布 PR。

## 文档

- [本地部署](docs/LOCAL_SETUP.md) · [产品使用](docs/USER_GUIDE.md)
- [实际升级对比](docs/UPGRADE_DIFF.md) · [本轮验收](docs/ACCEPTANCE_REPORT.md)
- [清理报告](docs/maintenance/CLEANUP_REPORT.md) · [分支报告](docs/maintenance/BRANCH_CLEANUP_REPORT.md) · [历史索引](docs/maintenance/HISTORY_INDEX.md)
- [路线图](docs/tasks.md) · [迁移恢复](docs/database-migrations.md) · [真实模型配置参考](docs/nvidia-provider.md)

## 已知限制

路线图仍在 Phase 5，版本元数据仍为 0.1.0，不表示稳定发布。陌生 Issue 的真实自主 Coding、Linux 原生 Docker、真实 Gateway 恢复和账号发布验收未被 Fake 测试替代。Runtime v2 和环境引擎默认关闭。离线实例不提供真实 AI 对话规划、凭据保存或发布。Plan16 仍 blocked 且授权已消费。

```bash
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q app
git diff --check
```
