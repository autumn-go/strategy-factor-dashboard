#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把策略因子平台的 HTML 报告发布到 GitHub Pages（autumn-go/strategy-factor-dashboard）

沿用行业轮动 / 交易拥挤度项目的发布方式：
  - GitHub Contents API（api.github.com 能过代理；git push 的 CONNECT 会被拦）
  - token 来源：env GITHUB_TOKEN > /tmp/ghtoken（不回显明文）

发布内容：
  output/report_<日期>.html  ->  index.html（Pages 首页，始终是当日最新）
                               report_<日期>.html（历史归档）
  output/boci_industries.html ->  boci_industries.html

用法：
  python3 publish_pages.py              # 发布
  python3 publish_pages.py --check      # 只校验远端内容，不上传
"""
import os
import sys
import json
import base64
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'output')

OWNER = 'autumn-go'
REPO = 'strategy-factor-dashboard'
BRANCH = 'main'
PAGES_URL = f'https://{OWNER}.github.io/{REPO}/'


def get_token():
    t = os.environ.get('GITHUB_TOKEN', '').strip()
    if t:
        return t
    # /tmp 会被系统清理，项目内 .ghtoken 作为持久备份
    for p in ('/tmp/ghtoken', os.path.join(HERE, '.ghtoken')):
        if os.path.exists(p):
            try:
                with open(p) as f:
                    v = f.read().strip()
                if v:
                    return v
            except Exception:
                pass
    return ''


def _headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'User-Agent': 'strategy-factor-publisher',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
    }


def get_sha(token, path):
    api = f'https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}?ref={BRANCH}'
    try:
        req = urllib.request.Request(api, headers=_headers(token))
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get('sha')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def put_file(token, path, content_bytes, message, retries=4):
    """Contents API PUT（单文件上限约 1MB，超限由调用方降级）"""
    b64 = base64.b64encode(content_bytes).decode('ascii')
    body = {'message': message, 'content': b64, 'branch': BRANCH}
    sha = get_sha(token, path)
    if sha:
        body['sha'] = sha
    data = json.dumps(body).encode('utf-8')
    api = f'https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}'
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(api, data=data,
                                         headers=_headers(token), method='PUT')
            with urllib.request.urlopen(req, timeout=180) as r:
                res = json.loads(r.read())
            commit = (res.get('commit') or {}).get('sha', '?')[:8]
            print(f'  [{path}] OK (commit {commit}, {len(content_bytes)/1024:.0f} KB)')
            return True
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'ignore')[:200]
            print(f'  [{path}] HTTP {e.code}: {detail}')
            if e.code in (401, 403):
                return False
        except Exception as e:
            print(f'  [{path}] attempt {attempt} error: {type(e).__name__} {e}')
        if attempt < retries:
            time.sleep(3)
    return False


def ensure_pages(token):
    """开启 Pages（source: main / root）。已开启则忽略 409。"""
    api = f'https://api.github.com/repos/{OWNER}/{REPO}/pages'
    body = json.dumps({'source': {'branch': BRANCH, 'path': '/'}}).encode()
    try:
        req = urllib.request.Request(api, data=body,
                                     headers=_headers(token), method='POST')
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read())
        print('  Pages 已开启:', (d.get('html_url') or PAGES_URL))
        return True
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print('  Pages 已存在（409），跳过')
            return True
        det = e.read().decode('utf-8', 'ignore')[:200]
        print(f'  Pages 开启失败 HTTP {e.code}: {det}')
        return False
    except Exception as e:
        print('  Pages 开启异常:', e)
        return False


def find_latest_report():
    """找到 output/ 里日期最新的 report_YYYYMMDD.html"""
    if not os.path.isdir(OUT):
        return None
    cands = []
    for f in os.listdir(OUT):
        if f.startswith('report_') and f.endswith('.html'):
            cands.append(f)
    if not cands:
        return None
    return os.path.join(OUT, sorted(cands)[-1])


def main():
    check_only = '--check' in sys.argv
    token = get_token()
    if not token:
        print('[fatal] 缺 GITHUB_TOKEN 或 /tmp/ghtoken')
        return 1

    rep = find_latest_report()
    boci = os.path.join(OUT, 'boci_industries.html')
    if not rep:
        print('[fatal] output/ 下没有 report_*.html')
        return 1

    date_tag = os.path.basename(rep)[len('report_'):-len('.html')]
    print(f'发布 {OWNER}/{REPO}  branch={BRANCH}')
    print(f'  主报告: {os.path.basename(rep)} ({os.path.getsize(rep)/1024:.0f} KB)')
    if os.path.exists(boci):
        print(f'  BOCI页: boci_industries.html ({os.path.getsize(boci)/1024:.0f} KB)')

    if check_only:
        for p in ('index.html', f'report_{date_tag}.html'):
            print(f'  remote {p} sha:', (get_sha(token, p) or '(不存在)')[:10])
        return 0

    ensure_pages(token)

    with open(rep, 'rb') as f:
        rep_bytes = f.read()
    msg = f'daily update {date_tag}'

    ok = True
    # 1) 当日报告归档（report_<日期>.html）
    ok &= put_file(token, f'report_{date_tag}.html', rep_bytes, msg)
    # 2) 首页 index.html（始终是当日最新）
    ok &= put_file(token, 'index.html', rep_bytes, msg)
    # 3) BOCI 交互页
    if os.path.exists(boci):
        with open(boci, 'rb') as f:
            boci_bytes = f.read()
        ok &= put_file(token, 'boci_industries.html', boci_bytes, msg)

    if ok:
        print('发布完成 ->', PAGES_URL)
    else:
        print('部分文件发布失败')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())