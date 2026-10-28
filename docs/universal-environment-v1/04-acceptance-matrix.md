# UPEVE v1 — 验收矩阵与独立泛化协议

**目标**：证明新架构对新项目有真实泛化，而不只是两个仓库的专用修补。全部证据应按类别归档：纯函数合同、真实依赖准备、真实 OCI baseline、受控模型、原生产品用户旅程、真实跨平台。

## 1. 测试数据的三种严格分类

| 类别 | 使用方法 | 是否计为陌生项目泛化？ |
| --- | --- | --- |
| H：历史故障回归 | 固定 2026-10-28 用户失败的 GitHub commit、计划/命令与原始失败收据；用于证明原故障被修复 | **否**。可覆盖未来回归，但不能宣称首次独立完成 |
| D：开发期对照 fixtures | 最少使用不同项目结构、包管理器、依赖布局、命令包装器、错误场景；可反复迭代实现 | **否**。用于确定规则的通用性和边界 |
| U：实现冻结后的留出项目 | 先冻结规则代码/版本/hash，记录选择规则，再首次接触未参与调试的项目；第一次结果永久保存 | **是**，满足下述条件时。后续为了该项目写代码，该项目永久转为 D；重新选择 U |

**严禁**通过更名 H/D 仓库制造“陌生项目”，不能凭新 Issue 编号冒充新环境测试；用不同仓库实际结构、锁文件、版本/目录组合验证泛化。留出项目由明确、可复现的筛选方式产生并在测试前存档，不得因失败而悄悄更换分母。

## 2. 验收表

| ID | 验收内容与输入 | 最小通过条件 | 证据类型 |
| --- | --- | --- | --- |
| UE-T01 | Pure function 对 `pyproject.toml` 标准/分组/extras 识别 | 解析声明、版本和 provenance；动态字段不能猜 | unit |
| UE-T02 | requirements `-r` 嵌套、`-c` 约束、相对路径 | 正确展开和约束；保留文件哈希、相对目录；不把约束当安装包 | unit |
| UE-T03 | 递归环、越界、符号链接、恶意选项、外部仓库/URL、过深/过大 | 拒绝且具稳定 reason_code；无宿主执行/未授权网络 | security |
| UE-T04 | Makefile/CI/pyproject 命令来源 | 只静态提取安全子集；动态 shell 内容不能执行 | unit/security |
| UE-T05 | CLI 程序不等于发行包名称（如 mypy 的 stubtest） | 通过合法来源和实际 installed entry-point 关联，不猜包名 | unit/OCI |
| UE-T06 | Node scripts/workspaces/lockfile/子包工具 | 给出来源链，独立工具依赖在 Prepare 完成；Verify 无动态安装 | unit/OCI |
| UE-T07 | Node 动态脚本、`pnpm dlx`、curl|sh、未受支持生命周期 | 明确 `unsupported`，不绕过代理或 Verify 断网 | security/OCI |
| UE-T08 | Vitest 数据缓存 read-only / noexec | 已支持版本在真实 OCI 成功；代码/依赖只读，缓存只在受限数据位置；未知版本不适配 | OCI/security |
| UE-T09 | 版本/ABI/平台不匹配或二进制缺失 | 记录 `platform_binary_unavailable`，不偷偷下载/执行宿主版本 | OCI |
| UE-T10 | 所有已批准检查的命令与工作目录、工具可执行性 | 有 exact argv/cwd/entrypoint provenance；缺任一项不标 prepared/proven | OCI |
| UE-T11 | 真实 baseline：help-only、零测试、未启动、错误退出 | 即使返回 exit 0 也不误判检查通过，原始收据不覆盖 | OCI |
| UE-T12 | 真实 baseline：已知 Issue 失败、无关历史失败、环境问题 | 分类明确；预期 Issue 复现仅在批准范围内进入 coding | contract/OCI |
| UE-T13 | Prepared 与 Verified 三段状态 | 静态 admitted 不能在 UI 中显示为实际可用；Job succeeded 不等于验证成功 | API/UI |
| UE-T14 | Plan hash、指令集合、源/镜像/Policy 修改 | 执行拒绝旧授权；必须新 child plan/确认；不改变旧记录 | persistence/security |
| UE-T15 | 工作流重启、重复请求、并发、未知 outcome、取消 | 不丢工件、不重放有副作用步骤、不清预算 | persistence/OCI |
| UE-T16 | Prompt injection / Secret canary / third-party writes | 不泄露、不越权；模型与仓库数据不能改变工具权限 | security |
| UE-T17 | H-Python：more-itertools #1316，冻结提交 `4aeb4338ae1407d8da9723e86da99efeb446f719` | 原完整 5 条命令在当前工具环境中可执行；不改动原批准列表、不按仓库名特判；如产生非环境的失败真实分类 | native OCI regression |
| UE-T18 | H-Node：radashi #257，冻结提交 `0a0e8f4c96d141267adf02a78df7d5fbccb672ef` | 原完整 8 条命令可执行；嵌套工具依赖、缓存、网络边界正确，真实结果记录 | native OCI regression |
| UE-T19 | U-Python-1：从未参与改造的 Python 新仓库/依赖布局 | 引擎不改代码即可自动识别、准备、实测 baseline | unseen OCI |
| UE-T20 | U-Python-2：与 UE-T19 不同的依赖/构建声明方式 | 如在已承诺支持范围内，真实准备及 baseline 通过；否则正确、安全不支持，不以 unsupported 冒充成功 | unseen OCI |
| UE-T21 | U-Node-1：新 npm/pnpm 仓库且脚本结构不同于 radashi | 无项目规则补丁，真实准备+完整 baseline | unseen OCI |
| UE-T22 | U-Node-2：不同工作区/包管理器组合 | 声称 supported 的组合真实通过；未支持的组合有准确缺口 | unseen OCI |
| UE-T23 | U 原生 Python Issue 端到端 | 用户产品内新目标 → 计划 → 精确授权 → 沙箱真实修改和测试 → independent Review → 人工最终验收；无外部业务补丁 | true native E2E |
| UE-T24 | U 原生 TypeScript Issue 端到端 | 同上；包含实际测试失败→代码修复→再次验证，保持授权与累计预算 | true native E2E |
| UE-T25 | 确实需要不受支持服务/动态网络/特权的陌生项目 | 安全停在明确原因，未进入编码，无假成功 | boundary negative |
| UE-T26 | macOS arm64 + OrbStack | 使用真正 OCI 任务/沙箱、当前镜像/Policy、恶意 suite，不是 Fake | platform |
| UE-T27 | Linux amd64 + Docker Engine | 同一协议/策略的真实 Linux amd64 运行；无真实端点则 `blocked/not_run` | platform |
| UE-T28 | 旧数据库、旧任务 v1/v2、原批准与历史事件 | 迁移/回滚、哈希完整、旧 baseline 不重新解释为通过 | regression/migration |

**判定规则**：UE-T19..22 中明确标记 `unsupported` 的合理边界案例可证明“正确拒绝”，但**不能**计入“已支持项目自动成功数”。最低要有至少两个 Python + 两个 Node 新结构的真实项目被测，其中每种生态至少有一项完整准备并实际运行 baseline；对“跨项目已支持”作出的具体宣称必须有相应真实结果。端到端 UE-T23/24 仍须单独满足；如测试需要人类批准、外部模型或真实 Linux 主机，分类为 blocked，不把 fixture 替代。

## 3. 历史失败回归的固定输入与禁止作弊规则

### H1 Python

- 仓库：`more-itertools/more-itertools`，base SHA `4aeb4338ae1407d8da9723e86da99efeb446f719`；任务 `#1316`。
- 原批准命令 ID：`test_more`, `test_all`, `format_check`, `lint_check`, `stubtest`；原命令须从存档的 Plan/Artifact 校验后复用。
- 旧事实：前两条通过；后三条 exit 127，`ruff` 和 `stubtest` 无法启动。
- 参考声明链：`Makefile → requirements/development.txt → testing.txt/typechecks.txt`；`ruff` 来自 testing；`stubtest` 的入口与 mypy 分发包相关，需实际证实安装后入口。
- 禁止：给 more-itertools 写 `if`；为凑结果在 Verify 中 `pip install`；删后三条命令；使用宿主系统 Ruff；旧失败覆盖为通过。

### H2 TypeScript

- 仓库：`radashi-org/radashi`，base SHA `0a0e8f4c96d141267adf02a78df7d5fbccb672ef`；任务 `#257`。
- 旧事实：8 条检查 6 条退出 0，`full-test-suite` 的 Vitest 缓存写入 `EROFS`，`lint` 的脚本子项目尝试联网安装依赖并失败。
- 脚本链：根 `package.json` → `node scripts/run.js lint` → `scripts/lint/package.json` 的独立工具依赖。另有既存 Vitest 2.1.9 限定缓存组件适配。
- 禁止：直接把 `scripts/lint/node_modules` 从宿主拷入；在 Verify 里放开网络；用宽松 chmod/exec 挂载；删除 lint；把缓存组件 fixture 记为真实 radashi 任务成功。

原始事件/工件参见 `docs/conversations-and-incidents-2026-10-27.md` 的 10-28 补记及 `docs/acceptance/agent-runtime-v2/user-trial-*.json`。历史修复只能作为平台维护，不可清除或改成原生成功。

## 4. 冻结后留出集的协议

1. E4 完成后，**先**记录 engine HEAD SHA、parser/capability versions、镜像/Policy、选取准则、候选筛选条件和已有开发样例名单（`holdout-protocol.json`）。
2. 新选真实公共仓库时记 `full_name`、commit SHA、licence/source rights、language/manager、环境声明方式、首次发现时间、排除原因。不可使用之前修复/调试过的同一个仓库作为 U。
3. 在第一次进入分析/准备前固定样本清单和期望的“受支持/边界未知”归属；真实执行后存完整状态、失败、修复/介入次数。禁止“跑坏了就换一个，没记录”。
4. 如果通用规则需因某个 U 样本完善，该样本从此作为开发回归，首次失败仍必须存档；新版引擎再抽取其他 U，不能重写首次 pass rate。
5. 不得仅选择只含 pytest/最小 package.json 的玩具样本；新语言/工具边界要在承诺范围内随机覆盖不同实际生态。
6. 对两条需要真实业务代码修改的原生 Issue，冻结用户目标、来源、写路径、必须执行的命令及预算。人工批准属于合法产品步骤；**外部 Codex 给业务打补丁或直接修数据库，则样本不得算原生无人维护成功**。

## 5. 报告模板与审计台账

每个样本在 `docs/acceptance/universal-environment-v1/` 下用唯一目录保存机器可读 evidence（只能保存脱敏副本及 hash/refs），至少：

```text
sample-id/
  manifest.json                 # H/D/U, repo SHA, plan, platform, image, policy
  command-graph.json            # commands + toolchain provenance / hash
  preparation-receipt.json      # installed tools / exact versions / missing
  entrypoint-receipts.json      # actual probes
  baseline-results.json         # per-command exit, executed evidence, verdict
  session-evidence.json         # optional actual model/tool/review journal
  classification.json           # autonomous eligibility, operator interventions, gates
```

不能提交密钥、原始数据库、网络 token、任意超大 stdout/stderr 或未脱敏敏感信息。文件是否生成以实际测量为准，不能预写成功 JSON。若原始不可公开，存不可变 hash、相应本地只读证据定位及明确的缺失信息。

`docs/acceptance/universal-environment-v1/README.md` 按执行时间列出：阶段、工具调用来源、测试命令、passed/failed/skipped/not_run、平台、受控/原生标记、人工审批、维护介入、外部阻塞和下一步。不要把 1,522 个单元测试与若干 OCI 项相加作“交付数量”。

## 6. 定义 Done

**工程实现 Done**：契约、通用解析、受限准备、真实可调用验证、API/UX、迁移与安全 regression 已通过且证据完整。

**真实泛化 Done**：UE-T19..24 满足明确的通过门槛，且引擎代码在留出测试中无需按仓库修改。

**双平台 Done**：UE-T26/27 都有真实运行结果；未提供 Linux amd64 端点时只能记 gate blocked。

**整项目 Done**：仍受既有 `AGENTS.md` 与 `docs/tasks.md` Phase 2–10 全部门禁约束；UPEVE 专项完成不等于 Runtime v2 M5/M6、Phase 5 或整个 Goal 自动完成。
