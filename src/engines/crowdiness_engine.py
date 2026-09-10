# -*- coding: utf-8 -*-
"""
行业拥挤度监测引擎

基于研报《行业拥挤度指标》实现：
4个主指标 + 滚动1250日分位数 + 95%阈值 → 4分制打分 → 20日窗口高危判定

指标体系：
- Ind1: 超额收益净值乖离率 (窗长40/60/120)
- Ind2: 超额收益动量 (窗长20/40/60)
- Ind3: 流通市值换手率均值 (窗长5/10/20/40/60)
- Ind4: 换手率乖离度 (窗长120/250)
"""

import sqlite3
import numpy as np
import pandas as pd
import logging
from datetime import datetime

logger = logging.getLogger('crowdiness')

# ==================== 数据库路径 ====================
DB_DIR = '/Volumes/BEANPAPER/data/databases'
INDUSTRY_DB = f'{DB_DIR}/industry.db'
INDEX_DB = f'{DB_DIR}/index_daily.db'
FACTORS_DB_DIR = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/data'

# ==================== 参数配置 ====================
CROWDINESS_PARAMS = {
    'benchmark': '000985.CSI',      # 中证全指
    'quantile_window': 1250,         # 滚动分位数窗口(5年)
    'threshold': 0.95,               # 分位数阈值
    # alert_level 直接映射 total_score: 0=安全, 1=起势, 2=上升, 3=加速, 4=高潮
    # 以下旧参数保留兼容但不再使用
    'alert_window': 20,              # (旧) 高危观察窗口
    'alert_threshold': 3,            # (旧) 单日拥挤度阈值
    'alert_count': 2,                # (旧) 窗口内触发次数阈值
    'ind1_windows': [40, 60, 120],   # 超额净值乖离率窗长
    'ind1_trigger': 2,               # 需要触发的窗数
    'ind2_windows': [20, 40, 60],    # 超额收益动量窗长
    'ind2_trigger': 2,
    'ind3_windows': [5, 10, 20, 40, 60],  # 换手率均值窗长
    'ind3_trigger': 3,
    'ind4_windows': [120, 250],      # 换手率乖离度窗长
    'ind4_trigger': 1,
}


def load_data(start_date='20200102', end_date=None):
    """加载行业日线、基准日线、行业列表"""
    conn_ind = sqlite3.connect(INDUSTRY_DB)
    conn_idx = sqlite3.connect(INDEX_DB)

    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d')

    # 881行业列表
    industry_df = pd.read_sql_query(
        "SELECT ts_code, name FROM ths_index WHERE ts_code LIKE '8811%'",
        conn_ind
    )
    industry_codes = industry_df['ts_code'].tolist()
    industry_names = dict(zip(industry_df['ts_code'], industry_df['name']))

    # 行业日线(需要更早的数据做MA/RSI预热，所以从2020开始全量加载)
    industry_daily_df = pd.read_sql_query("""
        SELECT ts_code, trade_date, close, pct_chg, turnover_rate, vol, amount
        FROM ths_daily
        WHERE ts_code LIKE '8811%%'
        ORDER BY ts_code, trade_date
    """, conn_ind)

    # 基准指数日线(中证全指)
    benchmark_df = pd.read_sql_query(f"""
        SELECT ts_code, trade_date, close
        FROM daily
        WHERE ts_code = '{CROWDINESS_PARAMS['benchmark']}'
        ORDER BY trade_date
    """, conn_idx)

    conn_ind.close()
    conn_idx.close()

    # 数据清洗
    industry_daily_df['trade_date'] = industry_daily_df['trade_date'].astype(str)
    benchmark_df['trade_date'] = benchmark_df['trade_date'].astype(str)

    # 换手率null用前值填充(同行业内)
    industry_daily_df['turnover_rate'] = (
        industry_daily_df.groupby('ts_code')['turnover_rate']
        .transform(lambda x: x.ffill())
    )

    logger.info(f"数据加载完成: {len(industry_codes)}行业, {len(industry_daily_df)}条行业日线, {len(benchmark_df)}条基准日线")

    return industry_codes, industry_names, industry_daily_df, benchmark_df


def compute_relative_strength(industry_daily_df, benchmark_df):
    """
    计算超额收益净值 RS = P_i / P_b
    返回: DataFrame with columns [trade_date, ts_code, rs, close, turnover_rate]
    """
    # 行业收盘价透视表
    ind_pivot = industry_daily_df.pivot_table(
        index='trade_date', columns='ts_code', values='close'
    )
    # 基准收盘价序列
    bench_series = benchmark_df.set_index('trade_date')['close']

    # 对齐日期
    common_dates = ind_pivot.index.intersection(bench_series.index)
    ind_pivot = ind_pivot.loc[common_dates]
    bench_aligned = bench_series.loc[common_dates]

    # RS = P_i / P_b
    rs_df = ind_pivot.div(bench_aligned, axis=0)

    return rs_df, common_dates


def compute_indicators(rs_df, turnover_pivot, params):
    """
    计算4个主指标的所有窗长版本
    rs_df: 行业x日期的RS矩阵
    turnover_pivot: 行业x日期的换手率矩阵
    返回: dict of {indicator_name: DataFrame}
    """
    results = {}

    # Ind1: 超额收益净值乖离率 = RS_t / RS_MA(N) - 1
    for n in params['ind1_windows']:
        col_name = f'ind1_n{n}'
        rs_ma = rs_df.rolling(window=n, min_periods=n).mean()
        results[col_name] = rs_df / rs_ma - 1.0
        logger.info(f"  计算 {col_name} 完成")

    # Ind2: 超额收益动量 = RS_t / RS_{t-N} - 1
    for n in params['ind2_windows']:
        col_name = f'ind2_n{n}'
        results[col_name] = rs_df / rs_df.shift(n) - 1.0
        logger.info(f"  计算 {col_name} 完成")

    # Ind3: 换手率均值 = mean(TR, N)
    for n in params['ind3_windows']:
        col_name = f'ind3_n{n}'
        results[col_name] = turnover_pivot.rolling(window=n, min_periods=n).mean()
        logger.info(f"  计算 {col_name} 完成")

    # Ind4: 换手率乖离度 = TR_t / TR_MA(N) - 1
    for n in params['ind4_windows']:
        col_name = f'ind4_n{n}'
        tr_ma = turnover_pivot.rolling(window=n, min_periods=n).mean()
        results[col_name] = turnover_pivot / tr_ma - 1.0
        logger.info(f"  计算 {col_name} 完成")

    return results


def compute_rolling_quantile_flags(indicators, params):
    """
    计算滚动1250日分位数，判断是否超过95%阈值
    返回: (flags, quantiles)
      flags: dict of {indicator_name: DataFrame of flags (0/1)}
      quantiles: dict of {indicator_name: DataFrame of percentile values (0~1)}
    """
    flags = {}
    quantiles = {}
    qw = params['quantile_window']
    threshold = params['threshold']

    for name, df in indicators.items():
        # 使用 rolling.rank(pct=True) 高效计算滚动百分位
        # 每列独立计算
        quantile_df = df.rolling(window=qw, min_periods=qw).rank(pct=True)
        quantiles[name] = quantile_df
        flags[name] = (quantile_df >= threshold).astype(int)
        logger.info(f"  分位数判定 {name} 完成")

    return flags, quantiles


def compute_crowdiness_scores(flags, params):
    """
    汇总各指标flag → 4分制拥挤度打分
    返回: DataFrame (industry_code x trade_date, total_score + 4个子分数)
    """
    # 取所有行业的并集日期
    all_dates = None
    all_codes = None
    for df in flags.values():
        if all_dates is None:
            all_dates = df.index
            all_codes = df.columns
        else:
            all_dates = all_dates.intersection(df.index)
            all_codes = all_codes.intersection(df.columns)

    score_df = pd.DataFrame(index=all_dates, columns=all_codes)

    # Score1: Ind1 触发
    ind1_flags = [flags[f'ind1_n{n}'] for n in params['ind1_windows']]
    score1 = sum(ind1_flags)
    score1 = (score1 >= params['ind1_trigger']).astype(int)
    score1 = score1.reindex(index=all_dates, columns=all_codes)

    # Score2: Ind2 触发
    ind2_flags = [flags[f'ind2_n{n}'] for n in params['ind2_windows']]
    score2 = sum(ind2_flags)
    score2 = (score2 >= params['ind2_trigger']).astype(int)
    score2 = score2.reindex(index=all_dates, columns=all_codes)

    # Score3: Ind3 触发
    ind3_flags = [flags[f'ind3_n{n}'] for n in params['ind3_windows']]
    score3 = sum(ind3_flags)
    score3 = (score3 >= params['ind3_trigger']).astype(int)
    score3 = score3.reindex(index=all_dates, columns=all_codes)

    # Score4: Ind4 触发
    ind4_flags = [flags[f'ind4_n{n}'] for n in params['ind4_windows']]
    score4 = sum(ind4_flags)
    score4 = (score4 >= params['ind4_trigger']).astype(int)
    score4 = score4.reindex(index=all_dates, columns=all_codes)

    total_score = score1 + score2 + score3 + score4

    return total_score, score1, score2, score3, score4


def compute_alert_status(total_score_df, params):
    """
    每日动态警戒等级 = total_score 直接映射：
    0=安全, 1=起势, 2=上升, 3=加速, 4=高潮
    total_score 本身就是 0~4 的4分制打分，直接作为 alert_level
    """
    alert_df = total_score_df.fillna(0).astype(int)
    # 确保值域在 0~4
    alert_df = alert_df.clip(0, 4)

    return alert_df


def run_crowdiness_calculation(start_date='20200102', end_date=None, progress_cb=None):
    """
    运行完整拥挤度计算
    """
    params = CROWDINESS_PARAMS
    t0 = __import__('time').time()

    # Step 1: 加载数据
    if progress_cb:
        progress_cb(0, 6, '加载数据')

    industry_codes, industry_names, industry_daily_df, benchmark_df = load_data(start_date, end_date)
    logger.info(f"加载完成, 耗时{__import__('time').time()-t0:.1f}s")

    # Step 2: 计算RS
    if progress_cb:
        progress_cb(1, 6, '计算超额收益净值')

    rs_df, common_dates = compute_relative_strength(industry_daily_df, benchmark_df)

    # 换手率透视表
    turnover_pivot = industry_daily_df.pivot_table(
        index='trade_date', columns='ts_code', values='turnover_rate'
    )
    # 对齐到RS的日期
    turnover_pivot = turnover_pivot.reindex(index=common_dates)
    logger.info(f"RS计算完成, 耗时{__import__('time').time()-t0:.1f}s")

    # Step 3: 计算4个指标
    if progress_cb:
        progress_cb(2, 6, '计算拥挤度指标')

    indicators = compute_indicators(rs_df, turnover_pivot, params)
    logger.info(f"指标计算完成, 耗时{__import__('time').time()-t0:.1f}s")

    # Step 4: 滚动分位数 + 阈值判定
    if progress_cb:
        progress_cb(3, 6, '滚动分位数判定')

    flags, quantiles = compute_rolling_quantile_flags(indicators, params)
    logger.info(f"分位数判定完成, 耗时{__import__('time').time()-t0:.1f}s")

    # Step 5: 打分
    if progress_cb:
        progress_cb(4, 6, '拥挤度打分')

    total_score, score1, score2, score3, score4 = compute_crowdiness_scores(flags, params)

    # 高危判定
    alert_df = compute_alert_status(total_score, params)
    logger.info(f"打分完成, 耗时{__import__('time').time()-t0:.1f}s")

    # Step 6: 保存
    if progress_cb:
        progress_cb(5, 6, '保存数据')

    # 转为长格式保存
    trade_dates = common_dates.tolist()
    # 过滤到目标日期范围
    if start_date:
        trade_dates = [d for d in trade_dates if d >= start_date]
    if end_date:
        trade_dates = [d for d in trade_dates if d <= end_date]

    records = []
    for td in trade_dates:
        if td not in total_score.index:
            continue
        for code in industry_codes:
            if code not in total_score.columns:
                continue
            ts = total_score.loc[td, code]
            if pd.isna(ts):
                continue
            s1 = score1.loc[td, code] if td in score1.index and code in score1.columns else 0
            s2 = score2.loc[td, code] if td in score2.index and code in score2.columns else 0
            s3 = score3.loc[td, code] if td in score3.index and code in score3.columns else 0
            s4 = score4.loc[td, code] if td in score4.index and code in score4.columns else 0
            alert = alert_df.loc[td, code] if td in alert_df.index and code in alert_df.columns else 0

            # 指标原始值
            ind_values = {}
            for name, df in indicators.items():
                val = df.loc[td, code] if td in df.index and code in df.columns else None
                ind_values[name] = round(val, 6) if pd.notna(val) else None

            # 分位数值
            q_values = {}
            for name, df in quantiles.items():
                val = df.loc[td, code] if td in df.index and code in df.columns else None
                q_values[name] = round(val, 6) if pd.notna(val) else None

            records.append({
                'trade_date': td,
                'industry_code': code,
                'industry_name': industry_names.get(code, ''),
                'total_score': int(ts),
                'score1_deviation': int(s1) if pd.notna(s1) else 0,
                'score2_momentum': int(s2) if pd.notna(s2) else 0,
                'score3_turnover': int(s3) if pd.notna(s3) else 0,
                'score4_tr_bias': int(s4) if pd.notna(s4) else 0,
                'alert_level': int(alert) if pd.notna(alert) else 0,
                'ind1_n40': ind_values.get('ind1_n40'),
                'ind1_n60': ind_values.get('ind1_n60'),
                'ind1_n120': ind_values.get('ind1_n120'),
                'ind2_n20': ind_values.get('ind2_n20'),
                'ind2_n40': ind_values.get('ind2_n40'),
                'ind2_n60': ind_values.get('ind2_n60'),
                'ind3_n5': ind_values.get('ind3_n5'),
                'ind3_n10': ind_values.get('ind3_n10'),
                'ind3_n20': ind_values.get('ind3_n20'),
                'ind3_n40': ind_values.get('ind3_n40'),
                'ind3_n60': ind_values.get('ind3_n60'),
                'ind4_n120': ind_values.get('ind4_n120'),
                'ind4_n250': ind_values.get('ind4_n250'),
                'q1_n40': q_values.get('ind1_n40'),
                'q1_n60': q_values.get('ind1_n60'),
                'q1_n120': q_values.get('ind1_n120'),
                'q2_n20': q_values.get('ind2_n20'),
                'q2_n40': q_values.get('ind2_n40'),
                'q2_n60': q_values.get('ind2_n60'),
                'q3_n5': q_values.get('ind3_n5'),
                'q3_n10': q_values.get('ind3_n10'),
                'q3_n20': q_values.get('ind3_n20'),
                'q3_n40': q_values.get('ind3_n40'),
                'q3_n60': q_values.get('ind3_n60'),
                'q4_n120': q_values.get('ind4_n120'),
                'q4_n250': q_values.get('ind4_n250'),
            })

    logger.info(f"保存 {len(records)} 条记录, 耗时{__import__('time').time()-t0:.1f}s")

    if progress_cb:
        progress_cb(6, 6, '完成')

    return records


def run_crowdiness_incremental(progress_cb=None):
    """增量计算：只计算已有数据之后的新日期"""
    factors_db = f'{FACTORS_DB_DIR}/factors.db'
    conn = sqlite3.connect(factors_db)
    row = conn.execute(
        "SELECT MAX(trade_date) FROM crowdiness_daily"
    ).fetchone()
    conn.close()

    last_date = row[0] if row and row[0] else None
    if last_date is None:
        # 无历史数据，走全量
        return run_crowdiness_calculation(progress_cb=progress_cb)

    # 增量：从上次数据的下一天开始
    # 但由于分位数需要1250日窗口，我们仍需加载全量数据
    # 只是保存时只保存新日期的记录
    start_date = '20200102'  # 全量加载用于分位数计算
    records = run_crowdiness_calculation(start_date=start_date, progress_cb=progress_cb)

    # 过滤只保留新日期
    new_records = [r for r in records if r['trade_date'] > last_date]
    logger.info(f"增量计算: 总{len(records)}条, 新增{len(new_records)}条 (已有数据到{last_date})")

    return new_records
