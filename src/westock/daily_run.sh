#!/bin/bash
# 策略因子平台 · 每日跑批
# 1) 用内置金融服务更新全市场行情  2) 计算各策略最新信号
# 3) BOCI 五维行业情绪             4) BOCI 机会池 + 历史交互页
# 5) 每日可买 ETF 清单             6) 生成 HTML 报告
# 7) 发布到 GitHub Pages           8) 推送飞书（含跳转按钮）
#
# 用法: bash daily_run.sh [回溯天数]
# 环境变量（可选）:
#   PYTHON_BIN        python 解释器路径（默认 python3）
#   NODE_BIN          node 解释器路径（默认自动探测）
#   WESTOCK_CLI_JS    westock CLI 的 index.js 路径（默认本机 workspace）
#   GITHUB_TOKEN      发布 GitHub Pages 用（默认读 /tmp/ghtoken 或 .ghtoken）

set -u
DAYS="${1:-150}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# python 解释器探测：PYTHON_BIN > 本机 quant-data venv > PATH 里的 python3
PY=""
for cand in "${PYTHON_BIN:-}" \
            /Users/beanpaper/.workbuddy/binaries/python/envs/quant-data/bin/python \
            "$(command -v python3)"; do
  if [ -n "$cand" ] && [ -x "$cand" ]; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
  echo "[fatal] 找不到 python 解释器，请设 PYTHON_BIN"
  exit 1
fi

cd "$DIR" || exit 1
echo "===== 每日跑批 $(date '+%F %T') ====="
echo "python: $PY"
echo "workdir: $DIR"

echo "--- [1/8] 更新行情（内置金融服务 westock）---"
"$PY" -c "
import sys; sys.path.insert(0,'$DIR')
from westock_source import refresh_universe
print(refresh_universe(days=$DAYS, workers=4))
" || echo "[warn] 行情更新异常，继续使用缓存"

echo "--- [2/8] 计算策略信号 ---"
"$PY" run_latest.py || { echo "[fatal] 信号计算失败"; exit 1; }

echo "--- [3/8] BOCI 五维行业情绪 ---"
"$PY" boci_backfill.py || echo "[warn] BOCI 计算异常"

echo "--- [4/8] BOCI 机会池 + 交互页 ---"
"$PY" boci_history.py || echo "[warn] 机会池异常"
"$PY" boci_view.py    || echo "[warn] 交互页生成异常"

echo "--- [5/8] 每日可买 ETF 清单 ---"
"$PY" etf_mapping.py || echo "[warn] ETF 映射异常"

echo "--- [6/8] 生成 HTML 报告 ---"
"$PY" build_report.py

echo "--- [7/8] 发布到 GitHub Pages ---"
"$PY" publish_pages.py || echo "[warn] Pages 发布异常（卡片按钮可能指向旧版本）"

echo "--- [8/8] 推送飞书 ---"
"$PY" feishu_push.py

echo "===== 完成 $(date '+%F %T') ====="
