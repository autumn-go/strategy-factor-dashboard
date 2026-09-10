# -*- coding: utf-8 -*-
"""生成 BOCI 行业情绪交互页 boci_industries.html
- 下拉选 90 个行业
- 双轴图：sentiment (左 0-1) + 收盘价 (右)
- 超买/超卖阈值线
- 机会池表格（超买/超卖 Top 10）
"""
import os
import json
import html as htmllib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'output')
OPP_FP = os.path.join(OUT, 'boci_opportunities.json')
DATA_FP = os.path.join(OUT, 'data', 'boci_history.json')


def fmt(v, nd=2):
    try:
        if v is None or v == '':
            return '-'
        return f'{float(v):,.{nd}f}'
    except Exception:
        return str(v)


def build():
    with open(OPP_FP, encoding='utf-8') as f:
        opp = json.load(f)
    with open(DATA_FP, encoding='utf-8') as f:
        hist = json.load(f)

    # ---- 机会池 ----
    oversold = [r for r in opp['items'] if r['tag'] == 'oversold']
    overbought = [r for r in opp['tag'] and opp['items'] if r['tag'] == 'overbought'] if False else [r for r in opp['items'] if r['tag'] == 'overbought']
    oversold = sorted(oversold, key=lambda r: r.get('pct_60') or 0)[:10]
    overbought = sorted(overbought, key=lambda r: -(r.get('pct_60') or 0))[:10]

    def row_html(r, kind):
        cls = 'os' if kind == 'os' else 'oh'
        tag = '超跌机会' if kind == 'os' else '超买风险'
        d5 = r.get('d5') or 0
        d5s = f'{d5:+.3f}'
        sec = htmllib.escape(r['sector'])
        nm = htmllib.escape(r['name'])
        return (
            f"<tr data-sec='{sec}' "
            f"data-kind='{kind}' class='{cls}'>"
            f"<td class='nm'>{nm}</td>"
            f"<td class='cd'>{r['sector']}</td>"
            f"<td class='num'>{r['sentiment']:.3f}</td>"
            f"<td class='num'>{(r.get('pct_60') or 0)*100:.0f}%</td>"
            f"<td class='num'>{d5s}</td>"
            f"<td class='num'>{(r.get('pct_chg5') or 0):+.2f}%</td>"
            f"<td><span class='badge {kind}'>{tag}</span></td></tr>"
        )

    os_rows = ''.join(row_html(r, 'os') for r in oversold)
    oh_rows = ''.join(row_html(r, 'oh') for r in overbought)

    # ---- 下拉选项 ----
    inds = sorted(hist['industries'], key=lambda x: x['name'])
    options = ''.join(
        f"<option value='{htmllib.escape(r['sector'])}' "
        f"data-name='{htmllib.escape(r['name'])}'>"
        f"{htmllib.escape(r['name'])} ({r['sector']})</option>"
        for r in inds)

    thresholds = opp['thresholds']
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOCI 行业情绪 · 历史与机会池</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js"></script>
<style>
:root{{--bd:#e5e3dc;--tx:#2c2c2a;--tx2:#5f5e5a;--tx3:#888780;--bg2:#f7f6f2;
--up:#c0392b;--down:#1d9e75;--amb:#b7791f;--oh:#a32d2d;--os:#0f6e56;}}
*{{box-sizing:border-box}}
body{{margin:0;background:#faf9f6;color:var(--tx);
font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;line-height:1.7}}
.wrap{{max-width:1200px;margin:0 auto;padding:24px 24px 64px}}
h1{{font-size:22px;font-weight:600;margin:0 0 6px}}
h2{{font-size:16px;font-weight:600;margin:28px 0 12px;padding-left:10px;
border-left:3px solid var(--tx)}}
.meta{{color:var(--tx2);font-size:13px;margin-bottom:4px}}

.toolbar{{display:flex;gap:12px;align-items:center;margin:16px 0 12px;
background:var(--bg2);padding:12px 16px;border-radius:10px}}
.toolbar label{{font-size:13px;color:var(--tx2)}}
.toolbar select{{padding:7px 10px;border:1px solid var(--bd);border-radius:6px;
background:#fff;font-size:14px;min-width:240px;cursor:pointer}}
.toolbar input{{padding:7px 10px;border:1px solid var(--bd);border-radius:6px;
background:#fff;font-size:13px;width:80px}}

.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:10px;margin:14px 0 20px}}
.kpi{{background:var(--bg2);border-radius:10px;padding:12px 16px}}
.kpi .l{{font-size:12px;color:var(--tx2)}}
.kpi .v{{font-size:22px;font-weight:600;margin:2px 0;font-variant-numeric:tabular-nums}}
.kpi.os .v{{color:var(--os)}}
.kpi.oh .v{{color:var(--oh)}}
.kpi.flat .v{{color:var(--amb)}}

.chart{{width:100%;height:460px;background:#fff;border-radius:10px;
border:1px solid var(--bd)}}

.cards2{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:24px}}
@media (max-width:780px){{.cards2{{grid-template-columns:1fr}}}}
table{{width:100%;border-collapse:collapse;font-size:13px;background:#fff;
border-radius:8px;overflow:hidden}}
th{{background:var(--bg2);text-align:left;padding:8px 12px;font-weight:600;
font-size:12px;color:var(--tx2)}}
td{{padding:7px 12px;border-top:1px solid var(--bd)}}
tr:hover td{{background:#fcfbf8}}
tr.os{{cursor:pointer}}
tr.oh{{cursor:pointer}}
.cd{{color:var(--tx3);font-size:12px;font-family:monospace}}
.nm{{font-weight:500}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.badge{{display:inline-block;font-size:11px;padding:2px 8px;border-radius:4px}}
.badge.os{{background:#e1f5ee;color:var(--os)}}
.badge.oh{{background:#fcebeb;color:var(--oh)}}

.legend{{font-size:12px;color:var(--tx2);margin:6px 0 0}}
.legend span{{margin-right:14px}}
.legend .dot{{display:inline-block;width:10px;height:10px;border-radius:2px;
margin-right:4px;vertical-align:middle}}

.disc{{margin-top:36px;padding-top:14px;border-top:1px solid var(--bd);
font-size:12px;color:var(--tx3)}}
</style>
</head>
<body>
<div class="wrap">
  <h1>BOCI 行业情绪 · 历史与机会池</h1>
  <div class="meta">数据源：腾讯自选股 / westock · 覆盖 <b>{len(inds)}</b> 个同花顺一级行业
    · 生成 {opp['gen_time']}</div>

  <h2>行业情绪历史走势 · sentiment vs 收盘价</h2>
  <div class="toolbar">
    <label>选择行业</label>
    <select id="sel"><option value="">-- 请选择 --></select>
    <label>窗口</label>
    <input id="win" type="number" value="120" min="30" max="240" step="10">天
  </div>
  <div id="kpis" class="kpis"></div>
  <div id="chart" class="chart"></div>
  <div class="legend">
    <span><i class="dot" style="background:#c0392b"></i>sentiment（左轴 0-1）</span>
    <span><i class="dot" style="background:#185fa5"></i>收盘价（右轴）</span>
    <span><i class="dot" style="background:#888780"></i>超买阈值 {thresholds['pct_overbought']*100:.0f}% / 绝对值 {thresholds['abs_overbought']}</span>
    <span><i class="dot" style="background:#888780"></i>超卖阈值 {thresholds['pct_oversold']*100:.0f}% / 绝对值 {thresholds['abs_oversold']}</span>
  </div>

  <h2>当日机会池</h2>
  <div class="cards2">
    <div>
      <h2 style="font-size:14px;color:var(--os);border-color:var(--os)">
        超跌机会（pct60 ≤ {thresholds['pct_oversold']*100:.0f}% 或 sentiment &lt; {thresholds['abs_oversold']}）</h2>
      <table>
        <thead><tr><th>行业</th><th>代码</th><th class='num'>sentiment</th>
          <th class='num'>60日百分位</th><th class='num'>5日Δ</th>
          <th class='num'>5日涨跌幅</th><th>信号</th></tr></thead>
        <tbody id="os_body">{os_rows}</tbody>
      </table>
    </div>
    <div>
      <h2 style="font-size:14px;color:var(--oh);border-color:var(--oh)">
        超买风险（pct60 ≥ {thresholds['pct_overbought']*100:.0f}% 或 sentiment &gt; {thresholds['abs_overbought']}）</h2>
      <table>
        <thead><tr><th>行业</th><th>代码</th><th class='num'>sentiment</th>
          <th class='num'>60日百分位</th><th class='num'>5日Δ</th>
          <th class='num'>5日涨跌幅</th><th>信号</th></tr></thead>
        <tbody id="oh_body">{oh_rows}</tbody>
      </table>
    </div>
  </div>

  <div class="disc">
    阈值说明：① 60 日百分位 = 当前 sentiment 在过去 60 个交易日（不含今日）的相对位置；
    pct60 ≤ 15% 视为超跌机会（情绪跌到近 60 日底部）；pct60 ≥ 85% 视为超买风险。
    ② 双重判定：sentiment 绝对值 &lt; 0.30 也算超跌，&gt; 0.70 也算超买。
    ③ 历史数据缓存于 factors.pkl['boci_daily']，时间范围近 240 天；
    详细指标定义见主报告「BOCI 五维行业情绪」区块。
  </div>
</div>

<script>
const TH = {json.dumps(thresholds)};
const INDUSTRIES = {json.dumps(hist['industries'], ensure_ascii=False)};
const OPP = {json.dumps(opp['items'], ensure_ascii=False)};

// 填充下拉
const sel = document.getElementById('sel');
const optList = INDUSTRIES.map(r => {{
  const o = document.createElement('option');
  o.value = r.sector;
  o.dataset.name = r.name;
  o.textContent = `${{r.name}} (${{r.sector}})`;
  sel.appendChild(o);
}});

// 排序：按 sentiment 升序（潜在机会在前）
const oppBySector = {{}};
OPP.forEach(r => oppBySector[r.sector] = r);
INDUSTRIES.sort((a, b) => {{
  const sa = (oppBySector[a.sector] || {{}}).sentiment || 0.5;
  const sb = (oppBySector[b.sector] || {{}}).sentiment || 0.5;
  return sa - sb;
}});
// 重新填充
sel.innerHTML = '';
INDUSTRIES.forEach(r => {{
  const o = document.createElement('option');
  o.value = r.sector;
  o.dataset.name = r.name;
  const op = oppBySector[r.sector] || {{}};
  let prefix = '';
  if (op.tag === 'oversold') prefix = '🟢 ';
  else if (op.tag === 'overbought') prefix = '🔴 ';
  o.textContent = prefix + r.name + ' (' + r.sector + ')  sent=' + (op.sentiment || 0).toFixed(3);
  sel.appendChild(o);
}});

// 默认选中 第一个 oversold
const firstOS = INDUSTRIES.find(r => (oppBySector[r.sector]||{{}}).tag === 'oversold');
if (firstOS) sel.value = firstOS.sector;

const winInput = document.getElementById('win');
let chart = null;

function pct60(arr) {{
  // 60日百分位：当前 sentiment 在历史里的位置（不含今天）
  if (!arr || arr.length < 5) return null;
  const last = arr[arr.length - 1];
  const hist = arr.slice(0, -1).filter(x => x != null);
  if (hist.length < 5) return null;
  return hist.filter(x => x < last).length / hist.length;
}}

function render() {{
  const sec = sel.value;
  if (!sec) return;
  const ind = INDUSTRIES.find(r => r.sector === sec);
  if (!ind) return;
  const win = Math.max(10, parseInt(winInput.value) || 120);
  const dates = ind.dates.slice(-win);
  const sent = ind.sentiment.slice(-win);
  const close = ind.close.slice(-win);
  const op = oppBySector[sec] || {{}};

  // KPI
  const k = document.getElementById('kpis');
  const lastSent = sent[sent.length - 1];
  const p60 = pct60(ind.sentiment);
  const lastClose = close[close.length - 1];
  const prevClose = close[0];
  const chgPct = prevClose ? (lastClose / prevClose - 1) * 100 : 0;
  const d5 = (op.d5 != null) ? op.d5 : null;

  let tagCls = 'flat', tagName = '中性';
  if (op.tag === 'oversold') {{ tagCls = 'os'; tagName = '超跌机会'; }}
  else if (op.tag === 'overbought') {{ tagCls = 'oh'; tagName = '超买风险'; }}

  k.innerHTML = `
    <div class="kpi ${{tagCls}}"><div class="l">状态</div>
      <div class="v">${{tagName}}</div></div>
    <div class="kpi"><div class="l">当前 sentiment</div>
      <div class="v">${{lastSent != null ? lastSent.toFixed(3) : '-'}}</div></div>
    <div class="kpi"><div class="l">60 日百分位</div>
      <div class="v">${{p60 != null ? (p60*100).toFixed(0)+'%' : '-'}}</div></div>
    <div class="kpi"><div class="l">5 日 sentiment 变化</div>
      <div class="v">${{d5 != null ? (d5>=0?'+':'') + d5.toFixed(3) : '-'}}</div></div>
    <div class="kpi"><div class="l">近 ${{win}} 日价格涨跌</div>
      <div class="v" style="color:${{chgPct>=0?'#c0392b':'#1d9e75'}}">${{chgPct>=0?'+':''}}${{chgPct.toFixed(2)+'%'}}</div></div>
  `;

  // ECharts: 双轴线图 + 阈值标线
  if (!chart) chart = echarts.init(document.getElementById('chart'));
  const closeMin = Math.min(...close.filter(x => x != null));
  const closeMax = Math.max(...close.filter(x => x != null));
  const closePad = (closeMax - closeMin) * 0.1 || 1;

  chart.setOption({{
    grid: {{left: 56, right: 64, top: 30, bottom: 60}},
    tooltip: {{trigger: 'axis', axisPointer: {{type: 'cross'}},
      formatter: function(params) {{
        const i = params[0].dataIndex;
        return `<b>${{dates[i]}}</b><br>` +
          params.filter(p => p.seriesName.includes('sentiment'))
            .map(p => `${{p.marker}} sentiment: ${{(p.value||0).toFixed(3)}}`).join('<br>') +
          '<br>' + params.filter(p => p.seriesName.includes('价格'))
            .map(p => `${{p.marker}} 收盘: ${{(p.value||0).toFixed(2)}}`).join('<br>');
      }}
    }},
    legend: {{top: 0, textStyle: {{fontSize: 12, color: '#5f5e5a'}}}},
    xAxis: {{type: 'category', data: dates, boundaryGap: false,
      axisLabel: {{fontSize: 11, color: '#888780'}},
      axisLine: {{lineStyle: {{color: '#b4b2a9'}}}}}},
    yAxis: [
      {{type: 'value', name: 'sentiment', min: 0, max: 1,
        nameTextStyle: {{fontSize: 11, color: '#888780'}},
        axisLabel: {{fontSize: 11, color: '#888780', formatter: '{{value}}'}},
        splitLine: {{lineStyle: {{color: '#eee'}}}}}},
      {{type: 'value', name: '收盘价',
        min: closeMin - closePad, max: closeMax + closePad,
        nameTextStyle: {{fontSize: 11, color: '#185fa5'}},
        axisLabel: {{fontSize: 11, color: '#185fa5'}},
        splitLine: {{show: false}}}}
    ],
    series: [
      {{name: 'sentiment', type: 'line', yAxisIndex: 0, data: sent,
        showSymbol: false, connectNulls: true,
        lineStyle: {{width: 2.2, color: '#c0392b'}},
        itemStyle: {{color: '#c0392b'}},
        markLine: {{
          symbol: 'none',
          lineStyle: {{type: 'dashed', width: 1.2, color: '#888780'}},
          data: [
            {{yAxis: TH.abs_overbought, name: `超买 (${{TH.abs_overbought}})`}},
            {{yAxis: TH.abs_oversold, name: `超卖 (${{TH.abs_oversold}})`}}
          ],
          label: {{fontSize: 10, color: '#888780'}}
        }},
        markArea: {{
          silent: true, itemStyle: {{opacity: 0.08}},
          data: [
            [{{yAxis: TH.abs_overbought, itemStyle: {{color: '#c0392b'}}}},
             {{yAxis: 1}}],
            [{{yAxis: 0, itemStyle: {{color: '#1d9e75'}}}},
             {{yAxis: TH.abs_oversold}}]
          ]
        }}
      }},
      {{name: '收盘价', type: 'line', yAxisIndex: 1, data: close,
        showSymbol: false, connectNulls: true,
        lineStyle: {{width: 1.8, color: '#185fa5'}},
        itemStyle: {{color: '#185fa5'}}}}
    ]
  }});
}}

sel.addEventListener('change', render);
winInput.addEventListener('change', render);
window.addEventListener('resize', () => chart && chart.resize());

// 表格行点击 → 切换行业
document.querySelectorAll('#os_body tr, #oh_body tr').forEach(tr => {{
  tr.addEventListener('click', () => {{
    sel.value = tr.dataset.sec;
    render();
    document.getElementById('chart').scrollIntoView({{behavior: 'smooth', block: 'start'}});
  }});
}});

render();
</script>
</body>
</html>"""


def main():
    if not os.path.exists(OPP_FP):
        print('缺少', OPP_FP, '请先跑 boci_history.py')
        return
    if not os.path.exists(DATA_FP):
        print('缺少', DATA_FP)
        return
    html = build()
    fp = os.path.join(OUT, 'boci_industries.html')
    with open(fp, 'w', encoding='utf-8') as f:
        f.write(html)
    print('生成:', fp, f'({len(html)/1024:.0f} KB)')


if __name__ == '__main__':
    main()