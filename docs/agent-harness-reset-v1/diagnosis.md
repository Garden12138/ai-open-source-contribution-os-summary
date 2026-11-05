# Agent Harness Reset v1 — Phase 0 诊断

状态：**Gate 0 passed（按2026-11-04用户调整）；历史调用信息 unavailable**。

用户接受两次被拒绝批次的原始调用信息无法恢复，不再以此阻塞Phase 0。
既有诊断、读取关联和调用链仍有效；以下保留首次诊断与原Gate判定的历史上下文。
缺失范围为两次响应正文、十个原始调用ID/名称/参数/顺序；计数、Usage、拒绝原因、
零执行事实可核验。无法据此判断历史批次是否全只读，不推测或伪造缺失字段。
历史缺口状态为 **unavailable**，不是已补齐，也不影响其他能力继续开发。

后续准入：进入Phase 1回归与可观测性；保留4-call整批拒绝和有限纠偏，禁止自动分批。
必须实现未来响应的安全工具信封持久化：原始ID、名称、参数、顺序、finish reason、
Usage及拒绝原因。响应先持久化再校验/拒绝；凭据不得入库，私有推理不保存。

后续真实验收：用户批准精确预览81e1d70a后，Plan12重新准备和Baseline四项通过。
真实Provider连续出现6/5/5-call超量批次，原两次协议纠偏额度耗尽后终态阻断。
三份原始调用信封均已先持久化再拒绝，整批零执行；这次阻断不是历史证据缺失。
Patch尚未产生，Gate4不通过，不进入Phase5；见[真实结果](real-acceptance-result.json)
及[完整审计索引](real-acceptance-audit.json)。旧Plan11信息仍为unavailable，不改写。

已还原 39 次成功读取的精确参数、源码/文件哈希、回执、后续 Prompt 引用，
已追踪运行链路并核验历史完整性。**两次被拒绝批次的原始 Provider 响应和十个
调用的 ID、名称、参数、顺序没有出现在现有持久化证据中，不能判断是否全部只读。**
首次诊断时按当时停止条件未进入Phase 1；此限制现由上述用户调整解除。

## 范围与证据来源

- 任务：Agent Harness Reset v1；优先级来自本次明确请求。仓库原路线图仍为
  Phase 5 / P5-G06；UPEVE-E04、Linux 及其他原门禁保持未通过。
- 当前分支 `refactor/agent-runtime-v2`；HEAD
  `5d0edde5635e907b1aae0fef6543463bdca34a07`。
- Plan11 当时执行版本 `cf051560b8aa64cd01dd2abd351b7d6710d4d74f`。
  其 242 个冻结生产文件与原预览哈希吻合；`git diff cf051560 HEAD -- app`
  为空。本文关于历史行为的代码判断针对该版本，另行说明当前未提交候选。
- 原始只读副本：`/tmp/upeve-plan11-terminal-20261103/state.db` 和同目录
  `artifacts/`。SQLite 以 `mode=ro`、`query_only` 读取，取证前后数据库字节哈希一致。
  它是历史终态快照，不代表重新验收或当前线上状态。
- 衍生索引：[phase0-evidence.json](phase0-evidence.json)。包含全部 39 条读取、
  14 个 Prompt、两条原始拒绝事件的持久化 payload、终态 Checkpoint、冻结绑定和预算。
  源码正文与模型私有推理不复制到新报告；每项保留内容寻址的原始引用。
- 可重放采集器：[collect_phase0.py](collect_phase0.py)。只读历史状态，只新建输出文件；
  不发模型请求，不访问网络，不执行目标仓库代码。
- 历史对照：[执行结果](../acceptance/universal-environment-v1/ci-native-plan11-execution.json)、
  [读取观察](../acceptance/universal-environment-v1/ci-native-plan11-read-observation.json)、
  [原预览](../acceptance/universal-environment-v1/ci-native-plan11-execution-proposal.json)、
  [Baseline](../acceptance/universal-environment-v1/ci-native-plan11-baseline.json)。

开始时存在的修改全部保留：`loop.py`、`native_tools.py`、`read_progress.py`、
`test_agent_native_tools.py`、`test_native_read_progress.py` 的哈希均等于
[暂停交接记录](../acceptance/universal-environment-v1/read-progress-paused-checkpoint.json)。
已有 ZIP 与设计文档也保留。没有切换分支、提交、Push、创建 PR 或修改业务状态。

## 现象、证据与判断

| 现象 | 证据 | 对应代码（历史版本） | 根因判断 | 置信度 |
| --- | --- | --- | --- | --- |
| 两次 5-call 整批拒绝 | Journal seq1618/1698，均为 `NativeCallCountProtocolError`，5/4/0 回执 | `native_tools.py::parse_native_decision` 在遍历调用之前检查数量；`loop.py::AgentModelWorker.execute` 捕获异常 | 数量契约确实触发，未部分执行；不能推断调用类型 | 已证实 |
| 无法还原被拒绝的十个调用 | 两事件均无输出 Artifact；递归扫描 1523 个注册 JSON Artifact，无 `choices` 或 `tool_calls` 响应数组 | `NativeCallCountProtocolError` 只持有计数/Usage/布尔诊断；`provider_failure_receipt` 不含调用；仅 `ModelProtocolError` 保存 raw output | 原生超量路径丢失审计所需调用信息；事后摘要不可逆 | 已证实（本快照及所读代码范围） |
| 39 次 offset=0，33 次重复起点 | 所有调用都省略 offset；有效默认值为 0；六个路径哈希不变 | `contracts.py::Page` 默认 offset0、4096字节；沙箱 `page()` 忠实执行，不管理游标 | 调用参数每次重置为首屏，平台没有自动续页；不是底层读取忽略非零 offset | 已证实 |
| 精确重复比起点重复更少 | 归一化参数/源码哈希/文件哈希/成功状态共有 10 个指纹，29 次重复；4096 与16000不同 | `AgentToolWorker.execute` 原幂等检查只按平台 call_id | 新回合的新 ID 不会命中历史同参数读取；无跨调用证据复用 | 已证实 |
| 工具拿到全文件，模型仍继续重读 | 例如 seq1533 返回6518字节，Prompt1546仅内联1024；seq1628返回6518，Prompt1646仅512 | `loop.py::prompt_for`；`native_tools.py::_feedback_page/project_native_prompt`，32000字节保守预算 | 取回更大页仍被投影回文件前缀；扩大 max_bytes 不保证更多源码进入模型请求 | 投影行为已证实；对模型行为的因果关系为高可信假设 |
| 不是所有历史结果都完全进入后续 Prompt | 35/39回执有后续反馈；43条反馈引用中1条完整送达该回执的4096字节，42条缩短；0条完整文件内联；最后4条无后续模型请求 | 最新批次反馈、8条历史窗口、history Artifact分页 | 正文持久化不等于完整送达；也不能说模型完全没看到历史 | 已证实 |
| `required_next_step` 一直未形成执行约束 | Prompt1491为 `inspect_source`；其余13个均 `continue_implementation` | `prompt_for` 中 `observed_source`；`AgentModelWorker` 不以该字段限制读取 Decision | 一次成功读取即可令提示变为继续实现；没有调查步骤完成判据或有限纠偏 | 已证实 |
| 没有语义阶段账本 | 116个 Checkpoint 只有 ready/model/tools等执行态、pending_calls、hash、usage等 | `contracts.py::AgentCheckpoint`；`journal.py::EDGES/transition` | CAS执行恢复已存在，但没有 Investigation/Plan/步骤证据/进度revision | 已证实 |
| 纠偏送达却仍无 Coding | Prompt1621/1701含真实拒绝回执，剩余修复1/0；随后有效Decision；下一有效回合后清除旧纠偏 | `prompt_for` 的 `NativeProtocolCorrection` 分支 | 数量纠偏路径有效；它只纠正调用数量，没有推进调查/实现的进度控制 | 已证实 |
| search/patch/check没有结果 | 12个有效Decision、43个结果只含 list_paths/read_file | `RestrictedToolRegistry` 包含全部工具；`native_tool_definitions` 给各工具同一句泛化描述 | 已接受回合没有请求这些工具；搜索/Patch/Verify并非未注册。被拒绝批次是否请求过它们未知 | 已证实；模型未选用的内部原因未知 |
| 只读调用可以不限进展地继续到总预算 | 14次模型启动、43次工具操作、0patch/check；第15个Job在派发前失败 | `schedule` 按执行态继续；`journal.charge` 只限资源；无语义无进展计数 | 资源封顶存在，但资源预算替代了进度停止条件 | 已证实 |
| 最终 input admission失败并非模型额度/密钥错误 | 保留输入171256/输出12756；剩余输入28744不足下一完整请求；实际报告128816/5408 | `AgentModelWorker.execute` 先保守预留；失败保留预留，成功结算 | 原生协议失败预留占额，加上持续读取请求最终阻塞；不可把两类数字混成实际消费 | 已证实 |
| Tool Result内部关联有证据，原始vendor ID没有 | 39条读取的Checkpoint call_id、回执call_id、input_hash及Decision命名空间转换全部匹配 | `parse_native_decision` 哈希vendor ID为native ID；Loop再哈希为tool ID | 内部匹配可核验；原始ID不可逆，尚未满足目标架构对原始调用身份保留的要求 | 已证实 |
| 未提交候选不能当作已解决 | 五个候选文件与暂停快照完全相同，未部署；代码仍允许前几次重复执行 | 当前 `read_progress.py`、Loop新增守卫 | 它提供可见提醒计数与局部停止，不等于目标持久化Ledger或零底层重复执行 | 代码观察；本轮未重跑其测试 |

## 两次被拒绝批次

| 项目 | 第一次 | 第二次 |
| --- | --- | --- |
| 拒绝事件序号 | 1618 | 1698 |
| 模型Job | `2c5d65ad-b89f-43de-819d-e9a3606fb5b6` | `c345eb8e-531b-4c79-abd4-69fc1afb2014` |
| 事件哈希 | `e3a1b66bf18eb96b8553e7ebbbf0fdfdd2ebb00cc7399e05d93def25d9e392cb` | `98378b13620b0dfc4227c93c33ed8188ea1ac661c9ad6aad962f37cf11fc5bb5` |
| 前置Prompt序号 | 1616 | 1696 |
| 接收 / 最大 / 执行 | 5 / 4 / 0 | 5 / 4 / 0 |
| finish / tool calls / final text | tool_calls / true / false | tool_calls / true / false |
| 报告输入 / 输出 | 9500 / 354 | 9550 / 490 |
| 保留预留输入 / 输出 | 30574 / 4096 | 30916 / 4096 |
| 原始响应、调用ID、名称、参数、顺序 | **unavailable** | **unavailable** |
| 是否全只读 | **无法证明** | **无法证明** |
| 下一纠偏Prompt / 后续有效Decision | 1621 / 1623 | 1701 / 1703 |

解析器把超量响应的诊断原因记为 `final_content_missing`，但该处明确先验证
`finish_reason=tool_calls`，再抛出数量错误。缺少最终文本不是本次拒绝的触发原因。
不能把后一回合的四个读取调用当成前一被拒绝批次的前四个调用。

1523个注册Artifact及17个未注册内容寻址文件（包括JSON字符串中的嵌套JSON）均已扫描；
额外17个文件也未发现Provider响应数组，没有将它们补记成已注册历史。七个其他 `raw_output`
候选属于4个 `conversation-read-step-v1` 和3个 `conversation-decision-rejection-v1`，
没有原生响应数组。Gateway处理成功响应后返回数据，不提供响应正文持久化；
Worker错误日志刻意只记安全代码/异常类型，不能从这些字段还原调用参数。
本轮没有读取凭据文件或模型私有推理，没有为了补证据重发付费请求。

## 39次读取与后续Prompt时间线

所有有效参数均为 offset0、expected_hash=null；工作区源码哈希统一为
`7f02d8377a922ec5473bd68159817d058064e8a2892f65ef33ba17362bf9265c`，
输入与输出相同。下表以路径代号缩短显示；完整文件哈希与每条receipt ID、input_hash、
平台call_id、归一化参数和Prompt Artifact引用均在衍生JSON的 `reads` 中。

| 代号 | 路径 | 文件SHA-256 |
| --- | --- | --- |
| A | `web/hooks/simulation/handle-tool-events.ts` | `43556669992773fba142d096766771af76ad64161d5d05e08f817cb0af0d2cd9` |
| B | `extension/src/transcript-parser.ts` | `1124a36d2667624dfcb44a1ed988c7ed63930e6233cebe7872105099343181d9` |
| C | `extension/src/hook-server.ts` | `b77b14ed4a731f7d21192759774568d93385fcb1c0b25c538f6601d6069ed3a9` |
| D | `web/hooks/simulation/types.ts` | `5a73297acb10135b1fe2de7ccbb5c5b59b918815d35bd88d2262f744e65aeb19` |
| E | `web/lib/agent-types.ts` | `aca228d727fc36f2dfd24aef581b760cb667960d62430484511d137f8719cbdd` |
| F | `web/hooks/simulation/process-event.ts` | `08808a3dbb5129f910b1bfa2c45db2641e81e333c09fcab1de7bea0f0e3585c1` |

“Prompt:字节”表示该回执正文实际进入后续持久化模型请求的范围，起点仍为0。
成功送达请求不证明模型内部注意或理解；原始完整Artifact仍可通过受限工具分页读取。

| 回执事件序号 | 路径 | max_bytes | 实际返回字节 | 后续Prompt:内联字节 |
| --- | --- | ---: | ---: | --- |
| 1503 | A | 4096 | 4096 | 1506:4096 |
| 1513 | A | 4096 | 4096 | 1526:2048 |
| 1518 | B | 4096 | 4096 | 1526:2048 |
| 1523 | C | 4096 | 4096 | 1526:2048 |
| 1533 | A | 16000 | 6518 | 1546:1024 |
| 1538 | D | 16000 | 4416 | 1546:1024 |
| 1543 | E | 16000 | 7545 | 1546:1024 |
| 1553 | A | 4096 | 4096 | 1571:512 |
| 1558 | E | 4096 | 4096 | 1571:512 |
| 1563 | D | 4096 | 4096 | 1571:512 |
| 1568 | F | 4096 | 4096 | 1571:512 |
| 1598 | A | 4096 | 4096 | 1616:512，1621:512 |
| 1603 | D | 4096 | 4096 | 1616:512，1621:512 |
| 1608 | E | 4096 | 4096 | 1616:512，1621:512 |
| 1613 | F | 4096 | 4096 | 1616:512，1621:512 |
| 1628 | A | 16000 | 6518 | 1646:512 |
| 1633 | D | 16000 | 4416 | 1646:512 |
| 1638 | E | 16000 | 7545 | 1646:512 |
| 1643 | F | 16000 | 4631 | 1646:512 |
| 1653 | A | 4096 | 4096 | 1671:512 |
| 1658 | D | 4096 | 4096 | 1671:512 |
| 1663 | E | 4096 | 4096 | 1671:512 |
| 1668 | F | 4096 | 4096 | 1671:512 |
| 1678 | A | 16000 | 6518 | 1696:512，1701:256 |
| 1683 | D | 16000 | 4416 | 1696:512，1701:256 |
| 1688 | E | 16000 | 7545 | 1696:512，1701:256 |
| 1693 | F | 16000 | 4631 | 1696:512，1701:256 |
| 1708 | A | 4096 | 4096 | 1726:512 |
| 1713 | B | 4096 | 4096 | 1726:512 |
| 1718 | C | 4096 | 4096 | 1726:512 |
| 1723 | D | 4096 | 4096 | 1726:512 |
| 1733 | A | 4096 | 4096 | 1751:512 |
| 1738 | D | 4096 | 4096 | 1751:512 |
| 1743 | E | 4096 | 4096 | 1751:512 |
| 1748 | F | 4096 | 4096 | 1751:512 |
| 1758 | A | 4096 | 4096 | 无后续模型请求 |
| 1763 | D | 4096 | 4096 | 无后续模型请求 |
| 1768 | E | 4096 | 4096 | 无后续模型请求 |
| 1773 | F | 4096 | 4096 | 无后续模型请求 |

模型工具参数没有显式offset，源码分页代码按默认0执行。Prompt投影保留正确的
`next_offset` 与“继续读取/读取完整回执”指令；模型没有请求该续页位置，也没有调用
`read_artifact`。不能把它描述成平台没有提供续读能力。其最大可见文件前缀为：
A4096、B2048、C2048、D1024、E1024、F512；文件后部从未通过反馈正文送达。

## 实际调用链及边界

```text
Workbench执行确认 / contribution_workflow.advance_workflows
  → agent_v2.integration.advance_coding
  → TaskOrchestrator.start / freeze_spec / require_current
      检查原授权、Plan/Base/Policy/Image、真实Baseline、冻结ModelTransport
  → AgentJournal + Workbench（append-only事件、Artifact、CAS Checkpoint）
  → loop.schedule（Checkpoint哈希幂等键，一次一个leased Job）
  → Provider Worker / AgentModelWorker.execute
      prompt_for → project_native_prompt → input_bound → charge/reserve
      → adapter.model_adapter → NativeToolsModelAdapter
      → providers.studio.StudioRunner.complete_native_tools
      → 短期任务凭据 → StudioGateway / InternalModelGateway → 绑定Provider
      → parse_native_decision（数量、ID、Schema、作用域验证）
      → Journal turn + pending_calls，或 model_rejected + 有限protocol repair
  → Sandbox Worker / AgentToolWorker.execute
      RestrictedToolRegistry.validate/cost → charge → tool_started
      → DockerAgentToolGateway.execute
          冻结归档 + 当前快照重建，独立容器读取/精确Patch/批准Checks
      → 绑定call_id/input_hash/source_hash的ToolResult Artifact
      → 原子记录回执和Checkpoint → 下一Job或阻塞
  → finish请求 → validate_finish（最新源码Verify、真实快照）
  → advance_coding导出变更 → 既有独立Verify/Review链 → Human Acceptance
```

运行装配在 `app/worker.py` 的Provider与Sandbox Worker构造函数中。
`get_task_status`、`read_artifact`、`inspect_failure` 在平台读取已签发证据，其他
源码工具经隔离Worker。API `workbench_api.py` 暴露runtime/control/artifact分页；
UI `static/workbench.js::showAgentTools` 展示执行态、预算、真实回执与失败下一步。
UI此时没有任务步骤Ledger，不能将“工具成功”解释为“实现完成”。

`native_tool_definitions` 的每个非终止工具描述都是同一句“Platform tool;
existing task scope, evidence and budget checks apply.”。Schema提供搜索的字节位置、
Patch文件hash和批准命令限制，但缺少具体调查/编辑/失败恢复的用途及下一步契约。
`required_next_step` 只是提示字段，Registry没有据其阻止继续读取。
Plan11本来已有精确写入批准和八个write_paths，没有证据表明它在等待新的写入批准；
12个有效回合选择的都是读取工具。为何模型没有选择其他工具只能提出可检验假设。

工具ID关联经过两次单向哈希，实际请求以一条system消息与一条包含JSON的user消息
发送；它没有使用聊天协议中的assistant/tool多消息回放。工具结果通过平台JSON字段
关联。上述内部关联成立，但不能据此宣称原始vendor ID完整保留。

## 冻结模型、预算与完成约束

Plan11实现与独立Review均绑定 `deepseek / deepseek-v4-pro`，adapter
`studio-deepseek-thinking-v1`，model_version
`bb3522b03a444b544c808217ec5ed4b4fa71253591ee8c09e3fc77c57d3a6049`。
本轮没有切换或调用模型。

原限额：model_calls24、reads64、writes24、checks6、protocol_repairs2、
provider_retries2、code_repairs2、input_tokens200000、output_tokens32000、
output_bytes500000、seconds1800。保留原usage14/43/0/0及全部失败预留；
第15个模型Job未派发也未扣账，终态保持 `blocked/agent_budget_exceeded`。
规划64次上限和三个未知预留属于另一预算，不能挪作本次执行资金。

现有 `validate_finish` 要求当前源码hash的成功Checks与真实快照；
`integration.advance_coding` 再拒绝没有实际内容差异的快照。沙箱Verifier检查
before/after inventory hash不变，Patch仍绑定文件hash与批准路径。
它们提供既有安全基础，但本样本没有Patch/Verify/Repair/Review，不能声称完整闭环通过。
目标Ledger、有限无进展纠偏、原始调用追踪和Human Acceptance还需后续阶段实现与验收。

## Phase 0 报告与准入结论

1. **目标**：只读取证Plan11失败，查清重复读取、提示投影和数量拒绝路径。
2. **本轮新增/修改文件**：本诊断、采集器、`phase0-evidence.json`、`docs/tasks.md`
   的专项状态。用户原有候选、设计文档及历史验收文件均保留。
3. **关键实现**：仅离线证据关联；没有产品代码实现或持久化模型迁移。
4. **验证命令与结果**：

   ```sh
   PYTHONPATH=. .venv/bin/python docs/agent-harness-reset-v1/collect_phase0.py \
     --snapshot-root /tmp/upeve-plan11-terminal-20261103 \
     --output docs/agent-harness-reset-v1/phase0-evidence.json
   ```

   校验1523个Artifact哈希、1782条事件链、原1473条前缀、SQLite完整性/FK及凭据扫描；
   39条读取的调用/input/source绑定和后续Prompt正文前缀全部匹配。
   另以原预览与 `git show cf051560:<path>` 核验242个生产文件，74个旧证据文件
   哈希全部未变。独立输出重放与最终文件逐字节一致；
   `python -m compileall -q app docs/agent-harness-reset-v1/collect_phase0.py`、
   `git diff --check` 及新增文件空白/凭据检查通过。
   `docs/tasks.md`整文件扫描命中的7条旧模式与HEAD逐条相同，未更改；新增任务记录
   单独扫描通过。未将既有文档模式匹配表述成全文件扫描通过。
   未运行Phase 1新增回归或真实模型验收，历史48/1940通过不充作本轮成绩。
5. **未通过项**：两次5-call的原始调用信息；是否纯只读；原始vendor ID。
6. **风险**：更大读取被缩回前缀、历史窗口依赖主动分页、无语义进度守卫；
   当前候选尚未验证全部边界。暂存快照可能丢失，衍生索引保留哈希和元数据，
   不可替代缺失的原始响应。Docker只读枚举受当前工具沙箱限制，未据此修改权限或重启服务。
7. **首次诊断的准入结论（已被页首用户调整取代）**：当时拒绝进入Phase 1–5。需要提供上述
   两个Job的已有原始响应/调用存档位置。补证应仅保留安全工具信封与Usage，排除密钥
   和私有推理；不得重发请求假装还原历史。已向用户询问存档位置。

本轮仅文档取证，无运行代码变更；为保持Phase 0只读边界不重部署既有未验证候选，
不恢复Plan11终态，不产生新Plan、新授权或付费重跑。真实Issue现状和基线重新冻结
属于后续真实执行前检查，尚未执行，不沿用历史状态声称当前Issue可验收。
