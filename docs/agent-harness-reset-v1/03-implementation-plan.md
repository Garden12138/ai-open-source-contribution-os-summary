# 03. Implementation Plan

采用“先诊断、再最小改造、最后真实验收”。按下述经用户调整的Gate0保留历史缺口，
复用已完成诊断继续实现，不伪造或推测缺失的Provider调用。

## Phase 0 — 只读诊断

工作内容：

1. 检查分支、HEAD、工作区和未提交修改。
2. 追踪 Provider、Native Tool、Loop、Orchestrator、Journal、Budget、Workbench 和 Tool Registry。
3. 尝试还原两次5-call原始响应；已确认不可恢复的ID、名称、参数和顺序记为`unavailable`，保留可核验计数、Usage、拒绝原因和零执行事实。
4. 还原 39 次 `read_file` 的 path、offset、max_bytes、源码 Hash、回执和后续 Prompt 引用。
5. 检查 `required_next_step`、Prompt 投影和工具描述。
6. 解释为何没有使用 `search_code`、`apply_patch`、`run_approved_checks`。
7. 区分已证实根因、高可信假设和缺少证据的问题。

交付：`diagnosis.md`、事件时间线、代码调用链、根因证据表。

**Gate 0（2026-11-04用户调整）：** 可恢复轨迹、代码链路与根因置信度完成后通过。
Plan11两次原始调用信息记为`unavailable`，不再阻塞Phase0或其他工作。
不据此推断只读；本轮继续整批拒绝，禁止自动分批。历史证据不改写。

## Phase 1 — 回归测试与可观测性

为重复读取、分页读取、源码变化后重读、5-call 只读批次、混合写入批次、Tool Call ID、无进展纠偏、Checkpoint 恢复建立测试。

建立一次模型请求到所有工具结果的 trace。
未来原生Provider响应必须在校验/拒绝前持久化安全工具信封，包含原始调用ID、名称、
参数与顺序、finish reason、Usage；拒绝原因通过同Job/响应hash关联。
凭据不保存，私有推理不保存；安全拒绝留脱敏证据，不能执行工具。

**Gate 1：** 先能稳定复现旧问题，再实施修复。

## Phase 2 — Progress Ledger 与 No-Progress Guard

- 持久化任务阶段、步骤、证据、源码 Hash、尝试次数和下一步。
- 更新具有 revision 和 Journal 记录。
- 平台验证后才能标记步骤完成。
- 精确重复只读调用不重复执行。
- 不同 offset 或不同源码 Hash 的合法读取不误判。
- 无进展先纠偏，仍无进展则暂停。
- 重启后恢复，不重复副作用。

**Gate 2：** 通过重复读取、分页、源码变化、暂停和恢复测试。

## Phase 3 — Provider / Tool Batch Adapter

根据 Phase 0 证据选择安全策略。保留原始 Tool Call ID、顺序、参数和 Usage。只读分批必须证明幂等；混合写入不得部分执行；协议纠偏必须有限。

**Gate 3：** 无调用丢失、无重复执行、无未经授权写入、无无限重试。

## Phase 4 — Investigation → Plan → Coding → Verify → Repair

形成带证据的调查结论和可验收计划；用户批准后才能写入；Patch 后更新进度；Verify 绑定最新源码 Hash；Repair 引用失败证据；独立 Review 使用真实 Diff 和测试结果。

**Gate 4：** 至少一个真实任务产生 Patch，并完成当前源码 Verify。

## Phase 5 — 真实模型端到端验收

优先使用 `patoles/agent-flow#107`。基线 Commit：`3489e17c48985b74fdb42cde122376b1eb12d8a7`。

执行前重新确认 Issue 和 Commit。如果上游变化或 Issue 已解决，冻结新基线并说明。

使用现有真实模型绑定，记录真实请求、Usage、工具调用、回执、Patch、Verify 和 Review。Mock 不能代替本阶段。

**Gate 5：** 真实 Patch + 当前源码 Verify + 独立 Review 全部通过。

## Phase 6 — 文档与交接

交付架构说明、迁移说明、事件版本、运维恢复、完整验收报告和未解决问题。

## 开发纪律

- 每阶段独立测试和审查，不得一次性大规模重写。
- 不删除历史证据。
- 不扩大预算或权限来掩盖失败。
- 数据结构变更必须兼容旧 Journal/Checkpoint。
- 真实模型测试前检查现有配置和预算。
- 未经批准不 Push、不创建 PR、不执行第三方写入。
