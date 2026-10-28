# UPEVE v1 — 通用项目环境与验证引擎工程规格

**文档版本**：v1.0 · 2026-10-28  
**项目**：`Garden12138/ai-open-source-contribution-os-summary`  
**目标分支**：`refactor/agent-runtime-v2`  
**业务阶段**：Phase 5 中的 Environment/Verification 专项，服务于 Runtime v2 M3、M5、M6。

## 1. 问题定义与事实边界

现有 `app/agent_v2` 并不是当前两次真实试用的第一阻塞点。`app/contribution_workflow.py` 先执行环境准备、pre-change baseline；`app/agent_v2/orchestrator.py::freeze_spec` 要求 baseline 通过才创建编码会话。因此静态 `preflight ready`、成功的准备 Job，都不保证 v2 能真正开始编码。

### 1.1 已证实的两个故障类别

| 类别 | 证据 | 系统性缺口 |
| --- | --- | --- |
| Python 开发工具链未闭合 | more-itertools #1316；`Makefile`、`requirements/development.txt`→`testing.txt`/`typechecks.txt`；`ruff`/`stubtest` 入口不存在 | 从批准命令反推工具/版本/递归声明、准备并核实命令入口没有形成完整契约 |
| Node 脚本的二级工具环境未闭合 | radashi #257；根 `pnpm lint` 调用 `node scripts/run.js lint`，动态进入 `scripts/lint` 并安装依赖；Vitest 缓存写入只读目录 | 多包脚本链、二级工具依赖、代码只读与数据缓存分离缺少通用建模 |

`ruff`/`stubtest` 是已观察到的入口缺失；其详细原因必须由原始依赖收据、PATH、包清单和命令检查共同验证，不可在未验证时把一切归因于 PATH 或网络。旧样本只能作回归，不能洗成自主成功。

### 1.2 目标

实现 **Universal Project Environment & Verification Engine (UPEVE)**：对新的、未针对性修补的 GitHub 仓库，基于**冻结源代码事实**发现技术栈、构建入口、验证命令的真实依赖图；通过插件化生态解析器和版本化执行配方在受限环境中准备、验证，并将可靠结论绑定到 Agent Runtime v2 的编码准入、恢复和原生 UI。允许明确的、不支持的边界，不承诺支持所有语言和任意原生服务。

## 2. 非目标

- 不重写 `app/agent_v2` 的 durable loop，不另建 LangGraph/Agents SDK business state store，也不改变 SQLite/Workbench 为唯一授权源。
- 不默认执行用户仓库的 `make install`、`preinstall`、`curl | sh`、任意 build hook、shell 命令来“侦测”依赖。
- 不要求初版支持任意语言、所有构建系统、GPU/浏览器/数据库集群或特权内核能力。
- 不以更换模型、放宽 model budget、加重试、放宽 Verify 联网、宿主机执行、删除断言/检查来解决问题。
- 不提供自动 Fork、Push、PR、Issue 评论或其他 GitHub 修改权限；Publisher 原有精确人类确认不变。

## 3. 能力覆盖与承诺级别

采用三级能力状态：`supported_proven`（对当前冻结配方和目标平台有真实收据）、`supported_unproven`（静态解析与准备方案可用，尚无当前真实检查）、`unsupported`（无法保证安全或完整性）。未知不得推断为支持。

**v1 最小工程覆盖：**

- Python：`pyproject.toml`（PEP 621、适用的开发依赖组/可验证 extras）、受约束的 `requirements*.txt` / `-r` / `-c` 递归引用、Makefile 与 CI 作为**声明性证据**、`python -m ...`/指定工具入口；已识别版本的 `pip` 轮包准备及离线安装。`tox`/Poetry/uv/PDM 先识别其存在和能力边界，只有做完对应适配与真实测试才能声称支持，不能默认套 `pip`。
- Node/TypeScript：`package.json`、workspace 配置、已支持的 lockfile/包管理器、声明式脚本链、独立工具包目录依赖、bin 链接与平台可用性；已存在的 Vitest 版本限定缓存适配可复用。`npm`、`pnpm` 为优先闭环；Yarn/Bun 必须保留既有已验证能力，不可宣称通用覆盖其未验证用法。
- 跨语言/生态：定义统一 `EnvironmentInspector`、`ManifestResolver`、`ToolchainProvider`、`CommandResolver`、`Verifier` 的扩展契约，让未来新增 Go/Rust/Java/Python 新包管理器无需把仓库名写入核心调度。

**范围约束**：通用是指**规则和扩展机制**通用，不等于当前版本的所有组合都通过真实验收。对目标平台、镜像、版本、私有包、原生库/系统服务依赖需要分别给出能力结果。

## 4. 功能需求（FR）

### FR-01 冻结事实采集

- 输入仅包含经归档、验证 hash 的仓库清单、源文件、版本化 Issue/Plan、目标平台/镜像/Policy、已批准的检查命令。
- 收集 `pyproject.toml`、`requirements`、`Makefile`、`tox.ini`、`package.json`、workspace/锁文件、GitHub Actions 等**按需**证据；对 CI/Makefile 使用安全语法子集做静态提取，不执行它们。
- 记录 `source_path`、`sha256`、证据来源、解析器版本、可信度及无法解析的剩余段；对超限、循环、路径逃逸、符号链接明确失败。

### FR-02 命令与依赖图闭环（核心）

- 每一条 Approved Plan 命令建立 `command_id → exact argv/cwd → wrapper/script chain → executable/entrypoint → tools/packages → manifests/lock/artifact` 映射。
- 支持 `-r` / `--requirement` 与 `-c` / `--constraint` 的相对包含，使用有限深度、总文件数、字节数、依赖数、路径白名单和检测循环的解析器；禁止危险的 pip 参数、外部路径、未经授权的 index/VCS/URL/可执行指令；对跨文件 constraint 必须保留语义和哈希。
- Node 子包脚本链和动态安装场景：解析可验证的目录及 `package.json` 依赖，形成离线预装配方；无法证明运行时动态行为的脚本一律 `unsupported_dynamic_installer`，不可预执行试探或打开 Verify 网络。
- 构建脚本/CI 提取的建议命令不是批准命令；不能静默添加或移除已批准的验收要求。重复和范围过大的命令可在计划确认**之前**提出去重/替代建议；确认后修改需新 PlanVersion 与新授权。

### FR-03 ToolchainPreparation

- 解算版本、锁文件和生态工具包，生成**封闭**的准备图/配方，检查当前目标 CPU 架构/OS/ABI、工具二进制可执行性和缓存需求。
- 在独立无 GitHub/模型凭据的沙箱准备容器、仅使用受限包代理取得批准的包，记录所有下载与构建散列；Verify 必须断网。
- 在不可变依赖卷和 `noexec` 临时空间前提下，使用受限版本化缓存数据适配；不得复制可执行工具到不可执行目录，也不得使执行源代码获得额外可写权限。
- 对未知 package script/生命周期 hook、平台不兼容、镜像缺系统依赖、离线工具不可得返回具体原因与安全恢复建议。

### FR-04 三段式就绪检查

明确区分：
1. `static_admitted`：语法/目录/命令/规则初步可支持；尚非可运行。
2. `toolchain_prepared`：精确依赖已准备、所需入口可调用并取得实际收据；不代表测试通过。
3. `baseline_verified`：按原授权的完整命令集合在真实当前源与当前环境运行，留下每条检查的实际退出码、识别到的测试数量/有效执行证据及分类。

**UI 禁止仅凭静态检查展示“环境已经准备好，可进行编码”。** 执行批准前显示“静态检查通过，真实准备/基线尚未完成”；批准后再进下一阶段。未满足准入条件绝不启动 Coding Agent。

### FR-05 Baseline Outcome 与原始 Issue 复现

- `environment_blocked`：命令入口缺失、依赖不可用、平台 ABI/服务缺失、网络策略冲突、权限/缓存限制等；不可计作代码修复预算。
- `preexisting_failure`：与 Issue 无关、在冻结原基线真实复现的失败；不允许默默修改无关源码或降低检查。
- `expected_issue_reproduction`：经明确已批准的预期失败测试与 issue 关联，且**对比测试/基线命令**可明确证明实际执行；此状态允许进入限定的修复流程，但不能把原本要求通过的其他命令豁免。
- `passed`：全部必需的有效检查真实通过；帮助文本、零测试误成功、未运行/跳过不等于通过。
- `unknown`：无法分类则阻断；不从模型描述推断成功。

### FR-06 与 Agent Runtime v2 的契约衔接

- 在 `app/contribution_workflow.py` 的 preparation/baseline 阶段落地；`app/agent_v2/orchestrator.py` 的冻结 TaskSpec 扩展明确的环境收据哈希与 capability hash，旧会话继续读原协议。
- 任何从旧失败恢复都必须经新证据、新的受控 EnvironmentPlan，不回写旧结果；模型无法扩大写入范围、检查参数或网络/工具权限。
- 准备逻辑可由规则实现；模型可**提出**候选命令/环境需求，但不能作为事实来源、授权或直接执行路径。

### FR-07 对话体验

- 在产品对话中按事实展示：已识别语言和构建工具、命令来源、已准备工具、未准备工具、具体失败、可恢复性以及关联证据。
- “继续”只有在允许的状态机转移时恢复原冻结任务，不能重置预算或悄悄重新批准；查看进度不调用不必要的模型，也不取消后台任务。
- 对环境缺口给出用户可理解的行动建议（如需增加验证环境适配、外部 Linux amd64 执行端不可用），但不承诺当前可支持。

## 5. 非功能需求（NFR）

- **泛化**：生产核心不含仓库名、Issue 号、文件 SHA 的特判；通用适配器由构建生态/配方/工具版本驱动。
- **确定性**：相同归档、计划、目标平台、镜像、策略及解析版本产生相同契约哈希；外部依赖下载与实际执行结果另外绑定时间/环境收据。
- **安全**：不执行非信任构建脚本作为预检；准备与验证隔离，No network Verify；持续沿用容器/凭据/资源限制、可执行性和路径安全检查。
- **可恢复性**：持久 Job、CAS、工件哈希、幂等重试；中断、未知完成结果保守处理。禁止改变审批哈希后继续旧任务。
- **可观察性**：按命令/工具/环境阶段有安全的 reason_code、来源、证据，不存泄露密钥的完整环境变量。
- **效率**：只对已批准/候选检查需要的依赖链进行有界展开；冻结缓存可复用但使用前必须验证来源/版本/平台/哈希；不能因为性能优化损失隔离或正确性。

## 6. 禁止特判验收要求

1. 核心引擎中不得出现 `repo == ...`、`owner/name`、Issue ID、固定 commit SHA、按某个项目目录名例外处理等业务规则。
2. 已知失败项目放在测试 fixtures/数据中；生产规则只能根据语言、生态、工具版本、清单和可证明的结构触发。
3. 每个新增适配能力都必须有至少一个**不同项目结构**的对照 fixture（含正/反例）；单个项目通过不算泛化。
4. 泛化留出集必须在实现规则冻结后、首次运行为止，不允许根据留出项目失败写项目私有适配后还称其为未见样本；失败如实记账并在新留出集重测。

## 7. 完工边界与成功条件

- G01：两个历史故障得到真正的通用规则回归，保留旧失败且不放宽隔离。
- G02：至少四个之前未用于调试的独立仓库/样例，跨 Python 与 Node，不改生产引擎代码即可完成受支持的环境准备/基线；至少两个是包含真实陌生 Issue 的原生模型—沙箱代码—Review 旅程。
- G03：无法支持的动态安装、无锁依赖、危险构建、需要特权服务等必须安全、准确拒绝，且不向用户假称 ready。
- G04：保留 v1、v2 数据与精确授权；真实 macOS arm64/OrbStack 与 Linux amd64/Docker 按 `AGENTS.md` 门禁分别报告。
- G05：所有验收结果附可核验的实际 Job、artifact、命令、测试和 Review 证据；受控 fixture、人工维护或外部 Codex 代码补丁不得混作原生自主成功。

**不能实际运行的外部环境或等待真实用户精确批准，记为 blocked/unverified，不伪造完成。**

## 8. 源码与已有证据（实施时复核）

本专项的具体模块包括 `app/execution_preflight.py`、`app/verification_commands.py`、`app/node_environments.py`、`app/sandbox_worker/workspace_jobs.py`、`app/sandbox_worker/workspace_dependencies.py`、`app/sandbox_worker/node_dependencies.py`、`app/sandbox_worker/verification.py`、`app/contribution_workflow.py`、`app/agent_v2/{orchestrator,capabilities,integration}.py`、既有 `docs/acceptance/agent-runtime-v2/`。文件可以按职责重构，但必须先检查实际调用链与测试，不能假定文件语义完全如旧版本。
