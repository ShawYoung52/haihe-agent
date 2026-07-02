#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../haiheliuyubaoyuagent-master/chainlitexam"

if [ ! -d ".venv" ]; then
  python -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
pip install -r requirements.txt

if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "已创建 .env，请先根据实际环境修改后再启动。"
  exit 1
fi

: "${CHAINLIT_HOST:=0.0.0.0}"
: "${CHAINLIT_PORT:=8003}"

chainlit run chainlit_app.py --host "$CHAINLIT_HOST" --port "$CHAINLIT_PORT"
