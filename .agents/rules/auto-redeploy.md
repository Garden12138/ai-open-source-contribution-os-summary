# 自动重新部署与运行服务约定

每次修改完代码后，必须自动重新部署并运行服务，确保最新的代码变更在本地服务中即时生效：

1. **容器化部署（Docker Compose）**：
   - 当检测到容器正在运行或存在 `.env` 配置文件时，执行构建并后台重新部署容器：
     ```bash
     docker compose --env-file .env -f docs/deployment/compose.yaml up -d --build
     ```
     或直接运行：
     ```bash
     ./scripts/deploy.sh
     ```
   - 检查容器运行状态与健康状态：
     ```bash
     docker compose --env-file .env -f docs/deployment/compose.yaml ps
     curl -s http://127.0.0.1:8000/health
     ```
   - 确保 `api` 和 `model-gateway` 均处于 `healthy` 状态，相关 worker 处于运行中。

2. **本地原生 Python 进程**：
   - 若未使用 Docker Compose 而是本地直接运行，确保在代码修改完成后重启 `contribos serve` 和相关的后台 `worker`。
