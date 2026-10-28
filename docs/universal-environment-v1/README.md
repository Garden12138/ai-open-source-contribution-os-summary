# Universal Project Environment & Verification Engine (UPEVE) v1

> AI Open Source Contribution OS · Agent Runtime v2 配套专项 · 2026-10-28
>
> **目标分支**：`refactor/agent-runtime-v2`（保持在此分支；不得修改 `main`）
>
> **当前状态**：实施规格，不是已完成的实现或验收证明。

## 为什么需要这项改造

`docs/conversations-and-incidents-2026-10-27.md` 的 2026-10-28 补记及 `docs/acceptance/agent-runtime-v2/README.md` 证明，v2 已具备持久化模型—沙箱工具循环，但最新两个原生用户任务在编码前被验证环境阻塞：

- `more-itertools/more-itertools #1316`：五条已批准命令中仅两条通过；`ruff`、`stubtest` 不在可执行路径中。冻结的准备清单以 `pyproject.toml` 为入口，但仓库的 `Makefile` 通过 `requirements/development.txt` 递归引用测试及类型检查工具需求。
- `radashi-org/radashi #257`：八条命令中六条通过；`pnpm test` 的版本化 Vitest 缓存写入只读依赖卷遭遇 `EROFS`，`pnpm lint` 所调用的 `scripts/run.js` 会尝试安装独立工具子项目依赖，而 Verify 必须断网。针对 Vitest 2.1.9 的受限数据缓存适配已有组件验证，原生任务仍未完整通过。

**本专项目标不是让这两个仓库“跑起来”，而是让全新的、未参与开发调试的仓库可通过通用能力自动识别并准备环境**；能力确实不支持时要明确地停止，不向用户假报 `ready`。

## 文档目录与阅读顺序

1. [`01-spec.md`](01-spec.md)：目标、范围、功能与非功能需求、必须遵守的安全/非特判约束。
2. [`02-architecture-and-contracts.md`](02-architecture-and-contracts.md)：通用发现、解析、冻结、准备、验证契约与数据模型。
3. [`03-implementation-plan.md`](03-implementation-plan.md)：代码落点、兼容迁移、分阶段实施及阻塞处理。
4. [`04-acceptance-matrix.md`](04-acceptance-matrix.md)：历史故障回归、陌生项目泛化、安全及平台验收矩阵。
5. [`codex-long-task-prompt.md`](codex-long-task-prompt.md)：在 Codex 普通 Agent 任务中一次性提交、自动持续分阶段推进的执行说明。

## 与现有文件的关系

- 此专项**补充** `docs/agent-runtime-v2-refactor-spec.md` 的 M3/M5/M6，不替换已存在的 Agent v2、Publisher、审批与不可变事件链。
- `AGENTS.md` 的约束继续有效；`docs/tasks.md` 仍是路线图唯一状态来源。Codex 应在 Phase 5 下新建本专项任务与验收门禁，不能私自勾选 M5/M6、P5-G06、ENV-G01 等未通过项。
- 不把过去两个用户实际失败样本及后续平台修复记录改写为成功的零干预样本。
- 当前文档包不会修改源码或远端仓库；部署、真实外部写入仍须按既有授权边界执行。

## 最终判定

**可以宣布引擎 v1 达到目标**：已支持的工具链经通用事实识别、冻结、受限准备和真实检查后，可在新仓库中不增加项目特判地进入原生 Coding → Verify → independent Review；不能支持的项目准确返回机器可读的环境缺口。

**不可宣布达到目标**：只解决两个历史仓库、只跑单测/Fixture、修改提示词或错误文案、或有外部 Codex 维护介入后把结果计为原生自主成功。
