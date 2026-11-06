# Plan13 精确执行预览：等待新的执行授权

本次人类授权仅创建新Plan和必要绑定。Plan13已保存，未批准、未启动；没有付费模型、
Coding、Verify、Review、新执行Attempt或第三方写入。
[完整机器预览](execution-preview.json)冻结下列实际标识及全部执行条件，未来请求体仅保存为文件，未发送。

| 字段 | 实际值 |
| --- | --- |
| 分支 / HEAD | `refactor/agent-runtime-v2` / `e5cc92a8e3154acf79025cb0e235357b514024fd` |
| Task ID | `f0e017fa-d2a6-4eb8-8a94-32bb1d29586c` |
| Plan13 ID | `b3c5acdd-bb80-461f-8dd9-5af5ca687280` |
| Plan Record Hash | `1831436b3da8904d514205c2c877b9019c44f2b88aaf2cb31f05ce4b16583865` |
| Plan Content Hash | `7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed` |
| Plan Binding Hash | `bcbf07e445030ca723db34c272feecfb05776de94496cc7fe86a3f0e434ccbe2` |
| 精确预览 Hash | `c628ea479f2647a18c3188575ed3bf7fbc17d611a5c58a7c55a43519fdfdf42c` |
| 原候选预览 Hash | `a7b2f501f7320db06810b37c04816b0d973e3aaee595c8cd00aac94d2c402291` |
| 模型 Binding Hash | `35237a2ad287596a5cd769489b65b640950210cdff57d4fb7799c2ada7d8a08b` |
| Native Transport Hash | `bc1d221e9b696c88d35a36c4862e143c148636bc83eeae3b85ee08475fe10260` |
| 生产源码 Hash（250文件） | `9601fd8b2ca3c1e7b605633ba7a5e67491591d5a61ff05bf1de3590cca85860d` |
| 沙箱 Policy Hash | `d9edc821c10ef7e84a6b9d29fe8d02f6d1b7927d7d1b47712ebe2a8c527a9086` |
| Runner镜像 | `sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6` |
| 新验证环境契约 Hash | `e5d143bf2b6a5ddec2446df574a8d3324cd4287b73c999cf0be56239bb5d09de` |
| 静态 Preflight Hash | `0c9746b76da8b221b853ae7e38b84491cfed3cfd243f51fbe72afcf3cd809b76` |
| Execution Revision | `123f665bfe9ed6f2d5b384a7c947c420cbe5b9dbe7beaf0c857549a5cc9c3a31` |

Plan13为Plan12（`5c6df081-f469-44be-a2de-81fec47b2289`）的同内容子版本；
父Record Hash为`c91a11677f13080efba7b36136bcf2eccffb71d0bf86b0fee421e2bf6d256f1b`。
新Plan Record和Binding因新状态版本/原生协议而不同，内容、路径、命令、模型和预算均没有改变。
新的执行授权Hash、Attempt ID及实际Diff/Test/Review Hash尚未创建，不能伪造为已审批或已验收。

## 上游、模型与范围

[patoles/agent-flow#107](https://github.com/patoles/agent-flow/issues/107)仍为open；只读重新核对正文Hash
`5512d263e7d8ab8c10262d9ac2f92b014c05117b070934507fe4b1673680897a`，与原冻结记录一致。
main与固定基线仍为`3489e17c48985b74fdb42cde122376b1eb12d8a7`，见[上游校验](upstream-check.json)。

实现和独立Review均保持`deepseek/deepseek-v4-pro`，适配器
`studio-deepseek-thinking-v1`，模型版本
`bb3522b03a444b544c808217ec5ed4b4fa71253591ee8c09e3fc77c57d3a6049`。
Profile标识与模型身份完整值在JSON中冻结。

仅允许写入原8条路径：

- `extension/src/transcript-parser.ts`
- `extension/src/hook-server.ts`
- `web/hooks/simulation/handle-tool-events.ts`
- `web/hooks/simulation/types.ts`
- `web/lib/agent-types.ts`
- `web/hooks/simulation/handle-tool-events.test.ts`
- `extension/test/transcript-parser.test.ts`
- `extension/test/hook-server.test.ts`

仅允许原4条命令，工作目录均为`.`：

- `pnpm test`
- `pnpm --dir extension test`
- `pnpm --filter agent-flow run lint`
- `pnpm --dir web exec tsc --noEmit`

## 待确认预算与执行条件

| 独立额外一轮上限 | 值 |
| --- | --- |
| 模型调用 / 读取 / 写入 / 检查 | 24 / 64 / 24 / 6 |
| 协议纠偏 / Provider重试 / 代码修复 | 2 / 2 / 2 |
| 输入 / 输出Token | 200000 / 32000 |
| 工具输出 / 会话时间 | 500000字节 / 1800秒 |
| 独立Review原单独上限 | 输入1800000字节、最多3个Job尝试、300秒/Job、单次输出16384 Token |

以上为原候选中的相同上限，尚未分配或批准。未来的同意必须明确覆盖这一独立额外轮次的费用，
不会重置、退还或扩充Plan11/12已用量、失败预留与未知占用。Planning仍已用64/64调用，
三个未知占用保留；本次同内容创建没有调用付费Planner。

获得新的精确执行确认后，允许的链为：重新准备 → 四项修改前Baseline → 原生调查及真实Patch
→ 最新源码的四项批准Verify → 最多两次代码修复 → 独立证据绑定Review → 等待人工验收。
当前只有静态预检ready，新Plan的toolchain/Baseline仍为unverified，不能引用Plan12旧结果冒充新结果。

纯读取批次仍要求5–8个调用全部验证与OCI预检通过，才能形成最多4-call的持久化子批次。
失败保留真实前缀和未知结果并停止剩余调用；子批次之间不请求模型Decision。
若真实模型没有提出该类批次，应标记分批覆盖`not_observed`，不自动购买重试来凑覆盖。
所有冻结输入和当前Revision必须在派发前重查；变化时停止并重新展示预览。
不得复用旧终态/审批、扩大路径/命令/网络/预算、自动另起一轮、Push、PR、评论或发布。

## 已完成的只读校验

[独立预览审计](preview-integrity-audit.json)核对实际Plan与Binding、原Hash链、全部旧行、
模型/预算/安全策略、未发送的请求Schema和当前Revision；结果通过。
[旧终态审计](legacy-terminal-audit.json)验证1946条任务事件和1581个Artifact，
Plan11/12终态检查点及其Usage未变。只追加1个Plan、1个状态版本与4条工作台事件；
审批仍10、模型启动仍86、无新Job、Attempt、Patch或预算分配。

本轮没有修改生产源码或重新部署，没有重跑付费或OCI验收；复用先前已记录的同Hash源码测试成绩。
原HR4/5与原生Linux amd64门禁仍未通过。本次也未提交、Push、创建PR或发表评论。

## 下一步所需的精确确认

以下文字是供人类将来明确发送的确认模板，文件本身不构成执行授权：

> 我批准执行预览 `c628ea479f2647a18c3188575ed3bf7fbc17d611a5c58a7c55a43519fdfdf42c`，
> 仅限Plan13 `b3c5acdd-bb80-461f-8dd9-5af5ca687280`、Plan Hash
> `1831436b3da8904d514205c2c877b9019c44f2b88aaf2cb31f05ce4b16583865`、Binding Hash
> `bcbf07e445030ca723db34c272feecfb05776de94496cc7fe86a3f0e434ccbe2`。
> 我同意预览列明的一次额外有界真实模型验收及原单独Review上限、原8条路径和4条命令；
> 保留旧预算和未知占用，不授权Push、PR、评论、发布或失败后自动另起付费轮次。

未来请求使用独立幂等键`harness-rb-plan13-execute-once`；仅在收到新的明确人类确认、
重新核对预览所有条件后，才可发送JSON中冻结的`future_execution_request`。
