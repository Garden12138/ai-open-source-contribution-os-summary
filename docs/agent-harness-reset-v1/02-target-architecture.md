# 02. Target Architecture

2026-11-05实施补充：[只读批次调度与恢复](read-batch-scheduler-v1/design-and-recovery.md)
定义当前用户授权的窄范围例外：最多8个已验证`read_file`，拆为最多4-call的持久化子批次。
本页后续“禁止分批/采用B”保留2026-11-04历史决策，不覆盖新的显式用户授权。
其他工具、混合批次和旧审批保持原边界；原始执行子批次上限仍为4。

## 1. 总体架构

```text
GitHub Issue / Task
        |
        v
Task Intake & Scope
        |
        v
Baseline Admission
        |
        v
Deterministic Orchestrator
  |       |        |
  v       v        v
Progress Budget  Checkpoint
Ledger   Policy
  |       |        |
  +-------+--------+
          |
          v
      Model Adapter
          |
          v
    Decision Validator
          |
          v
      Tool Scheduler
          |
    +-----+------+------+
    |            |      |
    v            v      v
 Read Tools  Write Tools Verify Tools
    |            |      |
    +------------+------+
                 |
                 v
       Evidence / Tool Receipts
                 |
                 v
        Progress Evaluator
          |       |       |
          v       v       v
       Continue Replan Block/Pause
                 |
                 v
       Verify / Repair / Review
                 |
                 v
          Human Acceptance
```

## 2. 核心组件

### Task Orchestrator
唯一负责阶段状态迁移的组件。模型不能自行决定越过 Baseline、Approval、Verify 或 Review 门禁。

### Implementation Progress Ledger
持久化任务步骤、状态、证据引用、源码 Hash、尝试次数、阻塞原因和下一步动作。优先复用现有 Journal、Checkpoint 和事件机制，避免另建不一致的平行状态库。

实际结构由`app/agent_v2/progress.py`的严格schema定义；账本内容寻址，
Journal记录连续revision、前一进度hash及当前Checkpoint hash。简例：

```json
{
  "version": "implementation-progress-v1",
  "revision": 1,
  "phase": "investigating",
  "source_hash": "workspace-hash",
  "steps": [{
    "step_id": "investigate",
    "status": "in_progress",
    "evidence_refs": [],
    "source_hash": "workspace-hash",
    "attempts": 1,
    "blocker": null,
    "next_action": "inspect-result-matching"
  }],
  "no_progress_streak": 0
}
```

模型不能单方面把步骤标记为 completed；平台必须验证证据和完成条件。
完整账本包含固定六步：baseline、plan、investigate、implement、verify、review。
新的源码写入使Verify/Review失效；只有验证通过的独立ReviewRun能使账本进入ready_for_human。

### Model Adapter
负责 Provider 差异，不负责业务阶段控制。必须区分 Provider 原始响应、原始工具调用、平台标准 Decision、可执行批次、Provider 失败和协议失败。保留 Tool Call ID、参数、顺序、结束原因和 Usage。

### Tool Scheduler
负责将已验证调用变成 Job。保证调用幂等、结果关联唯一调用、批次拆分不改变语义，且只读、写入、验证采用不同策略。

## 3. 任务状态机

| 阶段 | 允许行为 | 离开条件 |
|---|---|---|
| `intake` | 读取 Issue、仓库、分支 | 任务上下文冻结 |
| `baseline` | 环境准备、修改前检查 | Baseline 通过 |
| `investigating` | 搜索、读取、分析 | 关键问题有证据 |
| `planning` | 形成步骤、影响范围和验证计划 | 计划完整 |
| `awaiting_approval` | 等待用户批准 | 用户批准 |
| `implementing` | 在批准范围内修改 | 步骤完成或阻塞 |
| `verifying` | 执行批准命令 | 当前源码 Hash 有完整结果 |
| `repairing` | 根据失败证据有限修复 | 再次 Verify 或达到上限 |
| `reviewing` | 独立 Review | Review 结果持久化 |
| `ready_for_human` | 展示交付摘要 | 用户验收 |
| `blocked` / `cancelled` | 停止自动执行 | 终态或明确恢复条件 |

## 4. 无进展检测

### 精确重复指纹

至少包含：

- tool name；
- normalized arguments hash；
- source hash；
- expected file hash（如适用）；
- task phase；
- previous result status。

不能只按文件路径判断重复。

### 合法重读

- 同路径、同 offset、同 max_bytes、同源码 Hash，且已有有效成功回执：精确重复候选。
- 不同 offset：合法分页。
- 源码 Hash 变化：允许重读。
- 查询范围变化或证据失效：重新判断，不能直接复用。

### 初始无进展策略

1. 连续 2 个模型回合没有有意义进展：发送一次明确纠偏反馈。
2. 纠偏后仍无进展：暂停或阻塞并保留证据。
3. 不允许无进展循环耗尽全部预算。
4. 阈值可配置并有测试，不得散落魔法数字。

有意义进展包括新增唯一证据、完成计划步骤、产生 Patch、执行 Verify 或形成有证据支持的新结论。

### 精确重复只读调用

优先复用已有有效回执或证据引用，不重新执行底层读取。仍需记录重复请求及处理结果。证据失效或 Hash 不匹配时不得复用。

## 5. Tool Calling 批次策略

Plan11两次5-call的原始调用信息为`unavailable`；用户已解除历史缺口的Phase0阻塞。
无法证明历史批次全为只读，因此本轮固定采用策略B，禁止启用策略A。
未来原始调用信封在解析和拒绝前独立提交，保留ID/名称/原始参数字符串/顺序、
finish reason与Usage；拒绝事件通过同Job及响应hash关联。凭据脱敏后整批拒绝，
私有推理不保存。信封超出剩余输出字节预算时留显式unavailable摘要并停止，不扩额度。

策略记录：

**A. 只读调用确定性分批（本轮禁用）**

仅当批次全部为幂等只读工具时，拆分成不超过 4 个调用的子批次。保留原始 ID 和顺序，子批次持久化，不重新请求模型生成已返回的调用。

**B. 整批拒绝并有限纠偏（已实现）**

无法证明安全时，整批拒绝并明确反馈最大数量、实际数量、零执行事实、剩余预算和下一步要求。
最大4-call、协议纠偏最多2次，原始超量批次执行数为0。

写入、验证和终止调用不得混入自动只读拆分。写入批次不得未经授权部分执行。最终策略必须由测试证明。

## 6. Coding / Verify / Repair

- Coding：用户批准后才允许 Patch；Patch 绑定精确文件 Hash；成功后更新源码 Hash 和进度账本。
- Verify：只运行批准命令；结果绑定当前源码 Hash；保存 stdout、stderr、退出码、环境和源码 Hash。
- Repair：必须引用具体失败证据；修复产生新源码 Hash；受预算和次数限制。
- Review：使用真实 Issue、计划、Diff 和 Verify 证据；不能只复述 Coding Agent 自我总结。

## 7. 可交付条件

必须同时满足：

1. 计划和写入范围已批准；
2. 有真实源码修改；
3. 修改均在授权范围；
4. 当前源码 Hash 对应的批准验证全部通过；
5. 独立 Review 完成且无未解决阻断；
6. 证据链和预算完整；
7. 没有未完成工具 Job；
8. 没有未处理安全或环境阻断。

模型调用 `finish` 不能替代这些条件。
