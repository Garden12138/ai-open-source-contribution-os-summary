# Codex `/goal` 主任务提示词：Agent Harness Reset v1

你负责在当前仓库实施 Agent Harness Reset v1。

仓库：`Garden12138/ai-open-source-contribution-os-summary`\
目标分支：`refactor/agent-runtime-v2`

目标：让一个通过 Baseline 的真实开源 Issue 能够完成 Investigation → Plan → Coding → Verify → Repair（必要时）→ Independent Review。重点解决反复读取源码、无法进入 Coding、最终耗尽预算的问题。

本任务按 `/goal` 长任务方式持续推进。按阶段实施、验证、记录和继续，不要每个小步骤都等待用户指令。但遇到需要用户批准的外部写入、权限扩大、模型切换或预算扩大时必须停止并请求批准。

## 一、先阅读文档

完整阅读：

- `docs/agent-harness-reset-v1/README.md`
- `01-problem-spec.md`
- `02-target-architecture.md`
- `03-implementation-plan.md`
- `04-acceptance-matrix.md`
- `07-decision-log.md`
- `08-plan11-evidence.md`

如果文档不存在，先报告，不要假设已安装。

## 二、不可违反的约束

1. 先检查 `git status`、当前分支、HEAD 和未提交修改。
2. 不得覆盖、清理或重置用户已有修改。
3. 不得切换分支、Push、创建或合并 PR。
4. 不得删除或改写历史验收证据。
5. Mock Provider / Fake Tool 不能冒充真实模型验收。
6. 不得无限增加 Token、模型调用数、工具调用上限或超时。
7. 不得直接把单轮 4 个 Tool Calls 上限改大来掩盖协议问题。
8. 不得绕过 `write_paths`、Patch Hash、命令审批、沙箱、预算、Journal 和安全检查。
9. 不得自动扩大写入范围或执行未经批准的第三方写操作。
10. 不得擅自切换 Provider 或模型，优先沿用 Plan11 现有模型绑定。
11. 真实模型调用遵守当前预算，预算不足就停止，不得自行提高。
12. 环境准备成功、工具调用成功或模型返回 `finish` 都不等于任务完成。

## 三、Phase 0：只读诊断，禁止先改核心代码

先完成：

- 还原可恢复的Plan11证据；两次5-call的原始调用信息按用户决定标记unavailable，
  不推测参数，不再以该历史缺口阻塞其他工作。
- 还原 39 次 `read_file` 的 path、offset、max_bytes、源码 Hash、回执及后续 Prompt 引用。
- 解释同一文件反复从 offset 0 读取的原因。
- 检查读取结果是否完整进入后续 Prompt。
- 检查 `required_next_step`、Prompt 投影、Progress/Checkpoint 和 Tool Description。
- 检查为什么没有使用 `search_code`、`apply_patch` 和 `run_approved_checks`。
- 追踪 Provider、Native Tools、Loop、Orchestrator、Journal、Budget、Workbench、Tool Registry 的实际调用链。
- 确认两次 5-call 批次是否全部只读，不能自行假设。
  已无法证明，因此本轮保留4-call整批拒绝与有限纠偏，禁止自动分批。
- 区分已证实根因、高可信假设和缺少证据的问题。

输出 `docs/agent-harness-reset-v1/diagnosis.md`，使用“现象 / 证据 / 对应代码 / 根因判断 / 置信度”表格。

Phase 0 未完成前，不得修改 Agent 核心 Loop。
Gate0已按2026-11-04用户决定及ADR-008通过；继续当前未完成阶段，不重复诊断。

## 四、Phase 1：先写回归测试

复现并覆盖：

- 精确重复读取；
- 合法分页读取；
- 源码 Hash 变化后的重读；
- 连续无进展；
- 纠偏后仍无进展；
- Tool Call ID 与 Tool Result 关联；
- 5-call 只读批次；
- 混合写入超量批次；
- Checkpoint 重启恢复；
- Verify Hash 绑定。

尽可能先证明旧实现会失败，再实现修复。不能只补理想输入的测试。
未来原生响应必须在解析/拒绝前持久化原始调用ID、名称、参数、顺序、finish reason及Usage，
并关联拒绝原因；凭据字段脱敏后安全拒绝，私有推理不保存，不突破输出预算。

## 五、Phase 2：Progress Ledger 与 No-Progress Guard

实现持久化 Implementation Progress Ledger：

- 记录阶段、步骤、证据引用、源码 Hash、尝试次数、阻塞原因和下一步。
- 更新有 revision 和 Journal 记录。
- 平台验证证据后才能把步骤标记完成。
- 精确重复只读请求不得重复执行底层操作。
- 不同 offset、不同源码 Hash 的合法读取不得误判重复。
- 连续无进展先纠偏一次，仍无进展则暂停/阻塞。
- 无进展不得一直消耗到预算耗尽。
- 重启后能恢复，不重复执行已完成副作用。

优先复用现有 Journal、Checkpoint 和事件机制，不要另建无法保持一致性的平行状态库。

## 六、Phase 3：Provider 与 Tool Batch Adapter

根据 Phase 0 证据选择安全策略：

- 评估纯只读调用的确定性分批；
- 或保留整批拒绝并改善有限纠偏。

如果实现分批：
- 只允许幂等只读工具；
- 保留原始 Tool Call ID 和顺序；
- 每个子批次持久化；
- 不重新请求模型生成已返回的调用；
- 写入、验证、终止调用不得混入只读拆分。

无法证明安全就不要分批。写入批次不能未经授权部分执行。

## 七、Phase 4：完整 Coding Loop

实现以下闭环：

1. Investigation：形成有证据的发现。
2. Planning：形成可执行步骤和写入路径。
3. Approval：用户批准后才能写入。
4. Coding：产生真实 Patch。
5. Verify：执行批准命令，绑定最新源码 Hash。
6. Repair：根据真实失败证据有限修复。
7. Independent Review：基于真实 Diff 和 Verify 结果审查。
8. Human Acceptance：提供证据，等待用户验收。

Prompt 和工具描述应明确各工具用途、返回证据和下一步约束，避免模型反复读取已掌握文件。

## 八、Phase 5：真实模型端到端验收

优先使用：

`https://github.com/patoles/agent-flow/issues/107`

Plan11 基线 Commit：

`3489e17c48985b74fdb42cde122376b1eb12d8a7`

执行前重新确认 Issue 状态和 Commit。若 Issue 已解决或代码变化，冻结新基线并说明，不能沿用旧结论冒充复现。

必须使用现有真实 Provider 和模型绑定，真实读取源码、真实生成 Patch、真实运行批准测试、真实保存 Usage 和证据、真实执行独立 Review。

Mock 只能用于单元/集成测试，不能替代这一步。

## 九、每个 Phase 的报告

每阶段完成后记录：

1. 目标；
2. 修改文件；
3. 关键实现；
4. 测试命令和结果；
5. 未通过项；
6. 风险；
7. 下一阶段准入结论。

阶段未通过时先修复本阶段，不得跳过 Gate。

## 十、停止条件

以下情况停止并报告：

- 工作区有无法安全处理的用户修改；
- 当前执行缺少影响安全追踪的关键证据（已接受的Plan11历史unavailable不在此列）；
- 需要切换模型、Provider 或扩大预算；
- 需要扩大写入范围或执行第三方写入；
- 需要修改未批准的安全边界；
- Issue 已变化且没有可复现等价任务；
- 数据迁移可能破坏旧 Journal/Checkpoint；
- 真实验收被外部服务或环境阻断。

## 十一、最终成功定义

只有同时满足以下条件才能报告 `passed`：

- Plan11 关键问题有回归测试；
- Progress Ledger 和无进展保护通过；
- Tool Calling 批次处理通过安全测试；
- 真实任务产生源码 Patch；
- 当前源码 Hash 对应的批准 Verify 通过；
- 独立 Review 通过；
- 预算、工具调用和证据链完整；
- 没有未解决 P0 阻断。

否则只能报告 `partial`、`blocked` 或 `failed`，并附证据。

按docs/tasks.md及已验证阶段报告继续当前任务。不得因已接受的历史缺口重启Phase0；
真实执行仍需新工具协议绑定的精确人工批准，不重置旧用量或自动分配新预算。
