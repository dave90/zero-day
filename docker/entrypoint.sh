#!/usr/bin/env bash
# Container entrypoint: start the tool-RPC server on TCP. The orchestrator
# discovers the published host port via `docker port` after `docker run`.
#
# Run from /work so tools that use Path.cwd() (notes, etc.) land in the
# bind-mounted project tree, not inside the image.
set -euo pipefail

PORT="${ZERO_RPC_PORT:-7333}"

cd /work
exec uvicorn server:app \
    --app-dir /opt/zero_tool_server \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level info \
    --workers 1
