#!/usr/bin/env bash
# 兼容旧名称，实际执行 ./start.sh
exec "$(dirname "${BASH_SOURCE[0]}")/start.sh" "$@"
