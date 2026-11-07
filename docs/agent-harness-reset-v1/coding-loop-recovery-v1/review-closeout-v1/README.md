# Coding Loop Recovery v1 审查边界收尾

审查基线：核心 Commit `b404a92326ca01efa7e64ab91ff9831b548c6562`，开始工作时 HEAD `737951fa34d9a204b3290419d2705a5f39cbfc76`。本轮只改三个生产模块及相关测试，不修改 6/8 数值、工作集选择策略、模型 Profile、额度或沙箱。本轮不 Commit、部署、Push、PR、评论，不创建 Plan16、不恢复 Plan15、无真实 Provider 调用。

交付：[源码/测试 Diff](code.diff)、[验证及文件 Hash](validation-report.json)、[测试证据](test-report-index.json)、[场景数据](scenarios.json)、[Plan11–15 核验](history-plan11-15.json)、[策略对比](policy-options.md)。原离线报告、Git 交付记录及 Hash 保留原时点，不回填成此次结果。

## 一、警告送达判定：已复现并最小修复

原 `read_progress` 直接遍历 `model_started` 中的 Prompt，尚无响应就会将完整警告记为已见；原 `coding_context.observe` 等待响应，但只检查部分身份字段。修复前专项 16 项中 13 项失败、3 项通过，原失败证据保留，不能把重复渲染或 dispatch 意图当成送达。

新增共享 `model_receipts.confirmed_prompts`，两个消费者使用同一校验。可信证据要求：唯一 job 的 orchestrator 请求与响应事件、响应事件在请求之后、真实 Prompt/响应 Artifact Hash、响应引用的 Prompt、事件及响应中的 Binding、Prompt 中的 Binding 全部匹配冻结 TaskSpec；响应版本和 request_id 也要匹配。坏来源、错引用、错 Binding、孤立或重复响应一律 `agent_model_delivery_invalid`，不挑选最后一个重复记录。

| 场景 | 修复后结果 |
| --- | --- |
| 只有 model_started，没有 model_response | 已见警告 0，已送达源码 0，不把未知结果叫作成功 |
| 请求/响应身份匹配 | 警告可计数，精确源码区间计入送达 |
| Actor / Prompt / Binding / request_id / 顺序不匹配 | 两个消费者都拒绝证据，无可用的送达计数 |
| 同一个警告同时出现在反馈和工作集 | 按原始 receipt_artifact 去重，仅计 1 个 |
| 读取器/Worker 重启 | 从持久事件重算，不依赖进程内计数，不重复累加 |
| Worker 丢失，只有 dispatch 或已有响应但没有已提交 Decision | 两种情况均保持 `agent_step_outcome_unknown`，原输入/输出预留保留，不重放或发新模型请求 |

“响应确认”证明请求送达有可信平台证据，不证明模型理解源码，也不证明工具 Decision 已完成。独立的未知执行停止逻辑没有改动。相关原生读取 Worker 重启和真实子进程恢复回归均通过。

`read_progress.POLICY.warning_source` 改为身份确认语义，因此 native tools contract Hash 改变；旧协议记录仍能读取，不能静默用于新执行绑定。没有新数据库字段或迁移，没有重写旧进度、审批或终态。

## 二、6/8 轮边界：实测结果与建议

| 离线场景 | 当前策略实际表现 | 结论 / 建议 |
| --- | --- | --- |
| 请求 8 轮纯重复读取 | 旧 Progress Ledger 在第 4 轮就以 agent_no_progress 阻断，后 4 次请求尝试不派发 | 保持原较早守卫；不能声称实际完成 8 轮重复读取 |
| 8 轮持续获得新源码，无 Patch | 6 轮后提示纠偏；第 8 轮工具完成后 ready，第 9 次调用前 blocked；可用 1024 / 已送达 896 字节 | 有成本边界，但工具新证据尚未回馈时就可能截停；不应以所有读取新颖性无限清零来解决 |
| 第 8 轮才获得关键证据 | 合法 Python 函数锚点只在第 8 页出现，未进入已响应 Prompt；已验证授权路径、文件 Hash 和唯一原文前置条件的第 9 轮替身 Patch 候选未派发，剩余 16 次调用不使用 | 已证实会过早停止；建议先选择一次有证据交接方案，不能宣称当前可部署 |
| 第 6 轮真实 OCI Patch 成功 | 实际文件内容/Hash 改变，first_patch_round=6，未应用轮数归零；第 7 轮真实 OCI Verify 通过，第 8 轮 finish 通过 | 保持仅凭实际成功且 source_hash 改变重置；不因总调用到 8 就误杀此路径 |
| 第 8 轮提案被真实 OCI 拒绝 | patch_hash_conflict，工作区 Hash 未变，writes=1；未应用计数不重置，第 9 次调用前阻断 | 保持，提案或拒绝不能伪造实现进展 |
| 正常 Verify / Review | 最新源码 Verify 后可请求独立 Review；原四种确定性 Coding/Repair/独立 Review 门禁回归通过 | 保持当前正常路径；独立 Review job 不属于 Coding 6/8 计数，不以此放宽其原上限 |

两项新增 OCI 测试是真实容器源码读取、成功/拒绝 Patch、最新源码 Verify/readback，使用合成授权与脚本决策，没有真实模型。成功 Patch 是用于检验状态机的精确测试改动，不是原 Issue 实现成功。测试搭建时曾因归档根目录不匹配基线而被原沙箱 code=29 拒绝；仅修正合成归档后重跑，未放松沙箱校验。

**本轮保持 6/8 实现不动。建议后续调整触发条件，先评审 [对比方案](policy-options.md)，不简单调大数字。** 即使某候选仍受 24 次总上限约束，多出一次可能执行的调用仍有费用和协议影响，不能当成无需授权的“免费纠偏”。

## 三、工作集跨模块选择

当前四文件工作集按授权路径优先、最近 read_file 回执优先排序，没有模块公平性保证。等权的 4 个 web 与 4 个 extension 文件中：

- extension 最后读取：连续 4 个投影全为 extension，web 历史已送达区间为空。
- 调换读取顺序：连续 4 个投影全为 web，extension 为空。

因此是持续的近期读取偏斜，没有证明平台硬编码偏好某个模块。保存的 Plan15 第 7/13 轮诊断投影仍各有两个模块，不能把压力场景误写成当时已发生的事实。

恢复路径仍存在：从历史索引的 ToolResult 引用读取元数据，得到 source_page_artifact；四个合法 read_artifact 分页可在一个后续反馈中恢复被排除模块的全部 4096 字节已观察页，Hash 和连续偏移一致，整包输入仍 ≤32000。恢复 Artifact 不会自动提升该文件在工作集的优先级。4096 字节原始页完成也不是完整文件完成，后续文件区间必须继续普通 read_file，遵守原读取和输出预算。旧回执超出最近窗口时完整历史索引仍按原 Artifact 恢复；原 issued-artifact/外部引用拒绝回归通过。

**路径可靠不等于总能在剩余 6/8 轮内完成恢复。** 晚到的跨模块恢复可能再次撞上硬停止。覆盖优先的候选及取舍在策略对比中，本轮不静默更改选择算法。

## 验证与历史边界

- 相关回归 179 passed；最终边界专项 24 passed；两组重叠，不相加。
- 新增真实离线 OCI 2 passed；本轮不冒充重新执行了此前全部 24 项 OCI 或 2056 项默认测试。
- compileall app、diff check、Artifact/报告 Hash、凭据及编码变体扫描通过。
- query_only 前后相同：45 张表、11 个旧检查点、Plan11–15 的事件/终态/审批/预算未变；模型启动 106、执行授权及审批各 13、任务事件 2551、Artifact 1802，活动 Job=0。历史未知占用未退款或释放。
- 线上五角色仍为原 254 文件，未部署本地改动；Linux amd64 未验证。

**部署 NO-GO。** P1 的最小修复可继续代码审查；P2 的晚到证据截停与跨模块覆盖策略仍需先决策和验收。未来若另行授权执行，还必须冻结新源码/镜像/协议及独立绑定，本轮不创建这些对象。真实模型自主 Coding 效果仍未知。
