# -*- coding: utf-8 -*-
"""
形态学监控 - 核心计算引擎（批量优化版）
基于同花顺一级行业指数的形态学因子计算与策略信号打标

因子体系：
1. 赚钱效应因子 (ME) - 批量计算全市场排名后按行业聚合
2. 温度计因子 (TG) - 基于行业指数乖离率
3. 个股势能因子 (Energy) - 批量拉取个股数据后按行业聚合
4. 行业动量因子 (Sharpe) - 基于行业指数夏普比率

策略信号：
1. 赚钱效应优选 (右侧突破)
2. 牛回头 (强势板块调整买点)
3. 弱势行业反弹与反转
4. 极端底部/黄金坑

K线状态：
1. 底部企稳 - 金针探底
2. 底部突破 - 放量突围
3. 趋势主升 - 红K堆叠
4. 趋势加速 - 高举高打
"""
import os
import sqlite3
import json
import time
import numpy as np
from collections import defaultdict
from datetime import datetime, timedelta

# 使用量化数据平台的数据库
DB_DIR = '/Volumes/BEANPAPER/data/databases'


def get_industry_conn():
    return sqlite3.connect(os.path.join(DB_DIR, 'industry.db'))


def get_stock_conn():
    return sqlite3.connect(os.path.join(DB_DIR, 'stock_daily.db'))


def get_industry_list():
    """获取68个同花顺一级行业列表"""
    conn = get_industry_conn()
    rows = conn.execute(
        "SELECT ts_code, name, count FROM ths_index WHERE ts_code LIKE '8811%' ORDER BY ts_code"
    ).fetchall()
    conn.close()
    return [{'ts_code': r[0], 'name': r[1], 'count': r[2]} for r in rows]


def get_industry_daily(ts_code, days=300):
    """获取行业指数日线数据"""
    conn = get_industry_conn()
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close, vol, amount, pct_chg "
        "FROM ths_daily WHERE ts_code = ? ORDER BY trade_date DESC LIMIT ?",
        (ts_code, days)
    ).fetchall()
    conn.close()
    if not rows:
        return None
    data = list(reversed(rows))
    return {
        'dates': [r[0] for r in data], 'open': [r[1] for r in data],
        'high': [r[2] for r in data], 'low': [r[3] for r in data],
        'close': [r[4] for r in data], 'vol': [r[5] for r in data],
        'amount': [r[6] for r in data], 'pct_chg': [r[7] for r in data]
    }


def sma(data, window):
    """简单移动平均"""
    if len(data) < window:
        return [None] * len(data)
    result = []
    for i in range(len(data)):
        if i < window - 1:
            result.append(None)
        else:
            result.append(np.mean(data[i - window + 1:i + 1]))
    return result


# ==================== 温度计因子 ====================

def calc_temperature_gauge(daily_data):
    """
    计算温度计因子
    TG_High: Bias_60 在过去250日的百分位
    TG_Low:  Bias_120 在过去250日的百分位
    TG_Ultra: Bias_250 在过去250日的百分位
    TG_Comp: 三者等权平均
    """
    closes = daily_data['close']
    n = len(closes)
    if n < 60:
        return None

    ma60 = sma(closes, 60)
    ma120 = sma(closes, 120)
    ma250 = sma(closes, 250)

    bias60, bias120, bias250 = [], [], []
    for i in range(n):
        if ma60[i] and ma60[i] != 0:
            bias60.append((closes[i] - ma60[i]) / ma60[i])
        else:
            bias60.append(None)
        if ma120[i] and ma120[i] != 0:
            bias120.append((closes[i] - ma120[i]) / ma120[i])
        else:
            bias120.append(None)
        if ma250[i] and ma250[i] != 0:
            bias250.append((closes[i] - ma250[i]) / ma250[i])
        else:
            bias250.append(None)

    def percentile_rank(series, idx, window=250):
        start = max(0, idx - window + 1)
        vals = [v for v in series[start:idx + 1] if v is not None]
        if not vals or series[idx] is None:
            return None
        current = series[idx]
        rank = sum(1 for v in vals if v <= current) / len(vals) * 100
        return round(rank, 1)

    idx = n - 1
    tg_high = percentile_rank(bias60, idx, 250)
    tg_low = percentile_rank(bias120, idx, 250)
    tg_ultra = percentile_rank(bias250, idx, 250)

    vals = [v for v in [tg_high, tg_low, tg_ultra] if v is not None]
    tg_comp = round(np.mean(vals), 1) if vals else None

    # 昨日温度计（用于判断拐头）
    idx_prev = n - 2
    tg_high_prev = percentile_rank(bias60, idx_prev, 250) if n > 2 else None
    tg_low_prev = percentile_rank(bias120, idx_prev, 250) if n > 2 else None
    tg_ultra_prev = percentile_rank(bias250, idx_prev, 250) if n > 2 else None

    return {
        'tg_high': tg_high, 'tg_low': tg_low, 'tg_ultra': tg_ultra, 'tg_comp': tg_comp,
        'tg_high_prev': tg_high_prev, 'tg_low_prev': tg_low_prev, 'tg_ultra_prev': tg_ultra_prev,
    }


# ==================== 行业动量因子 ====================

def calc_sharpe(daily_data, window=120):
    """计算行业指数过去120日夏普比率"""
    pct_chgs = [r for r in daily_data['pct_chg'] if r is not None]
    if len(pct_chgs) < window:
        return None
    recent = pct_chgs[-window:]
    mean_ret = np.mean(recent)
    std_ret = np.std(recent)
    if std_ret == 0:
        return 0
    sharpe = (mean_ret / std_ret) * np.sqrt(250)
    return round(sharpe, 3)


# ==================== 赚钱效应因子（批量版） ====================

def batch_calc_money_effect(trade_dates):
    """
    批量计算所有行业的赚钱效应因子
    一次查询全市场排名，然后按行业聚合，避免 N*68 次 JOIN
    """
    conn_ind = get_industry_conn()
    conn_stock = get_stock_conn()

    all_members = conn_ind.execute("SELECT ts_code, con_code FROM ths_member").fetchall()
    industry_members = defaultdict(set)
    for ind_code, stock_code in all_members:
        if ind_code.startswith('8811'):
            industry_members[ind_code].add(stock_code)

    result = {}

    me_20_history = defaultdict(list)

    for offset in range(min(10, len(trade_dates) - 20)):
        td = trade_dates[offset]
        td_20_ago = trade_dates[offset + 19] if offset + 19 < len(trade_dates) else None
        if not td_20_ago:
            continue

        all_stocks = conn_stock.execute(
            "SELECT a.ts_code, (a.close - b.close) / b.close as ret_20 "
            "FROM daily a JOIN daily b ON a.ts_code = b.ts_code "
            "WHERE a.trade_date = ? AND b.trade_date = ? AND b.close > 0",
            (td, td_20_ago)
        ).fetchall()

        if not all_stocks:
            continue

        all_rets = sorted([r[1] for r in all_stocks if r[1] is not None])
        if not all_rets:
            continue
        threshold_10pct = all_rets[int(len(all_rets) * 0.9)]

        stock_ret = {r[0]: r[1] for r in all_stocks if r[1] is not None}

        for ind_code, members in industry_members.items():
            total = sum(1 for m in members if m in stock_ret)
            top = sum(1 for m in members if m in stock_ret and stock_ret[m] >= threshold_10pct)
            me_20 = round(top / total * 100, 1) if total > 0 else 0
            me_20_history[ind_code].append((td, me_20))

    for ind_code, history in me_20_history.items():
        if not history:
            continue
        history.sort(key=lambda x: x[0], reverse=True)
        me_20_today = history[0][1] if len(history) > 0 else None
        me_20_yesterday = history[1][1] if len(history) > 1 else None

        me_20_values = [h[1] for h in history[:10]]
        me_20_ma10 = round(np.mean(me_20_values), 1) if me_20_values else None

        me_20_ma10_yesterday = None
        if len(me_20_values) >= 2:
            me_20_ma10_yesterday = round(np.mean(me_20_values[1:min(11, len(me_20_values))]), 1)

        result[ind_code] = {
            'me_20': me_20_today,
            'me_20_yesterday': me_20_yesterday,
            'me_20_ma10': me_20_ma10,
            'me_20_ma10_yesterday': me_20_ma10_yesterday,
        }

    td = trade_dates[0]
    td_5_ago = trade_dates[4] if len(trade_dates) > 4 else None
    if td_5_ago:
        all_stocks_5 = conn_stock.execute(
            "SELECT a.ts_code, (a.close - b.close) / b.close as ret_5 "
            "FROM daily a JOIN daily b ON a.ts_code = b.ts_code "
            "WHERE a.trade_date = ? AND b.trade_date = ? AND b.close > 0",
            (td, td_5_ago)
        ).fetchall()
        if all_stocks_5:
            all_rets_5 = sorted([r[1] for r in all_stocks_5 if r[1] is not None])
            threshold_10pct_5 = all_rets_5[int(len(all_rets_5) * 0.9)] if all_rets_5 else None
            stock_ret_5 = {r[0]: r[1] for r in all_stocks_5 if r[1] is not None}

            for ind_code, members in industry_members.items():
                total = sum(1 for m in members if m in stock_ret_5)
                top = sum(1 for m in members if m in stock_ret_5 and stock_ret_5[m] >= threshold_10pct_5)
                me_5 = round(top / total * 100, 1) if total > 0 else 0
                if ind_code in result:
                    result[ind_code]['me_5'] = me_5
                else:
                    result[ind_code] = {'me_5': me_5, 'me_20': None, 'me_20_yesterday': None,
                                        'me_20_ma10': None, 'me_20_ma10_yesterday': None}

    conn_ind.close()
    conn_stock.close()
    return result


# ==================== 个股势能因子（批量版） ====================

def batch_calc_energy(trade_date, prev_date):
    """批量计算所有行业的结构改善比例"""
    conn_ind = get_industry_conn()
    conn_stock = get_stock_conn()

    all_members = conn_ind.execute("SELECT ts_code, con_code FROM ths_member").fetchall()
    industry_members = defaultdict(set)
    for ind_code, stock_code in all_members:
        if ind_code.startswith('8811'):
            industry_members[ind_code].add(stock_code)

    today_data = conn_stock.execute(
        "SELECT ts_code, close FROM daily WHERE trade_date = ?", (trade_date,)
    ).fetchall()
    prev_data = conn_stock.execute(
        "SELECT ts_code, close FROM daily WHERE trade_date = ?", (prev_date,)
    ).fetchall()

    today_dict = {r[0]: r[1] for r in today_data if r[1] is not None}
    prev_dict = {r[0]: r[1] for r in prev_data if r[1] is not None}

    result = {}
    for ind_code, members in industry_members.items():
        total = len(members)
        strength_count = 0
        improve_count = 0

        for code in members:
            if code not in today_dict or code not in prev_dict:
                continue
            close_t = today_dict[code]
            close_t1 = prev_dict[code]
            if close_t1 == 0:
                continue

            if close_t > close_t1:
                strength_count += 1
            if (close_t - close_t1) / close_t1 > 0:
                improve_count += 1

        ratio_strength = round(strength_count / total * 100, 1) if total > 0 else 0
        ratio_improve = round(improve_count / total * 100, 1) if total > 0 else 0
        structure_ratio = max(ratio_strength, ratio_improve)
        result[ind_code] = {
            'ratio_strength': ratio_strength,
            'ratio_improve': ratio_improve,
            'structure_ratio': structure_ratio,
        }

    conn_ind.close()
    conn_stock.close()
    return result


# ==================== K线状态识别 ====================

def identify_kline_state(daily_data):
    """识别4种K线状态（使用成交量vol，因ths_daily的amount全为None）"""
    states = []
    n = len(daily_data['close'])
    if n < 7:
        return states

    T = n - 1
    closes = daily_data['close']
    opens = daily_data['open']
    highs = daily_data['high']
    lows = daily_data['low']
    vols = daily_data['vol']
    pct_chgs = daily_data['pct_chg']

    # 状态1：底部企稳（金针探底）
    # 长下影线 + 振幅>2% + 位于近20日偏低位置(<60%)
    if opens[T] is not None and closes[T] is not None and highs[T] is not None and lows[T] is not None:
        k_length = highs[T] - lows[T]
        if k_length > 0 and closes[T - 1] > 0:
            shadow_lower = min(opens[T], closes[T]) - lows[T]
            shadow_ratio = shadow_lower / k_length
            amplitude = k_length / closes[T - 1]
            recent_20 = [c for c in closes[max(0, T - 19):T + 1] if c is not None]
            if recent_20:
                percentile_20 = sum(1 for c in recent_20 if c <= closes[T]) / len(recent_20) * 100
                if shadow_ratio > 0.5 and amplitude > 0.02 and percentile_20 < 60:
                    states.append('底部企稳')

    # 状态2：底部突破（放量突围）
    # 成交量>1.3x均量 + 涨>2% + 位于底部 + 突破近5日高点
    if T >= 5 and vols[T] is not None:
        vol_5_avg = np.mean([v for v in vols[T - 5:T] if v is not None])
        if vol_5_avg > 0 and vols[T] > 1.3 * vol_5_avg:
            if closes[T - 1] is not None:
                recent_20 = [c for c in closes[max(0, T - 20):T] if c is not None]
                if recent_20:
                    pct_prev = sum(1 for c in recent_20 if c <= closes[T - 1]) / len(recent_20) * 100
                    if pct_prev < 60 and pct_chgs[T] is not None and pct_chgs[T] > 2:
                        recent_5_high = max([c for c in closes[max(0, T - 4):T] if c is not None])
                        if closes[T] > recent_5_high:
                            states.append('底部突破')

    # 状态3：趋势主升（红K堆叠）
    # 7天内3根以上红K + 每根涨跌幅在-3%~8%之间(允许跳空大阳) + 7天涨>6% + 收盘>MA5 + 成交量>5日均量*0.8
    if T >= 6:
        window_7 = list(range(T - 6, T + 1))
        valid = all(closes[i] is not None and opens[i] is not None for i in window_7)
        if valid:
            red_count = sum(1 for i in window_7 if closes[i] > opens[i])
            daily_changes = [pct_chgs[i] for i in window_7 if pct_chgs[i] is not None]
            all_in_range = all(-3 <= dc <= 8 for dc in daily_changes) if len(daily_changes) == 7 else False
            total_gain = (closes[T] / closes[T - 7] - 1) * 100 if closes[T - 7] > 0 else 0
            ma5 = np.mean(closes[T - 4:T + 1])
            vol_ma5 = np.mean([v for v in vols[T - 4:T + 1] if v is not None]) if vols[T] else 0
            if (red_count >= 3 and all_in_range and total_gain > 6
                    and closes[T] > ma5 and vols[T] is not None and vols[T] > vol_ma5 * 0.8):
                states.append('趋势主升')

    # 状态4：趋势加速（高举高打）
    # 3天内2次涨>2% + 每天振幅>1.5% + 3天涨>6% + 创新高
    if T >= 2:
        window_3 = list(range(T - 2, T + 1))
        valid = all(pct_chgs[i] is not None for i in window_3)
        if valid:
            big_up_count = sum(1 for i in window_3 if pct_chgs[i] > 2)
            all_active = all(abs(pct_chgs[i]) > 1.5 for i in window_3)
            cum_return = (closes[T] / closes[T - 2] - 1) * 100 if closes[T - 2] > 0 else 0
            higher = closes[T] > max(closes[T - 1], closes[T - 2])
            if big_up_count >= 2 and all_active and cum_return > 6 and higher:
                states.append('趋势加速')

    return states


# ==================== 策略信号判定 ====================

def check_strategies(factors, all_sharpe_ranks):
    """根据因子值判定四大策略信号"""
    signals = []

    tg = factors.get('tg', {})
    sharpe = factors.get('sharpe')
    me = factors.get('me', {})
    energy = factors.get('energy', {})

    tg_comp = tg.get('tg_comp')
    tg_high = tg.get('tg_high')
    tg_low = tg.get('tg_low')
    tg_ultra = tg.get('tg_ultra')

    me_20_ma10 = me.get('me_20_ma10')
    me_20_ma10_yesterday = me.get('me_20_ma10_yesterday')
    me_5 = me.get('me_5')
    me_20 = me.get('me_20')
    me_20_yesterday = me.get('me_20_yesterday')

    structure_ratio = energy.get('structure_ratio')

    # Sharpe 排名百分位
    sharpe_rank = None
    if sharpe is not None and all_sharpe_ranks:
        sorted_sharpes = sorted(all_sharpe_ranks.values(), reverse=True)
        rank = sum(1 for s in sorted_sharpes if s >= sharpe)
        sharpe_rank = round(rank / len(sorted_sharpes) * 100, 1)

    daily_return = factors.get('daily_return', 0)

    # 策略1：赚钱效应优选
    me_rising = (me_20_ma10 is not None and me_20_ma10_yesterday is not None
                 and me_20_ma10 >= me_20_ma10_yesterday)
    if (me_rising and tg_comp is not None and tg_comp <= 70
            and sharpe_rank is not None and sharpe_rank <= 80
            and structure_ratio is not None and structure_ratio > 30):
        signals.append('赚钱效应优选')

    # 策略2：牛回头
    if (sharpe_rank is not None and sharpe_rank <= 10
            and me_20_ma10 is not None and me_20_ma10 > 20
            and tg_ultra is not None and tg_ultra > 50
            and me_20_ma10 <= 25
            and me_5 is not None and me_5 < 20
            and daily_return >= -2):
        signals.append('牛回头')

    # 策略3A：弱势反转
    if (sharpe_rank is not None and sharpe_rank >= 80
            and ((tg_high is not None and tg_high < 40) or (tg_low is not None and tg_low < 40))):
        if (me_20_ma10 is not None and me_20_ma10 > 5
                and me_20_yesterday is not None and me_20_ma10 > me_20_yesterday
                and structure_ratio is not None and structure_ratio > 40):
            signals.append('弱势反转')
        # 策略3B：弱势反弹
        if (me_20_ma10 is not None and me_20_ma10 <= 5
                and me_20_yesterday is not None and me_20_ma10 > me_20_yesterday
                and structure_ratio is not None and structure_ratio > 60):
            signals.append('弱势反弹')

    # 策略4：黄金坑
    if (tg_low is not None and tg_low < 10
            and tg_ultra is not None and tg_ultra < 10):
        me_turning = (me_20_yesterday is not None and me_20_ma10 is not None
                      and me_20_ma10 > me_20_yesterday)
        if me_turning and structure_ratio is not None and structure_ratio > 40:
            signals.append('黄金坑')

    return signals


# ==================== 温度计标签 ====================

def get_tg_label(tg_comp):
    if tg_comp is None:
        return '无数据'
    if tg_comp >= 80:
        return '极热'
    if tg_comp >= 60:
        return '偏热'
    if tg_comp >= 40:
        return '中性'
    if tg_comp >= 20:
        return '偏冷'
    return '极冷'


# ==================== 全量扫描 ====================

def get_industry_daily_up_to(ts_code, target_date, days=300):
    """获取行业指数日线数据，截取到指定日期"""
    conn = get_industry_conn()
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close, vol, amount, pct_chg "
        "FROM ths_daily WHERE ts_code = ? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (ts_code, target_date, days)
    ).fetchall()
    conn.close()
    if not rows:
        return None
    data = list(reversed(rows))
    return {
        'dates': [r[0] for r in data], 'open': [r[1] for r in data],
        'high': [r[2] for r in data], 'low': [r[3] for r in data],
        'close': [r[4] for r in data], 'vol': [r[5] for r in data],
        'amount': [r[6] for r in data], 'pct_chg': [r[7] for r in data]
    }


def run_full_scan(progress_cb=None, target_date=None):
    """对68个一级行业进行全量打标扫描
    target_date: 指定日期(YYYYMMDD)，为None则取最新
    """
    t0 = time.time()

    industries = get_industry_list()
    if not industries:
        return None

    conn = get_industry_conn()

    if target_date:
        scan_date = target_date
    else:
        scan_date = conn.execute(
            "SELECT MAX(trade_date) FROM ths_daily WHERE ts_code LIKE '8811%'"
        ).fetchone()[0]

    # 该日期之前的交易日序列（用于赚钱效应计算）
    trade_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT trade_date FROM ths_daily WHERE ts_code LIKE '8811%' AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT 30", (scan_date,)
    ).fetchall()]

    prev_date = conn.execute(
        "SELECT DISTINCT trade_date FROM ths_daily WHERE ts_code LIKE '8811%' AND trade_date < ? "
        "ORDER BY trade_date DESC LIMIT 1", (scan_date,)
    ).fetchone()
    prev_date = prev_date[0] if prev_date else None
    conn.close()

    total_steps = 4

    # 第一轮：行业指数因子
    if progress_cb:
        progress_cb(1, total_steps, '行业指数因子计算...')
    sharpe_map = {}
    daily_map = {}
    tg_map = {}
    kline_map = {}

    for ind in industries:
        daily = get_industry_daily_up_to(ind['ts_code'], scan_date, 300)
        if not daily:
            continue
        daily_map[ind['ts_code']] = daily
        sharpe_map[ind['ts_code']] = calc_sharpe(daily)
        tg_map[ind['ts_code']] = calc_temperature_gauge(daily)
        kline_map[ind['ts_code']] = identify_kline_state(daily)

    t1 = time.time()

    # 第二轮：批量赚钱效应因子
    if progress_cb:
        progress_cb(2, total_steps, '批量赚钱效应因子...')
    me_map = batch_calc_money_effect(trade_dates)
    t2 = time.time()

    # 第三轮：批量个股势能因子
    if progress_cb:
        progress_cb(3, total_steps, '批量个股势能因子...')
    energy_map = {}
    if prev_date:
        energy_map = batch_calc_energy(scan_date, prev_date)
    t3 = time.time()

    # 第四轮：策略信号判定
    if progress_cb:
        progress_cb(4, total_steps, '策略信号判定...')
    results = []
    for ind in industries:
        ts_code = ind['ts_code']
        if ts_code not in daily_map:
            continue

        daily = daily_map[ts_code]
        daily_return = round(daily['pct_chg'][-1], 2) if daily['pct_chg'][-1] is not None else 0

        factors = {
            'tg': tg_map.get(ts_code, {}),
            'sharpe': sharpe_map.get(ts_code),
            'me': me_map.get(ts_code, {}),
            'energy': energy_map.get(ts_code, {}),
            'daily_return': daily_return,
        }

        signals = check_strategies(factors, sharpe_map)
        kline_states = kline_map.get(ts_code, [])

        tg_comp = factors['tg'].get('tg_comp') if factors['tg'] else None

        results.append({
            'ts_code': ts_code,
            'name': ind['name'],
            'count': ind['count'],
            'date': scan_date,
            'daily_return': daily_return,
            'close': daily['close'][-1],
            'pct_chg': daily['pct_chg'][-1],
            'tg_high': factors['tg'].get('tg_high') if factors['tg'] else None,
            'tg_low': factors['tg'].get('tg_low') if factors['tg'] else None,
            'tg_ultra': factors['tg'].get('tg_ultra') if factors['tg'] else None,
            'tg_comp': tg_comp,
            'tg_label': get_tg_label(tg_comp),
            'sharpe': factors['sharpe'],
            'me_5': factors['me'].get('me_5'),
            'me_20': factors['me'].get('me_20'),
            'me_20_ma10': factors['me'].get('me_20_ma10'),
            'structure_ratio': factors['energy'].get('structure_ratio'),
            'ratio_strength': factors['energy'].get('ratio_strength'),
            'ratio_improve': factors['energy'].get('ratio_improve'),
            'signals': signals,
            'signal_str': ' / '.join(signals) if signals else '-',
            'kline_states': kline_states,
            'kline_str': ' / '.join(kline_states) if kline_states else '-',
        })

    elapsed = time.time() - t0
    signal_count = sum(1 for r in results if r['signals'])
    kline_count = sum(1 for r in results if r['kline_states'])

    return {
        'date': scan_date,
        'total': len(results),
        'elapsed': round(elapsed, 1),
        'signal_count': signal_count,
        'kline_count': kline_count,
        'industries': results,
    }
