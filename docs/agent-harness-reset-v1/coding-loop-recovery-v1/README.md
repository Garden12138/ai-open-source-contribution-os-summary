# Coding Loop Recovery v1：离线交付，等待代码审查

Plan15 的失败轨迹已还原，最小本地修复和离线验收完成。本轮真实模型调用 **0**，未创建 Plan16、恢复 Plan15、增加预算、部署、Commit、Push、PR 或外部评论。旧审批、历史费用和未知占用保持原值。真实自主 Coding 成功仍未证明。

- [逐轮诊断与根因](diagnosis.md)：事实、代码根因和待验证假设分开；[完整逐轮参数/上下文索引](plan15-trace.json) 覆盖 15 个 Prompt、15 个响应证据信封及 161 个已校验 Artifact。
- [设计与输入/输出风险](design.md)：独立推进度量、6/8 轮纠偏/停止、精确 Task Brief/源码工作集、原始证据恢复及 reasoning 候选方案。
- [最小源码和测试 Diff](code.diff)：6 个生产文件（含 1 个新增模块）；实现层没有数据库 schema 变化，没有依赖增加。
- [验收报告](validation-report.json)、[测试证据索引](test-report-index.json)、[保存信封重放](saved-envelope-replay.json)、[真实 OCI 容量证据](oci-capacity.json)。
- [业务状态只读兼容性](compatibility-live.json)、[本地源码清单](source-manifest.json)、[交付扫描](delivery-check.json)。

## 核心结论

旧进度把“新回执/新 Artifact 页面”当作推进，没有检查源码实际进入下轮请求，也没有独立限制长时间不应用 Patch。工具唯一读到 70855 字节，真实请求唯一送达只有 10624 字节；40 次调查不意味着 40 次有效 Coding 推进。第 14 轮混合 6 调用整批拒绝是契约正确执行；第 15 轮 Provider 报告 4096 completion tokens 全为 reasoning，耗尽共享输出后安全停止。

新指标从已有 Journal、回执和响应确认的 Prompt 精确重算。预览、仅有 Hash 或成功读取都不冒充送达。第 6 个未应用 Patch 回合后纠偏，第 8 个后停止，第 9 次模型调用不会发出；模型可报告具体阻塞，不要求无依据 Patch。旧重复读取警告阈值、崩溃恢复、原始事件身份和计费规则保留。

投影保留精确冻结验收条件及完整原始 Artifact，优先最多四个最近已读文件的精确片段，所有批次回执仍按原顺序可恢复。最终方案同时保留重复读取警告后，Plan15 第 7、13 轮的诊断投影均从原 1024 增至 **3072** 源码字节，输入预留分别 31423、31158，仍低于 32000。第 13 轮比较只是离线诊断；新守卫在固定保存轨迹上早已停止，不能据此预测真实模型行为。

## 验收结果与限制

| 项目 | 最终结果 |
| --- | --- |
| 默认确定性全量 | 2135 项：2056 passed / 79 skipped / 0 failed；两个互不重叠分片覆盖完整 collection |
| 警告兼容专项 | 36 passed；开发时发现的 1 项失败修复后重跑，原失败报告保留 |
| OCI 套件 | 26 passed，其中 24 项实际 OCI、2 项确定性 unit；macOS arm64 / OrbStack |
| 保存 Plan12/13/14 | 5 个信封：4+2、4+1、4+1、4+4、4+4；原 ID、状态、完整回执及计费守恒保留 |
| 替身 Coding/Verify/Review | 源码证据驱动真实 OCI Patch/readback；确定性工作流通过最新源码 Verify、独立 Review 门禁并停于人工验收 |
| 截断/恢复/预算 | 保存的 Plan15 length 响应重放保留真实 usage 与失败预留，不重试、不执行工具；旧恢复与预算回归通过 |
| 编译/语法/格式/泄漏 | compileall app、JavaScript syntax、diff check、凭据/已配置 secret 及编码变体扫描通过 |
| 业务状态 | 原 45 张表所有行与 Plan15 终态快照相同；106 次历史模型启动、13 个审批、11 个旧检查点不变；active jobs=0 |
| 未验证 | 真实自主 Coding 效果、真实最新源码 Verify/Review、Linux amd64；不能用替身/arm64 OCI 填平这些门禁 |

本地候选包含 255 个生产文件，集合 Hash：
`3af23ee0f476c2d80b75cffad33cafb85479cbd7daa9e9cc7d93d3ec9400a18f`。
线上五角色仍是原 254 文件 / `31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95`；本轮用户限定本地/离线范围，因此未执行 AGENTS.md 的通常自动重部署步骤，也没有宣称候选已在线运行。

模型仍是冻结 deepseek-v4-pro / reasoning_effort=high，Profile Hash `bb3522b03a444b544c808217ec5ed4b4fa71253591ee8c09e3fc77c57d3a6049`；单次 Coding 输出 4096、输入上界 32000，以及 24 模型/64 读取/24 写入/6 检查/2 协议纠偏/2 Provider 重试/2 修复、输入 200000、输出 32000、证据 500000 字节、1800 秒均不增加。独立 Review 和沙箱各自原上限也不变，完整字段见验收报告。

## 下一轮建议

**NO-GO：当前不能进入付费验收。** 先进行本次代码审查；后续若获单独指示，必须重新核验 Issue/base，并冻结新源码、app/worker 镜像、`native-coding-loop-recovery-v10` 协议、独立 Plan/Binding 和精确预览，再取得一次性明确授权。原 Plan15 审批不能用于新源码或协议。本轮没有创建这些对象。

high + 4096 的截断风险仍在；本轮不改冻结配置。low + 4096 是可用确定性 transport 验证的候选，不能宣称真实效果；改变 reasoning 配置会改变模型配置/身份 Hash，增加单次输出则改变调用契约和预留，两者都需要新的冻结和授权。HR4/HR5、P5-G06 仍开放。

复算入口：从仓库根目录以 `PYTHONPATH=.` 运行 `analyze_plan15.py`、`replay_saved_envelopes.py`；最终全量分片为 `run_regression.py 0` 和 `run_regression.py 1`。这些入口不连接真实 Provider。`check_compatibility.py` 仅对原部署执行 query_only 读取，`finalize.py` 仅导出本地验证/扫描结果。
