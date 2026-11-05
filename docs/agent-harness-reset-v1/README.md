# Agent Harness Reset v1.0

日期：2026-11-04\
目标仓库：`Garden12138/ai-open-source-contribution-os-summary`\
目标分支：`refactor/agent-runtime-v2`

当前状态：Gate0–3通过；Plan12真实验收因三次超量调用批次耗尽两次协议纠偏而终止，
Gate4未通过，Phase5未进入。新增终态Ledger/SQLite协调写锁修复已验证并部署。
当前源码718ed712与真实执行源码59862614分别保留，不冒充同一次验收。
详见[当前交接](handoff.md)、[真实结果](real-acceptance-result.json)及
[验证汇总](verification-report.json)。路线图状态以`docs/tasks.md`为准。

## 本次调整目标

不再优先扩大 UPEVE 环境支持范围，而是先让 Agent 能够从真实 Issue 调查，稳定进入代码修改、测试、修复和独立 Review，避免持续调用工具却没有任务进展。

本方案拆成三个工程：

1. **Agent Harness 重构（P0）**：任务进度账本、工具调用适配、无进展检测、Coding/Verify/Repair 闭环。
2. **Environment Engine 泛化（P1，暂缓扩张）**：保留现有能力，只修复阻断 Harness 最小验收的必要问题。
3. **端到端验收（P0）**：真实模型、真实工具执行、真实源码和真实测试。

## 文档清单

- `01-problem-spec.md`：问题定义、目标和约束。
- `02-target-architecture.md`：目标架构、状态机和进度账本。
- `03-implementation-plan.md`：分阶段实施计划。
- `04-acceptance-matrix.md`：验收标准。
- `05-codex-goal-prompt.md`：Codex `/goal` 主任务提示词。
- `06-cursor-independent-review-prompt.md`：Cursor 独立审查提示词。
- `07-decision-log.md`：架构决策与禁止事项。
- `08-plan11-evidence.md`：Plan11 证据摘要。
- `EXECUTION-CHECKLIST.md`：执行清单。

## 放置方式

在仓库根目录解压 ZIP，文件会放入 `docs/agent-harness-reset-v1/`。

## 推荐执行顺序

1. 阅读 `01`、`02`、`07`。
2. 在 Codex CLI `/goal` 模式中执行 `05-codex-goal-prompt.md`。
3. 每个阶段通过 Gate 后，再进入下一阶段。
4. 使用 Cursor 执行 `06-cursor-independent-review-prompt.md` 做独立审查。
5. 最终必须完成真实模型端到端验收。

## 重要约束

- Mock Provider / Mock Tool 只能用于单元和集成测试，不能冒充真实模型验收。
- 不允许通过无限增加 Token、模型调用次数或工具调用上限掩盖无进展。
- 不允许直接把单轮 Tool Call 上限从 4 改大。
- 不允许绕过写入路径、命令审批、沙箱、预算和证据链。
- 不允许自动发布 PR、合并代码或执行未经批准的第三方写操作。
- “环境准备通过”或“工具调用成功”不等于“任务完成”。
