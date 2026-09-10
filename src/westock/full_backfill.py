# -*- coding: utf-8 -*-
"""
完整因子回填 + 回测 —— 补齐原平台的"因子历史 / 情绪值 / 回测量化结果"

产出（写入 cache/factors_new.db）:
  ews_daily      每日 × 板块 全字段因子（对应原 ews_daily 表）
  sector_daily   板块日线（由成分股等权聚合，替代已停更的 ths_daily）
  ews_backtest   Top10 等权回测净值（全区间）
  ews_backtest_rf RF 择时 + EW-SDM 组合回测净值
  limit_history  每日涨停/炸板/连板（由日线还原）
"""
import os
import sys
import sqlite3
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from westock_source import (get_daily_df, get_sector_members, CACHE_DIR,
                            INDUSTRY_DB)
import ews_engine

# 沙箱环境下 SQLite 写 20 万行以上必报 disk I/O error，
# 因子/回测统一用 pickle 存（零依赖、稳定）。
STORE = os.path.join(CACHE_DIR, 'factors.pkl')
LOG = []


def log(m):
    print(m, flush=True)
    LOG.append(str(m))


def save_store(d):
    import pickle
    with open(STORE, 'wb') as f:
        pickle.dump(d, f, protocol=4)


def load_store():
    import pickle
    if not os.path.exists(STORE):
        return {}
    try:
        with open(STORE, 'rb') as f:
            return pickle.load(f)
    except Exception:
        return {}


def _thr(ts_code, name=''):
    code = ts_code.split('.')[0]
    if 'ST' in str(name).upper():
        return 5.0
    if code.startswith(('300', '301', '688', '689')):
        return 20.0
    if code.startswith(('83', '87', '43', '92')):
        return 30.0
    return 10.0


# ==================== 1. 全历史涨停 / 连板 ====================
def build_limit_history(daily):
    """一次性还原全历史涨停、炸板、连板（向量化，避免逐日重复计算）"""
    d = daily.sort_values(['ts_code', 'trade_date']).copy()
    d['thr'] = d['ts_code'].map(_thr)
    d['limit_px'] = d['pre_close'] * (1 + d['thr'] / 100.0)
    d['touched'] = d['high'] >= d['limit_px'] * 0.995
    d['sealed'] = d['close'] >= d['limit_px'] * 0.995

    # 连板数：连续 sealed 的累计计数，未封板清零
    g = d.groupby('ts_code', sort=False)['sealed']
    blk = (g.transform(lambda s: (s != s.shift()).cumsum()))
    d['streak'] = d.groupby(['ts_code', blk]).cumcount() + 1
    d.loc[~d['sealed'], 'streak'] = 0

    out = d[d['touched']][
        ['ts_code', 'trade_date', 'pct_chg', 'amount', 'streak', 'sealed']
    ].copy()
    out['fd_amount'] = 0.0
    out['up_stat'] = ''
    out['open_times'] = 0
    out['is_broken'] = (~out['sealed']).astype(int)
    return out.drop(columns=['sealed'])


# ==================== 2. 板块日线（成分股等权聚合，替代 ths_daily）====================
def build_sector_daily(daily, member_map):
    """板块日收益 = 成分股当日 pct_chg 等权平均。
    注意：与同花顺官方板块指数编制口径不同（官方多为市值加权），
    仅用于回测近似，需在报告中标注。

    实现用"个股→所属板块"反向展开 + 一次 groupby，避免 板块×日期 双重循环。
    """
    stock2sec = {}
    for sec, members in member_map.items():
        for m in members:
            stock2sec.setdefault(m, []).append(sec)
    d = daily[['ts_code', 'trade_date', 'pct_chg']].copy()
    d['sector'] = d['ts_code'].map(stock2sec)
    d = d[d['sector'].notna()].explode('sector')
    g = d.groupby(['sector', 'trade_date'])['pct_chg'].agg(['mean', 'count'])
    g = g.reset_index()
    g.columns = ['ts_code', 'trade_date', 'pct_chg', 'n']
    return g


# ==================== 3. 逐日计算 EW-SDM 全字段因子 ====================
def compute_ews_history(daily, limit_hist, sector_list, member_map):
    dates = sorted(daily.trade_date.unique())
    lim_by_date = {d: g for d, g in limit_hist.groupby('trade_date')}
    px_by_date = {d: g[['ts_code', 'trade_date', 'close', 'pct_chg', 'amount']]
                  for d, g in daily.groupby('trade_date')}
    all_res = []
    for i, dt in enumerate(dates, 1):
        px = px_by_date.get(dt)
        if px is None or len(px) == 0:
            continue
        lm = lim_by_date.get(dt, pd.DataFrame(
            columns=['ts_code', 'trade_date', 'pct_chg', 'amount', 'streak',
                     'fd_amount', 'up_stat', 'open_times', 'is_broken']))
        try:
            r = ews_engine.compute_daily_factors(
                dt, sector_list, member_map, px, lm)
            all_res.extend(r)
        except Exception as e:
            log(f'  [warn] {dt} 计算失败: {type(e).__name__}: {e}')
        if i % 50 == 0:
            log(f'  因子进度 {i}/{len(dates)}，累计 {len(all_res)} 条')
    return pd.DataFrame(all_res)


# ==================== 4. 回测（Top10 等权 + 可选 RF 择时）====================
def backtest(factor_df, sector_daily, top_n=10, rf_map=None):
    """信号：每日收盘做多 Top N 板块；收益：次日板块日收益等权。
    rf_map: {trade_date: 0/1}，为 1 才持仓（RF 择时过滤）。"""
    if 'final_score' not in factor_df.columns:
        return pd.DataFrame()
    ret = {(r.ts_code, r.trade_date): r.pct_chg
           for r in sector_daily.itertuples()}
    dates = sorted(factor_df.trade_date.unique())
    nav, recs = 1.0, []
    for i in range(len(dates) - 1):
        d0, d1 = dates[i], dates[i + 1]
        day = factor_df[factor_df.trade_date == d0]
        if len(day) == 0:
            continue
        code_col = 'concept_code' if 'concept_code' in day.columns else 'ts_code'
        top = day.nlargest(top_n, 'final_score')[code_col].tolist()
        hold = 1
        if rf_map is not None:
            hold = rf_map.get(d0, 1)
        rs = [ret.get((c, d1)) for c in top]
        rs = [x for x in rs if x is not None and not pd.isna(x)]
        if not rs:
            continue
        r = float(np.mean(rs)) / 100.0 * hold
        nav *= (1 + r)
        recs.append({'trade_date': d1, 'ret': r * 100, 'nav': nav,
                     'hold': hold, 'n_sector': len(rs)})
    return pd.DataFrame(recs)


def perf_stats(bt, label=''):
    if len(bt) == 0:
        return {}
    nav = bt['nav'].values
    days = len(bt)
    total = nav[-1] / nav[0] - 1
    ann = (nav[-1] / nav[0]) ** (250.0 / max(days, 1)) - 1
    peak = np.maximum.accumulate(nav)
    dd = nav / peak - 1
    mdd = dd.min()
    win = (bt['ret'].values > 0).mean()
    sharpe = (bt['ret'].mean() / bt['ret'].std() * np.sqrt(250)
              if bt['ret'].std() > 0 else 0)
    return {'label': label, 'days': days, 'total_return': round(total * 100, 2),
            'annual_return': round(ann * 100, 2),
            'max_drawdown': round(mdd * 100, 2),
            'win_rate': round(win * 100, 2),
            'sharpe': round(float(sharpe), 2),
            'final_nav': round(float(nav[-1]), 4)}


# ==================== 主流程 ====================
def main():
    t0 = datetime.now()
    log('=' * 62)
    log(f'完整因子回填 + 回测  启动 {t0:%Y-%m-%d %H:%M:%S}')
    log('=' * 62)

    log('[1/6] 载入全市场日线 ...')
    daily = get_daily_df(n=600)
    if len(daily) == 0:
        log('!! 无行情数据')
        return
    log(f'      {daily.ts_code.nunique()} 只 × {daily.trade_date.nunique()} 天, '
        f'{daily.trade_date.min()} ~ {daily.trade_date.max()}')

    log('[2/6] 还原涨停 / 炸板 / 连板 ...')
    limit_hist = build_limit_history(daily)
    log(f'      {len(limit_hist)} 条涨停记录')

    log('[3/6] 构建板块日线（成分股等权聚合，替代停更的 ths_daily）...')
    conn_ind = sqlite3.connect(INDUSTRY_DB)
    sector_list = ews_engine.load_sector_list(conn_ind)
    members = ews_engine.load_sector_members(conn_ind)
    conn_ind.close()
    member_map = members.groupby('sector_code')['stock_code'].apply(list).to_dict()
    log(f'      板块 {len(sector_list)} 个，成分股关系 {len(members)} 条')
    sector_daily = build_sector_daily(daily, member_map)
    log(f'      板块日线 {len(sector_daily)} 条')

    log('[4/6] 逐日计算 EW-SDM 全字段因子 ...')
    store = load_store()
    FORCE = '--force' in sys.argv
    if (not FORCE and 'ews_daily' in store and 'sector_daily' in store
            and store['ews_daily'].trade_date.max() == daily.trade_date.max()):
        factor_df = store['ews_daily']
        log(f'      复用缓存因子 {len(factor_df)} 条（--force 强制重算）')
    else:
        factor_df = compute_ews_history(daily, limit_hist, sector_list,
                                        member_map)
    if len(factor_df) == 0:
        log('!! 因子计算为空')
        return
    log(f'      因子记录 {len(factor_df)} 条，字段: {list(factor_df.columns)}')

    # 因子先落盘，避免后续步骤失败导致白算（全流程约 14 分钟）
    # 用 pickle 而非 SQLite：沙箱下 to_sql 写 20 万+ 行必报 disk I/O error
    log('[5/6] 因子落盘（先存后算，防白跑）...')
    store = {'ews_daily': factor_df, 'sector_daily': sector_daily,
             'limit_history': limit_hist}
    save_store(store)
    log(f'      已写入 {STORE}')

    log('[5b/6] 回测 ...')
    bt = backtest(factor_df, sector_daily, top_n=10)
    st = perf_stats(bt, 'EW-SDM Top10 等权')

    log('[6/6] 回测结果落盘 ...')
    if len(bt):
        store['backtest'] = bt
    store['stats'] = st
    save_store(store)

    log('=' * 62)
    log(f'因子 {len(factor_df)} 条 | 回测 {len(bt)} 天')
    log(f'EW-SDM: {st}')
    log(f'完成，耗时 {(datetime.now()-t0).total_seconds():.0f}s -> {STORE}')

    with open(os.path.join(HERE, 'output', 'backfill_stats.json'), 'w',
              encoding='utf-8') as f:
        import json
        json.dump({'factor_rows': len(factor_df),
                   'sector_daily_rows': len(sector_daily),
                   'limit_rows': len(limit_hist),
                   'backtest_days': len(bt), 'stats': st},
                  f, ensure_ascii=False, indent=2, default=str)


def get_index_daily_safe(code, n):
    from westock_source import get_index_daily
    try:
        return get_index_daily(code, n=n, refresh=True)
    except Exception:
        return pd.DataFrame()


if __name__ == '__main__':
    os.makedirs(os.path.join(HERE, 'output'), exist_ok=True)
    main()
