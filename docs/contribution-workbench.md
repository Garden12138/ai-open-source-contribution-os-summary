# AI 贡献方案工作台

入口“制定贡献方案”现在打开独立 Studio 任务页，默认对话，可按需展开可编辑方案。
模型连接／阶段覆盖、主题、卡片详情和升级配置见 [Studio 使用说明](studio-workbench.md)。
AI 从贡献任务的不可变分析和
Issue 快照出发，读取固定提交的仓库代码；信息不足时提出问题或补充阅读文件。
规划期间不执行仓库代码，也不创建 Fork。

## 使用流程

1. 与 AI 讨论改造目标，回答问题。右侧显示目标、验收标准、文件、实施步骤、
   测试、风险和待确认项。验证命令使用 argv 数组，不接受 shell 命令串。
2. 可以直接编辑方案；保存形成新的 `PlanVersion`，保留父版本及差异。代码依据或规划状态变化时，
   即使文字相同也生成新版本，不复用旧批准。
   其他页面生成新版本时，旧页面保留未保存内容，但不能确认执行旧视图。
3. 勾选批准并执行后，协调器重新读取上游 SHA。发生变化则停止，要求重新读取
   和规划。批准绑定精确方案、上下文、镜像、策略和实现／审查模型身份。
4. Sandbox Worker 执行 Explore；模型生成限于批准文件的 ChangeSet；隔离容器
   完成 Implement 和无网络 Verify；独立模型检查实际 diff 和测试产物。
   失败测试、未运行测试或阻断问题不能被模型的 `pass` 覆盖。审查阻断时最多
   自动修复两轮；基础设施失败停止并显示错误，不转为 Fake 执行。
5. 通过后停在人工验收。检查 diff、测试和审查，可停止并返回方案编辑。
6. 配置真实 Publisher 后，可以编辑 PR 标题／正文并准备预览。这一步只读远端，
   在受控临时 Git checkout 中重建并验证真实提交。预览包括账号、上游、Fork、
   分支、base/head、diff／测试／review 哈希、PR 文本和动作列表。
7. 最终确认有效期为 30 分钟且只能使用一次。确认后才允许创建或复用 Fork、
   Push 精确提交和创建 **Draft PR**。不会自动转正式 PR、评论、合并或强推。

执行授权不等于发布授权。仓库内容、模型答复和历史确认均不能授权新的外部动作。
停止操作会取消规划和执行子任务；发布确认后不能中途取消远端写入。
停止只作用于当时已有的任务。待取消完成并返回规划状态后，可重新发送消息；
旧停止请求的重放不会取消新一轮讨论。取消状态会明确显示在对话页，重新发送不会
自动重新授权执行或发布。

规划回复必须在“提问／补读文件／提交方案”中只选择一种。该互斥约束同时发送到
模型工具 JSON Schema 并在本地校验；不会把纯说明、互相冲突的分支或不完整方案
伪装成成功。失败时仅追加 `model_output_rejected` 诊断（白名单原因码、字段类别、
校验类型），不保存模型原文。无有效分支、分支冲突、问题编号重复和疑似敏感输出
有独立提示；其他字段／JSON 错误仍拒绝保存。失败不触发额外的模型修复调用。
`planning-v4-optional-narration` 将外层 `reply` 定义为可选展示文案：省略或空字符串
不会单独导致有效结果失败。界面可显示固定的“方案已生成”状态，数据库仍保留空
文案，不编造模型原话。问题、文件列表和完整方案的互斥、字段及安全校验不变；
实施步骤和测试项仍必须是字符串数组，仅 `commands_to_run` 接受结构化命令对象。
其他类型错误、缺少方案内容或敏感输出仍拒绝保存。

运行状态显示在对话流末尾左侧，与助手消息对齐；耗时用本地时钟每秒刷新，后台
状态仍每 3 秒查询，离开任务页时清理计时器。用户消息不再显示“您”的角色标签；
新模型回复使用直接的对话语气，历史消息正文保持原样。

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
不运行仓库 hooks、构建脚本或测试；临时 checkout 拒绝符号链接及不匹配的文件清单。
受控 Git 输出、超时、目录大小和容器资源均有限制。当前不支持通过不可重建的
补丁（例如缺少模式变更信息）发布，核对失败会停止。

Compose 提供可选独立 Publisher 容器：

```sh
docker compose --env-file .env -f docs/deployment/compose.yaml --profile publisher build publisher
docker compose --env-file .env -f docs/deployment/compose.yaml --profile publisher up -d publisher
```

启用前配置 `PUBLISHER_MODE=gh`（API）和绝对路径 `PUBLISHER_GH_CONFIG_DIR`。
该目录必须已经存在，仅包含专用发布账号的 gh 配置，并允许容器 UID `10001`
读取。只挂载这一个目录，禁止挂载完整 HOME。宿主机 keychain 中的 gh 登录
不会自动在 Linux 容器里生效；容器登录需使用独立凭证存储，不能复制发现用的
只读 token 或把凭证写进仓库。镜像构建和无网络空队列启动检查已通过；镜像只增加执行 Git 和 GitHub CLI 所需系统包，
不包含 Docker 或模型凭证。也可使用无 Docker 权限的独立 OS 账号运行 CLI；
启动检查会拒绝混入 GitHub 环境 token、模型／签名密钥或可写 Docker socket。

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
