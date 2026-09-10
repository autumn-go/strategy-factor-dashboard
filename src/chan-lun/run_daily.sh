#!/bin/bash
# 一键跑当日盘后扫描 + 飞书推送(交易日判断内置, 非交易日自动跳过)
# 用法: bash run_daily.sh [--date YYYY-MM-DD] [--no-push]
cd "$(dirname "$0")" || exit 1

# venv 定位优先级: $CHAN_VENV > 本机托管默认路径(chan09b) > 项目内 .venv
VENV="${CHAN_VENV:-}"
if [ -z "$VENV" ]; then
  if [ -x "$HOME/.workbuddy/binaries/python/envs/chan09b/bin/python" ]; then
    VENV="$HOME/.workbuddy/binaries/python/envs/chan09b"
  else
    VENV="$PWD/.venv"
  fi
fi

# 加载 .env(若存在): CHAN_FEISHU_MODE / CHAN_FEISHU_WEBHOOK 等
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "[run_daily] 未找到 python 环境($VENV), 请先执行 bash setup.sh 或设置 CHAN_VENV" >&2
  exit 2
fi
exec "$VENV/bin/python" run_daily.py "$@"
