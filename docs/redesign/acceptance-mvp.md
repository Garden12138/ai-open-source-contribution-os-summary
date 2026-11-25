# 产品验收

## 2026-11-25 · M8 Linux amd64

在 macOS arm64 宿主的 OrbStack 中运行 Debian 12 amd64 用户态，容器报告 `uname -m=x86_64`、`dpkg --print-architecture=amd64`。构建环境使用 Python 3.11、Rust 1.100 和 Tauri CLI 2.12.6。源码通过文件复制传入，容器没有挂载宿主目录或 Docker socket。

| 验收项 | 结果 |
| --- | --- |
| 后端与离线集成 | `pytest -q` 在 amd64 仿真环境耗时 80.21 秒，339 项通过，9 项可选真实验收跳过。覆盖 ACP 子进程、Git 发布、SQLite 迁移、跟踪和 HTTP 流程。另有一条 Starlette 测试客户端弃用提示。 |
| 桌面外壳 | `cargo test --release --locked` 4 项通过，覆盖回环导航、无 IPC、启动就绪信号和通知。 |
| 前端 | Node 22.23.2 运行 `node --test contribos/web/static/tests`，15 项通过。 |
| deb | 安装 amd64 deb 后以普通用户运行桌面入口。在 Xvfb 和独立 D-Bus 会话中，WebView 的无障碍树出现“发现机会”，回环健康检查和带本地身份的 `/api/stats` 成功，`/api/agents` 返回 OpenCode、OpenHands。 |
| AppImage | 容器缺少 `/dev/fuse`，通过 AppImage 自带的 `--appimage-extract-and-run` 启动。以普通用户验证了同一 WebView 页面与包内后端。FUSE 挂载启动仍待实机验证。 |
| 沙箱集成 | 使用自有测试仓库 `Garden12138/contribos-acceptance-sandbox#1` 运行 `CONTRIBOS_IT_SBX=1 pytest -q -m sandbox`。已有两项真实集成在创建 VM 时失败，CLI 不可用，构建容器也缺少 `/dev/kvm`。另有两项新 Agent 可选验收因未选中对应 Agent 而跳过。 |
| 隔离探针 | 包内 `contribos-backend doctor --probe-sandbox` 返回 `sandbox_policy=error`。回环、内网、包仓库和凭据四项均为 `not_run`。 |

可在本地发行目录打开 [deb（22.34 MiB）](../../desktop/target/release/bundle/deb/ContribOS_2.0.0_amd64.deb) 和 [AppImage（113.46 MiB）](../../desktop/target/release/bundle/appimage/ContribOS_2.0.0_amd64.AppImage)。

Linux 本地 microVM 验收仍需要一台可使用 KVM 的 Ubuntu 24.04+ amd64 主机、已登录的 `sbx` 和任务所需的 Agent 登录。M7 的三个真实 Issue 产品验收继续按清单单列。[Docker Sandboxes Linux 安装要求](https://docs.docker.com/ai/sandboxes/install/)
