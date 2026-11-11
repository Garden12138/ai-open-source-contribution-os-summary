# 四项审查问题：实现与关闭证据

以下“代码关闭”只表示实现和合成验收存在。真实材料恢复、独立审查与生产准入没有因此关闭。原[独立审查](../../../../maintenance/HISTORY_INDEX.md)保持原字节，不回写旧Hash或勾选旧门禁。

| 问题 | 实际实现 | 代码验收及边界 |
| --- | --- | --- |
| GCI-R1 / P1：同代、一致备份、切换和回滚 | [同代认证](../../../../../app/providers/gateway_storage.py#L137)、[独占维护租约](../../../../../app/providers/gateway_storage.py#L468)、[备份/恢复/回滚/热日志副本恢复](../../../../../app/providers/gateway_storage.py#L482)、[原子指针](../../../../../app/providers/gateway_storage.py#L363)；服务持有lease，维护拒绝活动实例 | `test_wrong_generation_key_rejected_even_empty_database`、`test_maintenance_refuses_active_service_and_stale_cas`、`test_backup_restore_atomic_cutover_rollback_and_mutation_guard`、`test_corrupt_backup_cannot_switch_pointer`、3个`test_process_crash_during_cutover_keeps_complete_generation`、真实热journal崩溃及初始指针前崩溃测试通过。**代码关闭；实际可信三件套、原删除主体和生产回滚条件仍未证明** |
| GCI-R2 / P2：持久目录、权限及启动一致性 | [路径/权限检查](../../../../../app/providers/gateway_storage.py#L56)、[平台默认配置](../../../../../app/config.py)、[未安装LaunchAgent生成器](../../../../../scripts/render-model-gateway-launchagent.py)、[Compose专用卷](../../../../../docs/deployment/compose.yaml#L210)；目录0700/文件0600、expected_store_id、禁止/tmp和符号链接 | `test_bad_paths_and_real_ephemeral_policy`、真实mode异常/可写父目录、硬链接/错库/父目录替换、launcher默认一致性、私有运行密钥文件和真实临时Gateway进程两次启动测试通过。OCI真实UID10001，宿主为当前用户；owner负向通过注入不同euid验证，未声称真实跨UID chown或ACL验收。**代码关闭；实际持久卷与Linux amd64验收仍开放** |
| GCI-R3 / P2：Publisher共享类兼容 | [Gateway专用子类](../../../../../app/providers/gateway_storage.py#L225)不调用旧构造器；正常open无创建。旧[GatewaySecretStore](../../../../../app/providers/model_secrets.py)以及[publisher_service.py](../../../../../app/publisher_service.py)不改；旧grant/save密码格式复用，Gateway管理写入既有库用rw | `test_legacy_import_is_explicit_forward_copy_and_publisher_unchanged`验证旧库不改、原ref可读、显式副本升级及旧管理写入；12项Publisher设置回归全部通过（含重开、断开连接和key bootstrap），35项模型设置回归通过。旧Store SHA256仍为`4b6077431339d226a0d14d296a545097682d5ef0fbc4a11d1f80185f0b78bcf8`。**兼容实现与离线回归关闭** |
| GCI-R4 / P2：readiness与不可重试契约 | [认证契约](../../../../../app/providers/gateway_http.py#L55)、[只读能力/证明校验](../../../../../app/providers/gateway_readiness.py)、[授权前验证](../../../../../app/contribution_workflow.py#L59)、[派发前阻断](../../../../../app/agent_v2/loop.py#L312)、两个Runner存储code分类；证明绑定profile/ref/store/generation/nonce，失效5秒 | 真实ASGI liveness/坏存储、过期/伪造/错误绑定、只读能力不能申请grant、两个Runner分类、实际AgentJournal/Job前后账本测试通过；预检失败model_calls/retries均0，已派发内部请求故障model_calls=1/retries=0、失败预留保留，零后继Job；缺证明授权不新增业务事件。**实现与离线验收关闭；真实执行仍须新冻结和独立授权** |

全部列出的用例在[test_gateway_storage.py](../../../../../tests/test_gateway_storage.py)中，共52个参数化实例；宿主完整回归与最终OCI均52/52。逐例结果在[完整XML](../../../../maintenance/HISTORY_INDEX.md)、[OCI XML](../../../../maintenance/HISTORY_INDEX.md)，不以Mock次数或纯表达式计算代替真实SQLite/账本断言。

本轮52项合成凭据均为临时数据；持久性测试使用临时目录跨真实进程重启，证明服务未重新生成材料，不等于已对真实宿主磁盘/卷执行恢复。进程强制退出验证的是软件崩溃边界，不是硬件断电证明。

本地完整回归2151通过/83显式opt-in跳过。之后补全类型及Publisher读取模型密钥文件前的早拒绝检查，见[精确收尾Diff](../../../../maintenance/HISTORY_INDEX.md)；精确候选另经最终OCI184通过/1既有opt-in跳过。独立审查方仍须评估新私有Schema v1、签名身份、路径信任和维护边界；本表不是独立签署或部署授权。
