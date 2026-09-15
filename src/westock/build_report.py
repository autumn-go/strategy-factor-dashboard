# -*- coding: utf-8 -*-
"""把 latest_signals.json 渲染成精美 HTML 汇报页（浅底深字研报风）"""
import os
import json
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'output')
SRC = os.path.join(OUT, 'latest_signals.json')


def fmt_num(v, nd=2):
    try:
        if v is None or v == '':
            return '-'
        return f'{float(v):,.{nd}f}'
    except Exception:
        return str(v)


def load():
    with open(SRC, encoding='utf-8') as f:
        return json.load(f)


def load_backfill():
    """读取回测指标 + 净值序列（策略 / 全板块等权 / 上证指数三条）

    同时加载 BOCI 当日情绪 + ETF 清单
    """
    import pickle
    stats, navs = {}, {'strategy': [], 'bench_sector': [], 'index': []}
    boci, etf = {}, {}
    opp_items = []
    p = os.path.join(HERE, 'output', 'backfill_stats.json')
    if os.path.exists(p):
        with open(p, encoding='utf-8') as f:
            stats = json.load(f)
    pk = os.path.join(HERE, 'cache', 'factors.pkl')
    d0 = None
    if os.path.exists(pk):
        try:
            with open(pk, 'rb') as f:
                d = pickle.load(f)
            bt = d.get('backtest')
            if bt is not None and len(bt):
                navs['strategy'] = [[str(x), round(float(y), 4)]
                                    for x, y in zip(bt.trade_date, bt.nav)]
                d0 = str(bt.trade_date.iloc[0])
            # 基准1: 全板块等权累计净值
            sd = d.get('sector_daily')
            if sd is not None and d0:
                s2 = sd[sd.trade_date.astype(str) >= d0]
                m = s2.groupby('trade_date').pct_chg.mean().sort_index()
                cum = (1 + m / 100).cumprod()
                navs['bench_sector'] = [[str(k), round(float(v), 4)]
                                        for k, v in cum.items()]
                stats['benchmark_sector'] = round(
                    (float(cum.iloc[-1]) - 1) * 100, 2)
            # BOCI 当日
            boci = d.get('boci_latest') or {}
            # ETF 清单
            etf = d.get('etf_watchlist') or {}
            # BOCI 机会池
            opp_items = d.get('boci_opportunities') or []
        except Exception as e:
            stats['benchmark_err'] = f'{type(e).__name__}: {e}'
    # 基准2: 上证指数（归一化净值）
    try:
        from westock_source import get_index_daily
        ix = get_index_daily('000001.SH', n=800, refresh=False)
        if len(ix) and d0:
            ix = ix[ix.trade_date.astype(str) >= d0].sort_values('trade_date')
            if len(ix):
                base = float(ix.close.iloc[0])
                navs['index'] = [[str(t), round(float(c) / base, 4)]
                                 for t, c in zip(ix.trade_date, ix.close)]
                stats['benchmark_index'] = round(
                    (float(ix.close.iloc[-1]) / base - 1) * 100, 2)
    except Exception:
        pass
    return stats, navs, boci, etf, opp_items


def render_etf_table(items):
    """把 ETF 清单 items 渲染成 HTML 表格（按板块分组，成交额降序）"""
    if not items:
        return "<div class='err'>无 ETF 映射结果（板块当日可能无对应场内 ETF）</div>"
    # 按板块分组
    by_sec, order = {}, []
    for r in items:
        sec = r.get('sector', '?')
        if sec not in by_sec:
            by_sec[sec] = []
            order.append(sec)
        by_sec[sec].append(r)
    rows = ''
    for sec in order:
        for i, r in enumerate(by_sec[sec]):
            sec_cell = (f"<td class='nm'>{sec}</td>" if i == 0
                        else f"<td class='nm' style='color:var(--tx3);font-size:12px'>↳</td>")
            amt = r.get('amount') or 0
            rows += (
                f"<tr>{sec_cell}"
                f"<td class='cd'>{r.get('code','')}</td>"
                f"<td>{r.get('fund_name','')}</td>"
                f"<td class='num'>{fmt_num(r.get('close'), 3)}</td>"
                f"<td class='num'>{amt/1e8:.2f}亿</td></tr>"
            )
    return f"""
    <table>
      <thead><tr>
        <th style='width:130px'>信号板块</th>
        <th style='width:90px'>代码</th>
        <th>ETF 名称</th>
        <th class='num' style='width:80px'>现价</th>
        <th class='num' style='width:90px'>成交额</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def render_boci_section(boci):
    """BOCI 五维行业情绪详细区：Top 列表 + 五维小条形"""
    if not boci or boci.get('error') or not boci.get('top'):
        return ''
    top_rows = ''
    tradeable = []
    for i, r in enumerate(boci.get('top', []), 1):
        n = r.get('name', '')
        sc = r.get('score', 0)
        sn = r.get('sentiment', 0)
        oh = r.get('overheat', False)
        if not oh:
            tradeable.append(n)
        # 五维小条形
        f1, f2, f3, f4, f5 = (r.get('f1', 0), r.get('f2', 0), r.get('f3', 0),
                              r.get('f4', 0), r.get('f5', 0))
        bars = ''.join([
            f"<span class='boci-bar' style='width:{max(2,int(v*100))}px;"
            f"background:{['#5f5e5a','#888780','#b7791f','#185fa5','#c0392b'][j]}'>"
            f"</span>" for j, v in enumerate([f1, f2, f3, f4, f5])
        ])
        tag = ("<span class='oh'>过热</span>" if oh
               else "<span class='ok'>可做</span>")
        top_rows += (
            f"<tr><td class='rk'>{i}</td><td class='nm'>{n}</td>"
            f"<td class='cd'>{r.get('sector','')}</td>"
            f"<td class='num'>{sc:.3f}</td>"
            f"<td class='num'>{sn:.3f}</td>"
            f"<td>{bars}</td><td>{tag}</td></tr>"
        )
    trade_str = ('、'.join(tradeable[:5]) if tradeable else '（全部过热，谨慎）')
    return f"""
    <div class='note' style='font-size:12px;margin:8px 0 12px'>
      共 <b>{boci.get('n_industry',0)}</b> 个行业参评 · 过热剔除
      <b>{boci.get('n_overheat',0)}</b> 个 · 当日可做：
      <b>{trade_str}</b><br>
      五维含义：F1 多头占比 · F2 RSI 强度 · F3 换手强度 · F4 涨停情绪差 · F5 成交额占比；
      score 为截面排序分（百分位），sentiment 为五维等权均值。
    </div>
    <table>
      <thead><tr>
        <th>#</th><th>行业</th><th>代码</th>
        <th class='num'>Score</th><th class='num'>Sentiment</th>
        <th>F1-F5</th><th>状态</th>
      </tr></thead>
      <tbody>{top_rows}</tbody>
    </table>"""


def render_opportunity_pool(opp_items):
    """渲染当日机会池（超买/超卖各 Top 5）

    opp_items 来自 factors.pkl['boci_opportunities']
    """
    if not opp_items:
        return ''
    overbought = sorted([r for r in opp_items if r.get('tag') == 'overbought'],
                        key=lambda r: -(r.get('pct_60') or 0))[:5]
    oversold = sorted([r for r in opp_items if r.get('tag') == 'oversold'],
                      key=lambda r: r.get('pct_60') or 0)[:5]

    def row(r, kind):
        cls = 'os' if kind == 'os' else 'oh'
        d5 = r.get('d5')
        d5s = f'{d5:+.3f}' if d5 is not None else '-'
        return (
            f"<tr class='{cls}'>"
            f"<td class='nm'>{r.get('name','')}</td>"
            f"<td class='cd'>{r.get('sector','')}</td>"
            f"<td class='num'>{r.get('sentiment',0):.3f}</td>"
            f"<td class='num'>{(r.get('pct_60') or 0)*100:.0f}%</td>"
            f"<td class='num'>{d5s}</td>"
            f"<td class='num'>{(r.get('pct_chg5') or 0):+.2f}%</td>"
            f"</tr>"
        )

    os_rows = ''.join(row(r, 'os') for r in oversold) or "<tr><td colspan='6'>无</td></tr>"
    oh_rows = ''.join(row(r, 'oh') for r in overbought) or "<tr><td colspan='6'>无</td></tr>"

    return f"""
    <div class='note' style='font-size:12px;margin:8px 0 12px'>
      <b>判定规则</b>：① 当前 sentiment 在过去 60 个交易日的百分位 &le; 15%
      （情绪跌到近 60 日底部 = 超跌机会） ② sentiment 绝对值 &lt; 0.30 同样算超跌；
      ③ 超买反向同理（pct60 &ge; 85% 或 sentiment &gt; 0.70）。
      ④ 点击下方任一行业可切换至历史走势交互页。
    </div>
    <div class='opp'>
      <div class='opp-col'>
        <h3 class='oh-h'>超跌机会（绿色 · 可能反转）</h3>
        <table>
          <thead><tr><th>行业</th><th>代码</th>
            <th class='num'>sentiment</th><th class='num'>60日百分位</th>
            <th class='num'>5日Δ</th><th class='num'>5日涨幅</th></tr></thead>
          <tbody>{os_rows}</tbody>
        </table>
      </div>
      <div class='opp-col'>
        <h3 class='oh-h'>超买风险（红色 · 谨慎追高）</h3>
        <table>
          <thead><tr><th>行业</th><th>代码</th>
            <th class='num'>sentiment</th><th class='num'>60日百分位</th>
            <th class='num'>5日Δ</th><th class='num'>5日涨幅</th></tr></thead>
          <tbody>{oh_rows}</tbody>
        </table>
      </div>
    </div>
    <div style='margin-top:8px;font-size:12px'>
      → <a href='boci_industries.html' target='_blank'>查看完整 BOCI 行业情绪历史交互页</a>
      　|　可下拉切换任意行业、调整窗口、对比 sentiment 与 K 线走势
    </div>"""


def build_html(d, bt_stats=None, navs=None, boci=None, etf=None,
               opp_items=None):
    td = d.get('trade_date', '')
    gen = d.get('gen_time', '')
    ews = d.get('ews', {}) or {}
    lc = d.get('lc', {}) or {}

    # 行情滞后告警横幅（见 run_latest.py 的"防呆对时"）
    data_lag = d.get('data_lag') or None
    stale_banner = ''
    if data_lag:
        stale_banner = (
            "<div style='background:#fff1f0;border:1px solid #ffa39e;"
            "border-left:5px solid #cf1322;border-radius:6px;"
            "padding:10px 14px;margin:10px 0;color:#a8071a;font-size:13px'>"
            "<b>⚠ 行情数据滞后告警</b>：本报告策略交易日 "
            f"<b>{data_lag.get('strategy_date', '?')}</b>，"
            f"但市场最新交易日为 <b>{data_lag.get('market_date', '?')}</b>。"
            "结果可能已陈旧，请检查数据刷新链路（refresh_universe）。</div>")

    # 三条净值曲线按策略交易日对齐（缺失填 null，前端 connectNulls）
    navs = navs or {}
    nav_lines, nav_dates = {}, []
    if navs.get('strategy'):
        nav_dates = [x[0] for x in navs['strategy']]
        for k in ('strategy', 'bench_sector', 'index'):
            m = dict(navs.get(k) or [])
            nav_lines[k] = [[dd, m.get(dd)] for dd in nav_dates]

    # ---- EW-SDM 榜单 ----
    rows = ews.get('top', []) or []
    sc = ews.get('score_col', 'score')
    trs = ''
    names, vals = [], []
    # 因子明细列（除基础列外，全部按数值右对齐展示）
    base = {'ts_code', 'name', 'type'}
    extra = [c for c in (rows[0].keys() if rows else [])
             if c not in base] if rows else []
    for i, r in enumerate(rows, 1):
        nm = r.get('name', r.get('ts_code', ''))
        val = r.get(sc, 0)
        try:
            val = float(val)
        except Exception:
            val = 0
        names.append(nm)
        vals.append(round(val, 4))
        tds = (f"<td class='rk'>{i}</td><td class='nm'>{nm}</td>"
               f"<td class='cd'>{r.get('ts_code','')}</td>")
        for c in extra:
            v = r.get(c, '-')
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                tds += f"<td class='num'>{fmt_num(v, 3)}</td>"
            else:
                tds += f"<td class='num'>{v}</td>"
        trs += f"<tr>{tds}</tr>"
    head_extra = ''.join(f"<th class='num'>{c}</th>" for c in extra)

    # ---- LC 状态 ----
    lc_card = ''
    if lc.get('error'):
        lc_card = f"<div class='err'>LC-KNN 计算失败：{lc['error']}</div>"
    elif lc:
        pred = lc.get('prediction')
        try:
            pred = f'{float(pred):.3f}'
        except Exception:
            pred = str(pred)
        pcls = 'up' if str(pred).replace('-', '').replace('.', '').isdigit() \
            and float(lc.get('prediction', 0)) > 0 else 'down'
        lc_card = f"""
        <div class='metric {pcls}'>
          <div class='mlabel'>LC-KNN 洛伦兹分类 · 小盘指数</div>
          <div class='mval'>{pred}</div>
          <div class='msub'>预测值（&gt;0 看多）· 收盘 {fmt_num(lc.get('close'))}</div>
        </div>"""

    # ---- BOCI 卡片 ----
    boci_card = ''
    if boci and not boci.get('error') and boci.get('n_industry'):
        top3 = boci.get('top3') or []
        top3_str = '、'.join(f"{r.get('name','')}" for r in top3[:3])
        n_oh = boci.get('n_overheat', 0)
        boci_card = f"""
        <div class='metric'>
          <div class='mlabel'>BOCI 五维行业情绪</div>
          <div class='mval'>{boci.get('n_industry', 0)} 行业</div>
          <div class='msub'>过热剔除 {n_oh} 个 · Top3：{top3_str}</div>
        </div>"""

    ews_err = ''
    if ews.get('error'):
        ews_err = f"<div class='err'>EW-SDM 计算失败：{ews['error']}</div>"

    # ---- 回测量化结果 ----
    bt_block = ''
    st = dict((bt_stats or {}).get('stats') or {})
    # 基准指标存在外层，合并进来便于模板取值
    st['benchmark_sector'] = (bt_stats or {}).get('benchmark_sector')
    st['benchmark_index'] = (bt_stats or {}).get('benchmark_index')
    if st:
        def cls(v):
            try:
                return 'up' if float(v) > 0 else 'down'
            except Exception:
                return 'flat'

        cards = ''.join([
            f"<div class='metric {cls(st.get(k))}'><div class='mlabel'>{lb}</div>"
            f"<div class='mval'>{st.get(k, '-')}{u}</div></div>"
            for k, lb, u in [
                ('total_return', '区间总收益', '%'),
                ('annual_return', '年化收益', '%'),
                ('max_drawdown', '最大回撤', '%'),
                ('win_rate', '日胜率', '%'),
                ('sharpe', '夏普比率', '')]])
        bt_block = f"""
  <h2>回测量化结果 · EW-SDM Top10 等权</h2>
  <div class="cards">{cards}</div>
  <div style="font-size:12px;color:var(--tx3);margin:-4px 0 10px">
    回测区间 {st.get('days','-')} 个交易日 · 期末净值 {st.get('final_nav','-')}
    · <b>基准对比</b>：全板块等权 {st.get('benchmark_sector','-')}%
    · 上证指数 {st.get('benchmark_index','-')}%
    · 超额 {round(st.get('total_return',0) - (st.get('benchmark_sector') or 0), 2)} 个百分点
  </div>
  <div class="note" style="margin-bottom:10px;font-size:12px">
    <b>结果偏乐观，务必打折看</b>：① 同花顺板块成分股是静态快照（含前视偏差）；
    ② 板块日收益用成分股等权聚合，非官方市值加权，小盘股权重被放大；
    ③ 未计交易成本与冲击成本；④ 回测区间 2025-01 起，小盘题材风格占优。
  </div>
  <div id="navchart" class="chart" style="height:340px"></div>"""

    # 在 HTML 模板中替换两个占位符
    html_body = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>策略因子平台 · 最新信号 {td}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js"></script>
<style>
:root{{--bd:#e5e3dc;--tx:#2c2c2a;--tx2:#5f5e5a;--tx3:#888780;--bg2:#f7f6f2;
--up:#c0392b;--down:#1d9e75;--amb:#b7791f;}}
*{{box-sizing:border-box}}
body{{margin:0;background:#faf9f6;color:var(--tx);
font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.7}}
.wrap{{max-width:1080px;margin:0 auto;padding:32px 24px 64px}}
h1{{font-size:24px;font-weight:600;margin:0 0 6px}}
h2{{font-size:17px;font-weight:600;margin:36px 0 12px;padding-left:10px;
border-left:3px solid var(--tx)}}
.meta{{color:var(--tx2);font-size:13px;margin-bottom:4px}}
.badge{{display:inline-block;background:#e1f5ee;color:#0f6e56;font-size:12px;
padding:2px 9px;border-radius:4px;margin-left:6px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
gap:12px;margin:20px 0}}
.metric{{background:var(--bg2);border-radius:10px;padding:16px 18px}}
.mlabel{{font-size:12px;color:var(--tx2)}}
.mval{{font-size:26px;font-weight:600;margin:4px 0}}
.msub{{font-size:12px;color:var(--tx3)}}
.metric.up .mval{{color:var(--up)}}
.metric.down .mval{{color:var(--down)}}
.metric.flat .mval{{color:var(--amb)}}
table{{width:100%;border-collapse:collapse;font-size:13px;background:#fff;
border-radius:8px;overflow:hidden}}
th{{background:var(--bg2);text-align:left;padding:9px 12px;font-weight:600;
font-size:12px;color:var(--tx2)}}
td{{padding:8px 12px;border-top:1px solid var(--bd)}}
tr:hover td{{background:#fcfbf8}}
.rk{{color:var(--tx3);width:38px}}
.cd{{color:var(--tx3);font-size:12px}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.nm{{font-weight:500}}
.chart{{width:100%;height:420px;background:#fff;border-radius:8px;
border:1px solid var(--bd)}}
.note{{background:#fff;border:1px solid var(--bd);border-radius:8px;
padding:14px 18px;font-size:13px;color:var(--tx2)}}
.err{{background:#fcebeb;color:#a32d2d;padding:12px 16px;border-radius:8px;
font-size:13px;margin:10px 0}}
.boci-bar{{display:inline-block;height:6px;border-radius:3px;margin-right:2px;
vertical-align:middle}}
.boci-bar:last-child{{margin-right:8px}}
.oh{{display:inline-block;background:#fcebeb;color:#a32d2d;font-size:11px;
padding:1px 7px;border-radius:4px}}
.ok{{display:inline-block;background:#e1f5ee;color:#0f6e56;font-size:11px;
padding:1px 7px;border-radius:4px}}
.opp{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
@media (max-width:780px){{.opp{{grid-template-columns:1fr}}}}
.oh-h{{font-size:14px;font-weight:600;margin:0 0 8px;padding:4px 10px;
border-left:3px solid;background:var(--bg2);border-radius:4px}}
.opp-col:first-child .oh-h{{color:var(--down);border-color:var(--down)}}
.opp-col:last-child .oh-h{{color:var(--up);border-color:var(--up)}}
tr.os td{{border-bottom:1px solid #e1f5ee}}
tr.oh td{{border-bottom:1px solid #fcebeb}}
.disc{{margin-top:40px;padding-top:16px;border-top:1px solid var(--bd);
font-size:12px;color:var(--tx3)}}
</style>
</head>
<body>
<div class="wrap">
  <h1>策略因子平台 · 最新信号</h1>
  <div class="meta">交易日 <b>{td}</b> · 生成于 {gen}</div>
  <div class="meta">数据源 <b>{d.get('data_source','')}</b>
    <span class="badge">已切换</span></div>
  {stale_banner}

  <h2>核心信号</h2>
  <div class="cards">
    {lc_card}
    {boci_card}
    <div class="metric">
      <div class="mlabel">EW-SDM 覆盖板块</div>
      <div class="mval">{ews.get('total_sectors','-')}</div>
      <div class="msub">涨停 {ews.get('limit_up_count','-')} 家 ·
        炸板 {ews.get('limit_broken_count','-')} 家</div>
    </div>
  </div>
  {ews_err}

  <h2>EW-SDM 情绪加权扩散动量 · Top 15</h2>
  <div id="chart" class="chart"></div>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>#</th><th>板块</th><th>代码</th>{head_extra}</tr></thead>
    <tbody>{trs}</tbody>
  </table>
  </div>

  {bt_block}

  <h2>BOCI 五维行业情绪 · 详细</h2>
  {{__BOCI_DETAIL__}}

  <h2>当日机会池 · 超买 / 超卖 Top 5</h2>
  {{__OPP_POOL__}}

  <h2>每日可买 ETF 清单</h2>
  <div class="note" style="font-size:12px;margin-bottom:8px">
    基于当日 EW-SDM Top 板块 + BOCI Top3 行业 映射到 A 股场内 ETF
    （按板块分组、成交额降序；数据来源 westock 官方通道）
  </div>__ETF_TABLE__

  <h2>数据源切换说明</h2>
  <div class="note">
    原数据源 Tushare Pro 自建代理已停更 3.5 个月（数据停在 2026-05）。本次切换为
    <b>内置金融服务</b>：行情走腾讯自选股（westock 同源）HTTP 接口并发拉取，
    板块与涨跌分布走 westock CLI 补充；<b>板块清单与成分股映射</b>为静态数据，
    继续复用本地库。算法引擎一行未改，仅替换取数函数（换管子不换发动机）。<br><br>
    <b>口径变化</b>：① 涨跌停与连板由日线还原（触及涨停价判定封板/炸板），
    与原 Tushare 涨跌停表存在细微差异；② LC-KNN 标的由中证2000回落为国证2000
    （新数据源无中证2000日线）；③ 行情为前复权。
  </div>

  <div class="disc">
    数据来源：腾讯自选股 / westock（内置金融服务），数据时点 {td}。
    以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。市场有风险，投资需谨慎。
  </div>
</div>
<script>
var names = {json.dumps(names, ensure_ascii=False)};
var vals  = {json.dumps(vals)};
var el = document.getElementById('chart');
if (el && names.length) {{
  var c = echarts.init(el);
  c.setOption({{
    grid: {{left: 130, right: 60, top: 20, bottom: 30}},
    tooltip: {{trigger: 'axis', axisPointer: {{type: 'shadow'}}}},
    xAxis: {{type: 'value', axisLine: {{lineStyle: {{color: '#b4b2a9'}}}},
      splitLine: {{lineStyle: {{color: '#eee'}}}}}},
    yAxis: {{type: 'category', data: names.slice().reverse(),
      axisLine: {{lineStyle: {{color: '#b4b2a9'}}}},
      axisLabel: {{color: '#2c2c2a', fontSize: 12}}}},
    series: [{{
      type: 'bar', data: vals.slice().reverse(),
      itemStyle: {{color: '#c0392b', borderRadius: [0, 3, 3, 0]}},
      barWidth: '62%',
      label: {{show: true, position: 'right', fontSize: 11, color: '#5f5e5a',
        formatter: function(p) {{ return p.value.toFixed(3); }} }}
    }}]
  }});
  window.addEventListener('resize', function() {{ c.resize(); }});
}}

var navDates = {json.dumps(nav_dates)};
var navStrat = {json.dumps(nav_lines.get('strategy', []))};
var navBench = {json.dumps(nav_lines.get('bench_sector', []))};
var navIndex = {json.dumps(nav_lines.get('index', []))};
var el2 = document.getElementById('navchart');
if (el2 && navDates.length) {{
  var c2 = echarts.init(el2);
  function mk(name, data, color, dashed) {{
    return {{name: name, type: 'line', showSymbol: false, data: data,
             connectNulls: true,
             lineStyle: {{width: name === 'EW-SDM Top10 等权' ? 2.4 : 1.6,
                          color: color, type: dashed ? 'dashed' : 'solid'}},
             itemStyle: {{color: color}}}};
  }}
  c2.setOption({{
    grid: {{left: 62, right: 24, top: 34, bottom: 46}},
    tooltip: {{trigger: 'axis', valueFormatter: function(v) {{
      return v == null ? '-' : Number(v).toFixed(3); }}}},
    legend: {{top: 0, textStyle: {{fontSize: 12, color: '#5f5e5a'}}}},
    xAxis: {{type: 'category', data: navDates, boundaryGap: false,
      axisLabel: {{fontSize: 11, color: '#888780'}},
      axisLine: {{lineStyle: {{color: '#b4b2a9'}}}}}},
    yAxis: {{type: 'value', scale: true,
      axisLabel: {{fontSize: 11, color: '#888780'}},
      splitLine: {{lineStyle: {{color: '#eee'}}}}}},
    series: [
      mk('EW-SDM Top10 等权', navStrat, '#c0392b', false),
      mk('全板块等权基准', navBench, '#888780', true),
      mk('上证指数基准', navIndex, '#185fa5', true)
    ]
  }});
  window.addEventListener('resize', function() {{ c2.resize(); }});
}}
</script>
</body>
</html>"""

    # ---- 替换三个占位符 ----
    etf_html = render_etf_table((etf or {}).get('items') or [])
    boci_html = render_boci_section(boci or {})
    opp_html = render_opportunity_pool(opp_items)
    html = html_body.replace('__ETF_TABLE__', etf_html)
    html = html.replace('__BOCI_DETAIL__', boci_html)
    html = html.replace('__OPP_POOL__', opp_html)
    return html


def main():
    if not os.path.exists(SRC):
        print('缺少', SRC)
        return
    d = load()
    bt_stats, navs, boci, etf, opp_items = load_backfill()
    html = build_html(d, bt_stats, navs, boci=boci, etf=etf,
                      opp_items=opp_items)
    fp = os.path.join(OUT, f"report_{d.get('trade_date','latest')}.html")
    with open(fp, 'w', encoding='utf-8') as f:
        f.write(html)
    print('报告已生成:', fp)
    return fp


if __name__ == '__main__':
    main()
