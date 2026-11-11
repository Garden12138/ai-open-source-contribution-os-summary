# 历史证据索引

本轮维护日期：2026-11-11。历史结果不作为本轮验收 PASS；本轮记录见 [验收报告](../ACCEPTANCE_REPORT.md)。

## 保全与恢复

清理前工作树、Git 对象、索引、未跟踪文件及原始证据均已归档在本机仓库外的 `~/Documents/contribos-private-archives/contribos-maintenance-20261111T020132Z/`。目录 0700、归档 0600。不要公开上传该目录：它包含原始路径和本地配置。私有 `cleanup-inventory.json` 记录每个归档文件的路径、大小和原始 SHA256。`workspace-before.tar.gz` SHA256：`a569a04ceaf39a533b97d983fce2beea65e9c1d91a0b83763f3f067d373bb66d`；`removed-files.tar.gz` SHA256：`7edf8c6f68189e44235f1d8ce04ea555a7e12800945c259e90fa3316f7ba9b7d`。

恢复时先校验哈希，解压到新的私有空目录，再按需复制文件；不要覆盖现有 `.env`、数据库或 Git 目录。已提交的历史还可用 `git show <commit>:<path>` 查看。未提交的 Plan16 与 repair-v1 原始证据只能从上述私有备份恢复。

## 保留结论

- Gateway repair-v1 源码与测试保留于 `884d015` 及后继目标分支；候选源码清单历史计数 257。历史专项 52/52、全量 2151 通过/83 跳过、最终 OCI 184 通过/1 跳过、Publisher 兼容 12/12。这里只保留既有结论，未将其改写为本轮通过。
- Plan16 仍为 blocked，授权已消费；本轮没有恢复、创建新付费 Plan、改预算或发送模型请求。原始授权、事件和费用证据完整私有保全。
- 真实 Gateway 部署、恢复材料配对、Linux amd64 和 Phase 5 的开放门禁仍未验证；不因本地 Fake 流程完成而关闭。
- 原始 JUnit 有真实用户路径，全部移出当前交付；没有直接提交 staged JUnit。下表保留原始文件哈希，公开的新验收摘要独立生成且不含原始 traceback。
- 仍被测试读取的 Plan14 全部 Artifact、index/final-audit、Plan15 `evidence-index.json` 与 Artifact、旧 Node command graph 保持原始字节；它们是回归输入，不能删作过程日志。

## 最终结果原件哈希

以下原件已私有归档；路径是归档内路径，不是当前仓库文件链接。原件内容与结论未变。完整目录以私有清单为准。

| 原路径 | 原始 SHA256 |
| --- | --- |
| `docs/agent-harness-reset-v1/real-acceptance-result.json` | `5870e5fb3490fd75f6cadba499c936d4b14659b389976116abbdaaecf10e73d6` |
| `docs/agent-harness-reset-v1/final-validation.json` | `16192c88b18707395f00f4b92e5c22da9d19741bec07b5d6d15f119ff3582088` |
| `docs/agent-harness-reset-v1/verification-report.json` | `001f097a393d7ecd4f93d641e3e20f68e6fc6518fc103a3caeb6156c9e977360` |
| `docs/agent-harness-reset-v1/read-batch-scheduler-v1/validation-report.json` | `228a4e2e449c0077a77eecd247447327f81d6883e27061292e119fc6c0a56576` |
| `docs/agent-harness-reset-v1/read-batch-scheduler-v1/output-capacity-v1/validation-report.json` | `c92aee6ceec3100ae93c247586ee3f5564d4c7aa70bd54b0c9bfbf0da71b3904` |
| `docs/agent-harness-reset-v1/read-batch-scheduler-v1/output-capacity-v1/plan14-real-oci.json` | `1d5ffb522b7e0a6e03607c741aa771b61ff2b596bcd09bc86c0f761bef5d4230` |
| `docs/agent-harness-reset-v1/read-batch-scheduler-v1/plan14-execution/result.json` | `21806de475eb8d18eb37c736133c053fa690784f2a524b193d19e9e6a4c23767` |
| `docs/agent-harness-reset-v1/read-batch-scheduler-v1/plan14-execution/evidence-index.json` | `246378b9557165f8e801056038f38e4d81f137affa4acc8fcb3773f0fa83be32` |
| `docs/agent-harness-reset-v1/read-batch-scheduler-v1/missing-file-semantics-v1/validation-report.json` | `c271b8ee5d590cecf60970a637df25cd4e9521997d8f5922b29dd286810afe91` |
| `docs/agent-harness-reset-v1/read-batch-scheduler-v1/plan15-execution/result.json` | `6337c9c98312fe175cbc9bbd178aee0f494c8660517e62ec3f854579710cd7df` |
| `docs/agent-harness-reset-v1/read-batch-scheduler-v1/plan13-execution/result.json` | `f73ebc25b482da8737c87580f241dd0c34ae35b489f03c5f351c72067ac3fd2b` |
| `docs/agent-harness-reset-v1/read-batch-scheduler-v1/plan13-execution/evidence-index.json` | `3509d2982873c9ac00b852f63fd0f059b5461942c38554af7012b3e1d576bf25` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/validation-report.json` | `5e40cd1eec59b768398f531827082281d8be02f19f9ba4ee4220f79ea83f48b9` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/plan16-execution/result.json` | `48e2545c6c0a2c9a8461148fbc3a3507527b3c381a46b8d922dcc1e165b459dd` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/plan16-execution/evidence-index.json` | `b7b83ac6c5fc72dbe60c0354924e1012ae82f4cae0f1042d3db80cc46a99b813` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/gateway-credential-incident-v1/validation-report.json` | `508f4ecba730a7974c49216977fa0375ce858b9a1631258aea984b25f8f49887` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/gateway-credential-incident-v1/repair-v1/validation-report.json` | `b4bfe6796ca80292a066df4c02e546a1f9e9c04ad5250152b02134614f51e2c3` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/gateway-credential-incident-v1/repair-v1/test-results/full.json` | `f3ec42910183f9d6074e3d7a2a04425bebfd3c166b32dc862689d3eef0f35589` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/gateway-credential-incident-v1/repair-v1/test-results/oci.json` | `324a6161cd16663ece48d26c09aafd51ada746cd6b1ba13d981faf25b1680c19` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1.1/gateway-credential-incident-v1/repair-v1/test-results/public-full.json` | `9a85ea23a2f7f1c737ca610fabd36ad7ff6a83b352b1066ccf91cf68d14b5c41` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1/validation-report.json` | `e8add5d90956994187a11d27476f0748b8a76a86b147ab18b6d00e117a6fd463` |
| `docs/agent-harness-reset-v1/coding-loop-recovery-v1/review-closeout-v1/validation-report.json` | `ef6fee5f8f9ab5516abfb168223463a576c1e7480dd78e6396e4e2dd490c7173` |
| `docs/acceptance/universal-environment-v1/environment-preload-oci.json` | `9103b48d788f941658a8c7f4402bc303c9c2807ca71ad4c52fa45536410fb226` |
| `docs/acceptance/universal-environment-v1/pnpm-filter-oci.json` | `b111405786abbccb29007f8e2711bffe776cd3edd1c6c240b4aed532fe8cdfcf` |
| `docs/acceptance/universal-environment-v1/D-python-cache-241/dependency-result.json` | `d91568ad17b738df5d0408523ffef47e967a4ca20ceeaccd11a05452b7f6c45b` |
| `docs/acceptance/universal-environment-v1/H-python-ci-parser-01/dependency-result.json` | `210e24750999d71950689e379ea05b045587bdcbd7e22478f4d273918b883cb4` |
| `docs/acceptance/universal-environment-v1/D-node-02/dependency-result.json` | `21586e7645f86e24b63bc42f6b62126c00e8494133e5136b90fffbbfa4d820c0` |
| `docs/acceptance/universal-environment-v1/D-python-04/dependency-result.json` | `354fe06f3a63b22a96a21f6b5588fe98c28dd4bf1e2f659fbd16211fe4add60e` |
| `docs/acceptance/universal-environment-v1/D-python-03/dependency-result.json` | `a296e1a323658acf3785fdf8252251b06be6e971c868631cc2a11fd4c9a5142b` |
| `docs/acceptance/universal-environment-v1/D-python-ci-01/dependency-result.json` | `1f4c1236a2eff78f3e9fc13e36d90dcb041ef33fb327cb91cb4d354c595237fe` |
| `docs/acceptance/universal-environment-v1/D-python-profile-01/dependency-result.json` | `cd48050cbc2dc383c05c9c457bda86b871fea49928ec40d4d455dfcb85a1159a` |
| `docs/acceptance/universal-environment-v1/D-node-cwd-01/dependency-result.json` | `67fb6ff9547e530d354bad79deae93566be1a5dac5225545b32c3189ce9054c2` |
| `docs/acceptance/universal-environment-v1/D-python-cache-final/dependency-result.json` | `1422e44d1fea2a8a35e63a03a2a7d0990b6334b7f368c8b0bdf896ce032c1e7f` |
| `docs/acceptance/universal-environment-v1/H-python-02/dependency-result.json` | `087ec628c2616f421c11ec90d2e261a3f316bca6fe50c9292674f0a4a620af92` |
| `docs/acceptance/universal-environment-v1/H-python-03/dependency-result.json` | `83fc5e417cb088bfdf376a987e8dfe3eeaa94c45a3c2e72c73f875c93bf9fc17` |
| `docs/acceptance/universal-environment-v1/H-python-cache-04/dependency-result.json` | `c91579c362ae6c1a1b0b9806aaf72c539dce858ae521fce91a377221e28ed9de` |
| `docs/acceptance/universal-environment-v1/H-python-cache-final/dependency-result.json` | `2ea2f8455dd37541b604f11a74ad030a2e4e96a18a3ebd9ba53cb864f69486af` |
| `docs/acceptance/universal-environment-v1/D-node-01/dependency-result.json` | `ef108960530a0ba83de6932aa327ccbba9f0e9cf006c1648c7c692b18bf4d111` |
| `docs/acceptance/universal-environment-v1/D-python-cache-01/dependency-result.json` | `e81746b41cebb0c22cd3247e2a22ea14c6742a57249c4fd078071c6e7d810fdb` |
| `docs/acceptance/universal-environment-v1/H-python-profile-01/dependency-result.json` | `689e67dde384698fd0c5f0096f3b4587c41212246fafec7c4659b5a6db3e7d7d` |
| `docs/acceptance/universal-environment-v1/H-python-canonical/dependency-result.json` | `826ac2d761495bd00bb4f40d06e3089a8b8ea3e9b8a2799fb6f94d42423906af` |
| `docs/acceptance/universal-environment-v1/D-python-cache-repeat-02/dependency-result.json` | `9c7c5188ef69133651a81ba4153f49cf35d739a54f263d044f8a7b6ea4e1a6f5` |
| `docs/acceptance/universal-environment-v1/H-python-01/dependency-result.json` | `06dbe92334b6091d90fdad531da829ffd29f64485454563a23a4cb490361ea25` |
| `docs/acceptance/agent-runtime-v2/controlled-success-04-python/result.json` | `e9e4d1a0529a63bd9ac4f0f15a3a6fb84be14c540429b79cb5f8b32270879e83` |
| `docs/acceptance/agent-runtime-v2/controlled-failure-01/result.json` | `8dd047c5892b196b6e4fd0a412bcbac147505ceb394e0df71e6c43a1f4014439` |
| `docs/acceptance/agent-runtime-v2/controlled-failure-04-typescript/result.json` | `779a08e77b7e6e0ef679de8f6c493d2f9da9dae30a2d9e8eb2b74e671cd3027f` |
| `docs/acceptance/agent-runtime-v2/controlled-success-06-typescript/result.json` | `fe30f17b194d8a9cadd756887db7a1c2e1033fc346f07ff3eb5983f6983efc7a` |
| `docs/acceptance/agent-runtime-v2/controlled-failure-05-typescript/result.json` | `d3c467745d546827eed06dc62ffbbdd572c63ac677a403ba57ed1ffb026e5a26` |
| `docs/acceptance/agent-runtime-v2/controlled-failure-03/result.json` | `c1617a32e61d1aad64c98f6ec3159f3aaf35cff978e23aa13db5c28fb555ff44` |
| `docs/acceptance/agent-runtime-v2/controlled-failure-02/result.json` | `cba79692193353479467edfc2e39bc7ed98780d98145867b67837be9e0bb6890` |
