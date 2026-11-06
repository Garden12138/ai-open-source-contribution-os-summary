# Plan13 真实模型验收结果：安全停止，目标未完成

仅消费精确预览 `c628ea479f2647a18c3188575ed3bf7fbc17d611a5c58a7c55a43519fdfdf42c` 的一次执行授权。
结果为 **blocked / source_unavailable**，未完成Coding、修改后Verify或独立Review；HR4/5均未通过。
没有自动重试、另起付费轮次、新Plan、Push、PR、评论或发布。

| 标识 | 实际值 |
| --- | --- |
| Plan13 | `b3c5acdd-bb80-461f-8dd9-5af5ca687280` |
| Plan Hash | `1831436b3da8904d514205c2c877b9019c44f2b88aaf2cb31f05ce4b16583865` |
| Binding Hash | `bcbf07e445030ca723db34c272feecfb05776de94496cc7fe86a3f0e434ccbe2` |
| 本轮执行授权 Hash | `7918e7cd6332a46f8cbe1977fab51150a7551c7a325def3326aa04f240759108` |
| Run ID | `1e4051f5-a286-489f-9075-6e47d85a7bf4` |
| Attempt ID | `e7d2a6ae-4a93-4af7-a790-e066d9c5fd06` |
| Session ID | `session-11901711068ace4c72aa1410a8847453` |
| 终态 Checkpoint | `abfebe97461aa4c4acecb446953902ba6d155dd5361b1a4ff1bdf7fe582e4f3c` |
| 生产源码 Hash | `9601fd8b2ca3c1e7b605633ba7a5e67491591d5a61ff05bf1de3590cca85860d` |

## 实际阶段与失败位置

独立依赖准备完成，新Baseline **4/4通过**，见
[真实Baseline原始Artifact](artifacts/a1af0ca2b051574914f91c07839f674f212ef06e61cd2a73df06586c16b551fa.json)。
随后完成2次真实DeepSeek调用：首轮提出4个read_file并全部成功；第二轮提出8个read_file。
原始信封、Provider ID、参数字符串、顺序、finish reason及Usage均已持久化。
这次8-call提案通过数量/类型/Schema/来源/预算准入，原始批次与预定4+4子批次已保存。

最后一条请求将计划新增的`web/hooks/simulation/handle-tool-events.test.ts`当成已有文件读取。
该路径属于允许写入的8条路径，但不存在于冻结基线库存；整批OCI预检返回`source_unavailable`。
**该8-call批次未开始任何执行子批次，8个应用读取全部未派发，未知结果0。**
此前另一原始批次已完成的4个真实读取回执保持原样，不能写成“本轮整批均未执行”。

本轮观察到了真实超量纯读提案与整批预检拒绝，**未观察到真实成功的4+4执行**。
没有发生旧版调用数量拒绝；也不能用此前离线重放结果冒充这次真实分批成功。
Patch数量0，修改后Verify与Independent Review均未运行，未进入最终人工验收。

## 真实用量与安全边界

| 项目 | 本轮实际值 |
| --- | --- |
| 模型调用 | 2 |
| Provider报告输入 / 输出Token | 15882 / 1164 |
| 已计费读取 | 12 = 成功4 + 未执行批次预留8 |
| 读取预留退还 | 0 |
| 本轮原生写入 / 检查调用 | 0 / 0；Baseline四项属于独立准备阶段 |
| 协议纠偏 / Provider重试 / 代码修复 | 0 / 0 / 0 |
| 工具/响应证据输出已用字节 | 24369 |
| 当前活动Job | 0 |

输出Token1164已包括606个reasoning tokens，不重复相加。两次请求均取得真实响应，
报告用量与Checkpoint输入15882/输出1164相同；本轮没有失败模型预留。
原24模型调用、200000/32000输入输出Token、64读取/24写入/6检查、1800秒等上限均未扩大。

Plan11/12及所有先前Checkpoint、数据库旧行、审批、事件前缀与预算占用逐一保持不变。
Planning仍已用64/64调用及三个未知占用，未增加或退还。
本轮只增加一次执行授权：累计审批11、累计模型启动88（原86+2）；Plan仍13。
2013条任务事件与1610个注册Artifact通过Hash链/凭据校验；五角色源码、镜像、资源/挂载/
网络/凭据隔离配置保持原样，四端健康。

## 可追溯证据

- [完整结果JSON](result.json)：实际标识、预算、阶段、失败与安全状态。
- [完整只读审计](final-audit.json)：模型Prompt引用、原始Tool ID与结果关联、批次及旧终态校验。
- [原始证据索引](evidence-index.json)：67条新增事件及27份按原Hash导出的JSON原始字节。
- [预检诊断](preflight-diagnostic.jsonl)：全部8条路径与冻结库存对照，最后一条不存在。
- [终态安全检查](terminal-checks.json)：预算、旧记录、服务健康与无额外执行。
- [一次性授权](approval.json)、[派发意图](dispatch-intent.json)、[单次派发回执](dispatch-receipt.json)。

原始响应分别为
[首轮4-call](artifacts/f1b1e83c7c407825a900981d3d9193998a688593b20a9aa545aed150365f2577.json)和
[第二轮8-call](artifacts/c111a4e2a155342f6ebcb3e13f731ef5db9bdbf44b1e588f8c69764d93a9d092.json)；
[整批预检](artifacts/fca28a42b142bea736c9bbe33575249ac3da23f5f1b094850add0a0997661042.json)与
[停止回执](artifacts/94fac848c60f384f429700c0a4dfa3bced420cfcb19079b310d3d655e98d1604.json)保存未执行/未知的准确范围。
原始Prompt、Tool Result及Baseline等JSON按内容地址留存，没有保存模型私有推理正文或凭据。

本轮授权已用于上述实际执行并终态停止，不能据此恢复终态或再次派发付费任务。
后续本地修复及任何新真实执行均需按其范围获得新的指令/精确授权；本轮没有自动开展这些工作。
原生Linux amd64门禁仍开放，已有离线2015通过/69跳过和OCI16通过均属于原冻结源码历史验收，
没有在本轮重跑，也没有被用来覆盖此次真实失败。
