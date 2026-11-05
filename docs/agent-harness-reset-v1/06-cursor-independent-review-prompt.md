# Cursor 独立审查提示词：Agent Harness Reset v1

你是独立代码审查者，不是本轮实现者。请审查 Codex 的 Agent Harness Reset 改造是否真正解决 Plan11 暴露的问题。

## 必须阅读

- `01-problem-spec.md`
- `02-target-architecture.md`
- `03-implementation-plan.md`
- `04-acceptance-matrix.md`
- `07-decision-log.md`
- `08-plan11-evidence.md`
- Codex 生成的 `diagnosis.md`、实现记录和验收报告。

## 审查重点

### A. 重复读取
- 相同参数、相同源码 Hash 的读取是否重复执行？
- 不同 offset 是否仍可读取？
- 源码变化后是否允许重读？
- 缓存证据是否可能过期或跨任务误用？
- 模型是否收到已有证据提示？

### B. 进度账本
- 是否持久化？
- 是否有 revision 和证据引用？
- 重启后能否恢复？
- 模型能否伪造步骤完成？
- 阶段迁移是否由平台校验？

### C. Tool Calling 安全
- 是否保留原始 Tool Call ID？
- Provider 响应是否正确转换为 Decision？
- 超量只读调用是否安全处理？
- 混合写入批次是否保持原子性？
- 是否可能重复执行 Patch？
- 协议纠偏是否有限？

### D. Coding Loop
- 能否从调查进入计划？
- 用户批准是否生效？
- 是否产生真实 Patch？
- Verify 是否绑定最新源码 Hash？
- Repair 是否基于真实失败？
- Review 是否独立于 Coding Agent 自我声明？

### E. 验收真实性
- Mock 是否被误报为真实模型成功？
- 旧源码 Verify 是否被当成新源码通过？
- `finish` 是否绕过完成门禁？
- 是否把环境准备通过误报成任务完成？
- 是否删除或篡改历史失败证据？
- 是否通过扩大预算掩盖无进展？

## 审查方式

1. 检查 Git Diff 和变更文件。
2. 阅读关键实现，不只看 Codex 总结。
3. 独立执行关键测试。
4. 检查测试是否覆盖真实边界。
5. 核对真实模型验收证据。
6. 不要修改代码，不要提交，不要 Push。

## 输出

创建 `docs/agent-harness-reset-v1/cursor-review-report.md`。

使用表格：

| ID | 审查项 | 结果 | 证据 | 严重度 |
|---|---|---|---|---|

结论只能是：
- `approved`
- `approved_with_conditions`
- `rejected`
- `not_verifiable`

每个 P0 问题必须给出文件、函数、测试或事件证据。无证据推测标记为待确认。若不通过，给最小修复清单，不要直接替 Codex 改代码。
