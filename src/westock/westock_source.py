# -*- coding: utf-8 -*-
"""
统一数据源适配层 —— 用内置金融服务（腾讯自选股 / westock）替换 Tushare 自建代理

设计原则:
  1. 行情数据  -> 腾讯自选股（westock 同源）HTTP 接口，并发拉取，0.45s/只/300根
  2. 低频权威  -> westock CLI（板块榜单 / 涨跌分布 / 成分股），单次约 21s，仅用于补数据
  3. 静态映射  -> 复用本地 SQLite（股票清单 / 板块清单 / 成分股关系），这些数据不随行情变化
  4. 本地缓存  -> westock_kline.db，支持增量更新，避免重复拉取

代码格式: 本地 ts_code(600519.SH) <-> 腾讯 tx_code(sh600519)
"""
import os
import json
import time
import sqlite3
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DB_DIR = os.path.join(BASE_DIR, 'local_dbs')
# 缓存库放 westock/cache/ ：local_dbs 目录存在 SQLite 写入限制(disk I/O error)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_DB = os.path.join(CACHE_DIR, 'westock_kline.db')

INDUSTRY_DB = os.path.join(LOCAL_DB_DIR, 'industry.db')
STOCK_DB = os.path.join(LOCAL_DB_DIR, 'stock_daily.db')

UA = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'}

# ---- westock CLI（内置金融服务官方通道）----
# 沙箱代理会拦截腾讯行情直连(返回审计跳转页/501)，故批量行情统一走 westock CLI 官方通道。
# CLI 已本地安装到受管 node workspace，单次调用约 1.1s（npx 方式需 21s，勿用）。
# 可移植性：NODE_BIN / WESTOCK_CLI_JS 环境变量优先；否则自动探测。
NODE_CANDIDATES = [
    '/Users/beanpaper/.workbuddy/binaries/node/versions/22.22.2-2/bin/node',
    '/Users/beanpaper/.workbuddy/binaries/node/versions/22.22.2/bin/node',
    '/Users/beanpaper/.workbuddy/binaries/node/versions/22.12.0/bin/node',
    '/Users/beanpaper/.local/node/current/bin/node',
    '/usr/local/bin/node',
    '/usr/bin/node',
]
CLI_JS = (os.environ.get('WESTOCK_CLI_JS', '').strip() or
          '/Users/beanpaper/.workbuddy/binaries/node/workspace/'
          'node_modules/westock-data-skillhub/index.js')
BATCH_SIZE = 50          # 单批标的数量（实测 50 只 2.4s，100% 成功）

# 腾讯不提供的指数 -> 用最接近的替代标的（数据源切换导致的映射调整，须在报告中标注）
INDEX_FALLBACK = {
    '932000.CSI': 'sz399303',   # 中证2000 -> 国证2000（同为小盘代表）
    '000922.CSI': 'sh000922',
}

KLINE_SQL = '''CREATE TABLE IF NOT EXISTS kline (
    tx_code    TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    open  REAL, close REAL, high REAL, low REAL,
    vol   REAL, amount REAL,
    PRIMARY KEY (tx_code, trade_date)
)'''


# ==================== 工具 ====================
def to_tx_code(ts_code):
    """600519.SH -> sh600519 ; 000001.SZ -> sz000001 ; 932000.CSI -> 走 fallback"""
    if ts_code in INDEX_FALLBACK:
        return INDEX_FALLBACK[ts_code]
    code, mkt = ts_code.split('.')
    m = mkt.lower()
    if m in ('sh', 'ss'):
        return 'sh' + code
    if m in ('sz',):
        return 'sz' + code
    if m == 'bj':
        return 'bj' + code
    return 'sh' + code


def _cache_conn():
    c = sqlite3.connect(CACHE_DB, timeout=30)
    c.execute(KLINE_SQL)
    return c


# ---- 行情主存储改用 pickle ----
# 原因: 沙箱环境下 SQLite 写入百万级行会触发 disk I/O error，
#       且回滚日志(-journal)残留会让整个库读不出。pickle 无此问题，
#       pandas 原生支持、零依赖、读写都快。
PICKLE = os.path.join(CACHE_DIR, 'kline.pkl')
_COLS = ['tx_code', 'trade_date', 'open', 'close', 'high', 'low', 'vol',
         'amount', 'turnover']


def _read_pickle():
    import pandas as pd
    if not os.path.exists(PICKLE):
        return pd.DataFrame(columns=_COLS)
    return pd.read_pickle(PICKLE)


def load_cache(tx_codes=None, start_date=None):
    """读取缓存 -> DataFrame（列: tx_code trade_date open close high low vol amount）"""
    df = _read_pickle()
    if len(df) == 0:
        return df
    if tx_codes is not None:
        df = df[df.tx_code.isin(set(tx_codes))]
    if start_date:
        df = df[df.trade_date >= start_date]
    return df


# ==================== 网络拉取 ====================
def fetch_kline_raw(tx_code, n=250, retries=3):
    """拉单只标的日K（前复权）。返回 [[date,open,close,high,low,vol], ...]"""
    url = ('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
           f'?param={tx_code},day,,,{n},qfq')
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode('utf-8', 'ignore'))
            node = (d.get('data') or {}).get(tx_code) or {}
            k = node.get('qfqday') or node.get('day') or []
            if k:
                return k
            return []
        except Exception as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise RuntimeError(f'fetch {tx_code} failed: {last}')


def find_node():
    """自适应探测 node 路径（版本目录会被环境更新替换，禁止写死单一路径）

    优先级：环境变量 NODE_BIN > NODE_CANDIDATES（本机受管版本） > PATH 里的 node
    """
    env = os.environ.get('NODE_BIN', '').strip()
    if env and os.path.exists(env):
        return env
    for p in NODE_CANDIDATES:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    import shutil
    return shutil.which('node') or 'node'


def _parse_cli_table(text, default_sym=None):
    """解析 westock CLI 的 markdown 表格 -> {symbol: [[date,open,close,high,low,vol,amt]]}

    两种输出形态:
      批量模式: symbol | date | open | last | high | low | volume | amount | exchange
      单只模式: date | open | last | high | low | volume | amount | exchange (无 symbol 列)
    收盘价列名是 last。
    """
    import re
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('|'):
            continue
        cells = [c.strip() for c in line.strip('|').split('|')]
        if len(cells) < 7:
            continue
        if re.match(r'^\d{4}-\d{2}-\d{2}$', cells[0]):
            # 单只模式：第一列是日期
            if default_sym is None:
                continue
            sym, date, off = default_sym, cells[0], 1
        elif cells[0] in ('symbol', '代码'):
            continue
        elif re.match(r'^\d{4}-\d{2}-\d{2}$', cells[1] if len(cells) > 1 else ''):
            sym, date, off = cells[0], cells[1], 2
        else:
            continue
        try:
            o, c, h, l, v = (float(cells[off]), float(cells[off + 1]),
                             float(cells[off + 2]), float(cells[off + 3]),
                             float(cells[off + 4]))
            amt = float(cells[off + 5]) if len(cells) > off + 5 and cells[off + 5] else 0.0
            # exchange 列 = 换手率(%)，BOCI 的 F3 换手率强度需要
            try:
                turn = float(cells[off + 6]) if len(cells) > off + 6 and cells[off + 6] else 0.0
            except (ValueError, IndexError):
                turn = 0.0
        except (ValueError, IndexError):
            continue
        out.setdefault(sym, []).append([date, o, c, h, l, v, amt, turn])
    return out


def cli_kline_batch(tx_codes, start=None, end=None, limit=None, timeout=180):
    """用 westock CLI 批量拉日K（官方通道，不受沙箱代理拦截）。
    返回 ({symbol: klines}, err)"""
    import subprocess
    args = [find_node(), CLI_JS, 'kline', ','.join(tx_codes), '--period', 'day']
    if start and end:
        args += ['--start', start, '--end', end]
    else:
        args += ['--limit', str(limit or 250)]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        txt = p.stdout or ''
        if '| date |' not in txt and '| symbol |' not in txt:
            return {}, (p.stderr or txt)[:300]
        # 单只模式无 symbol 列，需回填请求代码
        dflt = tx_codes[0] if len(tx_codes) == 1 else None
        return _parse_cli_table(txt, default_sym=dflt), None
    except Exception as e:
        return {}, f'{type(e).__name__}: {e}'


def fetch_many_cli(tx_codes, start=None, end=None, limit=250, workers=4,
                   log=None):
    """分批 + 并发调用 westock CLI 拉全市场日K。
    返回 (kline_map, fail_codes)"""
    batches = [tx_codes[i:i + BATCH_SIZE]
               for i in range(0, len(tx_codes), BATCH_SIZE)]
    out, fail = {}, []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(cli_kline_batch, b, start, end, limit): b
                for b in batches}
        for i, f in enumerate(as_completed(futs), 1):
            b = futs[f]
            try:
                m, err = f.result()
                out.update(m)
                if err:
                    fail.extend(b)
            except Exception:
                fail.extend(b)
            if log and i % 20 == 0:
                log(f'  批次 {i}/{len(batches)}, 已取 {len(out)} 只, 失败 {len(fail)}')
    return out, fail


def fetch_many(tx_codes, n=250, workers=16, log=None):
    """并发拉取多只标的（腾讯直连通道）。返回 {tx_code: klines}"""
    out, fail = {}, []
    total = len(tx_codes)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_kline_raw, c, n): c for c in tx_codes}
        for i, f in enumerate(as_completed(futs), 1):
            c = futs[f]
            try:
                out[c] = f.result()
            except Exception:
                out[c] = []
                fail.append(c)
            if log and i % 500 == 0:
                log(f'  拉取进度 {i}/{total}, 失败 {len(fail)}')
    return out, fail


# ==================== 缓存读写 ====================
def save_cache(kline_map):
    """合并写入 pickle 缓存（按 tx_code+trade_date 去重，新数据覆盖旧的）"""
    import pandas as pd
    recs = []
    for tx, klines in kline_map.items():
        for row in klines:
            if len(row) < 6:
                continue
            try:
                recs.append((tx, row[0].replace('-', ''),
                             float(row[1]), float(row[2]), float(row[3]),
                             float(row[4]), float(row[5]),
                             float(row[6]) if len(row) > 6 else 0.0,
                             float(row[7]) if len(row) > 7 else 0.0))
            except Exception:
                continue
    if not recs:
        return 0
    new = pd.DataFrame(recs, columns=_COLS)
    old = _read_pickle()
    if len(old):
        df = pd.concat([old, new], ignore_index=True)
        df = df.drop_duplicates(['tx_code', 'trade_date'], keep='last')
    else:
        df = new
    df = df.sort_values(['tx_code', 'trade_date']).reset_index(drop=True)
    df.to_pickle(PICKLE)
    return len(df)


def cache_max_date(tx_code):
    df = _read_pickle()
    if len(df) == 0:
        return None
    s = df[df.tx_code == tx_code]['trade_date']
    return s.max() if len(s) else None


# ==================== 静态映射（复用本地库）====================
def get_stock_universe():
    """全市场股票清单 -> [ts_code] （来自本地 stock_list，静态数据）"""
    c = sqlite3.connect(STOCK_DB)
    rows = [r[0] for r in c.execute('SELECT ts_code FROM stock_list')]
    c.close()
    return rows


def get_sector_list(types=None):
    """同花顺板块清单 -> DataFrame[ts_code, name, type]
    type: I=行业 N=概念 R=地域 S=... （EW-SDM 主要用 I/N）"""
    import pandas as pd
    c = sqlite3.connect(INDUSTRY_DB)
    if types:
        ph = ','.join('?' * len(types))
        df = pd.read_sql(f'SELECT ts_code,name,type FROM ths_index WHERE type IN ({ph})',
                         c, params=list(types))
    else:
        df = pd.read_sql('SELECT ts_code,name,type FROM ths_index', c)
    c.close()
    return df


def get_sector_members(sector_codes=None):
    """板块成分股映射 -> {sector_ts_code: [con_code, ...]}"""
    c = sqlite3.connect(INDUSTRY_DB)
    if sector_codes:
        ph = ','.join('?' * len(sector_codes))
        sql = f'SELECT ts_code,con_code FROM ths_member WHERE ts_code IN ({ph})'
        cur = c.execute(sql, list(sector_codes))
    else:
        cur = c.execute('SELECT ts_code,con_code FROM ths_member')
    m = {}
    for s, con in cur:
        m.setdefault(s, []).append(con)
    c.close()
    return m


# ==================== westock CLI（低频权威补数据）====================
def westock_cli(args, timeout=180):
    """调用内置 westock CLI。仅用于低频、权威、腾讯接口没有的数据。
    单次约 21s（npx 启动开销），禁止在循环里高频调用。

    优先走本地 node 调用（~1.1s/次），仅作为 fallback。
    """
    import subprocess
    node = find_node()
    try:
        cmd = [node, CLI_JS] + args
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.stdout:
            return p.stdout
        if p.returncode == 0:
            return p.stdout
    except Exception:
        pass
    # fallback: npx（首次启动慢）
    env = dict(os.environ)
    env['npm_config_registry'] = 'https://registry.npmjs.org'
    cmd = ['npx', '-y', 'westock-data-skillhub@1.0.5'] + args
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.stdout or p.stderr
    except Exception as e:
        return f'ERROR: {e}'


def get_market_changedist():
    """市场涨跌分布（涨停/跌停/上涨占比）—— westock changedist"""
    return westock_cli(['changedist'])


def get_sector_ranking():
    """全市场板块涨幅榜（行业 + 概念）—— westock sector ranking"""
    return westock_cli(['sector', 'ranking', '--limit', '30'])


# ==================== 高层接口 ====================
def split_segments(start, end, span_days=330):
    """把长区间切成多段。CLI 单次返回上限约 250 根K线，
    所以 250 交易日(≈365自然日)以上的区间必须分段请求。"""
    from datetime import timedelta
    s = datetime.strptime(start, '%Y-%m-%d')
    e = datetime.strptime(end, '%Y-%m-%d')
    segs, cur = [], s
    while cur < e:
        nxt = min(cur + timedelta(days=span_days), e)
        segs.append((cur.strftime('%Y-%m-%d'), nxt.strftime('%Y-%m-%d')))
        cur = nxt + timedelta(days=1)
    return segs


def refresh_universe(days=120, workers=4, log=print, only_missing_days=True,
                     limit=None, start_date=None, segment=True):
    """刷新全市场日线到缓存库（走 westock CLI 官方通道，分批 + 并发）。

    days: 回溯自然日天数（会转成起止日期传给 CLI）
    返回统计 dict
    """
    codes = get_stock_universe()
    tx = [to_tx_code(c) for c in codes]
    if limit:
        tx = tx[:limit]
    todo = tx
    if only_missing_days:
        done = set(_read_pickle().tx_code.unique()) if os.path.exists(PICKLE) \
            else set()
        todo = [c for c in tx if c not in done]
    if not todo:
        log('[数据源] 全部标的已是最新，跳过')
        return {'total': len(tx), 'updated': 0, 'rows': 0, 'fail': 0}

    end = datetime.now().strftime('%Y-%m-%d')
    if start_date:
        start = start_date
    else:
        from datetime import timedelta
        start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    segs = split_segments(start, end) if segment else [(start, end)]
    log(f'[数据源] 待更新 {len(todo)} / 全市场 {len(tx)}，'
        f'区间 {start}~{end}，分 {len(segs)} 段，批次 {BATCH_SIZE}，并发 {workers}')
    t0 = time.time()
    kmap, fail = {}, []
    for i, (s, e) in enumerate(segs, 1):
        m, f = fetch_many_cli(todo, start=s, end=e, workers=workers, log=log)
        for k, v in m.items():
            kmap.setdefault(k, []).extend(v)
        fail.extend(f)
        log(f'  段 {i}/{len(segs)} {s}~{e}: 累计 {len(kmap)} 只')
    rows = save_cache(kmap)
    log(f'[数据源] 取到 {len(kmap)} 只, 写入 {rows} 行, '
        f'耗时 {time.time()-t0:.0f}s, 失败 {len(fail)}')
    return {'total': len(tx), 'updated': len(todo), 'rows': rows,
            'fail': len(fail), 'seconds': round(time.time() - t0),
            'segments': len(segs)}


def get_daily_df(ts_codes=None, n=250, use_cache=True, refresh=False):
    """取个股日线 DataFrame（列: ts_code, trade_date, close, pct_chg, ...）
    这是给策略引擎的统一入口，替代原 Tushare 的 daily 表。"""
    import pandas as pd
    if ts_codes is None:
        ts_codes = get_stock_universe()
    tx2ts = {to_tx_code(c): c for c in ts_codes}
    if refresh:
        kmap, _ = fetch_many(list(tx2ts.keys()), n=n)
        save_cache(kmap)
    df = load_cache(list(tx2ts.keys()))
    if len(df) == 0:
        return pd.DataFrame()
    df['ts_code'] = df.tx_code.map(tx2ts)
    df = df.dropna(subset=['ts_code'])
    df = df.sort_values(['ts_code', 'trade_date'])
    # 保留最近 n 根
    df = df.groupby('ts_code', group_keys=False).tail(n)
    df['pre_close'] = df.groupby('ts_code')['close'].shift(1)
    df['pct_chg'] = (df['close'] / df['pre_close'] - 1) * 100
    return df.reset_index(drop=True)


def get_index_daily(ts_code, n=800, refresh=True):
    """取单个指数日线 -> DataFrame（用于 RF / LC 等择时策略，需要长历史预热）"""
    import pandas as pd
    tx = to_tx_code(ts_code)
    if refresh:
        m, err = cli_kline_batch([tx], limit=n)
        if m:
            save_cache(m)
        elif err:
            print(f'[warn] CLI 取 {tx} 失败: {err}')
    df = load_cache([tx])
    if len(df) == 0:
        return pd.DataFrame()
    df = df.sort_values('trade_date').tail(n).reset_index(drop=True)
    df['ts_code'] = ts_code
    df['pre_close'] = df['close'].shift(1)
    df['pct_chg'] = (df['close'] / df['pre_close'] - 1) * 100
    return df


if __name__ == '__main__':
    print('=== 数据源自检 ===')
    print('股票清单数量:', len(get_stock_universe()))
    s = get_sector_list(['I', 'N'])
    print('板块(行业+概念)数量:', len(s))
    print('板块样例:\n', s.head(5).to_string(index=False))
    df = get_index_daily('000001.SH', n=10)
    print('上证指数最新:\n', df[['trade_date', 'close']].tail(5).to_string(index=False))
    idx = get_index_daily('932000.CSI', n=10)
    print('中证2000(已回落国证2000)最新:\n',
          idx[['trade_date', 'close']].tail(3).to_string(index=False))
