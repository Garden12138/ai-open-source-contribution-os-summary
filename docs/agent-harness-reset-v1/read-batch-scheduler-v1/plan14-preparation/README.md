# Plan14 验收准备完成 — 等待精确执行授权

2026-11-05，`refactor/agent-runtime-v2`。本轮只读核验并追加独立Plan绑定，
没有调用付费模型、执行真实Coding/Verify/Review或批准新执行，没有Push/PR/评论。

实际Plan14：`e8503bc5-9b61-4e51-95c6-c4eb33bb919e`。
Plan Hash：`1006e34393baf6a239c3f4e001af5726baaf3c69e00065a25d5934531b693d06`。
Binding Hash：`6222c965647cdf54e039dad1c41544cdd5dd54245e0b59c6ee528bbd584c36bf`。
**精确预览 Hash：`3825f2d60954527af8276556fdff71235a185554177a3d5159da2aed06769250`**。

[可读精确预览](execution-preview.md)、[完整JSON](execution-preview.json)、
[下一步确认文本](confirmation-text.txt)。这些文件仅描述未来请求，`sent=false`，不是执行授权。

## 四项先决条件

| 条件 | 核验结果与证据 |
| --- | --- |
| Plan12真实6/5/5只读信封 | [原始离线重放](../offline-replay.json)为4+2/4+1/4+1；当前e78源码最终JUnit中三个保存信封用例均通过 |
| Plan13真实第二响应8-call | [持久化Scheduler重放](../missing-file-semantics-v1/scheduler-replay.json)与[冻结归档真实OCI重放](../missing-file-semantics-v1/frozen-oci-replay-final.json)均7成功+1可信not_found、原序反馈，最多4-call子批次 |
| 批准新建及后续门禁 | OCI归档重放成功创建原批准路径、源码Hash变化并可再次读取；[额外离线组合核对](new-file-gate-check.json)通过新建→再次修改→旧Verify失效→最新Verify→独立Review→等待人工完整链 |
| 旧任务兼容 | [原兼容证明](../missing-file-semantics-v1/legacy-compatibility.json)与[本轮独立审计](preview-integrity-audit.json)核验全部旧行、9个Checkpoint、原审批和历史用量/未知占用均不变，Plan13 reads12未退还 |

[先决条件报告](prerequisite-check.json)在新Plan创建前保存。没有关键失败。
全部最终源码/证据/JUnit Hash重新校验；最终全量**2025通过、77跳过、0失败**，
真实macOS arm64/OrbStack OCI **24通过、0跳过**。本轮检查已有真实OCI证据，没有重新启动真实验收。
原生Linux amd64仍未运行，原跨平台门禁与HR4/HR5保持开放。

额外组合检查使用临时数据库、合成测试授权与确定性模型/工具/Review，实际Provider请求0；
它验证门禁组合，不等于对目标仓库运行4条pnpm命令，也不是真实模型验收。
缺失上下文按现有CodingContextEntry契约使用content/prior_hash/executable均null，未放宽生产校验。

## 冻结范围和新绑定

[只读上游检查](upstream-check.json)：Issue标题、正文、更新时间与main SHA均与原冻结输入一致。
正文Hash沿用规范JSON字符串SHA256，并另保存原始UTF-8字节Hash，避免混淆编码口径。
计划内容Hash仍`7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed`，
原8条write_paths、4条命令、deepseek-v4-pro配置、Sandbox策略与预算上限不变。

HEAD `e5cc92a8e3154acf79025cb0e235357b514024fd`，原修复工作区未提交；
五角色均运行已验证252项源码Hash `e78f36d6fc6c16175da08ad77f38ce30ebcb0a8357e9262b1fef7231dc65474f`。
具体app/worker/Runner镜像、源码清单、Model/Transport/Context/Preflight/Revision均冻结在精确预览。
新绑定采用v8原生观察、v2缺失文件预检与source-file-observation-v1。

新Plan以Plan13为只读父版本，不恢复其终态或使用其审批。
没有活动Job；为保存新版本，通过既有本地API追加准备状态转换，再保存同内容Plan，
并追加maintenance标记，避免将机械准备计为自主模型完成。
仅plan_versions追加1、contribution_task_state_versions追加1、workbench_events追加4。
四个事件为stop_requested、plan_bound、assistant_message、agent_maintenance_recorded；旧行未编辑。
任务事件2013→2017；Artifact1610、模型启动88、执行授权11、审批11不变，活动Job0。
[创建前状态](before-state.json)与[创建后状态](after-state.json)只保存完整表行Hash与非敏感域读取结果；
Linux业务库始终在原API容器内以query_only读取，未在宿主打开SQLite。

未来单轮仍为24模型调用、200000输入/32000输出Token、1800秒、64读/24写/6检查，
协议纠偏/Provider重试/代码修复各2；原始纯读提案8、执行子批次4。
独立Review沿用输入1800000字节、Job最多3尝试/300秒、单次输出16384 Token。
这些是待新授权的原上限，本次没有新分配。Planning64/64与三个未知占用原样保留。

## 真实验收报告准备

[报告契约](acceptance-report-contract.json)Hash已纳入精确预览；
[初始模板](acceptance-report-template.json)绑定实际新Plan与预览，真实结果均not_run。
要求保存原始调用/子批次/真实回执、not_found是否实际进入下一真实模型输入、
Patch提案及OCI应用、最新源码4命令Verify、独立Review和失败阶段/可恢复性。
没有适用缺失调用时记not_observed；生成回执但没有下一模型请求不算模型已观察。
失败保留已执行/未派发/未知状态与用量，不自动重启终态或购买下一轮。

## 复核与恢复

[prepare_plan.py](prepare_plan.py)仅有本地stop/plans两个HTTP写入端点；
不含execute/model调用。`--materialize`有创建前文件守卫和独立幂等Key，已消费本次准备动作，
不得重跑以创建新版本；若响应不确定，只读对账，不能另建方案。
[audit_preview.py](audit_preview.py)独立只读核验真实ID/Hash/预算/部署与旧数据，结果已通过。
[最终检查](final-checks.json)确认四端健康、compileall/diff检查、旧预览Hash、日志/文档凭据检查通过；
模型启动仍88、审批11、Attempt11、Plan14、Artifact1610、活动Job0。
本轮没有生产源码或迁移变更、没有重部署；对已验证五角色进行只读核验与健康检查。

下一步仅接受绑定本预览、实际Plan和Binding的一次新执行确认；
收到后仍需重新核验关键输入，变化则停止重新预览。当前准备工作停止在等待用户授权。
