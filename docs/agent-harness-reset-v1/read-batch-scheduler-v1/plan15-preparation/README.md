# Plan15 冻结准备

用户本轮只授权冻结准备、必要只读检查、独立Plan/Binding和精确执行预览。
没有执行审批、真实模型调用、Coding、Verify、独立Review、Push、PR或评论。
本目录中的确认文本只是供用户之后单独确认的文件，不是已经取得的同意。

[精确执行预览](execution-preview.md)及[完整JSON](execution-preview.json)
包含实际Plan/Binding、源码/镜像、原授权范围、全部运行/Review/单次调用/沙箱上限，
以及真实Coding观察指标。所需确认文本见[confirmation-text.txt](confirmation-text.txt)。
初始未发送渲染保存在initial-preview.json；最终版仅补齐既有单次调用上限的说明，
预算没有增加。之后只使用execution-preview.json的最终Hash。

- Plan15：`626be813-5437-4ad3-8a24-e30d12c7b803`
- Plan Hash：`448b0a90cd2c9e4d3dfc14c94de8705ee5a7f2ec89487473397c18dc3e654740`
- Binding Hash：`2672bf166060bf9ec58eda0392a55ba0d141c02dad8fc9a4853a7d4b1d29e70b`
- 最终预览 Hash：`f7c000560f1e2e1bb9bad33ba20b21c81ceb39bbcfd97f843f1c4e745bc41a6e`
- Plan内容Hash仍为`7bcea4e1ed4eae3712343c18825c358acbc9ce785c390507732b9ee2a8d664ed`。

## 核验范围

[先决条件](prerequisite-check.json)：最新HEAD `6b335c173f2d76f0872d4776f3e5ab87334d5c5f`
的254份app文件与工作区、既有源码清单及五个部署角色逐项一致，集合Hash为
`31deb61be86711e475db5d75b3d08274be5b363b8f1487861897aed5586bfc95`。
四端健康，部署镜像/权限/环境/资源保持原样。本次没有修改或重部署生产代码。

[只读上游检查](upstream-check.json)确认Issue #107仍open、标题/正文/更新时间不变，
main仍`3489e17c48985b74fdb42cde122376b1eb12d8a7`。
[模型与归档检查](model-and-archive-check.json)保留原模型配置及实际冻结归档的Hash/大小。
8条写入路径、4条验证命令、原模型、运行/Review/安全预算均不变。

59项[新离线回归](offline-regression.xml)全部通过，未联网或请求真实模型。
归档的Plan12/13/14保存信封、最终全量/补充/OCI JUnit、原文Artifact全部重新核Hash；
OCI容量证书已用当前源码重算一致，输出计费逐项对应实际持久化字节。
本次没有重跑真实OCI；归档当前平台OCI证据不代表原生Linux amd64通过。
离线投影31027/32000、内联1024字节、10次Artifact分页与2次尾部读取仍是离线证据，
不能记作Plan15真实Coding观察。

## 仅追加准备记录

before-state/before-view是只读冻结快照，和容量修复验收后的业务状态一致。
同内容Plan15是Plan14的独立子版本，使用preflight-v3/capacity-v1/native-read-capacity-v9绑定。
必要的user_replan只追加状态；Plan11/12/13/14终态、旧行、10个Checkpoint、全部费用、
历史审批和3次未知Planning占用不变。准备操作明确标为maintenance，不算自主交付。

Plan总数14→15，任务事件2090→2094；仅plan_versions、
contribution_task_state_versions、workbench_events追加。
模型启动91、执行授权/审批各12、Artifact1640、活动Job0保持不变。
没有新增Job/Attempt/ReviewRun、预算分配或执行请求。

[最终只读审计](final-checks.json)核验Plan/Binding域Hash、旧行和事件链、Artifact、
实时revision/preflight、未发送请求schema、凭据/canary/日志、编译和空白检查。
助手脚本只属于准备材料；不改产品源码，不创建审批。不要重跑创建脚本以试图执行。

## 下一步

到此停止，等待用户单独明确确认最终预览。执行前必须重新核验全部冻结输入；
变化则停止重新预览。新调用不得使用Plan14审批，不恢复任何旧终态，不扩大预算。
真实Patch、最新源码4条Verify、独立Review和HR4/5仍未通过，原生Linux amd64门禁仍开放。
真实报告契约与not_run模板分别保存在acceptance-report-contract.json和acceptance-report-template.json。
