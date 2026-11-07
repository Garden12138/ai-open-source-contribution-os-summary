# Coding Loop Recovery v1.1 本地 Git 交付

用户在离线验收完成后另行授权本地 Commit，供独立审查。授权覆盖上一轮未提交的
可信警告送达修复，以及依赖它的 v1.1 条件停止、公平工作集、测试与脱敏材料。
本次未授权或执行 Push、部署、付费 Provider、Plan16、恢复 Plan15、历史预算修改、
PR 或外部评论。

源码与测试 Commit：`bd3f8927bfed1a280b8d8ae15cdd446d30357917`。
父 Commit：`737951fa34d9a204b3290419d2705a5f39cbfc76`。
文档 Commit 是包含本说明和 [Git交付核验清单](git-delivery-check.json) 的后继 Commit，
其 SHA 在最终交付消息中报告。

## 源码与测试文件

| 类型 | 路径 |
| --- | --- |
| 生产源码 | app/agent_v2/coding_context.py |
| 生产源码 | app/agent_v2/loop.py |
| 生产源码 | app/agent_v2/model_receipts.py |
| 生产源码 | app/agent_v2/native_tools.py |
| 生产源码 | app/agent_v2/read_progress.py |
| 测试 | tests/test_agent_v2_loop.py |
| 测试 | tests/test_coding_loop_recovery.py |
| 测试 | tests/test_coding_delivery_boundaries.py |
| 测试 | tests/test_coding_recovery_v11.py |
| 测试 | tests/test_coding_recovery_v11_oci.py |
| 测试 | tests/test_coding_stop_boundaries.py |
| 测试 | tests/test_coding_stop_boundaries_oci.py |
| 测试 | tests/test_coding_working_set_boundaries.py |

生产文件共5个，测试共8个。共享 start/response/Prompt/Binding 校验已纳入源码 Commit，
并非只提交 v1.1 的3个增量生产模块。没有新依赖、迁移或模型 Profile 改动。

已提交 Git tree 的255个生产文件集合 Hash：
`627a3689ca6647f8747796469f7e93fddff23a8445557066a7cae061e3346d47`。

## 文档与证据范围

仅暂存 `docs/tasks.md`、
`docs/agent-harness-reset-v1/coding-loop-recovery-v1/review-closeout-v1/` 和本目录中的
明确逐文件清单，排除缓存与编译产物；完整路径和摘要见 Git交付核验清单。
包含原 README、设计/策略说明、完整和增量 Diff、验证报告、脱敏 JUnit、兼容性与
历史预算证据，以及7个保存信封回归所需的201个内容寻址 Artifact。

没有提交原始数据库、凭据、未脱敏 Provider 数据、运行日志或本次范围外的 ZIP/XML/
诊断文件。原始源码页保持准确字节及 SHA256；保存响应只有既有的脱敏调用信封。

## 本次重新核验

- 工作区源码与测试生成的完整 Patch 与 [code.diff](code.diff) 逐字节一致，逆向检查通过。
  从核心提交 `b404a92326ca01efa7e64ab91ff9831b548c6562` 应用上一轮 closeout Diff，
  重建255文件旧快照并核验原 Hash；再应用 [增量 Diff](delta-from-review-closeout.diff)，
  还原后的生产源码与全部本次测试逐字节匹配工作区。
- 核对归档 XML 的 Hash、用例数、结果及分片覆盖：定向50通过；默认套件2099通过/
  83跳过/0失败/0错误。真实离线 OCI 为28项，另2项纯单元检查，两组OCI报告合计30通过。
  本次 Git 整理没有重跑全量、OCI 或真实模型，不把归档复核描述为新的运行。
- 保存信封7份的子批次仍为4+2、4+1、4+1、4+4、4+4、4+4、4+4；201个导出
  Artifact 的实际 SHA256 均与文件名一致。容量证明仍为68338字节真实源码页。
- 核对既有 query_only 前后报告及 Plan11–15 事件链、终态、审批、预算与未知占用的
  归档一致性；本次没有访问或变更线上数据库、Plan、审批、Job 或预算。
- 重新执行 compileall app、工作区/新文件及暂存区 diff check；每个暂存 blob 与已核验
  文件逐字节比对。没有使用 git add -A，没有关闭或跳过 Git hooks。
- 两份内容寻址原始源码页带有原始末尾空行，初次暂存空白检查据此拒绝；在保存信封
  目录新增仅匹配这两份页的局部空白属性，保持原始字节与Hash，再执行完整检查。
  未修改生产源码的空白规则，也未修改旧证据或原属性文件。
- 扫描全部交付源码、测试、两处审查文档、完整 Diff、本地已配置凭据及编码变体。
  唯一保留的凭据形状字符串是 `test_agent_v2_loop.py` 中既有的明确 canary 测试夹具，
  已与核心提交原字节核对；真实凭据或其编码变体无匹配，原始 Provider/持久化材料
  不允许出现该 canary。扫描不输出凭据值。

## 冻结时点与后续限制

原 README、source-manifest、delivery-check、验证报告及上一轮 closeout 材料保存离线
验收时点。它们的旧 HEAD、“尚未 Commit”或“本轮未 Commit”等字段是此前事实；本次
另获的 Git 授权及结果记录在本说明，不回写原 Hash 或伪造原验收发生时点。

本次 Commit 只改变本地版本记录，不构成新执行授权。仍为代码审查 Go、部署与真实
模型验收 No-Go；Linux amd64 尚未验证。未来真实执行必须重新冻结并创建独立绑定、
获得精确授权，原 Plan15 授权不可用于新版。

条件机会最多增加1次实际调用，输入最多32000、输出最多4096，均从原24次、输入
200000、输出32000、证据500000字节、1800秒等总预算扣除，不分配新容量。高 reasoning
仍可能耗尽4096而没有工具调用；第7轮保存投影少1024字节的上下文风险保持明示。
本地提交完成后停止，等待独立审查，不自动 Push。
