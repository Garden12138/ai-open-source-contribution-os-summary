# Plan14 精确执行预览 — 等待新的执行授权

本次只追加同内容独立Plan与最新协议绑定，未批准、未执行、未分配新预算。模型调用新增0，Job/Attempt新增0。
Plan11/12/13终态、历史审批、预算与未知占用均保留；不恢复Plan13。此预览不是执行审批。

- Plan14 ID: `e8503bc5-9b61-4e51-95c6-c4eb33bb919e`
- Plan Hash（record）: `1006e34393baf6a239c3f4e001af5726baaf3c69e00065a25d5934531b693d06`
- Plan Content Hash: `7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed`（与Plan13完全相同）
- Binding Hash: `6222c965647cdf54e039dad1c41544cdd5dd54245e0b59c6ee528bbd584c36bf`
- **预览 Hash: `3825f2d60954527af8276556fdff71235a185554177a3d5159da2aed06769250`**
- 上一候选 Hash: `5c20cae76a88bdc5f06cf2cb819cf63aa1355993512f6f0f74b30cda47999376`
- [完整精确JSON](execution-preview.json)、[原生Plan](plan.json)、[独立只读校验](preview-integrity-audit.json)

## 冻结输入与执行前提

目标 `patoles/agent-flow#107`，同一冻结Issue正文/标题与main基线。
Base `3489e17c48985b74fdb42cde122376b1eb12d8a7`；归档 `d269dc46fd82a9ae5d443b95aa389bb6637856f28f5e5cb7da0d0392384b2cdb`。
分支 `refactor/agent-runtime-v2`，HEAD `e5cc92a8e3154acf79025cb0e235357b514024fd`，工作区修复尚未提交。
实际源码252项 Hash `e78f36d6fc6c16175da08ad77f38ce30ebcb0a8357e9262b1fef7231dc65474f`；执行前必须逐项检查，不能仅凭HEAD判定。

- app: `sha256:d92bfb14dd645a3f0bf05a33f9910becebb2c7d1febb5f9f85bed6b53dfb588c`
- worker: `sha256:1d5c9749654e8de53ca876762c56ea015f27aac31bf63def9a8b64333b5fa833`
- Runner: `sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6`
- Sandbox Policy: `d9edc821c10ef7e84a6b9d29fe8d02f6d1b7927d7d1b47712ebe2a8c527a9086`
- Model Binding: `35237a2ad287596a5cd769489b65b640950210cdff57d4fb7799c2ada7d8a08b`
- Native Transport: `4c8934a87edf4305dfe614ea25e7665e3b6b3c75bd9051ef627a910bce4ef844`
- Context: `5f9b41facf3773266149daf5466d275792c6c23af665bcada4162121084fef7c`
- Preflight: `b99f6c9936d84302eef470b6e7c53d4d6e937e37bb46723385378963a8b61480`
- Execution Revision: `97bb32eb162cfeba9ba1c12a0da7b14a7e25233146b483aed1e4bc4cd82e4928`

实现与独立Review均沿用 `deepseek / deepseek-v4-pro / studio-deepseek-thinking-v1`；
模型版本 `bb3522b03a444b544c808217ec5ed4b4fa71253591ee8c09e3fc77c57d3a6049`。
新绑定使用native-source-observations-v8、native-read-batch-preflight-v2和source-file-observation-v1。
原始只读批次最多8、执行子批次最多4，全批安全预验，子批次之间无模型决策。
批准且OCI证实缺失的路径返回not_found，无伪造文本/hash；真正安全错误仍阻断。

当前静态Preflight ready，工具链准备和Baseline仍为unverified。
授权后须新鲜OCI准备和4条Baseline全过，才进入Investigation→Coding→最新源码Verify→独立Review→等待人工验收。
执行前重核全部冻结输入、revision和preflight；变化则停止重新预览。无付费Planner或额外自动验收轮次。

## 仅限原8条写入路径

- `extension/src/transcript-parser.ts`
- `extension/src/hook-server.ts`
- `web/hooks/simulation/handle-tool-events.ts`
- `web/hooks/simulation/types.ts`
- `web/lib/agent-types.ts`
- `web/hooks/simulation/handle-tool-events.test.ts`
- `extension/test/transcript-parser.test.ts`
- `extension/test/hook-server.test.ts`

## 仅限原4条验证命令

| ID | argv | cwd |
| --- | --- | --- |
| `root_tests` | `pnpm test` | `.` |
| `extension_tests` | `pnpm --dir extension test` | `.` |
| `extension_lint` | `pnpm --filter agent-flow run lint` | `.` |
| `web_typecheck` | `pnpm --dir web exec tsc --noEmit` | `.` |

## 待新授权的一轮预算（上限不变，本次未分配）

| 项目 | 上限 |
| --- | ---: |
| `model_calls` | 24 |
| `reads` | 64 |
| `writes` | 24 |
| `checks` | 6 |
| `protocol_repairs` | 2 |
| `provider_retries` | 2 |
| `code_repairs` | 2 |
| `input_tokens` | 200000 |
| `output_tokens` | 32000 |
| `output_bytes` | 500000 |
| `seconds` | 1800 |

独立Review保留原独立证据绑定Job：输入最多1,800,000字节、每Job最多3次尝试、300秒、单次输出16384 Token。
最多2次代码修复；协议纠偏2、Provider重试2均是本轮内部原有限额，不能另起付费轮次。
安全/未知/Hash冲突或预算耗尽安全停止；保留已消费用量和真实回执，不退款、不自动恢复终态。
历史Planning仍64/64，输入1,639,419、输出166,739，未知3次预留输入1,350,000/输出40,960不变。

## 真实验收报告要求

[机器可读报告契约](acceptance-report-contract.json) Hash `7e975bd947b051e09b3660a312556edd2ececd3f33d71eba81d25e117612df4e`已纳入预览Hash。
[报告初始模板](acceptance-report-template.json)中的真实结果均为not_run，不用离线结果冒充真实验收。

- 按真实Provider ID/参数/顺序保存每个请求、全批预检、子批次Checkpoint/幂等ID及实际/复用/未派发回执。
- not_found生成与真实模型观察分开记录：只有下一真实请求的prompt Artifact含对应回执才记observed。
- 没有相关缺失读取时记not_observed，不买另一轮补覆盖；终止于下一请求前也不能声称模型看到了。
- Patch提案与OCI成功应用分开，记录范围、diff、snapshot和应用前后源码Hash。
- Verify必须消费最新源码并列出全部4条命令；后续修改使旧Verify失效。
- Review必须有独立ReviewRun、invocation及diff/test/verify绑定Hash，编码器总结不替代独立Review。
- 失败明确记录阶段、reason_code、已执行/未派发/未知调用、预算和可恢复性；需要新授权的终态不自动恢复。

离线先决条件四项通过；冻结全量2025通过/77跳过与macOS arm64/OrbStack OCI24通过证据已核Hash。
本次还补做了纯离线新文件→源码变化→旧Verify失效→独立Review→人工门禁的确定性组合核对。
原生Linux amd64未运行，HR4/HR5真实交付门禁仍开放；本次准备不宣称通过。

## 下一步所需精确确认文本

当前只保存未发送的ExecutePlanRequest；以下文本需要用户另行明确批准后才能发送。

```text
批准执行预览 `3825f2d60954527af8276556fdff71235a185554177a3d5159da2aed06769250`。
仅授权 Plan14 `e8503bc5-9b61-4e51-95c6-c4eb33bb919e`，Plan Hash `1006e34393baf6a239c3f4e001af5726baaf3c69e00065a25d5934531b693d06`，Binding Hash `6222c965647cdf54e039dad1c41544cdd5dd54245e0b59c6ee528bbd584c36bf`。
使用预览冻结的现有 deepseek-v4-pro 配置，仅限其中8条写入路径和4条验证命令，进行一次额外有界真实模型验收及独立 Review。
保持预览全部模型调用、Token、工具、时间、重试和修复上限；历史 Plan11/12/13 预算与终态不变。
执行前重新核验精确预览、源码、镜像、Issue、绑定和预算；关键输入变化则停止重新确认。
完整保留真实模型请求、读取批次、not_found是否进入真实模型输入、Patch应用、最新源码Verify和独立Review证据；失败或耗尽预算则安全停止。
到人工最终验收前停止。禁止Push、PR、评论、发布、恢复旧终态或自动另起付费轮次。
```

禁止Push、PR、评论、发布、预算扩张、复用旧审批或另起付费轮次。本次准备到此停止。
