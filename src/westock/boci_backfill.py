# -*- coding: utf-8 -*-
"""
BOCI 一级行业情绪指标（五维）—— 补齐"情绪值"

方法来源: 中银国际《A股情绪指标体系之二：BOCI一级行业情绪指标》
五维子指标等权合成综合情绪:
  F1 MA20 多头占比   —— 成分股收盘站上 20 日均线的比例
  F2 RSI 归一化      —— 行业等权指数的 RSI(14)
  F3 换手率强度      —— 成分股平均换手率
  F4 涨跌停情绪差    —— (涨停数 - 跌停数) / 成分股数
  F5 成交额占比      —— 行业成交额 / 全市场成交额
截面打分: S1 情绪斜率(35%) + S2 加速度(15%) + S3 相对水平(30%)
        + S4 价格动量(10%) + S5 价格斜率(10%)
风控: 综合情绪 > 85 分位视为过热，一票否决
输出: 最新交易日各行业 F1~F5、综合情绪、综合打分 Top 榜
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

from westock_source import get_daily_df, CACHE_DIR, INDUSTRY_DB, STOCK_DB

STORE = os.path.join(CACHE_DIR, 'factors.pkl')
OVERHEAT = 0.85          # 过热阈值（综合情绪分位）
MA_PERIOD = 20
RSI_PERIOD = 14


def log(m):
    print(m, flush=True)


def _thr(code, name=''):
    if 'ST' in str(name).upper():
        return 5.0
    if code.startswith(('300', '301', '688', '689')):
        return 20.0
    if code.startswith(('83', '87', '43', '92')):
        return 30.0
    return 10.0


def load_universe_names():
    c = sqlite3.connect(STOCK_DB)
    d = dict(c.execute('SELECT ts_code, name FROM stock_list'))
    c.close()
    return d


def load_industries():
    """同花顺一级行业 881xxx 及其成分股"""
    c = sqlite3.connect(INDUSTRY_DB)
    ind = pd.read_sql("SELECT ts_code, name FROM ths_index "
                      "WHERE ts_code LIKE '881%'", c)
    mem = pd.read_sql("SELECT ts_code as sector, con_code as stock "
                      "FROM ths_member WHERE ts_code LIKE '881%'", c)
    c.close()
    return ind, mem


def expanding_minmax(s):
    """时序 expanding min-max 归一化到 0~1（与原 BOCI 口径一致）"""
    lo = s.expanding(min_periods=1).min()
    hi = s.expanding(min_periods=1).max()
    den = (hi - lo).replace(0, np.nan)
    return ((s - lo) / den).fillna(0.5).clip(0, 1)


def rsi(close, period=14):
    delta = close.diff()
    up = delta.clip(lower=0)
    dn = (-delta).clip(lower=0)
    # Wilder 平滑
    ru = up.ewm(alpha=1 / period, adjust=False).mean()
    rd = dn.ewm(alpha=1 / period, adjust=False).mean()
    rs = ru / rd.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50)


def build():
    t0 = datetime.now()
    log('=' * 60)
    log(f'BOCI 五维行业情绪  启动 {t0:%H:%M:%S}')

    log('[1/5] 载入行情 ...')
    daily = get_daily_df(n=600)
    if len(daily) == 0:
        log('!! 无行情')
        return
    log(f'      {daily.ts_code.nunique()} 只 × {daily.trade_date.nunique()} 天')

    log('[2/5] 个股层面：MA20 / 涨停 / 跌停 ...')
    names = load_universe_names()
    thr_map = {c: _thr(c.split('.')[0], names.get(c, ''))
               for c in daily.ts_code.unique()}
    daily['thr'] = daily.ts_code.map(thr_map)
    daily['up_px'] = daily.pre_close * (1 + daily.thr / 100.0)
    daily['dn_px'] = daily.pre_close * (1 - daily.thr / 100.0)
    daily['is_up'] = (daily.close >= daily.up_px * 0.995).astype(float)
    daily['is_dn'] = (daily.close <= daily.dn_px * 1.005).astype(float)
    daily['ma20'] = daily.groupby('ts_code').close.transform(
        lambda s: s.rolling(MA_PERIOD, min_periods=MA_PERIOD).mean())
    daily['above'] = (daily.close > daily.ma20).astype(float)

    log('[3/5] 聚合到行业日线 ...')
    ind, mem = load_industries()
    s2sec = {}
    for sec, stk in zip(mem.sector, mem.stock):
        s2sec.setdefault(stk, []).append(sec)
    d = daily[['ts_code', 'trade_date', 'close', 'pct_chg', 'amount',
               'turnover', 'is_up', 'is_dn', 'above']].copy()
    d['sector'] = d.ts_code.map(s2sec)
    d = d[d.sector.notna()].explode('sector')
    g = d.groupby(['sector', 'trade_date']).agg(
        close=('close', 'mean'), pct_chg=('pct_chg', 'mean'),
        amount=('amount', 'sum'), turnover=('turnover', 'mean'),
        up_cnt=('is_up', 'sum'), dn_cnt=('is_dn', 'sum'),
        above=('above', 'mean'), n=('close', 'size')).reset_index()

    # 全市场成交额（用于 F5 分母）
    mkt = daily.groupby('trade_date').amount.sum().rename('mkt_amount')
    g = g.merge(mkt, on='trade_date', how='left')
    log(f'      行业日线 {len(g)} 条，{g.sector.nunique()} 个行业')

    log('[4/5] 计算五维情绪 ...')
    g = g.sort_values(['sector', 'trade_date']).reset_index(drop=True)
    # F1 已在 above 列（比例）
    g['f1'] = g.groupby('sector').above.transform(expanding_minmax)
    # F2 RSI
    g['rsi'] = g.groupby('sector').close.transform(
        lambda s: rsi(s, RSI_PERIOD))
    g['f2'] = g.groupby('sector').rsi.transform(expanding_minmax)
    # F3 换手率强度
    g['f3'] = g.groupby('sector').turnover.transform(expanding_minmax)
    # F4 涨跌停情绪差
    g['limit_diff'] = (g.up_cnt - g.dn_cnt) / g.n.clip(lower=1)
    g['f4'] = g.groupby('sector').limit_diff.transform(expanding_minmax)
    # F5 成交额占比
    g['amt_ratio'] = g.amount / g.mkt_amount.clip(lower=1)
    g['f5'] = g.groupby('sector').amt_ratio.transform(expanding_minmax)
    g['sentiment'] = g[['f1', 'f2', 'f3', 'f4', 'f5']].mean(axis=1)

    log('[5/5] 截面打分 ...')
    g['s1'] = g.groupby('sector').sentiment.diff(5)          # 情绪斜率
    g['s2'] = g.groupby('sector').s1.diff(5)                 # 加速度
    g['s3'] = g.sentiment                                    # 相对水平
    g['mom20'] = g.groupby('sector').close.pct_change(20)    # 价格动量
    g['s4'] = g.mom20
    g['s5'] = g.groupby('sector').close.diff(5) / g.groupby(
        'sector').close.shift(5)                             # 价格斜率
    # 当日截面分位（0~1）
    for c in ['s1', 's2', 's3', 's4', 's5']:
        g[c + '_r'] = g.groupby('trade_date')[c].rank(pct=True)
    g['score'] = (g.s1_r * 0.35 + g.s2_r * 0.15 + g.s3_r * 0.30
                  + g.s4_r * 0.10 + g.s5_r * 0.10)
    # 过热过滤：综合情绪截面分位 > 85%
    g['sent_rank'] = g.groupby('trade_date').sentiment.rank(pct=True)
    g['overheat'] = g.sent_rank > OVERHEAT

    # ---- 最新交易日结果 ----
    last = g.trade_date.max()
    cur = g[g.trade_date == last].copy()
    name_map = dict(zip(ind.ts_code, ind.name))
    cur['name'] = cur.sector.map(name_map)
    cur = cur.sort_values('score', ascending=False)
    valid = cur[~cur.overheat]

    top = cur.head(15)[['sector', 'name', 'f1', 'f2', 'f3', 'f4', 'f5',
                        'sentiment', 'score', 'overheat', 'n']]
    top3 = valid.head(3)[['sector', 'name', 'sentiment', 'score']]

    log('=' * 60)
    log(f'交易日 {last} | 行业 {len(cur)} 个 | 过热剔除 {int(cur.overheat.sum())} 个')
    log('情绪 Top5:')
    for _, r in cur.head(5).iterrows():
        log(f"   {r['name']:12s} 情绪 {r.sentiment:.3f}  打分 {r.score:.3f}"
            f"  {'[过热]' if r.overheat else ''}")
    log('Top3 可交易（已过滤过热）: ' +
        '、'.join(f"{r['name']}({r.score:.2f})" for _, r in top3.iterrows()))

    # 入库（合并进 factors.pkl）
    import pickle
    store = {}
    if os.path.exists(STORE):
        try:
            with open(STORE, 'rb') as f:
                store = pickle.load(f)
        except Exception:
            store = {}
    store['boci_daily'] = g
    store['boci_latest'] = {
        'trade_date': str(last),
        'n_industry': int(len(cur)),
        'n_overheat': int(cur.overheat.sum()),
        'top': top.to_dict('records'),
        'top3': top3.to_dict('records'),
    }
    with open(STORE, 'wb') as f:
        pickle.dump(store, f, protocol=4)
    log(f'已写入 {STORE}，耗时 {(datetime.now()-t0).total_seconds():.0f}s')
    return store['boci_latest']


if __name__ == '__main__':
    build()
