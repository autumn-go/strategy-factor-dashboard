# -*- coding: utf-8 -*-
"""
策略最新信号计算 —— 数据源已切换为内置金融服务（腾讯自选股 / westock）

手法：monkey-patch 各引擎的「取数函数」，算法本体一行不动。
      (换管子，不换发动机)
"""
import os
import sys
import json
import sqlite3
import warnings
from datetime import datetime

import pandas as pd
import numpy as np

warnings.filterwarnings('ignore')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from westock_source import (get_daily_df, get_index_daily, get_sector_list,
                            get_sector_members, to_tx_code)

OUT_DIR = os.path.join(HERE, 'output')
os.makedirs(OUT_DIR, exist_ok=True)

LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(str(msg))


# ==================== 涨跌停推算（新数据源无连板表，从日线还原）====================
def _limit_threshold(ts_code, name=''):
    """按板块返回涨停幅度(%)"""
    code = ts_code.split('.')[0]
    if 'ST' in str(name).upper():
        return 5.0
    if code.startswith(('300', '301', '688', '689')):
        return 20.0
    if code.startswith(('83', '87', '43', '92')):      # 北交所
        return 30.0
    return 10.0


def build_limit_df(daily_all, target_date, stock_names=None):
    """从日线还原涨停/炸板/连板数据，替代原 Tushare limit_up/limit_broken 表。
    判定: high 触及涨停价 -> 曾涨停; close 收在涨停价 -> 封板; 否则 -> 炸板
    """
    d = daily_all[daily_all.trade_date == target_date].copy()
    if len(d) == 0:
        return pd.DataFrame(columns=['ts_code', 'trade_date', 'pct_chg',
                                     'amount', 'fd_amount', 'streak',
                                     'up_stat', 'open_times', 'is_broken'])
    d = d.dropna(subset=['pre_close', 'close', 'high'])
    d = d[(d.pre_close > 0) & (d.close > 0)]
    thr = d.ts_code.map(lambda c: _limit_threshold(c))
    limit_px = d.pre_close * (1 + thr / 100.0)

    touched = d.high >= limit_px * 0.995      # 盘中触及涨停
    sealed = d.close >= limit_px * 0.995      # 收盘封住

    # 连板数：向前数连续封板天数
    hist = daily_all[daily_all.trade_date <= target_date].copy()
    hist = hist.sort_values(['ts_code', 'trade_date'])
    hist['thr'] = hist.ts_code.map(lambda c: _limit_threshold(c))
    hist['lp'] = hist.pre_close * (1 + hist.thr / 100.0)
    hist['seal'] = (hist.close >= hist.lp * 0.995).astype(int)
    # 连续封板计数（遇未封板清零）
    grp = hist.groupby('ts_code')['seal']
    streak = grp.transform(
        lambda s: s * (s.groupby((s != s.shift()).cumsum()).cumcount() + 1))
    hist = hist.assign(streak=streak)
    last = hist[hist.trade_date == target_date].set_index('ts_code')['streak']

    out = pd.DataFrame({
        'ts_code': d.ts_code.values,
        'trade_date': target_date,
        'pct_chg': d.pct_chg.values,
        'amount': d.get('amount', pd.Series(0, index=d.index)).values,
        'fd_amount': 0.0,
        'streak': d.ts_code.map(last).fillna(0).astype(int).values,
        'up_stat': '',
        'open_times': 0,
        'is_broken': (touched & ~sealed).astype(int).values,
    })
    out = out[touched.values]   # 只保留当日触及涨停的（涨停 or 炸板）
    return out.reset_index(drop=True)


# ==================== 策略 1: EW-SDM ====================
def run_ews(daily_all, target_date):
    import ews_engine
    conn_ind = sqlite3.connect(ews_engine.INDUSTRY_DB)
    sector_list = ews_engine.load_sector_list(conn_ind)
    members = ews_engine.load_sector_members(conn_ind)
    conn_ind.close()
    member_map = members.groupby('sector_code')['stock_code'].apply(list).to_dict()

    daily_df = daily_all[daily_all.trade_date == target_date][
        ['ts_code', 'trade_date', 'close', 'pct_chg', 'amount']].copy()
    limit_df = build_limit_df(daily_all, target_date)

    if len(daily_df) == 0:
        return {'error': f'{target_date} 无行情数据'}

    res = ews_engine.compute_daily_factors(
        target_date, sector_list, member_map, daily_df, limit_df)
    df = pd.DataFrame(res)
    if len(df) == 0:
        return {'error': '因子计算结果为空'}

    # EW-SDM 引擎输出字段: concept_code / concept_name / final_score ...
    score_col = None
    for c in ['final_score', 'ewsdm', 'ews_sdm', 'score']:
        if c in df.columns:
            score_col = c
            break
    if score_col is None:
        num = [c for c in df.columns if df[c].dtype.kind in 'fi']
        score_col = num[0] if num else df.columns[-1]

    if 'name' not in df.columns and 'concept_name' in df.columns:
        df['name'] = df['concept_name']
    if 'ts_code' not in df.columns and 'concept_code' in df.columns:
        df['ts_code'] = df['concept_code']
    if 'type' not in df.columns and 'sector_type' in df.columns:
        df['type'] = df['sector_type']
    if 'stock_count' not in df.columns and 'total_stocks' in df.columns:
        df['stock_count'] = df['total_stocks']

    df = df.sort_values(score_col, ascending=False)
    top = df.head(15)
    # 保留全部因子字段（对应原平台 ews_daily 的因子面板）
    cols = [c for c in ['ts_code', 'name', 'type', 'stock_count',
                        'momentum', 'emotion_diff', 's_score', score_col,
                        'up_count', 'limit_up_count', 'broken_count',
                        'max_streak'] if c in top.columns]
    return {
        'trade_date': target_date,
        'score_col': score_col,
        'total_sectors': len(df),
        'limit_up_count': int((limit_df.is_broken == 0).sum()) if len(limit_df) else 0,
        'limit_broken_count': int((limit_df.is_broken == 1).sum()) if len(limit_df) else 0,
        'top': top[cols].to_dict('records'),
    }


# ==================== 策略 2: LC-KNN（原中证2000 -> 国证2000）====================
def run_lc():
    import lc_engine
    df = get_index_daily('932000.CSI', n=1600, refresh=True)   # 自动回落国证2000
    if len(df) == 0:
        return {'error': '小盘指数数据为空'}
    src = df[['trade_date', 'open', 'high', 'low', 'close', 'vol',
              'amount', 'pct_chg']].copy()
    lc_engine.load_index_daily = lambda start_date='20200102', end_date=None: (
        src[src.trade_date >= start_date] if end_date is None
        else src[(src.trade_date >= start_date) & (src.trade_date <= end_date)])
    try:
        out = lc_engine.compute_lc_signals_full(start_date='20230101')
    except TypeError:
        out = lc_engine.compute_lc_signals_full(start_date='20230101',
                                                end_date=None, params=None)
    if isinstance(out, tuple):
        sig_df = out[0]
        bt = out[1] if len(out) > 1 else None
    else:
        sig_df, bt = out, None
    if sig_df is None or len(sig_df) == 0:
        return {'error': 'LC 计算结果为空'}
    last = sig_df.iloc[-1]
    d = {k: (float(last[k]) if isinstance(last[k], (int, float, np.floating))
             else str(last[k]))
         for k in sig_df.columns}
    d['note'] = '标的由中证2000回落为国证2000（腾讯源无中证2000日线）'
    return d


# ==================== 主流程 ====================
def main():
    t0 = datetime.now()
    log('=' * 60)
    log(f'策略最新信号计算  启动 {t0:%Y-%m-%d %H:%M:%S}')
    log('数据源: 内置金融服务（腾讯自选股 / westock）')
    log('=' * 60)

    log('[1/3] 载入全市场日线 ...')
    daily_all = get_daily_df(n=300)
    if len(daily_all) == 0:
        log('!! 无行情数据，请先运行 refresh_universe')
        return
    target = str(daily_all.trade_date.max())
    ndays = daily_all.trade_date.nunique()
    log(f'      个股 {daily_all.ts_code.nunique()} 只, '
        f'{ndays} 个交易日, 最新 {target}')

    results = {'gen_time': t0.strftime('%Y-%m-%d %H:%M:%S'),
               'data_source': '腾讯自选股(westock同源) + 本地静态映射',
               'trade_date': target}

    log('[2/3] EW-SDM 情绪加权扩散动量 ...')
    try:
        results['ews'] = run_ews(daily_all, target)
        log(f"      板块 {results['ews'].get('total_sectors')} 个, "
            f"涨停 {results['ews'].get('limit_up_count')} 家")
    except Exception as e:
        results['ews'] = {'error': f'{type(e).__name__}: {e}'}
        log(f'      !! EW-SDM 失败: {e}')

    log('[3/3] LC-KNN 洛伦兹分类（小盘指数）...')
    try:
        results['lc'] = run_lc()
        log(f"      {results['lc'].get('trade_date', '')} "
            f"signal={results['lc'].get('signal', results['lc'].get('prediction',''))}")
    except Exception as e:
        results['lc'] = {'error': f'{type(e).__name__}: {e}'}
        log(f'      !! LC 失败: {e}')

    fp = os.path.join(OUT_DIR, 'latest_signals.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    log('=' * 60)
    log(f'完成，耗时 {(datetime.now()-t0).total_seconds():.0f}s -> {fp}')
    return results


if __name__ == '__main__':
    main()
