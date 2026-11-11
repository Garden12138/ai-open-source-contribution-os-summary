# 本轮产品验收报告

日期：2026-11-11（时间为 UTC；本机时区 Asia/Shanghai）。本轮不调用真实付费模型、不恢复 Plan16、不创建付费 Plan，不重启既有 Gateway/Compose。历史 52/2151/184/12 不是本轮 PASS 的来源。

## 环境与总体结果

macOS arm64，Python 3.12.11，Node 22.23.2；FastAPI 0.141.1、httpx 0.28.1、SQLAlchemy 2.0.54、uvicorn 0.53.0、cryptography 46.0.7、packaging 26.3、pytest 8.4.2、h2 4.4.1、requests 2.34.2。依赖区间保持 pyproject，未新增运行依赖。

初次完整回归有两项失败：一项为执行沙箱禁止 loopback 绑定；另一项为本轮清理遗漏了测试辅助模块间接读取的 Plan14 Fixture。前者仅放行自有本机端口重跑，后者从私有备份恢复完整原字节/哈希后重跑相关测试。没有通过改断言、删测试、伪造结果或运行真实模型消除失败。下表不能直接相加（有重复用例）。同一源码按最终逐用例核对：**2158 通过、0 未解决失败、83 跳过**。

| 测试运行 | 通过 | 初次失败 | 跳过 | 秒 |
| --- | --- | --- | --- | --- |
| default-full | 2149 | 2 | 83 | 1582.863 |
| new-demo-tests | 7 | 0 | 0 | 1.229 |
| focused | 111 | 1 | 1 | 62.274 |
| loopback-recheck | 1 | 0 | 0 | 2.951 |
| fixture-dependency-recheck | 67 | 0 | 0 | 60.092 |

原始失败名称、开始时间、时长和 JUnit 原始哈希见机器结果。原始 JUnit/日志仅私有归档，不提交真实用户路径。现有生产代码未改；恢复 Fixture 没有修改测试断言，也没有恢复旧 Plan 执行；重跑不会把初次失败 XML 改成 PASS。最终合并来自完整首跑与针对性重跑，未再重复整套回归。默认测试中的 canary 安全回归仍执行；跳过的 OCI/真实 Provider 等不算通过。

另有真实 Chromium **10 项通过**，临时服务/持久化/授权检查 **13 项通过**。无 Token 的真实 GitHub 只读 scan **PASS**：1 候选、1 合格、1 入选，0 模型调用。244 模块导入、compileall、7 个 ES 模块语法、3 个 shell 语法、依赖一致性（32 包）、Wheel 构建及 diff --check 全部通过。Wheel 包含 262 项、11 静态资源与 0046 迁移，不含数据库/.env。

## 实际命令与证据

完整命令、运行时间、模块计数和原始结果哈希见 [acceptance-results.json](maintenance/acceptance-results.json)。主要命令：

```bash
.venv/bin/python -m pytest --junitxml=/private/tmp/contribos-maintenance-baseline.xml
.venv/bin/python -m pytest tests/test_local_demo.py --junitxml=/private/tmp/contribos-local-demo.xml
.venv/bin/python -m pytest tests/test_gateway_storage.py tests/test_provider_gateway.py tests/test_execution_pipeline.py tests/test_migrations.py tests/test_model_settings.py tests/test_local_demo.py --junitxml=/private/tmp/contribos-focused.xml
.venv/bin/python -m pytest tests/test_gateway_storage.py::test_real_gateway_process_restart_retains_credential_and_readiness
.venv/bin/python -m pytest tests/test_read_capacity.py tests/test_coding_loop_recovery.py tests/test_environment_engine_resolution.py --junitxml=/private/tmp/contribos-fixture-recheck.xml
.venv/bin/python -m compileall -q app
uv pip check --python .venv/bin/python
uv build --wheel --offline --out-dir /private/tmp/contribos-maintenance-wheel
git diff --check
./scripts/local-start.sh --data-dir /private/tmp/contribos-product-acceptance-20261111 --port 18767
./scripts/local-smoke.sh --data-dir /private/tmp/contribos-product-acceptance-20261111
./scripts/local-stop.sh --data-dir /private/tmp/contribos-product-acceptance-20261111
./scripts/local-start.sh --data-dir /private/tmp/contribos-product-acceptance-20261111 --port 18767
# 已配置本机 Playwright 模块与 Chromium 路径后：
node tests/local_demo_browser.mjs
```

浏览器实际使用已有 Playwright 与缓存 Chromium，通过 PLAYWRIGHT_MODULE/PLAYWRIGHT_CHROMIUM_EXECUTABLE 指定；公开文档不写真实用户绝对路径。重现方法见 [本地手册](LOCAL_SETUP.md)。真实 discovery 使用独立临时 DB、三个 none Provider、PUBLISHER_MODE/SANDBOX_STAGE_RUNTIME=none、无 Token、重试 0、最多 3 候选：`contribos scan --query 'repo:pallets/flask is:issue is:open no:assignee' --top 3`。

## 业务验收矩阵

| 功能 / 输入 | 预期 | 实际 | 状态 / 证据 |
| --- | --- | --- | --- |
| 数据库初始化/迁移 | 非破坏升级，合法引用 | 全部前向到 0046；integrity/foreign_key 检查正常；迁移测试 9 通过 | PASS / focused, service |
| 真实 Issue 发现：固定 Flask 查询、最多 3 条 | 只读、持久 scan、可解释筛选 | 1/1/1 入库，无凭据、0 模型 | PASS / real_readonly_discovery |
| 合成发现/推荐 | 去重、评分、展示 | Web 和 HTTP 扫描可用 | PASS / browser, offline_e2e |
| Fake 分析：精确 snapshot | 新分析版本，不覆盖规则分 | 分析完成、ID 可追溯 | PASS / offline_e2e |
| 创建任务/Plan：预设结构化内容 | 绑定分析、版本与批准 | 保存 task/plan/approval，冻结合成 base | PASS / offline_e2e；非自主规划 |
| Coding：Fake ChangeSet | 保留精确差异哈希 | 预设 Patch、Implement 成功 | PASS（模拟）/ offline_e2e |
| Verify：Fake 阶段 | 形成规范化测试 Artifact | Verify 成功，共 3 阶段 manifest | PASS（模拟）；未执行仓库测试 |
| Review：Fake reviewer | 绑定精确执行与 Artifact | verdict=pass；默认契约覆盖 block/修复 | PASS（模拟）/ focused, offline_e2e |
| Web 任务结果 | 用户可打开差异/测试/Review | 真浏览器打开历史尝试与审查通过结果 | PASS / browser |
| 历史与重启 | 保留精确 task/review/Artifact | 重启后 ID 与清单一致 | PASS / service |
| 模型设置 | 读取；无 Gateway 时禁真实凭据 | 设置页面与 GET 可用，credential management=false | PASS（读取）；真实保存 BLOCKED |
| 未授权/跨域/缺 CSRF | 401/403，不产生 mutation | 按预期拒绝 | PASS / service, local_demo tests |
| Job 幂等/取消/重试/stale | 合法状态变迁、失败关闭 | 现有默认回归与 API/执行契约验证 | PASS / per_module |
| PR 流程 | 真实写需独立确认；Fake 契约保持 | 默认契约执行，试用 Publisher=none | PASS（契约）；真实发布 NOT_RUN |
| 真实 AI 规划/Coding/Review | 真实模型+隔离仓库实测 | 本轮禁止付费调用 | NOT_RUN；不可冒充自主 Coding 成功 |

## 可重复的离线端到端

两套独立数据库分别完成同一流程：默认 .local-demo（18765）与临时目录（18767）。SyntheticGitHub、FakeProvider、预设 Plan/ChangeSet、Fake Explore/Implement/Verify/Review 经真实持久 Job 和 HTTP 执行。新的任务使用独立 ID，没有操作旧业务 Plan、预算或授权。

临时运行：task `52e4ae87-f09b-4459-be74-fbf83985ea5e`；execution `82901229-9fbb-46ef-991c-0bb2d3d29373`；review `d36d5d78-89e8-4ffc-8a70-bfad195da902`。程序输出可追溯 ID、change_set_hash、三阶段 manifest 和结果地址。真实浏览器验证发现、详情、候选、设置、历史、结果以及 1440/390 宽度，无页面异常或外站请求。

**这不是 AI 自主 Coding 成功，也不是实际仓库测试通过或独立 AI Review 成功。** 合成文件不会作为不可信项目在宿主执行。真实 GitHub scan 的成功也不推导真实 AI 流程成功。

## 安全与隐私

精确本机敏感配置值扫描、真实用户路径扫描、意外数据库/日志/压缩包/JUnit 扫描通过。试用访问/签名 canary 未进入数据库、Artifact 或日志。.env 与原 data 文件逐字节未变。Gateway/其他持久服务未停机、未部署、未修改凭据。首次独立启动出现并发建表竞争，已在轻量脚本中修为先迁移再启动 API/Worker；失败试用库保留私有副本，未删除业务数据。

## 阻断与未运行

- 真实模型规划、Coding、Review：NOT_RUN，本轮禁止付费调用。
- 真实 Gateway 事故恢复：BLOCKED，未证明配对材料，原服务未操作。
- 真实 OCI：本轮 NOT_RUN；Linux amd64/Docker：BLOCKED，未获得原生平台证据。
- 真实 PR/CLA/ready：NOT_RUN，试用关闭发布，无业务对外写入。
- 真实模型密钥保存/切换：BLOCKED，未配置新的独立试用 Gateway；读取/契约已测。
- 容器 xcli Host Bridge：BLOCKED，未实现。

共 7 个外部验收组（4 BLOCKED、3 NOT_RUN），不会阻断本地 Fake 的上述可用功能。Phase 5、HR4/5、P5-G06 和 Plan16 状态保持原样。

## 推荐试用与待反馈

按 [使用手册](USER_GUIDE.md) 先运行 smoke，查看评分、报告、方案、差异/测试/Review，再重启查看历史；随后可用独立真实只读实例扫描 GitHub。需要用户反馈：评分是否符合个人目标、候选/任务导航是否清楚、失败原因和模拟标记是否易懂。真实模型/贡献发布留作用户另行配置后的迭代，不再扩展 Harness。

机器结果 SHA256：`6379b80ea1cc7fb47867941c62608cf13d5f59a332f2c8289bf6040cd50555c6`。原始运行材料仅保存在 [私有维护归档](maintenance/HISTORY_INDEX.md)；公开结果不含 traceback 与私有路径。
