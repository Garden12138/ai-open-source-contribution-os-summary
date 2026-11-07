# 离线验证范围与复现

所有模型决策都是确定性替身或保存的脱敏响应信封；运行下列测试不需要真实模型凭据。
默认回归不打开真实 OCI 开关。真实 OCI 另行使用已缓存的固定镜像，目标代码仅在
受限容器执行。没有运行部署脚本、真实 Provider Worker 或业务执行 API。

定向测试：

```sh
CONTRIBOS_CODING_BOUNDARY_EVIDENCE=/tmp/coding-v11-scenarios .venv/bin/python -m pytest -q tests/test_coding_recovery_v11.py tests/test_coding_stop_boundaries.py tests/test_coding_working_set_boundaries.py tests/test_coding_loop_recovery.py tests/test_coding_delivery_boundaries.py --junitxml=/tmp/coding-v11-focused-final.xml
```

全量测试按文件确定性分成互不重叠的两组。分片脚本先核对当前 255 文件 manifest，
覆盖收集到的全部默认用例，并在结束时再次检查源码未变：

```sh
PYTHONPATH=. .venv/bin/python docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/run_regression.py 0
PYTHONPATH=. .venv/bin/python docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/run_regression.py 1
```

真实 OCI 的固定前置输入：

- Runner `sha256:923d81e41243ce593159fc395de63b548f19cac29df678cf6650d4aa5e9b02e6`。
- 冻结上游归档 `d269dc46fd82a9ae5d443b95aa389bb6637856f28f5e5cb7da0d0392384b2cdb`；
  路径通过 `CONTRIBOS_PLAN13_ARCHIVE_PATH` 指定。缺少输入必须失败或跳过，不能下载替代品冒充。
- `CONTRIBOS_RUN_DOCKER_ACCEPTANCE=1`，`CONTRIBOS_SANDBOX_RUNNER_IMAGE` 指向上述 Digest。
- 证据导出可设置 `CONTRIBOS_CAPACITY_OCI_EVIDENCE` 与 `CONTRIBOS_CODING_BOUNDARY_EVIDENCE`。

在上述环境下，实际运行的测试目标为：

```text
tests/test_native_read_batches_oci.py
tests/test_missing_source_oci.py
tests/test_read_capacity_oci.py
tests/test_agent_v2_tools.py
tests/test_sandbox_implementation.py::test_real_fresh_container_verify_has_no_network_credentials_or_writes
tests/test_sandbox_implementation.py::test_real_verify_resource_exhaustion_is_bounded_and_structured
tests/test_sandbox_implementation.py::test_real_plan_bound_implementation_in_disposable_volume
tests/test_coding_stop_boundaries_oci.py
tests/test_coding_recovery_v11_oci.py
```

前面除 Plan 绑定隔离卷外的目标合并产生 `coding-v11-oci-final.xml`（29通过，其中
2项纯单元），隔离卷另产生 `coding-v11-oci-compatibility.xml`（1项真实 OCI 通过）。
因此真实 OCI 为28项，既有24项全部重跑；这些计数不与默认套件中的跳过项重复计算。
新第9轮成功/失败测试采用合成测试仓库，不宣称已完成原 Issue 的改动或四条真实项目命令。
成功路径执行的是真实测试仓库检查及源码 Hash 核验；独立 Reviewer 是确定性替身。

保存信封重放：

```sh
PYTHONPATH=. .venv/bin/python docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/replay_saved_envelopes.py
```

该脚本会在本目录生成新的合成证据，不能把新生成的 ID/Hash 当成旧报告的同一快照；
原 Plan12–15 文件只读，不覆盖原运行报告。报告保留原响应 Artifact 标识与原调用 ID。
`finalize.py` 校验导出 Artifact 的文件名 SHA256，并脱敏 JUnit；不复制数据库或运行日志。

历史检查脚本仅对现有旧服务执行 `BEGIN` 和 `PRAGMA query_only=ON`，比较原45表、11
检查点与五角色源码；无需运行任何部署或执行 API。只读检查脚本和原完整读取器都在
仓库中，前后结果及 Plan11–15 的事件链 Hash 已随本目录保存。

额外检查：

```sh
.venv/bin/python -m compileall -q app
git diff --check
```

Git交付导出还使用 `git apply --reverse --check` 校验完整源码 Diff，以及重建旧
closeout 后的 `git apply --check` 校验增量 Diff。所有源码及测试均未自动暂存或提交。
敏感信息扫描覆盖本轮源码/测试/文档、完整 Diff、本地凭据值及其 Base64/hex 变体；
只报告匹配数量，绝不输出凭据值。

本轮未验证 Linux amd64；保存的 high reasoning /4096 输出截断与未知执行测试继续
验证保留真实回执和预留、安全停止，不能据此承诺真实模型会产生有效 Patch。
