# 缠论三类买点 · 每日盘后扫描器

把「缠论选股」固化成可每日自动运行的本地工具链:
**腾讯日K(含当日已收盘bar) → czsc 0.8.30 生成笔序列 → 自研中枢/一二三类买点判定 → 分级(A/B/C) → HTML报告 → 飞书推送**

> 开源源码镜像: https://github.com/autumn-go/strategy-factor-dashboard → `src/chan-lun/`
> 每日报告公网地址: https://autumn-go.github.io/chan-report/

## 目录结构

```
chan-scanner/
├── run_daily.py          # 主入口: 交易日判断→抓K线→扫描→报告→发布公网→推送
├── run_daily.sh          # 一键运行(定位 venv + 加载 .env)
├── update_pool.sh        # 重建股票池(westock官方成分股接口)
├── setup.sh              # 一键初始化: venv依赖+建池+冒烟
├── publish_report.py     # HTML报告发布 GitHub Pages(API上传, 生成公网按钮地址)
├── push_feishu.py        # 飞书推送封装(文本 / 交互卡片 / bot文件 三通道)
├── core/
│   ├── config.py         # 全部配置(可用环境变量覆盖)
│   ├── fetch_kline.py    # 腾讯 K 线抓取(14并发)
│   ├── chan_scan.py      # 缠论核心(czsc笔+自研中枢/买点)
│   └── report_gen.py     # HTML报告 + 纯文本摘要
├── pool/pool.tsv         # 股票池: code\tname
├── .env.example          # 配置模板(复制为 .env 填写)
├── data/                 # 每日K线csv缓存
├── output/               # 报告 html/txt/json
└── logs/                 # 运行日志
```

## 安装(一次性)

```bash
bash setup.sh        # 1)建venv装依赖 2)重建股票池 3)冒烟
```

> **czsc 为什么用 0.8.30 `--no-deps`**: 0.9.x 依赖 clickhouse/streamlit/pywebview 等泥潭装不上; 1.0.x 重构掉了自研中枢所需 API。0.8.30 仅作**笔序列引擎**(bi_list), 中枢/买点全为自研。

## 使用

```bash
bash run_daily.sh                    # 今日盘后(非交易日自动跳过, exit 0)
bash run_daily.sh --date 2026-09-09  # 指定日期
bash run_daily.sh --no-push          # 只生成报告不推送
bash run_daily.sh --charts 0         # 报告不嵌K线图(更快,文件更小)
```

## 飞书推送配置(环境变量)

| 变量 | 说明 |
|---|---|
| `CHAN_FEISHU_MODE` | `none`(默认) / `bot` / `webhook` |
| `CHAN_FEISHU_WEBHOOK` | webhook模式完整URL(群自定义机器人) |
| `CHAN_FEISHU_CHAT_ID` / `CHAN_FEISHU_OPENID` | bot模式目标群 / 目标用户 |
| `CHAN_PAGES_URL` | HTML报告公网地址(由 publish_report.py 更新) |
| `CHAN_VENV` / `CHAN_PYTHON` | 自定义 venv 路径 / 建 venv 用的 python3(一般不用设) |

配置直接写进 `.env`(run_daily.sh 自动 source), 模板见 `.env.example`。`venv` 定位优先级:
`$CHAN_VENV` → 本机托管默认路径(`~/.workbuddy/.../envs/chan09b`) → 项目内 `.venv`。

**webhook 模式推送范式**(同 A股拥挤度/行业轮动):
1. 生成自包含 HTML 报告 → `publish_report.py` 经 GitHub Contents API 上传 `index.html` 到公开仓库 `autumn-go/chan-report`(令牌读 `GITHUB_TOKEN` / `/tmp/ghtoken` / `~/.ghtoken`, 不回显)
2. 飞书群收到**交互卡片**(摘要 + 大盘红绿头部), 内含「📊 查看完整HTML报告」按钮直达公网: https://autumn-go.github.io/chan-report/
3. 发布失败自动降级为纯文本推送, 不阻塞当日结果

例: `CHAN_FEISHU_MODE=webhook CHAN_FEISHU_WEBHOOK=... bash run_daily.sh`
建议写入 `.env`(run_daily.sh 自动 source), 避免每次手敲。

## 判据速览(core/config.py 可调)

- **一买**: 底背驰(力度衰减 ≤ 前段的 0.92) + 新鲜窗口 ≤6交易日 + 近20日均额 ≥1亿
- **二买**: 显著低点后反弹 ≥8%、回撤 ≤ 上涨段75%、新鲜窗口5日
- **三买**: 突破中枢上沿后回抽不破(离开高点>上沿, 回抽低点距上沿有限)
- 命中后再按信号强度分级 A(最强)/B/C; 已跌破买点价 >2% 的降为 C。

## 数据通道备注(踩坑记录)

- 本机 npm registry 被指华泰内网(502): **所有 npx 前先** `export npm_config_registry=https://registry.npmjs.org`
- 东财 push2his 对本机出口风控 → 行情统一走**腾讯**: 日K `web.ifzq.gtimg.cn/appstock/app/fqkline/get`(前复权,qfq); 快照 `qt.gtimg.cn/q=`(GBK)
- 指数成分股: **westock**(腾讯微证券官方) `npx westock-data-skillhub index constituent sz399300`, 深市代码格式(sz399905/sz399852)才可用
- requests 需默认走沙箱代理(勿 trust_env=False 直连)
