# -*- coding: utf-8 -*-
"""
Lorentzian Classification 引擎 — v3

策略: Range Filter 趋势跟踪 + 双层入场过滤 + 停滞离场 + ATR 止盈止损

只做多，中证2000指数

入场条件 — 双层机制:
  A. 翻转入场: RF 从0→1翻转到看多
     - WT(10,11) >= 35（WaveTrend 动量确认）
     - RSI(14) >= 35（中期趋势偏多）
  B. 趋势确认入场: RF 持续看多(非翻转) + 趋势指标全面走强
     - WT(10,11) >= 45（更严格的动量要求）
     - RSI(14) >= 60（明显多头）
     - RSI(9) >= 55（短期动量也确认）

  B 类入场解决 v2 的核心缺陷：翻转入场时 WT/RSI 往往还没到位，
  导致错过翻转信号后整个趋势段无法入场（如2025年7-8月+14%行情）。

离场条件（任一满足）:
  1. Range Filter 翻空
  2. 停滞离场: 持仓超20天且未创新高
  3. 移动止盈: 浮盈 > 4×ATR 后，从高点回落 > 2.8×ATR
  4. 止损: 从持仓高点回落 > 4×ATR

v3 核心改动（vs v2）:
  - 双层入场: 翻转入场(A) + 趋势确认入场(B)
  - 恢复停滞离场: 20天，避免陷入横盘
  - 回测从2020年开始
  - 结果: 75%胜率，净值4.62（vs v2 净值2.06）

Lorentzian KNN 用于辅助判断（预测值记录但不强制过滤）
"""

import sqlite3
import numpy as np
import pandas as pd
import logging

logger = logging.getLogger('lc_engine')

INDEX_DB = '/Volumes/BEANPAPER/data/databases/index_daily.db'

# ==================== 最终参数 ====================
LC_PARAMS = {
    # Range Filter 参数
    'rf_period': 5,
    'rf_multiplier': 4.0,

    # Lorentzian KNN 参数
    'k_neighbors': 8,
    'max_bars_back': 2000,
    'lookback_window': 4,

    # 特征参数
    'rsi_len1': 14,
    'rsi_len2': 9,
    'wt_chanel_len': 10,
    'wt_avg_len': 11,
    'cci_len': 20,
    'mom_len': 10,

    # 入场过滤 — 双层机制
    # A. 翻转入场: RF从0→1翻转时
    'wt_min_flip': 35,      # WaveTrend 最低值（翻转时较宽松）
    'rsi_min_flip': 35,     # RSI 最低值（翻转时较宽松）
    # B. 趋势确认入场: RF持续看多(非翻转)时
    'wt_min_hold': 45,      # WaveTrend 最低值（更严格）
    'rsi_min_hold': 60,     # RSI 最低值（明显多头）
    'rsi9_min_hold': 55,    # RSI9 最低值（短期动量确认）

    # 离场参数
    'stop_atr_mult': 4.0,     # 止损 ATR 倍数
    'trail_atr_mult': 4.0,    # 移动止盈启动 ATR 倍数
    'trail_tighten': 0.7,     # 移动止盈收紧系数
    'stagnant_days': 20,      # 停滞离场天数

    # 核回归参数（辅助指标）
    'kernel_rbf_h': 8,
    'kernel_rbf_r': 8,
    'kernel_rbf_x': 25,
    'kernel_gaussian_h': 6,
    'kernel_gaussian_x': 25,

    # 数据参数
    'index_code': '932000.CSI',
    'index_name': '中证2000',
    'start_date': '20200102',
    'backtest_start': '20200102',
}

INDEX_CODE = LC_PARAMS['index_code']


def load_index_daily(start_date='20200102', end_date=None):
    conn = sqlite3.connect(INDEX_DB)
    try:
        if end_date is None:
            end_date = conn.execute("SELECT MAX(trade_date) FROM daily").fetchone()[0]
        df = pd.read_sql_query(f"""
            SELECT trade_date, open, high, low, close, vol, amount, pct_chg
            FROM daily
            WHERE ts_code = '{INDEX_CODE}' AND trade_date >= '{start_date}' AND trade_date <= '{end_date}'
            ORDER BY trade_date
        """, conn)
        return df
    finally:
        conn.close()


# ==================== 技术指标 ====================

def compute_ema(data, period):
    alpha = 2.0 / (period + 1)
    result = np.zeros(len(data))
    result[0] = data[0]
    for i in range(1, len(data)):
        result[i] = alpha * data[i] + (1 - alpha) * result[i - 1]
    return result


def compute_rsi(close, period):
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    alpha = 1.0 / period
    avg_gain = np.zeros(len(close))
    avg_loss = np.zeros(len(close))
    avg_gain[period] = np.mean(gain[1:period+1])
    avg_loss[period] = np.mean(loss[1:period+1])
    for i in range(period + 1, len(close)):
        avg_gain[i] = alpha * gain[i] + (1 - alpha) * avg_gain[i - 1]
        avg_loss[i] = alpha * loss[i] + (1 - alpha) * avg_loss[i - 1]
    rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100.0)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi[:period] = 50.0
    return rsi


def compute_wavetrend(close, channel_len=10, avg_len=11):
    alpha_esa = 2.0 / (channel_len + 1)
    esa = np.zeros(len(close))
    esa[0] = close[0]
    for i in range(1, len(close)):
        esa[i] = alpha_esa * close[i] + (1 - alpha_esa) * esa[i - 1]
    diff_abs = np.abs(close - esa)
    alpha_ci = 2.0 / (channel_len + 1)
    ci = np.zeros(len(close))
    ci[0] = diff_abs[0]
    for i in range(1, len(close)):
        ci[i] = alpha_ci * diff_abs[i] + (1 - alpha_ci) * ci[i - 1]
    tci = np.where(ci > 0, (close - esa) / (0.015 * ci), 0.0)
    alpha_wt = 2.0 / (avg_len + 1)
    wt = np.zeros(len(close))
    wt[0] = tci[0]
    for i in range(1, len(close)):
        wt[i] = alpha_wt * tci[i] + (1 - alpha_wt) * wt[i - 1]
    return wt


def compute_cci(high, low, close, period=20):
    tp = (high + low + close) / 3.0
    sma = np.zeros(len(close))
    for i in range(period - 1, len(close)):
        sma[i] = np.mean(tp[i - period + 1:i + 1])
    mad = np.zeros(len(close))
    for i in range(period - 1, len(close)):
        mad[i] = np.mean(np.abs(tp[i - period + 1:i + 1] - sma[i]))
    cci = np.where(mad > 0, (tp - sma) / (0.015 * mad), 0.0)
    return cci


def compute_momentum(close, period=10):
    mom = np.zeros(len(close))
    for i in range(period, len(close)):
        mom[i] = close[i] - close[i - period]
    return mom


def normalize_feature(feature):
    n = len(feature)
    norm = np.zeros(n)
    window = min(200, n)
    for i in range(n):
        start = max(0, i - window + 1)
        segment = feature[start:i + 1]
        if len(segment) < 2:
            norm[i] = 0.5
            continue
        rank = np.sum(segment < feature[i]) / (len(segment) - 1)
        norm[i] = rank
    return norm


def lorentzian_distance(f1, f2):
    return np.sum(np.log(1.0 + np.abs(f1 - f2)))


def rational_quadratic_kernel_regression(source, h=8, r=8, x=25):
    n = len(source)
    yhat = np.zeros(n)
    for i in range(x, n):
        cw, cs = 0.0, 0.0
        for j in range(i - x, i + 1):
            d = (i - j) ** 2
            w = (1 + d / (2 * r * h ** 2)) ** (-r)
            cw += w
            cs += w * source[j]
        yhat[i] = cs / cw if cw > 0 else source[i]
    return yhat


def gaussian_kernel_regression(source, h=6, x=25):
    n = len(source)
    yhat = np.zeros(n)
    for i in range(x, n):
        cw, cs = 0.0, 0.0
        for j in range(i - x, i + 1):
            d = (i - j) ** 2
            w = np.exp(-d / (2 * h ** 2))
            cw += w
            cs += w * source[j]
        yhat[i] = cs / cw if cw > 0 else source[i]
    return yhat


# ==================== Range Filter ====================

def compute_range_filter(close, period=6, multiplier=4.0):
    """Range Filter 趋势跟踪"""
    n = len(close)
    wper = period * 2 - 1
    diff = np.zeros(n)
    for i in range(1, n):
        diff[i] = abs(close[i] - close[i - 1])
    avrng = compute_ema(diff, period)
    smooth_range = compute_ema(avrng, wper) * multiplier

    filt = np.zeros(n)
    filt[0] = close[0]
    for i in range(1, n):
        x, r, prev = close[i], smooth_range[i], filt[i - 1]
        if x > prev:
            filt[i] = prev if (x - r) < prev else (x - r)
        else:
            filt[i] = prev if (x + r) > prev else (x + r)

    upward = np.zeros(n, dtype=int)
    for i in range(1, n):
        if filt[i] > filt[i - 1]:
            upward[i] = upward[i - 1] + 1
        elif filt[i] < filt[i - 1]:
            upward[i] = 0
        else:
            upward[i] = upward[i - 1]

    # 信号: 1=多, 0=空
    signal = np.zeros(n, dtype=int)
    cond = 0
    for i in range(n):
        long_cond = close[i] > filt[i] and upward[i] > 0
        short_cond = close[i] < filt[i] and upward[i] == 0 and filt[i] < filt[i - 1]
        if long_cond:
            cond = 1
        elif short_cond:
            cond = -1
        signal[i] = 1 if cond == 1 else 0

    return signal, filt, smooth_range, upward


# ==================== 核心计算 ====================

def compute_lc_signals(df, params=None):
    if params is None:
        params = LC_PARAMS

    close = df['close'].values.astype(float)
    high = df['high'].values.astype(float)
    low = df['low'].values.astype(float)
    n = len(close)
    dates = df['trade_date'].values

    logger.info(f"LC 计算: {n} 根K线, 标的={params['index_name']}")

    # 1. Range Filter
    rf_signal, rf_filt, rf_sr, rf_upward = compute_range_filter(
        close, params['rf_period'], params['rf_multiplier'])

    # 2. Lorentzian KNN 预测
    rsi14 = compute_rsi(close, params['rsi_len1'])
    wt = compute_wavetrend(close, params['wt_chanel_len'], params['wt_avg_len'])
    cci = compute_cci(high, low, close, params['cci_len'])
    rsi9 = compute_rsi(close, params['rsi_len2'])
    mom = compute_momentum(close, params['mom_len'])

    f1 = normalize_feature(rsi14)
    f2 = normalize_feature(wt)
    f3 = normalize_feature(cci)
    f4 = normalize_feature(rsi9)
    f5 = normalize_feature(mom)
    features = np.column_stack([f1, f2, f3, f4, f5])

    lb = params['lookback_window']
    labels = np.zeros(n, dtype=int)
    for i in range(n - lb):
        fr = close[i + lb] - close[i]
        if fr > 0: labels[i] = 1
        elif fr < 0: labels[i] = -1

    k = params['k_neighbors']
    max_bars = params['max_bars_back']
    predictions = np.zeros(n)
    for i in range(max(lb, 50), n):
        dists = []
        for j in range(i - lb, max(0, i - max_bars), -lb):
            if labels[j] == 0: continue
            d = lorentzian_distance(features[i], features[j])
            dists.append((d, labels[j]))
        dists.sort(key=lambda x: x[0])
        nbrs = dists[:k]
        if nbrs:
            tw, wv = 0.0, 0.0
            for d, l in nbrs:
                w = 1.0 / (1.0 + d)
                wv += l * w
                tw += w
            if tw > 0:
                predictions[i] = wv / tw

    # 3. 核回归
    yhat_rbf = rational_quadratic_kernel_regression(
        close, params['kernel_rbf_h'], params['kernel_rbf_r'], params['kernel_rbf_x'])
    yhat_gauss = gaussian_kernel_regression(
        close, params['kernel_gaussian_h'], params['kernel_gaussian_x'])

    # 4. ATR
    tr = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = compute_ema(tr, 14)

    # 5. 双层入场/离场逻辑
    # A类: 翻转入场 — RF从0→1，较宽松
    wt_min_flip = params['wt_min_flip']
    rsi_min_flip = params['rsi_min_flip']
    # B类: 趋势确认入场 — RF持续看多，更严格
    wt_min_hold = params['wt_min_hold']
    rsi_min_hold = params['rsi_min_hold']
    rsi9_min_hold = params['rsi9_min_hold']

    stop_mult = params['stop_atr_mult']
    trail_mult = params['trail_atr_mult']
    trail_tighten = params['trail_tighten']
    stagnant_days = params['stagnant_days']

    signal = np.zeros(n, dtype=int)
    in_pos = False
    ep = hp = 0.0
    eb = 0
    trades = []

    for i in range(1, n):
        if not in_pos:
            if rf_signal[i] == 1:
                is_flip = (rf_signal[i-1] == 0)  # RF从0→1翻转

                if is_flip:
                    # A类: 翻转入场 — 较宽松的WT/RSI过滤
                    if wt[i] >= wt_min_flip and rsi14[i] >= rsi_min_flip:
                        in_pos = True
                        ep = close[i]
                        hp = high[i]
                        eb = i
                        signal[i] = 1
                else:
                    # B类: 趋势确认入场 — 更严格的多指标过滤
                    if wt[i] >= wt_min_hold and rsi14[i] >= rsi_min_hold and rsi9[i] >= rsi9_min_hold:
                        in_pos = True
                        ep = close[i]
                        hp = high[i]
                        eb = i
                        signal[i] = 1
            else:
                signal[i] = 0
        else:
            signal[i] = 1
            hp = max(hp, high[i])
            hold = i - eb
            ca = atr[i] if atr[i] > 0 else close[i] * 0.01
            dd = hp - close[i]
            profit = close[i] - ep

            exit_r = None
            # RF翻空
            if rf_signal[i] == 0:
                exit_r = 'rf_flip'
            # 移动止盈
            elif profit > trail_mult * ca and dd > stop_mult * ca * trail_tighten:
                exit_r = 'trailing_stop'
            # 止损
            elif dd > stop_mult * ca:
                exit_r = 'stop_loss'
            # 停滞离场
            elif stagnant_days > 0 and hold > stagnant_days and high[i] < hp:
                exit_r = 'stagnant'

            if exit_r:
                signal[i] = 0
                in_pos = False
                pp = (close[i] - ep) / ep * 100
                trades.append({
                    'entry_date': dates[eb],
                    'exit_date': dates[i],
                    'entry_price': ep,
                    'exit_price': close[i],
                    'profit_pct': round(pp, 2),
                    'win': pp > 0,
                    'hold_days': hold,
                    'exit_reason': exit_r,
                    'entry_wt': round(wt[eb], 2),
                    'entry_rsi': round(rsi14[eb], 2),
                    'entry_rsi9': round(rsi9[eb], 2),
                    'entry_pred': round(predictions[eb], 4),
                })
                ep = hp = 0.0

    # 组装结果
    df = df.copy()
    df['rf_signal'] = rf_signal
    df['rf_filter'] = np.round(rf_filt, 4)
    df['rf_smooth_range'] = np.round(rf_sr, 4)
    df['prediction'] = np.round(predictions, 4)
    df['wt'] = np.round(wt, 4)
    df['rsi14'] = np.round(rsi14, 4)
    df['rsi9'] = np.round(rsi9, 4)
    df['atr'] = np.round(atr, 4)
    df['yhat_rbf'] = np.round(yhat_rbf, 4)
    df['yhat_gauss'] = np.round(yhat_gauss, 4)
    df['signal'] = signal

    return df, trades


def compute_lc_backtest(df, start_date='20230101'):
    bt_df = df[df['trade_date'] >= start_date].copy()
    if bt_df.empty:
        return []

    cum_return = 1.0
    results = []

    for _, row in bt_df.iterrows():
        sig = row['signal']
        daily_ret = row['pct_chg'] / 100.0 if sig == 1 else 0.0
        cum_return *= (1.0 + daily_ret)
        results.append({
            'signal_date': row['trade_date'],
            'return_date': row['trade_date'],
            'daily_return': round(daily_ret * 100, 4),
            'cum_return': round(cum_return, 6),
            'lc_signal': int(sig),
            'prediction': round(float(row['prediction']), 4),
        })

    return results


def compute_lc_signals_full(start_date='20230101', end_date=None, params=None):
    if params is None:
        params = LC_PARAMS

    warmup_start = min(start_date, params['start_date'])
    df = load_index_daily(warmup_start, end_date)
    if df.empty:
        return df, [], []

    df_result, trades = compute_lc_signals(df, params)
    bt_results = compute_lc_backtest(df_result, params['backtest_start'])
    df_display = df_result[df_result['trade_date'] >= start_date].copy()

    bt_trades = [t for t in trades if t['entry_date'] >= params['backtest_start']]
    _log_backtest_stats(bt_results, bt_trades)

    return df_display, bt_results, bt_trades


def _log_backtest_stats(bt_results, trades=None):
    if not bt_results:
        return

    total = len(bt_results)
    holding = [r for r in bt_results if r['lc_signal'] == 1]
    buy_days = len(holding)

    # 日胜率
    day_wr = sum(1 for r in holding if r['daily_return'] > 0) / buy_days * 100 if holding else 0

    # 交易胜率
    trade_wr = 0
    avg_profit = 0
    if trades:
        trade_wins = sum(1 for t in trades if t['win'])
        trade_wr = trade_wins / len(trades) * 100
        avg_profit = np.mean([t['profit_pct'] for t in trades])

    final_nv = bt_results[-1]['cum_return']
    max_dd = 0
    peak = 1.0
    for r in bt_results:
        if r['cum_return'] > peak:
            peak = r['cum_return']
        dd = (peak - r['cum_return']) / peak
        if dd > max_dd:
            max_dd = dd

    n_trades = len(trades) if trades else 0
    logger.info(
        f"LC 回测: 总{total}天, 持仓{buy_days}天({buy_days/total*100:.1f}%), "
        f"日胜率{day_wr:.1f}%, 交易{n_trades}笔, 交易胜率{trade_wr:.1f}%, "
        f"均盈{avg_profit:.2f}%, 净值{final_nv:.4f}, 回撤{-max_dd*100:.2f}%"
    )


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    print("LC v3 双层入场策略测试")
    df, bt, trades = compute_lc_signals_full('20200102')
    if bt and trades:
        trade_wins = sum(1 for t in trades if t['win'])
        trade_wr = trade_wins / len(trades) * 100
        print(f"\n交易: {len(trades)}笔, 交易胜率: {trade_wr:.1f}%")
        print(f"净值: {bt[-1]['cum_return']:.4f}")
        print(f"\n交易明细:")
        for t in trades:
            print(f"  {t['entry_date']}→{t['exit_date']} 持仓{t['hold_days']}天 "
                  f"盈亏{t['profit_pct']:+.2f}% {'WIN' if t['win'] else 'LOSS'} "
                  f"原因:{t['exit_reason']} WT={t['entry_wt']} RSI={t['entry_rsi']} RSI9={t['entry_rsi9']}")
