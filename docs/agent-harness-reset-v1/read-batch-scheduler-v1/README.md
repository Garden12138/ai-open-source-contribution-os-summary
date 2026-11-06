# Native Read-Only Batch Scheduler v1 — 离线验收

最新真实验收（2026-11-06）：[Plan14已按精确授权执行一次](plan14-execution/README.md)，
Baseline4/4、真实模型3次；第三响应8个read_file各max_bytes16000，整批输出容量准入拒绝，
未创建子批次/派发，Session安全终态agent_read_batch_budget。Patch/修改后Verify/Review未完成，
not_found未观察到，HR4/5仍未通过；旧历史/预算不变，无新付费重试或Plan15。

最新修复：[批准缺失文件语义专项](missing-file-semantics-v1/README.md)离线门禁已通过并部署。
原信封8-call现在可得7个正常读取和1个可信not_found，原批准新建仍受Registry与OCI约束。
全量2025通过/77跳过、真实OCI24通过；旧任务和Plan13 reads12保留，无新付费调用。
随后按仅准备授权完成[独立Plan14绑定](plan14-preparation/README.md)，
[实际精确预览](plan14-preparation/execution-preview.md)等待新的执行确认；没有新增执行审批/Job/预算分配。
下列旧结果与[原文件候选](missing-file-semantics-v1/real-model-acceptance-preview.md)保持归档，不改写原Hash。

后续状态（2026-11-05）：用户先仅授权创建同内容Plan13与Scheduler绑定，随后批准精确预览c628ea47。
[真实结果](plan13-execution/README.md)：新Baseline4/4通过，2次模型调用；第二轮8-call提案通过解析，
但缺失文件使整批OCI预检拒绝，8个应用调用未派发，安全停止。Patch/Verify/Review未完成，
真实成功分批未观察到，没有额外付费重试。以下保留此前离线验收时点的事实与预览，不改写原Hash。

2026-11-05，在`refactor/agent-runtime-v2`完成Plan12暴露的原生Provider提案与执行契约修复。
未重构整个Runtime、未扩大UPEVE、未付费重现、未创建Plan13、未Push或PR。
原Plan11缺失信息仍为unavailable；Plan12终态和全部历史用量不变。

## 行为与代码 Diff

只对完整验证的5–8个`read_file`提案执行有界调度。先验证全部调用的来源、ID/参数、
权限、预算与沙箱策略，并在同一固定源码视图完成所有路径/hash/offset的OCI预检；
全部通过后才开始实际读取。原Decision与Checkpoint仍最多4个调用。

原批次是不可变证据对象，子批次有独立Checkpoint、幂等ID和回执；原始Provider信封、
Tool Call ID、名称、原始参数字符串和顺序保留，最终按原顺序反馈，中间无模型Decision。
失败时停止剩余调用，分别保存实际回执、已启动但结果未知、未执行调用。
整批预留读取数不退还，不重置既有预算；精确复用、分页、源码变化和无进展仍按现有规则处理。

[完整实现与测试 Diff](implementation.diff) SHA-256：
`22098bab1e7c4f4309221a67f655ae49550b0a012055c4ea5fda29f4664fc34b`。
Diff包括新增模块、迁移与测试，不包含文档，避免递归生成。
主要实现为`app/agent_v2/read_batches.py`、`loop.py`、`native_tools.py`、
`model_receipts.py`、`journal.py`、`progress.py`和`app/sandbox_worker/agent_tools.py`。
迁移`0044_native_read_batches`只追加Journal约束；旧Journal/Checkpoint保持可读。
[设计与恢复](design-and-recovery.md)记录升级、失败与恢复程序。

## 验收证据

| 验收项 | 结果与证据 |
| --- | --- |
| 真实Plan12调用事实 | [校验报告](facts-and-baseline.json)：6/5/5，全部16个read_file，原ID/参数/顺序与原始Artifact一致 |
| 保存信封离线重放 | [回执索引](offline-replay.json)：4+2、4+1、4+1，模型请求0，工具使用确定性夹具；不冒充真实模型或真实目标仓库执行 |
| 准入失败 | 混合写/检查/终止/未知工具、无效参数、越权路径、重复ID、>8均先拒绝；OCI预检覆盖末尾路径/链接/hash/UTF-8 offset错误 |
| 崩溃与重启 | 独立进程在第一子批次提交后退出，恢复不重复已完成调用；结果未知保持阻断 |
| 上下文与进度 | 原ID/结果对应、精确复用、合法offset、源码变化、实际可见字节/next_offset、重复无进展和Coding未完成均通过 |
| 最终源码全量回归 | **2015通过、69跳过、0失败**；158个测试文件分为不相交两组 |
| 最终源码真实OCI | **16通过、0跳过**，macOS arm64/OrbStack；包括新批次边界、原工具边界、真实Verify无网络/凭据/写入和资源耗尽限制 |
| 迁移与旧任务 | 升级约束测试通过；旧Plan12的1942条事件和1581个Artifact Hash/凭据扫描通过 |
| 部署 | 原五角色各250个生产文件校验、四端健康，44张业务表Hash不变，仅追加0044 |

完整成绩、初次失败及修复说明见[机器验收报告](validation-report.json)。
最终JUnit已保存于[test-results](test-results/)；移除了凭据样式的测试参数/skip文本，
保留原始报告Hash和全部用例/通过/跳过计数，没有删除测试或将跳过计作通过。

## 冻结源码与安全边界

最终生产源码Hash：`9601fd8b2ca3c1e7b605633ba7a5e67491591d5a61ff05bf1de3590cca85860d`，
见[250文件清单](source-manifest.json)、[镜像](build-report.json)、[部署](deployment-report.json)
及[部署后审计](postdeploy-audit.json)。Runner镜像和Policy Hash保持原值。

执行子批次上限4；原始只读提案上限8；混合与其他超量工具仍整批拒绝。
write_paths、批准命令、网络、凭据隔离、容器资源与默认Token/时间/工具预算不扩大。
旧Plan12 Checkpoint/Usage、Planning的64次用量及三个未知占用均保持原样。
新增模型请求0，累计模型启动仍86；执行授权仍10；Plan仍12，无活动Job。
[升级后旧任务审计](legacy-plan12-audit-after-upgrade.json)保留旧协议与原始调用关联。
[最终检查](final-checks.json)核对预览生成后的相同计数、四端健康、凭据扫描、编译与内部模块完整性。

原生Linux amd64验收未运行，原平台门禁仍开放。离线与OCI结果没有证明真实Patch、
当前源码Verify或独立模型Review已完成，原HR4/5仍未通过。

## 独立真实模型预览

[可读预览](real-model-acceptance-preview.md)及[精确JSON](real-model-acceptance-preview.json)
冻结同一Issue/基线/方案内容、原模型、8条路径、4条命令和原上限。
新协议Hash使旧Plan12绑定过期，预览没有虚构新Plan/授权ID或复用旧审批。
用户未授权前不物化新方案、不派发付费任务；待明确允许相同内容新绑定后，再提交带实际ID的精确执行确认。

上述绑定与[Plan13实际执行预览](plan13-preparation/execution-preview.json)的单次执行授权现均已收到并消费，
结果见[真实审计](plan13-execution/final-audit.json)。原a7b2f501与c628ea47预览JSON均保持原样；
不能用已消费的审批恢复终态或再次派发付费任务。
