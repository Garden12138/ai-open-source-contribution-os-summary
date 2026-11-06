# Plan15 精确执行预览（尚未批准）

本次仅完成独立Plan/绑定和预览；新模型调用0、执行审批0、Job/Attempt0，未分配新运行预算。Plan11–14历史终态、费用、审批和未知占用不变。

- Plan15 ID：`626be813-5437-4ad3-8a24-e30d12c7b803`
- Plan Hash：`448b0a90cd2c9e4d3dfc14c94de8705ee5a7f2ec89487473397c18dc3e654740`
- Plan Content Hash：`7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed`（与Plan14相同）
- Binding Hash：`2672bf166060bf9ec58eda0392a55ba0d141c02dad8fc9a4853a7d4b1d29e70b`
- **精确预览 Hash：`f7c000560f1e2e1bb9bad33ba20b21c81ceb39bbcfd97f843f1c4e745bc41a6e`**

[完整精确JSON](execution-preview.json) · [Plan](plan.json) · [冻结先决条件](prerequisite-check.json) · [最终只读核验](final-checks.json)

## 冻结输入

目标 `patoles/agent-flow#107`，Issue仍open、正文Hash `5512d263e7d8ab8c10262d9ac2f92b014c05117b070934507fe4b1673680897a`。
Base `3489e17c48985b74fdb42cde122376b1eb12d8a7`；归档 `d269dc46fd82a9ae5d443b95aa389bb6637856f28f5e5cb7da0d0392384b2cdb`，实际877322字节已核Hash。
本仓库分支 `refactor/agent-runtime-v2`，HEAD `6b335c173f2d76f0872d4776f3e5ab87334d5c5f`；254份生产源码逐项对应HEAD与五个现有部署角色。
源码集合 Hash：`31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95`。

- app：`sha256:ba41b3d68e9281a7e5ce965a2d2780fd30b4f997f1ac6b2bc1ffed52b541313a`
- worker：`sha256:2b0e9e989c01562706169f2f5fe6e856f558c8b251ddc3c8926ca9371919f2d2`
- Runner：`sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6`
- Sandbox Policy：`d9edc821c10ef7e84a6b9d29fe8d02f6d1b7927d7d1b47712ebe2a8c527a9086`
- Model Binding：`35237a2ad287596a5cd769489b65b640950210cdff57d4fb7799c2ada7d8a08b`
- Native Transport：`95c9e5e54214196a219e122c5701a200cd3ca3933db03d6ce0b2d2641117ba0f`
- Context：`5f9b41facf3773266149daf5466d275792c6c23af665bcada4162121084fef7c`
- Preflight：`9fb4e9a6729212de492b5543eac09181d47276fae92bee9d54320b5051777e01`
- Execution Revision：`925ae4a1ba64bdd53cfa9c50301d65b0825ac8a44d2d4c6ef77d6f321a22cd87`

实现与独立Review沿用 `deepseek / deepseek-v4-pro / studio-deepseek-thinking-v1`，模型版本 `bb3522b03a444b544c808217ec5ed4b4fa71253591ee8c09e3fc77c57d3a6049`。
Profile输出上限16384 Token、超时300秒、reasoning_effort=high、structured_output=tools、temperature=null；原生Coding每调用最多4096输出Token，并受本轮剩余额度约束。

协议：`native-read-batch-preflight-v3`、`native-read-capacity-v1`、`native-read-capacity-v9`、`source-file-observation-v1`。只读原批最多8-call，子批和Decision最多4-call；保留整批路径/Hash/权限预验。

## 授权范围（需另行一次性确认）

授权后仅允许新鲜OCI准备及原4条Baseline全过 → 原生Investigation与同范围Patch → 最新源码4条Verify → 最多2次有限代码修复 → 独立证据绑定Review → 停在人工最终验收。禁止付费Planner、Push、PR、评论、发布、复用旧审批或恢复旧终态。

8条允许写入路径：

- `extension/src/transcript-parser.ts`
- `extension/src/hook-server.ts`
- `web/hooks/simulation/handle-tool-events.ts`
- `web/hooks/simulation/types.ts`
- `web/lib/agent-types.ts`
- `web/hooks/simulation/handle-tool-events.test.ts`
- `extension/test/transcript-parser.test.ts`
- `extension/test/hook-server.test.ts`

4条批准命令，cwd均为`.`：

| ID | argv |
| --- | --- |
| `root_tests` | `pnpm test` |
| `extension_tests` | `pnpm --dir extension test` |
| `extension_lint` | `pnpm --filter agent-flow run lint` |
| `web_typecheck` | `pnpm --dir web exec tsc --noEmit` |

## 原预算与安全边界

以下为待另行授权的新一轮原定上限，本次未分配：

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

原生每步Job最多1次尝试、300秒；输入投影上限32000、每源码/Artifact页最多16000字节、有界诊断信封8192字节。协议纠偏/Provider重试属于本轮内部限额，不能启动新付费轮次。
独立Review沿用独立证据绑定Job：最多1800000输入字节、每Job最多3次尝试、300秒、每次最多16384输出Token；最多2次代码修复。Review调用与Coding调用分别报告，不隐藏于24次Coding计数中。
沙箱原上限：CPU 2、内存2147483648字节、PID 256、每操作600秒、磁盘4294967296字节、日志4194304字节。无网络、无凭据/模型、只读根文件系统、非root、cap_drop=ALL；仅实现阶段可写批准工作区。依赖准备保持单独受限代理边界。
历史Planning仍64/64次，已用输入1639419/输出166739；3次未知调用的输入1350000/输出40960预留保持不变。

## 离线先决条件与真实观察指标

本轮59项纯离线回归通过，保存Plan12的6/5/5→4+2/4+1/4+1、Plan13的7成功+1not_found、Plan14的8大页→4+4仍有效。归档最终2047通过/78跳过及补充2项、当前平台OCI25项（23实际OCI+2单元）的报告Hash和结果已核验；本次未重跑真实OCI或模型。
OCI完整容量上界228743≤459508，原页68338字节；精确输出按最终回执/原文页/元数据字节计费，原文页按会话/Hash去重。
离线投影31027/32000、内联1024字节；10次Artifact分页加2次尾部read_file可恢复8文件完整Hash。这些是离线可达性证据，不是Plan15真实观察结果。

[真实验收报告契约](acceptance-report-contract.json) Hash `cb40bac7961c49ecfad986d8d4eb685b5c351e342f29b18c13403ef91dc755ec`，已纳入预览Hash。[初始模板](acceptance-report-template.json)保留not_run/null：

| 真实Coding指标 | 记录规则 | 当前值 |
| --- | --- | --- |
| 模型调用数 | 当前Attempt的真实请求/响应，按阶段区分实际、失败、未知；独立Review单列 | 0，未运行 |
| 有效读取 | 按source/file Hash及唯一字节区间计算；区分工具观察与后续真实输入送达 | not_run |
| 重复读取 | 精确/重叠范围、同offset、实际执行/复用分别统计；max_bytes变化不自动算进展 | not_run |
| 首次Patch轮次 | 从1开始的真实Coding请求轮次；提案和OCI成功应用分别绑定证据 | null/not_run |
| Prompt内联源码覆盖 | 每次真实输入的逐文件可用/内联/截断字节及原文引用，累计唯一覆盖 | not_run |
| Artifact分页恢复 | 原文Hash、offset/next_offset、回执与后续真实Prompt，区分页完整和文件完整 | not_run |
| Verify | 最新修改源码的全部4条命令，绑定diff/test/contract/source Hash | not_run |
| 独立Review | 独立ReviewRun/invocation消费最新diff/Verify/test证据，记录费用和结论 | not_run |

not_found只有进入后续真实模型输入才记observed；无对应调用记not_observed。未知或缺失证据不得推断为成功，不自动购买额外覆盖轮次。失败保留已消费费用与实际/未派发/未知回执，并安全停止。
当前只通过静态Preflight，Plan15工具链准备/Baseline/Coding/Verify/Review均未运行。HR4/5及原生Linux amd64门禁仍开放。

## 所需的一次性执行确认文本

以下文本仅供用户之后单独确认；本次没有提交或消费执行审批。

```text
批准执行预览 `f7c000560f1e2e1bb9bad33ba20b21c81ceb39bbcfd97f843f1c4e745bc41a6e`。
仅授权 Plan15 `626be813-5437-4ad3-8a24-e30d12c7b803`，Plan Hash `448b0a90cd2c9e4d3dfc14c94de8705ee5a7f2ec89487473397c18dc3e654740`，Binding Hash `2672bf166060bf9ec58eda0392a55ba0d141c02dad8fc9a4853a7d4b1d29e70b`。
使用该预览冻结的254文件源码 Hash `31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95`、app/worker/Runner镜像 Digest、原 Issue/基线和 deepseek-v4-pro 配置，仅限其中8条写入路径和4条验证命令，进行一次有界真实模型验收及独立 Review。
本轮上限：模型24次、读取64、写入24、检查6、协议纠偏2、Provider重试2、代码修复2、输入200000 Token、输出32000 Token、工具/证据输出500000字节、1800秒；保留预览内全部单次调用、独立Review和沙箱上限，不增加任何容量。
执行前重新核验精确预览、Issue/base、源码/镜像、Plan/Binding、模型/协议、revision/preflight和预算；关键输入变化则停止重新确认。历史Plan11/12/13/14终态、审批、费用、预算和未知占用不变，不复用Plan14审批。
按预览契约保留真实模型调用、有效/重复读取、首次Patch轮次、Prompt源码覆盖、Artifact分页恢复、最新源码Verify及独立Review证据；未观察到的指标如实标注，失败或预算耗尽安全停止。
到人工最终验收前停止。禁止Push、PR、评论、发布、恢复旧终态、退还历史费用或自动另起付费轮次。
```

未发送的执行请求与独立幂等键保存在精确JSON；最终核验之后停止。初次未发送渲染保存在initial-preview.json，最终版仅补齐既有单次调用上限说明，未增加任何预算。
