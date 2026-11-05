# Harness当前交接 — 2026-11-04

Gate0–3通过；Gate4未通过，Phase5未进入，专项及仓库Goal均未完成。
当前任务仍为HR4真实Patch及当前源码Verify。历史Plan11原始调用信息为unavailable，
这一缺口不再阻塞工作；当前终止原因是新Plan12实际用尽两次协议纠偏额度。

用户“批准”绑定精确预览81e1d70a及额外一轮原上限预算。
[批准](real-acceptance-approval.json)已消费一次，
[执行回执](real-acceptance-execution.json)记录唯一Job d4bbb222。
不得重复调用该执行脚本、重新分配预算或恢复旧终态。

Plan12在源码59862614上重新准备环境并通过四项Baseline，真实模型调用8次。
13个工具结果中6次精确复用，没有重复执行底层读取。
模型返回6/5/5-call三个超量批次，均先保存原始ID、名称、参数、顺序、finish reason、
Usage，再按4-call规则整批拒绝，执行数均为0；仍禁止自动分批。
原始响应和一调用一结果关联见[原始审计](real-acceptance-audit.json)。
真实Patch为0，修改后Verify和独立Review均未运行，不能用Baseline或Mock代替。

实际报告Usage为输入72200、输出5029；计入三次拒绝保留预留的账本用量为
输入137303、输出15038。两者不可混算。原Plan11的用量和终态不变；
规划生命周期64次上限、三个未知预留不变。详见[真实结果](real-acceptance-result.json)。

实际运行暴露的两项平台缺陷已经修复：

- 终态Checkpoint转为blocked时，Ledger在同一事务中追加阻塞阶段、步骤、原因、
  证据hash和下一步；回滚一起回滚，不创建新Job，不退还预算，不回填旧无Ledger会话。
- 协调Worker的本地数据库事务遇到SQLITE_BUSY时，回滚后延后到既有下一轮询；
  未知数据库错误继续失败，模型/工具/其他效果派发不在此重试范围。

终态Ledger回归先复现3失败/1旧兼容通过；修复后r3专项227通过/8跳过、工作流63通过。
Worker真实双连接锁冲突先复现1失败/4安全边界通过，修复后92项Worker增量回归通过。
原r1全量1982通过/62跳过、r2单模块回归保留。上述套件可能重叠，不能相加为独立用例，
也不能描述成r4全量重跑。全部来源与hash见[验证汇总](verification-report.json)。

当前部署源码718ed712，只在r2基础上增加journal.py、progress.py与worker.py修复。
原五个角色248文件逐一验证、四端健康；44张业务表hash保留，没有新迁移或依赖变化。
旧容器与一致备份均保留；Model Gateway未重启，正式实例仅GET健康。
SQL写锁退出的原Worker在无活动Job后用原配置恢复，随后部署新版本。
恢复、部署和Ledger修订均未增加模型调用或授权。

[Ledger追加回执](terminal-progress-reconciliation.json)保留旧Checkpoint及Usage，
[最终审计](real-acceptance-final-audit.json)验证1942条任务事件及1581个Artifact；
[部署审计](postdeploy-audit-r4.json)记录无活动Job、累计模型启动86次、授权10次。
这些数量包括旧历史，不能作为本轮付费调用数。旧原始审计与失败测试报告没有覆盖。

不自动重跑：本次原两次协议纠偏已耗尽，另一轮真实调用需要新的具体预览和人类授权，
当前没有该授权。后续必须先处理Provider持续违反4-call约束的实际问题，不能以盲目加预算、
放宽上限、自动分批或更换既有模型绑定代替。代码/协议/模型/冻结输入改变后旧批准不能沿用。

macOS arm64/OrbStack的真实Python/TypeScript OCI边界7项通过；Linux amd64仍not_run。
不声明跨平台通过、原生全自动交付成功或最终Human Acceptance完成。
全程没有提交、Push、PR、评论、发布或其他第三方写入。
迁移与恢复继续按[migration-and-recovery.md](migration-and-recovery.md)执行。
