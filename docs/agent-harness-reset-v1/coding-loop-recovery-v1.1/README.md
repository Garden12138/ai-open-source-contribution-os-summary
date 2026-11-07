# Coding Loop Recovery v1.1 — 离线审查交付

本轮实现用户已确定的 6+8+1 与跨模块公平工作集策略，保留上一轮可信警告送达修复。
**本地离线审查交付；部署与新的真实模型验收 NO-GO，等待代码审查及后续独立授权。**
确定性替身通过与真实 OCI 通过都不证明真实模型能够自主完成原 Issue。

## 冻结来源与代码

开始时位于 `refactor/agent-runtime-v2`，HEAD
`737951fa34d9a204b3290419d2705a5f39cbfc76`，核心提交
`b404a92326ca01efa7e64ab91ff9831b548c6562`。先核对上一轮
[code.diff](../coding-loop-recovery-v1/review-closeout-v1/code.diff) 与当时工作区逐字节一致，
255 文件来源集合也一致；本轮导出再次从核心提交和该 Diff 重建上一轮快照并核验。
上一轮共享送达模块 `model_receipts.py` 和 `read_progress.py` 保持原字节。

候选生产源码仍为 255 文件，集合 Hash：
`627a3689ca6647f8747796469f7e93fddff23a8445557066a7cae061e3346d47`。
Native tools Schema Hash：
`52999661311852705547ff40da95c4d33cf16c61fc03ed7deff51b99418f7226`，
Native transport Hash：
`b661a6b2bc3b7c5e6bd83a307024e26e529f82625bd9aba263c95cb2b48d9174`，
Prompt 投影为 `native-coding-loop-recovery-v11`。
详见 [source-manifest.json](source-manifest.json)。

- [设计与预算风险](design.md)：条件证据准入、最多一次机会、跨模块重算规则和授权影响。
- [增量 Diff](delta-from-review-closeout.diff)：相对上一轮 closeout 的变化；生产文件只有
  `coding_context.py`、`native_tools.py`、`loop.py`。
- [完整 Diff](code.diff)：相对核心提交，包含必须保留的上一轮送达修复及本轮测试。
- [原 Plan15 根因诊断](../coding-loop-recovery-v1/diagnosis.md)：逐轮事实、代码根因和假设的原始区分，本轮不修改该报告或原始轨迹。

## 定向验收矩阵

| 要求 | 实际行为与证据 |
| --- | --- |
| 第 8 轮重复 | 前 7 轮新页、第 8 轮重复时，第 9 次前停止；从开始纯重复则原较早守卫第 4 次后停止，不伪称已派发 8 次 |
| 第 8 轮真实新增且未送达 | 新页的 `[896,1024)` 实际进入第 9 个 Prompt，记录原 receipt、origin job、候选范围和实际 inline_ranges |
| 仅索引、Hash、旧内容、无关路径 | 全部不授予机会；特别区分“从未送达的旧页”和“第 8 轮新观察的页” |
| 第 9 轮仍读取或 OCI 拒绝 Patch | 不授予第 10 轮调查；成功提案不等于成功应用，原用量保留 |
| 第 9 轮 OCI 成功 Patch | 实际工作区 Hash 改变，计数重置，真实 OCI 最新源码 Verify 通过，独立脚本 Reviewer 通过，停在 ready_for_human |
| 新证据不能进入 Prompt | 过大正文、缺正文、伪造正文、缺 marker、路径/页 Hash/分页/内联范围不匹配均在派发与计费前拒绝；剩余输入不足同样拒绝 |
| 多模块争四槽 | 6 个改名模块、4 个位置在后续 Prompt 轮换；两模块调换读取先后均不会长期被排除；不依赖模块名称 |
| 单模块与任务相关性 | 单模块填满 4 槽；从真实已观察页选取，优先相关路径与未送达区间，无关页不能挤占相关槽 |
| 旧警告、未知响应、恢复 | start/response/Prompt/Binding 身份共享校验；第 9 次响应丢失消耗原机会并保留预留；重建 Worker 与同授权子 Session 视图不能重新领取 |
| 保存信封与历史 OCI | Plan12–15 共 7 个信封离线重放；既有容量与安全检查重跑，历史 Plan 不恢复、不复用审批 |

工作集没有选中的页仍通过历史 receipt → source_page_artifact → 原始页分页恢复。
4096 字节已观察页的恢复不冒充完整文件恢复；文件后续区间仍需原有有界工具读取。
保存 Plan15 第 7/13 轮的新版投影容量、模块和偏移见
[saved-plan15-selection.json](saved-plan15-selection.json)，这是投影诊断，没有派发模型。
同一保存轨迹的容量变化如下；不声称公平性和额外来源字段没有输入成本：

| 保存轮次 | closeout 内联源码 | v1.1 内联源码 | v1.1 输入上界 |
| --- | --- | --- | --- |
| 7 | 3072字节 | 2048字节 | 31522 |
| 13 | 3072字节 | 3072字节 | 31817 |

两轮均保留两个模块及所有原页恢复路径。第7轮少1024字节是明确的上下文风险，
不能用扩大32000上限掩盖，也不能由离线公平性测试推断真实模型效果更好。

## 验证记录

最终计数以 [test-report-index.json](test-report-index.json) 与
[validation-report.json](validation-report.json) 为准，各套件重叠，不相加。
定向边界验收最终 **50 passed**，无失败或跳过。
最终默认套件两个独立分片合计 **2099 passed / 83 skipped / 0 failed / 0 errors**，
覆盖收集的全部2182用例；跳过项主要是未开启的真实环境门禁，真实 OCI 另按下述记录。
JUnit 脱敏仅处理机器路径、邮箱和主机名，校验用例身份、顺序、通过/失败/跳过数未变。

固定本地 Runner 镜像为
`sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6`。
本轮真实离线 OCI 合计 28 项，另有 2 项纯单元检查，共 30 项通过：原有 24 项 OCI、
上一轮的成功第 6 轮/拒绝第 8 轮两项、本轮成功/拒绝第 9 轮两项。
覆盖真实 4+4、整批预检拒绝、缺文件、符号链接、路径逃逸、Hash/offset 错误、
Plan 绑定隔离卷、无凭据无网络 Verify、零测试/仅 help 不通过，以及资源耗尽边界。
容量原始证据见 [oci-capacity.json](oci-capacity.json)，真实源码页总量仍为 68338 字节。
平台为 macOS arm64 + OrbStack；**Linux amd64 未运行**。

[保存信封回归](saved-envelope-replay.json) 使用新的合成数据库、合成授权及确定性工具，
没有恢复旧 Session。子批次是 4+2、4+1、4+1、4+4、4+4、4+4、4+4，保存原调用 ID/
工具参数并核验真实计费守恒。原始回复采用已有脱敏调用信封，不提交 reasoning 正文。
必要的合成 Prompt/回执/原始页均内容寻址，未引用的本轮中间导出已清除。

开发中发现过旧投影容量断言与新增来源字段占用不符，已去掉重复工具路径以保留源码
空间；另一个测试误用了小于工具最小值的 127 字节读取，已修正为合法的重叠 128 字节
区间并重跑。未放宽工具、Prompt 或沙箱边界。失败的中间日志不作为最终通过证据。

compileall app、git diff --check、新文件空白检查、Diff 逆向校验和敏感信息/本地凭据
编码变体扫描由导出脚本执行。未纳入数据库副本、凭据、未脱敏 Provider 响应或运行日志。

## 历史一致性与 Go/No-Go

[历史检查](history-plan11-15.json) 与前后 compatibility 文件记录 query_only 比对：
45 张表、11 个检查点全部与保存的 Plan15 终态一致；Plan11–15 的事件链、终态、审批、
预算和未知占用未改。模型启动106、执行授权13、Plan审批13、任务事件2551、Artifact1802，
活动 Job=0。历史费用未退款、预算未扩张。

五个部署角色仍为原 254 文件 Hash
`31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95`；
本地候选未部署。Profile 仍为 deepseek-v4-pro / high，配置 Hash
`bb3522b03a444b544c808217ec5ed4b4fa71253591ee8c09e3fc77c57d3a6049`，
Coding 单次输出仍4096。独立 Review 原边界未变。

**Go：进入人工独立代码审查。No-Go：当前部署或新的真实模型验收。**
新策略最多增加 1 次实际模型调用，增量最多输入32000、输出4096，仍从原24次/
输入200000/输出32000/证据500000字节/1800秒及其他原限额中扣除，不分配新预算。
reasoning high 仍可能耗尽4096而没有工具调用，离线替身不证明该风险已消失。

原 Plan15 的源码、协议与授权不兼容新版，原 confirmation-text 不可用于新版执行。
未来必须重新冻结镜像/源码/策略/协议及所有执行输入，创建新的独立绑定并另获精确授权。
本轮没有创建 Plan16、恢复 Plan15、真实 Provider 调用、执行审批、业务 Job、部署、
Commit、Push、PR、评论或其他外部写入。交付后停止，等待代码审查。
