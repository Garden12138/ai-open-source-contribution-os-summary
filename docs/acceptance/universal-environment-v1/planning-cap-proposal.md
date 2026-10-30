# 本次任务的规划输出上限提案（已按用户同意应用）

2026-10-30 用户已明确同意；[批准记录](planning-cap-approval.json)与[实际结果](planning-cap-live-retest.json)保存执行事实。以下为原提案内容，JSON 原件及 hash 保持不变。

提案时现状：MiniMax-M3 最后一条规划 Job `038eb98e-3b06-4d88-87f9-d23eda9ff214`
在 8192 completion token 上限处截断，没有 Plan。模型/令牌连接已可用。
[原始 D 失败](adaptive-live-retest.json)与[真实预算投影](budget-reservation-live.json)
保留；提高上限不保证成功，不会重写首次 U 失败。

待确认动作：只为隔离任务 `f0e017fa-d2a6-4eb8-8a94-32bb1d29586c` 创建一个
新的 MiniMax-M3 规划 Profile，将单次输出 cap 从 8192 改为 16384，随后提交
一次原目标的只读规划请求。自动读取/格式修订仍受现有 4/2 轮约束，最多 7 次
调用，所有调用继续扣当前任务累计预算，单次输出还受实时剩余额度约束。
超时保持 180 秒；任务总额保持 32 调用 / 2,000,000 输入 / 100,000 输出。
旧 Profile、全局默认、其他任务和 Coding/Review 阶段不变；不新增或读取凭据。
这项确认不批准依赖准备、Baseline、Coding、PR、Push 或其他外部写入。

[精确 JSON 提案](planning-cap-proposal.json)绑定原任务、snapshot、源 SHA/归档、
原 Profile/默认/任务绑定哈希、当前预算 revision、失败 Job、原目标和新 Profile
payload/hash。执行前须重新核验这些依据；若改变则不能沿用该提案。新 Profile
只能通过原生 API 创建；不编辑数据库或旧事件，不回收未知调用预留。

当前已知用量为 17 调用 / 400,325 输入 / 21,859 输出。另有 1 次未知失败调用
预留 450,000 输入 / 8,192 输出；这不是实际消费。扣除预留后剩余 15 调用 /
1,149,675 输入 / 69,949 输出。本提案不增加总额度，单次可用输出分配会增加。

需要单独决定的依据：[专项规范 §2](../../universal-environment-v1/01-spec.md)
禁止自行通过“放宽 model budget”解决问题。本提案请求明确的新资源决定，
不宣称是已接受的工程方案；未获同意前不保存配置或发起调用。复测仍为 D，
不能关闭原生/泛化门禁；历史 Node、Linux amd64 的独立缺口仍须分别处理。

提案 hash：`50d46b44cfac323e080c4ae0fa9c1c70eeeacaa3cebf1af6e31c3eb2a1e3d0a7`。
新 Profile record hash：`0708deb053c3c3177047384f2d6e0d7f0e0949fb3442eb96eaaf234251a921a7`。
