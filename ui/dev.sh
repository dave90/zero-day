#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
uv sync --project ui/server
if [ ! -d ui/web/node_modules ]; then npm --prefix ui/web ci; fi
uv run --project ui/server zero-web --dev-origin http://127.0.0.1:4319 &
service_pid=$!
trap 'kill "$service_pid" 2>/dev/null || true' EXIT INT TERM
npm --prefix ui/web run dev -- --port 4319 --strictPort
