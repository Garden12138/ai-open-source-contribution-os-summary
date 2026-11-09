"""Render the already frozen, unapproved preview and unique future consent text."""
from __future__ import annotations

import json
import shlex

from app.security import ensure_no_sensitive_data
from verify import ROOT, checked


def write(name: str, text: str) -> None:
    ensure_no_sensitive_data(text, context=name)
    with (ROOT / name).open('x') as stream:
        stream.write(text)


def main() -> None:
    p = checked(ROOT / 'execution-preview.json', 'proposal_hash')
    f = checked(ROOT / 'frozen-inputs.json', 'frozen_inputs_hash')
    plan = p['plan']; budget = checked(ROOT / 'budget-and-risk.json')
    confirmation = f'''批准且仅一次执行 Plan16 精确预览 `{p['proposal_hash']}`。
仅授权独立 Plan16 `{plan['id']}`，Plan Hash `{plan['record_hash']}`，Plan Content Hash `{plan['content_hash']}`，新 Binding Hash `{p['binding_hash']}`，冻结输入 Hash `{f['frozen_inputs_hash']}`，预算/费用风险说明 Hash `{budget['report_hash']}`。
源码提交 `{f['source_commit']}`，255 文件集合 Hash `{f['production_source_hash']}`；app `{f['images']['app']}`，Sandbox Worker `{f['images']['worker']}`，Runner `{f['runner']}`，Sandbox Policy `{f['sandbox_policy_hash']}`。
Native transport `{f['native_transport_hash']}`，Schema `{f['native_transport']['tools_schema_hash']}`，Prompt `native-coding-loop-recovery-v11`；模型绑定 `{f['model_binding_hash']}`，deepseek-v4-pro、reasoning_effort=high，单次 Coding 输出最多4096且受剩余额度限制，模型配置不变。
目标 patoles/agent-flow#107，base `{f['target']['base_sha']}`，Issue 正文 `{checked(ROOT / 'upstream-check.json')['issue_body_hash']}`，归档 `{f['archive']['archive_hash']}`，Context `{f['context_hash']}`；仅限预览中的原8条写入路径、4条命令及Issue验收目标。
Execution revision `{p['execution_revision']}`，Preflight `{p['preflight_hash']}`。执行前重核全部冻结输入和静态门禁，任一变化立即停止；只有新的本次确认可生成Plan16执行授权，不能复用Plan15确认、审批、Binding或终态。
仅授权一次有界真实验收：新鲜受限OCI依赖准备及原4条Baseline全过 → 原生真实Coding/Patch → 修改后最新源码4项Verify → 原范围最多2次有限修复及独立真实Review → 停在最终人工验收门禁前。
保留原总上限：Coding模型24次、读取64、写入24、检查6、协议纠偏2、Provider重试2、代码修复2、输入200000 Token、输出32000 Token、工具/证据500000字节、1800秒。保留单步最多1次尝试/300秒、输入投影32000、页16000字节、原只读批8-call/子批及Decision4-call、有界诊断8192字节及冻结沙箱所有限制。
6轮纠偏、8轮判断；仅第8轮OCI新观察的相关当前源码确实进入第9轮真实Prompt且原剩余预算足够，才允许跨Worker/修复Session同一授权下唯一一次条件机会。最多1次调用/输入32000/输出4096全部从原总预算扣除，不增加容量。第9轮没有真实OCI成功应用Patch则第10次请求前停止；未知操作保留预留并阻断。预算不足立即停止，不保证第9轮可派发。
独立Review沿用每Job最多3次尝试、300秒、证据输入1800000字节、Profile每次输出最多16384 Token及原最多2次修复限制；独立Review是Coding24次之外的额外计费，分开报告。知悉high reasoning可能耗尽4096输出且无Patch，9次全额4096已超过32000原总额；没有冻结货币单价或货币硬上限，按实际Provider计费，失败和未知请求可能仍有费用，不提高Token或其他预算。
依契约记录首次真实apply_patch提案轮次、首次OCI成功应用轮次、当前源码唯一读取/真实Prompt送达覆盖、重复读取/Artifact恢复效率、6+8+1证据、截断及费用、最新源码4项Verify、独立真实Review和最终人工门禁；未观察到的值保留null/not_observed/not_run，读取成功、替身通过或调用数增加均不算真实Coding成功。
历史Plan11–15终态、审批、费用、预算和3次未知占用原样保留；不退款、不重置。禁止付费Planner、模型配置实验、恢复旧终态、复用旧授权、增加容量、Push、PR、评论、发布或自动另起付费轮次。失败、预算耗尽、未知结果或门禁失败安全停止。
本确认只绑定上述Plan16预览；幂等键 `harness-coding-v11-plan16-execute-once`，仅消费一次，完成后等待最终人工验收。
'''
    write('confirmation-text.txt', confirmation)
    hashes = {
        'Plan ID': plan['id'], 'Plan Hash': plan['record_hash'], 'Plan Content Hash': plan['content_hash'],
        'Binding Hash': p['binding_hash'], '冻结输入 Hash': f['frozen_inputs_hash'], '精确预览 Hash': p['proposal_hash'],
        'Execution revision': p['execution_revision'], 'Preflight': p['preflight_hash'],
        'Context': f['context_hash'], 'Snapshot': f['target']['snapshot_hash'],
        '255 文件源码': f['production_source_hash'], 'app 镜像': f['images']['app'],
        'Sandbox Worker 镜像': f['images']['worker'], 'Runner': f['runner'], 'Sandbox Policy': f['sandbox_policy_hash'],
        'Native transport': f['native_transport_hash'], 'Native schema': f['native_transport']['tools_schema_hash'],
        'Model binding': f['model_binding_hash'], 'Issue 正文': checked(ROOT / 'upstream-check.json')['issue_body_hash'],
        'base SHA': f['target']['base_sha'], '归档': f['archive']['archive_hash'],
        '只读先决核验': p['prerequisite_report_hash'], '验收契约': p['real_acceptance_contract']['record_hash'],
        '预算/费用风险': budget['report_hash'], '备份 SHA256': checked(ROOT / 'backup-check.json')['sha256'],
    }
    rows = '\n'.join(f'| {k} | `{v}` |' for k, v in hashes.items())
    limits = '\n'.join(f'| `{k}` | {v} |' for k, v in budget['coding_limits'].items())
    paths = '\n'.join(f'- `{x}`' for x in f['write_paths'])
    commands = '\n'.join(f"| `{c['command_id']}` | `{shlex.join(c['argv'])}` | `{c['working_directory']}` |" for c in f['commands'])
    criteria = '\n'.join(f'- {x}' for x in f['acceptance_criteria'])
    history = checked(ROOT / 'history-check.json')['Plan11_15']
    historic = '\n'.join(f"| {label} | {r['checkpoint']['state']} | {r['checkpoint']['usage']['model_calls']} | {r['checkpoint']['usage']['input_tokens']} | {r['checkpoint']['usage']['output_tokens']} | {r['checkpoint']['usage']['output_bytes']} |" for label, r in history.items())
    doc = f'''# Plan16 独立冻结精确执行预览（尚未批准）

**冻结准备 Go；真实付费执行 No-Go，等待独立审查和新的精确一次性授权。**
本次真实 Provider/审批/Coding Job/Review Job/Attempt/新运行预算均为0。Plan16已独立创建，旧Plan11–15终态、审批、费用与未知占用不变；没有恢复Plan15。

[完整精确JSON](execution-preview.json) · [冻结清单](frozen-inputs.json) · [Plan16](plan.json) · [预算与费用风险](budget-and-risk.json) · [验收契约](acceptance-report-contract.json) · [最终审计](final-checks.json)

## 精确标识与Hash

| 项目 | 值 |
| --- | --- |
{rows}

本仓库分支 `refactor/agent-runtime-v2`，核验HEAD `{f['git_HEAD']}`，生产源码提交 `{f['source_commit']}`。工作区、该提交和五角色255文件逐项一致。
镜像Digest为本地不可变Docker Image ID，没有发布Registry镜像。Prompt为`native-coding-loop-recovery-v11`。
业务Binding新建并绑定Plan/context/Native协议；完整源码、镜像及其余输入由独立冻结清单和本预览Hash精确绑定。现有ExecutePlanRequest没有新增源码Hash字段，执行者必须在发送前重核完整冻结清单，不能只依赖API静态ready。
Plan16以Plan15为不可变父版本引用，业务内容Hash保持原目标；Plan ID、Plan Hash、状态版本和Binding均为新记录。父版本引用不携带旧审批或执行权限。

## 上游、路径、命令与验收目标

现有容器只读代理核验[Issue #107](https://github.com/patoles/agent-flow/issues/107)仍open、无人分配、未锁定；仓库公开且未归档/禁用。标题、正文、更新时间`2026-10-29T22:23:57Z`和main/base均未变。实际归档877322字节Hash一致，归档根目录`agent-flow-{f['target']['base_sha']}`与base一致，未执行仓库代码。

原8条允许写入路径：

{paths}

原4条批准命令；修改后必须在最新源码上全部Verify，修改前Baseline不能代替：

| ID | argv | cwd |
| --- | --- | --- |
{commands}

原Issue验收目标原样保留：

{criteria}

## 原预算与费用风险

以下为等待独立授权的一次有界运行原上限，本次未分配，未增加容量：

| 项目 | 上限 |
| --- | ---: |
{limits}

deepseek-v4-pro，reasoning_effort=high，temperature=null、structured_output=tools；原Profile输出16384/超时300秒，Coding每次输出min(4096,剩余额度)。模型配置和版本均在冻结JSON内，不混入实验。
单步Job最多1次尝试/300秒；输入投影32000；单页16000字节；原只读批8-call、子批及Decision4-call；有界失败信封8192字节。所有读取/写入/检查/协议纠偏/Provider重试/修复/证据/时间均受原总额约束。

6轮未成功Patch后纠偏；8轮后判断是否具备一次条件机会：第8轮实际OCI新观察、相关路径、当前source Hash和真实回执必须匹配，新增源码还必须确实出现在第9轮Prompt。跨Worker/修复Session同一授权只能领取一次；未知调用或只有重复读取均不能领取。第9轮无真实OCI成功应用Patch，第10次请求前停止。
该机会最多消耗原额度内1次调用、32000输入上界及4096输出；派发前核验精确Prompt及剩余各维预算。输出不足4096时按剩余收窄，低于128或输入/时间等不足则不派发。要保留完整32000/4096，之前累计输入最多168000、输出最多27904（含保留预留）。9×4096=36864>32000，9×32000=288000>200000，不能保证走满9轮或完整流水线；绝不加预算覆盖。

独立真实Review沿用独立证据绑定Job：输入证据最多1800000字节、最多3次尝试、每Job300秒、Profile每次最多16384输出Token；最多2次代码修复可能产生新证据与再次Review，仍受原有状态机约束。Review费用在Coding24次之外单列，不隐藏为免费步骤。
本地模型配置未冻结可验证货币单价或货币硬上限，所以不编造人民币/美元总价。费用为Coding及每次独立Review的实际输入/输出乘Provider单价，失败/未知请求也可能计费；未知结果保持预留并阻断，不退款或重试未知操作。high reasoning可耗尽4096且没有final_text/tool_calls，Plan15已真实发生，不能因此提高容量或降低reasoning。

沙箱保持CPU2、2GiB内存、PID256、每操作600秒、4GiB磁盘、4MiB日志，非root、断网、无凭据/真实模型、只读根、drop ALL/no-new-privileges；只有实现阶段允许批准工作区写入。依赖准备保持独立且仅允许受限包代理。五角色原挂载/权限/网络/资源逐字段匹配保留旧容器；无重新部署。

历史Planning仍64/64、输入1639419/输出166739，3次未知输入1350000/输出40960预留不变。本次不调用Planner。
下表为历史账本已计费/预留数，不冒充Provider实际Token，原事件保留两者区分：

| Plan | 终态 | model_calls | input_tokens | output_tokens | output_bytes |
| --- | --- | ---: | ---: | ---: | ---: |
{historic}

## 真实验收指标与停止点

| 指标 | 必须保留的真实证据 | Plan16当前值 |
| --- | --- | --- |
| 首次apply_patch提案 | 1起算真实Coding轮次、response/call Hash | null/not_run |
| 首次OCI成功应用Patch | 独立提案/应用轮次、receipt/source/diff Hash | null/not_run |
| 当前工作区唯一源码读取 | 按当前source/file Hash及UTF8唯一区间，not_found不计源码 | not_run |
| 真实Prompt送达覆盖 | 每次实际请求的完整Prompt Artifact、逐文件区间/截断/恢复引用；区分工具返回与确认送达 | not_run |
| 重复读取与Artifact恢复效率 | 同offset/重叠/复用、请求和实际字节、原文分页连续性、后续Prompt及重建Hash | not_run |
| 6+8+1 | 第6轮纠偏、第8轮来源/新增区间/预算判断、第9轮真实Prompt与机会消费；第10次前停止证据 | not_run |
| high截断 | finish_reason、实际final_text/tool_calls/Token；不推断隐藏reasoning | not_observed |
| 最新源码Verify | 原4条命令真实结果及最新source/diff/tests/contract Hash | not_run |
| 独立真实Review | 独立ReviewRun/invocation、最新diff/Verify/tests、身份/结论/发现/单列费用 | not_run |
| 最终人工门禁 | 独立Review之后停止，等待人的最终验收 | not_run |

读取成功、测试替身通过或调用次数增加均不算真实Coding成功。没有实际发生的指标保持null/not_observed/not_run，不自动购买下一轮覆盖。完整计数/回执/费用契约见JSON。
真实执行若获另行授权，顺序为新鲜OCI准备及4条Baseline全过、同范围Coding/Patch、最新源码4项Verify、原预算内有限修复、独立真实Review，然后停于最终人工验收。任何关键输入变化、未知结果、预算不足或门禁失败均停止。

## 核验、恢复和限制

全部45表旧行、11检查点、2551旧事件前缀、1802Artifact原字节/Hash及Plan11–15链均核验一致。仅追加1个Plan、必要状态版本与4个准备事件；任务事件2551→2555，模型启动106、执行授权13、Plan审批13、活动业务Job0不变。
备份权限0700/0600、摘要、完整性/外键/45表Hash通过；与已归档旧镜像读取验收一致。本轮未执行完整回滚恢复；新增准备记录后禁止用旧备份覆盖当前库。旧五容器stopped且旧镜像仍可用。
原代理宿主密钥文件缺失，使用现有容器配置完成同一只读代理核验，密钥未导出或重建。公共API备用探针504没有作为通过证据。首次核验工具错误假设归档命名和checkpoint字段；归档原Hash已匹配，纠正为实际规范名和state字段后全门禁重跑通过，未绕过产品门禁。
离线证据为此前相同部署镜像50/53、真实受限OCI28+2unit和7组信封。本轮只读复算原容量证明228743≤459508；没有重跑真实OCI、Coding或Review。确定性替身Review不能证明真实Review。HR4/HR5、P5-G06和Linux amd64仍开放。

## 唯一待单独确认的执行文本

下文仅供之后独立审查确认，本次没有提交、发送或消费该授权。完整原字节保存于[confirmation-text.txt](confirmation-text.txt)，原Plan15文本绝不可复用。

```text
{confirmation}```

未发送的ExecutePlanRequest和新幂等键保存在精确JSON。diff/test/review/执行授权/Attempt Hash当前均为null，因为尚未运行；不得伪造未来Hash。完成冻结交付后立即停止。
'''
    write('execution-preview.md', doc)
    write('README.md', f'''# Plan16 独立冻结准备

**准备Go；真实执行No-Go，等待独立审查与新的精确一次性授权。**

Plan16 `{plan['id']}`；预览Hash `{p['proposal_hash']}`。
本轮只有只读核验和已授权的独立Plan/Binding冻结；付费调用/新审批/业务Job/Attempt/ReviewRun/预算分配均0。

- [完整精确执行预览、预算表、Go/No-Go、所有Hash与确认文本](execution-preview.md)
- [精确JSON](execution-preview.json) · [冻结清单](frozen-inputs.json)
- [只读先决核验](prerequisite-check.json) · [最终审计](final-checks.json)
- [真实验收契约](acceptance-report-contract.json) · [not_run模板](acceptance-report-template.json)
- [唯一一次性确认文本](confirmation-text.txt)（未提交、未消费；原Plan15不可复用）

核验HEAD `{f['git_HEAD']}`，生产源码 `{f['source_commit']}`；255文件Hash `{f['production_source_hash']}`。
仅保留新Plan与机械准备事件；旧Plan11–15终态、审批、费用/未知占用、11检查点和1802Artifact不变。
没有改生产代码、部署、恢复旧终态、增加预算、Push/PR/评论/发布。
HR4/5、P5-G06和Linux amd64仍开放。备份可读取，完整回滚未执行；之后不得用旧备份覆盖新准备记录。

`verify.py`只读；`freeze.py`仅一次创建已授权Plan/Binding与未发送JSON；`render.py`只写本地文档。
冻结脚本故意拒绝已有Plan文件，不能盲目重跑；异常须先只读核对幂等结果。
''')
    print(json.dumps({'preview_hash': p['proposal_hash'], 'confirmation_generated': True, 'authorization_consumed': False}))


if __name__ == '__main__':
    main()
