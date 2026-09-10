# -*- coding: utf-8 -*-
"""HTML 报告 + 摘要文本生成(K线缠论结构图 base64 内嵌,自包含)。"""
import os, io, base64, logging
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
# 中文字体: 优先 macOS PingFang SC(避免 SimHei 缺失刷屏), 并静默字体查找告警
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Hiragino Sans GB", "Heiti SC",
                                   "STHeiti", "Arial Unicode MS", "Songti SC"]
plt.rcParams["axes.unicode_minus"] = False

from core.chan_scan import df_to_czsc, bis_snapshot

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

def plot_png_base64(code, lookback=120):
    """绘制近 lookback 根日K + 笔结构, 返回 base64 png; 失败返回 None"""
    path = os.path.join(DATA_DIR, code + ".csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    dfa = df.tail(lookback).reset_index(drop=True)
    c = df_to_czsc(df, code)
    bis = bis_snapshot(c)
    fig, ax = plt.subplots(figsize=(9.2, 5), dpi=96)
    for i, row in dfa.iterrows():
        o, h, l, cl = row["open"], row["high"], row["low"], row["close"]
        color = "#d32f2f" if cl >= o else "#2e7d32"
        ax.plot([i, i], [l, h], color=color, lw=0.6, zorder=2)
        ax.add_patch(mpatches.Rectangle((i - 0.3, min(o, cl)), 0.6, max(abs(cl - o), 0.01),
                                        facecolor=color, edgecolor=color, zorder=3))
    dmap = {d: i for i, d in enumerate(dfa["date"])}
    for bi in bis:
        x0, x1 = dmap.get(bi["sdt"][:10]), dmap.get(bi["edt"][:10])
        if x0 is None or x1 is None:
            continue
        ax.plot([x0, x1], [bi["sa"], bi["ea"]], color="#1a237e", lw=1.4, zorder=4)
    ax.set_title(f"{code}  close={df['close'].values[-1]}  {df['date'].values[-1]}", fontsize=10)
    ax.grid(alpha=0.2)
    ax.margins(x=0.01)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()

def esc(x):
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def sig_desc(s):
    t = s["type"]
    if t == "BUY3":
        if s.get("note"):
            return f"突破中枢上沿[{s['zg']}],现价在其上方;回抽不破 {s['zg']} 即构成标准三买"
        return (f"向上离开中枢(上沿{s['zg']})后回抽至 {s['back_low']},未回中枢,三买成立"
                + (";且已重新上攻" if s.get("rev_up") else ""))
    if t == "BUY2":
        return (f"自显著低点 {s['L0']} 反弹 {s.get('up_chg')}% 至 {s.get('up_high')},"
                f"回撤 {int(s.get('retr', 0) * 100)}% 至 {s['low']} 不破前低,右侧二买")
    if t == "BUY1":
        extra = f",力度为前段的 {int((s.get('chg_ratio') or 1) * 100)}%" if s.get("chg_ratio") else ""
        star = "已现底分型反转" if s["confirmed"] else "尚在下跌笔末端(未确认反转)"
        return f"创新低至 {s['low']}{extra},{star} —— 底背驰一买(左侧)"
    return ""

BADGE = {"BUY1": ("一买·左侧", "#e65100", "#fff3e0"),
         "BUY2": ("二买·右侧", "#1565c0", "#e3f2fd"),
         "BUY3": ("三买·突破", "#2e7d32", "#e8f5e9")}

def card_html(item, with_img=True):
    r, s = item["r"], item["s"]
    name = esc(r.get("name") or "")
    badge = BADGE[s["type"]]
    conf = "已确认" if s["confirmed"] else "形成中"
    img = ""
    if with_img:
        b64 = plot_png_base64(r["code"])
        if b64:
            img = f'<img src="data:image/png;base64,{b64}" style="width:100%;border-radius:6px;margin-top:8px"/>'
    chg = r.get("chg1d")
    chg_txt = f'{chg:+.2f}%' if chg is not None else "-"
    kv = "".join(
        f'<div class="kv"><span>{k}</span><b>{v}</b></div>' for k, v in [
            ("现价", f'{r["close"]}'), ("当日涨跌", chg_txt),
            ("买点参考", f'{item["buy"]}'), ("距买点", f'{item["off"]}%'),
            ("止损参考", f'{item["stop"] or "-"}'),
            ("触发日", f'{s["trigger_date"]} ({s.get("fresh_days")}日前)')])
    return (f'<div class="card"><div class="chead"><b class="cname">{name} <span class="code">{r["code"]}</span></b>'
            f'<span class="badge" style="background:{badge[2]};color:{badge[1]};border:1px solid {badge[1]}33">{badge[0]}·{conf}</span></div>'
            f'<div class="desc">{esc(sig_desc(s))}</div><div class="kvgrid">{kv}</div>{img}</div>')

def row_html(item):
    r, s = item["r"], item["s"]
    name = esc(r.get("name") or "")
    chg = r.get("chg1d")
    chg_txt = f'{chg:+.2f}%' if chg is not None else "-"
    return (f'<tr><td><b>{name}</b><span class="code">{r["code"]}</span></td>'
            f'<td>{s["type"]}{"*" if s["confirmed"] else "~"}</td>'
            f'<td>{r["close"]} <span style="color:{"#c62828" if (chg or 0) >= 0 else "#2e7d32"}">{chg_txt}</span></td>'
            f'<td>{item["buy"]}</td><td>{item["off"]}%</td><td>{item["stop"] or "-"}</td>'
            f'<td>{s["trigger_date"]}</td></tr>')

def build(rows, meta, out_html, max_chart_a=16):
    """rows: item dicts 已按分级+分排序; meta: {asof, pool_n, market...}"""
    a = [x for x in rows if x["g"] == "A"]
    b = [x for x in rows if x["g"] == "B"]
    c = [x for x in rows if x["g"] == "C"]
    n3 = sum(1 for x in rows for s in [x["s"]] if s["type"] == "BUY3")
    n1 = sum(1 for x in rows for s in [x["s"]] if s["type"] == "BUY1")
    n2 = sum(1 for x in rows for s in [x["s"]] if s["type"] == "BUY2")
    a_cards = "".join(card_html(x, with_img=(i < max_chart_a)) for i, x in enumerate(a))
    b_rows = "".join(row_html(x) for x in b)
    c_rows = "".join(row_html(x) for x in c)
    mk = meta.get("market")
    mkt_line = ""
    if mk:
        mkt_line = (f'<div class="stat"><b>{mk["close"]}</b><span>上证指数 {mk["date"]} '
                    f'({mk["chg"]:+.2f}%)</span></div>')
    html = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>缠论三类买点盘后扫描 · {meta["asof"]}</title>
<style>
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1c2833;margin:0}}
.wrap{{max-width:1080px;margin:0 auto;padding:28px 20px 60px}}
h1{{font-size:23px;margin:0 0 4px}}.sub{{color:#7b8794;font-size:13px;margin-bottom:18px}}
.panel{{background:#fff;border-radius:12px;padding:22px 24px;margin-bottom:20px;box-shadow:0 1px 4px rgba(20,40,80,.06)}}
.sum{{display:flex;gap:14px;flex-wrap:wrap;margin:14px 0}}
.stat{{flex:1;min-width:140px;background:#f8fafc;border:1px solid #e8edf3;border-radius:10px;padding:12px 16px}}
.stat b{{font-size:22px;display:block}}.stat span{{color:#7b8794;font-size:12px}}
h2{{font-size:17px;border-left:4px solid #1565c0;padding-left:10px;margin:6px 0 14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}}
.card{{background:#fff;border:1px solid #e8edf3;border-radius:12px;padding:14px 16px}}
.chead{{display:flex;justify-content:space-between;align-items:center;gap:8px}}
.cname{{font-size:15px}}.code{{color:#90a4ae;font-size:11px;font-weight:400;margin-left:4px}}
.badge{{font-size:11px;padding:3px 8px;border-radius:20px;white-space:nowrap}}
.desc{{font-size:12.5px;color:#455a64;line-height:1.6;margin:8px 0}}
.kvgrid{{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;font-size:12px}}
.kv{{display:flex;justify-content:space-between;border-bottom:1px dashed #eceff1;padding:3px 0}}
.kv span{{color:#90a4ae}}.kv b{{font-weight:600}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th,td{{padding:7px 10px;border-bottom:1px solid #eceff1;text-align:left}}
th{{background:#f8fafc;color:#546e7a;font-weight:600}}
.concl{{font-size:13px;color:#37474f;line-height:1.8}}
.method{{font-size:12px;color:#546e7a;line-height:1.7}}
.disclaim{{font-size:12px;color:#90a4ae;line-height:1.7;border-top:1px solid #eceff1;padding-top:14px;margin-top:24px}}
</style></head><body><div class="wrap">
<h1>缠论三类买点 · 盘后扫描</h1>
<div class="sub">数据: 腾讯行情日K(前复权) · {meta["asof"]} 收盘 · 股票池 {meta["pool_n"]}只(沪深300+中证500+中证1000) · 生成 {meta["gen_time"]}</div>
<div class="panel"><h2>核心结论</h2>
<div class="sum">
{mkt_line}
<div class="stat"><b>{len(rows)}</b><span>命中买点结构</span></div>
<div class="stat"><b style="color:#2e7d32">{len(a)}</b><span>A级·贴近买点</span></div>
<div class="stat"><b style="color:#1565c0">{len(b)}</b><span>B级·待确认</span></div>
<div class="stat"><b style="color:#e65100">{n3}</b><span>三买结构</span></div>
</div>
<p class="concl">{meta.get("conclusion", "")}</p></div>
<div class="panel"><h2>A级 · 买点已现且现价贴近 ({len(a)})</h2><div class="grid">{a_cards}</div></div>
<div class="panel"><h2>B级 · 结构形成中/略高 ({len(b)})</h2>
<table><tr><th>标的</th><th>类型</th><th>收盘(涨跌)</th><th>买点</th><th>距买点</th><th>止损</th><th>触发日</th></tr>{b_rows}</table></div>
<div class="panel"><h2>C级 · 已远离买点不追 ({len(c)})</h2>
<table><tr><th>标的</th><th>类型</th><th>收盘(涨跌)</th><th>买点</th><th>距买点</th><th>止损</th><th>触发日</th></tr>{c_rows}</table></div>
<div class="panel"><h2>方法与口径</h2>
<p class="method">笔/分型=czsc 0.8.30 社区实现; 中枢/买卖点=自研规则(一买:创新低+力度衰减≥8%; 二买:近10笔低点→反弹≥8%→回撤≤75%不破低; 三买:离开中枢≥1%后回抽不回上沿)。触发日在5-6交易日内; 近20日日均额≥1亿; 前复权口径。缠论为主观结构分析,买点为概率信号非确定性,须带止损。</p></div>
<div class="disclaim">免责声明: 以上内容基于公开数据和量化分析,仅供参考,不构成投资建议。市场有风险,投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断,必要时咨询持牌专业机构。过往表现不预示未来收益。</div>
</div></body></html>'''
    with open(out_html, "w") as f:
        f.write(html)
    return out_html

def build_summary_txt(rows, meta):
    """生成推送用纯文本摘要(飞书/终端)。"""
    a = [x for x in rows if x["g"] == "A"]
    b = [x for x in rows if x["g"] == "B"]
    n3 = sum(1 for x in rows for s in [x["s"]] if s["type"] == "BUY3")
    mk = meta.get("market")
    mk_txt = f"上证指数 {mk['close']} ({mk['chg']:+.2f}%)" if mk else "大盘数据缺失"
    lines = [f"【缠论三类买点 · 盘后扫描】{meta['asof']}",
             f"{mk_txt} ｜ 股票池 {meta['pool_n']} 只",
             f"命中 {len(rows)} ｜ A级(贴近买点) {len(a)} ｜ B级(待确认) {len(b)} ｜ 三买 {n3}"]
    if n3 == 0:
        lines.append("三买=0: 无趋势中继结构,市场处弱转强/震荡期,关注右侧二买回踩")
    if a:
        lines.append("\n— A级重点(距买点≤5%) —")
        for x in a[:10]:
            s = x["s"]
            t = {"BUY1": "一买", "BUY2": "二买", "BUY3": "三买"}[s["type"]]
            star = "*" if s["confirmed"] else "~"
            stop = x["stop"] or "-"
            lines.append(f"{x['r'].get('name','')}({x['r']['code']}) {t}{star} 现价{x['r']['close']} 买点{x['buy']} 距买点{x['off']}% 止损{stop}")
    else:
        lines.append("\n今日无A级贴近买点,宜观望")
    lines.append("\n完整报告含K线结构图,见飞书文件/输出目录。")
    lines.append("免责声明: 仅供参考,不构成投资建议。")
    return "\n".join(lines)
