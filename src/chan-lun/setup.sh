#!/bin/bash
# 缠论盘后扫描器 一键初始化:
#   1) 建 venv + 装依赖(czsc 0.8.30 --no-deps 是重点, 见 README)
#   2) 重建股票池(沪深300+中证500+中证1000)
#   3) 冒烟: 交易日判断 + 扫描接口是否可用
# 可用环境变量覆盖: CHAN_PYTHON(建venv的python3) / CHAN_VENV(venv路径)
set -e
cd "$(dirname "$0")" || exit 1

# 选一个可用的 python3 建 venv: $CHAN_PYTHON > 本机托管版本 > PATH
PY_SRC="${CHAN_PYTHON:-}"
if [ -z "$PY_SRC" ]; then
  for c in "$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3" \
           "$HOME/.workbuddy/binaries/python/versions/3.14.3/bin/python3" \
           "$(command -v python3 2>/dev/null)"; do
    if [ -n "$c" ] && [ -x "$c" ]; then PY_SRC="$c"; break; fi
  done
fi
if [ -z "$PY_SRC" ]; then
  echo "[setup] 未找到 python3, 请安装后重试或设置 CHAN_PYTHON" >&2
  exit 2
fi

# venv 定位: $CHAN_VENV > 本机托管默认路径(chan09b) > 项目内 .venv
VENV="${CHAN_VENV:-}"
if [ -z "$VENV" ]; then
  if [ -x "$HOME/.workbuddy/binaries/python/envs/chan09b/bin/python" ]; then
    VENV="$HOME/.workbuddy/binaries/python/envs/chan09b"
  else
    VENV="$PWD/.venv"
  fi
fi

echo "==> [1/3] Python venv ($VENV)"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY_SRC" -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
# czsc 0.8.30: 官方依赖太重/装不上, 用 --no-deps 手动补最小集
"$VENV/bin/pip" install -q "czsc==0.8.30" --no-deps
"$VENV/bin/pip" install -q "pandas<3" requests matplotlib loguru scikit-learn scipy Deprecated
"$VENV/bin/python" -c "import pandas,czsc,requests,matplotlib;print('   deps OK',pandas.__version__,czsc.__version__)"

echo "==> [2/3] 重建股票池"
bash update_pool.sh

echo "==> [3/3] 冒烟: 交易日判断 + 模块导入"
"$VENV/bin/python" - <<'PY'
import os, sys
sys.path.insert(0, os.getcwd())
from core import fetch_kline, chan_scan, report_gen, config as C
lb = fetch_kline.last_bar_date(C.MARKET_INDEX)
print(f"   上证最近K线日期={lb} 池={len(open(C.POOL_TSV).readlines())}行 模块导入OK")
PY
echo "==> 完成。每日盘后运行: bash run_daily.sh (或 python run_daily.py --date <交易日>)"
