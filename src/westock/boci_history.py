# -*- coding: utf-8 -*-
"""
BOCI 行业情绪 · 历史与机会池

输入: cache/factors.pkl['boci_daily'/'boci_latest'] + cache/kline.pkl
输出:
  1. 机会池 — 当前 sentiment 在历史百分位 + 与 K 线对比
  2. 交互页 data/boci_history.json — 每个行业 sentiment/close/f1-f5 历史序列
"""
import os
import sys
import json
import pickle
import warnings
from datetime import datetime

import pandas as pd
import numpy as np

warnings.filterwarnings('ignore')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

STORE = os.path.join(HERE, 'cache', 'factors.pkl')
KL_STORE = os.path.join(HERE, 'cache', 'kline.pkl')
OUT_DIR = os.path.join(HERE, 'output')
DATA_DIR = os.path.join(OUT_DIR, 'data')


# 阈值（百分位 + 绝对值双重判断）
PCT_OVERBOUGHT = 0.85
PCT_OVERSOLD = 0.15
ABS_OVERBOUGHT = 0.70      # sentiment > 0.70 算超买
ABS_OVERSOLD = 0.30        # sentiment < 0.30 算超跌
WINDOW = 60                # 百分位回看窗口


def log(m):
    print(m, flush=True)


def load_boci():
    with open(STORE, 'rb') as f:
        d = pickle.load(f)
    return d.get('boci_daily')


def compute_opportunities(bd):
    """对每个行业（按最后一天）算：当前 sentiment、在近 60 日历史里的百分位、机会类型"""
    rows = []
    for sec, g in bd.groupby('sector'):
        g = g.sort_values('trade_date').reset_index(drop=True)
        if len(g) < 5:
            continue
        cur = g.iloc[-1]
        s_now = float(cur['sentiment']) if pd.notna(cur['sentiment']) else None
        if s_now is None:
            continue
        # 近 60 日百分位（不含今天，看今天在历史上的相对位置）
        last60 = g['sentiment'].dropna().tail(WINDOW + 1).values
        if len(last60) < 10:
            continue
        pct60 = (last60[:-1] < s_now).mean()   # 不含今天
        pct120 = None
        last120 = g['sentiment'].dropna().tail(121).values
        if len(last120) >= 20:
            pct120 = (last120[:-1] < s_now).mean()
        # 5 日变化
        s5 = g['sentiment'].dropna().tail(6).values
        d5 = (s_now - float(s5[0])) if len(s5) >= 6 else None
        # K 线信息
        pct_chg5 = None
        c5 = g['close'].dropna().tail(6).values
        if len(c5) >= 6:
            pct_chg5 = (c5[-1] / c5[0] - 1) * 100
        # 评分标签
        if s_now >= ABS_OVERBOUGHT or (pct60 is not None and pct60 >= PCT_OVERBOUGHT):
            tag = 'overbought'
        elif s_now <= ABS_OVERSOLD or (pct60 is not None and pct60 <= PCT_OVERSOLD):
            tag = 'oversold'
        else:
            tag = 'neutral'
        rows.append({
            'sector': sec,
            'name': cur.get('name') or _name_of(sec),
            'trade_date': str(cur['trade_date']),
            'sentiment': round(s_now, 4),
            'score': round(float(cur.get('score', 0) or 0), 4),
            'pct_60': round(pct60, 4) if pct60 is not None else None,
            'pct_120': round(pct120, 4) if pct120 is not None else None,
            'd5': round(d5, 4) if d5 is not None else None,
            'pct_chg5': round(pct_chg5, 2) if pct_chg5 is not None else None,
            'close': round(float(cur['close']), 4) if pd.notna(cur['close']) else None,
            'overheat': bool(cur.get('overheat', False)),
            'tag': tag,
        })
    return pd.DataFrame(rows)


def _name_of(code):
    """881101.TI -> 查 industry.db 拿名字（懒加载）"""
    try:
        import sqlite3
        c = sqlite3.connect(os.path.join(
            os.path.dirname(HERE), 'local_dbs', 'industry.db'))
        row = c.execute("SELECT name FROM ths_index WHERE ts_code=?", (code,)).fetchone()
        return row[0] if row else code
    except Exception:
        return code


_NAME_MAP = None


def _name_map():
    global _NAME_MAP
    if _NAME_MAP is None:
        try:
            import sqlite3
            c = sqlite3.connect(os.path.join(
                os.path.dirname(HERE), 'local_dbs', 'industry.db'))
            _NAME_MAP = {r[0]: r[1] for r in c.execute(
                "SELECT ts_code, name FROM ths_index WHERE ts_code LIKE '881%'").fetchall()}
        except Exception:
            _NAME_MAP = {}
    return _NAME_MAP


def attach_names(bd):
    nm = _name_map()
    bd = bd.copy()
    bd['name'] = bd['sector'].map(lambda c: nm.get(c, c))
    return bd


def dump_history(bd):
    """把每个行业的 sentiment / close / f1-f5 时序写入 data/boci_history.json
    用于前端交互页"""
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {'gen_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
               'window': WINDOW,
               'thresholds': {
                   'pct_overbought': PCT_OVERBOUGHT,
                   'pct_oversold': PCT_OVERSOLD,
                   'abs_overbought': ABS_OVERBOUGHT,
                   'abs_oversold': ABS_OVERSOLD,
               },
               'industries': []}
    for sec, g in bd.groupby('sector'):
        g = g.sort_values('trade_date').reset_index(drop=True)
        # 取近 240 天（约 1 年），更早的数据前段值无效不展示
        g = g.tail(240)
        if len(g) < 5:
            continue
        payload['industries'].append({
            'sector': sec,
            'name': _name_of(sec),
            'dates': g['trade_date'].astype(str).tolist(),
            'sentiment': [None if pd.isna(x) else round(float(x), 4)
                          for x in g['sentiment']],
            'close': [None if pd.isna(x) else round(float(x), 4)
                      for x in g['close']],
            'pct_chg': [None if pd.isna(x) else round(float(x), 2)
                        for x in g['pct_chg']],
            'f1': [None if pd.isna(x) else round(float(x), 4) for x in g['f1']],
            'f2': [None if pd.isna(x) else round(float(x), 4) for x in g['f2']],
            'f3': [None if pd.isna(x) else round(float(x), 4) for x in g['f3']],
            'f4': [None if pd.isna(x) else round(float(x), 4) for x in g['f4']],
            'f5': [None if pd.isna(x) else round(float(x), 4) for x in g['f5']],
        })
    fp = os.path.join(DATA_DIR, 'boci_history.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
    log(f'  历史序列: {fp} ({len(payload["industries"])} 个行业, '
        f'{os.path.getsize(fp)/1024:.0f} KB)')
    return fp


def update_store(opp):
    """写回 factors.pkl['boci_opportunities'] 供 report 渲染"""
    with open(STORE, 'rb') as f:
        d = pickle.load(f)
    d['boci_opportunities'] = opp.to_dict('records')
    with open(STORE, 'wb') as f:
        pickle.dump(d, f, protocol=4)
    log(f'  boci_opportunities 写回 factors.pkl ({len(opp)} 行)')


def main():
    t0 = datetime.now()
    log('=' * 60)
    log(f'BOCI 行业情绪 · 历史与机会池  {t0:%H:%M:%S}')
    bd = load_boci()
    if bd is None or len(bd) == 0:
        log('[fatal] factors.pkl 缺 boci_daily，请先跑 boci_backfill.py')
        return
    bd = attach_names(bd)
    log(f'加载 {len(bd)} 行情绪历史 '
        f'({bd.sector.nunique()} 行业 × {bd.trade_date.nunique()} 天)')

    log('--- 算机会池 ---')
    opp = compute_opportunities(bd)
    n_oh = (opp.tag == 'overbought').sum()
    n_os = (opp.tag == 'oversold').sum()
    log(f'  超买 {n_oh} 个 / 超跌 {n_os} 个 / 中性 {len(opp)-n_oh-n_os} 个')
    # 列出 Top 5 超跌机会
    if n_os:
        os_top = opp[opp.tag == 'oversold'].sort_values(
            'pct_60', ascending=True).head(5)
        log('  超跌 Top 5（pct_60 从小到大）:')
        for _, r in os_top.iterrows():
            log(f"    {r['name']:14s} sentiment={r.sentiment:.3f} "
                f"pct60={r.pct_60:.2%} d5={r.d5:+.3f}")
    if n_oh:
        oh_top = opp[opp.tag == 'overbought'].sort_values(
            'pct_60', ascending=False).head(5)
        log('  超买 Top 5（pct_60 从大到小）:')
        for _, r in oh_top.iterrows():
            log(f"    {r['name']:14s} sentiment={r.sentiment:.3f} "
                f"pct60={r.pct_60:.2%} d5={r.d5:+.3f}")

    log('--- Dump 历史序列（前端交互页用）---')
    dump_history(bd)

    log('--- 写回 factors.pkl ---')
    update_store(opp)

    # 另存一个精简版给 report 用
    fp = os.path.join(OUT_DIR, 'boci_opportunities.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump({'gen_time': t0.strftime('%Y-%m-%d %H:%M:%S'),
                   'window': WINDOW,
                   'thresholds': {
                       'pct_overbought': PCT_OVERBOUGHT,
                       'pct_oversold': PCT_OVERSOLD,
                       'abs_overbought': ABS_OVERBOUGHT,
                       'abs_oversold': ABS_OVERSOLD,
                   },
                   'items': opp.sort_values(['tag', 'pct_60']).to_dict('records')},
                  f, ensure_ascii=False, indent=2, default=str)
    log(f'  {fp}')
    log(f'完成 耗时 {(datetime.now()-t0).total_seconds():.0f}s')
    return opp


if __name__ == '__main__':
    main()