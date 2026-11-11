# 源码复核与Go/No-Go

**独立源码审查：NO-GO_PENDING_INDEPENDENT_REVIEW。真实Gateway部署：NO-GO。**

本文件是实施代理的第二遍源码/测试复核，不能冒充另一人的独立源码审查。本轮实施和复核由同一代理完成，没有另一审查者签署。前一轮独立审查针对旧设计，不能套用为本次新Diff批准。

自检认为本地候选已具备交由独立审查的证据：代码和测试实际存在，见[code.diff](../../../../maintenance/HISTORY_INDEX.md)、[关闭表](review-resolution.md)、[validation-report.json](../../../../maintenance/HISTORY_INDEX.md)。检查了以下边界：

- 正常服务只open existing；初始化、旧格式迁移及恢复仅由显式离线CLI调用。缺少材料不调用旧初始化器；唯一调用旧构造器的位置是明确new-install的新空代。Publisher仍走原构造器。
- 新格式身份同时由RSA签名、AES认证检查和数据库身份行约束，密钥缺失/错配不会被内存缓存掩盖。旧格式不能从RSA key_id推断AES配对，导入须既有凭据认证解密及可信外部材料摘要，原文件不改。
- 只读读取和显式管理写入使用不同SQLite URI；后者mode=rw不能创建缺库。Schema列/类型/主键、user_version、integrity、引用和密码材料共同检查，单靠表名或integrity=ok不能就绪。
- 生命周期共享lease拒绝运行中维护；整代持久化后替换单一指针。路径/文件身份逐操作重核，恢复复制已核验的精确字节，异常时不覆盖唯一原材料。回滚在新代写入后拒绝，避免损失新配置。
- Readiness使用独立只读认证域，Provider Worker没有新增admin权限；nonce、签名、时效和profile/ref/store绑定检查。授权前及派发前检查不调用Provider，不把故障变成退款或扩容。两个消费入口仅对明确存储code不可重试，未改写所有5xx。
- 新授权增加只读证明摘要，原事件/审批/已消费授权不改写。历史幂等查询只返回原Job，不新增执行；没有任何Plan16恢复或预算补偿路径。

自检未发现需要本轮放宽门禁才能运行的行为。独立审查仍应重点核对：

1. 私有文件权限和同代证明的威胁范围：Gateway专用本地文件系统、受保护父目录和合作锁。不能防御已拥有同UID/root及全部密钥的攻击者；不支持NFS/不明锁语义挂载，WAL/SHM存在则拒绝自动恢复。
2. 备份仅在服务停机、无绕过lease的外部SQLite写入时执行。legacy源必须是独立证明的静态Gateway候选，不能把Publisher同格式库当恢复来源。未知来源、空旧库及缺配套材料继续NO-GO。
3. 进程崩溃测试不等于真实持久卷失效或硬件断电验证。Mac/Linux权限/目录fsync差异、Linux amd64和真实卷演练仍待另行授权环境。
4. Credential readiness证明本地引用/解密可用，不证明远端Provider账号、余额、模型可用性或实际计费。新Profile/ref需要新不可变配置版本及重新冻结，不能恢复Plan16。
5. 新代码有额外只读预检，老宿主启动器必须显式换成持久路径并配置store_id/readiness能力；仅部署五角色容器不足以升级独立宿主Gateway。当前未部署，宿主已有进程内存版本没有重新取证。

完整回归后补全类型并加强Publisher在读取模型密钥文件前的隔离检查；[收尾Diff](../../../../maintenance/HISTORY_INDEX.md)及精确OCI源码一致性在机器报告中单列。不混淆原冻结255文件、当前257文件、旧Git HEAD与未来部署版本。真实源码/镜像变化使旧冻结不再适用于新代码，不能通过更新旧Hash绕过。

停止点：独立审查者核对本次新Diff、可信恢复或明确重新配置方案、部署/回滚输入及权限后，再单独决定是否授权真实修复。此文不提供新的执行确认文本、付费授权或Plan17。
