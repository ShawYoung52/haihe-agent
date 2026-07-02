#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp"

if [ ! -d ".venv" ]; then
  python -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install -r requirements.txt

: "${MCP_HOST:=0.0.0.0}"
: "${MCP_PORT:=3333}"

python main.py --host "$MCP_HOST" --port "$MCP_PORT"
