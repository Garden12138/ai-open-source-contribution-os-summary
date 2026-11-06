# Coding Loop Recovery v1 本地 Git 交付

用户在离线修复完成后单独授权本地 Commit，以供独立代码审查；未授权 Push、部署、PR、Plan16、恢复 Plan15、扩容或真实模型调用。源码与测试、审计文档分别提交，使用逐文件路径暂存，未禁用 Git hooks。

源码/测试 Commit：`b404a92326ca01efa7e64ab91ff9831b548c6562`，父 Commit：`5177157909e87cb540d3bb52f06f0521d09e8ac4`。
审计文档 Commit 是包含本说明及 [Git 交付核验清单](git-delivery-check.json) 的后继 Commit，其 SHA 在最终交付消息中报告。

## 文件范围

源码与测试共 12 个文件：

| 类型 | 文件 |
| --- | --- |
| 生产源码 | app/agent_v2/coding_context.py |
| 生产源码 | app/agent_v2/loop.py |
| 生产源码 | app/agent_v2/native_tools.py |
| 生产源码 | app/agent_v2/orchestrator.py |
| 生产源码 | app/agent_v2/read_progress.py |
| 原生 UI | app/static/workbench.js |
| 测试 | tests/test_agent_native_tools.py |
| 测试 | tests/test_coding_loop_recovery.py |
| 测试 | tests/test_coding_loop_recovery_oci.py |
| 测试 | tests/test_harness_coding_journey.py |
| 测试 | tests/test_harness_progress_boundaries.py |
| 测试 | tests/test_native_read_batches.py |

文档仅包含 `docs/tasks.md` 和本目录的明确文件清单，排除缓存和编译产物。所有路径及文件摘要见 git-delivery-check.json。未纳入数据库副本、凭据文件、未投影的私有 Provider 响应或敏感运行日志。保存信封中的响应仅含工具、状态和用量的严格投影；源字节页是可核验的目标仓库源码证据。

## 验证与证据时点

- 提交前从工作区重新生成的完整源码/测试 patch 与 [code.diff](code.diff) 逐字节相同，反向补丁检查通过；12 个暂存 blob 再次与已核验文件逐字节比对。
- 255 个生产文件集合 Hash 仍为 `3af23ee0f476c2d80b75cffad33cafb85479cbd7daa9e9cc7d93d3ec9400a18f`。
- 本次重新执行 compileall app、JavaScript 语法检查、工作区及暂存区 diff check、已配置凭据/编码变体和凭据格式扫描；结构化数据检查不含凭据字段。7 处既有路线图英文说明经核对属于词法误报，原文未改，运行时敏感信息守卫未放宽。
- 复核既有冻结报告：全量 2056 passed / 79 skipped / 0 failed；26 项 OCI 套件包含 24 项实际 OCI、2 项 unit。JUnit 数量、结果、文件 Hash、容量证明、兼容性报告和 15 轮真实轨迹索引均重新核对。本次 Git 整理没有重跑全量或 OCI，也没有运行真实模型。
- `README.md`、`source-manifest.json`、`delivery-check.json` 和原始审计保留离线验收时点及 Hash。它们记录的旧 git_HEAD、尚未 Commit 等状态属于当时事实；后续本地 Git 授权与结果由本说明单独记录，不回写历史字段。

真实自主 Coding、真实最新源码 Verify/独立 Review 和 Linux amd64 仍未验证，付费验收建议仍为 NO-GO。交付结束后等待人工 Push 和代码审查，不自动继续执行。
