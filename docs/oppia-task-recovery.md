# Oppia 任务恢复检查

任务：`9727c1c6-04d4-4422-a8b0-e47b61517816`。
检查日期：2026-10-09。来源为本地已缓存、只读检查的仓库归档；未执行安装脚本，未访问 GitHub 写接口。

## 当前事实

- 状态为 `planning`，没有活动执行。最新方案 ID 为
  `95056cfa-4af9-40e8-96fc-5a7331eee9e4`。
- 最新方案是修 Puppeteer 调用参数，与原 Issue 的 Playwright 迁移目标不同。
  两个方向需要用户选择，不能因模型审查建议就自动替换贡献目标。
- 最近实际执行为第 19 轮 `1a23ecef-7b3d-42b6-8209-8913fdbafa73`。
  六条命令均失败，缺 `filetype`、`yaml`、`packaging`；审查为 `block`。
  后续授权取消，未生成新 Attempt，所以当前授权的 `latest_attempt_id` 为空，
  不表示历史上没有执行过。历史 Diff、测试和审查仍保留。
- 方案保留了 lint、TypeScript 检查、两个 suite 的 desktop/mobile 共六条命令。
  `--skip_install` 跳过安装，不能使一个未准备环境变为可用。

## 环境证据

Base：`ad22e91c318560644f25dd5c9d978afae6740677`。
Archive SHA-256：`9a2043ac602743ef219ddeb46e88393cfd5c76a924f1fedb50562d36b951b03a`。

| 归档内路径 | 静态检查所得要求 |
| --- | --- |
| `requirements_dev.txt` | filetype 1.2.0、packaging 25.0、PyYAML 6.0.2；还有其他开发依赖，并非只需三个包 |
| `requirements.txt` | 带哈希的生产依赖，包括应用服务使用的 Google 库 |
| `scripts/install_third_party_libs.py` | 检查 Python 3.12.13，安装 Node/Yarn 和 Google Cloud SDK 等；不可在宿主机直接执行 |
| `scripts/common.py` | Node 16.13.0；Playwright 使用 Node 20.11.1；Yarn 1.22.15；Redis 6.2.4；固定 oppia_tools 路径布局 |
| `package.json` | 仓库本地 eslint 插件、GitHub tarball、Git 依赖；当前 npm registry 白名单及通用依赖挂载不覆盖 |
| `scripts/run_acceptance_tests.py` | Playwright 路径安装浏览器；启动 Redis 和本地应用服务；完整环境还需验证实际离线行为 |

这些是该 base 的要求，不能推断后续上游版本相同，也不能认定全部依赖均已列出。

## 继续顺序

1. 确认贡献目标：原 Playwright 迁移，或最新的 Puppeteer 参数修复。
   在同一任务中选择“讨论方案”形成新版本；保留原有历史。
2. 实现并验证 Oppia 专用环境配置。固定镜像与工具版本，在独立、无凭证的
   依赖准备阶段获取经校验的依赖；适配双 Node、Yarn、本地依赖布局、浏览器、
   Redis 及应用服务。第三方依赖来源需要明确的校验与代理策略，不能开放任意网络。
3. 在未修改的同一 base 上用新环境执行方案的原始六条命令，记录基线失败与环境问题。
   基线成功前不开始新一轮自动代码修复，也不删除或降级验证命令来伪造通过。
4. 环境通过后刷新绑定该环境的代码上下文，保存新方案版本，由用户在对话内确认执行。
   新 Attempt 必须绑定新的镜像、策略和依赖证据；不可复用旧审批或改写旧测试结果。
5. 修改后执行相同命令并独立审查。Fork、Push、工作流触发、Issue 评论及分配请求
   不属于本工作台自动执行范围；真实外部发布仍受原有一次性确认边界约束。

当前工作台可继续讨论方案、查询进度和查看历史证据；**完整 Oppia 执行环境仍未实现**。
重新发送“继续”、重新建任务或再次批准旧方案，都不能解决这个环境缺口。
