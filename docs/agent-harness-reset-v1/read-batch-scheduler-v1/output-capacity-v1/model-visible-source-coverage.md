# 模型可见源码覆盖：离线重建

这是保存的第三次真实响应之后的假设性下一轮 Native Prompt，未发送给模型，也不是恢复 Plan14。
使用真实冻结 Issue/Plan、逐文件 Hash 校验的源码、确定性工具结果、生产 Ledger reducer 与投影器。
预算保留 Plan14 已计入的调用/Token/工具费用，再加上本次离线计算的工具证据成本；秒数为保存时的观察值。
另外，offline-replay.json 的 prompt_artifact 来自真正运行的隔离持久化 Scheduler。
两者均为离线证据，不能代替真实模型 Patch/Verify/Review 验收。

原始页合计 **68338** 字节，完整文件合计 **85468** 字节。
下一轮 Native 输入界限 **31027 / 32000**，原文内联 **1024** 字节。
全部八个文件均非完整内联；表中 next_offset 是模型实际看见的后续位置，不能误当作 OCI 原始页的结束。
完整 ToolResult 与原文页 Artifact 均按 Hash 保存，原始页 next_offset 另列在 JSON 中。

| 文件 | 实际返回原文字节 | 实际内联字节 | 可见 offset / next_offset / 文件总长 | 原文页 Artifact 恢复调用 |
| --- | ---: | ---: | --- | ---: |
| web/hooks/simulation/handle-tool-events.ts | 6518 | 128 | 0 / 128 / 6518 | 1 |
| web/hooks/simulation/types.ts | 4416 | 128 | 0 / 128 / 4416 | 1 |
| web/lib/agent-types.ts | 7545 | 128 | 0 / 128 / 7545 | 1 |
| web/hooks/simulation/process-event.ts | 4631 | 128 | 0 / 128 / 4631 | 1 |
| extension/src/transcript-parser.ts | 16000 | 128 | 0 / 128 / 33130 | 2 |
| extension/src/hook-server.ts | 12122 | 128 | 0 / 128 / 12122 | 2 |
| extension/src/protocol.ts | 7015 | 128 | 0 / 128 / 7015 | 1 |
| extension/test/transcript-parser.test.ts | 10091 | 128 | 0 / 128 / 10091 | 1 |

通过现有 read_artifact 共 **10** 次可逐文件恢复全部已返回页。
每个原文页 Artifact 从 offset=0 开始；页外内容不能靠该 Artifact 伪造。
transcript-parser.ts 的剩余内容另经 **2** 次 read_file（仍请求 max_bytes=16000），
按实际可见 next_offset=16000→24192→EOF 恢复。最终重组的八个文件均与冻结 content_hash 一致。
所有分页输入均未超过32000，逐次请求/返回/可见大小与回执引用见 JSON。

这是可行的证据恢复路径，不是对真实模型下一步、Token 使用或最终补丁成功率的预测。
完整 Issue/Plan 仍以可恢复 Artifact 提供，不能假定模型记得之前的独立请求。
单个 Prompt 无法包含八个完整文件；上述最小改进解决恢复通道，未增加上下文、工具、Token 或模型次数上限。

完整下一轮 Native Prompt：
[内容寻址 Artifact](artifacts/e90f9784c74692033b62433cffacc2bbae011072043833ca842e4981424531a1.json)

[逐文件及逐次机器报告](model-visible-source-coverage.json)
