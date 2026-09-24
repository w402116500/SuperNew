#!/usr/bin/env bash
# 兼容旧名称，实际执行 ./stop.sh
exec "$(dirname "${BASH_SOURCE[0]}")/stop.sh" "$@"
