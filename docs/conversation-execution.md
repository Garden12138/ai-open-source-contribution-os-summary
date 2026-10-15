# 对话内执行与环境准备

任务页保留一个输入框、当前状态和按时间排列的消息。方案、代码、测试、审查、发布预览和历史记录按需进入同一个详情视图。

新消息进入持久会话队列。进度问题根据实际记录只读回答，其他问题由冻结证据问答处理；补充要求按顺序接续。改变目标时先安全停止，再生成子方案并请你重新确认，无需选择消息用途。停止会保留未处理消息，发送“继续”恢复。

规划与执行共用同一个输入框。执行问题依据提问时的快照回答，不会停止工作；环境不支持时如实说明，重新规划不能解除环境限制。协议、队列边界和恢复方法见 [会话协议](conversation-agent-contract.md)。

Oppia 任务 `9727c1c6-04d4-4422-a8b0-e47b61517816` 的具体检查及恢复顺序见 [任务恢复记录](oppia-task-recovery.md)。

## 结果与失败

- `Job.succeeded` 表示任务完成，不代表测试通过。
- 执行接口新增 `verification`，区分 `not_run / passed / failed / environment_blocked`。
- 只有完整必需验证通过，且独立审查通过，才可以进入人工验收。
- 缺少 Python 模块或命令属于环境阻断，不消耗代码修复轮次。
- 代码失败或审查不通过仍限于最多两轮修复。修复模型同时获得上一轮 Diff、测试输出和审查证据，生成相对原始 base 的累计变更。
- 执行时不再静默创建并授权新的方案。状态依据改变时先保存新版本，再确认。

## 当前环境支持

环境检查只静态读取仓库清单，不在宿主机运行仓库代码。

| 类型 | 当前能力 |
| --- | --- |
| 内置工具 | 固定 Runner 中已有的 Python、Node 等；缺失依赖会明确阻断 |
| Python | 根目录 requirements.lock 或 requirements.txt；pip 强制版本哈希和 wheel，禁止源码包构建 |
| Node | 根目录 package.json + package-lock.json；npm ci 禁用安装脚本 |
| 混合技术栈、其他锁文件 | 执行前明确提示需要专用适配 |
| 浏览器、应用服务、源码构建 | 尚未纳入通用环境适配 |

独立 `execution_dependencies` Job 通过白名单包代理准备依赖，工作容器不持有凭证。依赖产物、安装清单、镜像、策略和 Implement 结果哈希绑定后才调度 Verify；Verify 使用新容器、只读依赖、无模型、无凭证、无网络。Node 工具通过 PATH/NODE_PATH 提供；依赖本地 node_modules 布局、ESM 解析或安装脚本的项目仍需专用适配，不能仅因有锁文件就认定全部测试可运行。

选定 Python 包的 workspace-v2 支持冻结依赖后的修改前基线及修改后离线验证，详见 `sandbox-dependencies.md`；这不代表通用环境或跨平台能力矩阵已完成。Oppia 的混合依赖、浏览器和服务环境未适配；历史六条命令因缺包失败的证据保留，不标记为通过。

## 接口与持久化

- `GET /api/v1/tasks/{id}/workbench` 新增 `execution` 聚合视图：运行 ID、版本、轮次、验证、环境、可用操作和问答状态。
- `GET .../workbench/timeline?after=N` 返回新增可展示事件、游标和当前视图。客户端活动时每秒检查，空闲时每五秒；版本不变时不重复读取详情。
- `POST .../workbench/messages` 新增 `mode=question`；默认 planning 保持旧调用兼容，但不再隐式停止执行。
- `POST .../workbench/acceptance` 接收 `binding_hash` 和验收消息 `text`；要求同源/CSRF 或本地令牌和幂等键。核对最新方案、执行、通过的测试与审查后追加 `execution_accepted`，不依赖模型或 Publisher，不创建发布 Job 或外部写入授权。
- `POST .../workbench/replan` 接收新目标和 `expected_revision`，要求 Origin/CSRF 或本地访问令牌及幂等键。
- `GET /api/v1/executions/{id}` 新增证据导出的 `verification`。

一次运行直接使用既有不可变 `execution_authorized` 事件 ID；初始 Attempt 通过 `automation_started` 关联，修复轮次通过同一 PlanApproval 归组。缺乏可证明授权关联的历史显示 `legacy_unverified`，不回填授权。

初始对话执行切片没有新增表、列；后续环境/基线/工作区能力的数据库版本已升级至 0035（以 `docs/tasks.md` 为准）。新增 Job kinds 与追加事件复用现有开放式日志契约；旧事件、方案、执行和产物哈希保持不变。依赖工作区分配及结果写入内部事件，不能通过时间线暴露工作区路径。重启后从事件重建 Verify 输入与清理状态；不宣称覆盖所有 Docker 创建与事件落盘之间的崩溃窗口。

2026-10-11 修复：失败摘要同时返回脱敏、限长的 `stdout`、`stderr` 和合并 `output`，覆盖 pytest/Ruff 主要写 stdout 的诊断。已有文件的模型提案支持 `kind=edit`：指定匹配冻结 `prior_hash` 的路径与 `edits=[{old_text,new_text}]`，每段必须唯一匹配，系统保留其余字节并转换为既有 write ChangeSet；未读取文件、歧义、过期哈希和敏感内容仍拒绝。新文件继续使用完整 write。模型原有 write/delete 提案保持兼容，不改变持久 ChangeSet、签名或沙箱权限。

“重新读取上游代码”现在保留当前方案的源码与依赖清单路径，并在新快照重新读取，禁止用旧快照内容冒充新文件。澄清选项对象误入字符串字段时，最多两轮定向纠正模型输出，不跳过 schema 校验或自动批准。范围内两轮代码修复额度耗尽后，仍需保存并确认新的方案版本；工作台修复不构成延长执行授权。

2026-10-11 修复轮上下文回归修复：通过执行开始状态的 `repair_started`、同一 PlanApproval 的前一轮以及完整审查/阶段哈希识别修复轮，不能用一直保存为 `start_execution` 的 action 字段判断。模型读取前一轮实际 Implement 的累计 ChangeSet 叠加原始快照形成的临时编辑视图；新增文件在该视图中有真实内容和哈希，可以精确编辑。系统自动保留未提及的前轮操作，检查当前视图的哈希，再把累计操作绑定回原始 base，原始 CodingContext 不覆盖。删除前轮新文件会撤销对应新增操作，不生成对原始不存在文件的删除。模型回复和审查建议均不能放宽批准方案。

`POST /api/v1/jobs/{id}/retry` 可恢复当前授权内失败的 coding_turn/change_set_proposal/provider_review 子任务；校验最新执行、方案、模型、会话或冻结审查输入和停止状态，保留每个 Job 最多三次、每份批准最多两轮修复的额度。`automation_resumed` 绑定原阻塞、原启动、授权哈希和当前 Job 次数，与排队在同一事务落盘；旧失败事件不修改。重试再次失败会追加新的阻塞事件。若阻塞清理已回收依赖，继续实施前创建独立环境准备 Job、重新冻结依赖并运行全部修改前基线，新基线记录被替代的旧基线哈希，后续验证使用新环境。旧基线和依赖证据保留，不能冒充仍可用的工作区；已回收依赖禁止直接用于 Verify。

回退前停止接收执行，停止并排空 execution_question/execution_dependencies 和 sandbox Jobs，完成依赖清理，再部署上一版。不要让旧 Worker 继续消费新运行；必要时停止服务，恢复部署前一致 SQLite 备份，保留对应内容寻址产物目录，验证 integrity_check / foreign_key_check 后启动。禁止修改历史事件或方案哈希以兼容回退。

## 对话验收

验证与审查通过后，收到结果回复。查看结果再点击“确认结果”；普通结果讨论不能隐式批准。
验收回执绑定方案、最新执行、状态、diff、Verify 和 Review 哈希；重试不重复
写入，重新规划后旧回执仅保留为历史。验收不需要模型或发布服务。
确认后自动准备英文文案与发布预览，修订要求直接在对话中说明。Publisher 未启用
时显示设置提示；启用后准备精确发布预览，查看全文并点击“创建草稿 PR”才可能 Fork、Push
和创建 Draft PR。数据库不变，不新增迁移；旧 UI 回退后验收记录仍留在日志中，
不转换为执行或发布授权。

## 验证

离线：`python -m pytest`；新版浏览器回归：启动 `python -m tests.studio_browser_server` 后执行 `tests/conversation_agent_browser.mjs`（设置本机 Playwright 路径）。浏览器使用一次性数据库并拦截实际执行请求，不代表真实模型验收。旧浏览器脚本保留为旧界面的历史检查，不再是新版验收入口。

真实容器：设置 `CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1` 和固定 `CONTRIBOS_SANDBOX_RUNNER_IMAGE` 后运行 sandbox 依赖与隔离验证测试。真实模型在专用非生产 Python/TypeScript 仓库的完整验收、Oppia 适配、原生 Linux/Docker 验收仍单独记录，不以离线或本机夹具代替。

独立审查读取批准方案的全部约束（包括 implementation_steps），格式错误以固定安全原因返回，不回显被拒绝的模型文本。重试补充精确输出 schema；未知 severity、无证据发现和无效 reason_code 仍拒绝，不能把格式修复当成审查通过。依赖清理只响应尚未解除的阻塞，历史失败事件不能回收已恢复运行的环境。
