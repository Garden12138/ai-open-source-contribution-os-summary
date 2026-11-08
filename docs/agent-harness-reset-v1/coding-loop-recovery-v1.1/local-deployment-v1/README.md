# Coding Loop Recovery v1.1 本地隔离部署验收

用户完成源码独立审查后，另行授权本轮原五角色的受控部署与离线验收。
**本地范围验收通过，等待人工审查；下一轮付费执行 No-Go。**
没有调用真实模型、创建 Plan16、恢复 Plan15、发起执行审批、增加预算、Push、PR、
评论或对外发布镜像/代码。Linux amd64 未运行，不能据此宣称跨架构生产就绪。

## 冻结输入与镜像

Git HEAD：`0e6d43f8c6d32034d5af5fc2407c08c3d5d430bb`。
源码提交：`bd3f8927bfed1a280b8d8ae15cdd446d30357917`。
工作区生产源码和测试保持已提交字节；完整/增量 Diff Hash、源码/测试文件摘要及
完整 Patch 逆向检查均通过，包含上一轮可信警告送达修复。

| 项目 | 部署前 | 部署后 |
| --- | --- | --- |
| 生产文件 | 254 | 255 |
| 源码集合 Hash | `31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95` | `627a3689ca6647f8747796469f7e93fddff23a8445557066a7cae061e3346d47` |
| app 镜像 | `sha256:ba41b3d68e9281a7e5ce965a2d2780fd30b4f997f1ac6b2bc1ffed52b541313a` | `sha256:c9c07093cc6d8cd001c3fbd3d39a819580da5ab4e16b7c0990bfcd0f13d7917f` |
| Sandbox Worker 镜像 | `sha256:2b0e9e989c01562706169f2f5fe6e856f558c8b251ddc3c8926ca9371919f2d2` | `sha256:1c82d9a6e0a0143d13a1ba3ab2728cec19472130f28a346172c87daacbbe9476` |

API、普通 Worker、Provider Worker、Publisher 使用 app 镜像。上述 Digest 是本地
不可变镜像配置 Digest（Docker Image ID），没有伪造 Registry RepoDigest 或推送镜像。
构建仅从原已部署镜像增加已审查源码 COPY 层；不拉取镜像、不联网、不安装依赖。
原层、镜像运行配置及所有迁移文件逐项核对不变。[构建证据](build-report.json)。

Runner 保持：
`sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6`。
冻结上游归档仍为 `d269dc46fd82a9ae5d443b95aa389bb6637856f28f5e5cb7da0d0392384b2cdb`。

Native tools Schema Hash：
`52999661311852705547ff40da95c4d33cf16c61fc03ed7deff51b99418f7226`；
transport Hash：`b661a6b2bc3b7c5e6bd83a307024e26e529f82625bd9aba263c95cb2b48d9174`；
Prompt：`native-coding-loop-recovery-v11`。五角色均实际导入新镜像 `/app/app` 源码并
核对这些值、6+8+1策略和跨模块选择策略，完整255文件集合也均匹配。

## 部署过程与健康

[部署前校验](preflight.json)全部通过后才停旧角色。原容器不删除，原数据库/Artifact
业务卷与 Publisher 私有卷均保持，原资源、安全、环境、挂载配置逐字段复用。
原 Host Gateway 与只读代理未重启。普通服务先启动，重新核验完整历史与活动 Job=0，
再最后启动 Provider Worker；没有创建业务 Job。

| 角色 | 部署后健康依据 |
| --- | --- |
| API | `/health` 返回状态/数据库正常；PID1及模块检查通过 |
| Publisher | `/health` 正常；PID1及模块检查通过 |
| 普通 Worker | PID1存活、预期进程/模块、无运行错误；无配置的HTTP健康接口 |
| Provider Worker | PID1存活、预期进程/模块、无运行错误；无配置的HTTP健康接口 |
| Sandbox Worker | PID1存活、预期进程/模块、无运行错误；无配置的HTTP健康接口 |

原五容器没有 Docker HEALTHCHECK，本轮没有添加或伪称通过该探针。
所有角色 UID/GID `501:20`、只读根、drop ALL、no-new-privileges、1 CPU、128 PIDs
保持；普通四角色内存512MiB，Sandbox Worker为2GiB。原业务网络保持，Sandbox Worker
仍为 network none，Docker socket仍只属于原可信Sandbox Worker；目标Runner无网络、
无凭据、无宿主敏感挂载。[部署记录](deployment-report.json)、[最终安全审计](postdeploy-audit.json)。

## 部署后离线验收

| 验收 | 结果及分类 |
| --- | --- |
| v1.1定向边界 | 与部署相同的app镜像、Python3.11中50通过，零失败/跳过 |
| 恢复、身份、预算、Review门禁 | 同一镜像中53通过，含真实子进程退出/重启与事件/调用ID保持 |
| OCI | 28项真实受限OCI通过，另2项纯单元检查通过 |
| 保存信封 | 同一app镜像中Plan12–15共7组通过，201个导出Artifact SHA256核验通过 |

各组有重叠，不相加为新的独立用例数。本轮没有重新运行全部2099/83默认套件；
其原归档保持，生产源码未改，本轮新增证据是已部署镜像中的50/53与新OCI回归。
[JUnit索引](test-report-index.json)、[总验收报告](acceptance-report.json)、[场景证据](scenarios.json)。

测试框架从本地已有缓存复制纯Python pytest及依赖，以只读目录提供，未安装进生产
镜像。输入为已提交测试和脱敏fixture；无网络、Docker socket、生产卷、宿主HOME或
真实模型凭据。合成SQLite/Artifact在私有tmpfs，只有独立空的临时证据目录可写。
子进程测试的cwd别名指向同一镜像的实际app源码，避免环境清空后误读旧安装包。

保存批次为4+2、4+1、4+1、4+4、4+4、4+4、4+4，原调用ID、状态、分页恢复与计费
守恒保留。容量证明仍覆盖68338字节真实源码页。[保存信封](saved-envelope-replay.json)、
[OCI容量证明](oci-capacity.json)。实际OCI使用固定Runner/冻结归档，覆盖成功/拒绝Patch、
最新源码Verify、受限资源与路径/符号链接/Hash安全拒绝。

第9轮成功路径的独立Reviewer是确定性替身，停止于人工验收前；这不是原Issue实现
成功，也不是一次真实模型自主Coding或真实付费Review。6轮纠偏、8后条件机会、9轮
仍无实际成功Patch就停止、Prompt不含可信新增字节时不派发、重复/未知送达不授予机会、
模块公平与原始页恢复等断言均保持。

## 回滚、历史与凭据

五个旧容器保留为stopped，旧镜像可用；旧角色注册文件单独保留，卷没有删除或重建。
数据库备份仅在仓库外0700私有目录、0600文件中，报告只含位置与摘要。
旧镜像已在无网络、只读根与有界tmpfs中实际读取恢复副本，完整性/外键/45表Hash均
与冻结记录一致。[回滚证据](rollback-readiness.json)。不自动写回数据库或回退历史费用。
需要回滚时应先确认没有新业务事件，再仅切换原容器；有未知数据差异时停止，不覆盖数据。

最终query_only审计再次确认45张表全部行、11个旧Checkpoint、Plan11–15事件链/终态/
审批/费用/预算/未知占用不变，1802个Artifact原字节Hash与凭据检查通过。模型启动106、
执行授权13、Plan审批13、任务事件2551、活动Job=0。新迁移0，新审批/业务Job/模型调用0。
[部署前历史](history-before.json)、[部署后历史](history-after-tests.json)。运行日志只在
内存扫描凭据/错误并保存摘要，不归档原日志，真实密钥只在原配置复制内存中处理。

## 测试工具探针的纠正与限制

保留最初未通过的工具探针，不当作应用成功或隐藏其失败：

- WAL头的SQLite序列化镜像不能由最初的纯内存读取方法打开；备份本身只读完整性与
  45表核验通过后，改为旧镜像在有界私有tmpfs中做真实文件恢复读取。正式数据库未变。
- 缓存pytest导出最初遗漏其py.py兼容模块，未执行用例；补齐同一缓存包，没有安装依赖。
- 测试环境默认API导入路径落在只读输入目录，产生3个收集错误、执行用例0；显式
  指向临时SQLite/Artifact路径，根文件系统与测试输入保持只读。
- 首次额外恢复组51通过/2失败：子进程故意清环境，无法找到外挂pytest，尚未到崩溃
  检查点。补齐只读cwd别名后原53项完整重跑通过，未改测试断言或生产代码。

这些工具fixture修正有独立JSON与脱敏失败JUnit，不修改生产安全策略、镜像依赖或持久
数据范围。没有遇到生产源码/历史/镜像来源不一致后继续部署的情况。

## 下一阶段 Go / No-Go

**Go：本地隔离部署和离线验收完成，可审查本报告。No-Go：任何自动付费执行。**
high reasoning与Coding单次4096保持；模型24、读64、写24、检查6、协议纠偏2、Provider
重试2、代码修复2、输入200000、输出32000、证据500000字节、1800秒未增加，独立
Review原上限未改。条件机会最多额外1次实际调用、输入32000/输出4096，从原总额度
扣除；仍可能截断，保存第7轮少1024字节的上下文风险也没有由离线通过消除。

原Plan15绑定与新源码/协议/镜像不兼容。未来仍需新的冻结、独立绑定及单独精确授权。
当前不创建这些对象。Linux amd64门禁保持NOT_RUN/未通过，不宣称跨架构生产就绪。
本轮源码与测试未改、Git HEAD未动，报告未自动Commit或Push；完成后停止等待用户审查。
