# 真实验收精确预览 — Plan12（尚未批准）

后续状态：本预览已由用户明确批准并消费一次。下文保留提交前原文；
当前结果见[批准记录](real-acceptance-approval.json)和
[真实验收结果](real-acceptance-result.json)。禁止把本存档当成另一轮执行授权。

预览hash：`81e1d70a6cc7433461cc5cee1ea8ba7abb93c859236d6392e2870beea772420f`。
完整机器记录：[real-acceptance-preview.json](real-acceptance-preview.json)。

目标：[patoles/agent-flow#107](https://github.com/patoles/agent-flow/issues/107)。
基线：`3489e17c48985b74fdb42cde122376b1eb12d8a7`。
新Plan ID：`5c6df081-f469-44be-a2de-81fec47b2289`；父Plan11内容hash保持一致。
源码版本：`59862614eb46caf2b8cdb495220ee92b1fe638db75e2ab0fc3bf8e57f7be8bf8`，已部署并检查五个角色。
实现与独立Review沿用原`deepseek / deepseek-v4-pro`绑定，没有切换模型或账号。

## 本次需要确认的原因

Plan11终态及已用输入171256/输出12756、14次模型启动、两次失败预留保持原样。
其时间余额为0。新的Native v6工具/进度协议已改变冻结hash，按AGENTS.md不可变链规则，
旧批准不授权这轮执行。

确认本预览将授权**额外一轮有界真实验收预算**。每轮上限沿用原值，旧用量不返还、
不清零；规划64次上限及三个未知预留保持不变。保存本草稿未分配新预算或发起模型调用。

| 执行项目 | 申请上限 |
| --- | ---: |
| 模型调用 | 24 |
| 输入token | 200000 |
| 输出token | 32000 |
| 时间 | 1800秒 |
| 读取 / 写入 / Checks | 64 / 24 / 6 |
| 协议纠偏 / Provider重试 / 代码修复 | 2 / 2 / 2 |
| 输出字节 | 500000 |

独立Review保留原单Job上限：最多3次尝试，每次
300秒、输出至多16384token，
输入证据上限1800000字节。它使用独立上下文与真实Diff/Verify证据。

## 写入路径

- `extension/src/transcript-parser.ts`
- `extension/src/hook-server.ts`
- `web/hooks/simulation/handle-tool-events.ts`
- `web/hooks/simulation/types.ts`
- `web/lib/agent-types.ts`
- `web/hooks/simulation/handle-tool-events.test.ts`
- `extension/test/transcript-parser.test.ts`
- `extension/test/hook-server.test.ts`

## 批准检查

- `pnpm test`，工作目录`.`。
- `pnpm --dir extension test`，工作目录`.`。
- `pnpm --filter agent-flow run lint`，工作目录`.`。
- `pnpm --dir web exec tsc --noEmit`，工作目录`.`。

## 执行动作

重新准备环境与修改前Baseline；原生Investigation、批准范围内真实Patch；
对最新源码运行四条原命令；必要时在原上限内Repair；独立Review；停在Human Acceptance。
执行前再次检查Issue状态、HEAD及全部冻结hash，变化时拒绝使用本预览。

单轮4-call上限、整批拒绝、沙箱/命令/文件hash/预算/Journal门禁保持。
第三方写入授权为0项。真实Patch、修改后Verify与真实模型Review目前均未运行，
本预览不表示已通过Gate4/5。

可明确回复：**批准预览 `81e1d70a`，同意上述额外有界验收预算**。
