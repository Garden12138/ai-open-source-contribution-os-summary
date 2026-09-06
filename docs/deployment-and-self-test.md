# 部署与产品功能验收指南

这是一份面向产品使用者的操作手册：先用 Docker Compose 启动产品，再通过浏览器
完成一轮从“发现机会”到“贡献进度”的功能验收。本文不要求阅读源码，也不把
`pytest` 等开发者测试当作产品验收步骤。

当前版本是单用户、本地优先版本，只允许从本机访问。Compose 仅把 Web 端口发布到
宿主机 `127.0.0.1:8000`，不要改成 `0.0.0.0` 或部署到公网。

## 1. 先了解当前能力边界

| 功能 | 使用的数据或运行方式 | 当前是否真实 |
| --- | --- | --- |
| GitHub Issue 扫描、过滤、评分、榜单 | GitHub 只读 API | 是 |
| 偏好、候选、比较、提醒、贡献看板 | 本地 SQLite | 是 |
| AI 深入评估与批量筛选 | `fake` 或 NVIDIA/Nemotron 3.5 Lightning | NVIDIA 配置后真实调用模型 |
| Vibe Coding / ChangeSet | NVIDIA/DeepSeek + 精确确认 | 完整五进程模式下真实调用模型 |
| Explore / Implement / Verify | `fake` 或专用 Docker Sandbox Worker | Docker 模式真实执行不可信代码 |
| Review | `fake` 或 NVIDIA/MiniMax | NVIDIA 配置后真实审查精确 Artifact |
| 发布意图、Draft PR | 本地 Fake Publisher | 否，不会在 GitHub 创建 PR |

因此，本文把验收分成两组：

- “真实产品功能”可以验证 GitHub 只读发现和本地工作台。
- “离线完整流程演示”可以验证页面、状态流和操作衔接，但不能作为真实 AI、真实
  容器执行或真实 Draft PR 的验收证据。
- NVIDIA 与真实 Sandbox 的配置见
  [`nvidia-provider.md`](nvidia-provider.md)；Linux P5-G06 在真实机器验收前仍未完成。

## 2. 使用 Docker Compose 部署

### 2.1 前置条件

只需要：

- Docker Desktop、OrbStack 或 Docker Engine；
- Docker Compose v2；
- 能访问 GitHub 的网络；
- 一个现代浏览器。

在仓库根目录确认环境：

```bash
docker version
docker compose version
```

### 2.2 准备配置

在仓库根目录执行：

```bash
cp .env.example .env
```

打开 `.env` 后，按需要配置以下项目：

| 配置 | 如何填写 | 用途 |
| --- | --- | --- |
| `GITHUB_TOKEN` | GitHub fine-grained 只读 Token | 提高真实扫描额度；推荐配置 |
| `LOCAL_ACCESS_TOKEN` | 本地随机字符串 | 保护页面中的保存、扫描等操作；推荐配置 |
| `APP_TIMEZONE` | 例如 `Asia/Shanghai` | 每日扫描和榜单日期 |
| `ANALYSIS_PROVIDER` | `none`、`fake` 或 `nvidia_nim` | `nvidia_nim` 通过内部 Gateway 调用 MiniMax M3 |
| `SANDBOX_STAGE_RUNTIME` | `none`、`fake` 或 `docker` | `fake` 用于离线体验；`docker` 只能由专用 Sandbox Worker 使用 |
| `SANDBOX_JOB_SPEC_SIGNING_KEY` | 64 位以上十六进制字符串 | 允许 Fake 执行任务进入队列 |

生成本地访问令牌和 Fake 执行签名密钥时，可分别运行一次：

```bash
openssl rand -hex 32
```

把两次输出分别复制到 `LOCAL_ACCESS_TOKEN` 和
`SANDBOX_JOB_SPEC_SIGNING_KEY`。不要把 `.env` 提交到 Git，也不要给 GitHub Token
授予写权限。

有两种推荐配置模式：

**只使用当前真实功能**

```dotenv
ANALYSIS_PROVIDER=none
SANDBOX_STAGE_RUNTIME=none
```

**体验完整离线产品流程**

```dotenv
ANALYSIS_PROVIDER=fake
SANDBOX_STAGE_RUNTIME=fake
SANDBOX_JOB_SPEC_SIGNING_KEY=<替换为 openssl rand -hex 32 的输出>
```

`fake` 模式不会调用真实模型、不会执行不可信仓库代码，也不会写入 GitHub。

若要启用 NVIDIA Build，把分析、实现和 Review Provider 设为 `nvidia_nim`，并只
向 Model Gateway 提供 `NVIDIA_API_KEY`。完整配置和权限拆分不要直接照抄到同一个
进程，按 [`NVIDIA Provider 接入指南`](nvidia-provider.md) 操作。

### 2.3 一条命令启动

在仓库根目录执行：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml up -d --build
```

第一次启动会构建本地镜像并安装依赖。随后检查状态：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml ps
```

预期结果：

- `api` 为 `Up`，健康状态最终变成 `healthy`；
- `worker` 为 `Up`；
- 浏览器可以打开 <http://127.0.0.1:8000>；
- 健康检查 <http://127.0.0.1:8000/health> 返回
  `{"status":"ok","database":"ok"}`。

Compose 会启动两个容器：

- `api`：Web 页面和 API；
- `worker`：处理扫描、分析、归档和 Fake 执行任务。

两者共用名为 `contribos-local-data` 的持久卷。重新创建容器不会清空数据库和
Artifact。

启用 NVIDIA Profile 时使用：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia up -d --build
```

这会额外启动不能直接出网的 `provider-worker` 和唯一持有 NVIDIA key 的
`model-gateway`。基础 Compose 不挂载 Docker socket；真实 Vibe Coding 的专用
Sandbox Worker 使用本机分离启动方式。

必须显式传入仓库根目录的 `--env-file .env`。部分 Compose 版本会以 Compose
文件所在的 `docs/deployment` 作为默认环境文件目录；省略该参数会让 NVIDIA 和
Gateway 密钥展开为空。API 同时连接私有应用网络和一个仅 API 使用的 host bridge，
以兼容 OrbStack 对 internal-only 网络不发布宿主机端口的行为；宿主机仍只绑定
`127.0.0.1:8000`。

### 2.4 常用运维命令

下面展示的是基础 Profile。若 `.env` 启用了 NVIDIA，所有 `ps`、`logs`、`up`、
`stop`、`start`、`down`、`cp` 和 `restart` 命令都应在
`-f docs/deployment/compose.yaml` 后增加 `--profile nvidia`，否则可能遗漏 Gateway
和 Provider Worker。不要运行不带 `-q` 的 `config` 后公开其输出，以免泄露展开后的
密钥。

查看运行状态：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml ps
```

查看最近日志：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml logs --tail=200 api worker
```

持续查看日志：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml logs -f api worker
```

修改 `.env` 后重建容器以加载新配置：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml up -d --force-recreate
```

NVIDIA Profile 更新代码或配置时使用：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia config -q
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia up -d --build --force-recreate
```

停止但保留数据：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml stop
```

再次启动：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml start
```

停止并删除容器，但保留数据卷：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml down
```

不要执行 `down -v`，除非明确要永久删除本地产品数据。

### 2.5 备份本地数据

先停止所有会写业务数据的容器，确保 SQLite、WAL 和 Artifact 处于同一个一致恢复点。
基础 Profile 使用：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml stop
docker compose --env-file .env -f docs/deployment/compose.yaml cp api:/data ./contribos-data-backup
docker compose --env-file .env -f docs/deployment/compose.yaml start
```

NVIDIA Profile 必须把 Provider Worker 一起停止：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia stop
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia cp api:/data ./contribos-data-backup
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia start
```

请把生成的 `contribos-data-backup` 放到安全位置。不要只复制一个正在写入的
`contribos.db` 文件。

## 3. 产品验收前准备

打开 <http://127.0.0.1:8000>。如果配置了 `LOCAL_ACCESS_TOKEN`，第一次点击扫描、
保存偏好或其他写操作时，浏览器会要求输入令牌。这里应输入本地访问令牌，不是
GitHub Token；刷新页面后可能需要重新输入。

建议按以下顺序执行案例。每个案例都记录：

- 通过、失败或因前置条件未执行；
- 操作时间；
- 页面截图或脱敏后的错误信息；
- 涉及任务时记录页面显示的 Job、Analysis、Plan 或 Execution 标识。

真实 GitHub 数据随时会变化。“扫描成功但候选为 0”不代表部署失败，但后续需要
候选数据的案例暂时无法继续。可以稍后重试，或在 `.env` 的 `GITHUB_QUERIES` 中
加入一个你熟悉的公开仓库查询。

## 4. 真实产品功能验收

### U-01：首次打开与个人偏好

前置条件：首次部署，或还没有保存过偏好。

操作：

1. 打开产品首页。
2. 确认顶部存在“发现机会 / 我的候选 / 贡献进度”三个入口。
3. 在首次设置中选择目标，例如“技术成长”。
4. 填写偏好语言、每周投入时间和最低赏金。
5. 点击“保存并查看推荐”。

通过标准：

- 页面没有 401、403 或 500 错误；
- 首次设置收起，顶部出现“调整偏好”；
- 页面显示当前目标，推荐列表会按偏好重新排序；
- 刷新页面后偏好仍然保留。

### U-02：扫描 GitHub 机会

前置条件：Worker 正在运行；推荐配置只读 `GITHUB_TOKEN`。

操作：

1. 在“发现机会”点击“立即扫描新机会”。
2. 观察按钮和提示文字中的排队、运行和完成状态。
3. 扫描完成后查看“扫描候选 / 通过硬筛选 / 今日精选”三个数字。

通过标准：

- 扫描进入持久任务队列，而不是页面长时间无响应；
- 任务成功后页面自动更新本期榜单和最近更新时间；
- 每个入选项都显示仓库、Issue、推荐类型和分数；
- GitHub 上不会新增认领、评论、Fork、Push 或 PR。

如果任务在扫描完成前被取消，页面应显示明确的取消状态，并保留上一次成功榜单。
失败或超时的任务应提供可重试入口。

### U-03：查看筛选、评分和风险解释

前置条件：榜单至少有一个机会。

操作：

1. 依次点击“赏金优先 / 战略匹配 / 技术栈匹配 / 高影响力”等筛选项。
2. 在一个机会卡片中展开“查看评分构成”。
3. 查看预计投入、接收机会、竞争压力、项目影响和风险提示。
4. 点击“查看 Issue”打开 GitHub 原页面。

通过标准：

- 类型筛选只改变当前展示，不触发新的扫描；
- 评分构成包含七个可解释维度；
- 风险和赏金提示不会被隐藏成一个无法解释的总分；
- GitHub 链接对应卡片上的仓库和 Issue。

### U-04：加入候选并并排比较

前置条件：榜单至少有两个机会。

操作：

1. 在两个或三个机会卡片上点击“加入候选”。
2. 打开顶部“我的候选”。
3. 勾选 2～3 个候选，点击“比较所选机会”。
4. 尝试勾选第 4 个候选。

通过标准：

- 顶部候选数量及时更新；
- 比较区并排展示推荐、投入、接收机会、竞争和风险；
- 一次最多允许比较三个机会；
- 刷新页面后候选仍然存在；
- 点击“移出候选”后，该项从候选页消失。

### U-05：忽略与恢复机会

前置条件：榜单至少有一个未加入候选的机会。

操作：

1. 点击机会卡片中的“暂不适合”。
2. 选择一个原因并点击“确认忽略”。
3. 点击榜单右上角“查看已忽略”。
4. 在该机会上点击“恢复推荐”。

通过标准：

- 忽略后该项从默认推荐列表中隐藏；
- “查看已忽略”能找回该记录和忽略状态；
- 恢复后该项重新进入普通推荐列表；
- 忽略不会删除历史扫描或评分。

### U-06：候选提醒与通知已读

前置条件：至少有一个候选，Worker 正在运行。

操作：

1. 进入“我的候选”。
2. 把提醒时间设为当前时间或一分钟以前，点击“保存提醒”。
3. 等待约 5 秒并刷新页面。
4. 点击顶部“提醒”，打开通知抽屉。
5. 将单条通知设为已读，再测试“全部已读”。

通过标准：

- 提醒时间保存后刷新仍存在；
- Worker 到期同步后出现“候选机会提醒”；
- 未读数量与抽屉中的未读项一致；
- 单条已读和全部已读都会更新未读数量。

### U-07：每日自动扫描

前置条件：当天尚未有成功扫描；Worker 持续运行。

操作：

1. 点击“调整偏好”。
2. 勾选“每日自动扫描”。
3. 把扫描时间设置为当前本地时间之前的一分钟并保存。
4. 等待 Worker 创建并处理扫描任务，再刷新页面。

通过标准：

- 当天自动创建至多一个扫描任务；
- 扫描完成后榜单更新；
- 重启 Worker 不会为同一天重复创建相同自动扫描。

如果当天已经存在成功扫描，这个案例应改到下一天执行，不能通过删除数据库制造
“通过”结果。

## 5. 离线完整流程演示

以下案例要求 `.env` 已设置：

```dotenv
ANALYSIS_PROVIDER=fake
SANDBOX_STAGE_RUNTIME=fake
SANDBOX_JOB_SPEC_SIGNING_KEY=<至少 64 位十六进制字符串>
```

修改后必须重新创建容器：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml up -d --force-recreate
```

这些案例验证“用户能否顺畅使用流程”，不证明真实 AI、真实隔离执行或真实 GitHub
发布已经接通。

### U-08：运行深入评估

前置条件：榜单至少有一个机会，Fake Provider 已启用。

操作：

1. 在“发现机会”点击“AI 筛选前 5 个（演示）”，观察批量 Job 进度。
2. 点击“只看 AI 推荐”，确认只保留 `pursue / consider` 结论。
3. 也可以在单张机会卡片点击“查看分析报告”，运行或查看精简 Markdown 结论。
4. 确认报告先展示基于仓库快照的“项目介绍”和基于 Issue 快照的“需求内容”，再给出与两者具体关联的综合分析、风险与验收、行动建议。
5. 下载同一份 `.md` 文档；旧版分析应显示关联性不足提示，并提供“重新生成关联分析”。
6. 再点击“生成新版本”，确认版本对比包含项目介绍、需求内容和其他重要内容的中文语义差异。

通过标准：

- 分析在 Worker 中完成，页面不会因刷新丢失任务；
- 批量操作最多创建 5 个当前规则 Top-30 候选 Job，总调用、Token、成本和时长预算有界；
- 每次运行生成新的不可变版本；
- 页面与下载使用同一份 Markdown，且不出现分析 JSON、证据 ID、Token、Prompt、哈希或报告级 Provider 信息；
- 新版分析的项目介绍引用冻结仓库证据，需求内容引用冻结 Issue 证据，结论和至少一项匹配理由同时关联两者；
- 工作量、竞争、风险和建议包含具体项目或需求事实，不使用与当前机会无关的通用模板；
- 可以查看版本详情和精简语义差异；
- AI 结论只调整独立决策排序，不覆盖基础规则分；
- 历史 Snapshot 结论只读，不能从旧分析创建本轮贡献任务；
- 页面明确显示这是 Fake Provider，而不是伪装成真实 AI 结论。

### U-09：创建、修订和批准贡献计划

前置条件：U-08 已生成一个分析版本。

操作：

1. 在“我的候选”点击“开始 Vibe Coding”，或在分析结果下点击“创建贡献任务”。
2. 检查目标、验收标准、实施步骤和测试，点击“创建初始计划”。
3. 在“计划对话与决策”追加一条修改建议。
4. 修改计划并保存为新修订。
5. 在“计划版本差异”中比较两个版本。
6. 从目标仓库默认分支复制完整的 40 位 commit SHA。
7. 输入该 SHA，点击“批准此计划”。
8. 使用相同 SHA 点击“验证执行就绪”。

通过标准：

- 计划修订会新增版本，不会覆盖旧版本；
- 对话、版本链和差异都可以查看；
- 批准后显示 Approval、Base 和哈希摘要；
- 短 SHA、错误 SHA 或批准后变化的输入会被拒绝；
- 批准计划后不能直接原地编辑。

### U-10：演示 Explore / Implement / Verify

前置条件：U-09 的计划已批准；目标仓库和 commit SHA 可以从 GitHub 下载。

操作：

1. 点击“采集仓库归档”，等待页面自动填入归档 SHA-256。
2. 在 Runner digest 输入以下仅用于 Fake 演示的值：

   ```text
   sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
   ```

3. 点击“启动隔离执行”。
4. 等待 Explore 成功后，点击“提交变更方案”。
5. 等待 Implement 和 Verify 完成；必要时点击“刷新执行”。
6. 展开代码变更、测试结果、文件变化、运行记录和执行溯源。

通过标准：

- 页面按 Explore → Implement → Verify 顺序展示阶段；
- Explore 成功前不能提交 ChangeSet；
- 每个阶段都有明确状态，任务失败时有明确原因；
- 可以查看 Fake diff、规范化测试结果、文件清单和内容哈希；
- 这里填写的 Runner digest 只是演示输入，不能作为真实镜像或沙箱验收证据。

### U-11：演示 Review、发布确认和贡献进度

前置条件：U-10 的 Verify 已成功。

操作：

1. 点击“启动独立 Review”。
2. 确认 Review 显示“通过”及其绑定哈希。
3. 填写 Draft PR 标题和说明，点击“创建 Fake 发布意图”。
4. 检查仓库、分支和允许动作，再点击“确认发布 Draft PR（本地演示）”。
5. 打开顶部“贡献进度”，点击某个任务的“继续贡献”重新进入工作台。

通过标准：

- Review 绑定精确的执行、diff 和测试结果；
- 创建发布意图后仍需要第二次明确确认；
- 确认后只生成本地 Fake Draft PR 记录；
- GitHub 上不应出现真实分支或 PR；
- “贡献进度”显示任务漏斗、历史和下一步状态，并可继续已有任务。

### U-12：重启后的数据恢复

前置条件：已经保存偏好、候选或贡献任务。

操作：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml restart
```

等待 `api` 恢复健康后刷新页面。

通过标准：

- 偏好、候选、提醒、分析、计划和执行历史仍然存在；
- 未完成的持久任务会恢复或进入明确终态；
- 重启不会生成一套新的空数据库。

## 6. 验收记录模板

| 案例 | 结果 | 证据或说明 |
| --- | --- | --- |
| U-01 首次设置 | 通过 / 失败 | 目标、语言、刷新后状态 |
| U-02 GitHub 扫描 | 通过 / 失败 | 时间、候选数、Job ID |
| U-03 评分解释 | 通过 / 失败 | 评分维度和风险截图 |
| U-04 候选比较 | 通过 / 未执行 / 失败 | 候选数、比较结果 |
| U-05 忽略恢复 | 通过 / 失败 | 忽略原因和恢复结果 |
| U-06 提醒通知 | 通过 / 失败 | 提醒时间、未读变化 |
| U-07 自动扫描 | 通过 / 未执行 / 失败 | 日期和 Job ID |
| U-08 Fake 分析 | 通过 / 未执行 / 失败 | AnalysisVersion ID |
| U-09 计划批准 | 通过 / 未执行 / 失败 | PlanVersion、Approval ID |
| U-10 Fake 执行 | 通过 / 未执行 / 失败 | Execution ID、三阶段状态 |
| U-11 Fake 发布 | 通过 / 未执行 / 失败 | Review、PublishIntent、本地 Draft 编号 |
| U-12 重启恢复 | 通过 / 失败 | 重启前后同一记录 ID |

记录中不要包含 GitHub Token、`LOCAL_ACCESS_TOKEN`、签名密钥或完整 `.env`。

### 2026-08-31 本地产业功能验收记录

环境：macOS arm64 + OrbStack；GitHub 只读扫描使用真实公开数据；U-08～U-11
按本文定义使用 Fake Provider、Fake Stage Runtime 和本地 Fake Publisher。未向
GitHub 执行 Fork、Push、评论或 PR 创建。

| 案例 | 结果 | 证据或说明 |
| --- | --- | --- |
| U-01 首次设置 | 通过 | 目标在 `learning` 与 `bounty` 间切换时排序变化；恢复 `learning` 后刷新保持，最终偏好版本 7、自动扫描关闭 |
| U-02 GitHub 扫描 | 通过 | Job `99b45246-30eb-4524-b865-96cb8045315b`；Scan `b29d23c0-d733-4924-9803-e62ee8437178`；94 候选、67 通过、10 精选 |
| U-03 评分解释 | 通过 | 机会 93 展示七项评分、赏金条款风险、接收/竞争/影响等级及正确 Issue 链接；未触发扫描 |
| U-04 候选比较 | 通过 | 2～3 项比较成功，第 4 项返回 422；移除、刷新和持久化正常 |
| U-05 忽略恢复 | 通过 | 机会 66 以 `too_large` 忽略后默认隐藏，包含已忽略时可见，恢复后重新出现；扫描记录未变 |
| U-06 提醒通知 | 通过 | 机会 55 到期生成通知 `ad9a392e-8965-4a11-bb79-f162375aa90c`；单条/全部已读正确，提醒以 UTC `Z` 返回 |
| U-07 自动扫描 | 未执行 | 当天已有成功扫描，未删除数据库伪造前置条件；同日启用及重启均未创建重复 `scheduled-scan:2026-08-31`，次日首次触发仍待验收 |
| U-08 Fake 分析 | 通过 | 同一 Snapshot 生成不可变版本 `2f38abc7-9997-4388-805e-18ed496a45de`、`e167bfd4-3a41-4c14-ad33-41ce3bfe9d90`；差异比较成功，页面/API 标识 `fake` |
| U-09 计划批准 | 通过 | Plan v1/v2/v3 链保留；有效 Approval `a593d4fb-3422-43d5-bebd-498c0fea079c`；短/错误 SHA 和批准后编辑均被拒绝 |
| U-10 Fake 执行 | 通过 | Execution `e6e67143-97c8-4074-bcf2-debb6905455a` 按 Explore → Implement → Verify 成功；3 个清单、9 条状态、全部 Artifact 内容哈希复验一致 |
| U-11 Fake 发布 | 通过 | 修复后 Review `ec34b066-6543-4955-91d6-553a2506155e` 直接匹配 diff/test Artifact 和 binding hash；Intent `c0a1fdd2-a009-49a1-acc9-76199fd7c62d`；本地 Draft `bf7c2d82-66cc-4567-9905-f87ad1e713dc` 使用 `local.contribos.invalid`，未写 GitHub |
| U-12 重启恢复 | 通过 | API/Worker 重启前后业务状态摘要均为 `390f77cd84ff716fbb8a84f6031314aed5d2bde9a37656257abc99cde1c7f4cb`；SQLite `integrity_check=ok`，非终态 Job 为 0 |

验收中修复了 GitHub 二级限流识别、分析语料中单个不安全候选阻断全部候选、
产品时间戳 UTC 展示、Review 直接 Artifact 绑定、Fake Draft 误导性 GitHub URL，
以及 macOS 启动器自动注入 Worker 环境变量的问题。完整离线回归为
`285 passed, 10 skipped`；跳过项和未执行的 U-07 次日案例均不计为通过。

## 7. 常见问题

| 现象 | 用户侧检查方式 |
| --- | --- |
| 首页打不开 | 运行 `docker compose --env-file .env -f docs/deployment/compose.yaml ps`，确认 `api` 为 `healthy` |
| API 一直不健康 | 查看 `docker compose --env-file .env -f docs/deployment/compose.yaml logs --tail=200 api` |
| 扫描一直排队 | 确认 `worker` 为 `Up`，再查看 Worker 日志 |
| 页面提示 401 | 输入 `.env` 中的 `LOCAL_ACCESS_TOKEN`，不要输入 GitHub Token |
| 扫描提示 403/429 | 检查只读 Token 和 GitHub 配额；Worker 会按 `Retry-After`/额度重置时间有界重试，无提示头的二级限流会先等待至少 60 秒，重试耗尽后再从页面重试 |
| 没有“运行深度分析” | 将 `ANALYSIS_PROVIDER` 设为 `fake`，或按 NVIDIA 指南启用 `nvidia_nim` Profile |
| NVIDIA Job 一直排队 | 确认 `provider-worker` 与 `model-gateway` 健康，且两者使用同一 `MODEL_GATEWAY_SIGNING_KEY` |
| Gateway 反复重启并提示缺少 `MODEL_GATEWAY_SIGNING_KEY` | 确认命令显式包含 `--env-file .env`，密钥为至少 64 位偶数长度十六进制，然后用 NVIDIA Profile `up -d --force-recreate` |
| API 显示 `healthy` 但 `curl 127.0.0.1:8000` 失败，端口只有 `8000/tcp` | 当前容器仍使用旧的 internal-only 网络；执行 NVIDIA Profile `down`（不要加 `-v`），再按最新 Compose 文件 `up -d --build --force-recreate`，端口必须显示 `127.0.0.1:8000->8000/tcp` |
| Vibe Coding 上下文一直排队 | 真实模式需要单独运行 `contribos sandbox-worker`；不要把 Docker socket 或模型密钥给 API |
| 执行停在 pending | 确认 Fake Runtime 和签名密钥均已配置，且 API/Worker 使用同一 `.env` |
| 归档采集失败 | 确认输入的是目标仓库真实、完整的 commit SHA，且能访问 GitHub |
| 提醒没有出现 | 确认 Worker 运行，等待数秒后刷新页面 |
| 修改 `.env` 没生效 | `restart` 不会更新容器环境；使用带正确 Profile 的 `up -d --force-recreate`，普通浏览器刷新不会重载容器环境变量 |

## 8. 不使用 Docker 的备用启动方式

只有在无法使用 Docker Compose 时，才需要本机 Python 3.11+。在仓库根目录执行：

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .
set -a
source .env
set +a
contribos serve
```

另开一个终端并启动 Worker：

```bash
source .venv/bin/activate
set -a
source .env
set +a
contribos worker
```

浏览器仍然访问 <http://127.0.0.1:8000>，后续产品验收步骤与 Docker 部署相同。
