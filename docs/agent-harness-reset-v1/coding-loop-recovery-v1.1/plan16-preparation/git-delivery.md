# Plan16 独立冻结准备：本地 Git 交付

本次仅获整理、核验和本地文档 Commit 授权。原27份准备文件保持原字节；新增本说明、
只读核验记录和逐文件清单，并同步必要路线图说明。未重建 Plan16、重复冻结 Binding、
追加业务记录、分配预算、创建审批或 Job。完成本地提交后停止，等待人工推送与独立审查。

[本次只读核验](git-readonly-verification.json) · [逐文件清单](git-delivery-check.json)
· [原精确预览](execution-preview.md) · [原最终审计](final-checks.json)

## 冻结与交付的两个 Git 时点

冻结时及本次提交前 HEAD 均为：
`b33349d684b1705cc9e1d30ee4aa7d71fd32e740`。
生产源码提交仍为 `bd3f8927bfed1a280b8d8ae15cdd446d30357917`。
本次文档提交是上述 HEAD 的直接后继，实际 Commit SHA 在最终交付消息中报告；
不会把这个新 SHA 回填进冻结预览或历史审计。

**文档提交不改变业务执行准入输入。** 源码与部署镜像不变，业务 Plan/状态/context、
Native 协议、模型身份、Runner/策略、环境、历史和 Job 均未变。业务 Revision 与
Preflight 由这些业务输入计算，不读取宿主 Git HEAD；源码依据见
`app/workbench_view.py`、`app/execution_preflight.py`、`app/workbench_api.py` 和
`app/contribution_workflow.py`，本次核验记录保存了其文件摘要。
提交后仍须用只读检查确认业务 Hash 原样，不能把文档提交当作执行审批。

**原归档脚本的逐字 HEAD 断言会失败。** `verify.py:local()` 保留
`git rev-parse HEAD == b33349d6…`；文档提交后 HEAD 前进，不能声称该归档脚本仍可原样
通过。没有修改该断言、覆盖旧 HEAD、改写冻结 Hash 或 monkeypatch/跳过检查。
若后续执行流程要求逐字 HEAD 相等，应停止，由人审查文档后继证据并明确决定如何
处理此差异；本次交付没有自动授予例外、重新冻结或执行权限。
当前真实付费执行仍为 **No-Go**，须新的独立精确一次性授权并重核全部执行条件。

## 原冻结标识（全部保持）

| 项目 | 值 |
| --- | --- |
| Plan16 | `6944ba99-2de3-492f-a687-0c71e804d94a` |
| Plan Hash | `aa03690633bcebc8e9c5346f5e930aa2c09a56aa9636a14b2528bb7fabeb74ca` |
| Binding Hash | `e97e605fc33502b47241c13e0930865dc8a0ffe33fd76a31ec26d1a989dc7b38` |
| 冻结输入 Hash | `9ba3758151c2cae54c2ce85bc86111ec57c72bc9add3cb1abcb22e3eaf2154cb` |
| 精确预览 Hash | `dab070664ebed9d0451414078c1648ffa0fed5c64e63242dec2ed9a0e33504ce` |
| Execution revision | `c34e29efb4b361d47b1be7345f84f825fa66f9a8251b0c6e651cc3da2f96408a` |
| Preflight | `ed34d5d1ae4306d1037836ac31e9b85cdd95ba04a41ff1aaf2e753f158c2b376` |
| 原最终审计 Hash | `0743f9a5c2d4b9e0fe90f09999f470637d4a43babbbccbdcfedc8790b066f11e` |
| 原确认文本 SHA256 | `2094a95350379342560db1cd42984610e5c134f2c893730bbf45926f38d9fb30` |

27份原文件的自Hash、原最终审计的逐文件摘要、先决报告的关联证据、JSON/Markdown
本地链接及预览中嵌入的完整确认文本均核验。`delivery-check.json` 中路线图摘要仍是
冻结交付时点，之后仅追加两行本次 Git 说明；原记录未回写。

## 当前部署、模型与预算

五角色源码均为255文件，集合Hash：
`627a3689ca6647f8747796469f7e93fddff23a8445557066a7cae061e3346d47`。

| 角色 | 实际本地不可变镜像配置 Digest |
| --- | --- |
| API / 普通 Worker / Provider Worker / Publisher | `sha256:c9c07093cc6d8cd001c3fbd3d39a819580da5ab4e16b7c0990bfcd0f13d7917f` |
| Sandbox Worker | `sha256:1c82d9a6e0a0143d13a1ba3ab2728cec19472130f28a346172c87daacbbe9476` |
| Runner | `sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6` |

五角色运行配置、权限、挂载、网络、资源限制和旧容器/镜像保留均核验，未部署或重启。
Native transport/Prompt `native-coding-loop-recovery-v11`、deepseek-v4-pro/high、单次
Coding4096、模型版本/绑定、原全部预算和3次未知占用保持。6+8+1仍从原总额扣除，
高reasoning截断和独立Review额外费用说明未改；没有付费调用、预算扩大或配置实验。
本次重核的是冻结归档和本地只读状态；没有刷新上游Issue，实际执行前必须再次核验上游。
API、Publisher、只读代理和模型网关的健康响应正常；健康查询不代表真实执行验收。

## 已有 Plan16 记录与旧历史的区别

本次 Git 任务新增业务记录数为0。上一轮明确授权的准备仅追加：

| 记录 | 标识或变化 |
| --- | --- |
| PlanVersion16 | `6944ba99-2de3-492f-a687-0c71e804d94a`，Plan总数15→16 |
| planning 状态版本 | `ce855144-4546-4787-8a31-4d5f980312ac`，状态版本42→43 |
| 新 Binding / plan_bound | `47d7d6df-58f7-457d-8749-51ce6fa8fd37`，任务sequence2553 |
| stop_requested | `9dc0740e-7452-44fa-ac30-217b60af40ed`，sequence2552 |
| assistant_message | `4f30856b-130d-4b23-acc1-ce01b656a951`，sequence2554 |
| agent_maintenance_recorded | `5837b441-5de8-456d-82d7-eb19fb0a0d5d`，sequence2555 |

Binding属于上述4条事件之一，没有重复计算。全库workbench_events2857→2861，当前任务
2551→2555；本次重新读取仍为冻结后的45表全部行，11旧Checkpoint、1802 Artifact和
Plan11–15终态/审批/费用/预算/未知占用均原样。模型启动106、执行授权13、Plan审批13、
活动业务Job0。Plan16审批/Attempt/Coding Session/ReviewRun均未创建。
父Plan15仅作不可变血缘引用，不复用其终态、审批或Binding。

## 文件选择、安全扫描与交付检查

提交范围为本目录30文件（原27文件及本次3份Git交付记录）和`docs/tasks.md`，共31文件。
逐文件路径、字节数、原文SHA256和Git blob标识见清单。清单自身用独立的canonical
`report_hash`校验，提交后再读取全部Commit blob与工作区逐字节核对，避免自引用Hash。
不纳入`__pycache__`、数据库、私有备份、凭据、工作区、日志或其他6个既存未跟踪文件。
选择性暂存具体路径，正常运行Git Commit，没有禁用hooks或使用全量暂存。

敏感扫描覆盖原材料、新交付记录、路线图新增行及最终暂存blob，检查凭据模式、内存中
读取的已配置凭据及编码变体/canary；只保存计数，不导出密钥。路线图原有7个检测器
模式匹配与HEAD版本一致，新增文字和精确凭据检查通过。原始审计中的扫描结论不回写。
`git diff --check`、新增文件空白检查和`compileall`通过；本次没有重跑模型、OCI、Coding、
Review或默认测试套件，也不把已有离线/替身证据当作新的真实成功。

预览、确认文本和冻结Hash仍是上一轮时点。Git Commit不消费确认文本，不启动执行。
HR4/5、P5-G06、Linux amd64仍开放。提交完成后停止，等待人工推送与独立审查。
