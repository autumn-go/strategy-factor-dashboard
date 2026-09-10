# -*- coding: utf-8 -*-
"""
EW-SDM 情绪加权扩散动量因子计算引擎

基于同花顺概念板块和行业板块，计算每日 EW-SDM 因子值。
数据源：BEANPAPER 磁盘上的 SQLite 数据库。

核心公式：final_score = momentum * emotion_diff * s_score

momentum:     板块内个股涨跌幅的（成交额+封单）加权平均
emotion_diff: 板块内个股情绪权重的（成交额+封单）加权平均
s_score:      基于 max_streak 的 Sigmoid 连板杠杆
"""

import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import logging
import json
import os

logger = logging.getLogger('ews_engine')

# ==================== 数据库路径 ====================
# 使用本地 APFS 磁盘路径（BEANPAPER 是 exFAT，不支持 SQLite 文件锁）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, 'local_dbs')
os.makedirs(DB_DIR, exist_ok=True)
STOCK_DB = os.path.join(DB_DIR, 'stock_daily.db')
INDUSTRY_DB = os.path.join(DB_DIR, 'industry.db')
LIMIT_DB = os.path.join(DB_DIR, 'limit_data.db')

# ==================== 因子参数 ====================
PARAMS = {
    'trend_threshold': 3.0,       # 趋势阈值（3%），涨幅低于此的非涨停股权重为0
    'broken_drop_threshold': -5.0, # 炸板负反馈跌幅阈值
    'sigmoid_offset': 3.5,         # Sigmoid 中心偏移
}


def load_sector_list(conn_industry):
    """加载概念 + 行业板块列表"""
    df = pd.read_sql_query("""
        SELECT ts_code, name, type, count as stock_count
        FROM ths_index
        WHERE (type = 'N' AND ts_code LIKE '885%')
           OR (type = 'I' AND ts_code LIKE '884%')
        ORDER BY type, ts_code
    """, conn_industry)
    return df


def load_sector_members(conn_industry):
    """加载概念 + 行业成分股关系"""
    df = pd.read_sql_query("""
        SELECT m.ts_code as sector_code, m.con_code as stock_code
        FROM ths_member m
        WHERE m.ts_code IN (
            SELECT ts_code FROM ths_index
            WHERE (type = 'N' AND ts_code LIKE '885%')
               OR (type = 'I' AND ts_code LIKE '884%')
        )
    """, conn_industry)
    return df


def load_daily_prices(conn_stock, trade_date):
    """加载某日全部个股行情"""
    df = pd.read_sql_query(f"""
        SELECT ts_code, trade_date, close, pct_chg, amount
        FROM daily
        WHERE trade_date = '{trade_date}'
    """, conn_stock)
    return df


def load_limit_data(conn_limit, trade_date):
    """加载某日涨停和炸板数据，合并为一张表"""
    limit_up = pd.read_sql_query(f"""
        SELECT ts_code, trade_date, pct_chg, amount, fd_amount,
               limit_times as streak, up_stat, open_times,
               0 as is_broken
        FROM limit_up
        WHERE trade_date = '{trade_date}'
    """, conn_limit)

    limit_broken = pd.read_sql_query(f"""
        SELECT ts_code, trade_date, pct_chg, amount, fd_amount,
               limit_times as streak, up_stat, open_times,
               1 as is_broken
        FROM limit_broken
        WHERE trade_date = '{trade_date}'
    """, conn_limit)

    # 涨停优先（如果同一天既在 limit_up 又在 limit_broken，以 limit_up 为准）
    if len(limit_broken) > 0:
        broken_only = limit_broken[~limit_broken['ts_code'].isin(limit_up['ts_code'])]
        combined = pd.concat([limit_up, broken_only], ignore_index=True)
    else:
        combined = limit_up

    return combined


def calculate_emotion_weight(pct_chg, streak, is_broken):
    """
    计算个股情绪权重

    规则：
    1. 炸板且跌幅超5% → -1.0（强负反馈）
    2. 无涨停：涨幅 > 3% → 1.0，否则 → 0.0
    3. 首板 → 1.5
    4. 连板(n>=2) → 1.0 + 0.5 * n
    """
    if is_broken and pct_chg < PARAMS['broken_drop_threshold']:
        return -1.0

    if pd.isna(streak) or streak <= 0:
        return 1.0 if pct_chg > PARAMS['trend_threshold'] else 0.0

    if streak == 1:
        return 1.5

    return 1.0 + 0.5 * streak


def calculate_s_score(max_streak):
    """基于最高连板数计算 S-Score（Sigmoid 连板杠杆）"""
    if pd.isna(max_streak) or max_streak <= 0:
        return 1.0
    sigmoid = 1.0 / (1.0 + np.exp(-(max_streak - PARAMS['sigmoid_offset'])))
    return 2.0 * sigmoid + 1.0


def compute_daily_factors(trade_date, sector_list, member_map,
                          daily_df, limit_df):
    """
    计算某日所有板块的 EW-SDM 因子值

    返回: list[dict], 每个板块一条记录
    """
    results = []

    for _, sector in sector_list.iterrows():
        code = sector['ts_code']
        name = sector['name']
        sector_type = 'concept' if sector['type'] == 'N' else 'industry'

        # 获取该板块的成分股
        members = member_map.get(code, [])
        if not members:
            continue

        member_df = daily_df[daily_df['ts_code'].isin(members)].copy()
        if len(member_df) == 0:
            results.append({
                'trade_date': trade_date,
                'concept_code': code,
                'concept_name': name,
                'sector_type': sector_type,
                'momentum': 0.0,
                'emotion_diff': 0.0,
                's_score': 1.0,
                'final_score': 0.0,
                'total_stocks': len(members),
                'up_count': 0,
                'limit_up_count': 0,
                'broken_count': 0,
                'max_streak': 0,
            })
            continue

        # 合并涨停数据
        merged = member_df.merge(
            limit_df[['ts_code', 'fd_amount', 'streak', 'is_broken']],
            on='ts_code', how='left'
        )

        # 填充缺失值
        merged['fd_amount'] = merged['fd_amount'].fillna(0)
        merged['streak'] = merged['streak'].fillna(0).astype(int)
        merged['is_broken'] = merged['is_broken'].fillna(0).astype(int)

        # 异常数据处理
        merged['pct_chg'] = pd.to_numeric(merged['pct_chg'], errors='coerce')
        merged['amount'] = pd.to_numeric(merged['amount'], errors='coerce').fillna(0)
        # 过滤 pct_chg 异常值（涨跌停限制 ±20%/±10%，ST ±5%）
        merged['pct_chg'] = merged['pct_chg'].clip(-30, 30)
        # 过滤成交额为负的异常
        merged['amount'] = merged['amount'].clip(lower=0)

        # 计算情绪权重
        merged['emotion_weight'] = merged.apply(
            lambda row: calculate_emotion_weight(
                row['pct_chg'] if pd.notna(row['pct_chg']) else 0,
                row['streak'],
                row['is_broken']
            ), axis=1
        )

        # 计算综合权重 = 成交额 + 封单金额
        merged['weight'] = merged['amount'].fillna(0) + merged['fd_amount']

        # 统计
        total_stocks = len(members)
        up_count = int((merged['pct_chg'] > 0).sum()) if 'pct_chg' in merged else 0
        limit_up_count = int((merged['streak'] > 0).sum())
        broken_count = int(merged['is_broken'].sum())
        max_streak = int(merged['streak'].max()) if len(merged) > 0 else 0

        # 加权计算
        total_weight = merged['weight'].sum()
        if total_weight > 0:
            momentum = (merged['weight'] * merged['pct_chg'].fillna(0)).sum() / total_weight
            emotion_diff = (merged['weight'] * merged['emotion_weight']).sum() / total_weight
        else:
            momentum = 0.0
            emotion_diff = 0.0

        # S-Score
        s_score = calculate_s_score(max_streak)

        # 最终得分
        final_score = momentum * emotion_diff * s_score

        results.append({
            'trade_date': trade_date,
            'concept_code': code,
            'concept_name': name,
            'sector_type': sector_type,
            'momentum': round(momentum, 4),
            'emotion_diff': round(emotion_diff, 4),
            's_score': round(s_score, 4),
            'final_score': round(final_score, 4),
            'total_stocks': total_stocks,
            'up_count': up_count,
            'limit_up_count': limit_up_count,
            'broken_count': broken_count,
            'max_streak': max_streak,
        })

    return results


def compute_backtest(daily_results, sector_type=None):
    """
    基于每日因子排名，回测 Top10 等权策略

    参数:
        daily_results: 全部因子记录
        sector_type: None=全部, 'concept'=仅概念, 'industry'=仅行业

    信号：每天收盘后，做多 EW-SDM 排名前 10 的板块
    收益：次日该板块日行情 pct_chg（用 ths_daily 近似）
    """
    if sector_type:
        filtered = [r for r in daily_results if r.get('sector_type') == sector_type]
    else:
        filtered = daily_results

    if not filtered:
        return []

    # 按日期聚合
    date_map = {}
    for r in filtered:
        d = r['trade_date']
        date_map.setdefault(d, []).append(r)

    sorted_dates = sorted(date_map.keys())

    # 加载板块日行情
    conn_industry = sqlite3.connect(INDUSTRY_DB)
    all_dates_str = "','".join(sorted_dates)

    if sector_type == 'industry':
        code_filter = "AND ts_code LIKE '884%'"
    elif sector_type == 'concept':
        code_filter = "AND ts_code LIKE '885%'"
    else:
        code_filter = ""

    concept_daily = pd.read_sql_query(f"""
        SELECT ts_code, trade_date, pct_chg
        FROM ths_daily
        WHERE trade_date IN ('{all_dates_str}') {code_filter}
    """, conn_industry)
    conn_industry.close()

    concept_return_map = {}
    for _, row in concept_daily.iterrows():
        concept_return_map[(row['ts_code'], row['trade_date'])] = row['pct_chg']

    backtest = []
    cum_return = 1.0

    for i in range(len(sorted_dates) - 1):
        date = sorted_dates[i]
        next_date = sorted_dates[i + 1]

        sectors = date_map[date]
        sectors.sort(key=lambda x: x['final_score'], reverse=True)
        top10 = sectors[:10]

        if len(top10) == 0:
            continue

        returns = []
        for c in top10:
            key = (c['concept_code'], next_date)
            if key in concept_return_map:
                ret = concept_return_map[key]
                if pd.notna(ret):
                    returns.append(ret)

        if returns:
            daily_ret = np.mean(returns) / 100.0
            # 异常日收益裁剪（±20%）
            daily_ret = np.clip(daily_ret, -0.2, 0.2)
            cum_return *= (1 + daily_ret)
            backtest.append({
                'signal_date': date,
                'return_date': next_date,
                'daily_return': round(daily_ret * 100, 4),
                'cum_return': round(cum_return, 4),
                'top10_concepts': [c['concept_name'] for c in top10],
                'top10_codes': [c['concept_code'] for c in top10],
            })

    return backtest


def compute_backtest_rf(daily_results, sector_type=None):
    """
    RF-EWSDM 回测：仅在 Range Filter Buy 区间操作 EW-SDM Top10 等权

    与 compute_backtest 逻辑相同，但加入 Range Filter 过滤：
    - Buy 区间(signal=1)：正常做多 EW-SDM Top10
    - Sell 区间(signal=-1)：空仓，日收益为 0

    参数:
        daily_results: 全部因子记录
        sector_type: None=全部, 'concept'=仅概念, 'industry'=仅行业
    """
    from rf_engine import compute_rf_signals

    if sector_type:
        filtered = [r for r in daily_results if r.get('sector_type') == sector_type]
    else:
        filtered = daily_results

    if not filtered:
        return []

    # 按日期聚合
    date_map = {}
    for r in filtered:
        d = r['trade_date']
        date_map.setdefault(d, []).append(r)

    sorted_dates = sorted(date_map.keys())

    # 获取 RF 信号
    rf_df = compute_rf_signals(sorted_dates[0], sorted_dates[-1])
    rf_signal_map = dict(zip(rf_df['trade_date'], rf_df['signal']))

    # 加载板块日行情
    conn_industry = sqlite3.connect(INDUSTRY_DB)
    all_dates_str = "','".join(sorted_dates)

    if sector_type == 'industry':
        code_filter = "AND ts_code LIKE '884%'"
    elif sector_type == 'concept':
        code_filter = "AND ts_code LIKE '885%'"
    else:
        code_filter = ""

    concept_daily = pd.read_sql_query(f"""
        SELECT ts_code, trade_date, pct_chg
        FROM ths_daily
        WHERE trade_date IN ('{all_dates_str}') {code_filter}
    """, conn_industry)
    conn_industry.close()

    concept_return_map = {}
    for _, row in concept_daily.iterrows():
        concept_return_map[(row['ts_code'], row['trade_date'])] = row['pct_chg']

    backtest = []
    cum_return = 1.0

    for i in range(len(sorted_dates) - 1):
        date = sorted_dates[i]
        next_date = sorted_dates[i + 1]

        # 检查 RF 信号：sell 区间空仓
        rf_signal = rf_signal_map.get(date, 1)
        if rf_signal == -1:
            # 空仓，日收益为0
            backtest.append({
                'signal_date': date,
                'return_date': next_date,
                'daily_return': 0.0,
                'cum_return': round(cum_return, 4),
                'rf_signal': -1,
                'top10_concepts': [],
                'top10_codes': [],
            })
            continue

        sectors = date_map[date]
        sectors.sort(key=lambda x: x['final_score'], reverse=True)
        top10 = sectors[:10]

        if len(top10) == 0:
            backtest.append({
                'signal_date': date,
                'return_date': next_date,
                'daily_return': 0.0,
                'cum_return': round(cum_return, 4),
                'rf_signal': rf_signal,
                'top10_concepts': [],
                'top10_codes': [],
            })
            continue

        returns = []
        for c in top10:
            key = (c['concept_code'], next_date)
            if key in concept_return_map:
                ret = concept_return_map[key]
                if pd.notna(ret):
                    returns.append(ret)

        if returns:
            daily_ret = np.mean(returns) / 100.0
            daily_ret = np.clip(daily_ret, -0.2, 0.2)
            cum_return *= (1 + daily_ret)
        else:
            daily_ret = 0.0

        backtest.append({
            'signal_date': date,
            'return_date': next_date,
            'daily_return': round(daily_ret * 100, 4),
            'cum_return': round(cum_return, 4),
            'rf_signal': rf_signal,
            'top10_concepts': [c['concept_name'] for c in top10],
            'top10_codes': [c['concept_code'] for c in top10],
        })

    return backtest


def run_full_calculation(start_date='20260101', end_date=None, progress_cb=None):
    """
    运行完整计算（概念 + 行业）
    优化：一次性加载所有数据到内存，避免逐日查询
    """
    conn_stock = sqlite3.connect(STOCK_DB)
    conn_industry = sqlite3.connect(INDUSTRY_DB)
    conn_limit = sqlite3.connect(LIMIT_DB)

    try:
        if end_date is None:
            end_date = conn_stock.execute(
                "SELECT MAX(trade_date) FROM daily"
            ).fetchone()[0]

        # 一次性加载所有需要的交易日
        trade_dates = pd.read_sql_query(f"""
            SELECT DISTINCT trade_date FROM daily
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
            ORDER BY trade_date
        """, conn_stock)['trade_date'].tolist()

        logger.info(f"计算范围: {trade_dates[0]} ~ {trade_dates[-1]}, 共 {len(trade_dates)} 个交易日")

        # 加载板块列表和成分股
        sector_list = load_sector_list(conn_industry)
        member_df = load_sector_members(conn_industry)
        member_map = {}
        for _, row in member_df.iterrows():
            member_map.setdefault(row['sector_code'], []).append(row['stock_code'])

        n_concept = len(sector_list[sector_list['type'] == 'N'])
        n_industry = len(sector_list[sector_list['type'] == 'I'])
        logger.info(f"概念: {n_concept} 个, 行业: {n_industry} 个, 成分股关系: {len(member_df)} 条")

        # 一次性加载个股日线数据（只加载需要的日期范围）
        all_daily = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg, amount
            FROM daily
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """, conn_stock)
        logger.info(f"个股日线: {len(all_daily)} 条")

        # 一次性加载涨跌停数据
        all_limit_up = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, pct_chg, amount, fd_amount,
                   limit_times as streak, up_stat, open_times,
                   0 as is_broken
            FROM limit_up
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """, conn_limit)

        all_limit_broken = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, pct_chg, amount, fd_amount,
                   limit_times as streak, up_stat, open_times,
                   1 as is_broken
            FROM limit_broken
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """, conn_limit)

        # 合并涨跌停（涨停优先）
        if len(all_limit_broken) > 0:
            broken_only = all_limit_broken[~all_limit_broken['ts_code'].isin(all_limit_up['ts_code'])]
            all_limit = pd.concat([all_limit_up, broken_only], ignore_index=True)
        else:
            all_limit = all_limit_up
        logger.info(f"涨跌停数据: {len(all_limit)} 条")

        conn_stock.close()
        conn_limit.close()

        # 逐日计算
        all_results = []
        total = len(trade_dates)

        for i, td in enumerate(trade_dates):
            daily_df = all_daily[all_daily['trade_date'] == td].copy()
            limit_df = all_limit[all_limit['trade_date'] == td].copy()

            day_results = compute_daily_factors(
                td, sector_list, member_map, daily_df, limit_df
            )
            all_results.extend(day_results)

            if progress_cb:
                progress_cb(i + 1, total, td)

            if (i + 1) % 5 == 0 or (i + 1) == total:
                logger.info(f"进度: {i+1}/{total} ({td})")

        logger.info(f"因子计算完成，共 {len(all_results)} 条记录")

        # 分别回测
        bt_all = compute_backtest(all_results, sector_type=None)
        bt_concept = compute_backtest(all_results, sector_type='concept')
        bt_industry = compute_backtest(all_results, sector_type='industry')
        logger.info(f"回测完成: 全部={len(bt_all)}, 概念={len(bt_concept)}, 行业={len(bt_industry)}")

        return all_results, {
            'all': bt_all,
            'concept': bt_concept,
            'industry': bt_industry,
        }

    finally:
        conn_stock.close()
        conn_industry.close()
        conn_limit.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    results, bt = run_full_calculation('20260101')
    print(f"计算结果: {len(results)} 条")
    for k, v in bt.items():
        print(f"回测({k}): {len(v)} 条")
    if results:
        print("\n最新一天 Top10:")
        latest = max(r['trade_date'] for r in results)
        top = sorted([r for r in results if r['trade_date'] == latest],
                     key=lambda x: x['final_score'], reverse=True)[:10]
        for r in top:
            print(f"  [{r['sector_type']:7s}] {r['concept_name']:12s}  score={r['final_score']:8.4f}  "
                  f"mom={r['momentum']:7.3f}  emo={r['emotion_diff']:6.3f}  "
                  f"s={r['s_score']:5.3f}  涨停={r['limit_up_count']}  炸板={r['broken_count']}  "
                  f"最高连板={r['max_streak']}")
