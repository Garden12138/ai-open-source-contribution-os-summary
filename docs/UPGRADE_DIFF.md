# 升级对比与可用性

日期：2026-11-11。结论来自实际代码、迁移、路由、测试与本地实例，非复制设计文档。

## 主要基线

远程默认分支为 origin/main，SHA `7dbdf5f82daa3009a555af7b3b136754e666c210`。已用 git ls-remote 核验远程 HEAD。仓库没有 Tag、release 分支或正式发布引用，因此无第二个可验证稳定 Tag。“稳定基线”指默认主分支快照，不代表全部产品门禁通过。

初始本地 main 为 27802bb，落后一个文档提交；开发 HEAD 为 884d015。merge-base 为 27802bb，origin/main...884d015 独有提交为 1/109。main 已有发现、规划、Coding、Review、PR 等大部分实现，不能将它们全部算作本分支新增。当前交付保留版本元数据 0.1.0 与 Phase 5 开放状态。

## 代码结构差异

清理前提交对比 origin/main → 884d015：2,491 文件变化、约 106 万新增行，主要为过程证据。仅 app 为 111 文件、9,170 新增行、271 删除行；生产文件共 257。当前版本保留有效源码，移出过程噪声，并新增独立本地试用入口。

| 范围 | 实际变化及位置 |
| --- | --- |
| 新模块 | app/agent_v2：Journal、工具回合、读取批次/容量/进度；app/environment_engine：Python/Node 环境契约、解析和准入 |
| 规划/恢复 | planner.py、planning_continuation.py、planning_delivery.py、planning_read_reuse.py、context_pressure.py、node_ci.py：读复用、续接、失败与用量记录 |
| 新服务能力 | providers/gateway_storage.py、gateway_readiness.py：显式持久代、CAS/lease、备份恢复、认证只读 readiness；github_read_proxy.py：通过 Publisher 复用只读能力 |
| API | 主要业务路由已在 main；新增/扩展内部模型工具接口、Publisher GitHub read proxy、Gateway storage/credential readiness，以及工作台恢复和状态反馈 |
| 数据库 | 0038–0046 增量迁移：v2 Journal CAS、环境契约、DeepSeek Review、native 工具/响应证据、实现进度、读批次、源文件观察与容量计量。以既有 workbench_events/Artifact 的约束与触发器为主，未替换 SQLite |
| Worker | 原有 API/Provider/Sandbox/Publisher 职责保留；新增 v2 model/tool 协调和环境解析服务，未合并信任域 |
| 前端 | 仍为原生 HTML/CSS/JS；工具进度、恢复反馈、模型预设调整。未引入前端框架 |
| 部署 | 原 Compose/容器资源保留；Gateway 改为显式存储身份，新增 LaunchAgent 渲染器。真实既有实例未自动迁移 |
| 依赖 | 相对 main 新增 packaging>=24,<27；本轮未增加运行依赖 |
| 删除 | 无有效生产模块删除；清理旧部署/执行辅助脚本、重复快照、原始 JUnit、缓存与临时探针 |
| 本轮产品入口 | scripts/local.py 与 local-start/stop/smoke.sh：独立数据库、Fake Worker、loopback、真实 HTTP 结果链路、进程归属检查和离线页面提示 |

重现对比：

```bash
git diff --stat origin/main 884d015 -- app tests scripts containers pyproject.toml
git diff --name-status origin/main HEAD -- app
git diff origin/main HEAD -- app/migrations/versions pyproject.toml
```

## 功能对照

READY=可按手册直接体验；PARTIAL=部分实现/验收；BLOCKED=缺前提；EXPERIMENTAL=实验实现；PLANNED=无实现。表中 Fake 成功仅说明应用链路，不能替代真实模型/OCI/远程写验收。

| 功能 | main 是否已有 / 当前增量 | 当前实现与状态 | 主要代码（app/） | 使用与依赖 | 验证 / 立即体验 |
| --- | --- | --- | --- | --- | --- |
| Issue 发现与推荐 | 有；新增只读代理等 | READY：过滤、七维评分、3/3/2/2、偏好候选 | service.py, scoring.py, github.py, product_experience.py | Web 扫描/CLI scan；真实数据需 GitHub 网络 | 默认测试、HTTP、浏览器；另有一次无 Token 的真实 GitHub scan 成功（1 候选/1 合格/1 入选） |
| 分析与价值评估 | 有；上下文/模型增强 | READY（Fake）；真实 PARTIAL | providers/workflow.py, fake.py, analysis_documents.py | 详情/AI 筛选；真实需 Gateway/Provider | Fake 报告、版本和契约；可直接看 |
| 任务、方案与批准 | 有；冻结/续接增强 | READY：结构化 API；对话 PARTIAL | planning.py, plans.py, approvals.py, planner.py | smoke 或真实任务对话 | smoke 预设方案，默认规划测试；可体验结构化链路 |
| Agent 自主规划 | 有；v2 与恢复增强 | PARTIAL：陌生 Issue 闭环未验收 | agent_runtime.py, planner.py, agent_v2 | 对话需模型、Sandbox 读取 | 脚本响应测试；离线不提供真实对话规划 |
| 源码读取/理解 | 有；读批次、去重、容量和来源跟踪 | PARTIAL：工具实现；理解质量未验收 | sandbox_worker/agent_tools.py, planning_context.py, agent_v2/read_* | 专用 Sandbox；真实需 Docker/OrbStack | 默认契约；本轮未运行真实仓库 OCI |
| Agent Coding/Patch | 有；精确工具循环 | PARTIAL；v2 EXPERIMENTAL，默认关闭 | coding.py, changesets.py, agent_v2/loop.py | 精确批准后执行；需模型和镜像 | Fake 预设 Patch 链路；非 AI Coding 成功 |
| Verify/自动测试 | 有；环境契约和诊断增强 | READY（模拟流程）；真实 PARTIAL | execution_worker.py, sandbox_worker/verification.py, environment_engine | 执行详情；真实仓库测试必须隔离 | 默认测试、Fake Verify；OCI/Linux 未验收 |
| 独立 Review | 有；DeepSeek/恢复增强 | READY（Fake）；真实 PARTIAL | reviews.py, review_provider.py | 查看 Review；真实需独立 Provider | Fake pass/block 和阻断契约；可查看样例 |
| PR 准备/发布 | 有；兼容/隔离修复 | PARTIAL；本地试用 BLOCKED（关闭） | publish_intents.py, publisher_service.py, post_publication.py | 独立 Publisher、专用账号、精确确认 | 默认 Fake/契约；无真实业务发布 |
| 任务/执行历史 | 有；持久回合/工具证据 | READY | dashboard.py, workbench_view.py, models.py | 贡献概览、最近任务、结果 API | 重启、持久化、HTTP；可直接用 |
| 模型配置/切换 | 有；预设/readiness 调整 | READY：配置查看；真实保存 BLOCKED | model_settings*.py, providers/studio.py, static/model-settings.js | 设置→模型设置；需独立 Gateway | 页面和默认契约；不调用真实连接 |
| Web 工作台 | 有；工具/恢复 UI 增强 | READY：发现、候选、历史、设置 | static/, workbench_api.py | 本地 URL | 真浏览器/API/语法检查 |
| API/CLI | 有；新增 Gateway storage CLI | READY：serve/worker/scan/API | api.py, cli.py | /docs、contribos --help、三个试用脚本 | 真实 HTTP、健康、Wheel 检查 |
| Gateway repair-v1 | 无；本分支新增 | PARTIAL：源码保留；真实恢复 BLOCKED | providers/gateway_storage.py, gateway_readiness.py | 独立正式运维流程 | 合成存储/恢复测试；未操作真实 Gateway |
| xcli 容器 Host Bridge | main 已明确缺失 | PLANNED：未实现 | providers/xcli_local.py 显式拒绝容器 | 原生已注册端点另有实现 | 不作为首次体验 |

## 架构与用户实际获得的新能力

核心仍为原生 Web → FastAPI → SQLite/Artifact/持久 Job，Provider、Sandbox、Publisher 分离。v2 在既有事件模型上增加持久工具回合与精确编辑；环境引擎将运行要求/失败结构化。两者保留默认关闭与未通过门禁。

本轮把已有成果变成可启动、可停止、可重复生成样例和查看结果的产品入口，减少交付噪声，明确真实和模拟边界。没有扩展新 Harness、付费 Plan 或预算。

## 限制及推荐顺序

先体验离线 smoke：发现评分→分析→方案→差异/测试/Review→重启后历史。其次用独立实例体验真实 GitHub 只读扫描。真实模型、自主 Coding、Gateway 恢复、Linux 原生 Docker 和真实账号发布需要后续配置/验收。Plan16 blocked/consumed 与 Phase 5 开放门禁不变。精确测试结果见 [验收报告](maintenance/HISTORY_INDEX.md)。
