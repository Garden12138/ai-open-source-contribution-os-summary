# 本地部署与运行

## 要求

| 项目 | 要求及本轮环境 |
| --- | --- |
| 系统 | 本轮验证 macOS Apple Silicon；Linux 原生与 Linux/Docker 未验证 |
| Python | 3.11+；实际 3.12.11 |
| 环境管理 | 推荐 uv；也可 venv + pip |
| Node.js | 启动不需要；JS 与可选浏览器测试使用 22.23.2 |
| Docker | 离线试用和只读发现不需要；真实外来仓库执行必须 Docker/OrbStack |
| 存储 | SQLite + 本地 Artifact；默认隔离目录 .local-demo |

原生 HTML/CSS/JS 由 API 直接提供，无 npm 前端构建。准确依赖版本见验收报告。

## 克隆与安装

```bash
git clone --branch feature/upgrade-chatgpt https://github.com/Garden12138/ai-open-source-contribution-os-summary.git
cd ai-open-source-contribution-os-summary
uv venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
```

已有本地仓库和可用 .venv 时跳过克隆和重建。没有 uv：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

## 默认离线试用

```bash
./scripts/local-start.sh
./scripts/local-smoke.sh
open http://127.0.0.1:18765
```

start 先顺序初始化/迁移数据库，再启动自有 API 和 Fake Worker。Web、API 和 `/docs` 共用端口。脚本不加载 .env；子进程使用环境变量白名单，不继承 Provider、GitHub、Publisher 凭据或真实验收开关。随机的本地访问与 Job 签名材料保存在 `.local-demo/config.json`（0600），目录 0700。

smoke 通过真实本地 HTTP API 完成：合成扫描 → Fake 分析 → 任务 → 预设方案 → 批准合成 base → Explore → 预设 ChangeSet → 模拟 Verify → Fake Review。没有真实 AI，也没有执行仓库测试。每次运行新增一条样例历史；结果包含精确 ID、差异哈希、Artifact 数和访问地址。

```bash
curl --fail --silent http://127.0.0.1:18765/health
cat .local-demo/last-smoke.json
tail -f .local-demo/serve.log .local-demo/worker.log
```

可以自行点击“立即扫描新机会”“AI 筛选候选”，查看 Fake 结果。模型设置页可以打开，但凭据管理不可用；发布关闭。真实对话规划不在此模式中运行。

停止与重启：

```bash
./scripts/local-stop.sh
./scripts/local-start.sh
```

脚本仅对 PID 和完整命令均匹配的自有进程发送终止信号，保留数据，不重启 Gateway、Compose 或其他服务。端口冲突时：

```bash
./scripts/local-start.sh --port 18766
./scripts/local-smoke.sh
open http://127.0.0.1:18766
./scripts/local-stop.sh
```

可隔离多套数据；三个脚本传相同目录：

```bash
./scripts/local-start.sh --data-dir /private/tmp/contribos-my-trial --port 18766
./scripts/local-smoke.sh --data-dir /private/tmp/contribos-my-trial
./scripts/local-stop.sh --data-dir /private/tmp/contribos-my-trial
```

目录必须新建或有脚本标记，不能指向既有业务目录。自定义目录放仓库外，勿提交。

## 可选：真实 GitHub 只读发现

需要可访问 GitHub；建议正式获取只读 Token。此模式使用独立 `.local-live`，不调用 AI。不要从生产 shell 继承已有 Gateway/Provider 配置，不要 source 整份 .env。

终端一（macOS 默认 zsh）：

```bash
mkdir -p .local-live
chmod 700 .local-live
export DATABASE_URL="sqlite+pysqlite:///$PWD/.local-live/contribos.db"
export ARTIFACT_ROOT="$PWD/.local-live/artifacts"
export ANALYSIS_PROVIDER=none IMPLEMENTATION_PROVIDER=none REVIEW_PROVIDER=none
export SANDBOX_STAGE_RUNTIME=none PUBLISHER_MODE=none
read -r -s 'GITHUB_TOKEN?GitHub read-only token: '; echo
export GITHUB_TOKEN
export LOCAL_ACCESS_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/contribos serve --host 127.0.0.1 --port 18766
```

bash 的交互输入改用 `read -r -s -p 'GitHub read-only token: ' GITHUB_TOKEN`。未认证也可访问部分公开数据，但额度较低；不需要 Token 时跳过输入与 export。

API 首次迁移完成后，在终端二设置相同目录和只读 Token：

```bash
export DATABASE_URL="sqlite+pysqlite:///$PWD/.local-live/contribos.db"
export ARTIFACT_ROOT="$PWD/.local-live/artifacts"
export ANALYSIS_PROVIDER=none IMPLEMENTATION_PROVIDER=none REVIEW_PROVIDER=none
export SANDBOX_STAGE_RUNTIME=none PUBLISHER_MODE=none
read -r -s 'GITHUB_TOKEN?GitHub read-only token: '; echo
export GITHUB_TOKEN
.venv/bin/contribos worker
```

打开 18766 页面，点击“立即扫描新机会”。也可在相同环境运行 `.venv/bin/contribos scan` 做一次同步扫描。两个前台进程分别 Ctrl-C 停止；按相同命令重启，日志位于各自终端。

serve/worker/scan 自动前向迁移。已有数据升级前备份匹配的数据库、Artifact 与代码；恢复使用一致备份，不删除旧库重建。见 [迁移说明](database-migrations.md)。

## 切换真实 AI 模型

离线脚本固定 Fake，不能靠修改继承环境变量切换真实 Provider。真实模式需要单独的 API、Provider Worker、Gateway；代码读取/执行还需要专用 Sandbox Worker、固定摘要镜像及受控依赖准备。

1. 阅读 [Provider 指南](nvidia-provider.md)、[Compose](deployment/compose.yaml) 和 [Gateway 恢复说明](agent-harness-reset-v1/coding-loop-recovery-v1.1/gateway-credential-incident-v1/repair-v1/deployment-rollback.md)。不要对已有服务直接运行 deploy.sh。
2. 仅对全新、独立 Gateway 正式执行 `contribos gateway-storage initialize --root PERSISTENT_ROOT --intent new-install`。预建专用持久目录，按存储策略设置权限、记录 store ID，配置独立运行/管理/readiness 密钥文件。已有 Gateway 不能初始化覆盖，须先核验匹配备份与身份。
3. 各角色只接收其专用能力；只有 Gateway 保存模型凭据，Sandbox 不得持有凭据或宿主秘密。
4. 真实实例“设置”→“模型设置”：添加服务商连接、模型、API Key，再选默认或四阶段模型。API Key 通过浏览器加密设置流程输入，不能写入脚本、聊天或业务 SQLite。
5. “测试连接”可能计费，应由用户在配置完成后手动执行；本轮未运行。

实现支持 NVIDIA、DeepSeek、MiniMax、OpenAI 兼容服务与已注册的原生 xcli，实际能力以端点与 readiness 为准。容器 xcli Host Bridge 未实现。Gateway 事故恢复、真实模型和陌生 Issue 自主贡献本轮均未验收；Plan16 不可恢复。

## 测试、构建与排错

在无真实凭据、未设置 CONTRIBOS_RUN_* 的 shell：

```bash
.venv/bin/python -m pytest tests/test_local_demo.py tests/test_execution_pipeline.py
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q app
git diff --check
uv pip check --python .venv/bin/python
uv build --wheel --out-dir /private/tmp/contribos-wheel
```

默认测试采用 fakes，无需真实网络/凭据；opt-in OCI、跨平台和付费测试的跳过不代表通过。

可选真实浏览器检查使用 `tests/local_demo_browser.mjs`，只连接有离线标记的 loopback 实例并阻断外站请求。已有 Playwright 时设置 PLAYWRIGHT_MODULE 与 PLAYWRIGHT_CHROMIUM_EXECUTABLE 后运行；也可把浏览器依赖安装到仓库外：

```bash
npm install --prefix /private/tmp/contribos-browser --no-save playwright
/private/tmp/contribos-browser/node_modules/.bin/playwright install chromium
PLAYWRIGHT_MODULE=/private/tmp/contribos-browser/node_modules/playwright node tests/local_demo_browser.mjs
```

先启动并 smoke 默认 18765 实例；浏览器测试需要至少一个候选和任务。若指定其他端口，设置 CONTRIBOS_BROWSER_ORIGIN。本轮复用了已有 Playwright 与 Chromium 缓存，未新增项目依赖。


| 问题 | 处理 |
| --- | --- |
| 找不到 .venv/bin/python | 先创建环境、安装 .[dev] |
| No module named pip | uv 环境可不含 pip；用 uv pip --python .venv/bin/python |
| 端口冲突 | 指定 --port；不要停止未知归属进程 |
| 已有 processes.json | 先 local-stop.sh，检查私有日志；不要 kill 无关 PID |
| 启动失败 | 看 serve.log/worker.log；脚本已改为先迁移再并发启动 |
| Job 一直 queued | 确认对应 Worker 正在运行且数据库相同 |
| 真实扫描 403/429/空 | 检查 Token、网络/代理、查询和过滤；不可用 Fake 冒充真实成功 |
| 规划/Coding 阻断 | 离线只提供预设 smoke；真实模式检查模型、Gateway、镜像和 Sandbox |
| 模型密钥无法保存 | 离线属预期；需要独立 Gateway |

默认仅监听本机。内网访问必须另配 TLS、认证、同源反向代理与 CSRF，并禁止直接暴露裸 API；该模式本轮未验证。不要直接使用 --host 0.0.0.0。
