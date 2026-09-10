# -*- coding: utf-8 -*-
"""腾讯行情抓取：日K前复权(可保留当日已完成bar)。
用法: python fetch_kline.py <pool.tsv> <outdir> <maxbars> [keep_today_date]
keep_today_date: 形如 2026-09-09, 当日bar将被保留(盘后使用); 缺省丢弃当日未完成bar。
"""
import sys, os, time, csv, requests
from concurrent.futures import ThreadPoolExecutor, as_completed

URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

def fetch_one(code, maxbars, outdir, keep_today):
    for att in range(4):
        try:
            r = requests.get(URL, params={"param": f"{code},day,,,{maxbars},qfq"},
                             headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120"},
                             timeout=12)
            j = r.json()
            data = (j.get("data") or {}).get(code) or {}
            key = "qfqday" if "qfqday" in data else "day"
            kl = data.get(key) or []
            if not kl:
                return (code, 0, "empty")
            rows = []
            today = time.strftime("%Y-%m-%d")
            for arr in kl:
                if len(arr) < 6:
                    continue
                d = arr[0]
                if d >= today and d != keep_today:      # 丢未完成当日(非keep目标)
                    continue
                if d > today:                            # 容错: 未来日期不取
                    continue
                # 腾讯 fqkline 列序: date,open,close,high,low,volume
                rows.append((d, float(arr[1]), float(arr[3]), float(arr[4]), float(arr[2]), float(arr[5])))
            if len(rows) < 40:
                return (code, len(rows), "tooshort")
            with open(os.path.join(outdir, code + ".csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "open", "high", "low", "close", "volume"])
                for row in rows:
                    w.writerow(row)
            return (code, len(rows), "ok")
        except Exception as e:
            if att == 3:
                return (code, 0, f"err:{type(e).__name__}")
            time.sleep(0.5 * (att + 1))
    return (code, 0, "unknown")

def fetch_pool(codes, outdir, maxbars, keep_today=None):
    """批量拉取。返回 (ok, short, fail, total_bars)"""
    os.makedirs(outdir, exist_ok=True)
    ok = short = fail = nbars = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=14) as ex:
        futs = {ex.submit(fetch_one, c, maxbars, outdir, keep_today): c for c in codes}
        done = 0
        for fut in as_completed(futs):
            code, n, st = fut.result()
            done += 1
            if st == "ok":
                ok += 1; nbars += n
            elif st == "tooshort":
                short += 1
            else:
                fail += 1
                print(f"  FAIL {code}: {st}", flush=True)
    return ok, short, fail, nbars, time.time() - t0

def last_bar_date(code):
    """查某标的最近一根日K的日期(用于交易日判断)"""
    try:
        r = requests.get(URL, params={"param": f"{code},day,,,2,qfq"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        data = (r.json().get("data") or {}).get(code) or {}
        kl = data.get("qfqday") or data.get("day") or []
        return kl[-1][0] if kl else None
    except Exception:
        return None

if __name__ == "__main__":
    pool_tsv, outdir, maxbars = sys.argv[1], sys.argv[2], int(sys.argv[3])
    keep = sys.argv[4] if len(sys.argv) > 4 else None
    codes = []
    with open(pool_tsv) as f:
        for line in f:
            line = line.strip()
            if line:
                codes.append(line.split("\t")[0])
    print(f"pool={len(codes)} maxbars={maxbars} keep_today={keep}", flush=True)
    res = fetch_pool(codes, outdir, maxbars, keep)
    print(f"DONE ok={res[0]} short={res[1]} fail={res[2]} bars={res[3]} elapsed={res[4]:.0f}s")
