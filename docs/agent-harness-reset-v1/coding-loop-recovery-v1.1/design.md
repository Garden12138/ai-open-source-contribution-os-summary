# Coding Loop Recovery v1.1

本轮范围是 Phase 5 / HR3-CLR 的本地离线修复。基础为核心提交
`b404a92326ca01efa7e64ab91ff9831b548c6562` 加
[review-closeout-v1](../coding-loop-recovery-v1/review-closeout-v1/README.md)。
Plan15 的逐轮事实与根因仍见[原诊断](../coding-loop-recovery-v1/diagnosis.md)；
本轮不重写真实模型轨迹，不推断隐藏思维，也不执行历史 Plan。

## 6+8+1 的精确语义

计数来自当前 Journal 的 `model_started`，成功 Patch 后重新计数。
成功必须同时满足可信 orchestrator 回执、内容寻址校验、OCI `docker`
后端、`succeeded`、无 failure、输入与输出源码 Hash 不同。
Patch 提案、哈希冲突、失败或相同源码均不重置计数。

| 时点 | 行为 |
| --- | --- |
| 6 次未成功 Patch 后 | 保留原纠偏，在下一 Prompt 指向依据源码实施或明确阻碍 |
| 8 次后、调用 9 前 | 默认停止；只检查第 8 次已确认响应及其接受的工具结果 |
| 第 8 次有新证据 | 必须是当前工作区、允许修改或冻结 Plan 指定检查路径的真实新增源码区间；减去此前已观察及已确认送达的区间 |
| 调用 9 准入 | 对实际投影后的 Prompt 逐字节验证该源码片段及完整来源信息，检查 32000 输入估算上限和剩余原预算 |
| 调用 9 派发前 | 在原 `model_started` Prompt 中记录机会版本、authorization_hash、原始 receipt、产生它的 job、候选区间和实际内联区间；与原计费及 checkpoint 一起提交 |
| 调用 9 后无成功 Patch | 在调用 10 前停止；读到更多源码、协议纠偏或返回未知结果都不能再延长 |
| 调用 9 成功 Patch | 按原流程进入最新源码 Verify、独立 Review、人工验收；不跳过任何门禁 |

候选要求第 8 次真的产生新字节。仅有 Artifact 索引、文件 Hash、旧数据的首次投影、
重复读取、平台缓存重放、未知模型送达都不够。平台不为获得机会而新读文件。
若多个页符合，按允许写入优先、路径与区间确定性排序选择一页；投影可能只内联其中
有界片段，`inline_ranges` 明确记录实际片段，绝不宣称整页或整个文件已送达。
剩余可信页仍可通过原回执与原始页恢复。

机会在一次 execution authorization 下最多一次。恢复时扫描该任务现有 Session
的原始 `model_started` 记录，同一授权的修复 Session 也不能再次取得机会。
即使第 9 次响应丢失，其调用和预留也已消耗；未知执行继续按原规则安全停止。
没有新增表、迁移或可以与预算独立漂移的可变计数器。

## 送达与来源

保留上一轮共享 `model_receipts.confirmed_prompts()`，重复读取警告和 Coding
源码送达共用同一身份语义：start/response 各自唯一、actor 正确、job 一致、顺序正确、
Prompt 与响应 Artifact Hash 正确、事件/响应/Prompt 的 Binding 与实际 TaskSpec 一致。
没有 response 的 start 不算已见。身份冲突安全拒绝，绝不据此恢复未知执行。

候选源码先校验原回执 Hash 和原始页 SHA256，再校验当前 workspace Hash、文件 Hash、
offset、total_bytes、next_offset。准入时再次比对 Prompt 中的路径、Hash、页 Artifact、
分页元数据、实际 UTF-8 字节和 `inline_ranges`。只有 Hash 或引用没有源码正文不能准入。
单纯构造 Prompt 不计为送达；收到匹配响应后才计入有效送达范围。

## 最多四文件的公平工作集

只从真实工具已观察且属于当前工作区的页选取，不额外访问仓库。每文件选一个最佳页，
模块内允许写入路径优先于 Plan 检查路径，再优先尚未送达的范围；模块间公平选择允许
持有可信检查证据的模块取得位置。无关的已观察文件不能挤占
已有相关文件的槽位。第 9 次所需证据首先占一个位置，其余位置遵循同一选择规则。

模块由相关路径目录的共同父目录及其下一层目录推导；根文件分组为 `.`。
没有硬编码 web、extension 或其他产品模块。每轮先避免同模块占满槽位，再以历史
确认 Prompt 中各模块的工作集服务次数选择较少服务的模块，最后以页优先级和路径
确定性破同分。服务次数通过原 receipt 反查原始页，重复读取换新 receipt 不会清零。
单模块可用满四槽，多于四模块在后续 Prompt 轮换，不保证单轮全部展示。

所有选择依据均来自不可变事件、TaskSpec、Plan 和 receipt，可跨 Worker 重启重算。
新增 `module`、`priority` 仅解释选择；文件内容始终是原始源码的字节子串。
保持当前工作区 Hash、文件 Hash、receipt_artifact、source_page_artifact、offset、
next_offset；保留原始历史索引和分页恢复路径。对未观察文件不会编造摘要或保留空槽。

新增的逐页来源字段会占用输入空间。投影去掉最近决策中与反馈/历史索引重复的工具
名称和路径，保留最后 outcome/reply；完整工具顺序、参数、回执仍在原 Artifact。
保留全部冻结任务条件、授权、失败与验证事实，不扩大 Prompt 上限。

## 预算与重新授权

deepseek-v4-pro 的冻结 Profile `reasoning_effort=high`、Profile max_tokens=16384
均不变；Coding 单次真实输出仍限 4096，reasoning 与最终工具调用共同竞争该额度。
本轮没有把 reasoning 降为其他级别，也没有增加输出。截断仍保存真实 Provider usage
并安全停止，不将未观测到的工具调用或 Patch 视作成功。

相对第 8 次后立即停止，条件机会最多多 **1 次实际模型调用**。其最大增量为输入
32000、输出4096（分别还受当时剩余预算限制），单 Job 仍受原超时约束，且全程仍受
1800 秒总限额。额外读取/写入也只从原额度扣除。价格未冻结在本轮材料中，因此不
编造金额；按冻结计费单价，上界为 `32000 × 输入单价 + 4096 × 输出单价`，单价应按
每 Token 单位换算，最终费用以 Provider 回执为准。该机会可能消耗本可用于后续 Verify/
Review 前决策的剩余额度，不保证足够完成任务。

总限额保持：模型24、读64、写24、检查6、协议纠偏2、Provider重试2、代码修复2、
输入200000、输出32000、工具/证据输出500000字节、1800秒。独立 Review 继续使用
原独立门禁与限额：输入1800000字节、最多3次 Job 尝试、300秒、单次输出16384。
本轮并未执行真实独立模型 Review。

源码、Native Prompt 协议和工具 Schema Hash 都改变。即使 Profile Hash 不变，
本设计也与原 Plan15 精确执行授权不兼容。未来必须重新冻结源码/镜像/协议/策略/
预算/Issue/base/revision/preflight，创建新的独立绑定并获得独立精确授权。
当前任务不创建 Plan16、不恢复 Plan15、不申请执行审批。
