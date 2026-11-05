# 07. Architecture Decision Log

## ADR-001：先重构 Agent Harness，不继续扩大 UPEVE
本轮 P0 聚焦 Agent 任务推进能力。Plan11 环境准备和修改前 Baseline 通过，但 Agent 仍然大量读取而没有 Patch；继续增加环境适配不能解决核心失败。

## ADR-002：不立即更换 Agent 框架
保留 Runtime v2，先补足确定性 Orchestrator、Progress Ledger 和无进展控制。当前证据不能证明框架本身达到能力上限。

## ADR-003：不直接扩大单轮 Tool Call 上限
原始历史调用已按ADR-008记为unavailable；本轮保留整批拒绝与有限纠偏，禁止自动分批。
放宽数量不能证明调用身份、幂等性和写入原子性正确。

## ADR-004：进度必须由证据证明
模型不能单方面声明步骤完成；平台必须校验真实工具、Patch、Verify 和 Review 证据。

## ADR-005：精确重复读取不应反复执行
相同参数、相同源码版本且已有有效成功回执的只读调用，优先复用证据。不同 offset、不同源码 Hash 或失效证据不能误判为重复。

## ADR-006：真实模型端到端验收是最终 Gate
Mock 只用于确定性测试。最终必须用真实模型、真实仓库和真实测试。

## ADR-007：环境引擎暂缓扩张
保留现有 UPEVE，只有其问题阻断 Harness 最小真实任务时才做最小修复。

## ADR-008：历史调用 unavailable 不阻塞；未来先留证据再拒绝（2026-11-04用户决定）

Plan11两次超量响应的原始调用信息无法恢复，明确记录缺失字段及影响，不虚构历史。
Gate0接受已有诊断，后续继续。未证明全只读，因此保留4-call整批拒绝与有限纠偏，
本轮禁止自动分批。未来原生响应在解析前提交安全工具信封Artifact与Journal事件，
保留原始ID/名称/参数/顺序/finish reason/Usage，以响应hash关联拒绝原因和执行回执。
敏感内容只保留脱敏占位与安全拒绝，凭据及私有推理不持久化；不扩大任何预算或权限。

## ADR-009：仅验证后的有界原生read_file批次可调度（2026-11-05用户决定）

Plan12原始6/5/5批次均已证明为read_file，
允许新协议对完整验证的纯read_file、最多8-call原始批次进行持久化4-call子批次调度。
整批参数/来源/权限/预算与OCI路径/hash/offset预检必须先通过；中途失败保留真实前缀，
未知结果单列并阻断。不适用于其他工具、混合批次、旧终态或旧审批；不扩大资源和权限。
来源与实现见[事实校验](read-batch-scheduler-v1/facts-and-baseline.json)及
[调度与恢复](read-batch-scheduler-v1/design-and-recovery.md)。

## 明确禁止（除ADR-009明确的只读例外，保留原约束）

- 只在 Prompt 加一句“不要重复读取”，没有平台级检测。
- 只增加模型预算、上下文或超时。
- 只把 `max_length=4` 改大。
- 把所有工具调用都当作可安全重试。
- 无审计地部分执行写入批次。
- 用模型自我总结替代 Patch、Verify 和 Review 证据。
- 把单个 Issue 成功写成通用能力已完成。
