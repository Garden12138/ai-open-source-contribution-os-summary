# Plan14 单次真实模型验收 — blocked

2026-11-06。用户明确批准预览
`3825f2d60954527af8276556fdff71235a185554177a3d5159da2aed06769250`，
仅限Plan14 `e8503bc5-9b61-4e51-95c6-c4eb33bb919e`、原8路径/4命令、
现有deepseek-v4-pro及原预算的一次真实执行与独立Review。该轮执行已经消费，不能复用。

**结果blocked：新准备与Baseline4/4通过，真实模型调用3次；
第三次响应的8个read_file请求在输出容量准入时整批拒绝。没有Patch、修改后Verify或独立Review。**
未恢复Plan11/12/13/14、未扩预算、未另起付费轮次、未Push/PR/评论或发布。

## 精确绑定与本轮身份

| 项目 | 值 |
| --- | --- |
| Plan Hash | `1006e34393baf6a239c3f4e001af5726baaf3c69e00065a25d5934531b693d06` |
| Binding Hash | `6222c965647cdf54e039dad1c41544cdd5dd54245e0b59c6ee528bbd584c36bf` |
| 本次执行授权事件Hash | `3aaa2d98e361552e5660de5dc13b14a1f2d2d2bff3dc6e3cf77e0355375df66d` |
| 单次提交Job | `469e1c19-b153-4a7d-abf7-9e5914cf295b` |
| Run | `c119d687-50bf-4176-b9ce-5272d3d7dcb5` |
| Attempt | `122cc13a-0573-4317-85cd-58b89688fb6f` |
| Native Session | `session-2f7eb4b35c9954bc3c1623c299cf8dde` |
| 终态Checkpoint | `657f9c2dd0c8a05866d4104dc790de3c5d45e8db6308b583c2bd0d3b533df31b` |
| Runtime源码 | `e78f36d6fc6c16175da08ad77f38ce30ebcb0a8357e9262b1fef7231dc65474f`，252项，与批准预览一致 |
| 冻结Issue基线 | `3489e17c48985b74fdb42cde122376b1eb12d8a7` |

执行前重新逐项核验源码、五角色镜像/资源/挂载/隔离、Issue正文/标题/main、
实际Plan/Binding、模型、Context/Revision/Preflight与原预算；全部一致。
[上游记录](upstream-before-execution.json)、[新授权](approval.json)、
[持久化提交意图](dispatch-intent.json)、[唯一HTTP202回执](dispatch-receipt.json)均保留。
没有改写批准预览或历史文件。全部不可信仓库执行由原OCI Worker完成。

## 实际阶段与请求

| 阶段 | 真实结果 |
| --- | --- |
| 准备 | 新鲜OCI准备成功 |
| Baseline | 原4条批准命令4/4通过 |
| 模型第1轮 | 4个read_file，默认offset0/max_bytes4096，均真实读取成功 |
| 模型第2轮 | 读取平台签发的完整Plan Artifact，max_bytes16000，成功；不是源码读取 |
| 模型第3轮 | 8个read_file，均offset0/max_bytes16000；原始响应先持久化，随后输出容量准入整批拒绝 |
| 第3轮执行状态 | 0个应用工具派发、0个读取预留、0个子批次、未进入OCI全批路径预检 |
| not_found | **not_observed**：本轮没有请求批准缺失路径，没有缺失回执，也没有向下一真实模型传递缺失结果 |
| Patch | 提案0、真实OCI应用0、snapshot/diff未产生 |
| 修改后Verify | not_run；Baseline不代替修改后Verify |
| 独立Review | not_run，未进入人工最终验收 |

第3轮请求路径按原顺序为：

1. `web/hooks/simulation/handle-tool-events.ts`
2. `web/hooks/simulation/types.ts`
3. `web/lib/agent-types.ts`
4. `web/hooks/simulation/process-event.ts`
5. `extension/src/transcript-parser.ts`
6. `extension/src/hook-server.ts`
7. `extension/src/protocol.ts`
8. `extension/test/transcript-parser.test.ts`

完整原始ID/参数字符串/顺序/finish reason/Usage见
[第三原始响应](artifacts/f941ae3218fd1994b1e683e8ab0a57d4cc788a6ed4700624bd1d05f0fb8feee3.json)，
以及[实际报告](result.json)的actual_requested_tool_batches/actual_read_batches。
前三个请求的4/1/8形态均保留；accepted_oversized_read_batches为空，不能说8-call已成功分批。
首次4个路径后来再次请求时max_bytes从4096变为16000，并非精确重复；不应假称缓存失效问题。

实际请求工具总数13：首轮4个read_file、第二轮1个read_artifact、第三轮8个read_file。
实际执行并保存成功回执5个，第三轮8个均未派发，未知结果0个；没有apply_patch或run_approved_checks请求。

## 首次异常与Checkpoint / Ledger

首次异常发生在第三个模型Job `ef6094b1-a931-43b8-9c43-dc2f620111dd`处理响应时，
`app/agent_v2/read_batches.py:159`因输出容量上界不足抛出`agent_read_batch_budget`。
这不是Provider网络错误或OCI工具执行失败；原始响应已先持久化。

| 顺序 | 真实记录 |
| --- | --- |
| 2082 | agent_v2_model_response，保存第三原始响应f941ae32… |
| 2083 | 模型响应证据字节计入Checkpoint，仍处于model状态 |
| 2084 | agent_v2_model_rejected，首次记录agent_read_batch_budget，retryable=false |
| 2085 | Checkpoint进入blocked，Hash657f9c2d…，pending_calls为空；无源码snapshot |
| 2086 | Progress Ledger第7版进入blocked，事件Hash2ee3729a…，blocker同上 |
| 2088 | automation_blocked，自动工作流停止；后续清理依赖工作区 |

Ledger中的baseline与plan已完成，investigate为blocked，implement/verify/review仍pending。
no_progress_streak=0、correction_issued=false、next_action=inspect_diagnostic，
因此此次停止不属于无进展守卫或协议纠偏耗尽。完整状态以[独立审计](final-audit.json)和
[事件索引](evidence-index.json)为准，没有将Ledger的部分完成等同于Coding完成。

## 已证实的阻塞原因

错误码`agent_read_batch_budget`，位置为
`app/agent_v2/read_batches.py:155-159`。现行保守输出容量公式：

```text
sum(6 × max_bytes + 8192)
= 8 × (6 × 16000 + 8192)
= 833536 bytes
remaining output_bytes = 464474
```

整批估算超出剩余额度369062字节，因此立即Session终态blocked。
原500000字节总上限不变；即使尚未用任何输出，5个max_bytes16000的请求上界520960也超过该总上限。
此处使用请求上限和最坏JSON转义/元数据上界，不先使用可信实际文件长度/读取范围，
也没有在该分支提供有限的缩小请求纠偏；这构成本轮新的实际阻塞。

这不是Tool Call数量超8、读取次数耗尽、Token耗尽或合法缺失文件再次失败。
平台按当前保守规则安全拒绝，没有绕过沙箱或假装未派发的调用已经执行。
路径OCI预检尚未发生，因此也不借此证明后续路径安全检查或missing-file真实覆盖通过。
[只读定量诊断](admission-diagnostic.json)与[可复算脚本](diagnose_admission.py)保存精确输入及断言。

## Usage、预算与历史

| 项目 | 本轮真实值 |
| --- | ---: |
| 模型请求 | 3 |
| Provider报告输入/输出Token | 24500 / 2665 |
| Runtime计入失败预留的输入/输出Token | 46008 / 4560 |
| 已计读取 | 5（4次read_file + 1次read_artifact） |
| 写入/检查/协议纠偏/Provider重试/代码修复 | 均0 |
| 已计输出字节 | 35526 / 500000 |
| 第3批未派发读取是否扣除8次 | 否；预算准入失败早于整批读取预留 |

失败回合的Token预留保留，没有因已收到响应或未执行工具而退款。
旧Plan13 reads12和全部历史/未知预留不变，Planning仍64/64及3次未知占用。
原24模型、64读/24写/6检查、Token200000/32000、1800秒和各2次修复/重试上限未修改。
剩余预算不构成恢复终态或另起付费轮次的授权；UI的resume_with_budget建议也不是用户许可。

本轮只追加新授权/执行和审计事实。全部旧行Hash、2017条旧任务事件前缀、9个旧Checkpoint、
Plan11/12/13及旧审批不变；没有新Plan15或模型配置版本。
任务事件2090、Artifact1640、模型启动91（88+3）、审批/执行授权12（11+1）、活动Job0。
原始28份JSON字节按原Hash另行导出，[索引](evidence-index.json)包含本轮73条事件。
二进制准备/测试日志Artifact仍按Hash保存在原业务存储，不转换成虚构文本。

## 交付与恢复边界

- [独立真实审计](final-audit.json)：1640个Artifact Hash/凭据、事件前缀、调用身份、原预算、源码与Review来源检查。
- [结构化真实结果](result.json)：显式列出实际读取、not_found、Patch/Verify/Review、阻塞阶段和可恢复性。
- [原始证据索引](evidence-index.json)与[原始JSON](artifacts/)：不删除失败或未知信息。
- [创建前状态](before-dispatch-state.json)、[终态状态](after-terminal-state.json)、[逐次观察](observations/)保留。
- [终态安全检查](terminal-checks.json)：252项源码/五角色镜像与安全配置、1640个Artifact、
  旧数据/预算/Checkpoint、四端健康、日志/保存文件凭据扫描、compileall与diff检查均通过；活动Job0。

HR4/HR5没有通过，仍有上述P0阻塞，不能报告端到端passed或通用能力完成。
本轮不修改冻结Runtime，不恢复Plan14、不扩大预算、不重用旧授权、不自动创建Plan15。
下一步需要用户单独指示最小离线修复：评估保守且准确的输出准入与有限纠偏，
保留整体安全预验证、输出预算、原始ID/顺序和4-call执行上限。
任何后续付费验收仍必须重新准备精确预览并获得明确授权。

后续只读复核补充：[容量分析](output-capacity-analysis.json)逐一校验冻结源码后，
证明8页内容合计68338字节；按实际文件长度保留六倍估算仍超出11090字节。
仅data规范JSON预测72123字节不含外层回执和进度等元数据，不能作为OCI容量准入证明。
[待授权的最小离线修复范围](offline-fix-scope.md)列出拟验证的容量证明、安全及兼容测试；
尚未实施、未新增Plan或模型调用。
