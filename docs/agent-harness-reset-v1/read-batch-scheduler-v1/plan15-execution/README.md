# Plan15 单次真实模型验收：blocked

用户本次明确批准精确预览`f7c000560f1e2e1bb9bad33ba20b21c81ceb39bbcfd97f843f1c4e745bc41a6e`，仅限既有Plan15和原预算的一次真实验收及独立Review。
本轮已消费该授权并安全停止：Baseline 4/4通过，真实模型15次，Patch提案/应用均0，修改后Verify和独立Review未运行。
停止原因`deepseek_completion_truncated`。未恢复旧终态，未提高任何上限，未另起付费执行，未Push/PR/评论或发布。

## 绑定与身份

- Plan15：`626be813-5437-4ad3-8a24-e30d12c7b803`
- Plan Hash：`448b0a90cd2c9e4d3dfc14c94de8705ee5a7f2ec89487473397c18dc3e654740`
- Binding Hash：`2672bf166060bf9ec58eda0392a55ba0d141c02dad8fc9a4853a7d4b1d29e70b`
- 人类本次独立授权Hash：`f27e76fc680eb7c2a4da8172dafc8ec6deb5f5b02cd8da02aeda0388fc41d26f`
- 服务端执行授权Hash：`c0cffa5486fb041c5f82f0788e0d539f8053c52ce7c3fb7338339f5fb456bd7a`
- Run：`103ce13d-e235-4e50-ad29-eac67fc6d587`
- Attempt：`f1174c5e-92ba-46a8-bf5c-f6fc052a2d2b`
- Session：`session-02147971b2194757d6b48d00358fbf5c`
- 254文件源码Hash：`31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95`

执行前再次核验原Issue/main/实际归档、8写入路径/4命令、模型/协议、沙箱、镜像、Revision、Preflight及预算一致。
本仓库HEAD8aa9051相对准备时点6b335c17的23文件差异仅为文档与准备材料；生产源码未变。
原预览及其Hash没有修改。独立授权、fsync派发意图和回执分别留存，POST仅一次，没有自动重发。

## 真实结果

| 项目 | 实际观察 |
| --- | --- |
| 新准备与Baseline | 4/4通过，新的依赖准备/对照检查证据 |
| 真实模型请求/原始响应 | 15/15，包含最后length响应 |
| 纯读整批 | 第6轮8条默认4096页、第12轮8条max_bytes16000页；均4+4、原序完成，子批之间模型请求0 |
| OCI容量证书 | 上界192802≤400899；227735≤237030，均在子批执行前持久化 |
| 计费读取 | 40=27条成功源码回执+13次read_artifact |
| 有效/重复源码读取 | 16次带来新字节；11次精确重复并复用；18次同偏移重读（含扩大max_bytes带来新尾部） |
| 工具观察/模型输入覆盖 | 去重源码70855/10624字节；逐请求实际投影均≤32000，最大31923 |
| 首次Patch提案/成功应用轮次 | null/null，没有提案或应用 |
| Artifact恢复 | 完整9431字节Plan可核Hash恢复；5个其他证据Artifact仅部分送达。没有调用source_page_artifact原文页，没有完整八文件恢复 |
| 第14轮协议拒绝 | 5个search_code+1个read_file，总6调用，非5–8个纯read_file；整批零执行，用原协议纠偏1次 |
| 第15轮终态 | finish_reason=length；输出4096 Token全为reasoning；无final_text/tool_calls；retryable=false |
| 修改后Verify/独立Review | not_run/not_run |
| not_found送达 | not_observed，本轮没有对应缺失路径读取，不补买覆盖轮次 |

实际Provider用量：输入135863、输出14939 Token（含缓存输入42112）。
失败保留预留后的账本：输入176505、输出17211，输出证据426518/500000字节；
协议纠偏1/2，Provider重试/代码修复/写入/检查均0。失败预留与实际用量分列，未退还历史费用。
最后截断不是整轮模型次数或500000字节耗尽；单次4096输出Token达到原上限即停止，没有提高Profile或单次额度。

完整Plan在请求间多次重新读取；记录中源码回执的可用字节显著多于实际Prompt内联送达。
上述事实不证明模型理解了源码，也不能将离线10页恢复方案当成本次真实恢复。
首次派生指标使用了错误的Provider原始usage键名；初版保存在coding-metrics-initial.json，最终coding-metrics.json已校正。
原始响应、原文页和事件始终保持原Hash。

## 证据和剩余门禁

- [执行前复核](pre-dispatch-check.json)、[独立人类授权](approval.json)、[唯一派发回执](dispatch-receipt.json)
- [原始证据索引](evidence-index.json)：457条新任务事件，161份可达JSON/text Artifact按原字节导出
- [真实来源/容量/计费审计](final-audit.json)：Hash `24b2bbcffff1e6f60cac824d86304ac639249ba3f5496076611448d5f8ca358c`
- [逐请求真实Coding指标](coding-metrics.json)：Hash `17e1e00422a37654c924a95b536f585872fde25062721b4a741302f6b1fdb897`
- [最终结果](result.json)、[终态安全检查](terminal-checks.json)

任务事件2094→2551，Artifact1640→1802；模型启动91→106、执行授权/审批各12→13，Plan仍15。
旧10个Checkpoint、旧终态/费用/审批和Planning64/64及3次未知占用保留，无活动Job。
生产源码与五角色镜像/权限不变，本次只产生沙箱只读观察和验收材料，不重部署生产。
HR4/5真实Patch/最新源码Verify/独立Review及原生Linux amd64仍未通过。
本轮授权不能复用；后续只可由用户另行指示，不自动修改预算、恢复终态或创建Plan16。
