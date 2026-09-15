# -*- coding: utf-8 -*-
"""日K抓取(多源容错链): 腾讯(web.ifzq.gtimg.cn) -> 东方财富(push2his.eastmoney.com)。
本机网络对行情站点存在间歇性拦截(同一主机时通时断), 因此单只股票按「源顺序 + 多次重试」获取,
任一源成功即写出CSV。统一输出格式: date,open,high,low,close,volume (前复权, 可保留当日已完成bar)。

用法: python fetch_kline.py <pool.tsv> <outdir> <maxbars> [keep_today_date]
keep_today_date: 形如 2026-09-11, 当日bar将被保留(盘后使用); 缺省丢弃当日未完成bar。

环境变量:
  CHAN_DATA_SOURCE = auto(默认,腾讯→东财链) | tencent | eastmoney
  CHAN_FETCH_TRIES = 每源重试次数(默认 8)
"""
import sys, os, time, csv, random, requests
from concurrent.futures import ThreadPoolExecutor, as_completed

TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
EM_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_FIELDS1 = "f1,f2,f3,f4,f5,f6"
EM_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57"   # date,open,close,high,low,volume,amount
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120"}
TIMEOUT = (6, 14)          # (连接, 读取) 秒 —— 连接超时短, 快速失败快速重试
TRIES = int(os.environ.get("CHAN_FETCH_TRIES", "8"))

# 兼容旧引用
URL = TX_URL


# ---------------- 工具 ----------------
def tx_available(timeout=6):
    """探测腾讯行情是否可达"""
    try:
        r = requests.get(TX_URL, params={"param": "sh000001,day,,,2,qfq"},
                         headers=UA, timeout=timeout)
        j = r.json()
        return bool((j.get("data") or {}).get("sh000001"))
    except Exception:
        return False


def resolve_source(force=None):
    """返回数据源标记(auto|tencent|eastmoney), 仅用于日志/分支兼容"""
    want = (force or os.environ.get("CHAN_DATA_SOURCE") or "auto").lower()
    if want in ("tencent", "eastmoney"):
        return want
    return "auto"


def em_secid(code):
    """sh600519 -> 1.600519 ; sz000001 -> 0.000001 ; sh000001(上证指数) -> 1.000001"""
    c = code.strip().lower()
    if c.startswith("sh"):
        return "1." + c[2:]
    if c.startswith("sz"):
        return "0." + c[2:]
    if c.startswith("bj"):
        return "0." + c[2:]
    return "1." + c


def _normalize(kl, keep_today):
    """(date,open,close,high,low,volume) 序列 -> 统一列序, 并丢弃未完成当日bar"""
    rows = []
    today = time.strftime("%Y-%m-%d")
    for rec in kl:
        arr = rec.split(",") if isinstance(rec, str) else rec
        if len(arr) < 6:
            continue
        d = arr[0]
        if d >= today and d != keep_today:      # 丢未完成当日(非keep目标)
            continue
        if d > today:                           # 容错: 未来日期不取
            continue
        try:
            o, c, h, l, v = (float(arr[1]), float(arr[2]),
                             float(arr[3]), float(arr[4]), float(arr[5]))
        except ValueError:
            continue
        rows.append((d, o, h, l, c, v))
    return rows


def _write_csv(outdir, code, rows):
    with open(os.path.join(outdir, code + ".csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for row in rows:
            w.writerow(row)


# ---------------- 单次抓取(按源) ----------------
def _fetch_tx(code, maxbars):
    r = requests.get(TX_URL, params={"param": f"{code},day,,,{maxbars},qfq"},
                     headers=UA, timeout=TIMEOUT)
    data = (r.json().get("data") or {}).get(code) or {}
    key = "qfqday" if "qfqday" in data else "day"
    return data.get(key) or []


def _fetch_em(code, maxbars):
    r = requests.get(EM_URL, params={
        "secid": em_secid(code), "fields1": EM_FIELDS1, "fields2": EM_FIELDS2,
        "klt": 101, "fqt": 1, "end": 20500101, "lmt": maxbars,
    }, headers=UA, timeout=TIMEOUT)
    return ((r.json() or {}).get("data") or {}).get("klines") or []


_SRC_FN = {"tencent": _fetch_tx, "eastmoney": _fetch_em}


def _source_chain(source=None):
    s = resolve_source(source)
    return [s] if s in ("tencent", "eastmoney") else ["tencent", "eastmoney"]


def fetch_one(code, maxbars, outdir, keep_today, source=None):
    """按源链+多次重试抓单只; 成功即写CSV。返回 (code, nbars, status)"""
    last_err = "unknown"
    for src in _source_chain(source):
        fn = _SRC_FN[src]
        for att in range(TRIES):
            try:
                kl = fn(code, maxbars)
                if not kl:
                    last_err = "empty"
                    break                      # 该源无数据, 换下一源
                rows = _normalize(kl, keep_today)
                if len(rows) < 40:
                    return (code, len(rows), "tooshort")
                _write_csv(outdir, code, rows)
                return (code, len(rows), "ok")
            except Exception as e:
                last_err = f"err:{type(e).__name__}"
                time.sleep(min(0.4 * (att + 1), 2.0) + random.random() * 0.3)
    return (code, 0, last_err)


# ---------------- 批量 ----------------
def fetch_pool(codes, outdir, maxbars, keep_today=None, source=None, max_workers=14):
    """批量拉取。返回 (ok, short, fail, total_bars, elapsed)"""
    os.makedirs(outdir, exist_ok=True)
    ok = short = fail = nbars = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(fetch_one, c, maxbars, outdir, keep_today, source): c
                for c in codes}
        for fut in as_completed(futs):
            code, n, st = fut.result()
            if st == "ok":
                ok += 1; nbars += n
            elif st == "tooshort":
                short += 1
            else:
                fail += 1
                print(f"  FAIL {code}: {st}", flush=True)
    return ok, short, fail, nbars, time.time() - t0


def fetch_klines(code, maxbars=3, keep_today=None, source=None):
    """取单只最近maxbars根日K, 返回 [(date,open,high,low,close,volume), ...](升序)
    注意: 该函数用于交易日判断/大盘快照, 默认保留当日bar(keep_today默认=今天),
    否则当日bar会被 _normalize 当作"未完成bar"丢弃, 导致误判为非交易日。
    """
    keep = keep_today or time.strftime("%Y-%m-%d")
    for src in _source_chain(source):
        fn = _SRC_FN[src]
        for att in range(4):
            try:
                kl = fn(code, maxbars)
                if kl:
                    return _normalize(kl, keep)
            except Exception:
                time.sleep(0.4 * (att + 1))
    return []


def last_bar_date(code):
    """查某标的最近一根日K的日期(用于交易日判断)"""
    rows = fetch_klines(code, 2)
    return rows[-1][0] if rows else None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pool_tsv")
    ap.add_argument("outdir")
    ap.add_argument("maxbars", type=int)
    ap.add_argument("keep", nargs="?", default=None)
    ap.add_argument("--source", default=None)
    a = ap.parse_args()
    codes = []
    with open(a.pool_tsv) as f:
        for line in f:
            line = line.strip()
            if line:
                codes.append(line.split("\t")[0])
    print(f"pool={len(codes)} maxbars={a.maxbars} keep_today={a.keep} "
          f"source={resolve_source(a.source)} tries={TRIES}", flush=True)
    res = fetch_pool(codes, a.outdir, a.maxbars, a.keep, source=a.source)
    print(f"DONE ok={res[0]} short={res[1]} fail={res[2]} bars={res[3]} elapsed={res[4]:.0f}s")
