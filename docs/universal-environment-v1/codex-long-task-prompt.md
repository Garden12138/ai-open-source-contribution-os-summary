# Codex 一次性持续开发提示词 — UPEVE v1

> 用法：解压文档包到仓库根目录，打开该仓库的 Codex **普通 Agent 任务**（不是仓库级 `/goal`），将下面「实际提交内容」完整复制到 Codex。该文本本身是任务输入，不是产品运行时系统提示词。

## 实际提交内容

请作为本仓库的长期工程执行 Agent，直接在现有的 `refactor/agent-runtime-v2` 开发分支完成 **Universal Project Environment & Verification Engine（UPEVE）v1** 专项实现，而不是再写一份建议报告。一次接受这个目标，然后在同一个任务中**自动按 E0→E6 连续推进，阶段结束不要停下来等我发送“继续”**。你必须如实报告无法独立跨越的外部前提，不能把未验收写成完成。

**第一步 / 强制上下文**

1. 先检查 `pwd`、`git status --short`、`git branch --show-current`、当前分支与 `origin/refactor/agent-runtime-v2` 的差异及 HEAD，保护所有用户未提交改动。目标分支必须是 **`refactor/agent-runtime-v2`**。如果当前在 `main`，在不覆盖任何修改的前提下安全切换到已存在分支；分支不存在时先确认本地/远端，再安全创建同名分支。不得在 `main` 改代码；不得 reset/clean/force-switch 覆盖现有数据。不同 worktree 中若找不到本次文档，请先核对文档所在的实际工作区，不能猜测。
2. 按顺序阅读 `AGENTS.md`、`docs/tasks.md`（Phase 5 当前状态）、`docs/agent-runtime-v2-refactor-spec.md`、`docs/agent-runtime-v2-architecture.md`、`docs/agent-runtime-v2-migration.md`、`docs/adr/agent-runtime-v2-engine-selection.md`、`docs/conversations-and-incidents-2026-10-27.md` 的 **2026-10-28 补记**及相关原生失败证据。超大附录只按需搜索读取，不要一次把全文件送入模型。
3. 本次的唯一实施规范为以下文件，按 README 串联：
   - `docs/universal-environment-v1/README.md`
   - `docs/universal-environment-v1/01-spec.md`
   - `docs/universal-environment-v1/02-architecture-and-contracts.md`
   - `docs/universal-environment-v1/03-implementation-plan.md`
   - `docs/universal-environment-v1/04-acceptance-matrix.md`
4. 阅读实际相关代码及测试，形成最小调用链图：`Conversation/Plan → preflight → dependency preparation → pre-change baseline → agent_v2/orchestrator.freeze_spec → coding loop → Verify → independent Review`。不要凭文档猜调用链、不能只改 Prompt。

**核心问题（必须解决）**

当前 v2 在真实用户任务中的首要障碍是批准的验证命令所要求的完整工具链未进入冻结环境准备：Python more-itertools #1316 实际缺 `ruff` / `stubtest`；Node radashi #257 的 lint 子项目动态安装依赖、Vitest 只读缓存阻塞。禁止为这两个仓库增加项目名/issue id/commit SHA 特判，也禁止放宽 Verify 网络、权限、执行位、绕过检查或在宿主执行未信任仓库代码。这次真正要实现的是能处理**全新项目**的通用、版本化、可扩展 Environment & Verification Engine。

**按阶段自主持续执行**

- **E0 基线**：复核两条已证实的真实故障、核心代码及分支；将 UPEVE E01..E07 项接入 `docs/tasks.md` 的 Phase 5，不勾选未完成门禁；建立专项的验收索引，保留历史失败证据和哈希。
- **E1 契约**：实现通用项目证据发现、Command Graph/Capability Registry/VerificationEnvironmentContract、准确 readiness 状态。`static_admitted` 绝不能冒充 `toolchain_prepared` 或 `baseline_verified`。严格哈希、数据来源、安全与协议兼容性。
- **E2 Python**：实现受限 PEP 621/dev group、requirements `-r/-c` 递归与约束、安全 Makefile/CI 静态证据、tool→distribution/installed executable 的真实映射；接入既有受限 pip/venv/sandbox 准备。针对项目未知结构不猜测、不执行任意配置脚本；不能按项目名加特殊分支。
- **E3 Node**：实现 npm/pnpm package script/workspace/子项目工具依赖的可证明静态解析与离线准备，复用既有 Vitest 限定数据缓存方案并守住 read-only/noexec；未知动态 installer 明确拒绝。保留 Yarn/Bun 既有回归和不能支持的能力边界。
- **E4 真实衔接**：把完整 EnvironmentContract/准备收据/入口验证绑定到 pre-change baseline 与 v2 admission；区分环境失败、既有测试失败、确切批准的 Issue 复现失败、真实通过与无法证明；失败不消耗编码修复预算，不能静默修改原 Plan/已批准命令；UI 显示准确证据。
- **E5 泛化与原生验收**：先固化已知故障回归，再记录引擎 HEAD/规则版本与留出协议，之后选择**未参与开发调试**的至少 2 Python + 2 Node 新结构项目。保持生产引擎代码冻结，实际运行；失败如实记录，不能换分母。对其中适合的陌生真实 Issue，分别完成 Python 和 TypeScript 原生产品对话→计划→精确批准→编码→真实测试→独立 Review→最终人工验收；人类必须批准时正确暂停等待，不要代替用户批准，也不要用外部 Codex 自己给业务 Issue 打补丁冒充原生交付。
- **E6 交付**：完成安全、重启、原生 OCI、老数据迁移/回滚、Mac OrbStack + 真实 Linux amd64 Docker（若没有则准确阻塞）、API/UI 回归。仅在可确认的**隔离测试实例**上运行部署验证；不得影响正式业务实例或生产数据。更新 architecture/migration/ADR（仅发生真实决策时）、验收索引和 `docs/tasks.md`。

**验收刚性要求**

1. 实现主体必须抽象为通用语言/生态/工具/版本适配器；核心逻辑不含 `more-itertools`、`radashi` 等仓库名分支。历史两个项目是 **H 类回归**，不是完成指标。
2. 引擎代码冻结后才能抽取 **U 类陌生项目**；每次测试保留原始执行事实。只通过脚本 Fake/Mock、受控 model fixture、预装好的特制容器或新增测试数量，都不能证明原生交付。
3. 不能删减原已批准验证命令、忽略失败、冒充 help-only/零测试成功。需要更改方案/命令/权限/环境重要冻结输入时，生成子 PlanVersion、重新精确审批；批准的真实 Issue 复现可通过明确限缩的 `expected_issue_reproduction` 契约进入代码修复，不能泛化豁免。
4. 不允许通过临时 `pip/npm install` 于 Verify 中联网，不允许对宿主机运行仓库代码，不泄露凭据。准备容器只通过原有受限 package proxy 获取版本化、可验证的依赖，Verify 保持断网，Publisher 权限独立。
5. Agent Runtime v2 的 durable jobs/checkpoints、现有审查与发布审批、数据不可变链不得破坏；不能擅自引入另一个权威任务状态库。
6. 保留真实 failures/unverified/platform blockers、量化实际用户 task 到达 Coding 与 Review 的数量；不得把计划保存、HTTP200、部署健康、组件 1,522 passed 误标为自主完成。

**执行与对话方式**

直接做代码修改、测试、真实沙箱验证；不要只分析或只生成文档。每完成阶段写可追踪证据/文档和下一步，**立即自动进入下阶段**。如果遭遇长上下文截断，优先按已存 Git 状态和阶段 checkpoint 重建进度，不重复已提交副作用、不擅自跳过任务。可以在当前授权范围内按阶段做清晰的**本地**提交；未经进一步授权不得 push、创建 GitHub PR、合并 main 或对上游任何仓库执行写操作。

真实无法推进时，只暂停该受阻门禁，其余安全独立工作继续；不要使用“需要你说继续”作为进度阻塞理由。仅当需要真实人类精确审批/缺凭据/缺 Linux amd64 环境或存在实质安全冲突时说明当前缺口，不编造结果。最终交付请按：完成的代码能力、文件/测试、两个历史回归结果、四个新项目泛化结果、两个原生 E2E 结果、Mac/Linux 证据、保留的旧记录、缺口与下一步、当前分支和 commits，清晰区分 passed/failed/blocked/not_run。

现在从 E0 开始，自动连续执行直到可完成的工作全部落地，严格遵守本仓库 `AGENTS.md` 与上述文档，不要把 Phase 5 或 Runtime v2 的未通过门禁提前宣布完成。
