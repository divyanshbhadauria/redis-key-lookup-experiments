#!/usr/bin/env bash
# Usage: scripts/redis.sh start|stop|status|cli [args...]
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BIN="$ROOT/redis/src"
cd "$ROOT" && mkdir -p data   # config uses paths relative to the repo root
case "${1:-}" in
  start)  "$BIN/redis-server" "$ROOT/redis-experiments.conf"
          for _ in $(seq 20); do "$BIN/redis-cli" ping >/dev/null 2>&1 && break; sleep 0.2; done
          echo "redis $("$BIN/redis-cli" info server | grep -E 'redis_version' | tr -d '\r') up on :6379" ;;
  stop)   "$BIN/redis-cli" shutdown nosave 2>/dev/null || true; echo stopped ;;
  status) "$BIN/redis-cli" ping ;;
  cli)    shift; exec "$BIN/redis-cli" "$@" ;;
  *)      echo "usage: $0 start|stop|status|cli"; exit 1 ;;
esac
