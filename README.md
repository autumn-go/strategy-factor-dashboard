# A 股策略因子平台

一套面向 A 股的**行业 / 板块层面**量化策略集合，覆盖情绪、动量、轮动、拥挤度、形态学等维度，
每日自动计算信号 → 生成 HTML 报告 → 发布公网 → 推送飞书。

数据源为**腾讯自选股 / 同花顺（经 westock CLI 官方通道）**，不依赖 Tushare / Wind 付费接口。

---

## 一、策略模块

| 模块 | 引擎 | 说明 |
|---|---|---|
| **EW-SDM 情绪加权扩散动量** | `ews_engine.py` | 基于同花顺概念 / 行业板块，用情绪加权扩散动量刻画板块强度，日度排名 |
| **BOCI 五维行业情绪** | `boci_engine.py` | 中银国际《A股情绪指标体系》实现。F1 多头占比 / F2 RSI / F3 换手强度 / F4 涨跌停情绪差 / F5 成交额占比，截面排序 + 过热剔除 |
| **LC-KNN 洛伦兹分类** | `lc_engine.py` | Range Filter 趋势跟踪 + 双层入场过滤 + 停滞离场 + ATR 止盈止损（只做多，小盘指数） |
| **行业拥挤度监测** | `crowdiness_engine.py` | 4 主指标 + 滚动 1250 日分位数 + 95% 阈值 → 4 分制打分 → 20 日窗口高危判定 |
| **宽基指数情绪** | `index_sentiment_engine.py` | 上证 / 沪深300 / 中证1000 / 中证2000 四宽基，5 子指标等权合成情绪 |
| **形态学扫描** | `morphology_engine.py` | 赚钱效应等形态学因子 + 策略信号打标（同花顺一级行业） |
| **RF 趋势择时** | `rf_engine.py` | Range Filter，PineScript v5 版本还原（**当前日报已弃用**，保留供参考） |
| **RRG 相对轮动 + NN** | `rrg_engine.py` / `rrg_nn_model.py` | 相对轮动图（日 / 周频）+ 神经网络打分模型 |
| **缠论三类买点** | `chan-lun/core/chan_scan.py` | czsc 笔序列 + 自研中枢/一买二买三买判定，沪深300+中证500+中证1000 全市场盘后扫描（**独立工具链**，见 `src/chan-lun/`） |

### 每日信号链路（`src/westock/`）

| 脚本 | 职责 |
|---|---|
| `westock_source.py` | **数据适配层**：westock CLI 批量拉 K 线、缓存、静态映射复用 |
| `run_latest.py` | 计算当日 EW-SDM / LC 信号 → `output/latest_signals.json` |
| `boci_backfill.py` | BOCI 五维行业情绪回填（90 个 881 一级行业 × 全历史） |
| `boci_history.py` | 计算行业情绪**机会池**（60 日百分位 → 超跌/超买判定） |
| `boci_view.py` | 生成 BOCI 行业情绪**历史交互页**（双轴图：sentiment vs 收盘价） |
| `etf_mapping.py` | 把 Top 板块 / 行业映射到**可买场内 ETF**（走 westock 搜索 + 流动性过滤） |
| `build_report.py` | 渲染 HTML 日报（ECharts） |
| `publish_pages.py` | 发布到 GitHub Pages（Contents API） |
| `publish_code.py` | 把源码同步到本开源仓库（Git Data API，支持 `--dry`） |
| `feishu_push.py` | 推送飞书交互卡片（含跳转按钮） |
| `daily_run.sh` | 一键跑批（8 步） |

### 缠论三类买点扫描器（`src/chan-lun/`）

独立的**个股层面**盘后扫描工具链（不依赖本平台的行业数据），链路：
**腾讯日K → czsc 0.8.30 生成笔序列 → 自研中枢/一买二买三买判定 → A/B/C 分级 → HTML 报告 → 飞书推送**。

| 文件 | 职责 |
|---|---|
| `run_daily.py` / `run_daily.sh` | 主入口（交易日判断 → 抓K线 → 扫描 → 报告 → 发布 → 推送） |
| `core/config.py` | 全部判据参数（买点阈值、新鲜窗口、流动性下限，可用环境变量覆盖） |
| `core/fetch_kline.py` | 腾讯日K抓取（14 并发，前复权） |
| `core/chan_scan.py` | 缠论核心：czsc `bi_list` 笔 + 自研中枢与三类买点 |
| `core/report_gen.py` | HTML 报告 + 纯文本摘要 |
| `setup.sh` / `update_pool.sh` | 一键初始化 / 重建股票池（westock 官方成分股接口） |
| `publish_report.py` | 报告发布到 `autumn-go/chan-report` 的 GitHub Pages |
| `push_feishu.py` | 飞书推送（交互卡片 / 文本 / bot 三通道） |

> 判据速览：**一买**=底背驰（力度衰减 ≤ 前段 0.92）+ 新鲜窗口 ≤6 交易日；**二买**=显著低点后反弹 ≥8% 且回撤 ≤ 上涨段 75%；**三买**=突破中枢上沿后回抽不破。
> czsc 用 **0.8.30 `--no-deps`** 作笔序列引擎（0.9.x 依赖装不上、1.0.x 移除了所需 API），中枢/买点判定全为自研。

---

## 二、目录结构

```
├── index.html                 # Pages 首页（脚本自动发布，勿手改）
├── boci_industries.html       # BOCI 行业情绪交互页（脚本自动发布）
├── report_YYYYMMDD.html       # 每日归档
├── src/
│   ├── westock/               # 数据源 + 每日流水线脚本
│   │   ├── westock_source.py
│   │   ├── run_latest.py
│   │   ├── boci_backfill.py
│   │   ├── boci_history.py
│   │   ├── boci_view.py
│   │   ├── etf_mapping.py
│   │   ├── full_backfill.py   # 全量因子回填 + 回测
│   │   ├── build_report.py
│   │   ├── publish_pages.py   # 发布 HTML 到 GitHub Pages
│   │   ├── publish_code.py    # 同步源码到本仓库
│   │   ├── feishu_push.py
│   │   ├── feishu_preview.py  # 本地预览飞书卡片样式
│   │   ├── daily_run.sh
│   │   └── config/
│   │       └── feishu.example.json
│   ├── engines/               # 策略引擎（可独立调用）
│   ├── chan-lun/              # 缠论三类买点盘后扫描器（独立工具链）
│   │   ├── run_daily.py       # 主入口
│   │   ├── core/              # config / fetch_kline / chan_scan / report_gen
│   │   ├── push_feishu.py     # 飞书推送（交互卡片）
│   │   ├── publish_report.py  # 发布到 chan-report 仓库 Pages
│   │   ├── pool/pool.tsv      # 股票池（沪深300+中证500+中证1000）
│   │   └── .env.example       # 推送 / 发布配置模板
│   ├── static/                # 原平台前端页面
│   └── models/                # RRG 神经网络权重
└── README.md
```

---

## 三、数据依赖

代码**不含数据**，需自备以下资源（可通过 westock CLI 或其它渠道获取）：

| 路径 | 内容 | 说明 |
|---|---|---|
| `local_dbs/industry.db` | `ths_index`（板块清单）、`ths_member`（成分股关系） | 静态映射，不随行情变 |
| `local_dbs/stock_daily.db` | 股票清单与日线 | 全市场标的 |
| `cache/kline.pkl` | 行情缓存（pandas pickle） | 脚本自动生成 |
| `cache/factors.pkl` | 因子 / 情绪 / 回测结果归档 | 脚本自动生成 |

> **注意**：本项目在沙箱环境下发现 SQLite 写入 >20 万行会报 `disk I/O error`，
> 因此**大批量行情统一用 `pandas.to_pickle`**，SQLite 只存万行级结果表。

### westock CLI（行情通道）

```bash
npm i westock-data-skillhub
# 批量 K 线（50 只/批约 2.4s）
node node_modules/westock-data-skillhub/index.js kline sh600519,sz000001 --period day \
     --start 2026-01-01 --end 2026-09-10
```

---

## 四、快速开始

### 1. 环境

```bash
pip install pandas numpy requests
# 可选：发布 / 推送
export GITHUB_TOKEN=ghp_xxx          # 或用你自己的 GitHub PAT
```

若 node / westock CLI 不在默认位置，用环境变量指定：

```bash
export NODE_BIN=/usr/local/bin/node
export WESTOCK_CLI_JS=/path/to/node_modules/westock-data-skillhub/index.js
export PYTHON_BIN=/path/to/python3
```

### 2. 配置飞书推送（可选）

```bash
cp src/westock/config/feishu.example.json src/westock/config/feishu.json
# 填入你自己的群机器人 webhook 与 dashboardUrl
```

### 3. 跑一次

```bash
cd src/westock
bash daily_run.sh 150        # 参数 = 行情回溯天数
```

单步执行示例：

```bash
python3 run_latest.py        # 算信号
python3 build_report.py      # 出 HTML
python3 feishu_push.py --dry # 只看推送内容
```

> 脚本内的数据库路径常量（如 `LOCAL_DB_DIR`）按你本机布局调整即可，
> `westock_source.py` 的 `BASE_DIR` 已相对文件位置自适应。

### 4. 缠论扫描器（可选，独立工具链）

```bash
cd src/chan-lun
bash setup.sh          # 建 venv（czsc 0.8.30 --no-deps）+ 重建股票池 + 冒烟
cp .env.example .env   # 填飞书 webhook / CHAN_PAGES_URL
bash run_daily.sh      # 盘后跑一次（非交易日自动跳过）
```

venv 定位优先级 `CHAN_VENV` → `~/.workbuddy/.../envs/chan09b` → 项目内 `.venv`，
可用 `CHAN_PYTHON` 指定建 venv 的 python3。详见 `src/chan-lun/README.md`。

---

## 五、每日流水线（8 步）

```
1. refresh_universe()      用 westock 更新全市场日线行情
2. run_latest.py           EW-SDM / LC-KNN 当日信号
3. boci_backfill.py        BOCI 五维行业情绪（90 个 881 行业）
4. boci_history.py         行业情绪机会池（60 日百分位）
   boci_view.py            行业情绪历史交互页
5. etf_mapping.py          每日可买 ETF 清单
6. build_report.py         HTML 日报
7. publish_pages.py        发布 GitHub Pages
8. feishu_push.py          推送飞书交互卡片
```

### 情绪机会池判定规则

- 当前 `sentiment` 在过去 **60 个交易日**的百分位 `pct60`
- `pct60 ≤ 15%` 或 `sentiment < 0.30` → **超跌机会**（情绪底部）
- `pct60 ≥ 85%` 或 `sentiment > 0.70` → **超买风险**（情绪顶部）

---

## 六、回测口径（务必注意）

`full_backfill.py` 内置 EW-SDM Top10 等权回测，**结果偏乐观**，原因：

1. 板块成分股是**静态快照**，含前视偏差；
2. 板块日收益用成分股**等权聚合**，非官方市值加权，小盘权重被放大；
3. **未计**交易成本与冲击成本；
4. 回测区间小盘题材风格占优。

建议只看**相对基准的超额**（全板块等权 / 上证指数），而非绝对收益。

---

## 七、免责声明

本项目仅供**研究与技术交流**，不构成任何投资建议。市场有风险，投资需谨慎。
数据来源为公开行情接口，请遵守相应服务条款。
