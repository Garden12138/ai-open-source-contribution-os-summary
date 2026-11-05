# 08. Plan11 Evidence Summary

## 样本

- 仓库：`patoles/agent-flow`
- Issue：[#107](https://github.com/patoles/agent-flow/issues/107)
- 冻结基线 Commit：`3489e17c48985b74fdb42cde122376b1eb12d8a7`
- 执行记录：`docs/acceptance/universal-environment-v1/ci-native-plan11-execution.json`
- 读取观察：`docs/acceptance/universal-environment-v1/ci-native-plan11-read-observation.json`

## 已确认事实

| 项目 | Plan11 结果 |
|---|---:|
| 环境准备 | 通过 |
| 原生入口探针 | 8 |
| 修改前 Baseline | 4/4 |
| 模型启动 | 14 |
| 有效 Decision | 12 |
| 被拒绝批次 | 2 |
| 每批调用数 | 5 |
| 每批实际执行数 | 0 |
| 工具执行结果 | 43 |
| `list_paths` | 4 |
| `read_file` | 39 |
| `search_code` | 0 |
| `apply_patch` | 0 |
| `run_approved_checks` | 0 |
| 源码修改 | 0 |
| 最终状态 | `native_terminal_blocked_agent_budget_exceeded` |

## 读取分布

| 文件 | 读取次数 | 唯一起始偏移数 | 总字节数 |
|---|---:|---:|---:|
| `web/hooks/simulation/handle-tool-events.ts` | 11 | 1 | 6,518 |
| `extension/src/transcript-parser.ts` | 2 | 1 | 33,130 |
| `extension/src/hook-server.ts` | 2 | 1 | 12,122 |
| `web/hooks/simulation/types.ts` | 9 | 1 | 4,416 |
| `web/lib/agent-types.ts` | 8 | 1 | 7,545 |
| `web/hooks/simulation/process-event.ts` | 7 | 1 | 4,631 |

39 次读取涉及 6 个路径，其中 33 次请求重复使用 offset 0。必须结合完整参数、源码 Hash、回执内容和 Prompt 引用判断，不能只凭 offset 断言所有调用无效。

## 两次协议拒绝

两次批次都因收到 5 个 Tool Calls、平台上限为 4 而整批拒绝：

- `whole_batch_rejected=true`
- `executed_calls=0`
- 原因：`agent_native_call_count_invalid`

两次 Provider 响应均显示：
- `finish=tool_calls`
- `tool_calls_present=true`
- `final_text_present=false`

需要检查 Provider Adapter 对工具调用响应的解释和转换。不能把缺少最终文本简单视为模型没有响应。

本摘要没有列出两次原始工具名称和参数，Codex 必须从真实事件/Artifact 还原，不能假设批次全部只读。

## 诊断边界

### 已证实
- 环境准备和修改前 Baseline 通过。
- Agent 执行大量源码读取。
- Agent 没有产生 Patch，也没有执行修改后 Verify。
- 两次超量批次整批拒绝。
- 任务最终因预算耗尽阻塞。

### 高可信假设，仍需代码和事件证明
- 缺少有效进度状态，模型无法从调查切换到实现。
- Prompt 对工具用途、已有证据和下一步约束不足。
- Provider Adapter 与平台批次契约存在不匹配。
- 系统缺少足够强的重复读取和无进展保护。

### 不能直接下结论
- 不能断言模型完全没看到历史结果。
- 不能断言只读分批一定安全。
- 不能断言换框架就能解决。
- 不能把该样本当成所有模型和仓库的普遍表现。
