# UPEVE v1 — 分阶段工程实施与迁移计划

**前提**：在原有 `refactor/agent-runtime-v2` 开发分支实施。不是新 Runtime 重构，不新建工作流权威存储，不修改 `main`。正式服务、原数据库、凭据和真实上游仓库不得被试验破坏。

## 总原则

**交付顺序必须由真实用户失败驱动，但实现只能抽象通用规则。** 每阶段完成最小的可运行垂直切片、记录失败与实际证据，再推进；不要先写完整架构文档就宣布通过，也不要不断加非必要抽象。

### E0 — 分支和证据基线

**读**：`AGENTS.md`、`docs/tasks.md`、原 v2 spec/ADR/architecture/migration、2026-10-28 复盘、所有原生试用失败工件；追查当前 `contribution_workflow → preparation → baseline → v2 admission`。

**动作**：`git status`、`git branch --show-current`、`git log`；切换/坚持 `refactor/agent-runtime-v2`，保护现有未提交改动；如需新 worktree，确保本包文档复制到实际执行工作区。记录当前 `main` 对比差异和本地/远端追踪分支关系。

**产物**：新增 Phase 5 专项任务 ID `UPEVE-E01..E07`（不改既有 Gate 语义）；`docs/acceptance/universal-environment-v1/README.md`，保存冻结故障回归入口和来源。现有记录哈希逐项核验。

**门禁**：能逐文件指出导致 `ruff`/`stubtest` 未准备、radashi nested install 和缓存失败的实际调用链；不靠字符串错误猜根因。

### E1 — 类型契约、静态发现与正确就绪语义

- 建立纯数据、严格、可版本化的 command/evidence/toolchain/receipt/verdict 类型。
- 静态 Source Inspector 与 Capability Registry 用现有 archive 工具和哈希；`Makefile`、CI、构建脚本只读**声明性安全子集**；拒绝未受支持动态解释。
- `preflight` 返回 `static_admitted` 与 `unverified`，不把 `supported` 冒充 `prepared` 或 `baseline_verified`；兼容现有外部 API 使用者，必要时扩展字段而非破坏旧历史。
- 准备参数和检查原文必须与 PlanVersion/Attempt 同哈希绑定。

**落点参考**：新建独立、最少量的 `app/environment_engine/` 子包，复用 `execution_preflight.py`、`verification_commands.py`、`agent_v2/capabilities.py`；不引入新框架。

**门禁**：陌生 fixtures 形成同结构但不同项目名称的相同 verdict；缺关键证据不显示 ready；schema/hash/路径安全/过期 Plan 失败闭合。

### E2 — Python 通用 requirements/工具解析

- 受限递归 `-r`/`-c`、PEP 508、pyproject/部分 extras/dev groups、相对目录、大小/深度/循环/禁止参数/哈希 provenance。
- 从批准的 `ruff format --check`、`ruff check`、`stubtest` 等命令来源反推实际 toolchain；发行包映射由项目**声明 + 发行包入口元数据**核实，不能把所有命令直接猜成同名包。
- `Makefile`/CI 标明开发 requirements 与命令关联；支持版本约束冲突、重复 include、约束文件与 requirements 混用。
- 支持现有封闭 pip 下载/离线 venv；工具不在当前可执行 PATH 必须拒绝，不能回退使用宿主全局 `ruff`。

**门禁**：more-itertools 历史回归能识别并在真实沙箱中准备所有必要工具，不改核心逻辑以外的项目特判；另一个结构不同的陌生 Python fixture 成功；恶意 include/参数/循环 fixture fail closed。

### E3 — Node 脚本链与受限数据缓存

- 基于 `package.json` / lockfile / workspace/工具目录静态构建脚本/依赖关系。
- 识别固定的通用脚本包装器语义，说明“声明可以解析”与“动态 JS 逻辑无法安全静态证明”的边界；不要通过运行任意脚本侦测环境。
- 独立工具目录依赖在 preparation 阶段按实际锁关系预装并冻结；Verify 中若脚本还企图联网安装，明确阻断。
- 复用并回归 Vitest 2.1.9 版本限定数据缓存适配，扩展工具缓存应按工具版本/路径和真实 OCI 测量，不按仓库加例外。
- Yarn/Bun 既有能力不被回归破坏；未知动态安装不谎称支持。

**门禁**：radashi 历史回归全部批准检查可执行且结果诚实；至少一个不同脚本布局的 Node fixture 真实通过；动态 installer、平台二进制缺失、read-only/noexec 负面案例保持拒绝。

### E4 — 准备收据、EntryPoint Probe、Baseline 分类与 Runtime v2 衔接

- 生成 `VerificationEnvironmentContract` hash、完整图收据与 `ToolchainPreparationReceipt`；与现有 `prepare`、Explore、Verify、`freeze_spec()` 精确绑定。
- 命令入口预检在受限容器完成；检查 `working_directory`、实际工具路径、版本/二进制/平台 ABI；不能把 shell help-only 当成检查通过。
- `baseline_verdict` 区分准备失败、检查执行失败、实际断言失败、计划范围内预期复现、上游已修复/no-change；任何需要改变 approved commands 或 write paths 的请求必须生成新 child plan 和新授权。
- 环境失败不消耗 Coding Repair Budget，也不触发用模型瞎改测试/业务代码；可报告冻结缺口。恢复时绑定环境 graph/hash、审批和预算，不重写历史。

**门禁**：历史两个原生任务在真实隔离环境中能至少到达 v2 编码准入前的完整检查；遇到遗留基线失败可判定真实类别且拒绝范围膨胀；历史 Attempt/Approval/hash 原样保留。

### E5 — UI/API、真实泛化留出集、原生全流程

- 对话页直接展示发现、解析、准备、入口探针、基线、Coding、Verify、Review，具体缺少的工具和被哪个冻结声明引用；没有侧栏强制依赖；不重做前端框架。
- 对本专项实施规则做一次**冻结**：记录代码提交、解析器版本、已知测试仓库清单与保密/独立的留出集选取协议。
- 在冻结后首次选择陌生 Python 与 Node 项目，用**不修改生产引擎代码**的同一版本测试其环境；失败如实记录。另选真实陌生 Issue 完成至少一条 Python、一条 TypeScript 的原生对话 → Plan → exact user approval → Prepare → Baseline → Sandbox Coding → real Verify → independent Review → final human acceptance。
- 需要人工计划/最终批准时，明确暂停等待；任何人工审批不等于外部工程修复，外部 Codex/操作者修改业务补丁则必须标记维护介入、排除零维护样本。
- 若缺少实际模型/外部账户/平台，完成所有独立安全工作，留下可复现阻塞，不要伪造 M5 成功。Draft PR 只生成发布预览，未经另一次授权不写上游。

**门禁**：真实新任务跑出可审计变更和 Review；不能拿此前受控 fixture 成功代替原生用户任务。

### E6 — 安全、恢复、跨平台和交付

- 确认 provenance/CAS/幂等事务、取消/重启、未知 in-flight、恶意包、资源预算、凭据 canary、symlink/路径逃逸、lock 冲突、下载供应链、read-only/noexec 及 Verify 断网。
- 回归现有 v1/v2、数据库升级与回滚、API/UI、真实 OCI + Linux amd64/Docker + macOS arm64/OrbStack。若某平台环境不存在，记录 `not_run` 和具体阻塞，不使用模拟结果占门禁。
- 只部署到已确认的**隔离试验实例**；如果现有 `AGENTS.md` 的“自动重部署”涉及正式服务，先确认安全边界，不能覆盖现网、持久业务数据或原有服务。
- 文档/图/证据更新，与 Phase 5 门禁对齐；`docs/tasks.md` 仅勾选已有真实证据的条目；提供可审计的最终报告。

## 代码变更建议位置

| 类别 | 现有代码落点 | 工作方向 |
| --- | --- | --- |
| 静态执行能力 | `app/execution_preflight.py`, `app/verification_commands.py` | 静态命令图、三段 readiness、明确缺口 |
| Python 依赖准备 | `app/sandbox_worker/workspace_jobs.py`, `workspace_dependencies.py`, `python_dependency_check.py` | 递归 requirements、必要工具元数据、准备/探针收据 |
| Node 环境 | `app/node_environments.py`, `app/sandbox_worker/node_dependencies.py`, `node_cache.py` | 嵌套工具依赖、离线脚本、版本化缓存 |
| 实际基线与错误分类 | `app/sandbox_worker/verification.py`, `app/execution_outcome.py` | 实际命令语义、复现与既有失败分离 |
| 编码接续 | `app/contribution_workflow.py`, `app/agent_v2/orchestrator.py`, `app/agent_v2/capabilities.py` | 环境契约 hash、验证准入、恢复兼容 |
| 对话和 UI | `app/conversation.py`, `app/workbench_api.py`, `app/static/workbench.js` | 用户能看懂的事实状态与停止/恢复 |
| 测试 | 新增 `tests/test_environment_engine_*.py`、真实 OCI/Browser 测试 | 正/负、模糊、恢复、双平台及泛化 |

不要只按文件名机械修改；先读取真实代码与测试依赖，重构时坚持现有 Job/Artifact/DB 责任边界。新增 `app/environment_engine/` 只是候选目录。

## 迁移与回滚

1. 新版本只接管**新生成的环境契约**；现有 v1/v2 Attempt、Approval、baseline 仍由其原版本解析，保持只读事实。
2. 需要关联新环境收据时添加 forward migration、实际旧数据库升级、只读兼容、备份恢复及不可变哈希核验；无必要不改 DB schema。
3. 新旧运行时保持可关闭的安全开关；默认值与本分支原有策略保持一致，不因为本专项上线自动把实验 Runtime v2 投产。
4. 关闭 UPEVE 时旧会话不强制接入新解析器；不能用旧审批对变化后的新 ToolchainRecipe 执行副作用。
5. 恢复必须验证 `plan_hash`, `authorization_hash`, `source_archive_hash`, `image_digest`, `policy_hash`, `contract_hash` 一致；不一致保留旧事件并提示新计划/授权。

## 持续开发执行管理

Codex 在同一个正常 Agent 任务里按 E0→E6 推进。每完成一个实际门禁，更新 `docs/acceptance/universal-environment-v1/README.md` 和 `docs/tasks.md`，保持简洁的已完成/阻塞/下一步与证据链接。无需每阶段再让用户发“继续”；只有**真实外部前提**（人类精确审批、模型凭据、Linux amd64 主机、安全边界或其他无法自行提供的授权）才暂停对应分支，同时继续其他可做的工作。

可以按之前已明确的 v2 开发授权在**当前分支**做小范围本地提交；不得自动 push、PR、merge、覆盖 main 或向第三方仓库写入。任何新权限必须由用户再次明确授权。
