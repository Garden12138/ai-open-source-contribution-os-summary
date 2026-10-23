# AI 贡献方案工作台

入口“制定贡献方案”打开独立任务对话。发送要求即可接续工作，运行中可以继续补充或停止。
方案、结果与确认按时间显示；长方案、代码、测试、发布预览及协议使用同一个详情视图。
修改方案或文案直接在对话中说明。模型移到设置，归档从任务列表操作。
新版消息、恢复与升级契约见 [会话协议](conversation-agent-contract.md)。
模型连接／阶段覆盖、主题、卡片详情和升级配置见 [Studio 使用说明](studio-workbench.md)。
AI 从贡献任务的不可变分析和
Issue 快照出发，读取固定提交的仓库代码；信息不足时提出问题或补充阅读文件。
规划期间不执行仓库代码，也不创建 Fork。

## 使用流程

1. 说明改造目标、回答必要问题。方案回复简述修改范围和测试安排，“查看方案”打开全文。
2. 需要调整时直接回复；新方案保存为子版本，保留父版本和原有批准记录。
   代码依据或规划状态变化后必须重新确认，旧页面不能执行旧方案。
3. 点击“核对方案并继续”，查看完整方案后点击“确认方案并开始”。后台校验当前方案、代码、执行环境和模型，重新读取上游。
   依据变化时会停止并说明需要重新规划。晚些确认不会使任务失效；期间补充要求后应等待处理完成并核对最新方案。
   聊天中的“确认方案”只会返回真实执行条件和按钮提示，不会授予执行权限。
   Node 项目会读取工作区、`packageManager`、Node 版本声明和原始 npm/Yarn/pnpm 锁文件，按项目配置准备隔离环境。
   配置缺失时先补读；没有锁文件、声明冲突或超出资源预算时说明具体原因。
4. Sandbox Worker 执行 Explore，准备冻结依赖并运行修改前基线；模型生成限于批准文件的 ChangeSet；隔离容器
   完成 Implement 和无网络 Verify；独立模型检查实际 diff 和测试产物。
   失败测试、未运行测试或阻断问题不能被模型的 `pass` 覆盖。审查阻断时最多
   自动修复两轮；基础设施失败停止并显示错误，不转为 Fake 执行。
5. 通过后收到结果回复。查看改动、测试和审查，再点击“确认结果”；随后自动准备文案与预览。
6. 在“设置 → GitHub 发布”连接发布账号后，可在对话中要求修订标题／正文。这一步只读远端，
   在受控临时 Git checkout 中重建并验证真实提交。预览包括账号、上游、Fork、
   分支、base/head、diff／测试／review 哈希、PR 文本和动作列表。
7. 最终确认有效期为 30 分钟且只能使用一次。确认后才允许创建或复用 Fork、
   Push 精确提交和创建 **Draft PR**。不会自动转正式 PR、评论、合并或强推。

执行授权不等于发布授权。仓库内容、模型答复和历史确认均不能授权新的外部动作。
停止操作会取消规划和执行子任务；发布确认后不能中途取消远端写入。
停止只作用于当时已有的任务。未处理的补充信息保留，发送“继续”后按顺序接续；
旧停止请求的重放不会取消新一轮讨论。取消状态会明确显示在对话页，重新发送不会
自动重新授权执行或发布。

`planning-v5-independent-actions` 允许说明、问题、补读请求和方案草稿组合返回。
系统先等待问题答案，再补读必要文件，最后校验并保存完整方案；等待答案或代码的
草稿不能批准。外层 `reply` 可省略，有效说明也可单独返回；实施步骤和测试项
必须是字符串数组，仅 `commands_to_run` 接受结构化命令对象。

补读按冻结仓库清单逐个校验：有效路径继续读取，不存在的路径和 `content=null`
的不可读文件不会拖累其余请求，也不会被当作已读证据。提问附带的补读在收到
答案后同样遵循此规则。`assistant_message.read_paths` 只保留本次有效路径，
`read_feedback` 记录 `planning_read_invalid`、不存在／不可读路径及固定修正提示；
危险路径只记录数量，不写入原文。模型通过下一次请求中的 `read_feedback` 获得
实际工具反馈，不能猜测替代路径或绕过敏感内容检查。全部路径无效时最多自动
修正两次，仍受每次请求四轮补读、任务总调用和 Token 预算限制。

拒绝的方案和读取请求追加 `model_output_rejected` 安全诊断；读取反馈、回复用量、
子任务入队和本轮完成原子保存，重启后继续使用原有答案。达到补读上限会保留
明确回执，不回滚整轮。敏感输出仍拒绝保存且不触发修复；缺失字段和其他可修复
的结构错误使用同一个有界修订预算。任何补读、修订或聊天回复均不授予实施或发布权限。

同一仓库最多支持四个 Python 包共同修改。工作台为每个包独立冻结依赖与本地包
闭包，因此各包可以保留不同依赖版本。方案须在每个包自己的目录安排既有 pytest
基线；所有命令按目录选择对应环境。修改前检查失败会进入持久化的自动诊断：能在
已批准文件范围内处理、且无需改变环境或命令的代码问题，进入原修复与验证链；
需要额外文件、依赖、命令或新依据时，自动准备具体的子方案，供单独确认。环境
失败、证据不完整或诊断未完成时不会开始修改。最终验证和独立审查覆盖全部所选包，
不能把原有错误记录当成最终检查通过。适配就绪不代表项目测试已经通过，仍需核对当前方案并
单独确认开始。只读挂载、断网验证和一次性授权保持不变。

执行详情分别展示“修改前测试”和“修改后测试”。前者失败时可查看命令的标准输出
与错误输出；尚未实施时不会将修改前测试显示成修改后的验证结果。历史基线缺少
摘要输出时，从原始不可变检查证据生成只读展示，不改写原记录或清除阻塞。
诊断本身不能授予改动权限。新确认绑定 `baseline-feedback-v1`；旧授权只能恢复
只读诊断与修订方案，不能在后台补上新的执行许可。每次执行最多诊断两个基线，
每项诊断最多两次模型尝试，仍计入任务的规划调用和 Token 总预算；原代码修复
次数不增加。停止、取消、模型/方案/源码变化会使诊断或恢复失效。

当前状态显示在任务顶部，与任务列表使用相同的状态来源。页面每 2 秒读取状态，
离开任务时清理轮询。Enter 发送、Shift+Enter 换行，中文选字不会误发；Ctrl/Cmd+Enter 兼容。
发送失败保留原文，同一浏览器标签页刷新后恢复未发送草稿。用户阅读旧消息时不会自动跳到底部。
旧原文与诊断保留在“历史记录”，不改写原始证据。

## 服务与配置

所有应用进程必须使用同一份 SQLite 数据库和 Artifact 根目录。API、协调器、
Provider、Sandbox Worker 和 Publisher 分开运行；不要把所有 `.env` 变量传给
每个进程。已有 Compose 的 API、协调器、Provider／Gateway 配置仍适用。

| 进程 | 需要的配置／访问 |
| --- | --- |
| API | 本地访问令牌、数据库／产物、模型名称、Runner 摘要、执行签名、发布模式；无 GitHub token、模型密钥和 Docker socket |
| `contribos worker` | 只读 GitHub token、数据库／产物、执行签名、Runner 摘要、实现和审查模型身份；无 Docker socket |
| `contribos provider-worker` | 数据库／产物、内部 Gateway 地址及任务令牌签名；无 GitHub 凭证和 Docker socket |
| `contribos sandbox-worker` | 数据库／产物、Docker supervisor 配置、执行签名；无 GitHub／真实模型凭证。子容器只获得批准的只读输入或一次性工作区 |
| `contribos publisher-worker` | 数据库／产物、GitHub CLI 登录配置；无 Docker socket、执行签名和模型凭证 |

启用规划／执行需要 Studio 中配置规划、实现和审查模型，或保留旧部署的
`IMPLEMENTATION_PROVIDER=nvidia_nim`、`REVIEW_PROVIDER=nvidia_nim`。另外仍需
`SANDBOX_STAGE_RUNTIME=docker`、有效的
`SANDBOX_JOB_SPEC_SIGNING_KEY` 和 `WORKBENCH_RUNNER_IMAGE=sha256:<digest>`。
旧环境配置的规划使用实现模型；Studio 可独立覆盖规划模型。
审查始终使用独立调用，不复用实现模型的输出作为审查证据。

构建现有 Runner 并读取 **Docker 实际可寻址的镜像 ID**：

```sh
docker build -f containers/sandbox-runner/Dockerfile -t contribos-sandbox-runner:workbench-v1 containers/sandbox-runner
docker image inspect contribos-sandbox-runner:workbench-v1 --format '{{.Id}}'
```

把输出配置为 `WORKBENCH_RUNNER_IMAGE`。不要使用构建日志中的其他中间摘要。
Sandbox supervisor 必须可以读取与其他进程相同的产物；其归档及临时文件的
绝对路径也必须能被 Docker daemon 挂载。现有 Compose 持久卷不会自动变成本机
`./data`。采用宿主机 Sandbox Worker 时，应先配置一份共同的专用数据目录并在
容器与宿主机使用一致路径，或部署已有的隔离 Worker 环境；不能混用两个数据库。

已有 Compose 部署也可启用 `sandbox` Profile，直接复用 `contribos-local-data`
持久卷，无需复制或迁移业务数据库。先读取 Docker daemon 中的数据卷路径：

```sh
docker volume inspect contribos-local-data --format '{{.Mountpoint}}'
```

在本地 `.env` 设置 `COMPOSE_PROFILES=nvidia,sandbox`、
`SANDBOX_STAGE_RUNTIME=docker`、上文核实的 `WORKBENCH_RUNNER_IMAGE`，以及
`SANDBOX_DATA_ROOT=<上一步输出的绝对路径>`，保留已有执行签名密钥。
`SANDBOX_DOCKER_SOCKET` 和 `SANDBOX_DOCKER_GID` 分别为 daemon 所在环境中的
socket 路径和组 ID；OrbStack 默认使用 `/var/run/docker.sock` 和 `0`。
这两个值不能指向宿主机的其他目录。

在现有 API 运行时准备专用临时目录，然后构建并更新服务：

```sh
docker compose --env-file .env -f docs/deployment/compose.yaml exec -T api python -c 'from pathlib import Path; Path("/data/sandbox-tmp").mkdir(mode=0o700, exist_ok=True)'
docker compose --env-file .env -f docs/deployment/compose.yaml --profile sandbox build sandbox-worker
docker compose --env-file .env -f docs/deployment/compose.yaml --profile nvidia --profile sandbox up -d --no-deps --no-build api worker provider-worker sandbox-worker
```

Sandbox Worker 是可信 Docker supervisor，以 UID `10001` 运行，网络关闭，仅有
业务数据卷、执行签名和 Docker socket。API、协调器和 Provider 不接触 socket。
其持久卷挂载位置与 daemon 路径相同，`TMPDIR` 使用该卷中的专用目录，保证
Runner 能挂载同一份输入。规划及代码读取时，Runner 只挂载两个临时只读文件，不接触数据库、
整个产物目录、socket 或凭证。归档临时副本经 hash 校验，使用可供 Runner UID
读取的权限，原始归档仍为私有文件；成功或失败后均删除临时副本。

`restart` 不会载入新的环境变量；修改 `.env` 后必须使用 `up -d` 重新创建
受影响的服务。首次启用前应备份数据库和产物。撤销配置时停止
`sandbox-worker`，恢复原来的执行模式并清空 Runner 配置，再重新创建
API／Worker／Provider；不要删除持久卷或用备份覆盖新增业务数据。

发布模式默认 `PUBLISHER_MODE=none`。`fake` 只显式启用原有离线演示 API；
真实工作台发布只能使用 `gh`，不会回退到 Fake。Publisher 只操作 Git 数据，
不运行仓库 hooks、构建脚本或测试。发布端从执行授权的审计链读取冻结工作区范围，
将范围及其哈希绑定到发布预览；只物化该范围，逐文件核对沙箱基线与结果清单。
范围内的原有、未改动符号链接只校验 Git blob 的目标文本，保留在索引中，不物化
也不跟随链接；新增、修改、删除链接或子模块、额外文件、内容或权限差异都会失败。
清单格式直接复用沙箱 `FileInventoryEntry` 契约（`type` 字段）。范围外的原始
Git 对象完整保留在索引和最终提交中，不物化、不执行，也不纳入本次改动。
最终 Git diff 必须与已审查改动路径完全一致。旧的完整仓库任务仍验证完整清单。

发布预览有效期为 30 分钟。页面显示到期时间，到期禁用确认。需要调整或重新准备时
直接在对话中说明；重建预览使用新 nonce，必须再次查看并确认。
上游分支前进时，不再仅凭 SHA 不同要求重做：Publisher 只读证明已审查基线是
新上游的祖先，且执行授权中每个工作区根的 Git 对象（含完整子树、文件模式和
链接）完全相同，才可沿用旧验证结果。检查使用完整、未截断的 Git tree API，
不依赖 Compare 的截断文件列表。任一工作区变化、历史分叉、证据不完整或旧的
全仓库任务都要求重新读取、确认方案并在沙箱验证；页面提供直接重规划入口。
这不会 rebase 或改变冻结的 `base_sha`、档案、方案、diff、测试、审查和验收记录。
预览另存 `target_base_sha` 与 `publication-upstream-check-v1` 证明，绑定当前观察到
的发布目标；完整预览 hash 仍进入一次性确认、审计和持久发布 Job。确认后的
写入前、创建 PR 前都核对这个精确目标，变化时停止，不自动扩大旧确认。
本改动复用现有 append-only JSON 事件，不改变数据库结构；回退前停止 Publisher，
排空发布队列，并使新预览过期后重新生成，不能把新目标字段解释成新的执行基线。
受控 Git 输出、超时、目录大小和容器资源均有限制。当前不支持通过不可重建的
补丁（例如缺少模式变更信息）发布，核对失败会停止。

Compose 默认启动独立 Publisher 服务，管理接口只在 API/Publisher 专用内部网络
开放，不发布宿主机端口。设置页提供连接、状态、重新检测、换绑和确认断开。
账号连接检测使用持久 `publisher_account_probe` Job，仅 GET GitHub `/user`，
核对 Classic Token 的 `public_repo` 或 `repo` scope；检测成功不代表对任意仓库
拥有权限，准备发布预览时还会核对目标仓库及上游 SHA。
公开仓库贡献推荐仅授予 `public_repo`。当前页面连接不支持 Fine-grained Token；
GitHub 对“贡献非成员公共仓库”的现有限制见
[官方 Token 文档](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)，
scope 说明见 [官方权限文档](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)。

使用已有 `.env` 部署：

```sh
./scripts/deploy.sh
```

部署脚本为已有 `.env` 一次性生成独立 `PUBLISHER_MANAGEMENT_KEY`，只提供给
API 和 Publisher，不复用模型或沙箱密钥；直接运行 Compose 时可先执行
`python3 scripts/ensure-publisher-key.py`。这不是 GitHub Token。页面用 Publisher
公钥封装 Token，API 仅转发密文；私钥和加密凭证只位于
`contribos-publisher-secrets` 卷，API/Provider/Sandbox 均不挂载该卷。
业务数据库仅保存凭证引用、账号、scope、检测时间和审计链。Publisher 内部
将解密 Token 临时提供给固定 gh 子进程，结束后清除，不导出到产物或日志。
连接成功自动启用工作台真实发布入口，不需要修改 `PUBLISHER_MODE` 或重启 API。
`PUBLISHER_MODE=fake` 保留显式离线演示用途，独立 CLI `publisher-worker` 保留
旧的 gh 登录部署方式；新的 Compose 使用 `publisher-service`，不再挂载宿主机
gh 登录目录。启动检查拒绝混入发现用 Token、模型／沙箱密钥及可写 Docker socket。

换绑、重新检测与断开要求精确 `account_hash`；旧页冲突返回 409。发布预览和
发布 Job 绑定该账号版本，账号变化要求重新预览。存在进行中的检测/预览/发布
或尚未核清的发布写入时禁止换绑/断开。断开仅移除本机凭证，不调用 GitHub 撤销
接口；如需撤销 Token，在 GitHub 账号设置操作。

该切片复用现有 AuditEvent/Job 和凭证库 schema，无业务表迁移。备份/恢复需同时
保留 SQLite、产物及 Publisher 私有卷（master.key、envelope.pem、credentials.sqlite3）；
仅恢复业务数据库不能恢复凭证。旧实例没有账号事件时显示未连接，不伪造登录。
回退前停止 Publisher、排空账号检测及发布队列，再部署旧服务；保留私有卷和
新审计事件，旧版不会将其视为发布授权。手动 gh 部署继续使用旧独立账号方案。

## 失败与恢复

规划失败会区分“下载仓库归档”“读取仓库代码”和“规划模型回复”三个阶段。
读取失败后，使用“重新读取上游代码”生成新的归档和上下文，失败历史保持不变。
GitHub API 返回的 `legacy.tar.gz` 下载地址会先核对仓库、完整提交号和允许的
HTTPS 主机，再转换成同一提交的 `tar.gz` 地址；下载请求不携带 GitHub 凭证。
这样归档根目录与 Runner 的完整 SHA 校验保持一致，无需放宽目录安全检查。

若仓库文件包含凭证模式，规划上下文会保留文件名并将内容标记为不可用，
不将敏感文本存入上下文产物或发送给模型。其余安全文件继续参与规划。
不可用文件不是新文件，也不能作为已阅读证据；方案不得修改这些已有文件，
需要它们时由模型提出澄清。重试开始或成功后，页面不会继续显示历史任务错误。

模型服务失败和回复格式不合法会分别提示，已有上下文保留，可直接发送消息重试。
NVIDIA 规划请求只使用规划阶段的输出契约，不混入分析阶段的引用字段。
若已读取的上游代码已解决 Issue，模型应说明证据并提出下一步选择，不能生成
无实际改动的方案。服务诊断只记录异常类型及校验错误类型，不记录回复原文。

长操作全部为持久 Job，浏览器关闭不影响运行。每个任务最多 32 次规划模型调用，
每条消息最多 4 轮补充阅读。取消后的迟到模型结果不落成方案。方案、快照、
上游 SHA、上下文、镜像、策略或模型身份变化时，旧授权失效。

确认发布时先原子写入真实 `PublishIntent`、确认事件、审计及发布 Job，然后
Publisher 才可能写远端。Fork／分支采用固定目标并先查询核对；PR POST 前记录
持久事件。若 POST 超时，重试只查找匹配的 Draft PR，不重复创建。UI 的
“重试核对发布结果”复用原 Job；最多三次 Job 尝试。始终无法确定的结果会停止，
保留确认和证据，需人工核对远端；不能另造确认、删除记录或强推来绕过。

迁移 `0029_workbench` 保留所有既有方案和演示 PR；升级前的方案没有阅读上下文时
需重新规划。详见 [数据库迁移与恢复](database-migrations.md)。

## API

所有路径以 `/api/v1/tasks/{task_id}/workbench` 为前缀。浏览器变更要求同源、
CSRF 和已配置的本地访问令牌；CLI 使用本地令牌。

| 方法／后缀 | 含义 |
| --- | --- |
| `GET` | 状态、对话、版本、代码依据、Jobs 与配置能力 |
| `POST /messages` | 消息及父版本 ID/hash，可指定 `refresh` 重新读取上游 |
| `POST /plans` | 保存编辑后的子版本及上下文 hash |
| `POST /execute` | 明确批准精确方案并启动自动执行，`max_repairs=2` |
| `POST /stop` | 取消子任务，结束后返回编辑 |
| `POST /publication` | 编辑后的 PR 文本，排队只读发布预览 |
| `POST /publication/confirm` | 精确预览 hash 及一次性 nonce，排队发布 |

除最终确认外，上述 POST 需要 `Idempotency-Key`。版本或授权冲突返回 409，
未配置的功能失败关闭，错误不返回原始 subprocess／GitHub／模型异常。

## 验证记录（2026-09-07）

发布账号设置接口统一前缀 `/api/v1/publisher-settings`：

| 方法／后缀 | 含义 |
| --- | --- |
| `GET` | 发布服务、账号、scope、检测 Job 和安全错误状态 |
| `POST /credential-grants` | 为指定 connection_id 获取短期公钥封装授权 |
| `POST /credentials` | 提交密文 envelope、expected_hash，返回持久检测 job_id |
| `POST /test` | 按 expected_hash 重新检测已保存账号，返回 job_id |
| `POST /disconnect` | 按 expected_hash 断开并移除本机凭证 |

这些 POST 均要求 Origin/CSRF 或本地令牌；除 grant 外还要求 Idempotency-Key。
凭证 schema 拒绝 token 明文字段，错误不反射原始请求输入。检测 Job 可通过
既有 `/api/v1/jobs/{id}/cancel` 取消，失败后在页面重新检测；账号变更前必须
完成/取消当前检测，不能让迟到结果覆盖新账号。

2026-10-11 设置页切片另见 `tests/test_publisher_settings.py` 与
`tests/publisher_settings_browser.mjs`。默认测试使用确定性 GitHub fake；真实账号
连接、目标仓库权限及真实 Draft PR 验收仍需用户在页面输入凭证后完成，不冒充通过。

完整离线回归为 `352 passed, 11 skipped`。离线测试覆盖提问／回答、编辑与陈旧版本、取消后的迟到答复、上游移动、完整自动
流程、两轮修复上限、重新规划再执行、一次性发布、响应丢失核对、真实 Git
提交重建和迁移保留旧 Draft／外键。Fake 外部接口不代表真实 GitHub 验收。

浏览器使用临时数据库验证保存、差异和轮询，`1280×900`／`390×844` 无横向溢出
或脚本错误。真实 macOS arm64／OrbStack 用 Runner
`sha256:8e60dac389c292d775edbb6b625c69fd26868ae42004e402120fbcb6e40203f6`
验证只读规划检查及路径穿越拒绝。可重复运行：

```sh
CONTRIBOS_PLANNING_TEST_IMAGE=sha256:<local-image-id> python -m pytest tests/test_planning_context.py
```

本次未重启现有业务服务，未用真实模型执行贡献，也未创建真实 Fork／PR。
真实模型全流程、专用测试账号的 Draft PR 验收与原生 Linux amd64／Docker Engine
验收仍需完成；Phase 5 的 P5-G06 保持未完成。

## 规划修复验证（2026-09-08）

修复 Runner 配置、归档目录格式、读取权限、敏感示例文件处理以及模型阶段提示词后，
已更新本地 API／Worker／Provider／Sandbox 服务。`gptme/gptme#3698` 的真实归档、
代码读取、模型请求补充阅读和最终澄清回复均已成功，最后一个 Job 为
`200763e1-19d1-48c2-bb2b-3b6659599073`。上游已有核心启用提示和相应测试，
工作台等待用户选择下一步改进范围；没有生成空方案或自动授权执行。

完整离线回归为 `373 passed, 11 skipped`，Python 编译、JavaScript 语法、差异检查、
凭证 canary 和真实响应／日志脱敏检查通过。真实执行与审查全流程、Draft PR 和
原生 Linux amd64／Docker Engine 验收仍未完成。


## 服务重启记录（2026-09-08）

已按用户要求重建并重启现有四个 NVIDIA Profile 服务，数据库升级到
`0029_workbench`，既有数据及所有五个任务页面检查通过。升级前备份保存在
持久卷 `/data/backups/pre-workbench-20260908T010332Z`。该次重启保留现有模拟
执行配置，AI 规划仍需配置 Runner 镜像和独立 Sandbox Worker，真实发布未启用。

同日修复“尚未配置规划使用的固定 Runner 镜像”：本地 `.env` 已绑定上述实际
Runner ID，切换至 `SANDBOX_STAGE_RUNTIME=docker`，并启用独立 `sandbox`
Profile。更新前的数据库及产物备份位于
`/data/backups/pre-sandbox-config-20260908T025134Z`。6 个现有贡献任务接口均返回
`planner_available=true`。真实 Compose Sandbox Worker 以 UID `10001` 完成规划
检查和代码读取，拒绝路径穿越归档，未运行仓库脚本；私有归档权限保持 `0600`，
临时只读副本完成后清理。完整离线测试为 `358 passed, 11 skipped`。
这次验证未调用真实模型生成方案、执行用户贡献或创建 Fork／PR；WB-G03、
WB-G04 和原生 Linux amd64／Docker Engine 的 P5-G06 继续保持未完成。

在仓库根目录，普通重启使用：

```sh
docker compose --env-file .env -f docs/deployment/compose.yaml --profile nvidia restart
```

代码或配置更新后，先按上文备份数据，再重建并重新创建容器：

```sh
docker compose --env-file .env -f docs/deployment/compose.yaml --profile nvidia up -d --build
```

## 英文贡献文案与仓库规范

新增的公开贡献内容统一使用英文：代码注释、docstring、测试名称和说明、文档、
Changelog、commit subject、PR 标题及正文。内部讨论和审查解释仍可使用中文；
既有文本和有意测试多语言的 fixture 不要求改写。规划应读取实际贡献规范，
实现和独立审查都检查这些要求，不凭空声称已满足未读取的规范。

验收后的发布顺序是：

1. 确认实现结果，或在对话中发送“准备发布”。独立 Publisher 只读获取固定
   Git 提交下的 README、贡献指南、包级 CONTRIBUTING、PR 模板和 Actions 工作流；
   缺少贡献指南或模板时读取 owner 的 `.github` 默认社区文件。限制请求、文件数、
   总字节和目录范围，验证 Git blob SHA，不跟随 symlink、不运行任何仓库代码。
2. 独立 Provider 使用讨论模型，结合实际 diff、冻结 Issue、批准方案、真实验证
   命令及结果生成英文标题和完整正文，保留选中模板的章节和检查项。不会截取中文
   规划目标作为标题，也不会把本地测试等同于 GitHub CI。
3. 在详情中核对草稿、仓库规范和人工事项。需要调整时在对话中说明要求。
   常见 Semantic PR action 的字面量 types/scopes/requireScope 会进行确定性校验；
   任意自定义脚本、表达式、regex 和自然语言规范不能全部静态证明，会明确列为人工
   检查项。此功能不宣称所有 GitHub 检查一定通过。
4. 生成文案后自动准备预览，重新读取规范、校验英文正文、标题和模板结构。生成依据和规范
   hash 绑定精确预览；规范改变须重新生成。commit subject 使用审核后的英文 PR
   标题。修改文案须重新准备预览，原有确认不能复用。
5. CLA/DCO 必须由贡献者阅读并亲自决定是否签署；系统不推断法律同意、不自动勾选
   模板。已发布 Draft PR 的 CLA Assistant 固定声明可通过下述独立确认流程提交。
   Fork Actions 的维护者批准也不能由系统替代。确认后、首次写入前再次核对
   规范，包括组织默认文件，防止确认之后规范发生变化。

API：`POST /api/v1/tasks/{id}/workbench/publication/document` 返回持久 Job；
Publisher 的 `publication_requirements` 成功后，原子地排入 Provider 的
`publication_document`，产出 append-only `publication_document_ready`。模型配置
在排队时绑定，单任务最多 16 次生成请求，无无限重试。`POST .../publication`
支持 `document_hash` 和 `template_path`；即使手工提供文案，也必须通过重新读取的
仓库规则校验。仓库信息始终作为不可信数据，不能改变执行权限或发布动作列表。

### 对话内签署 CLA（已发布 Draft PR）

在任务对话中发送“读取贡献者协议”。Publisher 只读核对
发布来源、GitHub 账号与 PR，读取固定提交的 CLA Assistant 工作流和协议，显示
完整原文、固定版本链接、SHA-256 和将提交的完整签署评论。支持经过核对的
`contributor-assistant/github-action` 固定版本与同仓库协议；外部签署平台和其他
协议机制仍需在 GitHub 按说明操作。

阅读并自行决定同意后，勾选协议确认，点击“同意并确认签署”。无需复制授权文本或切换输入用途。
普通聊天中的“确认”、模型回复、开发
功能的同意以及原发布确认均不能替代本次协议授权。此流程无需配置模型。
预览有效期 30 分钟；协议/工作流、账号、PR/head 改变须重新读取并单独确认。
评论提交与 CLA 检查通过分别显示，可点击“核对 CLA 结果”进行只读核对。
核对排队和执行期间按钮显示“正在核对 CLA 结果…”，完成后会在对话中留下
持久的结果回执、核对时间和经过校验的 GitHub 检查链接，按钮旁也显示最近
核对结果。已签署或已通过时协议原文默认折叠，仍可展开阅读。失败会明确提示，
可重新核对；`pending`、未运行、暂不可读取都不会显示为通过。重新打开任务
仍能查看历史核对回执，无需模型参与，也不会重复签署。

API：`POST .../workbench/cla/prepare`、`/cla/check` 创建只读持久 Job；
`/cla/confirm` 接受严格的 preview hash、nonce、agreement hash、完整授权语句和
`read_and_agree: true`，由独立 Publisher 的 `cla_write` 提交固定签署声明。
确认、发送前 outbox 和评论回执使用 append-only Event/Audit，协议存入内容寻址
Artifact，无数据库结构变化。浏览器同源/CSRF 和 CLI Token 保护保持不变。
远端响应丢失时先找相同账号的精确评论；发送标记存在而结果未知时禁止重复 POST、
新签署和更换账号/归档任务，只允许只读核对。确认后的写任务不可中途取消。

回滚：先停止创建新 CLA 确认，让已确认任务完成，并核对所有存在发送标记的
未知结果；确认没有未完成签署后才能降级旧服务。保留所有 Event/Audit/Artifact，
无需回滚数据库结构。不能把未知结果当作失败而重新提交签署声明。

此功能复用开放的 Job、WorkbenchEvent、Artifact 和 JobModelBinding 合约，不改变
数据库表结构，也不修改历史发布或已提交的 PR。回退前先停止新请求并排空/取消未
确认的文案任务；已确认的发布必须先完成或核对到明确远端结果，再过期未确认预览
并回退应用，不能让旧版本接管未结束的新版本发布。

OpenInference 的真实验收发现其指南为无扩展名 `CONTRIBUTING`：要求正文第一行
引用原始 Issue、修改 emitted spans 时附 trace 截图，并要求先获得维护者明确确认。
读取器包含此类无扩展名指南和相关包的 `DEVELOPMENT.md`；首行 Issue 引用会核对
实际任务编号，维护者确认及条件性截图要求以来源标注展示，不自动伪造满足。


### 对话内更新已发布 Draft PR 的标题和正文

在原任务对话中直接输入修改要求，例如：

> 请更新当前 PR #4025 的标题和正文为英文。标题使用 fix(openai): accumulate streamed refusal and reasoning content。正文按仓库贡献指南和 PR 模板编写，第一行关联 Fixes #3785，说明修复、两项回归测试及实际沙箱验证结果，去掉内部规划过程和证据 hash。不要声称 GitHub CI 已通过。先给我预览。

系统自动识别请求，先读取当前远端标题/正文、PR 身份和固定
版本的仓库规范，由原任务讨论模型结合原始已发布 diff、测试及审查生成英文预览。
“能否修改标题”等问句继续作为问答；自然语言分类只创建读取/生成任务。
预览在对话下方展示完整新文案，可展开旧文案比较；不满意时继续在对话中描述修改。

在详情中核对完整旧/新文案后，点击“确认更新文案”。确认和核对结果不需要模型。
普通“确认”、模型输出和原发布/CLA
同意均不能代替本次授权。新预览、过期或账号变化会清除勾选，30 分钟内有效的
一次性授权绑定账号、仓库/PR、base/head、完整旧/新文案、规则以及原始发布证据。
Publisher 只 PATCH 标题和正文；代码、commit、分支、Draft 状态及已签署的 CLA
保持原样。结果在对话中持久显示，重新打开仍可查看。

API：`POST .../workbench/messages` 支持 `mode: "pr_update"`；默认 `question`
模式识别明确的 PR 文案修改请求。Publisher `pr_update_read` 后原子排入 Provider
`pr_update_document`；`POST .../pr-update/confirm` 严格接收 preview hash、nonce、
完整 `text` 与 `approved: true`，创建 Publisher 的 `pr_update_write`；
`POST .../pr-update/check` 创建只读核对任务。任务预算为 16 次预览和 64 次核对，
重试受 Job 合约约束。读取/生成/核对可取消，确认后的写任务不可取消。

发送前重新核对远端旧文案、账号、head/base 和仓库规范，变化则拒绝并要求新预览。
发送标记先持久化；超时/丢失响应后只读核对，禁止重复 PATCH 和开始新修改，
直到确认远端符合已授权文案。确定的 GitHub 拒绝可以重新预览。未知结果时禁止
更换发布账号和归档任务。“核对成功”指文案一致，不代表 GitHub CI 已通过。
GitHub 更新 PR 接口不提供有文档保证的原子条件 PATCH；发送前最后一次 GET
与 PATCH 间仍有极短的并发编辑窗口，执行确认期间请避免同时在 GitHub 手工改文案。
此限制不能靠 SQLite 的本地 CAS 消除，也不能用不受支持的 If-Match 宣称解决。

此功能复用 Job/Event/Artifact/JobModelBinding，没有数据库结构变更。回退前停止
新请求，排空或取消尚未确认的读取/生成；让已确认写任务完成或核对到明确结果，
保留发送标记、授权和回执后才能降级。不能让旧版本接管未结束的新版本写任务。


### 对话内转为正式审核与发布后跟进

原对话创建 Draft PR 后，普通后台协调器会自动接续一次有界的只读核对，
生成审核预览并在原对话展示下一步，无需补发消息或由维护会话代排任务。
队列、发布来源 hash、账号绑定和处理进度持久化；重启或并发调度不会重复创建。
临时读取失败在同一 Job 的两次尝试额度内恢复；取消、暂停、归档和账号变化会停止
接续，失败原因留在原对话。失败后可直接发送“继续”重新核对并准备预览。
已有有效预览会保留并只刷新远端事实；过期后创建新的不可变版本。已观测到非草稿、
已关闭或已合并的旧任务不再排入审核预览；明确要求只读已保存记录时不追加远端读取。

回执显示“系统自动跟进”“手动请求触发”或“维护操作触发”，来源绑定到实际 Job
及发布事件。已核实的历史维护调用通过追加来源更正事件展示，旧回执、Job payload、
actor 和 hash 均不改写；来源更正不能生成确认或授权。

已发布任务可直接输入“请准备将当前 PR 转为 Ready for review，先给我确认预览”。
该请求自动接续当前任务，只创建
`pr_ready_prepare` 只读任务，不需要模型，不会改变 GitHub 状态。Publisher 核对
原始发布链、账号、当前 PR/head/base、英文文案及固定版本贡献规范，展示完整
目标/文案、仓库人工事项和本地验证 base 与当前 PR base 的差异，以及最新 CI
和审核结果。CI 尚未运行、失败或等待维护者批准会如实显示，不伪装为通过，也不
引入 GitHub 本身没有要求的“所有检查先通过才能请求审核”的强制约定。

在详情中核对完整预览后，点击“提交正式审核”，无需勾选、复制或再次发送授权文本。
`POST .../workbench/pr-ready/confirm` 严格接收 preview hash、nonce、完整 text 和
`approved: true`，授权有效期 30 分钟且只可使用一次。隔离 Publisher 的
`pr_ready_write` 通过固定 GraphQL `markPullRequestReadyForReview` 修改 Draft 状态；
通用 GitHub API 入口不接受 GraphQL POST，也不接受自由查询/动作。文案、代码、
分支和签署不随此操作改动。可能通知代码所有者；转为正式审核不表示 CI、审核
或合并已经完成。Ready PR 仍可使用“更新 PR 文案”，需新的文案预览和确认。

输入“跟进当前 PR 的 CI 状态和审核意见，告诉我下一步由谁处理”，选择“跟进 PR”，
或点击“刷新 PR 跟进”会创建 `pr_followup` 持久只读任务，查询当前远端 head 对应的
Checks、PR 工作流、commit statuses、审核、行内意见和 PR 讨论。每类列表最多
四页/400 条，展示有界的最新记录；最多补充三个失败工作流的 jobs/失败步骤和
三个失败检查的 annotations，不下载执行仓库代码或跟随外部日志重定向。反馈按
不可信数据处理，凭证脱敏、正文截断、来源链接限定本仓库 GitHub 地址。

对话会持久记录草稿、等待 CI 批准、CI 运行/失败、审核要求修改、等待审核/合并、
已关闭/合并等状态，显示核对时间、来源和下一步责任人。相同 workflow/check 取
最新运行，同一审核者的后续正式审核覆盖旧结论，针对旧 head 的 approval 不当作
当前提交已获批准。读取不全/未知结论不显示全部通过；观察到检查通过也不等同于
已满足全部仓库合并规则。Fork 工作流批准和最终合并仍由上游相应权限的人完成。
失败和审核意见可在对话中继续分析；本次扩展不包含自动修改代码并推送原分支，
这些外部代码变更仍需要重新执行、验证、审查和独立精确发布授权。

API：`POST .../workbench/messages` 新增 `pr_ready`、`pr_followup`；默认 question
模式识别明确请求并只排入读取/预览任务。`POST .../pr-ready/prepare` 和
`POST .../pr-followup` 是相同入口。普通“确认”、模型输出、原发布确认不能改变
审核状态。所有入口保留同源/CSRF 和 CLI Token 保护。发布后的自动接续只进行上述
一次有界核对，其后按需读取，不持续轮询 GitHub；UI 自动轮询的是本地 Job 进度，
远端状态以核对时间为准。自动预览不会调用 Ready 写入接口，仍须新的精确人工确认。

状态观测和 Ready 授权/发送/回执存入 append-only WorkbenchEvent/Audit，观测完整
内容存入内容寻址 Artifact，并在同一事务中记录 PullRequestEvent 和 Job 结果。
原 DraftPullRequest 是不可改写的发布里程碑；当前 draft/ready/closed 由远端观测
投影展示，不把历史里程碑伪装为当前状态；观测合并时沿现有合法状态链记录 merged。
没有数据库结构变更。发送标记先持久化，未知响应只读核对，禁止重复 mutation、
更换账号、归档及开启新远端操作。GraphQL clientMutationId 仅作关联 ID，不作为
服务端幂等或条件 head 保证；最后一次 GET 到 mutation 的并发变更窗口仍存在，
操作期间避免同时手工修改 PR。确认后的写任务不可取消，读取可取消。回退前
排空读取任务并完成或核对已确认的写任务，保留所有授权、发送标记、观测和回执。

## Baseline import metadata

During automatic baseline replanning, a mypy `entry_points` diagnostic can refer
to a file whose full source was withheld. The persistent
`planning_import_metadata` job may inspect at most four such exact inventory
paths in the same frozen archive, using the existing credential-free,
network-disabled read-only OCI boundary. Its `python-entrypoints-imports-v1`
output contains only the requested path, source hash and a fixed import-origin
enum; ambiguous imports return `unknown`. It parses syntax without importing or
executing repository code. Source text, literals and arbitrary identifiers never
leave that container through this operation.

The original unavailable source entry remains unavailable and cannot be edited.
Archive/base/image/policy hashes and before/after job leases are checked; the
metadata artifact is bound into the child planning input and resulting plan's
audit evidence. No execution approval is created. Automatic recovery rejects
unresolved acceptance questions and `.pyi` files that shadow existing repository
`.py` source. All original command IDs, arguments, working directories and
contribution paths remain required; failed checks cannot become passing results
through diagnosis prose. Cancellation or stale evidence discards the continuation.
Rollback uses the same drained-job database/artifact restore procedure as
migration 0037; these jobs require no further schema change.

An explicit continue after failed, timed-out or rejected recovery planning retains
the same baseline packet, metadata and source/context checks. It does not use a
model call merely to classify the retry, change execution consent or extend model
budgets. Stops and changed inputs still invalidate the continuation. Status follows
the durable job continuation chain, including the metadata-enriched input hash.
The isolated Compose Worker receives the configured fixed Runner digest explicitly;
missing or mismatched configuration fails closed.

## 自动整理上下文

规划、基线诊断和正常任务对话会自动整理背景代码、长日志及历史讨论。当前要求、
规则、当前方案、批准方案和本轮精确读取的材料保留；旧方案、被拒绝的建议及较早
的用户原话保留来源索引。摘要中的助手建议不代表已验证事实或人类批准。

原文保存在不可变产物中，需要时由模型按任务内索引分段取回；仓库源文件也可通过
原有阅读工具补读。未处理的后续消息不会提前进入当前任务记忆。不可读文件仍不可读。
停止或依据变化会使旧索引失效，重试不沿用过期读取。

大型仓库目录只在本轮保留相关路径，并明确标注清单不完整。模型可用目录索引按路径
关键词查询完整目录，再读取所需文件；查询结果只包含路径，不会绕过文件读取限制。

状态栏显示文本体积的减少比例；这不等于模型供应商的计费 token 减少比例。
累计任务预算按已记录的实际用量计算，压缩不清零，也不自动追加预算。若达到任务
累计上限，系统会明确显示是哪一项达到上限，现有记录、方案和批准历史继续保留。

输入框下方的“输入预算”下拉框可选择本任务的累计输入上限：200 万、300 万、
500 万或 1000 万 token。“自定义”可同时设置累计输入、输出及调用次数。当前已用量
显示在旁边；选项低于已用量时不可选。选择后立即保存，仅影响当前任务，历史用量
不会清零，其他任务不变。提高上限允许后续模型计费调用；保存本身不会自动开始
规划或执行。需要继续时，在原对话发送“继续”，新方案仍须单独确认。

任务正在处理时不能修改预算，请等待结束或先停止。自定义输入范围为 1000–2000 万
token，输出范围为 1000–100 万 token，调用次数为 1–256，均不能低于已用量。
这是一项任务累计阈值，不是模型账户余额或单次上下文窗口；每次调用的其他限额
继续生效。预算修改以 `planning-budget-v1` 追加事件保存，支持重试并拒绝过期修改。

`task-context-v1` 使用现有 Artifact/WorkbenchEvent 的版本化记录，不改变数据库
表结构。回退前排空规划与对话作业；旧版本可忽略这些索引记录，或使用部署前完整
数据库/产物备份恢复。不要通过改写历史用量或批准来恢复任务。
