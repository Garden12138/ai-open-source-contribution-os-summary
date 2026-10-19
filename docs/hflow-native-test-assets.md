# 固定原生测试资源与 HFlow 适配

状态：用户于 2026-10-16 授权实施固定资源准备及应用内下载授权。
首个适配是此冻结 HFlow 归档的 FFmpeg / ffprobe；应用只接受内置目录中的精确
资源，不接受用户、模型或仓库传入下载 URL。实际资源下载仍须应用内单独确认。

任务 `ac50e857-e176-434e-94ef-677fbc00f4cf` 的冻结源码为
`6577350d54ff1ffd9acebf491b57d6d7c2e6375d`。wheel 与 Python 开发依赖已在
真实隔离环境准备成功。直接运行 ruff check、ruff format --check、ty check
通过；pytest 修改前基线因项目夹具尝试下载 FFmpeg 而失败。
不得通过开放 Verify 网络、跳过相关测试或替换测量工具版本解决。

## 固定来源

项目 `src/hflow/ffmpeg/_binary.py` 固定以下版本与哈希：

- Release：`autobuild-2026-08-31-13-27`。
- Version：`n8.1.2-50-g1a748fe2cd`。
- Linux arm64：[资源](https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-08-31-13-27/ffmpeg-n8.1.2-50-g1a748fe2cd-linuxarm64-gpl-8.1.tar.xz)，
  SHA-256 `ae5da4f51b9052390f414005f8ab26c1eed1268f327cce7cb79aa076b29bd66e`。
- Linux amd64：[资源](https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-08-31-13-27/ffmpeg-n8.1.2-50-g1a748fe2cd-linux64-gpl-8.1.tar.xz)，
  SHA-256 `c733b4b2951e5957e15505f788b2c65a7a41b6da4b289e295852cc38079b4d2b`。

以上是冻结源码中的声明，尚未代表资源已下载、哈希已验证或实际测试通过。

## 执行边界

1. 在独立、无凭据的准备步骤读取精确资源。只允许上述固定 URL，以及经过校验的
   GitHub release-assets HTTPS 跳转；禁止任意 URL、用户信息、非标准端口、IP
   地址和跨域凭据转发。每次 TLS 连接只连接已验证的公网 DNS 地址并保留主机名
   证书校验；仅查询 IPv4（`AF_INET`），并拒绝所选地址族中任一非公网地址。
   不继承代理或凭据。下载容器不挂载仓库或数据库，也不执行项目代码。
   应用默认不下载，30 分钟的精确预览由用户一次性授权后才生成持久任务。
2. 下载、时长、内存、磁盘和展开大小均有限制；建议单归档最多 256 MiB、两项
   必要文件合计最多 512 MiB、单步最多 600 秒。归档核对完整 SHA-256 后，仅
   提取项目固定目录中的 `ffmpeg`、`ffprobe` 普通文件；拒绝符号链接、硬链接、
   重复成员、路径越界和异常膨胀。下载和解包分别运行，解包容器断网。
3. 持久记录归档、两个二进制、来源、平台与准备策略的哈希，将其绑定到环境
   和新的执行确认。不要用旧批准启动带新资源的任务。
4. Verify 继续 `network=none`、只读根、非 root、无额外权限与原有配额。资源
   通过窄范围只读挂载提供。环境仅允许固定 `HFLOW_FFMPEG` / `HFLOW_FFPROBE`
   指向已验证资源，不能提供任意环境变量或宿主路径。
5. 下载授权不批准执行。资源就绪会改变环境哈希；用户重新核对当前子方案后，
   单独确认执行，既有执行流程先运行真实 curation/CLI 修改前测试，再开始改动。
   记录通过、失败和跳过，只有随后实施、修改后测试及独立审查成功才可验收。

## API 与持久化

- `POST /api/v1/tasks/{id}/workbench/native-resources/prepare` 接受空 JSON 对象，
  需要 Origin/CSRF 或本地访问令牌，以及 `Idempotency-Key`；仅创建预览，不下载。
- `POST .../native-resources/confirm` 需要精确 `preview_hash`、`nonce`、`approved:true`；
  拒绝额外 URL、平台、路径、环境变量等输入。确认重复提交返回同一 Job。
- Workbench 响应新增 `native_resources`，展示待授权、排队、运行、失败及就绪状态。
  未授权、过期、方案/上下文变化、待处理消息、暂停或忙碌时不能新建下载任务。
- `native_resource_prepared → native_resource_confirmed → native_resource_ready` 使用现有
  不可变 WorkbenchEvent 链；确认和 Job 原子提交，同时追加审计记录。无 SQL 模型变更。
  现有签名依赖计划、依赖结果及工作区增加可选 `native_resource`；未设置时不进入
  旧计划/结果的 wire payload，旧哈希保持不变。资源回执与文件哈希同时绑定到计划、
  最终依赖清单、Verify 输入和新的执行确认。
- 缓存位于制品根下 `native-resources/<资源规范哈希>/`。命中需要不可变历史回执，
  并重新核对两个文件的大小、类型及 SHA-256；孤立目录不是有效回执。缓存损坏
  时只有新授权任务可以重新下载、校验并替换。不能把下载成功当作测试通过。
- 每任务最多 32 次预览，任务仅尝试一次；失败、超时或取消后须新预览、新确认。
  排队过期不会下载。持久分配记录用于回收终态任务残留容器和专属临时目录；
  下载完成与写入 ready 之间再次检查任务租约、停止事件和冻结依据。

## 验证及回退

覆盖 URL/跳转限制、无凭据、哈希不符、归档逃逸、大小/时间限制、平台不符、
陈旧授权、重启/清理、只读挂载以及断网实际测试。Linux 原生平台门槛仍须独立
验证。回退时停用新能力，排空或安全停止新任务，保留制品和历史记录；已有
资产不能被当作当前批准。回退镜像前排空新资源任务及消费资源的执行任务，保留
事件、回执和缓存；旧 API/Worker 不得消费新资源绑定。资源不存在或下载/验证失败
必须保留阻塞，不能替换版本。macOS/OrbStack 的资源夹具验收不等于真实 FFmpeg
已下载或 HFlow 基线通过；Linux 原生门槛 P5-G06 仍开放。

2026-10-16 生产修复：实际下载容器的 DNS 同时返回公网 IPv4 与 `2001:2::/48`
合成 IPv6，旧 `AF_UNSPEC` 查询因整组结果含非公网地址而拒绝下载。下载器明确
使用 IPv4 传输后，仍校验该族所有地址、固定连接 IP、TLS 主机名和每次跳转。
不允许合成 IPv6、私网 IPv4、元数据地址或任意新来源。真实容器只读响应头检查
已验证 `github.com → release-assets.githubusercontent.com` 为 302 → 200，正文为
107695184 字节，探针未读取正文；这不表示制品已下载或 SHA-256 校验通过。
