# Coding Loop Recovery v1

范围是已有 native Coding 循环的最小离线修复。沿用 Journal、Progress Ledger、TaskSpec、ArtifactStore 和 CAS 检查点；不引入新执行器、Plan 或持久状态机，不改变模型 Profile、工具容量、授权路径或验证命令。

## 推进与证据

新增 `coding_context.observe` 从不可变事件、已验证回执和 Prompt/响应重算独立 Coding 度量。旧 Ledger 的 `no_progress_streak` 保留原语义；新度量在 `implementation_progress.coding` 显示，并随 model_started 的完整 Prompt Artifact 保存。API/UI 只为当前新 native 协议派生显示，不给旧 Plan 追加“已验证”进展。

- 可用源码：平台成功回执中的当前工作区、路径、文件 Hash、精确字节区间；原始 source_page Hash 必须一致。
- 已送达源码：只有同 job、同 binding、同 Prompt 的已保存真实或替身响应才能确认该请求。只计精确匹配的内联字节；预览、索引/Hash、未知结果不计。read_artifact 的原始源码页和 JSON 中完整编码字符也按原区间映射，不能把整个回执都算作源码。
- 分别记录可用/已送达唯一字节、源区间索引、未应用有效 Patch 的轮数、未尝试 Patch 的轮数、未增加送达源码的轮数、重复 Plan/重叠源码读取、首次成功 Patch 轮次、当前 next_action，以及 not_found 的完整回执引用。
- 成功读取和新 Artifact 不能清零“未应用 Patch”轮数。第 6 次后在下一正常请求明确纠偏，第 8 次后于第 9 次调用之前安全 blocked，不创建额外调用，不消耗协议纠偏额度。旧的更早停滞/预算/超时守卫仍有效。只有成功且 source_hash 实际变化的 Patch 重置应用计数；无效/无变化 Patch 不能规避。
- 纠偏要求根据已有源码进行精确修改，或指出缺失的具体源码/阻塞原因；绝不强制猜测 Patch。6/8 是保守的离线候选策略，不是已验证真实模型最佳阈值。

所有指标都可从原事件恢复；没有新数据库字段、表或历史模型更新，故不需要 forward migration。现有 0046 schema、旧进度 JSON 字节和终态保持可验证。新 stop 使用现有 blocked 事件/检查点路径与现有 Progress Ledger blocker，不恢复 terminal、不更改账本、没有独立可漂移状态。崩溃后的未知请求仍走原 unknown/blocked 路径。

## 投影

32000 保守输入界限不变。这是按 canonical JSON UTF-8 长度、工具定义、系统指令与固定开销计算的预留上界，不等同于 Provider 实测 Token 数。

无可用源码时保留原完整任务可内联策略。有可用源码时，Task Brief 仅按字段从已验证冻结 Plan 提取：验收条件、实施步骤、测试要求、风险等精确保留；相同 goal、命令和授权路径在根部提供，来源不明时不去重。Issue title/URL 保留，Issue body 与完整 Plan 通过原始 task_context_artifact 恢复。Brief 明示 partial，不能假装全文 Issue 已送达，更不能平台重写目标或授予权限。

源码工作集最多四个最近观察到的不同路径，优先已授权写入路径；只从已经成功读取的当前源码页面抽取精确 UTF-8 片段。优先尚未送达区间，全部已送达时可重现已有字节。每段含真实 receipt_artifact、路径、offset、total_bytes、next_offset；该回执带有文件 Hash、工作区 Hash 和原始 source_page_artifact。绝不从完整归档擅自加载未读取源码，不生成文件摘要。

工作集并不声称模型具有跨请求记忆：`source_index.delivered` 是历史请求并集，`source_working_set` 才是当前实际内联。目录和不存在事实、每个原始批次回执的身份/状态、失败、检查、授权及原始恢复引用仍保留。重复 input/source Hash、固定说明和历史概要可去重；整批所有回执按原顺序可恢复。显式 read_artifact 请求仍反馈至多 1024 字节的真实页并调整 next_offset，原始回执页界不变；不会让 Plan/Issue 的剩余内容变得不可获取。

重复读取警告若投影到工作集，其完整数字 note、回执引用和工作区 Hash 一并保留。旧观察器仅在 note 精确匹配且片段确实来自该回执时计入“已见警告”。回归曾发现仅移动源码会让警告观察数丢失，现已补上这条兼容路径；原两条可见警告阈值保持不变，不能把仅可恢复但未内联的 note 记作已看到。

在不改变 32000 的前提下，依次尝试 4096/2048/1024/768/512/256/128 字节片段。mandatory 元数据仍太大时由既有 worker 在调用/计费前 fail closed。没有增加读取、Token、输出容量或模型调用数。最新源码 Verify、独立 Review、8 个纯 read_file 的 4+4 及容量证明逻辑保持原样。

协议升级为 `native-coding-loop-recovery-v10`，包括新的 Coding policy 和 Task Brief 策略，工具协议 Hash 因而变化。旧 native TaskSpec 仍可读取；执行准入拒绝旧 transport，不能将 Plan15 的授权静默套用新源码。下一真实验收必须重新冻结新源码/镜像/协议/Binding/精确预览并单独授权，本轮不准备或创建它。

## 模型输入/输出风险与候选

| 方案 | 本轮行为 | Hash / 预算 / 授权影响 |
| --- | --- | --- |
| high，单次 4096，总输出 32000 | 保持；先验证上下文/推进修复 | Profile Hash 不变；源码和 native 协议 Hash 已变，仍需新冻结/授权 |
| low，单次 4096，总输出仍 32000 | 仅候选，不改配置；可用 fake transport 核验请求字段 | reasoning_effort 改变 Profile / model identity Hash，Provider thinking 参数变化；需要新 Plan/Binding/预览和授权 |
| medium，其他不变 | 不推荐作为独立实验 | 当前 Studio 分支把非 low 都映射为 high；Profile Hash 即使变了也不意味着实际请求降低 reasoning |
| high，增加单次输出 | 不实施；不建议作为本轮后续默认值 | 改变请求契约/单次预算和失败预留，即使总上限不变也需要新协议冻结/授权；若还增加总上限则另需用户扩大授权 |

Profile cap=16384，实际单次 min(4096, remaining_output)；reasoning 与最终调用共享输出。24 次调用、每次最多 4096 并不保证能全部用满，受总 32000、累计输入 200000、500000 字节和 1800 秒更早约束。纠偏/Provider 重试也从原模型/Token/时间预算支付。拒绝和截断保留实测用量与失败预留，不退历史费用。本轮不作外部供应商性能承诺；low 是否有用仍未验证。

输入风险：Brief 未内联全文 Issue，需要时必须可靠分页；四文件工作集可能不足以完成跨文件修改；尚未送达区间优先可能需要重新恢复锚点。所有这些都应在下一真实验收观察，不能用替身通过掩盖。达到任务停滞阈值就停止，不能自动扩大工作集、额度或另起付费轮次。

独立 Review 保留原独立工作/证据契约：输入最多 1800000 字节、最多 3 次 job attempts、300 秒、单次输出 16384；它不在 Coding 6/8 轮守卫内，不得把其额度转给 Coding。沙箱仍为 CPU 2、内存 2 GiB、PIDs 256、单步 600 秒、磁盘 4 GiB、日志 4 MiB、禁网、只读 root、无凭据/模型访问/宿主 HOME/Docker socket。全部完整字段见最终 validation-report，未增容。
