# Plan15 离线根因分析

本报告消费已提交的真实证据，不恢复 Plan15。完整逐轮目标、历史索引、进度、参数、用量、回执及 Hash 见 [plan15-trace.json](plan15-trace.json)。复算入口是 [analyze_plan15.py](analyze_plan15.py)。15 个完整 Prompt、15 个真实响应证据信封及导出集合内 161 个 Artifact 均逐字节校验；原始文件没有修改。响应信封按原协议不保存助手私有 reasoning 文本，不是完整 HTTP wire capture；本报告不猜测隐藏思维。

## 已证实事实

15 轮使用同一个冻结任务目标、Issue/Plan Artifact `ee0895c86fac2d1cf225f045e22925d13df14666fd8aa66c2dbd04ddd67b1d73`（9431 字节）、授权路径、命令和模型配置。所有原 Prompt 的 `approved_task_input` 都是 partial 引用，并重复要求先读取完整 Plan/Issue。没有修改任务目标的证据。历史以最近 8 条索引和较早完整索引 Artifact 提供；反馈只选最近完成的原始批次，完整旧回执仍可读取。

表中“源码内联”是本轮请求直接包含的 read_file 源码字节，另有 JSON 回执中可完整解码的源码区间；不能把字节入 Prompt 等同于模型理解。新增区间按工作区 Hash / 路径 / 文件 Hash 求并集。工具读取与模型送达分开计算。

| 轮 | 模型请求 | 本轮工具源码 / 新增字节 | 本轮 Prompt 源码内联 | 工具后调查次数 / 旧停滞数 |
| --- | --- | ---: | ---: | --- |
| 1 | 4 个 web 文件，各默认 4096 | 16384 / 16384 | 0 | 4 / 0 |
| 2 | Plan offset=0 | 0 / 0 | 8192 | 5 / 0 |
| 3 | Plan offset=4095，恢复剩余 5336 | 0 / 0 | 0 | 6 / 0 |
| 4 | 再读完整 Plan + 3 个已读 web 前缀 | 12288 / 0 | 0 | 10 / 0 |
| 5 | 再读完整 Plan | 0 / 0 | 3072 | 11 / 1 |
| 6 | 8 个 read_file，默认 4096，4+4 | 32768 / 16384 | 0 | 19 / 0 |
| 7 | 再读完整 Plan | 0 / 0 | 1024 | 20 / 1 |
| 8 | 重读 handle-tool-events + 3 个回执 Artifact 前缀 | 4096 / 0 | 0 | 24 / 0 |
| 9 | 再读完整 Plan | 0 / 0 | 1024 | 25 / 1 |
| 10 | 回执/历史 Artifact + 3 个重复源码前缀 | 12288 / 0 | 0 | 29 / 0 |
| 11 | 再读完整 Plan | 0 / 0 | 3072 | 30 / 1 |
| 12 | 8 个 read_file，max_bytes=16000，4+4 | 66759 / 38087 | 0 | 38 / 0 |
| 13 | Plan 前 4096 + 进度/历史 Artifact 前 4096 | 0 / 0 | 1024 | 40 / 0 |
| 14 | 5 个 search_code + 1 个 read_file，整批拒绝 | 0 / 0 | 0 | 无工具执行 |
| 15 | length 截断，无工具调用 | 0 / 0 | 0 | 最终 40 / 0，blocked |

27 个成功源码回执中，16 次提供新增区间，11 次为精确重复，18 次重复/重叠；包含重复的返回源码 144583 字节，工具唯一源码 70855 字节。真实响应确认的请求中，唯一送达源码仅 10624 字节（含保存 JSON 回执中完整编码字符的映射）。40 次调查包括 27 次 read_file 和 13 次 read_artifact，不是 40 次独立有效调查。没有 apply_patch、最新源码 Verify 或独立 Review。

完整 Plan 在第 2–3 轮已经恢复；此后又多次从 0 恢复。其后另外五个回执/历史/进度 Artifact 只恢复部分。没有请求原始 `source_page_artifact`。详见原 [coding-metrics.json](../read-batch-scheduler-v1/plan15-execution/coding-metrics.json) 的恢复区间。重复完整 Plan 是真实响应中的参数；不能断言模型为什么“决定”重复。

第 14 轮六调用来自真实响应 `choices[0].tool_calls`，没有平台合成或子批次扩展：

| 工具 | 参数 |
| --- | --- |
| search_code | query=handleToolUse, prefix=extension/src/transcript-parser.ts |
| search_code | query=handleToolResult, prefix=extension/src/transcript-parser.ts |
| search_code | query=handlePreToolUse, prefix=extension/src/hook-server.ts |
| search_code | query=handleToolCallStart, prefix=web/hooks/simulation/handle-tool-events.ts |
| search_code | query=handleToolCallEnd, prefix=web/hooks/simulation/handle-tool-events.ts |
| read_file | path=extension/src/hook-server.ts |

5–8 调用例外只适用于纯 read_file。因此解析器先于执行整体拒绝，实际执行 0 次；原协议纠偏消耗 1/2，Provider 重试消耗 0。第 15 轮确实看到了可信 `native-protocol-correction-v1`，要求下一轮 1–4 调用，声明上轮 0 执行、剩余纠偏 1，不增加权限/预算。

第 15 轮请求 output_limit=4096，响应 completion_tokens=4096、reasoning_tokens=4096、finish_reason=length；诊断记录没有可执行最终工具输出。实际输入 10089，计费保留输入预留 30802 和输出 4096。停止码 `deepseek_completion_truncated`、retryable=false，没有自动第 16 次请求。

## 代码级根因

1. `progress.record_tool` 的 meaningful 以回执指纹/返回区间/结果判断，再对整批求 OR；成功的新 Artifact 页面也算 meaningful。它没有验证下一请求实际包含哪些源码。第 13 轮读到新的进度/历史页面后就能清零，调查计数与实现推进没有独立边界。
2. 精确读取复用约束相同源码/阶段/参数，能够避免部分重复 OCI 工作，但请求仍消耗 reads。`read_progress` 的旧范围阻断在 Progress Ledger 启用时不承担独立任务停滞判断；新指纹或不同页面仍能重置旧 streak。修改 max_bytes 并非必定重置（已有相同返回字节回归），真正问题是回执新颖性不足以证明 Coding 推进。
3. `loop.prompt_for` 每轮重新发 partial Plan 的必读指令；`project_native_prompt` 优先尝试完整任务内联，反馈缩页对所有回执使用相同上限。8 个大回执常降到每个 128 字节，而工具已经读到的其他区间没有持久的源码工作集投影。成功读到不保证后续实际送达。
4. 最近批次反馈策略意味着 read_artifact(Plan) 后一轮的主体再次是 Plan；更早源码只留索引。Provider 每轮是 fresh user/system 请求，不能假定先前源码自动保留。重复恢复 Plan 的诱因有直接指令和投影证据支持；其内部决策原因仍未知。
5. 原 4 调用 / 8 个纯源码读取契约在第 14 轮按设计工作。需要保留整批预校验，不能把任意混合 6 调用静默拆开。错误来自该真实响应超出合同；没有证明是 Scheduler 故障。
6. `AgentModelWorker` 取 min(4096, 剩余输出)，Studio/Gateway 再取 profile.max_tokens 与请求上限的最小值。Profile 的 16384 没有使单次请求变成 16384。reasoning 和最终函数参数共用 4096，而非各获 4096；没有最终函数的安全结果只能停止。Native 实际解析入口是 `parse_native_decision`；`nvidia_nim._completion` 的通用 length/actual usage 检查也已核对，不能把通用解析器误写成此次 native 的入口。

累计实际输入/输出为 135863 / 14939，计费保留为 176505 / 17211。差值来自第 14、15 轮失败预留未退回：输入 `(29684−9755)+(30802−10089)=40642`，输出 `(4096−1824)+(4096−4096)=2272`。不能按实际用量重写旧账或据此增加可用预算。

## 待验证假设

有来源的 Task Brief 与聚焦源码能降低重复恢复、帮助更早形成准确 Patch；high reasoning 在 4096 下可能比 low 更易耗尽最终调用空间。单次 trace 和离线替身不能验证这两个真实模型效果，也不能证明 high 一定不适配。尚无真实 Coding/Patch/Verify/Review 成功证据；下一轮是否更早修改、修改是否正确，必须等新的单独授权验收。禁止从“没有 Patch”推断隐藏推理内容或模型主观意图。
