# ContribOS v2

本地运行的开源贡献副驾驶：发现 GitHub Issue，在对话中调度现成编码 Agent，确认后发布 Draft PR。

当前完成 M0：Python 后端骨架、SQLite 迁移、本地登录与基础自检。
Agent、沙箱、任务对话与桌面外壳按实施计划后续接入。

## 快速开始

需要 Python 3.11+；`git`、`gh` 用于自检与后续 GitHub 集成。

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
contribos doctor
contribos serve
```

打开终端打印的一次性登录链接（60 秒有效，只能使用一次）。
默认服务地址为 `http://127.0.0.1:8765`，仅允许回环地址。
`curl http://127.0.0.1:8765/api/health` 可检查服务是否正常。

数据默认写入 `~/.contribos/`，可通过 `CONTRIBOS_HOME` 覆盖。
本地访问令牌位于该目录的 `local-token`（0600）；CLI 客户端用 `Authorization: Bearer` 传入。
浏览器 API 使用 HttpOnly 会话 Cookie；变更请求须同源，并用 `X-CSRF-Token` 回传 `contribos_csrf` Cookie。
本地令牌请求可不带浏览器 Origin/CSRF；如果携带 Origin，则同样校验。

## 开发

```sh
pytest -q
python -m compileall -q contribos
node --test contribos/web/static/tests
```

默认测试使用临时数据，不需要网络或真实凭据。

## 文档

- [产品规格](docs/redesign/02-product-spec.md)
- [2026-11-25 桌面试用反馈与 Hive 任务对话](docs/redesign/feedback-2026-11-25.md)
- [技术架构与架构图](docs/redesign/03-architecture.md#2-总体架构)
- [实施计划与进度](docs/redesign/04-implementation-plan.md#6-进度清单)
- [Agent 指南](AGENTS.md)
- [旧版位置](docs/legacy.md)
