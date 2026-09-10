# -*- coding: utf-8 -*-
"""
BOCI 一级行业情绪指标计算引擎

来源：中银国际证券《A股情绪指标体系之二：BOCI一级行业情绪指标》

策略核心：
1. 五个子指标等权合成综合情绪指标
   - F1: MA20 多头占比（价格趋势）
   - F2: RSI 相对强弱（超买超卖）
   - F3: 换手率强度（交投热度）
   - F4: 涨跌停情绪差（极端情绪）
   - F5: 成交额占比（资金拥挤度）

2. 五截面打分体系选行业
   - S1: 情绪20日斜率 (35%)
   - S2: 情绪加速度 (15%)
   - S3: 情绪相对水平 (30%)
   - S4: 价格40日动量 (10%)
   - S5: 价格20日斜率 (10%)

3. 85%过热阈值一票否决
4. Top3 等权持有，10日调仓
"""

import sqlite3
import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from datetime import datetime
import logging
import os

logger = logging.getLogger('boci_engine')

# ==================== 数据库路径 ====================
DB_DIR = '/Volumes/BEANPAPER/data/databases'
STOCK_DB = os.path.join(DB_DIR, 'stock_daily.db')
INDUSTRY_DB = os.path.join(DB_DIR, 'industry.db')
LIMIT_DB = os.path.join(DB_DIR, 'limit_data.db')

# ==================== 策略参数 ====================
BOCI_PARAMS = {
    # --- 综合情绪子指标参数 ---
    'ma_period': 20,              # MA20 多头占比
    'rsi_period': 14,             # RSI 周期
    'turnover_ma5': 5,            # 换手率5日均线
    'turnover_lookback': 250,     # 换手率滚动窗口
    'winsorize_low': 0.05,        # 缩尾下界
    'winsorize_high': 0.95,       # 缩尾上界
    'log_compensate': 0.0001,     # 涨跌停对数补偿（0.01%）
    'turnover_5dma': 5,           # 成交额占比5日均线

    # --- 截面打分参数 ---
    'slope_window': 20,           # 情绪斜率窗口
    'accel_window': 5,            # 加速度差分窗口
    'price_momentum_window': 40,  # 价格动量窗口
    'price_slope_window': 20,     # 价格斜率窗口
    'overheat_threshold': 0.85,   # 过热阈值

    # --- 截面权重 ---
    'w_slope': 0.35,              # 情绪斜率
    'w_accel': 0.15,              # 情绪加速度
    'w_relative': 0.30,           # 情绪相对水平
    'w_momentum': 0.10,           # 价格动量
    'w_price_slope': 0.10,        # 价格斜率

    # --- 交易参数 ---
    'rebalance_days': 10,         # 调仓频率（交易日）
    'top_n': 3,                   # 持仓行业数
}

# 只用有6年完整数据的68个881行业（排除22个2024年新增的）
VALID_INDUSTRY_MIN_DATES = 1200  # 至少1200个交易日才认为完整


def load_industry_list(conn_industry):
    """加载881一级行业列表（只取数据完整的）"""
    df = pd.read_sql_query("""
        SELECT i.ts_code, i.name, i.count as stock_count,
               COUNT(d.trade_date) as daily_count,
               MIN(d.trade_date) as min_date,
               MAX(d.trade_date) as max_date
        FROM ths_index i
        LEFT JOIN ths_daily d ON i.ts_code = d.ts_code
        WHERE i.ts_code LIKE '881%'
        GROUP BY i.ts_code
        HAVING daily_count >= ?
        ORDER BY i.ts_code
    """, conn_industry, params=(VALID_INDUSTRY_MIN_DATES,))
    return df


def load_industry_members(conn_industry, industry_codes):
    """加载881行业成分股"""
    codes_str = "','".join(industry_codes)
    df = pd.read_sql_query(f"""
        SELECT m.ts_code as industry_code, m.con_code as stock_code
        FROM ths_member m
        WHERE m.ts_code IN ('{codes_str}')
    """, conn_industry)
    return df


def expanding_minmax_normalize(series):
    """Expanding Min-Max 动态标准化，输出严格 0~1"""
    if len(series) == 0:
        return series
    expanding_min = series.expanding(min_periods=1).min()
    expanding_max = series.expanding(min_periods=1).max()
    denom = expanding_max - expanding_min
    # 避免除零
    denom = denom.replace(0, np.nan)
    result = (series - expanding_min) / denom
    result = result.fillna(0.5)  # 单值情况默认0.5
    return result.clip(0, 1)


def winsorize_series(series, low=0.05, high=0.95):
    """Winsorize 缩尾处理"""
    if len(series) < 10:
        return series
    q_low = series.quantile(low)
    q_high = series.quantile(high)
    return series.clip(q_low, q_high)


# ==================== 五个子指标计算 ====================

def compute_ma20_ratio(stock_daily_df, member_map, trade_date, industry_codes, ma_period=20):
    """
    F1: MA20 多头占比
    每个行业内，收盘价 >= MA20 的成分股占比
    """
    # 获取所有需要的个股
    all_stocks = set()
    for code in industry_codes:
        all_stocks.update(member_map.get(code, []))
    
    if not all_stocks:
        return {}
    
    # 过滤出该日有数据的个股
    day_df = stock_daily_df[stock_daily_df['trade_date'] == trade_date]
    
    # 需要前 ma_period 天的数据来计算 MA
    # 这里简化：用当日数据中已有 close 的个股
    # MA20 需要历史数据，我们预先加载
    result = {}
    for ind_code in industry_codes:
        members = member_map.get(ind_code, [])
        if not members:
            result[ind_code] = np.nan
            continue
        
        member_day = day_df[day_df['ts_code'].isin(members)]
        # 需要判断哪些股站上了 MA20
        # 简化处理：用 pre_close 和 close 的关系近似
        # 更精确的实现需要加载历史数据
        total = len(member_day)
        if total == 0:
            result[ind_code] = np.nan
            continue
        
        # 用当日涨跌 > 0 近似（简化版）
        # 精确版需要历史MA20，这里后续优化
        above_ma = (member_day['pct_chg'] > 0).sum()
        result[ind_code] = above_ma / total if total > 0 else np.nan
    
    return result


def compute_rsi_normalized(industry_daily_df, rsi_period=14):
    """
    F2: RSI 相对强弱（Expanding Min-Max 归一化）
    基于行业指数日线计算
    """
    results = {}
    
    for ind_code in industry_daily_df['ts_code'].unique():
        ind_df = industry_daily_df[industry_daily_df['ts_code'] == ind_code].sort_values('trade_date')
        if len(ind_df) < rsi_period + 1:
            continue
        
        close = ind_df['close'].values
        deltas = np.diff(close)
        
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        
        # Wilder's smoothing
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
        
        # Expanding Min-Max 标准化
        rsi_series = pd.Series(rsi)
        rsi_norm = expanding_minmax_normalize(rsi_series)
        
        # 与 ind_df 对齐（rsi 少了一天因为 diff）
        dates = ind_df['trade_date'].values[1:]  # 从第二天开始
        for d, v in zip(dates, rsi_norm.values):
            if pd.notna(v):
                results[(ind_code, d)] = round(v, 6)
    
    return results


def compute_turnover_strength(industry_daily_df, params):
    """
    F3: 换手率强度
    1. 换手率5日均线
    2. 250日滚动均值±2倍标准差布林带
    3. 5%~95% Winsorize
    4. Expanding Min-Max 标准化
    """
    ma5 = params['turnover_ma5']
    lookback = params['turnover_lookback']
    
    results = {}
    
    for ind_code in industry_daily_df['ts_code'].unique():
        ind_df = industry_daily_df[industry_daily_df['ts_code'] == ind_code].sort_values('trade_date')
        
        if 'turnover_rate' not in ind_df.columns:
            continue
        
        tr = ind_df['turnover_rate'].dropna()
        if len(tr) < lookback:
            continue
        
        # 5日均线
        tr_ma5 = tr.rolling(ma5, min_periods=1).mean()
        
        # 250日滚动统计
        rolling_mean = tr_ma5.rolling(lookback, min_periods=20).mean()
        rolling_std = tr_ma5.rolling(lookback, min_periods=20).std()
        
        # 布林带分位
        upper = rolling_mean + 2 * rolling_std
        lower = rolling_mean - 2 * rolling_std
        band_width = upper - lower
        band_width = band_width.replace(0, np.nan)
        tr_position = (tr_ma5 - lower) / band_width
        
        # Winsorize
        tr_position = winsorize_series(tr_position, params['winsorize_low'], params['winsorize_high'])
        
        # Expanding Min-Max
        tr_norm = expanding_minmax_normalize(tr_position)
        
        # 映射回日期
        valid_idx = tr.dropna().index
        for idx in valid_idx:
            if idx in tr_norm.index and pd.notna(tr_norm.get(idx, np.nan)):
                trade_date = ind_df.loc[idx, 'trade_date']
                results[(ind_code, trade_date)] = round(tr_norm[idx], 6)
    
    return results


def compute_limit_emotion_diff(limit_up_df, limit_down_df, member_map, industry_codes, params):
    """
    F4: 涨跌停情绪差
    ln(涨停家数 + ε) - ln(跌停家数 + ε)，然后 Expanding Min-Max
    """
    compensate = params['log_compensate']
    
    # 统计每个行业每天的涨停/跌停家数
    # 需要个股到行业的映射
    stock_to_industry = {}
    for ind_code in industry_codes:
        for stock_code in member_map.get(ind_code, []):
            stock_to_industry[stock_code] = ind_code
    
    # 统计涨停
    if len(limit_up_df) > 0:
        limit_up_df = limit_up_df.copy()
        limit_up_df['industry_code'] = limit_up_df['ts_code'].map(stock_to_industry)
        up_count = limit_up_df[limit_up_df['industry_code'].notna()].groupby(
            ['industry_code', 'trade_date']
        ).size().reset_index(name='up_count')
    else:
        up_count = pd.DataFrame(columns=['industry_code', 'trade_date', 'up_count'])
    
    # 统计跌停
    if len(limit_down_df) > 0:
        limit_down_df = limit_down_df.copy()
        limit_down_df['industry_code'] = limit_down_df['ts_code'].map(stock_to_industry)
        down_count = limit_down_df[limit_down_df['industry_code'].notna()].groupby(
            ['industry_code', 'trade_date']
        ).size().reset_index(name='down_count')
    else:
        down_count = pd.DataFrame(columns=['industry_code', 'trade_date', 'down_count'])
    
    # 合并
    if len(up_count) > 0 and len(down_count) > 0:
        merged = up_count.merge(down_count, on=['industry_code', 'trade_date'], how='outer').fillna(0)
    elif len(up_count) > 0:
        merged = up_count.copy()
        merged['down_count'] = 0
    elif len(down_count) > 0:
        merged = down_count.copy()
        merged['up_count'] = 0
    else:
        return {}
    
    # 对数差
    merged['log_diff'] = np.log(merged['up_count'] + compensate) - np.log(merged['down_count'] + compensate)
    
    # 按行业排序后 Expanding Min-Max
    results = {}
    for ind_code in merged['industry_code'].unique():
        ind_data = merged[merged['industry_code'] == ind_code].sort_values('trade_date')
        if len(ind_data) < 5:
            continue
        norm = expanding_minmax_normalize(ind_data['log_diff'])
        for _, row in ind_data.iterrows():
            idx = ind_data.index.get_loc(row.name) if row.name in ind_data.index else None
            if idx is not None and idx < len(norm):
                results[(ind_code, row['trade_date'])] = round(norm.iloc[idx], 6)
    
    return results


def compute_amount_ratio(stock_daily_df, member_map, industry_codes, params):
    """
    F5: 成交额占比
    1. 行业成交额 / 全A成交额
    2. 5日均线
    3. Z-Score（250日滚动）
    4. Winsorize 5%~95%
    5. Expanding Min-Max
    """
    ma5 = params['turnover_5dma']
    lookback = params['turnover_lookback']
    
    # 全市场每日总成交额
    total_amount = stock_daily_df.groupby('trade_date')['amount'].sum()
    
    # 每个行业每日总成交额
    results = {}
    
    for ind_code in industry_codes:
        members = member_map.get(ind_code, [])
        if not members:
            continue
        
        ind_stocks = stock_daily_df[stock_daily_df['ts_code'].isin(members)]
        ind_amount = ind_stocks.groupby('trade_date')['amount'].sum()
        
        # 成交额占比
        ratio = ind_amount / total_amount
        ratio = ratio.dropna()
        
        if len(ratio) < lookback:
            continue
        
        # 5日均线
        ratio_ma5 = ratio.rolling(ma5, min_periods=1).mean()
        
        # Z-Score
        rolling_mean = ratio_ma5.rolling(lookback, min_periods=20).mean()
        rolling_std = ratio_ma5.rolling(lookback, min_periods=20).std()
        z_score = (ratio_ma5 - rolling_mean) / rolling_std.replace(0, np.nan)
        
        # Winsorize
        z_score = winsorize_series(z_score, params['winsorize_low'], params['winsorize_high'])
        
        # Expanding Min-Max
        z_norm = expanding_minmax_normalize(z_score)
        
        for trade_date, val in z_norm.items():
            if pd.notna(val):
                results[(ind_code, trade_date)] = round(val, 6)
    
    return results


# ==================== 综合情绪指标合成 ====================

def compute_composite_sentiment(f1_map, f2_map, f3_map, f4_map, f5_map, industry_codes, all_dates):
    """
    五个子指标等权合成综合情绪指标
    返回 dict: (industry_code, trade_date) -> sentiment
    """
    results = {}
    
    for ind_code in industry_codes:
        for td in all_dates:
            vals = []
            for fmap in [f1_map, f2_map, f3_map, f4_map, f5_map]:
                v = fmap.get((ind_code, td), np.nan)
                if pd.notna(v):
                    vals.append(v)
            
            if len(vals) >= 3:  # 至少3个子指标有值
                results[(ind_code, td)] = round(np.mean(vals), 6)
    
    return results


# ==================== 五截面打分 ====================

def compute_cross_section_scores(sentiment_map, industry_daily_df, params, industry_codes, all_dates):
    """
    五截面打分体系
    
    S1: 情绪20日斜率 (35%)  - 线性回归斜率
    S2: 情绪加速度 (15%)    - 斜率的5日差分
    S3: 情绪相对水平 (30%)  - 剥离大盘后的Alpha
    S4: 价格40日动量 (10%)  - 行业指数40日收益率
    S5: 价格20日斜率 (10%)  - 价格线性回归斜率
    """
    slope_window = params['slope_window']
    accel_window = params['accel_window']
    momentum_window = params['price_momentum_window']
    price_slope_window = params['price_slope_window']
    overheat = params['overheat_threshold']
    
    # 构建情绪时序 DataFrame
    sentiment_records = []
    for (ind_code, td), val in sentiment_map.items():
        sentiment_records.append({
            'industry_code': ind_code,
            'trade_date': td,
            'sentiment': val
        })
    
    if not sentiment_records:
        return {}
    
    sent_df = pd.DataFrame(sentiment_records)
    
    # 大盘情绪（所有行业每日均值）
    market_sentiment = sent_df.groupby('trade_date')['sentiment'].mean().to_dict()
    
    # 行业指数收盘价映射
    price_map = {}
    for _, row in industry_daily_df.iterrows():
        price_map[(row['ts_code'], row['trade_date'])] = row['close']
    
    # 行业指数收益率映射
    ret_map = {}
    for ind_code in industry_daily_df['ts_code'].unique():
        ind_data = industry_daily_df[industry_daily_df['ts_code'] == ind_code].sort_values('trade_date')
        closes = ind_data['close'].values
        dates = ind_data['trade_date'].values
        for i in range(1, len(dates)):
            if closes[i-1] > 0:
                ret_map[(ind_code, dates[i])] = (closes[i] / closes[i-1] - 1) * 100
    
    # 逐行业逐日计算截面因子
    score_records = []
    
    for ind_code in industry_codes:
        ind_sent = sent_df[sent_df['industry_code'] == ind_code].sort_values('trade_date')
        if len(ind_sent) < slope_window:
            continue
        
        dates = ind_sent['trade_date'].values
        sentiments = ind_sent['sentiment'].values
        
        # S1: 情绪斜率（20日线性回归）
        slopes = np.full(len(sentiments), np.nan)
        for i in range(slope_window - 1, len(sentiments)):
            y = sentiments[i - slope_window + 1:i + 1]
            x = np.arange(slope_window)
            if len(y) == slope_window and not np.any(np.isnan(y)):
                slope, _ = np.polyfit(x, y, 1)
                slopes[i] = slope
        
        # S2: 加速度（斜率的5日差分）
        accel = np.full(len(slopes), np.nan)
        for i in range(accel_window, len(slopes)):
            if pd.notna(slopes[i]) and pd.notna(slopes[i - accel_window]):
                accel[i] = slopes[i] - slopes[i - accel_window]
        
        # S3: 相对水平（行业情绪 - 大盘情绪）
        relative = np.full(len(sentiments), np.nan)
        for i, td in enumerate(dates):
            mkt = market_sentiment.get(td, np.nan)
            if pd.notna(mkt):
                relative[i] = sentiments[i] - mkt
        
        # S4: 价格40日动量
        momentum = np.full(len(dates), np.nan)
        for i in range(momentum_window, len(dates)):
            td = dates[i]
            # 用行业指数的40日收益率
            # 简化：从 price_map 获取
            ret_40d = 0
            count = 0
            for j in range(i - momentum_window + 1, i + 1):
                r = ret_map.get((ind_code, dates[j]), np.nan)
                if pd.notna(r):
                    ret_40d += r
                    count += 1
            if count > momentum_window * 0.8:
                momentum[i] = ret_40d
        
        # S5: 价格20日斜率
        price_slopes = np.full(len(dates), np.nan)
        for i in range(price_slope_window - 1, len(dates)):
            prices = []
            for j in range(i - price_slope_window + 1, i + 1):
                p = price_map.get((ind_code, dates[j]), np.nan)
                if pd.notna(p):
                    prices.append(p)
            if len(prices) >= price_slope_window * 0.8:
                x = np.arange(len(prices))
                slope, _ = np.polyfit(x, prices, 1)
                price_slopes[i] = slope
        
        # 汇总
        for i, td in enumerate(dates):
            score_records.append({
                'industry_code': ind_code,
                'trade_date': td,
                'sentiment': sentiments[i],
                's1_slope': slopes[i],
                's2_accel': accel[i],
                's3_relative': relative[i],
                's4_momentum': momentum[i],
                's5_price_slope': price_slopes[i],
            })
    
    if not score_records:
        return {}
    
    score_df = pd.DataFrame(score_records)
    
    # 横截面排名打分（每个日期内，各行业排名百分位）
    def rank_normalize(series):
        """排名归一化到 0~1"""
        return series.rank(pct=True)
    
    score_df['rank_s1'] = score_df.groupby('trade_date')['s1_slope'].transform(rank_normalize)
    score_df['rank_s2'] = score_df.groupby('trade_date')['s2_accel'].transform(rank_normalize)
    score_df['rank_s3'] = score_df.groupby('trade_date')['s3_relative'].transform(rank_normalize)
    score_df['rank_s4'] = score_df.groupby('trade_date')['s4_momentum'].transform(rank_normalize)
    score_df['rank_s5'] = score_df.groupby('trade_date')['s5_price_slope'].transform(rank_normalize)
    
    # 加权得分
    w = params
    score_df['final_score'] = (
        w['w_slope'] * score_df['rank_s1'].fillna(0.5) +
        w['w_accel'] * score_df['rank_s2'].fillna(0.5) +
        w['w_relative'] * score_df['rank_s3'].fillna(0.5) +
        w['w_momentum'] * score_df['rank_s4'].fillna(0.5) +
        w['w_price_slope'] * score_df['rank_s5'].fillna(0.5)
    )
    
    # 85% 过热阈值：一票否决
    overheat_mask = score_df['sentiment'] > overheat
    score_df.loc[overheat_mask, 'final_score'] = -1.0  # 极值负分
    
    return score_df


# ==================== 回测 ====================

def compute_boci_backtest(score_df, industry_daily_df, params):
    """
    BOCI 策略回测：每10个交易日调仓，Top3 行业等权持有
    
    返回回测记录列表
    """
    if score_df is None or len(score_df) == 0:
        return []
    
    rebalance_days = params['rebalance_days']
    top_n = params['top_n']
    
    # 获取所有交易日
    all_dates = sorted(score_df['trade_date'].unique())
    if len(all_dates) < rebalance_days + 1:
        return []
    
    # 行业日收益率映射
    ind_ret_map = {}
    for ind_code in industry_daily_df['ts_code'].unique():
        ind_data = industry_daily_df[industry_daily_df['ts_code'] == ind_code].sort_values('trade_date')
        dates = ind_data['trade_date'].values
        pct = ind_data['pct_chg'].values
        for i in range(len(dates)):
            ind_ret_map[(ind_code, dates[i])] = pct[i] if pd.notna(pct[i]) else 0
    
    # 行业名称映射
    ind_name_map = {}
    for ind_code in score_df['industry_code'].unique():
        # 从 industry_daily_df 或其他地方获取
        ind_name_map[ind_code] = ind_code  # 稍后替换
    
    backtest = []
    cum_return = 1.0
    
    # 每10个交易日调仓
    rebalance_idx = 0
    current_holdings = []
    
    for i, date in enumerate(all_dates):
        # 检查是否调仓日
        if i % rebalance_days == 0:
            # 选 Top N
            day_scores = score_df[score_df['trade_date'] == date].sort_values('final_score', ascending=False)
            current_holdings = day_scores.head(top_n)['industry_code'].tolist()
            rebalance_idx = i
        
        # 计算当日收益（次日实现）
        if i + 1 < len(all_dates):
            next_date = all_dates[i + 1]
            
            if current_holdings:
                returns = []
                for ind_code in current_holdings:
                    ret = ind_ret_map.get((ind_code, next_date), np.nan)
                    if pd.notna(ret):
                        returns.append(ret / 100.0)
                
                if returns:
                    daily_ret = np.mean(returns)
                    daily_ret = np.clip(daily_ret, -0.2, 0.2)
                    cum_return *= (1 + daily_ret)
                else:
                    daily_ret = 0.0
            else:
                daily_ret = 0.0
            
            backtest.append({
                'signal_date': date,
                'return_date': next_date,
                'daily_return': round(daily_ret * 100, 4),
                'cum_return': round(cum_return, 4),
                'top_industries': current_holdings,
            })
    
    return backtest


# ==================== 主计算流程 ====================

def run_boci_calculation(start_date='20200102', end_date=None, progress_cb=None):
    """
    运行完整 BOCI 策略计算
    
    步骤：
    1. 加载881行业列表和成分股
    2. 加载行业日线数据
    3. 加载个股日线数据
    4. 加载涨跌停数据
    5. 计算五个子指标
    6. 合成综合情绪
    7. 五截面打分
    8. 回测
    """
    conn_stock = sqlite3.connect(STOCK_DB)
    conn_industry = sqlite3.connect(INDUSTRY_DB)
    conn_limit = sqlite3.connect(LIMIT_DB)
    
    try:
        if end_date is None:
            end_date = conn_stock.execute(
                "SELECT MAX(trade_date) FROM daily"
            ).fetchone()[0]
        
        logger.info(f"BOCI 计算范围: {start_date} ~ {end_date}")
        
        # Step 1: 加载881行业
        if progress_cb:
            progress_cb(0, 8, '加载行业列表')
        
        industry_df = load_industry_list(conn_industry)
        industry_codes = industry_df['ts_code'].tolist()
        industry_names = dict(zip(industry_df['ts_code'], industry_df['name']))
        logger.info(f"有效881行业: {len(industry_codes)} 个")
        
        # Step 2: 加载成分股
        if progress_cb:
            progress_cb(1, 8, '加载成分股')
        
        member_df = load_industry_members(conn_industry, industry_codes)
        member_map = {}
        for _, row in member_df.iterrows():
            member_map.setdefault(row['industry_code'], []).append(row['stock_code'])
        logger.info(f"成分股关系: {len(member_df)} 条")
        
        # Step 3: 加载行业日线
        if progress_cb:
            progress_cb(2, 8, '加载行业日线')
        
        codes_str = "','".join(industry_codes)
        industry_daily_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg, turnover_rate, vol
            FROM ths_daily
            WHERE ts_code IN ('{codes_str}')
              AND trade_date >= '{start_date}' AND trade_date <= '{end_date}'
            ORDER BY ts_code, trade_date
        """, conn_industry)
        logger.info(f"行业日线: {len(industry_daily_df)} 条")
        
        # Step 4: 加载个股日线
        if progress_cb:
            progress_cb(3, 8, '加载个股日线')
        
        # 获取交易日列表
        trade_dates = sorted(industry_daily_df['trade_date'].unique())
        
        # 只加载需要的个股
        all_member_stocks = set()
        for members in member_map.values():
            all_member_stocks.update(members)
        
        stock_daily_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg, amount
            FROM daily
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """, conn_stock)
        logger.info(f"个股日线: {len(stock_daily_df)} 条")
        
        # Step 5: 加载涨跌停数据
        if progress_cb:
            progress_cb(4, 8, '加载涨跌停数据')
        
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
        conn_industry.close()
        
        # ==================== 计算五个子指标 ====================
        
        if progress_cb:
            progress_cb(5, 8, '计算F1: MA20多头占比')
        
        # F1: MA20 多头占比（需要历史MA数据）
        # 精确实现：加载更早的数据计算MA
        conn_stock = sqlite3.connect(STOCK_DB)
        
        # 加载MA20需要的前置数据（20个交易日）
        first_date = trade_dates[0] if trade_dates else start_date
        # 获取第一个交易日之前的20个交易日
        pre_dates = pd.read_sql_query(f"""
            SELECT DISTINCT trade_date FROM daily
            WHERE trade_date < '{first_date}'
            ORDER BY trade_date DESC LIMIT 20
        """, conn_stock)['trade_date'].tolist()
        
        ma_start = min(pre_dates) if pre_dates else first_date
        
        # 加载所有成分股的完整日线（含MA预热期）
        member_stocks_str = "','".join(list(all_member_stocks)[:5000])  # 限制数量
        stock_history_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg
            FROM daily
            WHERE trade_date >= '{ma_start}' AND trade_date <= '{end_date}'
        """, conn_stock)
        conn_stock.close()
        
        # 计算每只股票的MA20
        logger.info("计算MA20...")
        stock_history_df = stock_history_df.sort_values(['ts_code', 'trade_date'])
        stock_history_df['ma20'] = stock_history_df.groupby('ts_code')['close'].transform(
            lambda x: x.rolling(20, min_periods=20).mean()
        )
        stock_history_df['above_ma20'] = (stock_history_df['close'] >= stock_history_df['ma20']).astype(int)
        # 停牌判断：有收盘价即为正常交易
        stock_history_df['is_trading'] = 1
        
        # 逐日计算各行业MA20占比
        f1_map = {}
        for td in trade_dates:
            day_stocks = stock_history_df[stock_history_df['trade_date'] == td]
            for ind_code in industry_codes:
                members = member_map.get(ind_code, [])
                if not members:
                    continue
                member_data = day_stocks[day_stocks['ts_code'].isin(members)]
                total = len(member_data)
                if total == 0:
                    continue
                above = member_data['above_ma20'].sum()
                f1_map[(ind_code, td)] = round(above / total, 6)
        
        logger.info(f"F1 (MA20占比): {len(f1_map)} 条")
        
        # F2: RSI
        if progress_cb:
            progress_cb(5, 8, '计算F2: RSI相对强弱')
        
        f2_map = compute_rsi_normalized(industry_daily_df, BOCI_PARAMS['rsi_period'])
        logger.info(f"F2 (RSI): {len(f2_map)} 条")
        
        # F3: 换手率强度
        if progress_cb:
            progress_cb(6, 8, '计算F3: 换手率强度')
        
        f3_map = compute_turnover_strength(industry_daily_df, BOCI_PARAMS)
        logger.info(f"F3 (换手率): {len(f3_map)} 条")
        
        # F4: 涨跌停情绪差
        if progress_cb:
            progress_cb(6, 8, '计算F4: 涨跌停情绪差')
        
        f4_map = compute_limit_emotion_diff(
            limit_up_df, limit_down_df, member_map, industry_codes, BOCI_PARAMS
        )
        logger.info(f"F4 (涨跌停): {len(f4_map)} 条")
        
        # F5: 成交额占比
        if progress_cb:
            progress_cb(6, 8, '计算F5: 成交额占比')
        
        f5_map = compute_amount_ratio(stock_daily_df, member_map, industry_codes, BOCI_PARAMS)
        logger.info(f"F5 (成交额占比): {len(f5_map)} 条")
        
        # ==================== 合成综合情绪 ====================
        if progress_cb:
            progress_cb(7, 8, '合成综合情绪指标')
        
        sentiment_map = compute_composite_sentiment(
            f1_map, f2_map, f3_map, f4_map, f5_map,
            industry_codes, trade_dates
        )
        logger.info(f"综合情绪: {len(sentiment_map)} 条")
        
        # ==================== 五截面打分 ====================
        if progress_cb:
            progress_cb(7, 8, '五截面打分')
        
        score_df = compute_cross_section_scores(
            sentiment_map, industry_daily_df, BOCI_PARAMS,
            industry_codes, trade_dates
        )
        
        if isinstance(score_df, pd.DataFrame) and len(score_df) > 0:
            # 补充行业名称
            score_df['industry_name'] = score_df['industry_code'].map(industry_names)
            logger.info(f"截面打分: {len(score_df)} 条")
        else:
            logger.warning("截面打分结果为空")
            score_df = pd.DataFrame()
        
        # ==================== 回测 ====================
        if progress_cb:
            progress_cb(8, 8, '回测')
        
        backtest = compute_boci_backtest(score_df, industry_daily_df, BOCI_PARAMS)
        logger.info(f"回测: {len(backtest)} 条")
        
        if backtest:
            final_cum = backtest[-1]['cum_return']
            max_dd = 0
            peak = 1.0
            for b in backtest:
                if b['cum_return'] > peak:
                    peak = b['cum_return']
                dd = (peak - b['cum_return']) / peak
                if dd > max_dd:
                    max_dd = dd
            logger.info(f"最终净值: {final_cum:.4f}, 最大回撤: {-max_dd*100:.2f}%")
        
        # 构建子指标汇总（用于入库）
        sub_indicators = []
        for ind_code in industry_codes:
            for td in trade_dates:
                record = {
                    'industry_code': ind_code,
                    'industry_name': industry_names.get(ind_code, ''),
                    'trade_date': td,
                    'f1_ma20_ratio': f1_map.get((ind_code, td), None),
                    'f2_rsi_norm': f2_map.get((ind_code, td), None),
                    'f3_turnover_strength': f3_map.get((ind_code, td), None),
                    'f4_limit_diff': f4_map.get((ind_code, td), None),
                    'f5_amount_ratio': f5_map.get((ind_code, td), None),
                    'sentiment': sentiment_map.get((ind_code, td), None),
                }
                # 只有至少一个子指标有值才保存
                if any(v is not None for v in [record['f1_ma20_ratio'], record['f2_rsi_norm'],
                                                 record['f3_turnover_strength'], record['f4_limit_diff'],
                                                 record['f5_amount_ratio']]):
                    sub_indicators.append(record)
        
        return sub_indicators, score_df, backtest
        
    except Exception as e:
        logger.error(f"BOCI 计算失败: {e}", exc_info=True)
        raise
    finally:
        try:
            conn_stock.close()
        except:
            pass
        try:
            conn_industry.close()
        except:
            pass
        try:
            conn_limit.close()
        except:
            pass


def run_boci_incremental(existing_sentiment_df, existing_backtest, progress_cb=None):
    """
    BOCI 增量计算：只计算已有数据之后的新日期
    
    参数:
        existing_sentiment_df: 已有的 boci_sentiment 数据 (DataFrame)
        existing_backtest: 已有的回测数据 (list of dict)
        progress_cb: 进度回调 (current, total, msg)
    
    返回:
        new_sub_indicators: 新增的子指标记录 (list of dict)
        new_score_df: 新增的截面打分 (DataFrame)
        full_backtest: 完整回测 (list of dict, 含已有+新增)
    """
    conn_stock = sqlite3.connect(STOCK_DB)
    conn_industry = sqlite3.connect(INDUSTRY_DB)
    conn_limit = sqlite3.connect(LIMIT_DB)
    
    try:
        # 确定增量范围：已有数据最后日期的次日
        if existing_sentiment_df is not None and len(existing_sentiment_df) > 0:
            max_existing_date = existing_sentiment_df['trade_date'].max()
            # 找下一个交易日
            next_dates = conn_stock.execute(
                "SELECT DISTINCT trade_date FROM daily WHERE trade_date > ? ORDER BY trade_date LIMIT 1",
                (max_existing_date,)
            ).fetchone()
            if not next_dates:
                logger.info("BOCI 增量: 数据已是最新，无需更新")
                return [], pd.DataFrame(), existing_backtest
            start_date = next_dates[0]
        else:
            # 没有历史数据，退回全量计算
            logger.info("BOCI 增量: 无历史数据，退回全量计算")
            return run_boci_calculation('20200102', progress_cb=progress_cb)
        
        end_date = conn_stock.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
        logger.info(f"BOCI 增量计算范围: {start_date} ~ {end_date} (已有数据截止 {max_existing_date})")
        
        if progress_cb:
            progress_cb(0, 6, f'增量更新 {start_date} ~ {end_date}')
        
        # Step 1: 加载行业列表和成分股
        if progress_cb:
            progress_cb(1, 6, '加载行业与成分股')
        
        industry_df = load_industry_list(conn_industry)
        industry_codes = industry_df['ts_code'].tolist()
        industry_names = dict(zip(industry_df['ts_code'], industry_df['name']))
        
        member_df = load_industry_members(conn_industry, industry_codes)
        member_map = {}
        for _, row in member_df.iterrows():
            member_map.setdefault(row['industry_code'], []).append(row['stock_code'])
        logger.info(f"有效881行业: {len(industry_codes)} 个, 成分股关系: {len(member_df)} 条")
        
        # Step 2: 加载全量行业日线（expanding归一化需要历史序列）
        if progress_cb:
            progress_cb(2, 6, '加载行业日线（含历史）')
        
        codes_str = "','".join(industry_codes)
        industry_daily_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg, turnover_rate, vol
            FROM ths_daily
            WHERE ts_code IN ('{codes_str}')
            ORDER BY ts_code, trade_date
        """, conn_industry)
        
        # 获取新日期列表
        new_trade_dates = sorted(industry_daily_df[
            industry_daily_df['trade_date'] >= start_date
        ]['trade_date'].unique())
        
        if not new_trade_dates:
            logger.info("BOCI 增量: 无新交易日数据")
            return [], pd.DataFrame(), existing_backtest
        
        logger.info(f"新增交易日: {len(new_trade_dates)} 天 ({new_trade_dates[0]} ~ {new_trade_dates[-1]})")
        
        # Step 3: 加载个股日线 + 涨跌停（仅增量范围，但 F1 MA20 需要预热期）
        if progress_cb:
            progress_cb(3, 6, '加载个股日线')
        
        # F1 MA20 预热：需要 start_date 之前至少20个交易日
        pre_dates = pd.read_sql_query(f"""
            SELECT DISTINCT trade_date FROM daily
            WHERE trade_date < '{start_date}'
            ORDER BY trade_date DESC LIMIT 20
        """, conn_stock)['trade_date'].tolist()
        ma_start = min(pre_dates) if pre_dates else start_date
        
        all_member_stocks = set()
        for members in member_map.values():
            all_member_stocks.update(members)
        
        stock_daily_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg, amount
            FROM daily
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """, conn_stock)
        
        # F1 专用：含 MA 预热期的数据
        member_stocks_str = "','".join(list(all_member_stocks)[:5000])
        stock_history_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg
            FROM daily
            WHERE trade_date >= '{ma_start}' AND trade_date <= '{end_date}'
        """, conn_stock)
        conn_stock.close()
        
        limit_up_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date FROM limit_up
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """, conn_limit)
        limit_down_df = pd.read_sql_query(f"""
            SELECT ts_code, trade_date FROM limit_down
            WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        """, conn_limit)
        conn_limit.close()
        conn_industry.close()
        
        # Step 4: 计算五个子指标（只输出新增日期的值）
        if progress_cb:
            progress_cb(4, 6, '计算子指标 F1-F5')
        
        # F1: MA20多头占比（只取新增日期）
        f1_map = {}
        stock_history_df = stock_history_df.sort_values(['ts_code', 'trade_date'])
        stock_history_df['ma20'] = stock_history_df.groupby('ts_code')['close'].transform(
            lambda x: x.rolling(20, min_periods=20).mean()
        )
        stock_history_df['above_ma20'] = (stock_history_df['close'] >= stock_history_df['ma20']).astype(int)
        stock_history_df['is_trading'] = 1
        
        for td in new_trade_dates:
            day_stocks = stock_history_df[stock_history_df['trade_date'] == td]
            for ind_code in industry_codes:
                members = member_map.get(ind_code, [])
                if not members:
                    continue
                member_data = day_stocks[day_stocks['ts_code'].isin(members)]
                total = len(member_data)
                if total == 0:
                    continue
                above = member_data['above_ma20'].sum()
                f1_map[(ind_code, td)] = round(above / total, 6)
        logger.info(f"F1 (MA20占比) 增量: {len(f1_map)} 条")
        
        # F2: RSI（全量算，只取新增日期）
        f2_map_full = compute_rsi_normalized(industry_daily_df, BOCI_PARAMS['rsi_period'])
        f2_map = {k: v for k, v in f2_map_full.items() if k[1] in new_trade_dates}
        logger.info(f"F2 (RSI) 增量: {len(f2_map)} 条")
        
        # F3: 换手率强度（全量算，只取新增日期）
        f3_map_full = compute_turnover_strength(industry_daily_df, BOCI_PARAMS)
        f3_map = {k: v for k, v in f3_map_full.items() if k[1] in new_trade_dates}
        logger.info(f"F3 (换手率) 增量: {len(f3_map)} 条")
        
        # F4: 涨跌停情绪差（增量范围数据 + expanding归一化）
        f4_map_full = compute_limit_emotion_diff(
            pd.read_sql_query(f"""
                SELECT ts_code, trade_date FROM limit_up
                WHERE ts_code IN ('{member_stocks_str}')
            """, sqlite3.connect(LIMIT_DB)),
            pd.read_sql_query(f"""
                SELECT ts_code, trade_date FROM limit_down
                WHERE ts_code IN ('{member_stocks_str}')
            """, sqlite3.connect(LIMIT_DB)),
            member_map, industry_codes, BOCI_PARAMS
        )
        f4_map = {k: v for k, v in f4_map_full.items() if k[1] in new_trade_dates}
        logger.info(f"F4 (涨跌停) 增量: {len(f4_map)} 条")
        
        # F5: 成交额占比（需要250日滚动窗口，加载足够长的历史数据）
        # 获取 end_date 之前约300个交易日作为滚动窗口预热
        pre_dates_300 = pd.read_sql_query(f"""
            SELECT DISTINCT trade_date FROM daily
            WHERE trade_date <= '{end_date}'
            ORDER BY trade_date DESC LIMIT 300
        """, sqlite3.connect(STOCK_DB))['trade_date'].tolist()
        f5_start = min(pre_dates_300) if pre_dates_300 else start_date
        conn_stock_tmp = sqlite3.connect(STOCK_DB)
        stock_daily_full = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, close, pct_chg, amount
            FROM daily
            WHERE trade_date >= '{f5_start}' AND trade_date <= '{end_date}'
        """, conn_stock_tmp)
        conn_stock_tmp.close()
        f5_map_full = compute_amount_ratio(stock_daily_full, member_map, industry_codes, BOCI_PARAMS)
        f5_map = {k: v for k, v in f5_map_full.items() if k[1] in new_trade_dates}
        logger.info(f"F5 (成交额占比) 增量: {len(f5_map)} 条")
        
        # Step 5: 合成综合情绪（只算新增日期）
        if progress_cb:
            progress_cb(5, 6, '合成情绪 + 截面打分')
        
        # 将已有情绪数据合并到 map 中（截面打分需要历史情绪算斜率等）
        sentiment_map_existing = {}
        if existing_sentiment_df is not None and len(existing_sentiment_df) > 0:
            for _, row in existing_sentiment_df.iterrows():
                if pd.notna(row.get('sentiment')):
                    sentiment_map_existing[(row['industry_code'], row['trade_date'])] = row['sentiment']
        
        # 合成新增日期的综合情绪
        new_sentiment_map = compute_composite_sentiment(
            f1_map, f2_map, f3_map, f4_map, f5_map,
            industry_codes, new_trade_dates
        )
        logger.info(f"新增综合情绪: {len(new_sentiment_map)} 条")
        
        # 合并全部情绪用于截面打分（截面排名需要当日全行业数据）
        full_sentiment_map = {**sentiment_map_existing, **new_sentiment_map}
        all_dates_for_score = sorted(set(
            [k[1] for k in full_sentiment_map.keys()]
        ))
        
        # 截面打分：只需要新增日期的结果，但 S1/S2 需要历史情绪序列
        score_df = compute_cross_section_scores(
            full_sentiment_map, industry_daily_df, BOCI_PARAMS,
            industry_codes, all_dates_for_score
        )
        
        # 只保留新增日期的打分结果
        if isinstance(score_df, pd.DataFrame) and len(score_df) > 0:
            new_score_df = score_df[score_df['trade_date'].isin(new_trade_dates)].copy()
            new_score_df['industry_name'] = new_score_df['industry_code'].map(industry_names)
            logger.info(f"截面打分增量: {len(new_score_df)} 条")
        else:
            new_score_df = pd.DataFrame()
            logger.warning("截面打分结果为空")
        
        # Step 6: 增量回测
        if progress_cb:
            progress_cb(6, 6, '增量回测')
        
        # 从已有回测的末尾继续算
        if existing_backtest and len(existing_backtest) > 0:
            last_cum_return = existing_backtest[-1]['cum_return']
            last_signal_date = existing_backtest[-1]['signal_date']
            # 已有回测的所有日期
            existing_bt_dates = set(b['signal_date'] for b in existing_backtest)
        else:
            last_cum_return = 1.0
            last_signal_date = None
            existing_bt_dates = set()
        
        # 用全量 score_df 做回测（需要连续信号）
        if isinstance(score_df, pd.DataFrame) and len(score_df) > 0:
            new_backtest_part = compute_boci_backtest_incremental(
                score_df, industry_daily_df, BOCI_PARAMS,
                last_cum_return, last_signal_date
            )
            full_backtest = list(existing_backtest) + new_backtest_part
        else:
            new_backtest_part = []
            full_backtest = list(existing_backtest)
        
        logger.info(f"增量回测: 新增 {len(new_backtest_part)} 条, 总计 {len(full_backtest)} 条")
        
        # 构建新增子指标记录
        new_sub_indicators = []
        for ind_code in industry_codes:
            for td in new_trade_dates:
                record = {
                    'industry_code': ind_code,
                    'industry_name': industry_names.get(ind_code, ''),
                    'trade_date': td,
                    'f1_ma20_ratio': f1_map.get((ind_code, td), None),
                    'f2_rsi_norm': f2_map.get((ind_code, td), None),
                    'f3_turnover_strength': f3_map.get((ind_code, td), None),
                    'f4_limit_diff': f4_map.get((ind_code, td), None),
                    'f5_amount_ratio': f5_map.get((ind_code, td), None),
                    'sentiment': new_sentiment_map.get((ind_code, td), None),
                }
                if any(v is not None for v in [record['f1_ma20_ratio'], record['f2_rsi_norm'],
                                                 record['f3_turnover_strength'], record['f4_limit_diff'],
                                                 record['f5_amount_ratio']]):
                    new_sub_indicators.append(record)
        
        return new_sub_indicators, new_score_df, full_backtest
        
    except Exception as e:
        logger.error(f"BOCI 增量计算失败: {e}", exc_info=True)
        raise
    finally:
        try:
            conn_stock.close()
        except:
            pass
        try:
            conn_industry.close()
        except:
            pass
        try:
            conn_limit.close()
        except:
            pass


def compute_boci_backtest_incremental(score_df, industry_daily_df, params,
                                       last_cum_return=1.0, last_signal_date=None):
    """
    BOCI 回测增量计算：从已有最后状态续算
    
    参数:
        score_df: 全量截面打分结果
        industry_daily_df: 行业日线
        params: 策略参数
        last_cum_return: 已有回测最后净值
        last_signal_date: 已有回测最后信号日期
    
    返回:
        新增回测记录 (list of dict)
    """
    if score_df is None or len(score_df) == 0:
        return []
    
    rebalance_days = params['rebalance_days']
    top_n = params['top_n']
    
    all_dates = sorted(score_df['trade_date'].unique())
    
    # 找到续算起点
    if last_signal_date:
        start_idx = 0
        for i, d in enumerate(all_dates):
            if d > last_signal_date:
                start_idx = i
                break
    else:
        start_idx = 0
    
    if start_idx >= len(all_dates) - 1:
        return []
    
    # 行业日收益率映射
    ind_ret_map = {}
    for ind_code in industry_daily_df['ts_code'].unique():
        ind_data = industry_daily_df[industry_daily_df['ts_code'] == ind_code].sort_values('trade_date')
        dates = ind_data['trade_date'].values
        pct = ind_data['pct_chg'].values
        for i in range(len(dates)):
            ind_ret_map[(ind_code, dates[i])] = pct[i] if pd.notna(pct[i]) else 0
    
    backtest = []
    cum_return = last_cum_return
    current_holdings = []
    
    # 需要确定从续算点开始的调仓周期
    # 从续算开始重新遍历，但需要确定当前是否在持仓周期中
    for i in range(start_idx, len(all_dates)):
        date = all_dates[i]
        
        # 计算在全部日期中的位置来判断调仓
        global_idx = i
        if global_idx % rebalance_days == 0:
            day_scores = score_df[score_df['trade_date'] == date].sort_values('final_score', ascending=False)
            current_holdings = day_scores.head(top_n)['industry_code'].tolist()
        
        if i + 1 < len(all_dates):
            next_date = all_dates[i + 1]
            
            if current_holdings:
                returns = []
                for ind_code in current_holdings:
                    ret = ind_ret_map.get((ind_code, next_date), np.nan)
                    if pd.notna(ret):
                        returns.append(ret / 100.0)
                
                if returns:
                    daily_ret = np.mean(returns)
                    daily_ret = np.clip(daily_ret, -0.2, 0.2)
                    cum_return *= (1 + daily_ret)
                else:
                    daily_ret = 0.0
            else:
                daily_ret = 0.0
            
            backtest.append({
                'signal_date': date,
                'return_date': next_date,
                'daily_return': round(daily_ret * 100, 4),
                'cum_return': round(cum_return, 4),
                'top_industries': current_holdings,
            })
    
    return backtest


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    
    sub_indicators, score_df, backtest = run_boci_calculation('20200102')
    
    print(f"\n子指标记录: {len(sub_indicators)}")
    print(f"截面打分记录: {len(score_df) if isinstance(score_df, pd.DataFrame) else 0}")
    print(f"回测记录: {len(backtest)}")
    
    if backtest:
        print(f"\n最终净值: {backtest[-1]['cum_return']:.4f}")
        print(f"最近5次调仓:")
        for b in backtest[-5:]:
            print(f"  {b['signal_date']} 日收益{b['daily_return']:+.2f}% 净值{b['cum_return']:.4f} 持仓{b['top_industries']}")
