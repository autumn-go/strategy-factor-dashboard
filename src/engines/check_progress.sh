#!/bin/bash
# 进度检查脚本
LOG="/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/backfill.log"
DB="/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/factors.db"

PID=$(ps aux | grep "backfill_2025.py" | grep -v grep | awk '{print $2}')

echo "=== backfill 进度检查 ==="
echo "时间: $(date '+%H:%M:%S')"

if [ -n "$PID" ]; then
    # 进程还在运行
    PROGRESS=$(tail -100 "$LOG" 2>/dev/null | grep "INFO:ews_engine:进度" | tail -1)
    CPU=$(ps -p "$PID" -o "%cpu" 2>/dev/null | tail -1 | tr -d ' ')
    echo "状态: ⏳ 运行中 (PID=$PID, CPU=${CPU}%)"
    if [ -n "$PROGRESS" ]; then
        echo "进度: $PROGRESS"
    else
        echo "进度: 正在计算前 5 个交易日..."
    fi
else
    # 进程已结束
    echo "状态: ✅ 已完成"
    if [ -f "$DB" ]; then
        COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM ews_daily;" 2>/dev/null || echo "N/A")
        MIN_DATE=$(sqlite3 "$DB" "SELECT MIN(trade_date) FROM ews_daily;" 2>/dev/null || echo "N/A")
        MAX_DATE=$(sqlite3 "$DB" "SELECT MAX(trade_date) FROM ews_daily;" 2>/dev/null || echo "N/A")
        echo "数据量: $COUNT 条"
        echo "日期范围: $MIN_DATE ~ $MAX_DATE"
    fi
    echo ""
    echo "=== 日志最后 10 行 ==="
    tail -10 "$LOG" 2>/dev/null
fi
