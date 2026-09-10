# -*- coding: utf-8 -*-
"""飞书推送封装: bot(lark-cli, 应用身份) 与 webhook(群自定义机器人) 双通道
用法(通常由 run_daily.py 调用):
    from push_feishu import push_text, push_file
    push_text(summary_text, mode="bot")     # 发给配置的群/用户
    push_file(html_path, mode="bot")        # 发送报告文件(仅 bot 支持)
环境变量(config.py 已代理):
    CHAN_FEISHU_MODE      none | bot | webhook
    CHAN_FEISHU_CHAT_ID   bot模式目标群 oc_xxx
    CHAN_FEISHU_OPENID    bot模式目标用户 ou_xxx (与CHAT_ID二选一)
    CHAN_FEISHU_WEBHOOK   webhook模式完整URL
"""
import os, sys, json, time, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import config as C

LARK_CLI = "lark-cli"   # 已在 PATH(WorkBuddy 托管)


def _target_args():
    """返回 (lark-cli 目标参数列表) 或抛错"""
    if C.FEISHU_CHAT_ID:
        return ["--chat-id", C.FEISHU_CHAT_ID]
    if C.FEISHU_USER_OPENID:
        return ["--user-id", C.FEISHU_USER_OPENID]
    raise RuntimeError("未配置目标: 请设置 CHAN_FEISHU_CHAT_ID(群) 或 CHAN_FEISHU_OPENID(用户)")


def _run(cmd, timeout=90):
    """执行 lark-cli, 返回 stdout 文本; 失败抛 RuntimeError(含 stderr)"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"lark-cli 执行失败: {e}")
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()[-800:]
        raise RuntimeError(f"lark-cli 返回 {p.returncode}: {err}")
    return p.stdout


def push_text(text, mode=None):
    """推送纯文本摘要。mode: bot | webhook, 默认取 config.FEISHU_MODE"""
    mode = mode or C.FEISHU_MODE
    if not text:
        return "空内容, 跳过推送"
    if mode == "webhook":
        if not C.FEISHU_WEBHOOK:
            raise RuntimeError("webhook 模式但未配置 CHAN_FEISHU_WEBHOOK")
        import requests
        r = requests.post(C.FEISHU_WEBHOOK, timeout=30,
                          json={"msg_type": "text", "content": {"text": text}})
        if r.status_code != 200 or r.json().get("code") not in (0, None):
            raise RuntimeError(f"webhook 发送失败 http={r.status_code}: {r.text[:300]}")
        return "webhook OK"
    # bot 模式: 用 --text 保留对齐排版; lark-cli 默认 --as bot
    out = _run([LARK_CLI, "im", "+messages-send", *_target_args(),
                "--as", "bot", "--text", text])
    mid = ""
    try:
        mid = json.loads(out).get("message_id", "")
    except Exception:
        pass
    return f"bot OK message_id={mid}"


def push_file(path, mode=None):
    """发送本地文件(HTML报告)。仅 bot 模式支持; webhook 机器人无法传文件。"""
    mode = mode or C.FEISHU_MODE
    if mode != "bot":
        return "webhook 不支持文件推送, 跳过"
    abspath = os.path.abspath(path)
    if not os.path.exists(abspath):
        raise RuntimeError(f"文件不存在: {abspath}")
    # lark-cli 的本地文件必须为 cwd 相对路径且不越界 -> 切到文件所在目录
    d, f = os.path.split(abspath)
    out = _run([LARK_CLI, "im", "+messages-send", *_target_args(),
                "--as", "bot", "--file", "./" + f], timeout=180)
    mid = ""
    try:
        mid = json.loads(out).get("message_id", "")
    except Exception:
        pass
    return f"file OK message_id={mid}"


def push_card(text, url, date=None, market_chg=None):
    """webhook 推送飞书交互卡片(摘要 + 「查看完整HTML报告」按钮直达 GitHub Pages)。
    参考拥挤度/行业轮动推送范式: HTML 发布公网后卡片带按钮跳转。
    """
    if C.FEISHU_MODE != "webhook" or not C.FEISHU_WEBHOOK:
        raise RuntimeError("push_card 仅支持 webhook 模式(需配置 CHAN_FEISHU_WEBHOOK)")
    # 红涨绿跌(中国习惯): 大盘涨=red卡片头, 跌=green
    template = "red" if (market_chg is not None and market_chg >= 0) else "green"
    d = date or time.strftime("%Y-%m-%d")
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text",
                                 "content": f"缠论三类买点 · 盘后扫描 · {d}"},
                       "template": template},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
                {"tag": "hr"},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text",
                                               "content": "📊 查看完整HTML报告"},
                     "type": "primary", "url": url}]},
                {"tag": "note", "elements": [
                    {"tag": "plain_text",
                     "content": "数据源: 腾讯行情 · 缠论结构(笔=czsc 0.8.30) · 仅供研究参考,不构成投资建议"}]}
            ]
        }
    }
    import requests
    r = requests.post(C.FEISHU_WEBHOOK, timeout=30, json=card)
    if r.status_code != 200 or r.json().get("code") not in (0, None):
        raise RuntimeError(f"webhook 卡片发送失败 http={r.status_code}: {r.text[:300]}")
    return "card OK"


if __name__ == "__main__":
    # 自测: python push_feishu.py "hello"
    t = sys.argv[1] if len(sys.argv) > 1 else "缠论扫描器测试消息 OK"
    print(push_text(t))
