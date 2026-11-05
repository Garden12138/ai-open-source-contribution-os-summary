# Harness持久化协议与恢复

`0042_agent_response_evidence`只新增响应事件约束：必须引用已登记Artifact、
同会话/Job的model_started与相同Prompt，每个Job只能有一个响应回执。
安全工具信封版本为`agent-model-response-v1`。模型文本与私有推理不持久化；
凭据字段脱敏并拒绝整个响应。信封超出剩余output_bytes时记录显式unavailable原因、
安全内容hash、字节数和调用数并阻塞，不扩预算，不执行任何调用。

`0043_implementation_progress`只新增进度CAS约束：revision必须连续、
expected_progress_hash必须等于上一事件hash，checkpoint_hash必须匹配当前检查点，
完整账本Artifact必须已经登记。账本版本`implementation-progress-v1`。
阶段、六个步骤、证据hash、源码hash、次数、阻塞及下一步均在原Journal中，
完整账本内容寻址；Prompt只携带有界摘要与完整Artifact引用。

两次迁移不改表、不回填旧事件或Checkpoint；旧会话缺少的进度与vendor ID保持缺失。
新Native v6把进度策略绑定进tools_schema_hash；旧Plan、授权、终态及Usage保留。
旧终态不能直接恢复。真实执行需要冻结同模型、同原限额的新预览并精确批准。

## 部署与备份

1. 确认五个既有角色及卷所有权，活动Job为0；不要部署到其他业务实例。
2. 冻结并验证全部运行源文件、镜像与安全配置。只读/非root/cap-drop/网络/挂载/配额
   必须保持原样；Docker socket仍仅在可信Sandbox Worker。
3. 在原Linux内核的业务容器中用SQLite online backup保存一致副本，保存所有表的hash、
   旧事件前缀、Artifact注册及预算。不要从宿主机并发打开正在使用的SQLite文件。
4. 停止现有写入者，保留旧容器和镜像；在同一原卷上用新代码前向迁移。
5. 验证只新增0042/0043迁移记录、所有旧行/hash不变、完整性/FK/审计链通过，
   原终态/Usage/模型绑定不变，再启用原角色并检查健康。Model Gateway无需重启。

## 故障与恢复

- 模型返回后先独立提交响应Artifact和事件，再做Decision解析。解析中断保留原预留；
  缺少完成事件的已启动Job终态后明确阻塞，不重发付费请求。原始工具调用可供人工追踪。
- 工具回执、进度事件、Checkpoint及Job完成在同一事务提交。中断或未知副作用不得重试；
  持久化完成的只读回执可在进程重启后复用，绑定新call_id/input_hash，保留原回执引用。
- 有意义的新证据/源码修改/当前Verify重置无进展连续计数；相同范围读取复用结果。
  两个无进展模型回合发一次纠偏，随后仍无进展即阻塞。所有请求仍计入原预算。
- 降级需要先排空写入者，导出并保留升级后新增事件、Artifact和Usage，恢复匹配的
  升级前SQLite/Artifact/代码备份，再验证旧行hash、integrity_check及foreign_key_check。
  禁止删除迁移记录、原地改写Journal或用旧代码跳过未知迁移。旧镜像直接连接新库不是恢复。

升级/备份恢复、旧hash保留、伪造响应、进度CAS和独立Worker进程重启均有离线测试。
离线检查不替代真实OCI及Linux amd64验收；状态见各Phase报告及`docs/tasks.md`。

2026-11-04后续修复：终态Checkpoint和Ledger阻塞修订同事务提交。
已存在Ledger的Plan12只依据现有协议拒绝追加一次修订，旧Checkpoint、Usage和所有原始事件
不变；没有为旧无Ledger会话回填来源。回执见`terminal-progress-reconciliation.json`。
协调Worker只处理本地协调事务的SQLITE_BUSY，延后到既有轮询；模型/工具效果、未知DB错误
不自动重试。五角色r4重部署没有新增迁移，原全部业务表hash保留，旧容器及备份保留。
