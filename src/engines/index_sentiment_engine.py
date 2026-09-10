# -*- coding: utf-8 -*-
"""
宽基指数情绪指数计算引擎

对上证指数、沪深300、中证1000、中证2000四个宽基指数，
基于成分股计算5个子指标等权合成的综合情绪指标，绘制时序图。

5个子指标（与BOCI一级行业情绪指标相同）：
- F1: MA20 多头占比（价格趋势）
- F2: RSI 相对强弱（超买超卖）
- F3: 换手率强度（交投热度）
- F4: 涨跌停情绪差（极端情绪）
- F5: 成交额占比（资金拥挤度）

不做截面打分，直接展示情绪时序图。
"""

import sqlite3
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from datetime import datetime
import logging
import os

logger = logging.getLogger('index_sentiment_engine')

# ==================== 数据库路径 ====================
DB_DIR = '/Volumes/BEANPAPER/data/databases'
STOCK_DB = os.path.join(DB_DIR, 'stock_daily.db')
INDEX_DB = os.path.join(DB_DIR, 'index_daily.db')
LIMIT_DB = os.path.join(DB_DIR, 'limit_data.db')

# ==================== 四个宽基指数 ====================
INDEX_CONFIG = {
    '000001.SH': '上证指数',
    '000300.SH': '沪深300',
    '000852.SH': '中证1000',
    '932000.CSI': '中证2000',
}

# 策略参数（与BOCI一致）
SENTIMENT_PARAMS = {
    'ma_period': 20,
    'rsi_period': 14,
    'turnover_ma5': 5,
    'turnover_lookback': 250,
    'winsorize_low': 0.05,
    'winsorize_high': 0.95,
    'log_compensate': 0.0001,
    'turnover_5dma': 5,
}


def expanding_minmax_normalize(series):
    """Expanding Min-Max 动态标准化，输出严格 0~1"""
    if len(series) == 0:
        return series
    expanding_min = series.expanding(min_periods=1).min()
    expanding_max = series.expanding(min_periods=1).max()
    denom = expanding_max - expanding_min
    denom = denom.replace(0, np.nan)
    result = (series - expanding_min) / denom
    result = result.fillna(0.5)
    return result.clip(0, 1)


def winsorize_series(series, low=0.05, high=0.95):
    """Winsorize 缩尾处理"""
    if len(series) < 10:
        return series
    q_low = series.quantile(low)
    q_high = series.quantile(high)
    return series.clip(q_low, q_high)


def load_index_members(conn_index, index_codes):
    """
    加载宽基指数成分股
    返回 dict: index_code -> list of stock_codes
    使用最近的成分股权重数据
    """
    member_map = {}
    for idx_code in index_codes:
        # 取最新的成分股权重数据
        df = pd.read_sql_query(f"""
            SELECT DISTINCT con_code FROM index_weight
            WHERE index_code = '{idx_code}'
            ORDER BY trade_date DESC
        """, conn_index)
        if len(df) > 0:
            member_map[idx_code] = df['con_code'].tolist()
            logger.info(f"{INDEX_CONFIG.get(idx_code, idx_code)}: {len(df)} 只成分股")
        else:
            logger.warning(f"{idx_code} 无成分股数据")
    return member_map


def compute_f1_ma20_ratio(stock_history_df, member_map, trade_dates, index_codes):
    """
    F1: MA20 多头占比
    每个指数内，收盘价 >= MA20 的成分股占比
    stock_history_df 已包含 ma20 和 above_ma20 列
    
    向量化实现：预计算每只股票所属指数，然后用 groupby 聚合
    """
    # 构建股票->指数映射
    stock_to_index = {}
    for idx_code in index_codes:
        for stock_code in member_map.get(idx_code, []):
            stock_to_index[stock_code] = idx_code

    # 过滤出成分股数据
    df = stock_history_df[stock_history_df['ts_code'].isin(stock_to_index)].copy()
    df['index_code'] = df['ts_code'].map(stock_to_index)
    
    # 只保留目标交易日
    date_set = set(trade_dates)
    df = df[df['trade_date'].isin(date_set)]

    # 按指数和日期分组计算
    grouped = df.groupby(['index_code', 'trade_date']).agg(
        total=('above_ma20', 'count'),
        above=('above_ma20', 'sum')
    ).reset_index()
    
    grouped['ratio'] = (grouped['above'] / grouped['total']).round(6)
    
    result = {}
    for _, row in grouped.iterrows():
        result[(row['index_code'], row['trade_date'])] = row['ratio']
    
    return result


def compute_f2_rsi_normalized(index_daily_df, rsi_period=14):
    """
    F2: RSI 相对强弱（Expanding Min-Max 归一化）
    基于宽基指数自身的日线计算
    """
    results = {}
    for idx_code in index_daily_df['ts_code'].unique():
        ind_df = index_daily_df[index_daily_df['ts_code'] == idx_code].sort_values('trade_date')
        if len(ind_df) < rsi_period + 1:
            continue

        close = ind_df['close'].values
        deltas = np.diff(close)

        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.zeros(len(deltas))
        avg_loss = np.zeros(len(deltas))

        avg_gain[rsi_period-1] = np.mean(gains[:rsi_period])
        avg_loss[rsi_period-1] = np.mean(losses[:rsi_period])

        for i in range(rsi_period, len(deltas)):
            avg_gain[i] = (avg_gain[i-1] * (rsi_period - 1) + gains[i]) / rsi_period
            avg_loss[i] = (avg_loss[i-1] * (rsi_period - 1) + losses[i]) / rsi_period

        rs = np.where(avg_loss != 0, avg_gain / avg_loss, 100)
        rsi = 100 - 100 / (1 + rs)
        rsi[:rsi_period-1] = np.nan

        rsi_series = pd.Series(rsi)
        rsi_norm = expanding_minmax_normalize(rsi_series)

        dates = ind_df['trade_date'].values[1:]
        for d, v in zip(dates, rsi_norm.values):
            if pd.notna(v):
                results[(idx_code, d)] = round(v, 6)

    return results


def compute_f3_turnover_strength(index_daily_df, params):
    """
    F3: 换手率强度（基于指数日线换手率）
    注意：宽基指数日线没有 turnover_rate 字段，用成交额替代
    改用：指数成分股的加权换手率
    """
    # 宽基指数日线没有换手率，用成交额(vol字段)/自由流通市值 的代理
    # 简化处理：直接用指数日线数据
    # 由于指数日线有 vol 和 amount，但无 turnover_rate
    # 我们用 amount（成交额）的变化来代理换手率强度
    results = {}
    lookback = params['turnover_lookback']
    ma5 = params['turnover_ma5']

    for idx_code in index_daily_df['ts_code'].unique():
        ind_df = index_daily_df[index_daily_df['ts_code'] == idx_code].sort_values('trade_date')

        if 'amount' not in ind_df.columns:
            continue

        amt = ind_df['amount'].dropna()
        if len(amt) < lookback:
            continue

        # 用成交额的5日均值做代理
        amt_ma5 = amt.rolling(ma5, min_periods=1).mean()

        # 250日滚动统计
        rolling_mean = amt_ma5.rolling(lookback, min_periods=20).mean()
        rolling_std = amt_ma5.rolling(lookback, min_periods=20).std()

        upper = rolling_mean + 2 * rolling_std
        lower = rolling_mean - 2 * rolling_std
        band_width = upper - lower
        band_width = band_width.replace(0, np.nan)
        position = (amt_ma5 - lower) / band_width

        # Winsorize
        position = winsorize_series(position, params['winsorize_low'], params['winsorize_high'])

        # Expanding Min-Max
        norm = expanding_minmax_normalize(position)

        valid_idx = amt.dropna().index
        for idx in valid_idx:
            if idx in norm.index and pd.notna(norm.get(idx, np.nan)):
                trade_date = ind_df.loc[idx, 'trade_date']
                results[(idx_code, trade_date)] = round(norm[idx], 6)

    return results


def compute_f4_limit_emotion_diff(limit_up_df, limit_down_df, member_map, index_codes, params):
    """
    F4: 涨跌停情绪差
    ln(涨停家数 + ε) - ln(跌停家数 + ε)，然后 Expanding Min-Max
    """
    compensate = params['log_compensate']

    # 统计每个指数每天的涨停/跌停家数
    stock_to_index = {}
    for idx_code in index_codes:
        for stock_code in member_map.get(idx_code, []):
            stock_to_index[stock_code] = idx_code

    if len(limit_up_df) > 0:
        limit_up_df = limit_up_df.copy()
        limit_up_df['index_code'] = limit_up_df['ts_code'].map(stock_to_index)
        up_count = limit_up_df[limit_up_df['index_code'].notna()].groupby(
            ['index_code', 'trade_date']
        ).size().reset_index(name='up_count')
    else:
        up_count = pd.DataFrame(columns=['index_code', 'trade_date', 'up_count'])

    if len(limit_down_df) > 0:
        limit_down_df = limit_down_df.copy()
        limit_down_df['index_code'] = limit_down_df['ts_code'].map(stock_to_index)
        down_count = limit_down_df[limit_down_df['index_code'].notna()].groupby(
            ['index_code', 'trade_date']
        ).size().reset_index(name='down_count')
    else:
        down_count = pd.DataFrame(columns=['index_code', 'trade_date', 'down_count'])

    if len(up_count) > 0 and len(down_count) > 0:
        merged = up_count.merge(down_count, on=['index_code', 'trade_date'], how='outer').fillna(0)
    elif len(up_count) > 0:
        merged = up_count.copy()
        merged['down_count'] = 0
    elif len(down_count) > 0:
        merged = down_count.copy()
        merged['up_count'] = 0
    else:
        return {}

    merged['log_diff'] = np.log(merged['up_count'] + compensate) - np.log(merged['down_count'] + compensate)

    results = {}
    for idx_code in merged['index_code'].unique():
        ind_data = merged[merged['index_code'] == idx_code].sort_values('trade_date')
        if len(ind_data) < 5:
            continue
        norm = expanding_minmax_normalize(ind_data['log_diff'])
        for i, (idx, row) in enumerate(ind_data.iterrows()):
            if i < len(norm) and pd.notna(norm.iloc[i]):
                results[(idx_code, row['trade_date'])] = round(norm.iloc[i], 6)

    return results


def compute_f5_amount_ratio(stock_history_df, total_amount, member_map, index_codes, params):
    """
    F5: 成交额占比
    每个指数成分股成交额 / 全A成交额，5日均线，Z-Score，Winsorize，Expanding Min-Max
    
    stock_history_df: 成分股日线（已含 amount 列）
    total_amount: 全A每日总成交额（Series，index=trade_date）
    """
    ma5 = params['turnover_5dma']
    lookback = params['turnover_lookback']

    # 构建股票->指数映射
    stock_to_index = {}
    for idx_code in index_codes:
        for stock_code in member_map.get(idx_code, []):
            stock_to_index[stock_code] = idx_code

    # 过滤成分股
    member_df = stock_history_df[stock_history_df['ts_code'].isin(stock_to_index)].copy()
    member_df['index_code'] = member_df['ts_code'].map(stock_to_index)

    # 按指数和日期聚合成交额
    ind_amount = member_df.groupby(['index_code', 'trade_date'])['amount'].sum().reset_index()

    results = {}
    for idx_code in index_codes:
        idx_data = ind_amount[ind_amount['index_code'] == idx_code].set_index('trade_date')['amount']
        ratio = idx_data / total_amount
        ratio = ratio.dropna()

        if len(ratio) < lookback:
            # 数据不够时，尝试用更短的窗口
            if len(ratio) < 30:
                continue
            # 用较短窗口
            ratio_ma5 = ratio.rolling(ma5, min_periods=1).mean()
            rolling_mean = ratio_ma5.rolling(min(lookback, len(ratio)), min_periods=20).mean()
            rolling_std = ratio_ma5.rolling(min(lookback, len(ratio)), min_periods=20).std()
        else:
            ratio_ma5 = ratio.rolling(ma5, min_periods=1).mean()
            rolling_mean = ratio_ma5.rolling(lookback, min_periods=20).mean()
            rolling_std = ratio_ma5.rolling(lookback, min_periods=20).std()

        z_score = (ratio_ma5 - rolling_mean) / rolling_std.replace(0, np.nan)

        z_score = winsorize_series(z_score, params['winsorize_low'], params['winsorize_high'])
        z_norm = expanding_minmax_normalize(z_score)

        for trade_date, val in z_norm.items():
            if pd.notna(val):
                results[(idx_code, trade_date)] = round(val, 6)

    return results


def compute_composite_sentiment(f1_map, f2_map, f3_map, f4_map, f5_map, index_codes, all_dates):
    """
    五个子指标等权合成综合情绪指标
    返回 dict: (index_code, trade_date) -> sentiment
    """
    results = {}
    for idx_code in index_codes:
        for td in all_dates:
            vals = []
            for fmap in [f1_map, f2_map, f3_map, f4_map, f5_map]:
                v = fmap.get((idx_code, td), np.nan)
                if pd.notna(v):
                    vals.append(v)

            if len(vals) >= 3:
                results[(idx_code, td)] = round(np.mean(vals), 6)

    return results


# ==================== 主计算流程 ====================

def run_index_sentiment_calculation(start_date='20200102', end_date=None, progress_cb=None):
    """
    运行宽基指数情绪指数计算

    步骤：
    1. 加载指数成分股
    2. 加载指数日线数据
    3. 加载个股日线数据（含MA20预热）
    4. 加载涨跌停数据
    5. 计算五个子指标
    6. 合成综合情绪
    """
    index_codes = list(INDEX_CONFIG.keys())
    index_names = INDEX_CONFIG.copy()

    conn_stock = sqlite3.connect(STOCK_DB)
    conn_index = sqlite3.connect(INDEX_DB)
    conn_limit = sqlite3.connect(LIMIT_DB)

    try:
        if end_date is None:
            end_date = conn_stock.execute(
                "SELECT MAX(trade_date) FROM daily"
            ).fetchone()[0]

        logger.info(f"宽基指数情绪计算范围: {start_date} ~ {end_date}")

        # Step 1: 加载成分股
        if progress_cb:
            progress_cb(0, 7, '加载指数成分股')

        member_map = load_index_members(conn_index, index_codes)
        total_members = sum(len(v) for v in member_map.values())
        logger.info(f"总成分股: {total_members} 只")

        # Step 2: 加载指数日线
        if progress_cb:
            progress_cb(1, 7, '加载指数日线')

        codes_str = "','".join(index_codes)
        index_daily_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg, vol, amount
            FROM daily
            WHERE ts_code IN ('{codes_str}')
              AND trade_date >= '{start_date}' AND trade_date <= '{end_date}'
            ORDER BY ts_code, trade_date
        """, conn_index)
        logger.info(f"指数日线: {len(index_daily_df)} 条")

        # 获取交易日列表（用个股日线为准，指数日线日期可能不完整）
        trade_dates = sorted(conn_stock.execute(
            f"SELECT DISTINCT trade_date FROM daily WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'"
        ).fetchall())
        trade_dates = [t[0] for t in trade_dates]
        logger.info(f"交易日: {len(trade_dates)} 天")

        # Step 3: 加载个股日线（含MA20预热）
        if progress_cb:
            progress_cb(2, 7, '加载个股日线')

        # MA20预热期
        first_date = trade_dates[0] if trade_dates else start_date
        pre_dates = pd.read_sql_query(f"""
            SELECT DISTINCT trade_date FROM daily
            WHERE trade_date < '{first_date}'
            ORDER BY trade_date DESC LIMIT 20
        """, conn_stock)['trade_date'].tolist()

        ma_start = min(pre_dates) if pre_dates else first_date

        # 加载成分股的完整日线（用于F1 MA20计算）
        all_member_stocks = set()
        for members in member_map.values():
            all_member_stocks.update(members)

        # 只加载成分股的日线（大幅减少数据量）
        member_stocks_str = "','".join(list(all_member_stocks))
        stock_history_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg, amount
            FROM daily
            WHERE ts_code IN ('{member_stocks_str}')
              AND trade_date >= '{ma_start}' AND trade_date <= '{end_date}'
        """, conn_stock)
        logger.info(f"成分股日线: {len(stock_history_df)} 条")

        # F5 成交额占比需要全A每日总成交额，用SQL直接聚合（不加载全量数据）
        total_amount_df = pd.read_sql_query(f"""
            SELECT trade_date, SUM(amount) as total_amount
            FROM daily
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
            GROUP BY trade_date
        """, conn_stock)
        total_amount = total_amount_df.set_index('trade_date')['total_amount']
        logger.info(f"全A每日成交额: {len(total_amount_df)} 天")

        # Step 4: 加载涨跌停数据
        if progress_cb:
            progress_cb(3, 7, '加载涨跌停数据')

        limit_up_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date FROM limit_up
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """, conn_limit)

        limit_down_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date FROM limit_down
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """, conn_limit)
        logger.info(f"涨停: {len(limit_up_df)} 条, 跌停: {len(limit_down_df)} 条")

        conn_stock.close()
        conn_limit.close()
        conn_index.close()

        # ==================== 计算MA20 ====================
        if progress_cb:
            progress_cb(4, 7, '计算MA20多头占比')

        stock_history_df = stock_history_df.sort_values(['ts_code', 'trade_date'])
        stock_history_df['ma20'] = stock_history_df.groupby('ts_code')['close'].transform(
            lambda x: x.rolling(20, min_periods=20).mean()
        )
        stock_history_df['above_ma20'] = (stock_history_df['close'] >= stock_history_df['ma20']).astype(int)

        # F1: MA20 多头占比
        f1_map = compute_f1_ma20_ratio(stock_history_df, member_map, trade_dates, index_codes)
        logger.info(f"F1 (MA20占比): {len(f1_map)} 条")

        # F2: RSI
        if progress_cb:
            progress_cb(5, 7, '计算F2: RSI相对强弱')

        f2_map = compute_f2_rsi_normalized(index_daily_df, SENTIMENT_PARAMS['rsi_period'])
        logger.info(f"F2 (RSI): {len(f2_map)} 条")

        # F3: 换手率强度（用成交额代理）
        if progress_cb:
            progress_cb(5, 7, '计算F3: 换手率强度')

        f3_map = compute_f3_turnover_strength(index_daily_df, SENTIMENT_PARAMS)
        logger.info(f"F3 (换手率): {len(f3_map)} 条")

        # F4: 涨跌停情绪差
        if progress_cb:
            progress_cb(6, 7, '计算F4: 涨跌停情绪差')

        f4_map = compute_f4_limit_emotion_diff(
            limit_up_df, limit_down_df, member_map, index_codes, SENTIMENT_PARAMS
        )
        logger.info(f"F4 (涨跌停): {len(f4_map)} 条")

        # F5: 成交额占比
        if progress_cb:
            progress_cb(6, 7, '计算F5: 成交额占比')

        f5_map = compute_f5_amount_ratio(stock_history_df, total_amount, member_map, index_codes, SENTIMENT_PARAMS)
        logger.info(f"F5 (成交额占比): {len(f5_map)} 条")

        # ==================== 合成综合情绪 ====================
        if progress_cb:
            progress_cb(7, 7, '合成综合情绪指标')

        sentiment_map = compute_composite_sentiment(
            f1_map, f2_map, f3_map, f4_map, f5_map,
            index_codes, trade_dates
        )
        logger.info(f"综合情绪: {len(sentiment_map)} 条")

        # 构建结果 DataFrame
        records = []
        for (idx_code, td), val in sentiment_map.items():
            records.append({
                'index_code': idx_code,
                'index_name': index_names.get(idx_code, idx_code),
                'trade_date': td,
                'sentiment': val,
                'f1_ma20': f1_map.get((idx_code, td), np.nan),
                'f2_rsi': f2_map.get((idx_code, td), np.nan),
                'f3_turnover': f3_map.get((idx_code, td), np.nan),
                'f4_limit': f4_map.get((idx_code, td), np.nan),
                'f5_amount': f5_map.get((idx_code, td), np.nan),
            })

        result_df = pd.DataFrame(records)
        if len(result_df) > 0:
            result_df = result_df.sort_values(['index_code', 'trade_date'])

        logger.info(f"计算完成: {len(result_df)} 条记录")

        return result_df, index_daily_df

    except Exception as e:
        logger.error(f"计算失败: {str(e)}", exc_info=True)
        raise
