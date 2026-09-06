# NVIDIA Build 模型接入

本实现通过一个只在内部网络或 `127.0.0.1` 上监听的 Model Gateway 调用
NVIDIA Build。`NVIDIA_API_KEY` 只交给 Gateway；API、Provider Worker 和 Sandbox
Worker 都不会获得这个长期密钥。

默认模型分工：

| 阶段 | Provider | 模型 |
| --- | --- | --- |
| 机会分析与筛选 | NVIDIA NIM | `nvidia/nemotron-3.5-lightning-30b-a3b` |
| 多轮 Vibe Coding / ChangeSet | NVIDIA NIM | `deepseek-ai/deepseek-v4-pro-0813` |
| 独立 Review | NVIDIA NIM | `minimaxai/minimax-m3` |

模型页：[DeepSeek V4 Pro 0813](https://build.nvidia.com/deepseek-ai/deepseek-v4-pro-0813)、
[MiniMax M3](https://build.nvidia.com/minimaxai/minimax-m3)。适配器使用 NVIDIA 官方的
OpenAI-compatible `/v1/chat/completions`，并支持 `202` 后按 `requestId` 有界轮询。
Review 保持 MiniMax M3 官方示例的 `temperature=1`、`top_p=0.95`、`max_tokens=8192`。
Gateway 仅向 Provider Worker 返回完整结构化结果；浏览器和业务数据库不会保存或转发
上游原始响应。

MiniMax M3 的公开模型页把输出标为文本，但声明具备 tool-calling 能力。适配器因此要求一次
`contribos_record_structured_output` 函数调用，并把冻结的证据 ID 白名单编入函数参数 schema
的枚举值；模型不能提交白名单外的引用，应用仍会再次校验 JSON、schema、证据和 provenance。

适配器仍有 Kimi K3 SSE 的传输测试覆盖，以便未来新增专门的 Kimi 配置；当前没有把它
作为可选的服务配置，MiniMax 路径也不会收到 SSE 参数。

Gateway 和 Provider Worker 会忽略 `HTTP_PROXY`、`HTTPS_PROXY` 等继承环境变量：模型
请求只能直连 allowlist 中的 NVIDIA HTTPS 域名，Provider Worker 只能直连内部 Gateway。
若你在宿主机手动运行 NVIDIA 示例，也应在 `requests.Session()` 中设置
`trust_env = False`，避免本机代理在握手阶段断连。

如果宿主机直连 NVIDIA 失败、但通过公司的 **不解密 HTTPS CONNECT 代理** 可以调用，
可只给 Gateway 显式配置 `NVIDIA_HTTPS_PROXY`。该 URL 只允许 `http://` 或 `https://`
的无账号密码根地址；API、Worker、Provider Worker 和 Sandbox Worker 都不会获得它。
不要自动继承通用 `HTTP_PROXY`/`HTTPS_PROXY`，也不要把代理用户名或密码写入 `.env`。
在 macOS/OrbStack 上，如果宿主机代理监听在 `127.0.0.1:<port>`，容器必须改写为
`http://host.docker.internal:<port>`；容器内的 `127.0.0.1` 不是宿主机。
Gateway 访问 NVIDIA 时固定使用 HTTPS 上的 HTTP/1.1，与 NVIDIA 的 `requests`
调用示例一致；这可避开部分本地 CONNECT 代理出现的 HTTP/2 framing 错误。

Nemotron 3.5 Lightning 是机会 AI 筛选的默认模型：它通过 Gateway 内部 SSE 流式
传输并强制一次原生函数调用，同时设置 `enable_thinking=false`，避免 reasoning 混入函数
参数。自建 NIM 文档中的 `nvext.guided_json` 会被 Build 托管端拒绝，因此这里不发送该
扩展。Gateway 聚合出完整参数对象后仍会执行应用侧严格 Schema 校验再持久化；为减少
结构化字段漏项，按 Nemotron 模型专用结构化示例使用 `temperature=0`。ContribOS 将结构化输出的
`max_tokens` 限制为 `6144`：真实 Top 5 验收中个别复杂候选会在 4096 token 截断，6144
在覆盖完整结构化结果与守住 360 秒总时限之间提供余量。
筛选摘要的关键列表固定为一条高信息密度结论及一条对应引用；冻结证据中没有真实相似
Issue/PR URL 时，相似项必须为空，不能生成示例链接。
`citation_map` 是逐陈述引用的权威来源；冗余的顶层 `cited_evidence_ids` 会在进入严格校验
前按首次出现顺序确定性重建。该步骤不补写陈述引用，且白名单外 ID 仍会失败关闭。
DeepSeek 编码使用单独、较低温度的参数。MiniMax M3 仍可通过 `ANALYSIS_MODEL` 显式
选择；每个模型的参数由 Provider Worker 固定选择，而不是由页面输入决定。

NVIDIA Build hosted Endpoint 会拒绝工具参数中的 JSON Schema 元数据 `$schema` 与
`uniqueItems`。Gateway 只在发送工具定义时移除这两个上游不兼容字段；原始 Schema
仍用于提示和本地完整校验，引用枚举、必填字段、未知字段拒绝和去重校验都不会放宽。

每次 NVIDIA 上游请求（包括 SSE 持续有数据时）及其有限重试的硬总时限为 360 秒；Provider 到内部 Gateway
保留 390 秒外层时限。机会分析的一项候选会依次执行 Inspect 和 Analyze 两次调用，
所以批量 Top 5 中每项 Job 的总时限为 840 秒。Provider Worker 为单进程串行消费，
页面会按候选在队列中的位置增加等待时间，并实时显示已完成、正在执行与排队数量；
不能把后排任务误判为丢失。

为了避免一次性消耗五个失败请求，Discover 页面会先要求“验证 1 个样例”。只有当前
Snapshot 的样例分析成功，才会启用“AI 筛选前 5 个”；样例失败不会自动创建批量 Job。

## 1. 生成配置

从 NVIDIA Build 获取 API key 后，在本机生成另一把仅用于短期任务令牌的 HMAC
密钥：

```bash
openssl rand -hex 32
```

`.env` 中填写：

```dotenv
ANALYSIS_PROVIDER=nvidia_nim
ANALYSIS_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b
IMPLEMENTATION_PROVIDER=nvidia_nim
IMPLEMENTATION_MODEL=deepseek-ai/deepseek-v4-pro-0813
REVIEW_PROVIDER=nvidia_nim
REVIEW_MODEL=minimaxai/minimax-m3

NVIDIA_API_KEY=<NVIDIA Build API key>
NVIDIA_HTTPS_PROXY=<可选的可信 CONNECT 代理，例如 http://proxy.example:8080>
MODEL_GATEWAY_SIGNING_KEY=<openssl rand -hex 32 的输出>
```

密钥不得写入数据库、Artifact、日志或仓库。`.env` 只适合本地 Compose 插值；
Compose 文件没有把 `NVIDIA_API_KEY` 注入其他服务。

分离的宿主机进程也可使用 `NVIDIA_API_KEY_FILE`、
`MODEL_GATEWAY_SIGNING_KEY_FILE` 和 `SANDBOX_JOB_SPEC_SIGNING_KEY_FILE` 指向仅对应
进程可读的 Secret 文件，避免把长期凭证放进环境变量。

## 2. Compose：真实分析与 Review

以下命令都必须从仓库根目录运行，并显式指定根目录 `.env`。不要依赖 Compose
自动寻找环境文件；部分版本会改用 `docs/deployment` 作为项目目录，导致密钥被展开
为空。

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia up -d --build
```

该 Profile 增加两个进程：

- `provider-worker`：只有 SQLite/Artifact 与短期任务令牌签名能力，不能直接出网；
- `model-gateway`：唯一持有 `NVIDIA_API_KEY` 的进程，只接受签名、限次、限时且绑定
  Provider/模型/阶段/输入哈希的请求，并只允许访问 NVIDIA 的固定 HTTPS 端点。

检查状态：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml --profile nvidia ps
docker compose --env-file .env -f docs/deployment/compose.yaml --profile nvidia \
  logs --tail=100 provider-worker model-gateway
```

预期有四个服务：

- `api`：`Up (healthy)`，端口必须显示
  `127.0.0.1:8000->8000/tcp`，仅显示 `8000/tcp` 不算成功；
- `worker`：`Up`；
- `model-gateway`：`Up (healthy)`；
- `provider-worker`：`Up`，它会等待 Gateway 健康后才启动。

验证宿主机入口：

```bash
curl http://127.0.0.1:8000/health
```

预期返回 `{"status":"ok","database":"ok"}`。

### 2.1 更新代码或配置后重新部署

先做不输出展开后密钥的配置检查：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia config -q
```

然后重建镜像和全部四个服务：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia up -d --build --force-recreate
```

若升级修改了网络拓扑，或 `api` 仍只显示 `8000/tcp`，先删除旧容器和网络再重建：

```bash
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia down
docker compose --env-file .env -f docs/deployment/compose.yaml \
  --profile nvidia up -d --build --force-recreate
```

`down` 不删除命名数据卷；绝对不要添加 `-v`，除非确实要永久删除 SQLite 和全部
Artifact。

### 2.2 Compose 网络边界

- API 与 Discovery Worker 通过 `contribos-app-local` 私有网络通信；
- API 额外连接一个只有 API 使用的 `contribos-api-host-local` bridge，因为 OrbStack
  不会为仅连接 `internal: true` 网络的容器落实宿主机端口发布；
- API 的宿主机端口仍严格绑定 `127.0.0.1:8000`；
- Provider Worker 只连接 `contribos-model-gateway-local` 内部网络；
- Model Gateway 同时连接模型内部网络与独立出网网络，且是唯一持有
  `NVIDIA_API_KEY` 的服务。

### 2.3 常见启动故障

| 现象 | 原因与处理 |
| --- | --- |
| Gateway 反复 `Restarting`，日志提示 `MODEL_GATEWAY_SIGNING_KEY is required` | 启动命令漏了 `--env-file .env`，或该值为空/不是至少 64 位偶数长度十六进制；修正后 `up -d --force-recreate` |
| `provider-worker` 没出现 | 它在等待 `model-gateway` 健康；先检查 Gateway 日志和两个服务使用的同一签名密钥 |
| API 为 `healthy`，但 `curl 127.0.0.1:8000` 失败，`PORTS` 只有 `8000/tcp` | 仍是旧的 internal-only 网络容器；用上面的 `down`（不带 `-v`）再 `up`，确认端口显示完整宿主机映射 |
| 修改 `.env` 后没有生效 | `restart` 不会更新容器环境；必须使用 `up -d --force-recreate` |
| 点击 AI 筛选后各项都失败，Gateway 日志有 `RemoteProtocolError: Server disconnected without sending a response` | 这是 NVIDIA 上游在返回响应前断开连接，不是超时或本地配置错误。更新到含有传输重试修复的镜像并重建；Gateway 会对这类错误做最多两次有界重试，持续发生时检查 NVIDIA Build 服务状态、额度和本机网络后再重试该批任务。 |
| 宿主机调用 MiniMax 成功，但 Gateway 的 `POST /v1/chat/completions` 返回 `502` | Gateway 默认不会继承宿主机代理。若宿主机依赖无认证 CONNECT 代理，在 `.env` 设置仅供 Gateway 使用的 `NVIDIA_HTTPS_PROXY`；OrbStack 的宿主机本地代理要写成 `http://host.docker.internal:<port>`，然后 `up -d --build --force-recreate`。Gateway 会关闭每次上游连接并在 360 秒总预算内最多重试四次，避免 Inspect 后复用空闲代理隧道。 |
| Gateway 直连在 10 秒内超时，代理路径又在 360 秒后报 `nvidia_nim_network_protocol` | Docker/OrbStack 到 NVIDIA 的出网路径尚不可用；这不是候选、提示词或 Schema 问题。当前 Gateway 在显式代理时使用 NVIDIA 官方示例同类的 `requests` 传输。重建后先执行“验证 1 个样例”；若仍失败，需要为 Docker/OrbStack 配置一个能持续 HTTPS CONNECT 到 `integrate.api.nvidia.com:443` 的无认证代理，或由网络管理员放通直连。不要在样例成功前启动 Top 5。 |
| 宿主机按 Kimi K3 官方 SSE 示例直连后收到 `504`、没有任何 `data:` 事件 | 这是已观察到的 Kimi 服务端不可用现象；当前分析默认使用 Nemotron 3.5 Lightning。重建后先在页面“验证 1 个样例”；不要在样例成功前启动批量。 |

不要运行不带 `-q` 的 `docker compose config` 并把输出粘贴到 Issue 或聊天中，展开后
的配置可能包含 NVIDIA、GitHub 和本地访问密钥。

Compose 模式可直接运行真实机会分析；也可让 MiniMax 审查由 Fake 执行生成的精确 diff
和测试 Artifact。它不会创建真实 PR。

## 3. 完整 Vibe Coding：分离的本机进程

真实 Explore/Implement/Verify 需要专用 Sandbox Worker 操作本机 Docker Engine。
当前 Compose 应用镜像刻意不带 Docker CLI，也不挂载 Docker socket；完整链路应在
开发机上用五个权限不同的进程启动。先安装项目并准备公共非密钥配置：

```bash
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e '.[dev]'

export DATABASE_URL='sqlite:///./data/contribos.db'
export ARTIFACT_ROOT='./data/artifacts'
export ANALYSIS_PROVIDER='nvidia_nim'
export ANALYSIS_MODEL='nvidia/nemotron-3.5-lightning-30b-a3b'
export IMPLEMENTATION_PROVIDER='nvidia_nim'
export IMPLEMENTATION_MODEL='deepseek-ai/deepseek-v4-pro-0813'
export REVIEW_PROVIDER='nvidia_nim'
export REVIEW_MODEL='minimaxai/minimax-m3'
export SANDBOX_STAGE_RUNTIME='docker'
export SANDBOX_JOB_SPEC_KEY_ID='local-v1'
export SANDBOX_JOB_SPEC_SIGNING_KEY='<另一份 openssl rand -hex 32 输出>'
export MODEL_GATEWAY_BASE_URL='http://127.0.0.1:8001/v1'
export MODEL_GATEWAY_NETWORK='contribos-model-gateway-local'
export MODEL_GATEWAY_SERVICE_NAME='contribos-model-gateway'
```

然后分别启动：

1. API：需要本地访问令牌和 Sandbox JobSpec 签名密钥，不需要 NVIDIA key 或 Docker
   socket。
2. Discovery Worker：需要只读 GitHub token，不需要 NVIDIA key 或 Docker socket。
3. Model Gateway：只设置 `NVIDIA_API_KEY` 和 `MODEL_GATEWAY_SIGNING_KEY`，运行
   `contribos model-gateway`。
4. Provider Worker：只设置 `MODEL_GATEWAY_SIGNING_KEY`，运行
   `contribos provider-worker`；不要设置 NVIDIA/GitHub/本地访问/Sandbox 密钥。
5. Sandbox Worker：只保留数据库、Artifact、Docker 环境和 JobSpec 签名密钥，运行
   `contribos sandbox-worker`；明确取消 NVIDIA、GitHub 和本地访问密钥。

示例（Sandbox Worker 终端）：

```bash
unset NVIDIA_API_KEY MODEL_GATEWAY_SIGNING_KEY GITHUB_TOKEN GH_TOKEN LOCAL_ACCESS_TOKEN
contribos sandbox-worker
```

Model Gateway 终端：

```bash
unset GITHUB_TOKEN GH_TOKEN LOCAL_ACCESS_TOKEN SANDBOX_JOB_SPEC_SIGNING_KEY
export NVIDIA_API_KEY='<NVIDIA Build API key>'
export MODEL_GATEWAY_SIGNING_KEY='<任务令牌 HMAC 密钥>'
contribos model-gateway --host 127.0.0.1 --port 8001
```

Provider Worker 终端不应设置 `NVIDIA_API_KEY`：

```bash
unset NVIDIA_API_KEY GITHUB_TOKEN GH_TOKEN LOCAL_ACCESS_TOKEN SANDBOX_JOB_SPEC_SIGNING_KEY
export MODEL_GATEWAY_SIGNING_KEY='<同一任务令牌 HMAC 密钥>'
contribos provider-worker
```

启动 Sandbox Worker 前先用精确 digest 验证 Runner：

```bash
contribos doctor --runner-image sha256:<64 位小写十六进制摘要>
```

页面流程是：Explore 成功后冻结批准路径 → 与 DeepSeek 多轮对话 → 生成不可变
ChangeSet → 用户确认页面显示的精确 ChangeSet 哈希 → Sandbox Implement/Verify →
MiniMax 对精确 diff 与测试 Artifact 独立 Review。模型输出本身从不构成执行、GitHub
写入或发布授权。

## 4. 当前边界

- NVIDIA 接入不会自动分析所有候选；页面仍由用户手动触发 Top 5 或单个分析。
- 验证容器默认 `network=none`，不持有模型、GitHub 或宿主机密钥。
- 真实 Draft PR Publisher 未接入；现有发布路径仍是明确标注的本地 Fake。
- Linux amd64 + Docker Engine 的 P5-G06 仍需在真实 Linux 环境运行同一恶意套件，
  不能用 macOS 模拟结果代替。
