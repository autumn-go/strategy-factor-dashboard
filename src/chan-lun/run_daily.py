# -*- coding: utf-8 -*-
"""每日盘后主入口: 交易日判断 → 抓全池K线(含当日) → 缠论扫描 → 报告+摘要 → 飞书推送
用法:
  python run_daily.py [--date YYYY-MM-DD] [--skip-fetch] [--no-push] [--charts N]
非交易日(周末/节假日)自动跳过,exit 0。
"""
import os, sys, json, time, subprocess, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import config as C
from core import fetch_kline, chan_scan, report_gen

def load_pool():
    pool = {}
    if os.path.exists(C.POOL_TSV):
        with open(C.POOL_TSV) as f:
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) >= 2:
                    pool[parts[0]] = parts[1]
    return pool

def market_snapshot(date):
    """上证指数当日收盘/涨跌(用于文案)。走 fetch_kline 的统一数据源(腾讯/东财自动切换)。"""
    try:
        rows = fetch_kline.fetch_klines(C.MARKET_INDEX, 3)
        if rows and rows[-1][0] == date and len(rows) >= 2:
            c0, c1 = rows[-1][4], rows[-2][4]
            return {"date": date, "close": c0, "chg": round((c0 / c1 - 1) * 100, 2)}
    except Exception:
        pass
    return None

def conclusion(rows, meta):
    n3 = sum(1 for x in rows for s in [x["s"]] if s["type"] == "BUY3")
    n1 = sum(1 for x in rows for s in [x["s"]] if s["type"] == "BUY1")
    n2 = sum(1 for x in rows for s in [x["s"]] if s["type"] == "BUY2")
    mk = meta.get("market")
    mk_dir = "上涨" if mk and mk["chg"] >= 0 else "下跌"
    if n3 == 0 and n1 > 0:
        txt = (f"今日大盘{mk_dir}({mk['chg']:+.2f}%),全池日线三买结构=0,"
               f"趋势中继机会未批量出现;一买({n1})与二买({n2})结构居多,"
               f"属弱转强/震荡期特征 —— 短线以「右侧二买回踩」为主,一买仅作左侧埋伏(必须带止损)。")
    elif n3 > 0:
        txt = (f"今日大盘{mk_dir}({mk['chg']:+.2f}%),全池出现 {n3} 个日线三买结构,"
               f"趋势中继信号回归,强势股回踩确认机会可重点跟踪;同时一买 {n1} / 二买 {n2}。")
    else:
        txt = (f"今日大盘{mk_dir}({mk['chg']:+.2f}%),全池一/二/三买均稀疏,"
               f"结构性机会有限,建议降低出手频率或聚焦强势主线。")
    return txt

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--charts", type=int, default=16)
    args = ap.parse_args()

    log = f"{C.LOG_DIR}/run_{args.date}.log"
    os.makedirs(C.LOG_DIR, exist_ok=True)
    def logp(msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log, "a") as f:
            f.write(line + "\n")

    os.makedirs(C.DATA_DIR, exist_ok=True)
    os.makedirs(C.OUT_DIR, exist_ok=True)

    # 1. 交易日判断: 上证指数最近一根日K是否=目标日
    src = fetch_kline.resolve_source()
    logp(f"数据源 {src}")
    lb = fetch_kline.last_bar_date(C.MARKET_INDEX)
    if lb != args.date:
        logp(f"{args.date} 非交易日(上证最近K线日期={lb}),跳过。")
        return 0
    mk = market_snapshot(args.date)
    logp(f"交易日确认 {args.date} · 上证 {mk['close'] if mk else '?'} ({mk['chg'] if mk else '?'}%)")

    # 2. 股票池
    pool = load_pool()
    if len(pool) < 100:
        logp(f"股票池异常: {len(pool)} 只,中止。请先运行 update_pool.sh")
        return 1
    logp(f"股票池 {len(pool)} 只")

    # 3. 抓K线
    if not args.skip_fetch:
        ok, short, fail, nbars, dt = fetch_kline.fetch_pool(
            list(pool.keys()), C.DATA_DIR, C.FETCH_BARS, keep_today=args.date)
        logp(f"抓K线({fetch_kline.resolve_source()}) ok={ok} short={short} fail={fail} bars={nbars} {dt:.0f}s")
        if ok < len(pool) * 0.9:
            logp("抓取成功率过低,中止。")
            return 1
    else:
        logp("skip-fetch: 使用已有K线数据")

    # 4. 扫描
    items = []
    nfiles = 0
    for code in pool:
        path = os.path.join(C.DATA_DIR, code + ".csv")
        if not os.path.exists(path):
            continue
        nfiles += 1
        try:
            df = __import__("pandas").read_csv(path)
            if len(df) < 120:
                continue
            r = chan_scan.analyze(df, code)
            s = chan_scan.best_signal(r)
            if s is None:
                continue
            r["name"] = pool[code]
            off = s.get("pct_from_buy")
            if off is None or off < -2:       # 已跌破买点价的不给A/B
                g = "C"
            else:
                g = chan_scan.grade_of(s)
            items.append({"r": r, "s": s, "g": g, "buy": s.get("buy_price"),
                          "off": round(off, 2) if off is not None else None,
                          "stop": chan_scan.stop_of(s)})
        except Exception as e:
            pass
    # 排序: A < B < C, 组内按分数
    items.sort(key=lambda x: ({"A": 0, "B": 1, "C": 2}[x["g"]], -chan_scan.sig_score(x["s"])))
    logp(f"扫描完成 {nfiles} 只, 命中 {len(items)} (A={sum(1 for x in items if x['g']=='A')}, "
         f"B={sum(1 for x in items if x['g']=='B')}, C={sum(1 for x in items if x['g']=='C')})")

    meta = {"asof": args.date, "pool_n": len(pool), "market": mk,
            "gen_time": time.strftime("%Y-%m-%d %H:%M"),
            "conclusion": conclusion(items, {"market": mk})}

    # 5. 报告
    out_html = os.path.join(C.OUT_DIR, f"chan_report_{args.date}.html")
    report_gen.build(items, meta, out_html, max_chart_a=args.charts)
    summary = report_gen.build_summary_txt(items, meta)
    json.dump({"meta": meta,
               "rows": [{"code": x["r"]["code"], "name": x["r"].get("name", ""),
                         "grade": x["g"], "close": x["r"]["close"],
                         "sig": x["s"]} for x in items]},
              open(os.path.join(C.OUT_DIR, f"chan_summary_{args.date}.json"), "w"),
              ensure_ascii=False, indent=1)
    with open(os.path.join(C.OUT_DIR, f"chan_summary_{args.date}.txt"), "w") as f:
        f.write(summary)
    logp(f"报告: {out_html}")
    print("\n" + summary)

    # 5.5 发布 HTML 到 GitHub Pages(webhook 卡片需要公网链接; 失败不阻塞主流程)
    pages_url = None
    if C.FEISHU_MODE == "webhook" and not args.no_push:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import publish_report
            if publish_report.main([args.date]) == 0:
                pages_url = publish_report.URL
        except Exception as e:
            logp(f"发布公网失败(忽略,改发纯文本): {e}")
    # 6. 推送
    if not args.no_push and C.FEISHU_MODE != "none":
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from push_feishu import push_text, push_file, push_card
            if C.FEISHU_MODE == "webhook":
                if pages_url:
                    push_card(summary, pages_url, date=args.date,
                              market_chg=(mk or {}).get("chg"))
                else:
                    push_text(summary, mode="webhook")
            else:
                push_text(summary, mode="bot")
                push_file(out_html, mode="bot")
            logp("飞书推送完成")
        except Exception as e:
            logp(f"飞书推送失败: {e}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
