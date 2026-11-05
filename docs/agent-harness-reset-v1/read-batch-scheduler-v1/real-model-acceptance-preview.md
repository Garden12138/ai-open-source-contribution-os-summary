# 独立真实模型验收预览（未授权、未执行）

预览 Hash：`a7b2f501f7320db06810b37c04816b0d973e3aaee595c8cd00aac94d2c402291`。
完整目标、方案内容、模型身份、镜像、路径、命令与预算见
[JSON 预览](real-model-acceptance-preview.json)。这是文件预览，未创建方案、审批、Job或模型请求。

目标仍为 [patoles/agent-flow#107](https://github.com/patoles/agent-flow/issues/107)，固定基线
`3489e17c48985b74fdb42cde122376b1eb12d8a7`，Snapshot
`ba5e120b-0d3f-4f6c-8ddd-dbee4fe8ef84`。
候选方案内容与Plan12完全相同，内容 Hash
`7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed`；
Plan12只作为只读父引用，原终态、审批与用量不恢复。公开只读检查确认正文与main仍匹配，实际执行前必须重查。

当前实现源码 Hash `9601fd8b2ca3c1e7b605633ba7a5e67491591d5a61ff05bf1de3590cca85860d`，
250个生产文件已逐一验证并部署。原生新协议 Hash与全部镜像Digest在JSON中冻结。
协议只允许全部验证通过的5–8个read_file形成最多4-call的执行子批次。
沙箱Policy、Runner镜像、环境引擎与网络边界均保持原值。
实现及独立Review使用原DeepSeek/deepseek-v4-pro配置，模型版本
`bb3522b03a444b544c808217ec5ed4b4fa71253591ee8c09e3fc77c57d3a6049`。
Profile与模型Binding的完整值在JSON中冻结。

候选额外一轮仍限：模型调用24、读取64、写入24、检查6、协议纠偏2、Provider重试2、
代码修复2，输入Token 200000、输出Token 32000、工具输出500000字节、1800秒。
这是有待新授权的独立额外用量；不会给旧会话退还、追加或重置预算。
独立Review保持原单次输入1800000字节、最多3个Job尝试、300秒/Job、16384输出Token的单独限制。
历史Planning已用64/64调用和三个未知占用原样保留，不调用付费Planner生成相同内容。

只允许以下8条写入路径：

- `extension/src/transcript-parser.ts`
- `extension/src/hook-server.ts`
- `web/hooks/simulation/handle-tool-events.ts`
- `web/hooks/simulation/types.ts`
- `web/lib/agent-types.ts`
- `web/hooks/simulation/handle-tool-events.test.ts`
- `extension/test/transcript-parser.test.ts`
- `extension/test/hook-server.test.ts`

只允许以下4条检查命令，工作目录均为`.`：

- `pnpm test`
- `pnpm --dir extension test`
- `pnpm --filter agent-flow run lint`
- `pnpm --dir web exec tsc --noEmit`

拟验收链为重新准备与四项Baseline → 原生调查 → 同范围真实Patch → 最新源码四项Verify
→ 最多两次代码修复 → 独立证据绑定Review → 等待人工验收。
读取批次成功不算Coding完成。如果模型没有发出5–8个read_file的提案，必须如实标记该真实
分批覆盖为`not_observed`，不能自动付费重试来凑覆盖。
任何阶段失败、未知结果、输入变化或预算耗尽均安全停止，保存实际回执。

当前不能给出可执行请求体：`freeze_spec`要求新的工具协议绑定，而Plan12冻结的是旧协议；
用户又明确禁止自动创建Plan13。因此新方案ID、Binding/授权/Attempt Hash与新环境验证契约均为
`not_created`，JSON中的`execution_request_body`为null，没有虚构标识或可执行审批。

下一步需人类明确决定是否允许按本预览的相同内容创建新方案绑定。获得该授权后，先物化方案、
校验冻结内容，提供带真实ID/Hash与当前Revision的精确执行预览；新的单次执行确认到达后才能付费派发。
本预览本身不授权创建Plan13或模型调用。实际执行预览的任一冻结输入变化都必须重新展示并确认。

本轮未付费调用模型、未生成Plan13、未Push或创建PR。真实HR4/5以及原生Linux amd64门禁仍未通过。
