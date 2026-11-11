# 真实部署、恢复与回滚方案（未执行）

**当前NO-GO。这里的命令仅是未来操作预览，不是本轮执行记录或授权。** 真实Gateway PID、父启动器、数据库、密钥和服务均未操作。不能用测试生成的密钥、store_id、generation或示例路径替代真实冻结输入。

## 先满足真实修复前置条件

1. 另一审查者签署本次[代码Diff](../../../../maintenance/HISTORY_INDEX.md)、新私有Schema与[源码复核边界](source-review.md)。确认原Plan16 blocked/授权消费，历史费用、预算及3次未知占用不动；再次零付费核验无活动业务Job/请求。
2. 重新定位实际18091监听者、父启动器/自动拉起机制、Worker访问地址、运行版本及所有Gateway实例。原记录的PID74991/PPID74984只是旧时点证据，不可直接对历史PID发信号。未证明旧启动器来源及停止方法前，不准切换。
3. 冻结独立Gateway发布目录/解释器/源码、所有角色候选镜像、规范存储路径、owner、权限、卷/设备、连接/Profile、readiness能力和精确操作/回滚清单。不要用五角色镜像证明宿主进程版本，也不要让部署后的Gateway继续依赖可变工作树。
4. 只接受来源/时间/原始Hash及配套密钥可证明的恢复候选。旧/tmp空库、缺密钥、业务备份和历史响应都不能作为匹配三件套。现有材料不能证明所有外部备份不存在，但当前没有已证明可用候选。
5. 将候选置于Gateway专用私有恢复边界，单独授权内部配对验证/恢复或明确新录入。任何读取真实材料、加载密钥、调整权限、停机、复制、恢复、重启及安装LaunchAgent都须在随后真实修复授权中列明。本轮未执行这些步骤。

## 两条互斥的数据路径

**可信既有材料：** 保留原件，证明DB/master/RSA来源及所需历史opaque引用。已有行AES认证解密证明DB/master配对；历史RSA key_id及外部材料摘要另证明RSA来源，不能从AES成功推导RSA同代。候选须是静态、一致的DELETE模式快照；有WAL/SHM或未完成外部事务时停止，不能用immutable忽略日志。禁止采用Publisher同格式凭据库。

对旧flat格式，`import-legacy`只把精确冻结的三件套复制到**全新持久目标**，升级副本私有Schema到user_version=1，保留原ref、密文和密钥。业务Schema不迁移。迁移回退是保留的原flat候选，不是在运行中降级新仓库；不存在完整健康旧候选时只能安全停止。

**无可信材料：** 明确宣布恢复未完成。另行授权`reconfigure-new-store`新位置/新store_id及新的凭据录入；录入生成新credential_ref，业务连接/Profile使用原追加版本和CAS流程。不得写成旧ref、覆盖历史配置、重置预算或恢复Plan16。新密码材料只是新配置，绝非旧凭据恢复。默认目标已非空时命令拒绝；需要另一持久路径和启动配置时须重新冻结该配置，不能自动换名或删除旧材料绕过。

## 未来CLI精确动作形状

以下所有大写值均需来自随后批准的精确输入，不能使用本轮Fixture值。操作前后都核对指针CAS、全部材料摘要和权限；不输出密钥、密文或明文。

```text
python -m app gateway-storage initialize --root PERSISTENT_ROOT --intent new-install
python -m app gateway-storage initialize --root NEW_PERSISTENT_ROOT --intent reconfigure-new-store
python -m app gateway-storage import-legacy --root NEW_PERSISTENT_ROOT --source TRUSTED_STATIC_GATEWAY_TRIO --material-hash EXACT_TRIO_HASH --envelope-key-id VERIFIED_HISTORICAL_KEY_ID
python -m app gateway-storage backup --root PERSISTENT_ROOT --store-id STORE_ID --expected-generation CURRENT_GENERATION --destination NEW_PRIVATE_BACKUP
python -m app gateway-storage restore --root PERSISTENT_ROOT --store-id STORE_ID --expected-generation CURRENT_GENERATION --source EXACT_PRIVATE_BACKUP --snapshot-hash EXACT_SNAPSHOT_HASH
python -m app gateway-storage recover --root PERSISTENT_ROOT --store-id STORE_ID --expected-generation CRASHED_GENERATION
python -m app gateway-storage rollback --root PERSISTENT_ROOT --store-id STORE_ID --expected-generation JUST_ACTIVATED_GENERATION
```

初始化只选其中一条，不按列表连续执行。`recover`仅用于新布局DELETE热日志的副本恢复；不迁移/猜测原/tmp事故库。CLI不读Settings环境或调用Provider，缺精确参数、候选不匹配或活动lease存在均阻断。正常服务启动没有初始化参数或自动恢复分支。

## 宿主macOS启动配置

规范根：`/Users/OWNER/Library/Application Support/ContribOS/model-gateway`，ContribOS父目录、仓库及generation目录归Gateway运行用户且0700；私有文件0600，祖先目录不可由不可信主体替换。禁止/tmp、相对路径、符号链接或临时shell覆盖。初始化不自动创建缺失祖先目录，也不修正已有文件权限。

使用[渲染器](../../../../../scripts/render-model-gateway-launchagent.py)生成待审查plist。`--workspace`应指向独立冻结发布目录，ProgramArguments使用其`.venv/bin/python -m app model-gateway --host 127.0.0.1 --port 18091`。WorkingDirectory同发布目录；EnvironmentVariables显式写规范根与批准的STORE_ID，不写凭据值。生成器不安装、不启动，RunAtLoad=false/KeepAlive=false；[本轮示例](../../../../maintenance/HISTORY_INDEX.md)中的OWNER、路径与id均为占位。

运行签名和管理密钥来自已授权的0600私有文件：`.../gateway-runtime/signing.key`、`.../gateway-runtime/management.key`；文件和环境值并存会拒绝。不能从报告重建缺失运行密钥。若需要换密钥，必须另列变更及所有历史/能力影响，不能本轮静默轮换。

Provider Worker只配置`MODEL_GATEWAY_READINESS_KEY`与`MODEL_GATEWAY_STORE_ID`，**不配置管理密钥**。readiness默认可由Gateway/API现有管理密钥以固定只读域HMAC派生；运维人员须在获授权后只把派生能力提供给Worker。也可显式配置独立readiness密钥，但API、Worker、Gateway必须逐项一致。这个能力不能申请grant/save。Gateway/运营密钥均不可注入Sandbox、源码、Git或业务Artifact。

本轮实际合成测试在两个自有随机loopback端口完整启动/关闭Gateway；没有在真实18091重启服务，也没有读取当前Gateway密钥。该证据不能替代未来安装后的实际PID/路径/卷核验。

## 切换和验收顺序

1. 暂停新业务派发，确认无在途/未知Provider操作或新审批。先取得单独停机授权，停止已验证的旧启动器/实例，避免父进程自动重拉起；不以健康响应猜测旧实例退出。
2. 在私有恢复边界完成候选验证、备份及匹配测试。新存储服务打开期间持有共享lease；维护必须取得独占lease。该锁只管理同一新仓库，不代替全机多实例/旧格式写入器盘点。
3. 维护命令冻结期拒绝活动服务；整代stage完成、逐文件验证/fsync后，再替换一个current.json并fsync父目录。保留旧代与未选中stage。崩溃前指针仍旧代，崩溃后只能指向完整新代，不逐文件覆盖活跃路径。
4. 只有授权后才安装/加载精确plist并启动一个新Gateway。新进程加载同一代的密钥和DB；核验真实监听者、版本、端点、配置及磁盘材料，不用旧内存对象解密新DB。
5. 零Provider检查：health、经认证storage_readiness、冻结实现/Review连接的credential_readiness。逐项比较store_id/generation/profile/ref摘要、nonce/签名、时效与源版本；故障注入只能在合成副本中进行。真实探针不申请grant、不写凭据、不创建Job/审批或消费授权。
6. 实际恢复与readiness通过后仍停止在新的执行准入之前。源码、镜像、配置已变化，必须新冻结和独立精确授权；本轮不创建Plan17或任何付费验收。

## 回滚矩阵

| 情况 | 安全动作 |
| --- | --- |
| staged候选未完成、校验失败或before_pointer崩溃 | 指针仍旧代；保留孤立stage/现场，不自动接管或重新初始化。旧代坏则停机 |
| after_pointer崩溃、完整新代可读 | 新进程只读验证完整代，可在另行启动授权下重开；不能自动开始业务 |
| 新代未产生管理写入且旧代完整可验证 | 停服务取得独占lease后按精确CAS执行rollback，验证旧代、重启同布局兼容版本、再次readiness |
| 新代已有grant/save等写入 | rollback拒绝：回退可能丢新配置；保留新代和备份，停机独立审查/另行导出方案，不丢弃记录 |
| 旧代缺密钥、热日志损坏、身份不匹配 | 不切回不可用旧代，安全停止；不能chmod、补表、补密钥或把失败称作恢复 |
| 新布局回滚旧源码 | 禁止让旧自动初始化构造器打开generation仓库根。只能选择已审查的兼容发布物与完整健康候选；原事故现场没有已证明健康回滚目标 |

`fsync`与进程退出测试不等于硬件断电/磁盘损坏证明。生产卷、macOS/OrbStack和Linux amd64的真实恢复演练，以及独立源码签署仍是部署前置门禁。任何失败/未知结果停止，历史预留与费用不退款、不重分配、不扩容。
