# Plan14 容量与输入投影：离线修复设计

本轮只修复本地 Harness；没有创建任务/Plan、使用付费模型、恢复 Plan14 或改变历史预算。
原 Plan13、Plan14 的失败报告和全部冻结 Artifact 保持原样。

## 已证实根因

Plan14 第三响应 `f941ae3218fd1994b1e683e8ab0a57d4cc788a6ed4700624bd1d05f0fb8feee3`
包含八个原始 `read_file(offset=0,max_bytes=16000)`。
旧准入固定计算 `8*(6*16000+8192)=833536`，大于已扣账后的剩余 `464474`。
因此该原始批次没有应用读取、子批次或读取预留，Session 按旧策略终止。
保存的完整冻结源码逐文件 SHA256 与真实 OCI 读取均证实：八页原文为 68338 字节。
72123 字节是原始 `data` 的规范 JSON，不是完整 ToolResult 或准入上界。

独立失败回归还发现输出记账口径不一致：先计量失败结果，随后修改
`failure.remaining_budget`，最终持久化对象较原计量增加 190 字节。
此前 batch/proof/aggregate 亦未纳入明确的输出计费契约。
[修复前回归](initial-regression.json)保留两项真实失败观测，未改写 Plan14 事实。

## 严格容量证明

新预检 `native-read-batch-preflight-v3` 沿用同一 OCI 文件系统扫描、路径/链接检查、
批准写入范围、预期文件 Hash、来源、页边界及冻结 inventory Hash。
整个原始批次全部通过这些检查后，OCI 对每个原参数生成实际 UTF-8 页，使用与平台相同的
`ensure_ascii=False,sort_keys=True,separators=(',',':')` 规范序列化。
返回 `page_bytes`、完整观察 `data_json_bytes` 和 `data_hash`；不把源正文装进预检。
缺失文件仍须由 OCI 证明为批准新建路径，观察不带虚构内容或 content_hash。

平台先验证原始 Provider ID/名称/参数/顺序、来源、8-call 上限、当前绑定与原预算，
持久化不可变原批次并预留全部读取，不退还已有消耗。
只给预检与终止记录预留结构性空间，然后运行整个批次的 OCI 安全预检。
平台根据 OCI 实际序列化大小构造 `native-read-capacity-v1`：

- 外层完整 ToolResult，保留实际调用 ID，并对 Hash/Artifact 使用最大固定长度。
- 完整 ReadProgressNote，包括重复次数、前序回执、警告证据、可见范围和说明文本。
- 源码页 Artifact 引用；另预留最多 16000 字节原文页持久化。
- 每调用额外 16384 字节：足以容纳原文页；异常分支则覆盖受限的观察结果与 8192 字节诊断。
  成功原文页分支与异常观察分支互斥；超大/无法证明的异常只记录真实 Hash、大小和明确 unavailable 状态。
- 两个子批次结果、整批汇总、已执行/未知/未派发前缀停止记录，以及证书本身。

容量证书成功持久化后才允许开始最多四调用的子批次。每个真实回执再次比对
OCI 的观察承诺和完整结果上界；冲突保存可容纳的实际观察，停止剩余调用。
真实不足仍返回 `agent_read_batch_budget`，零应用调用、保留读取预留与预检成本。
本轮未增加自动纠偏、无限重试或新模型 Decision。

## 输出计费契约

`output_bytes` 计量下列**最终不可变字节**：安全 Provider 响应信封、完整 ToolResult、
可容纳的异常原始结果、原批次、OCI 预检、容量证书、子批次结果、整批汇总和停止结果。
新签发原文页 Artifact 以 UTF-8 原始字节计量，同一会话/Hash 只计一次。
每项新输出有 `agent_v2_output_charge`，SQL guard 校验 Artifact 实际大小、角色、会话及平台 actor。
ToolResult 与相关批次记录的费用、Journal 和 Checkpoint 在同一事务提交。

Journal、Checkpoint、Progress Ledger 的控制索引单独作为受调用/字段上限约束的审计存储，
只含证据引用；模型输入的正文复制属于输入预算。它们不被伪称为输出正文，也不用于绕过输出上限。
容量预留不是实际费用，不会提前扣整份上界或事后退还读取。
失败回执中的 remaining_budget 明确定义为**持久化该回执之前**的快照；
最终 Checkpoint 和下一轮 Prompt 是持久化后的当前预算，避免递归修改自身字节数。
停止/已观察结果可以在时间耗尽后提交真相；新工具派发仍受原时间门禁约束。

## 输入投影的第二个根因与最小改进

`NATIVE_INPUT_LIMIT=32000` 覆盖 Prompt、函数定义、指令和原有开销。
68338 字节原文无法同时完整内联。开发期诊断还显示，完整 Issue/Plan 优先占位可使八页正文
全部内联为零；通过整份 JSON ToolResult 再分页会发生二次转义和二次截断，逐文件需 74 次恢复。
该开发快照没有冻结完整 app Hash，不作为最终验收版本。

最小改进仅使用现有 `read_artifact` 和字节分页：

1. 完整 Plan 只有与当前反馈的有用正文空间一起可容纳时才内联；否则保留已签发的完整任务 Artifact。
   不假定模型能记住前一请求；完整 Issue/Plan 始终可恢复。
2. 真实 `read_file` 完整回执继续保存全部正文，同时签发其**原始返回页**的内容寻址 Artifact。
   Artifact 偏移从零开始，与文件原 offset 的对应关系由完整回执证明；页外文件尾部仍用 read_file。
   仅平台已观察、已校验、已计费的字节可签发，不能访问任意 Artifact 或新增路径。
3. 投影不再重复八次固定的进度说明，所有计数、范围、前序/警告引用仍内联。
   完整说明保存在原回执；进度守卫只接受完整 Note 或仅省略固定 instruction 的精确投影。
4. 超过八项历史时，先签发完整历史索引，才从其窗口引用中省去重复的源码 Hash。
   短历史继续内联全部 Hash；每个完整 ToolResult 始终保留全部来源。

未新增工具、扩大上下文/Token 或缩小原 max_bytes。投影版本为 `native-read-capacity-v9`。
[覆盖报告](model-visible-source-coverage.json)重放真实冻结 Issue/Plan，使用生产投影器及生产
Ledger reducer 的内存影子；历史响应费用不退还，新工具证据费用另外加到假设性预算中。
命名空间、输入 Hash 与聚合对象属于离线诊断，不能批准或恢复真实 Plan14。
另有实际持久化 Scheduler 生成的 Prompt，避免把这份影子重建冒充真实已发送请求。

## 版本、恢复与安全边界

迁移 `0046_read_capacity` 只新增三条证据 guard，不修改旧表数据。
v1/v2 预检、旧 Journal/Checkpoint 仍可读取；旧证明不能签发新容量证书。
新批次的重启必须继续使用其原始批次、已提交证书、Checkpoint 与幂等键。
已完成调用不会重派发；已开始但结果未知的调用不被伪称为未执行，也不会自动重试。
终态 Plan11/12/13/14 均不得恢复；协议或源码变化必须重新冻结独立绑定并另获授权。

恢复部署时先停止写入者，保留数据库/Artifact/代码的匹配备份。
如需回退，排空新会话并恢复匹配快照；不得删减事件、倒退历史预算或就地编辑旧迁移。
本轮不改变 write_paths、命令、网络、沙箱策略/Runner、执行子批次上限及任何预算。
