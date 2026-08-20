#!/usr/bin/env bash
# 一键启动：构建终端前端、本地 web 服务 + 可选 frpc 穿透
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
else
  echo "错误: 未找到 .env，请从 .env.example 复制并设置 ADMIN_PASSWORD"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "创建虚拟环境并安装依赖…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi

if [ ! -d frontend/dist ]; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "错误: 前端尚未构建，且系统中找不到 npm"
    exit 1
  fi
  echo "构建终端前端…"
  if [ ! -d frontend/node_modules ]; then
    npm --prefix frontend install
  fi
  npm --prefix frontend run build
fi

./.venv/bin/python scripts/build_runtime_bundle.py \
  --source . \
  --output-dir runtime_dist

if [ -x bin/frpc ]; then
  echo "启动 frpc…"
  ./bin/frpc -c frpc.toml &
  FRPC_PID=$!
  trap 'kill $FRPC_PID 2>/dev/null || true' EXIT
else
  echo "提示: bin/frpc 不存在，跳过内网穿透（可运行 scripts/install_frp.sh 安装）"
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-18088}"

echo "启动 web 服务 ($HOST:$PORT)…"
exec ./.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
