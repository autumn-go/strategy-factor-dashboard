#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把策略平台的源码同步到 GitHub 仓库（Git Data API，一次提交多文件）

用于开源仓库 autumn-go/strategy-factor-dashboard 的代码部分：
  本地 strategy-platform/*.py          -> 仓库 src/engines/
  local strategy-platform/static/*.html -> 仓库 src/static/
  local strategy-platform/models/*      -> 仓库 src/models/
  local strategy-platform/westock/*.py  -> 仓库 src/westock/
  local strategy-platform/westock/daily_run.sh -> src/westock/

排除清单（敏感 / 大数据 / 无需开源）：
  .ghtoken  .webhook  config/feishu.json  cache/  output/  data/  local_dbs/
  __pycache__  static/plotly.min.js（第三方库，3.6MB）

用法：
  python3 publish_code.py            # 同步上传
  python3 publish_code.py --dry      # 只列出将上传的文件
"""
import os
import sys
import json
import base64
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(HERE)          # strategy-platform/

OWNER = 'autumn-go'
REPO = 'strategy-factor-dashboard'
BRANCH = 'main'
COMMIT_MSG = 'sync strategy source code'

# 排除的文件名 / 目录名
EXCLUDE_NAMES = {
    '.ghtoken', '.webhook', 'feishu.json', 'plotly.min.js',
    '__pycache__', 'cache', 'output', 'data', 'local_dbs', '.DS_Store',
    'backfill.log',
}
EXCLUDE_SUFFIX = ('.pkl', '.db', '.db-journal', '.pyc')


def get_token():
    t = os.environ.get('GITHUB_TOKEN', '').strip()
    if t:
        return t
    for p in ('/tmp/ghtoken', os.path.join(HERE, '.ghtoken')):
        if os.path.exists(p):
            with open(p) as f:
                v = f.read().strip()
            if v:
                return v
    return ''


def collect(mapping):
    """mapping: [(本地目录, 仓库子路径)]，返回 [(本地绝对路径, 仓库相对路径)]"""
    out = []
    for local_dir, repo_prefix in mapping:
        if not os.path.isdir(local_dir):
            continue
        for name in sorted(os.listdir(local_dir)):
            fp = os.path.join(local_dir, name)
            if not os.path.isfile(fp):
                continue
            if name in EXCLUDE_NAMES or name.endswith(EXCLUDE_SUFFIX):
                continue
            if repo_prefix == 'src/westock' and name.endswith(
                    ('.py', '.sh', '.md', '.json')):
                pass
            elif repo_prefix == 'src/engines' and name.endswith(
                    ('.py', '.sh')):
                pass
            elif repo_prefix == 'src/static' and name.endswith('.html'):
                pass
            elif repo_prefix == 'src/models' and name.endswith(
                    ('.pt', '.json')):
                pass
            else:
                continue
            out.append((fp, f'{repo_prefix}/{name}'))
    # westock/config 下的示例配置
    ex = os.path.join(HERE, 'config', 'feishu.example.json')
    if os.path.exists(ex):
        out.append((ex, 'src/westock/config/feishu.example.json'))
    return out


def main():
    dry = '--dry' in sys.argv
    token = get_token()
    if not token and not dry:
        print('[fatal] 缺 GITHUB_TOKEN 或 /tmp/ghtoken（或项目内 .ghtoken）')
        return 1

    mapping = [
        (os.path.join(PLATFORM, 'westock'), 'src/westock'),
        (PLATFORM, 'src/engines'),
        (os.path.join(PLATFORM, 'static'), 'src/static'),
        (os.path.join(PLATFORM, 'models'), 'src/models'),
    ]
    files = collect(mapping)
    print(f'将同步 {len(files)} 个文件:')
    for fp, rel in files:
        print(f'  {rel:<48} {os.path.getsize(fp)/1024:>8.1f} KB')
    if dry:
        return 0

    H = {'Authorization': f'Bearer {token}', 'User-Agent': 'code-sync',
         'Accept': 'application/vnd.github+json',
         'Content-Type': 'application/json'}
    API = f'https://api.github.com/repos/{OWNER}/{REPO}'

    def req(method, url, body=None, timeout=120, retries=3):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(1, retries + 1):
            try:
                r = urllib.request.Request(url, data=data, headers=H,
                                           method=method)
                with urllib.request.urlopen(r, timeout=timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode('utf-8', 'ignore')[:200]
                if attempt == retries:
                    raise RuntimeError(f'{method} {url} {e.code}: {detail}')
                print(f'  retry {attempt} ({e.code})')
            except Exception:
                if attempt == retries:
                    raise
                print(f'  retry {attempt}')
            time.sleep(2)

    ref = req('GET', f'{API}/git/refs/heads/{BRANCH}')
    base_commit = ref['object']['sha']
    base_tree = req('GET', f'{API}/git/commits/{base_commit}')['tree']['sha']
    print(f'base {base_commit[:8]}')

    def make_blob(item):
        fp, rel = item
        with open(fp, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('ascii')
        res = req('POST', f'{API}/git/blobs',
                  {'content': b64, 'encoding': 'base64'})
        return rel, res['sha']

    items = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(make_blob, it) for it in files]
        for fu in as_completed(futs):
            rel, sha = fu.result()
            items.append({'path': rel, 'mode': '100644',
                          'type': 'blob', 'sha': sha})
            print(f'  blob {rel}')

    tree = req('POST', f'{API}/git/trees',
               {'base_tree': base_tree, 'tree': items})
    commit = req('POST', f'{API}/git/commits',
                 {'message': COMMIT_MSG, 'tree': tree['sha'],
                  'parents': [base_commit]})
    req('PATCH', f'{API}/git/refs/heads/{BRANCH}', {'sha': commit['sha']})
    print(f'同步完成 -> https://github.com/{OWNER}/{REPO}/tree/{BRANCH}/src')
    return 0


if __name__ == '__main__':
    sys.exit(main())