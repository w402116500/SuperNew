#!/usr/bin/env bash
# 启动服务器上的 SuperMew：只拉起 app、PostgreSQL、Redis。
# 不会启动本地 Milvus，也不会重建或删除数据卷。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

compose_file="${COMPOSE_FILE:-docker-compose.prod.yml}"
rebuild=0

usage() {
  cat <<'EOF'
用法:
  ./start.sh           启动已有镜像
  ./start.sh --build   重新构建应用镜像后再启动

只操作当前目录的 docker-compose.prod.yml，不会停止其他 Docker 项目。
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
    --build)
      rebuild=1
      ;;
    -v|--volumes|down|rm)
      echo "start.sh 不接受 '$arg'。停止请用 ./stop.sh，它不会删除数据卷。" >&2
      exit 1
      ;;
    *)
      echo "不支持的参数: $arg" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "$compose_file" ]]; then
  echo "找不到 $compose_file，请在 /opt/supermew 下运行。" >&2
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "找不到 .env。请先按 .env.server.example 配好服务器配置，不要把密钥写进脚本。" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker 命令。" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "未找到 docker compose。" >&2
  exit 1
fi

app_port=18050
if app_port_value="$(grep -E '^APP_HOST_PORT=' .env | tail -n 1 | cut -d= -f2- | tr -d '[:space:]' | tr -d '"' | tr -d "'")"; then
  if [[ -n "${app_port_value}" ]]; then
    app_port="$app_port_value"
  fi
fi

echo "正在启动 SuperMew（$compose_file）..."
if [[ "$rebuild" -eq 1 ]]; then
  docker compose -f "$compose_file" up -d --build
else
  docker compose -f "$compose_file" up -d
fi

echo "等待应用健康检查..."
deadline=$((SECONDS + 90))
while (( SECONDS < deadline )); do
  if curl -fsS "http://127.0.0.1:${app_port}/health" >/dev/null 2>&1; then
    echo "应用健康检查通过: http://127.0.0.1:${app_port}/health"
    docker compose -f "$compose_file" ps
    echo "外网入口: https://rag.infoxy.xyz/"
    exit 0
  fi
  sleep 2
done

echo "启动命令已发出，但 90 秒内没有通过健康检查。当前容器状态：" >&2
docker compose -f "$compose_file" ps >&2
exit 1
