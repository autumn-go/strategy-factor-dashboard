#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当日缠论 HTML 报告发布到 GitHub Pages 公网(供飞书卡片按钮直达)。

为什么用 GitHub REST API 而不是 git push:
  本环境 HTTP 代理对 git 走 CONNECT 到 github.com 不稳, 而 api.github.com 走代理干净。
  (与 crowding / rotation 的 publish_pages.py 同一套方案)

令牌来源(不回显): 1) env GITHUB_TOKEN  2) /tmp/ghtoken
用法:
  python publish_report.py [YYYY-MM-DD]   # 默认取最近一份 output/chan_report_*.html
首次运行会自动: 建公开仓库 chan-report(若不存在) + 开启 Pages。
产出公网地址: https://autumn-go.github.io/chan-report/  (写入 .env 的 CHAN_PAGES_URL)
"""
import os, sys, json, base64, datetime, urllib.request, urllib.error

OWNER = "autumn-go"
REPO = "chan-report"
BASE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE, "output")
URL = f"https://{OWNER}.github.io/{REPO}/"


def get_token():
    t = os.environ.get("GITHUB_TOKEN", "").strip()
    if t:
        return t
    # /tmp 在部分沙箱上下文会缺失, 增加 home 稳定备份路径
    for p in ("/tmp/ghtoken", os.path.expanduser("~/.ghtoken")):
        try:
            if os.path.exists(p):
                t = open(p).read().strip()
                if t:
                    return t
        except Exception:
            pass
    return ""


TOKEN = get_token()
if not TOKEN:
    print("NO TOKEN: set GITHUB_TOKEN or write /tmp/ghtoken", file=sys.stderr)
    sys.exit(1)

H = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "chan-report-publisher",
     "Accept": "application/vnd.github+json", "Content-Type": "application/json"}
API = f"https://api.github.com/repos/{OWNER}/{REPO}"


def call(method, url, body=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=H, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def ensure_repo_and_pages():
    """返回默认分支名; 仓库/Pages 不存在则创建(幂等)。"""
    try:
        info = call("GET", API)
        branch = (info.get("default_branch") or "main")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        info = call("POST", "https://api.github.com/user/repos",
                    {"name": REPO, "description": "缠论三类买点盘后扫描报告(每日自动更新)",
                     "public": True, "auto_init": True}, timeout=60)
        branch = (info.get("default_branch") or "main")
        print(f"[publish] 仓库已创建 {REPO} (branch={branch})")
    try:
        call("GET", f"{API}/pages")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                call("POST", f"{API}/pages", {"source": {"branch": branch, "path": "/"}}, timeout=60)
                print("[publish] GitHub Pages 已开启")
            except Exception as ex:
                print(f"[publish] 开启 Pages 失败(可稍后手动): {ex}")
    return branch


def upload(path, remote, branch):
    html = open(path, "r", encoding="utf-8").read()
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    sha = None
    try:
        sha = call("GET", f"{API}/contents/{remote}?ref={branch}").get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"[publish] GET sha {remote} error {e.code}")
    except Exception as e:
        print(f"[publish] GET sha warn: {e}")
    body = {"message": f"daily update {datetime.date.today().isoformat()}",
            "content": b64, "branch": branch}
    if sha:
        body["sha"] = sha
    for attempt in range(1, 5):
        try:
            res = call("PUT", f"{API}/contents/{remote}", body)
            commit = ((res.get("commit") or {}).get("sha") or "?")[:10]
            print(f"[publish] {remote} published OK (commit {commit})")
            return True
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:120]
            print(f"[publish] {remote} HTTP {e.code}: {detail}")
        except Exception as e:
            print(f"[publish] {remote} attempt {attempt}: {e}")
    return False


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    date = argv[0] if argv else ""
    path = os.path.join(OUT_DIR, f"chan_report_{date}.html") if date else ""
    if not path or not os.path.exists(path):
        cands = sorted([f for f in os.listdir(OUT_DIR)
                        if f.startswith("chan_report_") and f.endswith(".html")])
        if not cands:
            print("[publish] 无报告可发布", file=sys.stderr)
            return 1
        path = os.path.join(OUT_DIR, cands[-1])
        print(f"[publish] 未指定日期, 取最近: {cands[-1]}")
    branch = ensure_repo_and_pages()
    ok = upload(path, "index.html", branch)
    # 附带存一份按日期命名的副本作历史(best-effort)
    try:
        upload(path, os.path.basename(path), branch)
    except Exception:
        pass
    if not ok:
        print("[publish] PUBLISH FAILED", file=sys.stderr)
        return 1
    print(f"[publish] 公网地址: {URL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
