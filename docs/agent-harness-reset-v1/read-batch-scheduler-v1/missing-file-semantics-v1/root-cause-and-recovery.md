# Plan13 合法缺失文件语义：根因与最小修复

取证针对已终止的Plan13，不恢复会话、不付费复现。
[事实JSON](forensics.json)核对原始安全响应的内容地址、审计Hash、原始ID、原始参数字符串和顺序；
[冻结库存证据](frozen-context-facts.json)来自原Linux业务容器的query_only读取。
取证基线HEADe5cc92a，生产源码Hash9601fd8b，旧检查点和12次读取用量保持原样。

原始响应Artifact：`c111a4e2a155342f6ebcb3e13f731ef5db9bdbf44b1e588f8c69764d93a9d092`，模型Job `20ba5800-0fea-4203-a601-e81fe16a69bd`。
全部8个调用均为`read_file`；均未提供expected_hash或max_bytes，规范化值分别为null、4096。
未显式提供offset时规范化为0。没有推测或补造历史参数，原始JSON字符串保存在事实索引。

| 顺序（1起） | 原始Provider ID | 路径 | offset |
| --- | --- | --- | --- |
| 1 | `call_00_6hMp94fXeKGfACUXgD1f9064` | `extension/src/transcript-parser.ts` | 4096 |
| 2 | `call_01_4TOIIsQcp3SpbcoIEcfs2260` | `extension/src/hook-server.ts` | 4096 |
| 3 | `call_02_YsOKBbWAMP123HT0Ptm92188` | `web/hooks/simulation/handle-tool-events.ts` | 4096 |
| 4 | `call_03_vxT0cgV1eqr4Zoi8DFuc2420` | `web/hooks/simulation/types.ts` | 4096 |
| 5 | `call_04_E82EWYCSob3AzE6LFA8u9419` | `web/lib/agent-types.ts` | 0 |
| 6 | `call_05_hPPCUvQPlTy4AuFib9u62084` | `web/hooks/simulation/process-event.ts` | 0 |
| 7 | `call_06_BpDUcfzQN20xYmFHc7WH0961` | `extension/src/protocol.ts` | 0 |
| 8 | `call_07_96KBUSYjqUV0obt3k3gJ7731` | `web/hooks/simulation/handle-tool-events.test.ts` | 0 |

第8个路径不在冻结库存，但位于Plan13的原8条批准写入路径中。
Plan Record Hash为1831436b，Content Hash为7bcea4e1，Binding Hash为bcbf07e4；
完整值与原批准范围见[Plan13预览](../plan13-preparation/execution-preview.json)。
文件未来允许新建，不代表已经存在，也不意味着应返回一个虚构的空文件。

## 根因

1. 旧SOURCE_TOOL_SCRIPT在整批预检循环中直接open每个路径，FileNotFoundError被泛化为
   source_unavailable。不存在、权限/IO失败与未知沙箱故障没有区分。
2. v1预检只有带content_hash/total_bytes的存在文件证明，无法表示无内容的可信缺失。
3. AgentToolWorker把全部预检失败按security处理，并且把任意非succeeded的批次调用阻塞到Session。
   即便仅增加一个普通失败回执，也不能让有合法缺失的原批次安全完成。
4. 模型Prompt有write_paths和Plan，但没有从已有冻结库存投影“存在/允许新建”的明确分类。
   原库存其实已经记录缺失，未必要引入新的、不可信文件状态来源。

旧OCI预检Artifact为fca28a42，停止回执为94fac848：该8-call批次应用执行0、未知0，
先前另一批4次真实读取仍然有效。上述事实不改写为“整轮零执行”。

## 新语义与边界

- 仅经真实OCI路径/符号链接/库存检查确认缺失、位于原write_paths且未声明expected_hash的读取，
  可返回status=not_found。data使用source-file-observation-v1，包含路径、reason_code、批准新建标志、
  Binding Hash及实际inventory_hash。它没有text、文件大小、content_hash或文件续页offset。
- source_not_found表示明确的缺失；未批准创建的缺失仍拒绝，不增加写入权限。
  source_unavailable表示未知/权限/IO失败；source_hash_conflict包括“声明旧Hash但文件不存在”；
  path_denied、symlink_denied和真正安全错误保持独立原因与阻断。未知错误不能降级成缺失。
- 原批次5–8、执行子批次最多4。整批所有调用都先验证Schema/来源/预算，再完成OCI安全预检；
  合法缺失成为一个有原始ID/参数关联的调查结果，其余有效读取继续4+4执行。
  一个失败的安全预检仍使全部应用调用零执行；中途真实失败仍保留实际前缀/未知/未执行范围。
- not_found结果必须与v2预检对应位置一致；工具/参数/批准范围/Binding/source不一致，保存观察并阻断。
  普通单条读取也校验同一缺失证明。已有exact apply_patch授权可用expected_hash=null、old_text为空创建，
  Registry和OCI均再次检查写入路径，未添加新的工具或命令。
- 进度账本把新缺失记为一次源Hash绑定的调查证据，不是读取成功，更不是Coding完成。
  精确重复可复用同源回执，重复缺失不产生新进展；源码Hash变化后不复用旧缺失。
- 模型source_files分类来自冻结context artifact与当前已注册snapshot，并引用其Hash；
  顶层write_paths仍是唯一权限范围。回执not_found完整保留，不被字节投影删去。
  8个回执加分类信息触及原32000输入边界时，完整Plan保持为平台已发放、可分页读取的Artifact；
  不增加输入上限，不删除Hash、缺失状态、失败或真实字节范围，也不放弃原无进展校验。

## 协议、持久化与恢复

新预检为native-read-batch-preflight-v2，present/not_found采用严格分支Schema；
旧v1仅表示存在文件，仍可解析。原批次Artifact格式保留v1，旧原始调用身份链保持可读。
新Prompt协议为native-source-observations-v8，策略变化纳入Native Transport Hash；旧批准不能启动新协议。
旧ToolResult三种状态、旧Journal/Checkpoint/Progress版本都保留，新增加的not_found不重解释历史失败。

前向迁移0045_source_file_observations只增加缺失回执准入约束，核验orchestrator、read_file、
空expected_hash、已注册Artifact、批准Plan写入路径和真实started/reused来源；不修改旧表/旧行。
升级测试覆盖0044→0045、旧链Hash保持及伪造回执拒绝。

部署前确认无活动Job，在原Linux容器做SQLite online backup，核对全部业务表Hash及旧检查点/预算。
保留原五角色隔离、镜像Runner、资源、挂载、网络和凭据配置。升级只追加0045迁移记录。
恢复时先排空写入者、保存所有新缺失/创建/检查点/用量证据，再恢复配套数据库、Artifact和代码备份；
不能删除事件、退还旧Plan13的12次读取或失败预留，也不能重发付费调用重建证据。
不能让旧代码读取新状态后继续执行，更不能自动恢复Plan11/12/13终态。

本轮仅离线重放及OCI验证，不创建Plan14/付费任务，不Push、PR或评论。
离线门禁满足后只生成文件形式的新真实验收候选预览，等待新的方案绑定和执行授权。
