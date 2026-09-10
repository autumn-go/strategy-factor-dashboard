# -*- coding: utf-8 -*-
"""把今日飞书推送的 lark_md 内容按飞书官方样式渲染成 HTML 预览"""
import os
import json
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SYS_DIR = os.path.join(HERE, '..', 'westock')
sys.path.insert(0, HERE)
sys.path.insert(0, SYS_DIR)

# 直接复用 build_md 但需要 import
import importlib.util
spec = importlib.util.spec_from_file_location(
    "feishu_push", os.path.join(SYS_DIR, "feishu_push.py"))
fp_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp_mod)

with open(os.path.join(SYS_DIR, 'output', 'latest_signals.json'),
          encoding='utf-8') as f:
    d = json.load(f)
md = fp_mod.build_md(d)

# 飞书 lark_md → HTML 转换（精简版）
def lark_md_to_html(md):
    # 先保护 code 块（飞书用 `xxx`）
    code_pattern = re.compile(r'`([^`]+)`')
    placeholders = []
    def save_code(m):
        placeholders.append(('<code>', m.group(1), '</code>'))
        return f'\x00CODE{len(placeholders)-1}\x00'
    html = code_pattern.sub(save_code, md)
    # <font color='xxx'>yyy</font> → span
    html = re.sub(r"<font color='grey'>", '<span class="grey">', html)
    html = re.sub(r"<font color='red'>", '<span class="red">', html)
    html = html.replace('</font>', '</span>')
    # > 引用
    html = re.sub(r'^> (.+)$', r'<blockquote>\1</blockquote>', html, flags=re.M)
    # **加粗**
    html = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', html)
    # 行首 "1. xxx" 这种有序列表
    lines = html.split('\n')
    out = []
    in_list = False
    for line in lines:
        m_ol = re.match(r'^(\d+)\. (.*)', line)
        if m_ol:
            if not in_list:
                out.append('<ol>')
                in_list = True
            out.append(f'<li>{m_ol.group(2)}</li>')
        else:
            if in_list:
                out.append('</ol>')
                in_list = False
            if line.strip():
                out.append(f'<p>{line}</p>')
            else:
                out.append('')
    if in_list:
        out.append('</ol>')
    html = '\n'.join(out)
    # 把 code 占位符换回
    for i, (open_, code, close) in enumerate(placeholders):
        html = html.replace(f'\x00CODE{i}\x00', open_ + code + close)
    return html


body = lark_md_to_html(md)
html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>飞书推送预览</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#f1f3f5;font-family:-apple-system,"PingFang SC",
"Microsoft YaHei",sans-serif;padding:32px 16px;color:#1f2329;line-height:1.6}}
.header{{max-width:560px;margin:0 auto 16px;color:#646a73;font-size:13px;
text-align:center}}
.wrap{{max-width:560px;margin:0 auto;background:#fff;border-radius:8px;
box-shadow:0 2px 12px rgba(0,0,0,0.05);overflow:hidden}}
.title{{background:#20c5b3;color:#fff;padding:18px 24px;font-size:18px;font-weight:600}}
.card{{padding:20px 24px;font-size:14px}}
.card p{{margin:8px 0}}
.card b{{font-weight:600}}
.card .grey{{color:#8f959e;font-size:12px}}
.card .red{{color:#d44a4a}}
.card code{{background:#f2f3f5;color:#1f2329;font-size:12px;padding:1px 6px;
border-radius:4px;font-family:-apple-system,monospace}}
.card ol{{margin:8px 0;padding-left:24px}}
.card ol li{{margin:4px 0}}
.card blockquote{{margin:6px 0;padding:6px 12px;background:#fef6e7;
border-left:3px solid #ff9a00;color:#646a73;font-size:13px;border-radius:4px}}
.divider{{border:none;border-top:1px solid #dee0e3;margin:0}}
.action{{padding:14px 24px;background:#fafbfc;text-align:center}}
.btn{{display:inline-block;padding:8px 18px;border-radius:6px;font-size:14px;
text-decoration:none;margin:0 6px;font-weight:500}}
.btn-primary{{background:#20c5b3;color:#fff}}
.btn-default{{background:#fff;color:#1f2329;border:1px solid #dee0e3}}
.note{{padding:14px 24px;color:#8f959e;font-size:12px;text-align:center;
background:#fafbfc}}
.url{{max-width:560px;margin:14px auto 0;color:#646a73;font-size:12px;
text-align:center;padding:10px;background:#fff;border-radius:6px;
word-break:break-all;font-family:monospace}}
</style>
</head>
<body>
<div class="header">↓ 以下是你群机器人 webhook 实际收到的卡片样式 ↓</div>
<div class="wrap">
  <div class="title">策略因子平台 · 最新信号</div>
  <hr class="divider">
  <div class="card">{body}</div>
  <hr class="divider">
  <div class="action">
    <a class="btn btn-primary" href="https://autumn-go.github.io/strategy-factor-dashboard/" target="_blank">📊 HTML 报告</a>
    <a class="btn btn-default" href="https://autumn-go.github.io/strategy-factor-dashboard/boci_industries.html" target="_blank">📈 行业情绪交互页</a>
  </div>
  <hr class="divider">
  <div class="note">数据来源：内置金融服务 westock / 腾讯自选股<br>仅供研究参考，不构成投资建议</div>
</div>
<div class="url">
  POST https://open.feishu.cn/open-apis/bot/v2/hook/&lt;your-webhook&gt;<br>
  返回：{{"StatusCode":0,"StatusMessage":"success","code":0,"data":{{}},"msg":"success"}}
</div>
</body>
</html>"""

fp = os.path.join(HERE, 'output', 'feishu_preview.html')
with open(fp, 'w', encoding='utf-8') as f:
    f.write(html)
print(f'生成: {fp}')
print()
print('=' * 60)
print('实际推送的 lark_md 内容（飞书收到的就是这一段）:')
print('=' * 60)
print(md)