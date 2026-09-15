# -*- coding: utf-8 -*-
"""把最新策略信号推送到飞书

用法:
    python feishu_push.py --chat-id oc_xxxxx          # 推到群
    python feishu_push.py --chat-id oc_xxxxx --dry    # 只看内容不发送

前置: 机器人需在目标群里（或改用 --user-id 以用户身份发）
"""
import os
import sys
import json
import argparse
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'output', 'latest_signals.json')
LARK = ('/Users/beanpaper/.workbuddy/binaries/node/cli-connector-packages/'
        'bin/lark-cli')

# GitHub Pages 公网托管地址（HTML 报告 + BOCI 交互页）
DEFAULT_DASHBOARD_URL = 'https://autumn-go.github.io/strategy-factor-dashboard/'
# 可选配置文件（与行业轮动项目同格式）
CFG_FILE = os.path.join(HERE, 'config', 'feishu.json')

# webhook 地址来源优先级: 参数 > 环境变量 > 配置文件
WEBHOOK_FILE = os.path.join(HERE, '.webhook')


def _load_feishu_cfg():
    """读取 config/feishu.json（与行业轮动 / 拥挤度项目同格式）
    { "webhook": ..., "secret": ..., "enabled": true, "dashboardUrl": ... }
    """
    if not os.path.exists(CFG_FILE):
        return {}
    try:
        with open(CFG_FILE, encoding='utf-8') as f:
            c = json.load(f) or {}
        if c.get('enabled') is False:
            return {}
        return c
    except Exception:
        return {}


def resolve_webhook(cli=None):
    if cli:
        return cli
    if os.environ.get('FEISHU_WEBHOOK'):
        return os.environ['FEISHU_WEBHOOK']
    if os.path.exists(WEBHOOK_FILE):
        with open(WEBHOOK_FILE, encoding='utf-8') as f:
            return f.read().strip()
    return ''


def send_webhook(webhook, md, title='策略因子平台 · 最新信号',
                 html_url=None, boci_url=None):
    """飞书群自定义机器人 webhook（interactive 卡片，支持 lark_md 渲染）

    html_url: HTML 报告公网 URL（部署到 GitHub Pages 后填入，飞书 web 端可点）
    boci_url: BOCI 交互页 URL（同上）
    """
    elements = [
        {'tag': 'div', 'text': {'tag': 'lark_md', 'content': md}},
        {'tag': 'hr'},
    ]
    # 加跳转按钮（如果有公网 URL）
    if html_url or boci_url:
        actions = []
        if html_url:
            actions.append({
                'tag': 'button',
                'text': {'tag': 'plain_text', 'content': '📊 HTML 报告'},
                'type': 'primary',
                'url': html_url,
            })
        if boci_url:
            actions.append({
                'tag': 'button',
                'text': {'tag': 'plain_text', 'content': '📈 行业情绪交互页'},
                'type': 'default',
                'url': boci_url,
            })
        elements.append({
            'tag': 'action',
            'actions': actions,
        })
        elements.append({'tag': 'hr'})
    elements.append({
        'tag': 'note',
        'text': {'tag': 'lark_md',
                 'content': '数据来源：内置金融服务 westock / 腾讯自选股 · '
                             '仅供研究参考，不构成投资建议'},
    })
    payload = {
        'msg_type': 'interactive',
        'card': {
            'config': {'wide_screen_mode': True},
            'header': {'title': {'tag': 'plain_text', 'content': title},
                       'template': 'turquoise'},
            'elements': elements,
        }
    }
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        webhook, data=data,
        headers={'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode('utf-8', 'ignore')


def fmt(v, nd=2):
    try:
        return f'{float(v):,.{nd}f}'
    except Exception:
        return str(v)


def build_md(d):
    td = d.get('trade_date', '')
    ews = d.get('ews', {}) or {}
    lc = d.get('lc', {}) or {}

    lines = [f"**策略因子平台 · 最新信号**", f"交易日 {td}", ""]

    # 行情滞后告警（见 run_latest.py 的"防呆对时"）——放最顶部，手机上第一眼可见
    lag = d.get('data_lag') or None
    if lag:
        lines.append(
            f"⚠️ **行情数据滞后告警**：策略交易日 {lag.get('strategy_date','?')} "
            f"< 市场最新交易日 **{lag.get('market_date','?')}**，"
            f"下列结果可能已陈旧，请检查数据刷新链路。")
        lines.append("")

    # EW-SDM
    if ews.get('error'):
        lines.append(f"> EW-SDM：{ews['error']}")
    elif ews:
        lines.append(
            f"**EW-SDM**：覆盖 {ews.get('total_sectors', '-')} 个板块"
            f"　涨停 {ews.get('limit_up_count', '-')} 家"
            f"　炸板 {ews.get('limit_broken_count', '-')} 家")
        rows = (ews.get('top') or [])[:8]
        if rows:
            sc = ews.get('score_col', 'score')
            lines.append("")
            lines.append("**情绪动量 Top 8**")
            for i, r in enumerate(rows, 1):
                nm = r.get('name', r.get('ts_code', ''))
                val = r.get(sc, 0)
                try:
                    val = f'{float(val):.4f}'
                except Exception:
                    val = str(val)
                lines.append(f"{i}. {nm}　`{val}`")

    # BOCI 五维行业情绪（从 factors.pkl 加载）
    boci = _load_factors_key('boci_latest') or {}
    if boci.get('top'):
        tradeable = [r for r in boci.get('top', []) if not r.get('overheat')][:3]
        n_oh = boci.get('n_overheat', 0)
        n_ind = boci.get('n_industry', 0)
        lines.append("")
        lines.append(
            f"**BOCI 五维行业情绪**：{n_ind} 行业 · 过热剔除 {n_oh} 个")
        if tradeable:
            lines.append("")
            lines.append("**可做 Top 3**")
            for i, r in enumerate(tradeable, 1):
                nm = r.get('name', r.get('sector', ''))
                sn = r.get('sentiment', 0)
                try:
                    sn = f'{float(sn):.3f}'
                except Exception:
                    sn = str(sn)
                lines.append(f"{i}. {nm}　`{sn}`")
        # BOCI Top 5 详细（含五维分）
        lines.append("")
        lines.append("**BOCI 详细 Top 5**")
        lines.append("| # | 行业 | Score | Sent | F1 | F2 | F3 | F4 | F5 | 状态 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(boci.get('top', [])[:5], 1):
            nm = r.get('name', '')
            sc = r.get('score', 0)
            sn = r.get('sentiment', 0)
            oh = r.get('overheat', False)
            tag = '过热' if oh else '可做'
            row_vals = []
            for k in ('f1', 'f2', 'f3', 'f4', 'f5'):
                v = r.get(k, 0) or 0
                row_vals.append(f'{float(v):.2f}')
            lines.append(
                f"| {i} | {nm} | {sc:.3f} | {sn:.3f} | "
                f"{' | '.join(row_vals)} | {tag} |")

    # LC
    if lc.get('error'):
        lines.append(f"> LC-KNN：{lc['error']}")
    elif lc:
        pred = lc.get('prediction')
        try:
            pred = f'{float(pred):.3f}'
        except Exception:
            pred = str(pred)
        lines.append("")
        lines.append(f"**LC-KNN 洛伦兹分类**：预测值 `{pred}`（>0 看多）"
                     f"　小盘指数收盘 {fmt(lc.get('close'))}")

    # ETF 清单
    etf = _load_factors_key('etf_watchlist') or {}
    items = etf.get('items') or []
    if items:
        lines.append("")
        lines.append(f"**每日可买 ETF（成交额≥1000万）共 {len(items)} 只**")
        lines.append("| 板块 | ETF 名称 | 代码 | 现价 | 成交额 |")
        lines.append("|---|---|---|---|---|")
        for r in items[:10]:
            amt = (r.get('amount') or 0) / 1e8
            lines.append(
                f"| {r.get('sector','')} | {r.get('fund_name','')} | "
                f"`{r.get('code','')}` | {r.get('close',''):.3f} | "
                f"{amt:.2f}亿 |")
        if len(items) > 10:
            lines.append(f"| … | （其余 {len(items) - 10} 只）| | | |")

    # 行业情绪机会池（从 factors.pkl['boci_opportunities']）
    opp = _load_factors_key('boci_opportunities') or []
    if opp:
        os_top = sorted([r for r in opp if r.get('tag') == 'oversold'],
                        key=lambda r: r.get('pct_60') or 0)[:5]
        oh_top = sorted([r for r in opp if r.get('tag') == 'overbought'],
                        key=lambda r: -(r.get('pct_60') or 0))[:5]
        if os_top or oh_top:
            lines.append("")
            lines.append("**行业情绪机会池（60日百分位）**")
            if os_top:
                lines.append("")
                lines.append("🟢 **超跌机会**（情绪底部）")
                lines.append("| 行业 | sentiment | pct60 | 5日Δ | 5日涨幅 |")
                lines.append("|---|---|---|---|---|")
                for r in os_top:
                    d5 = r.get('d5')
                    d5s = f'{d5:+.3f}' if d5 is not None else '-'
                    lines.append(
                        f"| {r.get('name','')} | {r.get('sentiment',0):.3f} | "
                        f"{(r.get('pct_60') or 0)*100:.0f}% | "
                        f"{d5s} | {(r.get('pct_chg5') or 0):+.2f}% |")
            if oh_top:
                lines.append("")
                lines.append("🔴 **超买风险**（情绪顶部）")
                lines.append("| 行业 | sentiment | pct60 | 5日Δ | 5日涨幅 |")
                lines.append("|---|---|---|---|---|")
                for r in oh_top:
                    d5 = r.get('d5')
                    d5s = f'{d5:+.3f}' if d5 is not None else '-'
                    lines.append(
                        f"| {r.get('name','')} | {r.get('sentiment',0):.3f} | "
                        f"{(r.get('pct_60') or 0)*100:.0f}% | "
                        f"{d5s} | {(r.get('pct_chg5') or 0):+.2f}% |")

    # 回测关键指标
    bt_stats = _load_factors_key('stats') or {}
    if bt_stats:
        lines.append("")
        lines.append("**EW-SDM Top10 等权 · 回测关键指标**")
        lines.append(
            f"区间 {bt_stats.get('days','-')} 个交易日 · "
            f"总收益 `{bt_stats.get('total_return','-')}%` · "
            f"年化 `{bt_stats.get('annual_return','-')}%` · "
            f"回撤 `{bt_stats.get('max_drawdown','-')}%` · "
            f"胜率 `{bt_stats.get('win_rate','-')}%` · "
            f"夏普 `{bt_stats.get('sharpe','-')}`")
        # 基准对比
        try:
            import json
            bp = os.path.join(HERE, 'output', 'backfill_stats.json')
            if os.path.exists(bp):
                with open(bp, encoding='utf-8') as f:
                    bj = json.load(f)
                bsec = bj.get('benchmark_sector')
                bidx = bj.get('benchmark_index')
                tot = bt_stats.get('total_return', 0) or 0
                if bsec is not None and bidx is not None:
                    excess = round(tot - bsec, 2)
                    lines.append(
                        f"基准对比：全板块等权 `{bsec}%` · 上证 `{bidx}%` · "
                        f"超额 `{excess:+}pp`")
        except Exception:
            pass

    lines.append("")
    lines.append(f"<font color='grey'>数据源：内置金融服务 westock / 腾讯自选股 · "
                 f"生成 {d.get('gen_time', '')}</font>")
    lines.append("<font color='grey'>仅供研究参考，不构成投资建议</font>")
    return '\n'.join(lines)


def _load_factors_key(key):
    """从 cache/factors.pkl 取 boci_latest / etf_watchlist 等键值"""
    import pickle
    p = os.path.join(HERE, 'cache', 'factors.pkl')
    if not os.path.exists(p):
        return None
    try:
        with open(p, 'rb') as f:
            d = pickle.load(f)
        return d.get(key)
    except Exception:
        return None


def send(chat_id, md, as_identity='bot', dry=False):
    cmd = [LARK, 'im', '+messages-send', '--chat-id', chat_id,
           '--as', as_identity, '--markdown', md]
    if dry:
        print('[dry-run] 将发送:')
        print(md)
        return 0
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    print(p.stdout or p.stderr)
    return p.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--chat-id', default=os.environ.get('FEISHU_CHAT_ID', ''),
                    help='飞书群 ID (oc_xxx)，走机器人身份发送')
    ap.add_argument('--webhook', default=None,
                    help='群自定义机器人 webhook 地址')
    ap.add_argument('--html-url', default=None,
                    help='HTML 报告公网 URL（默认 GitHub Pages 首页）')
    ap.add_argument('--boci-url', default=None,
                    help='BOCI 交互页公网 URL；默认与 html-url 同目录')
    ap.add_argument('--as', dest='identity', default='bot')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()

    if not os.path.exists(SRC):
        print('缺少', SRC, '，请先运行 run_latest.py')
        return 1
    with open(SRC, encoding='utf-8') as f:
        d = json.load(f)
    md = build_md(d)

    # 公网 URL：参数 > 环境变量 > config/feishu.json > 默认 Pages
    cfg = _load_feishu_cfg()
    html_url = (a.html_url or os.environ.get('STRATEGY_HTML_URL')
                or cfg.get('dashboardUrl') or DEFAULT_DASHBOARD_URL)
    boci_url = a.boci_url or os.environ.get('STRATEGY_BOCI_URL')
    if not boci_url and html_url:
        boci_url = html_url.rstrip('/') + '/boci_industries.html'

    wh = resolve_webhook(a.webhook) or cfg.get('webhook') or ''
    if a.dry:
        print('[dry-run] 将发送:\n')
        print(md)
        print()
        print('HTML URL:', html_url or '(未设置)')
        print('BOCI URL:', boci_url or '(未设置)')
        return 0
    if wh:
        print('[webhook] 发送中 ...')
        print(send_webhook(wh, md,
                           html_url=html_url or None,
                           boci_url=boci_url or None))
        return 0
    if a.chat_id:
        return send(a.chat_id, md, a.identity)
    print('未配置推送目标（--webhook / --chat-id / %s），仅预览:\n' % WEBHOOK_FILE)
    print(md)
    return 0


if __name__ == '__main__':
    sys.exit(main())
