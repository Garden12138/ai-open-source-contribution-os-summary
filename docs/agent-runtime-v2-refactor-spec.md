# AI Open Source Contribution OS — Agent Runtime v2 架构重构与完整任务闭环实施规格

> **文档状态**：Approved for implementation by this user request（实施提案；验收状态以实际证据为准）  
> **版本**：v1.0 · **编制日期**：2026-10-27  
> **目标仓库**：https://github.com/Garden12138/ai-open-source-contribution-os-summary  
> **目标目录**：`docs/agent-runtime-v2-refactor-spec.md`  
> **目标开发分支**：`refactor/agent-runtime-v2`（**禁止在 main 上实施**）  
> **阶段定位**：在现有 Phase 5 内开展跨模块可靠性重构与专项验收；不要把 Phase 6—10 自动标成已完成。  
> **源代码与证据基准**：实施时读取本地及 `origin/main` 的最新代码；本规格参考截至 2026-10-27 的公开仓库和 `docs/conversations-and-incidents-2026-10-27.md`，不假定部署态与历史快照相同。

## 0. 本规格的优先级、边界与完工定义

这是**必须落地的产品工程重构**，不是继续做故障分析、只输出 ADR、只调 Prompt 或仅增加测试数量。本任务的终点是：用户在 Contribution OS 原生对话里，针对被明确判定为受支持的陌生 GitHub Issue，能够无需外部 Codex 补丁完成「调查 → 计划 → 人工批准 → 沙箱编码 → 真实验证 → 独立 Review → 人工验收 → Draft PR 发布预览」；真实外部发布仍需单独精确授权。若真实 GitHub 测试账号可用，再完成只面向测试仓库的 Draft PR 验收。

**约束层级**：遵守仓库 `AGENTS.md`、`docs/tasks.md` 及本规格。用户本次明确授权**新建并切换指定开发分支、在该分支进行重构与本地阶段性提交**；不授权推送远端、不授权创建线上 PR、不授权修改真实外部仓库，也不授权自动替用户确认产品内的发布、CLA 或 ready-for-review。若 `docs/tasks.md` 的现有阶段锁与本专项任务冲突，新增明确的 Phase 5 子任务与门槛并记录原因，**不要跳过现有 Phase 5 门禁**。

**最重要的否定条件**：

- 无权用 Fake 模型、预先注入正确修改内容、手工维护脚本或外部 Codex 修业务任务来证明真实自主交付。
- 无权降低沙箱限制、将不可信仓库代码移到宿主机运行、放宽密钥隔离、略过审批或伪造通过的测试。
- 无权清空数据库、覆盖历史事件、重写批准后的 Plan、抹去失败记录或以扩大模型额度掩盖架构问题。
- **目标不是让 Agent 无限制循环**。独立完成意味着在明确的支持范围、权限与预算内自动推进，并在真实阻塞时准确停止、明确指出原因；不是凭空保证每个上游 Issue 都能成功。

## 1. 历史证据及根因假设

先阅读以下已有资料，只读核对事实；不要将完整超大附录无差别塞给模型：

1. `AGENTS.md`、`README.md`、`docs/tasks.md`。
2. `docs/conversations-and-incidents-2026-10-27.md`：优先阅读主报告与 B08（oh-my-pi）、B09（A2UI），再按需读附录 C09；完整文件很大，应按段检索。
3. `docs/autonomous-completion-audit-2026-10-24.md`、`docs/workflow-architecture-review.md`、`docs/agent-runtime.md`。
4. `docs/conversation-agent-contract.md`、`docs/conversation-agent-redesign.md`、`docs/conversation-workflow-reliability.md`。
5. `docs/project-environment-resolution.md`、`docs/sandbox-dependencies.md`、`docs/execution-evidence.md`、`docs/execution-state.md`。
6. 现有 `docs/acceptance/` 中相应真实/控制样例的 JSON 证据。

**历史规模与边界**（2026-10-27 快照，不能当成现版本统计）：9 个任务、2,241 条事件、46 个计划版本、48 次执行尝试、30 次独立审查、4 份发布记录；已记录的完整、无外部维护辅助的贡献交付证据为 0。历史测试 1,452 passed / 46 skipped 只能说明对应测试套件在当时通过，不能证明任意真实任务的完整旅程。

高优先级根因假设必须通过代码路径与重现实验逐项确认或推翻：

| ID | 假设 | 典型代码/证据 | 需证明什么 |
|---|---|---|---|
| RC-01 | 对话模型主要在选择工作流动作，缺少可以持续观察并调用工具的 Coding Harness | `app/conversation_protocol.py`、`app/conversation_reasoning.py` | 对话读取/调用/恢复与实际工具行为之间是否存在割裂 |
| RC-02 | 编码阶段让模型一次性生成复杂 ChangeSet，而不是在可审计沙箱内按反馈迭代修改 | `app/coding.py`、`app/sandbox_worker/implementation.py` | oh-my-pi 的重复路径、edit+append、精确锚点冲突是否系统性增加失败 |
| RC-03 | 对话、规划、基线、环境、编码和发布分散协调，失败恢复依赖特例 | `app/agent_runtime.py`、`app/conversation.py`、`app/planner.py`、`app/contribution_workflow.py` | 每种阻塞能否得到唯一责任主体、恢复动作与终态回执 |
| RC-04 | 输出 Schema 与服务端隐式互斥规则不一致 | `app/planner.py` | A2UI `read_context` + `read_paths` 为什么导致整轮失败 |
| RC-05 | 环境能力预判不等于真实可执行；工具链不匹配把小修改变成大型恢复工作 | `app/execution_environment.py`、`app/sandbox_worker/` | 对支持/未知/不支持环境是否早于执行授权明确判断 |
| RC-06 | 命令退出码零被误认为验证成功 | `app/verification_commands.py`、Verify Artifact | Bun 只打印 help 的情形是否能 fail closed |
| RC-07 | 预设模型答复的契约测试冒充自主闭环 | `tests/test_conversation_workflow_contract.py`、`tests/test_agent_live_acceptance.py` | 真模型、真实工具与陌生任务是否被完整测量 |

**既有正面资产**必须保留：内容寻址 Artifact、不可变事件哈希链、CAS/幂等、PlanVersion 精确审批、隔离 Sandbox Worker、独立 Review、独立 Publisher、GitHub 只读发现及原生 Studio。禁止为了清爽而把这些重写成未经审计的单体 Agent。

## 2. 目标架构：控制平面与执行平面分离

```text
用户 / Studio / 对话 UI
       │ 用户目标、追问、人工确认
       ▼
Conversation Facade（只负责理解意图、回答当前事实、投递任务请求）
       │
       ▼
Task Orchestrator v2  ←→ 持久 Task Journal / State / Checkpoint
  ├─ 任务阶段状态机 / 任务锁 / Lease / 去重 / 取消 / 超时
  ├─ Evidence / Context Manager（可分页读取、事实与历史分开）
  ├─ Capability & Preflight（仓库技术栈、基线可验证性）
  ├─ Recovery Policy（平台/环境/协议/代码/预算/上游变动）
  └─ Approval Gate（版本、基准 SHA、路径与命令范围精确绑定）
       │                        │
       │ 阶段任务                └── UI 当前状态 / 工具时间线 / 待确认动作
       ▼
Agent Runtime（可插拔模型及 Harness；回合内 tool use）
  ├─ Research/Plan Agent（只读源码和 Issue 证据）
  ├─ Coding Agent（按工具结果增量实现与测试修复）
  └─ Reviewer（与编码会话/模型策略隔离，引用真实 Artifact）
       │ 仅可调用授权 Tool Registry
       ▼
Sandbox Tool Gateway（强校验输入、冻结 workspace、原子产物、资源预算）
  ├─ list/search/read 源码
  ├─ apply_patch（范围、基准与 Hash 检查）
  ├─ run_approved_checks（沙箱中的实际测试命令）
  ├─ diff / inspect_failure / read_artifact
  └─ immutable receipts + tool-call idempotency
       │
       ▼
隔离 Worker / 无凭据工作区 / 有界依赖准备 / 无网络 Verify

独立 Publisher（完全不同信任域；仅凭用户一次性精确确认执行 Fork/Push/Draft）
```

### 2.1 五条硬性原则

1. **真正的 Agent Loop 在阶段内发生**：模型可以反复调用被授权的工具，观察后继续；系统拥有阶段迁移和写操作许可，不由对话模型自由越权迁移。
2. **任务事实只有一个权威来源**：继续使用现有 SQLite/Artifact/Journal，Agent 的聊天记忆仅辅助推理，不代表完成事实，也不产生审批。
3. **文件编辑交给工具，不让模型模拟文件编辑事务**：增量 Patch 优先、完整文件写入按需，精确检查由 Sandbox Tool Gateway 做；保留原 ChangeSet 兼容读取与历史回放。
4. **工具返回结果是强类型证据**：每条工具调用有状态、source hash、scope、实际产物、执行证明和有限错误码；“说测试通过了”没有任何效力。
5. **任何支持范围之外的任务都必须尽早诚实阻断**：不能让模型靠重复修改 Prompt/方案、重置预算或请求用户安装依赖来绕过能力缺口。

### 2.2 框架选型是第一阶段的工程决策，不是提前指定包名

先对现有运行时增强、LangGraph、OpenAI Agents SDK，以及 Codex SDK/app-server 作为独立 Coding Harness 进行短平快验证；比较标准必须包括：

- Python 3.11+、SQLite 持久化兼容、跨进程恢复与历史回放；
- 工具调用、事件流、模型/Provider 切换能力，以及 structured output 不可靠时的可恢复性；
- 中断、人工确认、租约失效、成本追踪、多回合预算；
- 对现有四个凭据/信任域的适配成本，绝不将 Agent 进程直接赋予 Docker socket 或 Publisher 权限；
- 已有仓库 Native Studio 和迁移成本；
- 是否真的减少 RC-01—RC-07 的故障面，以及长期维护负担。

生成 `docs/adr/agent-runtime-v2-engine-selection.md`（若 `docs/adr/` 不存在可新建），包含候选对照、针对 RC-01 与 RC-02 的最小 Spike、技术决定、弃选理由与回退方案。**默认倾向最少新依赖 + 现有 Task Journal 作为持久权威 + 新的 Tool Registry/Agent Loop**；若选通用 SDK，也不能产生与现有任务权威冲突的双重状态源。禁止“换一个大框架、其余不改”即宣称完成。

参考资料（须核对使用时最新版本及接口）：
- LangGraph durable execution：https://docs.langchain.com/oss/python/langgraph/durable-execution
- OpenAI Agents SDK：https://openai.github.io/openai-agents-python/
- Codex SDK：https://github.com/openai/codex/tree/main/sdk

## 3. 功能契约（Functional Requirements）

### FR-01 对话 Facade：像 Coding Agent 一样交谈，但不充当操作证据

- 一个对话输入框支持：提出新任务、按当前状态继续、解释源码、查看/比较计划、修改目标、暂停/停止、询问最新进度/测试/Review/PR 状态。
- 当前任务、对话引用焦点、原始请求、历史事实、当前授权是**不同数据对象**；问一句“完成了吗”不得触发新的编码或取消后台执行。
- 用户发送“继续”时不要求模型猜工作流阶段：先由 Task Orchestrator 根据当前状态、未决步骤和上一笔失败凭据确定安全后继；缺精确审批时只展示确认动作，不擅自批准。
- 模型必须能回答已经持久化的结果；无需重新生成已有方案、Diff、PR 文本或完成的测试。
- 回合内澄清问题、源码读取和中间说明可共存；不得要求模型在正常非危险的只读动作之间满足隐藏互斥条件。
- 当用户请求执行与只读解释混合时，可同步解释当前事实并将经过授权的工作入队；后台模型调用不能阻塞独立的进度问答。
- UI 维持 Native HTML/CSS/JS 与现有 API 的兼容，展示：正在调用的工具、任务阶段、上下文/预算、真实失败原因、可采取的合法下一步。

### FR-02 可恢复 Agent Loop：Observe → Tool → Feedback → Decide

- 新增 `AgentSession/AgentTurn/ToolCall/ToolResult/AgentCheckpoint` 或等价版本化持久结构，必须与已有 Task/ExecutionAttempt/Job/Artifact 哈希绑定。
- 最小循环：获取当前状态 → 模型决定工具或回答 → 平台校验工具 → 沙箱执行 → 结构化 ToolResult → 再次给模型 → 结束/等待审批/阻塞。
- 任务在任何模型调用、工具返回或 Worker 崩溃后恢复时，不得重复不可幂等的操作；每个工具有 `call_id` / `idempotency_key` / 执行状态 / 输入哈希。
- 维护者话语和模型语言**没有**批准权限，继续保留服务器端精确审批与一次性凭据。
- Provider 可能返回 native tool call、结构化动作 JSON 或普通文本。设计可插拔 `AgentModelAdapter`，只支持明确检测过的能力，拒绝模糊猜测；纯文本模型可降级到计划/解释，不能伪装为经验证的自动编辑能力。
- 长上下文必须有源绑定的分页读取、压缩索引与 checkpoint；工具返回过大时提供可继续读取的引用，不吞尾部，也不把任意截断文本当完整证据。
- 限制模型调用、读取、写入、测试、时间、Token、工具输出与资源成本；**分别计账**，允许用户配置合理上限，但不允许失败后隐式清零。

### FR-03 源码调查与计划

- 不依赖先安装项目依赖就可读取 GitHub 源码归档与 CONTRIBUTING、CI、锁文件、源文件。
- Agent 使用 `list_paths`、`search_code`、`read_file`、`read_context` 等只读工具，多轮查证后提出可编辑的 Plan。
- 计划的最小可信内容：Issue 目标/验收标准、证据引用、基准 SHA、允许修改路径/范围、主要步骤、验证命令和 working directory、环境能力预判、潜在维护者约束、不确定项。
- PlanVersion 与 Approval 仍为不可变版本；计划变化、上游基准改变、可执行范围改变时，需要新版本及新的精确确认。
- **A2UI 专项回归**：一个模型回合同时产生两种合法只读需求时，协调器必须拆分为顺序读取或明确可恢复反馈；不能仅清空并耗尽修正次数导致 0 个计划。

### FR-04 Capability & Environment Preflight

- 明确区分 `source_readable`、`manifest_recognized`、`dependencies_preparable`、`baseline_executable`、`verification_supported`、`unknown`、`unsupported`。
- 从实际仓库内容和 CI 证据识别 Python/TypeScript、npm/Yarn/pnpm/Bun（仅支持实际已验证子集），不能对所有项目默认承诺可执行。
- 在 UI 方案批准前尽量完成不带副作用的能力预判；依赖安装与真正基线运行仍必须在合法阶段、受限 Sandbox 中完成。
- 支持受限依赖代理、固定二进制/锁文件、原生模块、工作区视图的一致性验证；任何未知包或需要外网/特权宿主环境的场景如实 block。
- 禁止把环境失败当业务代码失败；不许消费代码修复额度修包管理器、用新宿主依赖污染测试或以扩大写入路径绕过依赖问题。

### FR-05 Tool Registry / Sandbox Editing

第一阶段必须实现并通过最小可执行链路的工具：

| 工具 | 作用 | 必备约束 |
|---|---|---|
| `list_paths` | 枚举归档/工作区路径 | 限根、分页、无 symlink 越界 |
| `search_code` | 在冻结源码中搜索 | 限 root、输出上限、来源定位 |
| `read_file` | 分页读取精确文件内容 | 相同 base/content hash、路径只读范围 |
| `apply_patch` | 受控增量修改文件 | 已批准写范围、原子写、前置 hash、冲突显式反馈 |
| `read_diff` | 返回当前改动及差异摘要 | 由实际磁盘/产物计算，不由模型编造 |
| `run_approved_checks` | 执行计划中批准的测试命令 | 完整 argv、工作目录、环境一致、网络禁止、预算限制 |
| `inspect_failure` | 查看真实测试/工具错误 | 保留真实错误类别与来源，脱敏、分页 |
| `get_task_status` | 查询真实任务阶段与剩余资源 | 只读取持久权威，不产生新副作用 |

**关键实现规则**：

1. 模型不可直接持有文件系统/宿主 Shell/任意 Docker API 的能力；Tool Gateway 的服务端按 ToolSpec 和原授权判定。
2. `apply_patch` 优先采用原子增量编辑；相同文件的连续补丁是正常编辑事务，不应触发老格式的“一个文件只能有一个操作”限制。可修复的补丁冲突返回最新摘要/候选和显式冲突，不允许 fuzzy/magic 猜测编辑意图。
3. 允许反复 `read_file → patch → inspect → patch`；每次 patch 均落可审计的工作区变更收据，最后由真实 Diff 构造不可变 ChangeSet/Review 输入。
4. 读取辅助文件**不**增加写权限；禁止路径遍历、符号链接越界、Git hooks、filter、submodule、凭据助手、宿主挂载、私有网络和元数据服务访问。
5. 测试必须在冻结工作区且与基线可比较的独立执行环境进行，默认禁止网络与模型调用；依赖准备单独处理。
6. 代码循环可以自动修复当前批准范围内的新回归，不得因旧基线失败自动改无关文件、弱化测试、追加 `ignore`、禁用 CI 或静默缩小检查。
7. 编码阶段结束时生成实际 Diff、改动清单、代码/依赖/验证收据；Review 使用真实 Artifact。编辑能力只开放在明确批准和合格隔离条件下。

### FR-06 独立 Review 与判定

- 独立 Reviewer 消费冻结 Plan、base SHA、真实 Diff、必要源码、前后测试 Artifact，不复用 Coder 对自己结论的假设。
- Reviewer 必须区分 `pass`、`block`、`insufficient_evidence`、`environment_blocked`；缺少关键测试或审查证据**不能默认 pass**。
- 任何审查 block 均产生结构化 findings：严重级别、路径/位置、实际证据、修复建议、是否允许在已有范围内修复。
- Coder 回到工具循环时必须带真实 findings 与现有工作区，限定次数重新验证并再 Review；不应靠重新生成完整 JSON 编辑整文件。
- 保留当前独立 Reviewer 与模型可配置能力，重构其数据来源而非取消 Review。

### FR-07 发布、安全与新鲜度

- 精确人工验收后才准备 Publisher 意图；Fork/Push/Draft 仅在独立、限权 Publisher 执行且需要独立一次性确认，冻结 target/base/head、diff、tests、review、PR 文案。
- 编码沙箱绝不持有 GitHub 写 Token。发布结果未知时先只读 reconcile，不能盲重试。
- 检查上游 base SHA / Issue / 现有 PR / 已修复事实；若上游已包含修复，显式结束为 `obsolete/no_change_needed`，不伪造新 PR。
- CLA、Ready、已发布 PR 文案更新仍受现有二次单独授权规则约束。

## 4. 非功能需求与持久化契约

### NFR-01 持久化与兼容

- 先盘点 `app/models.py`、`app/migrations/versions/`、`app/workbench.py` 的现有事实与迁移表。
- 新 runtime 的 schema 必须版本化，使用**前向迁移**；不修改历史 migration，不清库、不覆盖旧值。
- 历史旧会话和 Proposal 应继续可读；进行中的旧作业建议固定 v1 协议运行至安全终态，v2 在安全空闲边界激活，或者有显式且可验证的迁移程序。
- 保留历史事件哈希链、归档哈希、审批来源；可派生索引不能替代审计日志。
- AgentCheckpoint 原子关联上一 ToolResult 和待执行动作，支持 kill/restart 演练；使用事务 + CAS/租约来避免双执行。

### NFR-02 预算和故障分类

错误域及最低处理政策：

| 分类 | 示例 | 自动行为 | 停止条件 |
|---|---|---|---|
| protocol | JSON/tool schema、read-tool conflict | 校验后有界修正；机械无歧义转换交适配器 | 超过合法修正预算 |
| model/provider | 限流、超时、截断 | 按类型有界退避、分页/减少单次负载；保留用量 | 不可重试/额度不足 |
| environment | 依赖、原生 addon、运行时版本、网络准备 | 独立环境诊断/固定配方恢复 | 已证实不受支持或恢复失败 |
| baseline | 修改前已有错误 | 单独登记，不污染代码修复 | 不满足验收政策且无经批准新方案 |
| regression | 新增断言失败、代码异常 | 在批准范围内调用编码工具修复 | 修复额度耗尽或范围冲突 |
| platform | 缺内部模块、迁移失败、部署不完整 | 停止业务任务并显示平台级阻塞，不消耗代码额度 | 需要维护工程变更 |
| external_unknown | Push/Draft 等外部结果未知 | 只读核对与幂等 reconciliation | 无法确认结果时等待人工 |
| stale_upstream | 远端已修复、base 改变 | 只读刷新并使旧审批失效 | 当前任务过期或需新计划/批准 |
| security | 越权、密钥泄漏风险、沙箱逃逸 | 永久阻止该次操作并保留脱敏证据 | 直到策略/人工安全评估完成 |

每次错误必须输出 `category`、`safe_reason_code`、`retryable`、`remaining_budget`、`next_allowed_action`、`blocking_scope`、`evidence_ref`。不要把模型自然语言错误当作诊断权威；错误原文需脱敏。

### NFR-03 可观察性和成功率

同一 UI 应展示 Tool Timeline：调用工具、开始/结束时间、输入摘要、已产生 Artifact、预算消耗、原因码和恢复动作（不泄露密钥/原始恶意输入）。

新增下列统计口径且可按版本、项目类型、Agent Provider 对比：

- `native_journey_started`：用户从原生产品启动的真实任务数。
- `native_journey_completed`：通过审查及人工验收且未依赖维护介入的真实完整任务数。
- `operator_interventions`：维护人员/外部 Codex 修改平台或业务提案、直接修数据/执行恢复脚本的次数；**必须永久标记本次样本非自主**。
- `first_attempt_pass_rate`、`tool_schema_rejection_count`、`model_call_count`、`repair_rounds`、`baseline_blocks`、`environment_blocks`。
- `actual_tests_executed`、`verified_checks_passed`、`false_success_count`、`median_token_cost`（有真实账本时才算）。

`Job.succeeded`、`model_call succeeded`、`review pass`、`no_change_needed`、`complete_journey` 分别计量，不能混用。

## 5. 一次重构，七个自动执行阶段

所有阶段必须在**同一个 Codex 任务/对话**中按顺序推进。每阶段完成后执行测试、保留证据、更新专项进度、允许做本地提交；没有外部硬阻塞则**不要停下来等待用户发“继续”**。不得用“已生成设计文档”提前结束。

### M0 — 分支、基线与可复现审计

**先做分支安全检查**：

```bash
pwd
git rev-parse --show-toplevel
git remote -v
git status --short --branch
git branch --list 'refactor/agent-runtime-v2'
git fetch origin main
```

若工作区有与用户有关的未提交修改：**不得 reset/stash/clean 覆盖、不得自行提交进 main**；优先评估使用独立 `git worktree` 从 `origin/main` 创建新分支（不接触旧工作区），或仅在能确认无数据风险时切换。**特别注意**：本规格可能在创建分支前以未跟踪文件形式放入原 `docs/`；若创建新 worktree，不会自动带入该文件，必须安全地将同一份本规格复制到新 worktree 的相同 `docs/` 路径，核对内容后再继续，不得误删用户原件。若目标分支已存在，安全检验来源与是否有未合并工作，继续该分支，不覆盖、不 force。无冲突且原工作区干净时：

```bash
git switch -c refactor/agent-runtime-v2 origin/main
```

**必须先确认当前分支是** `refactor/agent-runtime-v2`，再修改任何工程代码。工作树冲突且无法安全迁移时只报告实际阻塞；不要在 main 上开工。

任务记录：新增 `docs/tasks.md` 下的 Phase 5 子项 `ARV2-M0..M6`、当前状态和严格退出门槛，原未完成门槛保留。建立 `docs/acceptance/agent-runtime-v2/` 证据目录与 `docs/adr/`（按需）。绘制当前真实调用链；从历史记录挑选 A2UI/oh-my-pi/Bun 三类症状，建立可执行回放测试与当前版本的失败基线。修复目标应对故障类别而不是硬编码项目名。

**Gate M0**：当前架构图、root-cause evidence map、原样回放失败用例、受支持环境矩阵、迁移方案与基线测试摘要齐全。

### M1 — 框架选型 Spike 与 Task Orchestrator v2

比较现有框架增强、LangGraph、Agents SDK、Codex SDK；在隔离 fixture 上证明最小多轮工具执行、暂停恢复和状态事实归属，形成 ADR。实现单一任务协调契约及原有 Job、ExecutionAttempt 和授权的适配，不必硬删旧模块；把「收到消息」「请求后台动作」「阶段到下一阶段」「恢复失败」做成明确定义的持久事件。重点解决只读进度问答与后台任务非阻塞、重复继续不重派发、已有批准不重复确认。

**Gate M1**：并发读/写、幂等继续、Worker 崩溃恢复、历史消息焦点、旧任务只读兼容通过；旧的 v1 路径继续可用。

### M2 — Agent Model Adapter + Tool Registry + Sandbox Gateway

创建受限工具协议与回合内循环；从 `list/search/read/apply_patch/read_diff` 构成第一条不联网、无密钥的可测试编码链。每个工具有 Type Schema、授权检查、目标 snapshot/plan/attempt 绑定、日志收据、错误契约。补好一条从任务新建 → 真正读代码 → 模型多次调用 → 增量修改 → 生成真实 Diff 的完整切片。旧 JSON ChangeSet 仍保留历史兼容，但 v2 的主要编辑路径不再让模型一次性合成全套文件操作 JSON。

**Gate M2**：模型连续编辑同一文件、重复工具调用与恢复、读越权、写越权、模糊 patch 锚点、并发冲突、安全 canary 测试通过；测试必须证明沙箱是实际执行者。

### M3 — 计划、环境与真正的测试反馈循环

Plan Agent 通过真实源码查证生成最小可执行计划；读取 `read_context` 与 `read_paths` 的复合请求不会整体失败。把环境 capability verdict 和执行准备分离。Verifier 保留原验证命令的 argv/working_directory 与真实启动/收集/结果证明；不把仅打印 help 的成功退出当作验证通过。Coding Agent 收到结构化失败反馈后在同一批准范围内连续修复，必要时恢复冻结工作区。

**Gate M3**：A2UI 读取冲突回放能顺序完成；Bun 错命令不能误报通过；Python/TypeScript 支持矩阵 fixture 执行前后真实测试且来源一致；baseline 与 regression 不混淆。

### M4 — Review、恢复与统一对话体验

独立 Reviewer 对实际 Diff 和验证证据作出判定；审查不通过时进入有界修复循环。对话显示阶段、工具调用、详细失败、合规下一步；“继续”“完成了吗”“按照我的要求修改”在前后台并行工作时均有准确效果。统一分类 protocol/model/environment/code/platform/budget/stale/external_unknown，不再只给“代码生成未完成”。任何模型叙述不能凭空变成已实施、已通过或已发布。

**Gate M4**：完整 Mock/record-replay 旅程与 Browser Test，包含 Review block→修复→重新 Verify、停止/恢复、服务重启、审批失效；UI 展示真实 Artifact 引用。

### M5 — 原生产品真实模型 + 真沙箱验收（硬门槛）

提前冻结测试样本并记录随机性/来源，不得边测边为某个 Issue 特判。至少：

- **S1 新的小型 Python Issue**：通过产品对话读取代码、讨论计划、人工确认、模型真实工具修改、真实测试、独立 Review、人工验收，无外部 Codex 修业务。
- **S2 新的小型 TypeScript Issue**：要求一次测试失败后由 Agent 在批准范围内修复，重新运行验证和 Review，最终达到人工验收。
- **S3 边界样本**：上游已修复（正确停止，无 PR）或环境不支持（清晰阻断，不能误判为编码失败）；S3 不算代码交付样本。

全流程通过原生 API/UI，模型与 Sandbox 都要真实运行；可在用户提供且合规的模型凭据与测试资源下执行。构建者本人用 Codex 编写平台代码正常，但**验收运行期间不能用外部 Codex 或运维脚本修正在跑的业务任务**；如需修平台，先结束该次验收、记为未通过，再修工程并换新样本重跑。

必须在 `docs/acceptance/agent-runtime-v2/` 留存脱敏的 case manifest（仓库与 base SHA/Issue/目标）、时间范围、工具/模型调用摘要、真实验证收据、review、Diff hash、成功/失败/操作员介入统计；不可把凭据、完整用户隐私和原始密钥提交仓库。

**Gate M5**：至少两例支持范围内真实代码交付由产品独立完成 + 一个边界样本正确终止，均有完整证据；若真实模型凭据/受控测试资源缺失，必须标记 `blocked_external_prerequisite`，不能写“完成”。

### M6 — 安全回归、跨平台、交付与整合

- 执行现有 pytest、`python -m compileall -q app`、UI 浏览器/契约测试及 `git diff --check`。
- 安全恶意 fixture：越界路径、符号链接、伪装脚本、Git hooks、submodule、Prompt injection、泄露密钥 canary、未知 Publisher 回执、网络/资源限制。
- 原生 macOS arm64 / OrbStack 与 Linux amd64 / Docker 的 P5-G06 门槛**只有真实跑过才勾选**；缺平台标明确阻塞，不得用模拟替代。
- 数据迁移：备份/升级/回放旧历史/回退恢复；不能删库重建。核对 API、Worker、原生 JS UI 的版本兼容。
- 如测试隔离部署资源可用，在**独立命名空间/端口/存储**构建并验证，不覆盖正在运行的正式实例；现有 AGENTS 的自动重部署约定在本次分支重构期间仅对明确隔离的测试实例适用。
- 生成用户可审阅的方案与发布预览；**没有独立授权，不执行真实远端 Fork、Push、Draft PR**。若已获得明确测试账户权限及精确确认，可对专用非生产测试仓库验证 Draft PR，未获得即保留待验收状态。
- 分阶段本地提交前核对 staged diff 无秘密/无未授权文件；最后确保分支未合并到 main、未自动 push、没有不相关改动。

**Gate M6**：所有可用环境中的真实测试及安全门槛通过，`docs/tasks.md` 准确反映各门禁（特别是 P5-G06、UXR-G01），操作手册/ADR/运行时契约/迁移与恢复说明同步，提供可重现实验证据。

## 6. 关键测试矩阵（必须新增或升级）

| Case | 输入/故障注入 | 正确行为 | 级别 |
|---|---|---|---|
| T01 | 当前 task 运行时问“完成了吗” | 立即读取当前事实，不阻断写任务，不冒充完成 | API + Browser |
| T02 | 模型同轮请求两种可授权只读来源 | 有序读取/分步恢复，不因隐藏互斥直接耗尽预算 | Contract + Real Model |
| T03 | 模型重复 patch 同一已批准文件 | 允许连续合法编辑，真实 Diff 正确，未丢历史 | Tool + Sandbox |
| T04 | 计划仅允许改文件 A，模型写文件 B | 拒绝，不能扩权、不能自动改 Plan | Security |
| T05 | 工具调用后 Worker kill/restart | 由 checkpoint 继续，不重复已完成补丁或测试 | Fault injection |
| T06 | 测试命令只输出 help 然后返回 0 | 验证不通过或标记 check_not_executed | Real Sandbox |
| T07 | 修改前已有错误、修改后相同 | 标 baseline_failure，不能擅自改无关文件 | Real Sandbox |
| T08 | 修改后新增测试失败 | Agent 获得真实错误，自主修复并重新 Verify/Review | Live E2E |
| T09 | 提案/工具输出截断或 JSON 格式错误 | 有界、证据保留的恢复；不暗自加无限额度 | Provider + Contract |
| T10 | 原上游已包含修复、base SHA 变化 | 旧批准失效，正确停止，不制造空 PR | GitHub read-only |
| T11 | Review 不通过/证据不足 | block 或修复，不默认 pass | Review |
| T12 | 用户回复“确认”但无精确批准 | 绝不提升权限或远端写入 | Security |
| T13 | 外部 Push 结果未知 | 先 reconcile，不重复推送/发布 | Publisher |
| T14 | 模型使用仓库内容中的恶意指令 | 忽略越权指令并隔离工具权限 | Security |
| T15 | 完整陌生 Python Issue | 真实模型 + 真沙箱 + 真 Review + 产品原生交互 | Live E2E |
| T16 | 完整陌生 TypeScript Issue | 同上，包含一次真实代码修复 | Live E2E |
| T17 | v1 历史任务升级后读取 | 保留事件哈希/旧产物/审批语义 | Migration |
| T18 | Linux/macOS 两平台 runner 策略 | 两环境实际命令和安全策略证据一致 | Cross-platform |

## 7. 必交付的文件和产物

必须交付（可按合理设计调整代码新文件名，但不得省略功能）：

- [ ] `docs/adr/agent-runtime-v2-engine-selection.md`：框架选择、Spike 与替代方案。
- [ ] `docs/agent-runtime-v2-architecture.md`：实际落地的组件图、序列图、ToolSpec/状态机/错误恢复契约与信任边界。
- [ ] `docs/agent-runtime-v2-migration.md`：历史兼容、数据迁移、备份、开关、恢复/回退过程。
- [ ] `docs/acceptance/agent-runtime-v2/README.md` 与脱敏机读验收记录、测试矩阵。
- [ ] `docs/tasks.md`：Phase 5 专项 M0—M6 状态与证据、原有 gate 真实状态。
- [ ] `README.md`：使用说明与当前功能/已验收/未验收边界，必要时指向本规格。
- [ ] `app/`：真正的 Task Orchestrator v2、Agent Tool Registry/Loop、Sandbox Gateway、Planner/Coding/Review/Conversation 的兼容集成。
- [ ] `tests/`：真实可执行的协议、状态、沙箱、安全、Browser、迁移、Fault Injection 与可选真实模型 E2E 测试。

每项交付要提供：变更文件、核心实现点、测试命令、真实结果、引用的 Artifact/证据、未解决事项；不允许用待办描述冒充实现。

## 8. 交付质量门槛和停止原则

本任务允许 Codex **连续推进多个阶段**，但不能为了凑出一个“全部完成”而降低门槛。每阶段内部出现普通错误时自行诊断、修复并复测；不同阶段之间无需等待人工逐次发送“继续”。只有以下情况应暂停并明确向用户报告：

1. Git 分支/工作区发生可能覆盖用户内容的冲突；
2. 需要未经明确授权的外部写入、法律同意或生产实例操作；
3. 缺少真实模型凭据、隔离运行环境或目标平台权限且无法合法取得；
4. 安全策略冲突、不可控的敏感数据风险或上游平台限制；
5. 当前可用资源/工具耗尽，导致无法证明硬性验收门槛。

在暂停前尽量完成不依赖该前置条件的剩余安全工作，持久记录 checkpoint、已通过阶段、未通过 gate、下一步唯一恢复条件。不要循环等待、不要声称以后会自动后台完成。

### 最终报告格式（Codex 必须遵守）

```text
Agent Runtime v2 重构结果
- 工作分支 / 起始 SHA / 当前 SHA：
- M0—M6：各阶段完成、未完成或 blocked 的准确状态：
- 框架选型、为何不是只换 Prompt：
- 主要代码变更与旧版兼容：
- 真实受限工具调用证据：
- 测试矩阵实际通过/失败/跳过统计：
- S1/S2/S3 真实自主验收及维护介入次数：
- 安全、数据库、跨平台、发布门禁状态：
- 是否已按授权完成本地提交（不得自动推送）：
- 阻塞的唯一具体前置条件及恢复步骤：
- 未修改 main，未执行未授权的远端写入：
```

## 9. 不可接受的伪完成示例

- “修复 A2UI 的 `context_read_tool_conflict`，本次重构完成。”——只修了一个实例，不是新 Runtime。
- “改用 LangGraph，所以 Agent 能自主执行。”——选型不是验收。
- “模拟工具循环成功，真实模型待以后测试，全部门槛通过。”——属于伪通过。
- “将大 JSON 输出限制调到 64K，再让模型重试。”——不能替代可增量执行的工具 Harness。
- “绕过沙箱直接用本机 Codex 修改外部 repo，因此系统有代码交付能力。”——越界且验收无效。
- “由于用户说继续，自动 Fork/Push/签署 CLA。”——未授权第三方写入。
- “简单 Issue 不需要实际测试。”——不满足产品承诺；确实不支持时应明确阻断或仅作只读调查。

## 10. 与未来 xcli 的兼容接口

v2 中禁止将商业工作流直接耦合于某个模型/CLI 的会话格式。设计 `AgentExecutor` 插件契约：

```python
class AgentExecutor(Protocol):
    async def start(self, task_spec: TaskSpec, tools: ToolRegistry) -> AgentRun: ...
    async def resume(self, checkpoint: AgentCheckpoint, feedback: ToolResult | None) -> AgentRun: ...
    async def cancel(self, run_id: str) -> None: ...
```

此处为**概念接口**，应按项目现有 Python 风格落实且遵循信任边界。第一阶段先让当前已配置 Provider 的模型通过受限工具运行；之后可新增 `xcli` 或 Codex SDK/app-server 适配器。接入新 CLI 不能跳过 Sandbox、任务事实记录、预算、用户审批或独立 Review。现有 `app/providers/xcli_local.py` 只说明模型网关已具备部分本机连接能力，不证明容器 Host Bridge 或完整 Coding Agent 运行能力。

---

**完成标准一句话**：不是“能让模型说自己完成了”，而是用户仅通过 Contribution OS 对话与合法确认，就能看到**真实工具操作、真实源码修改、真实测试、独立 Review 和可信的下一步**，并且这些结果在进程重启、模型失败和历史数据存在时依然可复验。
