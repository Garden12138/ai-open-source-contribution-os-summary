# Plan14 最小离线修复交付

以下验收结果是本次Git整理前已完成的离线验收快照；本轮仅整理并本地提交既有成果。
离线验收时没有付费模型调用、真实 Issue Coding、Plan15、新 Plan 绑定、恢复 Plan14、Commit、Push、PR 或评论。
之后用户单独授权本次本地Git Commit，仍禁止Push及任何付费/真实模型验收。
原 Plan13/14 失败证据未修改，Plan11/12/13/14 的终态和预算保持原样。

最终 app 源码 Hash：`31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95`，254 个文件。
分支 `refactor/agent-runtime-v2`，离线验收的Git基线为 `4328e4a02cd1b4ecff2492b4b4fc15ef238087f1`。
本地提交不改变上述app源码Hash；基线、部署、历史审计报告仍保留原时点字段，不回填为新的Git记录。

## 修复和 Diff

[根因、容量证明、记账契约与恢复说明](diagnosis.md)；[代码与测试 Diff](code.diff)；
[源码清单](source-manifest.json)；[完整验收报告](validation-report.json)。
Diff 不包含历史执行证据，使用零上下文以避免补丁上下文空行引入尾空格。
可只读检查：`git apply --reverse --check --unidiff-zero <code.diff路径>`。

- 使用可信 OCI 实际页序列化、完整 ToolResult/进度/原文页和有界失败/批次元数据证明容量。
  原始批次全部预验证，证书先持久化，然后最多4-call子批次执行；8-call、500000字节及所有权限不变。
- 按最终对象精确计费，修复失败 remaining_budget 的自引用修改；原文页按同会话/Hash 去重计费。
- 保留完整回执，以现有 read_artifact 恢复原始源码页；调整受限输入的投影优先级。
  固定说明不重复内联，进度数值/证据不删，短历史源码Hash仍完整保留。
- 新增协议 preflight-v3/capacity-v1/native-read-capacity-v9 和迁移0046；旧记录可读，旧证明不能批准新容量。

## 验收证据

| 类别 | 结果 | 证据 |
| --- | --- | --- |
| 全量默认回归 | 2047通过，78跳过，0失败 | 两份 capacity-final-full JUnit，见[test-report-index](test-report-index.json) |
| 全量后补充 | 两项新增唯一用例通过；含预算延续、事务中断；另重复核验完整投影 | capacity-final-budget-projection / capacity-final-atomic JUnit |
| 当前平台 OCI 套件 | 25通过，0跳过；23实际OCI，2登记/分页单元 | [最终JUnit](test-results/capacity-final-real-oci.xml) |
| 保存信封回放 | Plan12：4+2、4+1、4+1；Plan13：7成功+1not_found；Plan14：4+4 | [持久化Scheduler回放](offline-replay.json) |
| Plan14真实OCI原页 | 68338字节；严格完整容量上界228743，在剩余459508内 | [实际OCI结果](plan14-real-oci-final.json)、[可重算绑定/批次](oci-capacity-binding.json) |
| 模型输入投影 | 31027/32000；八文件各128字节内联，完整页/文件可恢复 | [逐文件报告](model-visible-source-coverage.md)、[机器报告](model-visible-source-coverage.json) |
| 状态链 | 确定性替身下 Investigation→Patch→Verify→独立Review→人工门禁；含有限修复及批次 | test_harness_coding_journey 四用例，属于确定性测试 |
| 历史兼容 | 44业务表、2090事件、10旧Checkpoint及历史费用/审批不变；只追加0046 | [真实只读审计](legacy-audit.json) |
| 部署 | 原五隔离角色保持权限/环境/限制；四端点健康；活动Job0 | [部署报告](deployment-report.json)、[构建报告](build-report.json) |

默认套件仅使用确定性替身，真实/付费模型开关关闭。
真实 OCI 运行于 macOS arm64 / OrbStack，使用原固定 Runner、无凭据、无网络沙箱。
OCI 中的 Patch、Verify 是安全测试夹具操作，不是模型完成了真实 Issue。
Plan14 OCI 观察真实，报告中的进度 Note 是有明确标签的有界平台夹具；完整生产进度由持久化回放和旅程测试验证。
源文件恢复与下一轮 Prompt 都没有发送给真实模型，不能据此宣称真实模型已观察 not_found 或已成功 Coding。

全量额外两项与全量中已覆盖的重复投影用例去重后，默认/补充共2049项通过、78项跳过。
跳过项包含未授权付费验收及其他显式先决条件，未转换成假通过。

## 输入覆盖与边界

完整下一轮 Prompt 不仅加入八个结果，还重算了 Ledger、批次汇总和假设性剩余预算。
保留真实历史已扣费用，再加入160461字节工具证据，剩余输出304013；未退还旧费用。
完整冻结 Issue/Plan 始终保留可恢复引用，不能假定模型记得前一独立请求。

八个原始页68338字节不能同时放入32000上限。当前内联1024字节，10次原文Artifact分页
可恢复全部原始页，另外2次合法 read_file 恢复 transcript-parser.ts 的尾部；八文件最终Hash全部一致。
这证明存在有界证据访问路径，不保证真实模型会选择该路径或在原预算内完成补丁。

原始参数没有缩页；真正容量不足、非法路径、symlink、Hash/来源冲突或未知OCI错误仍拒绝。
重复读取优先复用，合法offset正常读取，源码改变后旧证据不能作为当前读取或Verify证明。
无进展、崩溃、幂等、原始ID、回执顺序、已执行/未知/未派发以及独立Review门禁均保留。

## 历史与下一步

部署前后Plan仍14、模型启动91、审批/授权各12、Artifact1640、任务事件2090，活动Job0。
Plan13 reads=12、Plan14 reads=5/模型3次/输出35526，以及更早Plan的费用和终态均未重置。
迁移恢复需排空写入者并使用匹配数据库/Artifact/代码备份；不得删事件、退费用或复用旧审批。

[Go / No-Go](next-acceptance-readiness.json)：**当前平台离线修复 GO，直接付费执行 NO-GO**。
先由用户检查本地 Diff；未来另行授权独立 Plan/绑定冻结与精确真实执行预览，再决定是否付费。
本轮没有创建这些对象。原生Linux amd64门禁及HR4/5真实Patch、当前源码Verify、独立Review仍开放。

开发期失败JUnit与初始回归均保留，未只保留绿灯：新增旅程最初未排空子批次、OCI夹具缺少必填进度字段，
以及投影的短历史Hash省略/完整控制帧容量问题，均分别定位修正后再验收。
完整Plan优先内联的旧测试按新投影契约改为校验内容寻址与完整分页恢复，未降低权限/Hash/范围断言。
所有发布JUnit为脱敏投影，原始报告留在/tmp，Hash、顺序和结果对应见[test-report-index](test-report-index.json)。

最终文件/敏感信息检查见[交付检查](delivery-check.json)。

敏感信息检查覆盖实际配置凭据的内存比较和六个既有服务日志，无真实凭据或本地身份命中。
规则命中的旧英文技术描述及故意用于拒绝测试的canary均与HEAD逐项核对，未修改或新增这些历史字符串。
诊断目录的.gitattributes仅保留原文页的行尾/EOF及失败trace的行尾字节；应用源码的空白检查未放宽。

## 本地Git整理

用户本次只授权本地Commit，不授权Push、PR、付费模型或新执行。生产代码/迁移/测试与验收文档分开提交。
[本次提交清单与复核](git-delivery-check.json)记录代码Commit、文件范围、补跑离线结果及凭据扫描。
原delivery-check.json仍是离线验收时点快照；本次README和路线图的提交说明变动由新复核报告覆盖。
