# 新真实模型验收候选预览（仅文件，未创建新方案/授权/任务）

候选预览 Hash：`5c20cae76a88bdc5f06cf2cb819cf63aa1355993512f6f0f74b30cda47999376`。
[完整冻结JSON](real-model-acceptance-preview.json)记录源码、镜像、模型身份、计划内容、路径、命令和预算。
本轮用户只授权专项修复与离线验收；没有新的付费执行授权。

目标仍为[patoles/agent-flow#107](https://github.com/patoles/agent-flow/issues/107)，基线
`3489e17c48985b74fdb42cde122376b1eb12d8a7`，归档`d269dc46fd82a9ae5d443b95aa389bb6637856f28f5e5cb7da0d0392384b2cdb`。
[只读上游复核](upstream-before-preview.json)确认标题、正文、状态、更新时间与main仍匹配冻结事实。
候选计划内容 Hash `7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed`，与Plan13相同；
Plan13仅作为只读父引用，既有终态、审批和用量不恢复。

最终生产源码252文件 Hash `e78f36d6fc6c16175da08ad77f38ce30ebcb0a8357e9262b1fef7231dc65474f`；新Native Transport Hash
`4c8934a87edf4305dfe614ea25e7665e3b6b3c75bd9051ef627a910bce4ef844`。预检v2与source-file-observation-v1已纳入新协议。
旧Plan13的协议与审批过期，不能用旧执行确认启动新源码。
模型保持现有deepseek-v4-pro与原实现/Review Profile；模型版本、Binding、Policy与Runner Digest均在JSON中精确冻结。

待另行确认的独立额外轮次仍限：模型调用24、输入200000/输出32000 Token、读取64、写入24、检查6、
协议纠偏2、Provider重试2、代码修复2、输出500000字节、1800秒。
独立Review仍为输入1800000字节、最多3个Job尝试、300秒/Job、单次输出16384 Token。
没有分配任何额度，没有增加/退还旧预算；Plan13的12次读取、Planning64/64及三个未知占用原样保留。

候选仅允许原8条写入路径：

- `extension/src/transcript-parser.ts`
- `extension/src/hook-server.ts`
- `web/hooks/simulation/handle-tool-events.ts`
- `web/hooks/simulation/types.ts`
- `web/lib/agent-types.ts`
- `web/hooks/simulation/handle-tool-events.test.ts`
- `extension/test/transcript-parser.test.ts`
- `extension/test/hook-server.test.ts`

候选仅允许原4条命令，cwd均为`.`：

- `pnpm test`
- `pnpm --dir extension test`
- `pnpm --filter agent-flow run lint`
- `pnpm --dir web exec tsc --noEmit`

拟执行链：新环境准备及四项Baseline → 真实原生Investigation → 同范围Coding/Patch → 当前源码全部四项Verify
→ 最多两次代码修复 → 独立证据绑定Review → 等待人工最终验收。
批准缺失可返回有OCI证据的not_found并继续调查/授权创建；安全/Hash/来源/未知错误仍停止。
若真实模型不请求该类缺失读取或批次，标记真实覆盖not_observed，不自动购买重跑来凑覆盖。
任一冻结输入变化、未知结果、真实失败或预算耗尽均按原策略安全停止。
不授权Push、PR、评论、发布、扩大范围或自动另起付费轮次。

当前严格禁止自动创建Plan14，因此新Plan ID、Record/Binding/授权Hash和Attempt ID均为not_created，
execution_request_body为null；没有虚构可执行审批。本文件不是实际执行确认。
下一步只有在人类另行明确授权同内容新方案/新协议绑定后，才可物化方案并提供带实际ID、Hash、
Preflight与Revision的精确执行预览；随后仍需新的单次执行确认才能调用付费模型。

当前模型调用新增0、Plan14新增0、旧会话恢复0、Push/PR/评论0。
原真实HR4/5与Linux amd64门禁仍未通过；本候选预览不改变其状态。
