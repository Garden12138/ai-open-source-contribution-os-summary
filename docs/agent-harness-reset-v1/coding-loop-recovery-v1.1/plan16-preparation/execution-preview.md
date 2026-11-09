# Plan16 独立冻结精确执行预览（尚未批准）

**冻结准备 Go；真实付费执行 No-Go，等待独立审查和新的精确一次性授权。**
本次真实 Provider/审批/Coding Job/Review Job/Attempt/新运行预算均为0。Plan16已独立创建，旧Plan11–15终态、审批、费用与未知占用不变；没有恢复Plan15。

[完整精确JSON](execution-preview.json) · [冻结清单](frozen-inputs.json) · [Plan16](plan.json) · [预算与费用风险](budget-and-risk.json) · [验收契约](acceptance-report-contract.json) · [最终审计](final-checks.json)

## 精确标识与Hash

| 项目 | 值 |
| --- | --- |
| Plan ID | `6944ba99-2de3-492f-a687-0c71e804d94a` |
| Plan Hash | `aa03690633bcebc8e9c5346f5e930aa2c09a56aa9636a14b2528bb7fabeb74ca` |
| Plan Content Hash | `7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed` |
| Binding Hash | `e97e605fc33502b47241c13e0930865dc8a0ffe33fd76a31ec26d1a989dc7b38` |
| 冻结输入 Hash | `9ba3758151c2cae54c2ce85bc86111ec57c72bc9add3cb1abcb22e3eaf2154cb` |
| 精确预览 Hash | `dab070664ebed9d0451414078c1648ffa0fed5c64e63242dec2ed9a0e33504ce` |
| Execution revision | `c34e29efb4b361d47b1be7345f84f825fa66f9a8251b0c6e651cc3da2f96408a` |
| Preflight | `ed34d5d1ae4306d1037836ac31e9b85cdd95ba04a41ff1aaf2e753f158c2b376` |
| Context | `5f9b41facf3773266149daf5466d275792c6c23af665bcada4162121084fef7c` |
| Snapshot | `5db57facb4415022a4a1b0f682c10d065469ae3ae87d8b81c7298c589af17e74` |
| 255 文件源码 | `627a3689ca6647f8747796469f7e93fddff23a8445557066a7cae061e3346d47` |
| app 镜像 | `sha256:c9c07093cc6d8cd001c3fbd3d39a819580da5ab4e16b7c0990bfcd0f13d7917f` |
| Sandbox Worker 镜像 | `sha256:1c82d9a6e0a0143d13a1ba3ab2728cec19472130f28a346172c87daacbbe9476` |
| Runner | `sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6` |
| Sandbox Policy | `d9edc821c10ef7e84a6b9d29fe8d02f6d1b7927d7d1b47712ebe2a8c527a9086` |
| Native transport | `b661a6b2bc3b7c5e6bd83a307024e26e529f82625bd9aba263c95cb2b48d9174` |
| Native schema | `52999661311852705547ff40da95c4d33cf16c61fc03ed7deff51b99418f7226` |
| Model binding | `35237a2ad287596a5cd769489b65b640950210cdff57d4fb7799c2ada7d8a08b` |
| Issue 正文 | `5512d263e7d8ab8c10262d9ac2f92b014c05117b070934507fe4b1673680897a` |
| base SHA | `3489e17c48985b74fdb42cde122376b1eb12d8a7` |
| 归档 | `d269dc46fd82a9ae5d443b95aa389bb6637856f28f5e5cb7da0d0392384b2cdb` |
| 只读先决核验 | `3f6e42e7aacb4e55a751f386a8d86247346d0e6049b442c4651f4cf80691cf01` |
| 验收契约 | `be6425dcf778a436423bff76dcda56590c6cda640841573e8a8b35bf812a9f9d` |
| 预算/费用风险 | `38bece48c114352e382b71781ac57a98286e0ababdb6143fa9cb895296f91688` |
| 备份 SHA256 | `9444a3c9c4fc20b8155a5b4f30e5f2b6c2629eefcc6b532f769d312ba6b4b0a3` |

本仓库分支 `refactor/agent-runtime-v2`，核验HEAD `b33349d684b1705cc9e1d30ee4aa7d71fd32e740`，生产源码提交 `bd3f8927bfed1a280b8d8ae15cdd446d30357917`。工作区、该提交和五角色255文件逐项一致。
镜像Digest为本地不可变Docker Image ID，没有发布Registry镜像。Prompt为`native-coding-loop-recovery-v11`。
业务Binding新建并绑定Plan/context/Native协议；完整源码、镜像及其余输入由独立冻结清单和本预览Hash精确绑定。现有ExecutePlanRequest没有新增源码Hash字段，执行者必须在发送前重核完整冻结清单，不能只依赖API静态ready。
Plan16以Plan15为不可变父版本引用，业务内容Hash保持原目标；Plan ID、Plan Hash、状态版本和Binding均为新记录。父版本引用不携带旧审批或执行权限。

## 上游、路径、命令与验收目标

现有容器只读代理核验[Issue #107](https://github.com/patoles/agent-flow/issues/107)仍open、无人分配、未锁定；仓库公开且未归档/禁用。标题、正文、更新时间`2026-10-29T22:23:57Z`和main/base均未变。实际归档877322字节Hash一致，归档根目录`agent-flow-3489e17c48985b74fdb42cde122376b1eb12d8a7`与base一致，未执行仓库代码。

原8条允许写入路径：

- `extension/src/transcript-parser.ts`
- `extension/src/hook-server.ts`
- `web/hooks/simulation/handle-tool-events.ts`
- `web/hooks/simulation/types.ts`
- `web/lib/agent-types.ts`
- `web/hooks/simulation/handle-tool-events.test.ts`
- `extension/test/transcript-parser.test.ts`
- `extension/test/hook-server.test.ts`

原4条批准命令；修改后必须在最新源码上全部Verify，修改前Baseline不能代替：

| ID | argv | cwd |
| --- | --- | --- |
| `root_tests` | `pnpm test` | `.` |
| `extension_tests` | `pnpm --dir extension test` | `.` |
| `extension_lint` | `pnpm --filter agent-flow run lint` | `.` |
| `web_typecheck` | `pnpm --dir web exec tsc --noEmit` | `.` |

原Issue验收目标原样保留：

- 同一 agent 对同一工具发起的多次并行调用（不同 tool_use_id、不同参数/文件）均被独立记录，不再被去重丢弃。
- fileAttention 中每个被读取的不同文件都有独立条目。
- 乱序到达的 tool_call_end 按 tool_use_id 精确完成对应那条调用，不影响同工具的其他运行中调用。
- 同一 tool_use_id 的重复投递（Hook Server 与 Session Watcher 竞态）仍被去重为单条调用。
- 现有测试与类型检查全部通过；对不含 tool_use_id 的旧 replay 数据保持兼容回退。

## 原预算与费用风险

以下为等待独立授权的一次有界运行原上限，本次未分配，未增加容量：

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

deepseek-v4-pro，reasoning_effort=high，temperature=null、structured_output=tools；原Profile输出16384/超时300秒，Coding每次输出min(4096,剩余额度)。模型配置和版本均在冻结JSON内，不混入实验。
单步Job最多1次尝试/300秒；输入投影32000；单页16000字节；原只读批8-call、子批及Decision4-call；有界失败信封8192字节。所有读取/写入/检查/协议纠偏/Provider重试/修复/证据/时间均受原总额约束。

6轮未成功Patch后纠偏；8轮后判断是否具备一次条件机会：第8轮实际OCI新观察、相关路径、当前source Hash和真实回执必须匹配，新增源码还必须确实出现在第9轮Prompt。跨Worker/修复Session同一授权只能领取一次；未知调用或只有重复读取均不能领取。第9轮无真实OCI成功应用Patch，第10次请求前停止。
该机会最多消耗原额度内1次调用、32000输入上界及4096输出；派发前核验精确Prompt及剩余各维预算。输出不足4096时按剩余收窄，低于128或输入/时间等不足则不派发。要保留完整32000/4096，之前累计输入最多168000、输出最多27904（含保留预留）。9×4096=36864>32000，9×32000=288000>200000，不能保证走满9轮或完整流水线；绝不加预算覆盖。

独立真实Review沿用独立证据绑定Job：输入证据最多1800000字节、最多3次尝试、每Job300秒、Profile每次最多16384输出Token；最多2次代码修复可能产生新证据与再次Review，仍受原有状态机约束。Review费用在Coding24次之外单列，不隐藏为免费步骤。
本地模型配置未冻结可验证货币单价或货币硬上限，所以不编造人民币/美元总价。费用为Coding及每次独立Review的实际输入/输出乘Provider单价，失败/未知请求也可能计费；未知结果保持预留并阻断，不退款或重试未知操作。high reasoning可耗尽4096且没有final_text/tool_calls，Plan15已真实发生，不能因此提高容量或降低reasoning。

沙箱保持CPU2、2GiB内存、PID256、每操作600秒、4GiB磁盘、4MiB日志，非root、断网、无凭据/真实模型、只读根、drop ALL/no-new-privileges；只有实现阶段允许批准工作区写入。依赖准备保持独立且仅允许受限包代理。五角色原挂载/权限/网络/资源逐字段匹配保留旧容器；无重新部署。

历史Planning仍64/64、输入1639419/输出166739，3次未知输入1350000/输出40960预留不变。本次不调用Planner。
下表为历史账本已计费/预留数，不冒充Provider实际Token，原事件保留两者区分：

| Plan | 终态 | model_calls | input_tokens | output_tokens | output_bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| Plan11 | blocked | 14 | 171256 | 12756 | 216162 |
| Plan12 | blocked | 8 | 137303 | 15038 | 87173 |
| Plan13 | blocked | 2 | 15882 | 1164 | 24369 |
| Plan14 | blocked | 3 | 46008 | 4560 | 35526 |
| Plan15 | blocked | 15 | 176505 | 17211 | 426518 |

## 真实验收指标与停止点

| 指标 | 必须保留的真实证据 | Plan16当前值 |
| --- | --- | --- |
| 首次apply_patch提案 | 1起算真实Coding轮次、response/call Hash | null/not_run |
| 首次OCI成功应用Patch | 独立提案/应用轮次、receipt/source/diff Hash | null/not_run |
| 当前工作区唯一源码读取 | 按当前source/file Hash及UTF8唯一区间，not_found不计源码 | not_run |
| 真实Prompt送达覆盖 | 每次实际请求的完整Prompt Artifact、逐文件区间/截断/恢复引用；区分工具返回与确认送达 | not_run |
| 重复读取与Artifact恢复效率 | 同offset/重叠/复用、请求和实际字节、原文分页连续性、后续Prompt及重建Hash | not_run |
| 6+8+1 | 第6轮纠偏、第8轮来源/新增区间/预算判断、第9轮真实Prompt与机会消费；第10次前停止证据 | not_run |
| high截断 | finish_reason、实际final_text/tool_calls/Token；不推断隐藏reasoning | not_observed |
| 最新源码Verify | 原4条命令真实结果及最新source/diff/tests/contract Hash | not_run |
| 独立真实Review | 独立ReviewRun/invocation、最新diff/Verify/tests、身份/结论/发现/单列费用 | not_run |
| 最终人工门禁 | 独立Review之后停止，等待人的最终验收 | not_run |

读取成功、测试替身通过或调用次数增加均不算真实Coding成功。没有实际发生的指标保持null/not_observed/not_run，不自动购买下一轮覆盖。完整计数/回执/费用契约见JSON。
真实执行若获另行授权，顺序为新鲜OCI准备及4条Baseline全过、同范围Coding/Patch、最新源码4项Verify、原预算内有限修复、独立真实Review，然后停于最终人工验收。任何关键输入变化、未知结果、预算不足或门禁失败均停止。

## 核验、恢复和限制

全部45表旧行、11检查点、2551旧事件前缀、1802Artifact原字节/Hash及Plan11–15链均核验一致。仅追加1个Plan、必要状态版本与4个准备事件；任务事件2551→2555，模型启动106、执行授权13、Plan审批13、活动业务Job0不变。
备份权限0700/0600、摘要、完整性/外键/45表Hash通过；与已归档旧镜像读取验收一致。本轮未执行完整回滚恢复；新增准备记录后禁止用旧备份覆盖当前库。旧五容器stopped且旧镜像仍可用。
原代理宿主密钥文件缺失，使用现有容器配置完成同一只读代理核验，密钥未导出或重建。公共API备用探针504没有作为通过证据。首次核验工具错误假设归档命名和checkpoint字段；归档原Hash已匹配，纠正为实际规范名和state字段后全门禁重跑通过，未绕过产品门禁。
离线证据为此前相同部署镜像50/53、真实受限OCI28+2unit和7组信封。本轮只读复算原容量证明228743≤459508；没有重跑真实OCI、Coding或Review。确定性替身Review不能证明真实Review。HR4/HR5、P5-G06和Linux amd64仍开放。

## 唯一待单独确认的执行文本

下文仅供之后独立审查确认，本次没有提交、发送或消费该授权。完整原字节保存于[confirmation-text.txt](confirmation-text.txt)，原Plan15文本绝不可复用。

```text
批准且仅一次执行 Plan16 精确预览 `dab070664ebed9d0451414078c1648ffa0fed5c64e63242dec2ed9a0e33504ce`。
仅授权独立 Plan16 `6944ba99-2de3-492f-a687-0c71e804d94a`，Plan Hash `aa03690633bcebc8e9c5346f5e930aa2c09a56aa9636a14b2528bb7fabeb74ca`，Plan Content Hash `7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed`，新 Binding Hash `e97e605fc33502b47241c13e0930865dc8a0ffe33fd76a31ec26d1a989dc7b38`，冻结输入 Hash `9ba3758151c2cae54c2ce85bc86111ec57c72bc9add3cb1abcb22e3eaf2154cb`，预算/费用风险说明 Hash `38bece48c114352e382b71781ac57a98286e0ababdb6143fa9cb895296f91688`。
源码提交 `bd3f8927bfed1a280b8d8ae15cdd446d30357917`，255 文件集合 Hash `627a3689ca6647f8747796469f7e93fddff23a8445557066a7cae061e3346d47`；app `sha256:c9c07093cc6d8cd001c3fbd3d39a819580da5ab4e16b7c0990bfcd0f13d7917f`，Sandbox Worker `sha256:1c82d9a6e0a0143d13a1ba3ab2728cec19472130f28a346172c87daacbbe9476`，Runner `sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6`，Sandbox Policy `d9edc821c10ef7e84a6b9d29fe8d02f6d1b7927d7d1b47712ebe2a8c527a9086`。
Native transport `b661a6b2bc3b7c5e6bd83a307024e26e529f82625bd9aba263c95cb2b48d9174`，Schema `52999661311852705547ff40da95c4d33cf16c61fc03ed7deff51b99418f7226`，Prompt `native-coding-loop-recovery-v11`；模型绑定 `35237a2ad287596a5cd769489b65b640950210cdff57d4fb7799c2ada7d8a08b`，deepseek-v4-pro、reasoning_effort=high，单次 Coding 输出最多4096且受剩余额度限制，模型配置不变。
目标 patoles/agent-flow#107，base `3489e17c48985b74fdb42cde122376b1eb12d8a7`，Issue 正文 `5512d263e7d8ab8c10262d9ac2f92b014c05117b070934507fe4b1673680897a`，归档 `d269dc46fd82a9ae5d443b95aa389bb6637856f28f5e5cb7da0d0392384b2cdb`，Context `5f9b41facf3773266149daf5466d275792c6c23af665bcada4162121084fef7c`；仅限预览中的原8条写入路径、4条命令及Issue验收目标。
Execution revision `c34e29efb4b361d47b1be7345f84f825fa66f9a8251b0c6e651cc3da2f96408a`，Preflight `ed34d5d1ae4306d1037836ac31e9b85cdd95ba04a41ff1aaf2e753f158c2b376`。执行前重核全部冻结输入和静态门禁，任一变化立即停止；只有新的本次确认可生成Plan16执行授权，不能复用Plan15确认、审批、Binding或终态。
仅授权一次有界真实验收：新鲜受限OCI依赖准备及原4条Baseline全过 → 原生真实Coding/Patch → 修改后最新源码4项Verify → 原范围最多2次有限修复及独立真实Review → 停在最终人工验收门禁前。
保留原总上限：Coding模型24次、读取64、写入24、检查6、协议纠偏2、Provider重试2、代码修复2、输入200000 Token、输出32000 Token、工具/证据500000字节、1800秒。保留单步最多1次尝试/300秒、输入投影32000、页16000字节、原只读批8-call/子批及Decision4-call、有界诊断8192字节及冻结沙箱所有限制。
6轮纠偏、8轮判断；仅第8轮OCI新观察的相关当前源码确实进入第9轮真实Prompt且原剩余预算足够，才允许跨Worker/修复Session同一授权下唯一一次条件机会。最多1次调用/输入32000/输出4096全部从原总预算扣除，不增加容量。第9轮没有真实OCI成功应用Patch则第10次请求前停止；未知操作保留预留并阻断。预算不足立即停止，不保证第9轮可派发。
独立Review沿用每Job最多3次尝试、300秒、证据输入1800000字节、Profile每次输出最多16384 Token及原最多2次修复限制；独立Review是Coding24次之外的额外计费，分开报告。知悉high reasoning可能耗尽4096输出且无Patch，9次全额4096已超过32000原总额；没有冻结货币单价或货币硬上限，按实际Provider计费，失败和未知请求可能仍有费用，不提高Token或其他预算。
依契约记录首次真实apply_patch提案轮次、首次OCI成功应用轮次、当前源码唯一读取/真实Prompt送达覆盖、重复读取/Artifact恢复效率、6+8+1证据、截断及费用、最新源码4项Verify、独立真实Review和最终人工门禁；未观察到的值保留null/not_observed/not_run，读取成功、替身通过或调用数增加均不算真实Coding成功。
历史Plan11–15终态、审批、费用、预算和3次未知占用原样保留；不退款、不重置。禁止付费Planner、模型配置实验、恢复旧终态、复用旧授权、增加容量、Push、PR、评论、发布或自动另起付费轮次。失败、预算耗尽、未知结果或门禁失败安全停止。
本确认只绑定上述Plan16预览；幂等键 `harness-coding-v11-plan16-execute-once`，仅消费一次，完成后等待最终人工验收。
```

未发送的ExecutePlanRequest和新幂等键保存在精确JSON。diff/test/review/执行授权/Attempt Hash当前均为null，因为尚未运行；不得伪造未来Hash。完成冻结交付后立即停止。
