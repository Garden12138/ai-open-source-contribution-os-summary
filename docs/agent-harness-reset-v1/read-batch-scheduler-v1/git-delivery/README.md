# Agent Harness 本地Git交付整理

2026-11-06，本次仅获本地Git整理与提交授权，**等待用户另行决定是否Push**。
不执行offline-fix-scope.md，不启动模型/真实验收，不恢复Plan14，不创建Plan15或PR。
Goal保持blocked；Git提交不代表HR4/HR5通过。

## 提交边界

按时间与职责拆分为三个本地Commit：

1. Plan13准备与真实失败记录：仅plan13-preparation与plan13-execution。
2. 已完成缺失文件语义实现：app源码、0045迁移、测试及missing-file-semantics-v1离线/OCI证据。
3. Plan14准备、真实终态诊断、待批准离线范围与当前任务文档：仅文档/证据/核验辅助脚本。

第三个Commit不包含新的输出容量修复。具体文件分组见[file-manifest.json](file-manifest.json)，
交付检查见[checks.json](checks.json)。Commit Hash在本次交付回报中列出，避免自引用Hash。

## 报告核对

Plan13保留合法缺失文件触发整批预检终止的原失败，未改为成功。
[Plan14最终报告](../plan14-execution/README.md)明确：真实模型3次、请求工具13个、
实际成功回执5个、第三轮8个未派发、agent_read_batch_budget、终态Checkpoint及第7版blocked Ledger。
首次异常定位到read_batches.py:159和事件2084，原始响应先在2082保存。
无Patch/修改后Verify/Review，not_found未观察到，HR4/HR5保持未通过。

[待批准修复范围](../plan14-execution/offline-fix-scope.md)已区分确定事实与待验证假设，
列出拟修改模块、完整离线/OCI验收、安全风险和控制。本次只整理文档，不实现该范围。
所有既有原始Provider信封、事件、Plan/Binding/预算/Checkpoint及历史报告Hash保持原样。

## 脱敏与排除

- 不提交agent-harness-reset-v1.zip和Plan13原始preflight-diagnostic.jsonl；它们留在本地。
- 4份含私人路径/邮箱的全量JUnit原件留在本地不提交。
  [隐私投影索引](../missing-file-semantics-v1/test-results-public/export-index.json)
  保留原件/导出Hash、原顺序和结果计数，Git仅收录脱敏副本。
- before/after-state.json是业务表行Hash与非敏感域记录审计，不是SQLite文件或原始数据库副本。
- 所有拟提交文件扫描凭据、私钥、常见Provider/AWS Key、个人主目录与邮箱。
  既有测试/文档中的凭据样式样例保持原样，并证明与原HEAD一致且新增行没有敏感值。
  扫描结果只记录文件名、类别与计数，不导出任何匹配值。

本次针对回归90项通过，compileall与git diff --check通过。
归档JUnit中的pytest失败回溯保留原始尾空格以维持证据Hash；.gitattributes仅对上述
test-results和test-results-public两个XML目录关闭blank-at-eol告警，其他文件的检查保持默认。
既有全量/OCI报告的Hash与e78f36d6/252项源码清单核验一致，本轮没有重跑付费模型或真实OCI验收。
当前源码Hash未改变；不重新部署服务或修改运行任务。
