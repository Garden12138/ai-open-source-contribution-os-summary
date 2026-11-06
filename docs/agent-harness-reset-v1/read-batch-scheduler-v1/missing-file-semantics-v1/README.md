# 批准缺失文件语义专项 — 已实现并离线验收

2026-11-05，分支refactor/agent-runtime-v2，HEADe5cc92a，工作区未提交。
最终源码 Hash `e78f36d6fc6c16175da08ad77f38ce30ebcb0a8357e9262b1fef7231dc65474f`，252文件，已部署到原五个隔离角色。
本轮没有付费模型调用、新Plan14、旧Plan13恢复、预算退还、Push、PR或评论。

修复后，经过OCI安全预验证的批准缺失文件返回明确的not_found调查结果，
没有伪造文件文本、大小或content_hash；其余有效读取仍以最多4-call子批次执行，原始ID与结果按原序聚合。
模型可继续调查或通过原授权apply_patch创建。真正的越权、链接/Hash冲突、来源不一致与未知OCI错误仍阻断。

## 交付与证据

| 交付 | 文件 |
| --- | --- |
| 原始8-call、ID/参数/顺序及失败位置 | [根因与恢复说明](root-cause-and-recovery.md)、[事实索引](forensics.json) |
| 代码与测试Diff | [implementation.diff](implementation.diff)，SHA-256 `31322bdecd7f977d9d930be2fd3251b0200ea7f75cbc8b88c6c5cbe93746f5d0` |
| 新回归测试 | [持久化/模型上下文/缺失/新建/进度/崩溃/迁移](../../../../tests/test_missing_source_observations.py)、[真实OCI与冻结归档](../../../../tests/test_missing_source_oci.py) |
| 原信封持久化Scheduler离线重放 | [scheduler-replay.json](scheduler-replay.json)：4+4，7成功+1not_found，模拟下一轮仅创建批准路径；使用确定性工具与新合成测试授权，不冒充OCI或历史审批 |
| 真实冻结源OCI重放 | [frozen-oci-replay-final.json](frozen-oci-replay-final.json)：原归档d269dc46/源码7f02d837，7真实读取+1可信缺失，临时OCI批准新建成功，源码Hash相应改变；模型下一决策为模拟，Provider调用0 |
| 全量回归 | **2025通过、77跳过、0失败**；160测试文件分为不相交两组，最终源码两组均重跑 |
| 真实OCI安全验收 | **24通过、0跳过**，macOS arm64/OrbStack；含旧批次/工具、Hash/链接/未知错误、断网/无凭据与资源耗尽边界 |
| 测试原始失败与最终JUnit | [测试索引](test-report-index.json)、[验证报告](validation-report.json)、[保存JUnit](test-results/)；原始Hash保留，凭据样式的测试/skip文本脱敏，计数不变 |
| 部署与原业务表 | [部署报告](deployment-report.json)、[最终镜像](build-report-final.json)、[252文件清单](source-manifest.json) |
| 旧任务兼容性 | [独立兼容审计](legacy-compatibility.json)、[新代码读取旧Plan13完整审计](legacy-plan13-after-upgrade.json) |
| 下一轮文件预览 | [可读预览](real-model-acceptance-preview.md)、[JSON](real-model-acceptance-preview.json)，Hash `5c20cae76a88bdc5f06cf2cb819cf63aa1355993512f6f0f74b30cda47999376`；未创建新方案或执行请求 |

## 验收边界

完整预检先于所有应用工具调用。not_found仅表示批准路径的OCI证实缺失，status与succeeded区分，
不完成Coding/Verify。重复缺失可复用，同源相同缺失只算一次新调查事实；源码变化使旧缺失失效。
独立子进程在第一子批次提交后崩溃，重启后8个调用各执行一次且最后仍返回not_found。
未知/伪造回执、未批准创建、expected_hash冲突、越权及符号链接父路径都不能降级为缺失。

Plan11/12/13及9个历史Checkpoint、全部旧行、2013条任务事件、1610个Artifact和预算均未改变；
Plan13仍blocked，12次读取未退还。Planning64/64和三个未知占用保留。
累计模型启动88、审批11、Plan13、活动Job0不变；只追加0045迁移，44张业务表Hash逐一不变。
原权限、网络、凭据隔离、容器资源、Token/时间预算、执行4-call/原提案8-call上限都不扩大。
旧v1预检、Journal/Checkpoint/Progress保持可读；新v2预检、not_found与v8原生协议需新绑定。

首次全量仅1个旧分页引用形状用例失败。保留原部分引用字段后，冻结最终e78f36d6、重建镜像，
重新运行两组完整回归和全部24项OCI；初次失败与修复成绩均保留，没有弱化安全断言。
完整Plan在32000输入边界内优先内联，否则保留原平台Artifact及分页引用；8个回执状态和缺失证据仍完整。

原HR4/5需要新的真实模型Coding/Verify/Review，离线测试没有代替这些门禁。
原生Linux amd64未运行，原跨平台门禁仍开放。后续真实验收候选已生成，但必须另行获得人类明确授权。

## Git交付中的隐私投影

2026-11-06仅做Git整理时，发现4份全量JUnit中的本机路径、邮箱与测试参数需要脱敏。
这些原件仍在本地保持原始字节，未修改既有测试索引、验证报告或其Hash，且不纳入Git。
Git中对应证据见[脱敏导出索引](test-results-public/export-index.json)及其4份XML副本；
索引明确原件SHA256、导出SHA256、测试顺序及相同的通过/失败/跳过计数。
原历史索引中的4个路径表示本地原件，仓库读者应使用导出索引中的public_file。
脱敏仅处理隐私元数据，不改变测试结果，不把初始失败改写为通过。
