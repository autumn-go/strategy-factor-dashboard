# -*- coding: utf-8 -*-
"""缠论扫描核心: czsc生成笔 → 自研中枢/一二三类买点判定。
单票入口 analyze(code, df) ; 批量入口 scan_pool(codes)。
"""
import os
import numpy as np
import pandas as pd
from czsc import CZSC, RawBar, Freq
from czsc.enum import Direction

try:
    from core import config as C
except ImportError:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core import config as C

def df_to_czsc(df, symbol):
    bars = []
    for i, row in df.iterrows():
        bars.append(RawBar(symbol=symbol, id=i, dt=pd.Timestamp(row["date"]).to_pydatetime(),
                           freq=Freq.D, open=float(row["open"]), close=float(row["close"]),
                           high=float(row["high"]), low=float(row["low"]),
                           vol=float(row["volume"]), amount=0.0))
    return CZSC(bars)

def bis_snapshot(c):
    bis = []
    for b in c.bi_list:
        bis.append({"dir": 1 if b.direction == Direction.Up else -1,
                    "sdt": str(b.fx_a.dt)[:10], "edt": str(b.fx_b.dt)[:10],
                    "high": round(b.high, 3), "low": round(b.low, 3),
                    "sa": round(b.fx_a.fx, 3), "ea": round(b.fx_b.fx, 3),
                    "power": round(b.power_price, 3), "chg": round(b.change, 4)})
    return bis

def find_zs(bis):
    """最近中枢: >=3笔重叠[zd,zg],延伸重叠笔, 要求近端成形。"""
    n = len(bis)
    if n < 6:
        return None
    lo = max(n - 12, 0)
    for i in range(n - 3, lo - 1, -1):
        seg = bis[i:i + 3]
        zg = min(x["high"] for x in seg)
        zd = max(x["low"] for x in seg)
        if zg <= zd:
            continue
        j = i + 3
        while j < n and bis[j]["low"] <= zg and bis[j]["high"] >= zd:
            j += 1
        if j - 1 >= n - C.ZS_NEAR:
            return {"zd": round(zd, 3), "zg": round(zg, 3),
                    "start": i, "end": j - 1, "n": j - i}
    return None

def analyze(df, code, params=None):
    """返回结构 dict。params 可覆盖判据阈值。"""
    p = {"BUY1_DECAY": C.BUY1_POWER_DECAY, "BUY2_UP": C.BUY2_UP_MIN,
         "BUY2_RETR": C.BUY2_RETR_MAX, "BUY3_LEAVE": C.BUY3_LEAVE,
         "BUY3_FAR": C.BUY3_FAR, "FRESH": C.FRESH_DAYS}
    if params:
        p.update(params)
    c = df_to_czsc(df, code)
    bis = bis_snapshot(c)
    dates = df["date"].tolist()
    closes = df["close"].values
    vols = df["volume"].values
    n = len(bis)
    out = {"code": code, "bars": len(df), "n_bis": n,
           "last_date": dates[-1], "close": round(float(closes[-1]), 3),
           "chg1d": round((float(closes[-1]) / float(closes[-2]) - 1) * 100, 2) if len(closes) > 1 else None,
           "amt20": round(float(np.mean(vols[-20:] * closes[-20:] * 100)) / 1e8, 2),
           "date_pos": {d: i for i, d in enumerate(dates)}}
    if n < 7 or out["amt20"] < C.MIN_AMT20:
        return out
    out["bis"] = bis[-14:]
    zs = find_zs(bis)
    if zs is None or zs["end"] < n - C.ZS_NEAR:
        return out
    out["zs"] = zs
    zg, zd, end_bi = zs["zg"], zs["zd"], zs["end"]
    sigs = []

    # ---- 三买 ----
    if end_bi <= n - 3:
        u, d = bis[end_bi + 1], bis[end_bi + 2]
        if (u["dir"] == 1 and d["dir"] == -1 and u["high"] > zg * p["BUY3_LEAVE"]
                and zg < d["low"] <= zg * p["BUY3_FAR"]):
            sig = {"type": "BUY3", "confirmed": (end_bi + 2 < n - 1),
                   "back_low": d["low"], "zg": zg, "up_high": u["high"],
                   "trigger_date": d["edt"], "dist_zg": round((d["low"] - zg) / zg * 100, 2)}
            if end_bi + 3 < n and bis[end_bi + 3]["dir"] == 1:
                sig["rev_up"] = True
                sig["confirmed"] = True
            sigs.append(sig)
    elif end_bi == n - 2:
        u = bis[end_bi + 1]
        if u["dir"] == 1 and u["high"] > zg * p["BUY3_LEAVE"] and float(closes[-1]) > zg:
            sigs.append({"type": "BUY3", "confirmed": False, "back_low": None, "zg": zg,
                         "up_high": u["high"], "trigger_date": u["edt"],
                         "note": "突破中枢上沿,回抽不破zg即三买"})

    # ---- 一买 ----
    for i in (n - 1, n - 2):
        cur = bis[i]
        if cur["dir"] != -1 or i < 3:
            continue
        for j in range(i - 2, max(i - 8, 0), -1):
            prev = bis[j]
            if prev["dir"] != -1:
                continue
            if not any(bis[k]["dir"] == 1 for k in range(j + 1, i)):
                continue
            if cur["low"] < prev["low"] - 1e-9 and cur["power"] < prev["power"] * p["BUY1_DECAY"]:
                sigs.append({"type": "BUY1", "confirmed": (i < n - 1 and bis[i + 1]["dir"] == 1),
                             "low": cur["low"], "prev_low": prev["low"],
                             "trigger_date": cur["edt"],
                             "chg_ratio": round(cur["power"] / prev["power"], 3)})
            break
        if sigs and sigs[-1]["type"] == "BUY1":
            break

    # ---- 二买 ----
    for i in (n - 1, n - 2):
        if i < 3:
            break
        d2 = bis[i]
        if d2["dir"] != -1 or bis[i - 1]["dir"] != 1:
            continue
        u1 = bis[i - 1]
        d0_idx = None
        for k in range(i - 2, max(i - 8, 0), -1):
            if bis[k]["dir"] == -1:
                d0_idx = k
                break
        if d0_idx is None:
            continue
        d0 = bis[d0_idx]
        L0 = d0["low"]
        recent10 = bis[max(n - 10, 0):]
        if L0 > min(x["low"] for x in recent10) + 1e-9:
            continue
        up_chg = (u1["high"] - L0) / L0
        if up_chg < p["BUY2_UP"]:
            continue
        if d2["low"] <= L0:
            continue
        retr = (u1["high"] - d2["low"]) / (u1["high"] - L0)
        if retr > p["BUY2_RETR"]:
            continue
        if i == n - 1 and float(closes[-1]) < d2["low"]:
            continue
        confirmed = (i < n - 1) and (bis[i + 1]["dir"] == 1)
        sigs.append({"type": "BUY2", "confirmed": confirmed, "low": d2["low"],
                     "L0": round(L0, 3), "up_high": round(u1["high"], 3),
                     "retr": round(retr, 2), "up_chg": round(up_chg * 100, 1),
                     "trigger_date": d2["edt"], "dist_L0": round((d2["low"] - L0) / L0 * 100, 2)})
        break

    def days_back(tdate):
        if tdate not in out["date_pos"]:
            return 99
        return len(dates) - 1 - out["date_pos"][tdate]
    fw = p["FRESH"]
    out["sigs"] = [s for s in sigs if days_back(s["trigger_date"]) <= fw.get(s["type"], 6)]
    for s in out["sigs"]:
        s["fresh_days"] = days_back(s["trigger_date"])
        buy = s.get("back_low") or s.get("low") or s.get("L0")
        s["pct_from_buy"] = round((out["close"] - buy) / buy * 100, 2) if buy else None
        s["buy_price"] = buy
    return out

# ---------- 分级与排序 ----------
TYPE_BASE = {"BUY1": 40, "BUY2": 50, "BUY3": 60}

def sig_score(s):
    base = TYPE_BASE[s["type"]]
    if s["confirmed"]:
        base += 20
    if s["type"] == "BUY3" and s.get("rev_up"):
        base += 10
    base -= (s.get("fresh_days") or 9) * 1.5
    off = abs(s.get("pct_from_buy") or 99)
    base += 8 if off <= 3 else (3 if off <= 6 else (-4 if off <= 12 else -12))
    return base

def best_signal(r):
    if not r.get("sigs"):
        return None
    return max(r["sigs"], key=sig_score)

def grade_of(s):
    off = s.get("pct_from_buy")
    if off is None or off < -2:
        return "C"
    if off <= 5:
        if s["type"] == "BUY2" or s["confirmed"]:
            return "A"
        return "B"
    if off <= 8:
        return "B"
    return "C"

def stop_of(s):
    if s["type"] in ("BUY1", "BUY2"):
        lo = min(s.get("L0") or 9e9, s.get("low") or 9e9)
        return round(lo * 0.99, 2)
    if s["type"] == "BUY3":
        return round(s["zg"] * 0.99, 2)
    return None
