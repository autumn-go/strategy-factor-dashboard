# -*- coding: utf-8 -*-
"""缠论盘后扫描 · 共享配置
集中管理路径、指数池、过滤参数与推送配置(可用环境变量覆盖)。
"""
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # ~/chan-scanner
CORE = os.path.join(BASE, "core")
DATA_DIR = os.path.join(BASE, "data")       # 每日K线csv缓存
POOL_TSV = os.path.join(BASE, "pool", "pool.tsv")   # code \t name
OUT_DIR = os.path.join(BASE, "output")
LOG_DIR = os.path.join(BASE, "logs")

# 指数池: 名称 -> 腾讯成分指数代码(westock/腾讯体系)
POOL_INDICES = [
    ("沪深300", "sz399300"),
    ("中证500", "sz399905"),
    ("中证1000", "sz399852"),
]
MARKET_INDEX = "sh000001"     # 上证指数(交易日判断 + 大盘背景)
FETCH_BARS = 500              # 每只日K根数(约2年)
FETCH_WORKERS = 14

# 扫描判据参数(默认值, 可在命令行覆盖)
P_BI_MIN_GAP = None           # czsc内部笔参数,不使用
BUY1_POWER_DECAY = 0.92       # 一买: 力度衰减至前段的该比例以下
BUY2_UP_MIN = 0.08            # 二买: L0后反弹>=8%
BUY2_RETR_MAX = 0.75          # 二买: 回撤不超过上涨段的75%
BUY3_LEAVE = 1.01             # 三买: 离开高点>上沿*1.01
BUY3_FAR = 1.18               # 三买: 回抽低点距上沿上限
FRESH_DAYS = {"BUY1": 6, "BUY2": 5, "BUY3": 6}   # 触发新鲜窗口(交易日)
MIN_AMT20 = 1.0               # 近20日均成交额(亿元)下限
ZS_NEAR = 7                   # 中枢末笔须在最后N笔内

# 飞书推送: mode = none | bot | webhook
FEISHU_MODE = os.environ.get("CHAN_FEISHU_MODE", "none")
FEISHU_WEBHOOK = os.environ.get("CHAN_FEISHU_WEBHOOK", "")
FEISHU_CHAT_ID = os.environ.get("CHAN_FEISHU_CHAT_ID", "")   # bot模式目标群 oc_xxx
FEISHU_USER_OPENID = os.environ.get("CHAN_FEISHU_OPENID", "")  # bot模式私聊目标 ou_xxx
