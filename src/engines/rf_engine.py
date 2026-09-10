# -*- coding: utf-8 -*-
"""
Range Filter（范围滤波器）趋势跟踪引擎

忠实还原 TradingView PineScript v5 版本（@DonovanWall → @guikroth → @tvenn）
关键差异（相比之前的错误实现）：
  1. smoothrng: ema(abs(src - src[1]), per) → ema(avrng, per*2-1) * mult
     （不是 abs(close - sma)，第二次 EMA 周期是 per*2-1 而非 per）
  2. rngfilt: 用 src(close) 判断，不是 SMA
  3. 信号判定: close > filt && upward > 0，不需要 smooth 条件
"""

import sqlite3
import numpy as np
import pandas as pd
import logging

logger = logging.getLogger('rf_engine')

# 数据库路径
INDEX_DB = '/Volumes/BEANPAPER/data/databases/index_daily.db'

# ==================== Range Filter 参数 ====================
RF_PARAMS = {
    'period': 8,        # 采样周期
    'multiplier': 3.0,  # 倍数（波动率放大系数）
    'source': 'close',  # 数据源
}

# 上证指数代码
INDEX_CODE = '000001.SH'


def load_index_daily(start_date='20200101', end_date=None):
    """加载上证指数日线数据"""
    conn = sqlite3.connect(INDEX_DB)
    try:
        if end_date is None:
            end_date = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]

        df = pd.read_sql_query(f"""
            SELECT trade_date, open, high, low, close, vol
            FROM daily
            WHERE ts_code = '{INDEX_CODE}' AND trade_date >= '{start_date}' AND trade_date <= '{end_date}'
            ORDER BY trade_date
        """, conn)
        return df
    finally:
        conn.close()


def compute_range_filter(df, period=8, multiplier=3.0):
    """
    计算 Range Filter 指标 — 精确还原 PineScript v5 版本

    PineScript 原版逻辑：

    // Smooth Average Range
    smoothrng(x, t, m) =>
        wper = t * 2 - 1
        avrng = ta.ema(math.abs(x - x[1]), t)
        smoothrng = ta.ema(avrng, wper) * m

    // Range Filter
    rngfilt(x, r) =>
        rngfilt = x
        rngfilt := x > nz(rngfilt[1]) ? x - r < nz(rngfilt[1]) ? nz(rngfilt[1]) : x - r :
           x + r > nz(rngfilt[1]) ? nz(rngfilt[1]) : x + r

    // Filter Direction
    upward = filt > filt[1] ? nz(upward[1]) + 1 : filt < filt[1] ? 0 : nz(upward[1])
    downward = filt < filt[1] ? nz(downward[1]) + 1 : filt > filt[1] ? 0 : nz(downward[1])

    // Break Outs
    longCond = src > filt and upward > 0
    shortCond = src < filt and downward > 0

    // 状态切换信号（只在多空翻转时触发）
    longCondition = longCond and CondIni[1] == -1
    shortCondition = shortCond and CondIni[1] == 1

    参数:
        df: DataFrame，需包含 close 列
        period: 采样周期（默认8）
        multiplier: 波动率倍数（默认3.0）

    返回: DataFrame，新增 smooth_range, filter_line, upward, signal 列
    """
    src = df['close'].values.astype(float)
    n = len(src)

    if n < 2:
        df = df.copy()
        df['smooth_range'] = 0.0
        df['filter_line'] = src[0]
        df['upward'] = 0
        df['signal'] = 1
        return df

    # ==================== 1. EMA 辅助函数 ====================
    def ema(data, span):
        """PineScript ta.ema 的精确还原"""
        result = np.zeros(n)
        alpha = 2.0 / (span + 1)
        result[0] = data[0]
        for i in range(1, n):
            result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
        return result

    # ==================== 2. smoothrng(x, t, m) ====================
    # avrng = ta.ema(math.abs(x - x[1]), t)
    # smoothrng = ta.ema(avrng, wper) * m   其中 wper = t * 2 - 1
    wper = period * 2 - 1
    diff = np.zeros(n)
    diff[0] = 0.0
    for i in range(1, n):
        diff[i] = abs(src[i] - src[i - 1])
    avrng = ema(diff, period)
    smooth_range = ema(avrng, wper) * multiplier

    # ==================== 3. rngfilt(x, r) ====================
    # PineScript:
    #   rngfilt = x
    #   rngfilt := x > nz(rngfilt[1]) ?
    #       x - r < nz(rngfilt[1]) ? nz(rngfilt[1]) : x - r :
    #       x + r > nz(rngfilt[1]) ? nz(rngfilt[1]) : x + r
    filter_line = np.zeros(n)
    filter_line[0] = src[0]  # 第一根bar的初始值

    for i in range(1, n):
        x = src[i]
        r = smooth_range[i]
        prev_filt = filter_line[i - 1]

        if x > prev_filt:
            # close 在 filter 上方
            # 如果 x - r 还在 prev_filt 下方，就保持 prev_filt（粘性）
            # 否则更新为 x - r
            filter_line[i] = prev_filt if (x - r) < prev_filt else (x - r)
        else:
            # close 在 filter 下方
            # 如果 x + r 还在 prev_filt 上方，就保持 prev_filt（粘性）
            # 否则更新为 x + r
            filter_line[i] = prev_filt if (x + r) > prev_filt else (x + r)

    # ==================== 4. Filter Direction ====================
    # upward = filt > filt[1] ? nz(upward[1]) + 1 : filt < filt[1] ? 0 : nz(upward[1])
    # downward = filt < filt[1] ? nz(downward[1]) + 1 : filt > filt[1] ? 0 : nz(downward[1])
    upward = np.zeros(n, dtype=int)
    downward = np.zeros(n, dtype=int)

    for i in range(1, n):
        if filter_line[i] > filter_line[i - 1]:
            upward[i] = upward[i - 1] + 1
            downward[i] = 0
        elif filter_line[i] < filter_line[i - 1]:
            upward[i] = 0
            downward[i] = downward[i - 1] + 1
        else:
            upward[i] = upward[i - 1]
            downward[i] = downward[i - 1]

    # ==================== 5. 信号判定 ====================
    # PineScript:
    #   longCond  = src > filt and upward > 0
    #   shortCond = src < filt and downward > 0
    #   CondIni := longCond ? 1 : shortCond ? -1 : CondIni[1]
    #   longCondition  = longCond and CondIni[1] == -1   ← 翻转信号
    #   shortCondition = shortCond and CondIni[1] == 1
    #
    # 注意: longCond/shortCond 是当前bar的条件，CondIni 是状态变量
    # signal 维持上一次翻转后的状态（1=多头, -1=空头），直到下一次翻转
    signal = np.zeros(n, dtype=int)  # 1=buy(long), -1=sell(flat)
    cond_ini = 0  # CondIni 状态变量（1=多头, -1=空头, 0=初始）

    for i in range(n):
        # 判断当前 bar 的 longCond / shortCond
        long_cond = src[i] > filter_line[i] and upward[i] > 0
        short_cond = src[i] < filter_line[i] and downward[i] > 0

        # CondIni 状态更新（参考 [1] 即上一bar的值）
        prev_cond = cond_ini
        if long_cond:
            cond_ini = 1
        elif short_cond:
            cond_ini = -1
        # else: cond_ini 保持不变

        # 翻转信号：longCond 成立且前一个状态是空头 → Buy
        if long_cond and prev_cond == -1:
            signal[i] = 1
        elif short_cond and prev_cond == 1:
            signal[i] = -1
        else:
            # 保持前一个信号
            signal[i] = signal[i - 1] if i > 0 else 1

    df = df.copy()
    df['smooth_range'] = np.round(smooth_range, 4)
    df['filter_line'] = np.round(filter_line, 4)
    df['upward'] = upward
    df['signal'] = signal  # 1=buy, -1=sell

    return df


def compute_rf_signals(start_date='20230101', end_date=None):
    """
    计算上证指数的 Range Filter 趋势信号

    注意：为给 EMA 充分预热，实际从 2020-01-01 开始加载数据计算，
    但只返回 start_date 之后的结果。

    返回: DataFrame，包含 trade_date, close, smooth_range, filter_line, upward, signal
    """
    # 从 2020 年开始加载，给 EMA 充分预热
    warmup_start = min(start_date, '20200101')
    df = load_index_daily(warmup_start, end_date)
    if df.empty:
        logger.warning("上证指数日线数据为空")
        return df

    df = compute_range_filter(df, period=RF_PARAMS['period'], multiplier=RF_PARAMS['multiplier'])

    # 只返回 start_date 之后的数据
    if start_date != warmup_start:
        df = df[df['trade_date'] >= start_date].copy()

    # 统计
    buy_days = int((df['signal'] == 1).sum())
    sell_days = int((df['signal'] == -1).sum())
    logger.info(f"Range Filter 信号: 总{len(df)}天, Buy={buy_days}天, Sell={sell_days}天")

    return df


def get_buy_sell_map(start_date='20230101', end_date=None):
    """
    获取每日的 buy/sell 信号映射

    返回: dict, {trade_date: 1 or -1}
    """
    df = compute_rf_signals(start_date, end_date)
    return dict(zip(df['trade_date'], df['signal']))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    df = compute_rf_signals('20230101')
    print(f"\n最近10天信号:")
    print(df[['trade_date', 'close', 'smooth_range', 'filter_line', 'upward', 'signal']].tail(10).to_string(index=False))

    # 统计
    buy_pct = (df['signal'] == 1).sum() / len(df) * 100
    sell_pct = (df['signal'] == -1).sum() / len(df) * 100
    print(f"\nBuy 占比: {buy_pct:.1f}%, Sell 占比: {sell_pct:.1f}%")
