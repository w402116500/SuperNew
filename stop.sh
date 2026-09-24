#!/usr/bin/env bash
# 停止服务器上的 SuperMew 容器。
# 只停止，不删除容器、镜像或 volumes/postgres、volumes/redis。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

compose_file="${COMPOSE_FILE:-docker-compose.prod.yml}"

usage() {
  cat <<'EOF'
用法:
  ./stop.sh     停止 app、PostgreSQL、Redis

不会执行 docker compose down，也不会带 -v 删除数据卷。
不会停止本机其他 Docker 项目，也不会改 Nginx。
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
    -v|--volumes|down|rm|--rmi)
      echo "stop.sh 拒绝 '$arg'，避免误删数据卷或容器。" >&2
      echo "如需停止服务，直接运行: ./stop.sh" >&2
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

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker 命令。" >&2
  exit 1
fi

echo "正在停止 SuperMew（只停止容器，保留数据卷）..."
docker compose -f "$compose_file" stop
docker compose -f "$compose_file" ps
echo "已停止。再次启动请运行: ./start.sh"
