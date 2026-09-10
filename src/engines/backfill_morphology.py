#!/usr/bin/env python3
"""回填形态学监控历史数据 - 过去1个月（约22个交易日）"""
import os
import sys
import sqlite3
import time

sys.path.insert(0, os.path.dirname(__file__))
from morphology_engine import run_full_scan

DB_DIR = '/Volumes/BEANPAPER/data/databases'
FACTORS_DB = os.path.join(os.path.dirname(__file__), 'data', 'factors.db')


def get_trade_dates(last_n=22):
    """获取最近N个交易日（跳过已有数据的日期）"""
    conn = sqlite3.connect(os.path.join(DB_DIR, 'industry.db'))
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM ths_daily WHERE ts_code LIKE '8811%' "
        "ORDER BY trade_date DESC LIMIT ?", (last_n,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_existing_dates():
    """获取已有数据的日期"""
    conn = sqlite3.connect(FACTORS_DB)
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM morphology_scan ORDER BY trade_date"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def save_result(result):
    """保存扫描结果到 factors.db"""
    conn = sqlite3.connect(FACTORS_DB)
    for r in result['industries']:
        conn.execute(
            '''INSERT OR REPLACE INTO morphology_scan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                r['date'], r['ts_code'], r['name'], r['daily_return'], r['close'],
                r['tg_high'], r['tg_low'], r['tg_ultra'], r['tg_comp'], r['tg_label'],
                r['sharpe'], r['me_5'], r['me_20'], r['me_20_ma10'],
                r['structure_ratio'], r['ratio_strength'], r['ratio_improve'],
                r['signal_str'], r['kline_str'],
            ))
    conn.commit()
    conn.close()


def main():
    all_dates = get_trade_dates(22)
    existing = get_existing_dates()
    missing = [d for d in all_dates if d not in existing]
    missing.sort()  # 从旧到新回填

    print(f"共 {len(all_dates)} 个交易日，已有 {len(existing)} 天数据，需回填 {len(missing)} 天")
    if not missing:
        print("无需回填")
        return

    total_start = time.time()
    for i, date in enumerate(missing):
        print(f"\n[{i+1}/{len(missing)}] 回填 {date} ...", end=' ', flush=True)
        t0 = time.time()
        try:
            result = run_full_scan(target_date=date)
            if result:
                save_result(result)
                elapsed = time.time() - t0
                print(f"完成 {result['total']}行业 {result['signal_count']}信号 耗时{elapsed:.1f}s")
            else:
                print("无数据")
        except Exception as e:
            print(f"失败: {e}")

    total_elapsed = time.time() - total_start
    print(f"\n回填完成，总耗时 {total_elapsed:.1f}s")

    # 验证
    existing = get_existing_dates()
    print(f"现在共有 {len(existing)} 天数据: {sorted(existing)[0]} ~ {sorted(existing)[-1]}")


if __name__ == '__main__':
    main()
