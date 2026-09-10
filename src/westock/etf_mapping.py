# -*- coding: utf-8 -*-
"""
每日可买 ETF 清单 —— 把当日板块/行业信号映射成可交易的 ETF

做法:
  1. 取当日 EW-SDM Top 板块 + BOCI Top 行业
  2. 用板块名（去掉罗马数字、"概念"等后缀）走 westock 搜 ETF
  3. 只保留 A 股场内 ETF（sh/sz 开头），过滤 LOF / QDII / 港股
  4. 批量拉这些 ETF 的日线，取最新成交额做流动性排序
输出: output/etf_watchlist.json + 写回 factors.pkl
"""
import os
import re
import sys
import json
import pickle
import warnings
from datetime import datetime

import pandas as pd

warnings.filterwarnings('ignore')

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)

from westock_source import (CACHE_DIR, cli_kline_batch, westock_cli, find_node)

STORE = os.path.join(CACHE_DIR, 'factors.pkl')
OUT_DIR = os.path.join(HERE, 'output')
MIN_AMOUNT = 1e7          # 成交额下限 1000 万，过滤流动性过差的


def log(m):
    print(m, flush=True)


def clean_name(nm):
    """板块名 -> 搜索关键词：去掉罗马数字即可，'概念'等保留更有用"""
    s = re.sub(r'[ⅠⅡⅢⅣⅤ]+', '', str(nm))
    return s.strip()


def search_etf(kw):
    """用 westock 搜 ETF，返回 [(code, name, type)]"""
    txt = westock_cli(['search', kw, '--type', 'etf'])
    rows = []
    for line in (txt or '').splitlines():
        line = line.strip()
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) < 3 or cells[0] in ('code', '代码'):
            continue
        if set(cells[0]) <= set('-: '):
            continue
        if not re.match(r'^(sh|sz|hk)\d+', cells[0]):
            continue
        rows.append((cells[0], cells[1], cells[2]))
    return rows


def pick_etfs(sector_name):
    """逐级降级搜索：全名 -> 前4 -> 前3 -> 前2，取第一个有 A 股 ETF 的结果"""
    kw = clean_name(sector_name)
    tried = []
    candidates = [kw]
    for n in (4, 3, 2):
        if n < len(kw):
            candidates.append(kw[:n])
    for k in candidates:
        if not k or k in tried:
            continue
        tried.append(k)
        res = search_etf(k)
        a = [r for r in res if r[0].startswith(('sh', 'sz'))
             and r[2].upper() == 'ETF']
        if a:
            return k, a
    return kw, []


def attach_liquidity(etfs, timeout=120):
    """批量拉 ETF 日线，补最新价与成交额"""
    codes = sorted({c for _, c, _ in etfs})
    if not codes:
        return {}
    info = {}
    for i in range(0, len(codes), 50):
        batch = codes[i:i + 50]
        m, err = cli_kline_batch(batch, limit=5, timeout=timeout)
        for c, kl in m.items():
            if not kl:
                continue
            last = kl[0]        # CLI 返回倒序，第一条是最新
            # [date, open, close, high, low, vol, amount, turnover]
            info[c] = {'date': last[0], 'close': float(last[2]),
                       'amount': float(last[6]) if len(last) > 6 else 0.0}
        if err:
            log(f'  [warn] ETF 行情拉取异常: {str(err)[:80]}')
    return info


def build(top_sectors=None, top_industries=None, limit_sector=10):
    t0 = datetime.now()
    log('=' * 60)
    log(f'每日可买 ETF 清单  {t0:%H:%M:%S}')

    # ---- 信号来源 ----
    store = {}
    if os.path.exists(STORE):
        try:
            with open(STORE, 'rb') as f:
                store = pickle.load(f)
        except Exception:
            store = {}
    sectors = top_sectors or []
    if not sectors:
        p = os.path.join(OUT_DIR, 'latest_signals.json')
        if os.path.exists(p):
            with open(p, encoding='utf-8') as f:
                sig = json.load(f)
            sectors = [r.get('name') for r in
                       (sig.get('ews', {}).get('top') or [])][:limit_sector]
    inds = top_industries or []
    if not inds:
        b = store.get('boci_latest') or {}
        inds = [r.get('name') for r in (b.get('top3') or [])]
    targets = [('EW-SDM', s) for s in sectors if s] + \
              [('BOCI', s) for s in inds if s]
    log(f'信号标的 {len(targets)} 个：' +
        '、'.join(f'{s}' for _, s in targets[:12]))

    # ---- 搜索映射 ----
    mapping, all_etf = [], []
    for src, nm in targets:
        kw, etfs = pick_etfs(nm)
        if etfs:
            for code, ename, _ in etfs:
                all_etf.append((nm, code, ename))
            mapping.append({'source': src, 'sector': nm, 'keyword': kw,
                            'n': len(etfs)})
            log(f'  {nm} -> 关键词"{kw}" 命中 {len(etfs)} 只')
        else:
            mapping.append({'source': src, 'sector': nm, 'keyword': kw,
                            'n': 0})
            log(f'  {nm} -> 无匹配 ETF')

    # ---- 流动性 ----
    log('拉取 ETF 行情（成交额）...')
    liq = attach_liquidity(all_etf)

    rows = []
    for nm, code, ename in all_etf:
        info = liq.get(code, {})
        rows.append({'sector': nm, 'code': code, 'fund_name': ename,
                     'close': info.get('close'), 'amount': info.get('amount'),
                     'date': info.get('date')})
    df = pd.DataFrame(rows)
    if len(df):
        df['amount'] = pd.to_numeric(df.amount, errors='coerce').fillna(0)
        df = df[df.amount >= MIN_AMOUNT]
        df = df.sort_values(['sector', 'amount'], ascending=[True, False])

    out = {'gen_time': t0.strftime('%Y-%m-%d %H:%M:%S'),
           'n_targets': len(targets),
           'n_matched': sum(1 for m in mapping if m['n'] > 0),
           'items': df.to_dict('records') if len(df) else []}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, 'etf_watchlist.json'), 'w',
              encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    store['etf_watchlist'] = out
    with open(STORE, 'wb') as f:
        pickle.dump(store, f, protocol=4)

    log('-' * 60)
    if len(df):
        for _, r in df.head(20).iterrows():
            log(f"  {r.sector:10s} {r.code}  {r.fund_name:14s} "
                f"现价 {r.close}  成交额 {r.amount/1e8:.2f}亿")
    log(f'可买 ETF {len(df)} 只（成交额≥{MIN_AMOUNT/1e4:.0f}万）'
        f'，耗时 {(datetime.now()-t0).total_seconds():.0f}s')
    return out


if __name__ == '__main__':
    build()
