#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
补充计算 2025 年 EW-SDM 因子数据，并重新计算全量回测
"""
import sys, os, json
sys.path.insert(0, '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform')

from ews_engine import run_full_calculation, compute_backtest
from server import get_factors_db, _save_results
import sqlite3
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('backfill_2025')

FACTORS_DB = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/factors.db'

def load_all_results(conn):
    """从 ews_daily 表加载所有因子数据"""
    all_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM ews_daily ORDER BY trade_date"
    ).fetchall()]
    
    all_results = []
    for d in all_dates:
        rows = conn.execute("""
            SELECT trade_date, concept_code, concept_name, sector_type,
                   momentum, emotion_diff, s_score, final_score,
                   total_stocks, up_count, limit_up_count, broken_count, max_streak
            FROM ews_daily WHERE trade_date = ?
        """, (d,)).fetchall()
        for r in rows:
            all_results.append(dict(r))
    
    return all_results


def main():
    print("=" * 60)
    print("补充计算 2025 年 EW-SDM 因子")
    print("=" * 60)
    
    # Step 1: 计算 2025 年因子
    print("\n[1/4] 计算 2025 年因子 (20250101 ~ 20251231)...")
    results_2025, _ = run_full_calculation(
        start_date='20250101',
        end_date='20251231',
        progress_cb=None  # ews_engine 内部已有 logger.info() 输出进度
    )
    print(f"  ✓ 完成: {len(results_2025)} 条因子记录")
    
    # Step 2: 保存 2025 年因子到数据库（先删旧数据，再插新数据，避免重复）
    print("\n[2/4] 保存 2025 年因子到数据库...")
    conn = get_factors_db()
    # 先删除 2025 年的旧数据
    conn.execute("DELETE FROM ews_daily WHERE trade_date >= '20250101' AND trade_date <= '20251231'")
    conn.commit()
    _save_results(conn, results_2025, None)  # None = 不保存回测
    conn.close()
    print(f"  ✓ 已保存")
    
    # Step 3: 加载全量因子数据，重新计算回测
    print("\n[3/4] 加载全量因子数据，重新计算回测...")
    conn = get_factors_db()
    all_results = load_all_results(conn)
    conn.close()
    print(f"  ✓ 已加载 {len(all_results)} 条因子记录")
    
    bt_all = compute_backtest(all_results, sector_type=None)
    bt_concept = compute_backtest(all_results, sector_type='concept')
    bt_industry = compute_backtest(all_results, sector_type='industry')
    backtest = {'all': bt_all, 'concept': bt_concept, 'industry': bt_industry}
    print(f"  ✓ 回测计算完成: all={len(bt_all)}, concept={len(bt_concept)}, industry={len(bt_industry)}")
    
    # Step 4: 保存回测数据（会删除旧的，写入完整的）
    print("\n[4/4] 保存回测数据...")
    conn = get_factors_db()
    _save_results(conn, [], backtest)
    conn.close()
    print(f"  ✓ 已保存")
    
    # 验证
    print("\n验证结果:")
    conn = sqlite3.connect(FACTORS_DB)
    cnt_daily = conn.execute("SELECT COUNT(*) FROM ews_daily").fetchone()[0]
    min_d = conn.execute("SELECT MIN(trade_date) FROM ews_daily").fetchone()[0]
    max_d = conn.execute("SELECT MAX(trade_date) FROM ews_daily").fetchone()[0]
    cnt_bt = conn.execute("SELECT COUNT(*) FROM ews_backtest").fetchone()[0]
    min_bt = conn.execute("SELECT MIN(signal_date) FROM ews_backtest").fetchone()[0]
    max_bt = conn.execute("SELECT MAX(signal_date) FROM ews_backtest").fetchone()[0]
    conn.close()
    
    print(f"  ews_daily:  {cnt_daily} 条, 范围 {min_d} ~ {max_d}")
    print(f"  ews_backtest: {cnt_bt} 条, 范围 {min_bt} ~ {max_bt}")
    print("\n完成！现在图表应该能显示近一年的数据了。")


if __name__ == '__main__':
    main()
