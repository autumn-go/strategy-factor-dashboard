# -*- coding: utf-8 -*-
"""
RRG（Relative Rotation Graph）相对轮动图计算引擎

支持日频和周频两种计算模式。

日频 RRG:
  RS = 板块日收益 / 基准日收益（累积）
  RS-Ratio = (RS / RS_EMA(N)) × 100
  RS-Momentum = (RS-Ratio / RS-Ratio_EMA(N)) × 100
  日频 EMA 周期 = 周频周期 × 5（约每周期 5 个交易日）

四象限：
  领涨(Q0: RS-R>100, RS-M>100) → 转弱(Q1: RS-R>100, RS-M<100)
  → 落后(Q2: RS-R<100, RS-M<100) → 改善(Q3: RS-R<100, RS-M>100)

数据源：BEANPAPER 磁盘 SQLite 数据库
- 板块价格: industry.db / ths_daily (close, pct_chg)
- 基准指数: index_daily.db / daily (close, pct_chg), 默认沪深300 (000300.SH)
"""

import sqlite3
import numpy as np
import pandas as pd
import logging
import os

logger = logging.getLogger('rrg_engine')

# ==================== 数据库路径 ====================
DB_DIR = '/Volumes/BEANPAPER/data/databases'
STOCK_DB = os.path.join(DB_DIR, 'stock_daily.db')
INDUSTRY_DB = os.path.join(DB_DIR, 'industry.db')
INDEX_DAILY_DB = os.path.join(DB_DIR, 'index_daily.db')

FACTORS_DB = os.path.join(
    os.path.dirname(__file__), 'data', 'factors.db'
)

# ==================== 默认参数 ====================
# 周频参数
WEEKLY_PARAMS = {
    'rs_ma_period': 10,        # RS-Ratio 中的 MA 周期（周）
    'momentum_ma_period': 10,  # RS-Momentum 中的 MA 周期（周）
    'baseline_code': '000300.SH',
}

# 日频参数（EMA 周期 = 周频 × 5）
DAILY_PARAMS = {
    'rs_ema_period': 50,        # RS-Ratio 的 EMA 周期（日，≈10周）
    'momentum_ema_period': 50,  # RS-Momentum 的 EMA 周期（日）
    'baseline_code': '000300.SH',
    'angle_lookback': 20,       # 轨迹角度回看天数（≈4周）
    'vel_long_lookback': 40,    # 长期速度回看天数（≈8周，用于加速度）
}


def load_daily_prices(start_date='20240101', end_date=None):
    """
    加载板块和基准的日频价格数据。

    返回:
        sector_daily: DataFrame[ts_code, trade_date, close, pct_chg, amount, turnover_rate, vol]
        baseline_daily: DataFrame[trade_date, close, pct_chg] (基准指数)
    """
    conn_sector = sqlite3.connect(INDUSTRY_DB)
    conn_idx = sqlite3.connect(INDEX_DAILY_DB)

    if end_date is None:
        end_date = conn_sector.execute(
            "SELECT MAX(trade_date) FROM ths_daily"
        ).fetchone()[0]

    sector_daily = pd.read_sql_query(f"""
        SELECT ts_code, trade_date, close, pct_chg, amount, turnover_rate, vol
        FROM ths_daily
        WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        ORDER BY ts_code, trade_date
    """, conn_sector)
    conn_sector.close()

    baseline_code = DAILY_PARAMS['baseline_code']
    baseline_daily = pd.read_sql_query(f"""
        SELECT trade_date, close, pct_chg
        FROM daily
        WHERE ts_code = '{baseline_code}'
          AND trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        ORDER BY trade_date
    """, conn_idx)
    conn_idx.close()

    # 确保数值类型
    for col in ['close', 'pct_chg', 'amount', 'turnover_rate', 'vol']:
        if col in sector_daily.columns:
            sector_daily[col] = pd.to_numeric(sector_daily[col], errors='coerce')
    for col in ['close', 'pct_chg']:
        baseline_daily[col] = pd.to_numeric(baseline_daily[col], errors='coerce')

    logger.info(f"日频数据: {len(sector_daily['ts_code'].unique())} 板块, "
                f"{sector_daily['trade_date'].nunique()} 天, "
                f"{sector_daily['trade_date'].min()} ~ {sector_daily['trade_date'].max()}")

    return sector_daily, baseline_daily


def compute_rrg_daily(sector_daily, baseline_daily,
                      rs_ema_period=50, momentum_ema_period=50,
                      angle_lookback=20, vel_long_lookback=40):
    """
    计算所有板块的日频 RRG 指标。

    参数:
        sector_daily: 板块日频数据
        baseline_daily: 基准日频数据
        rs_ema_period: RS-Ratio 的 EMA 周期（日）
        momentum_ema_period: RS-Momentum 的 EMA 周期（日）
        angle_lookback: 轨迹角度回看天数
        vel_long_lookback: 长期速度回看天数

    返回:
        DataFrame: 日频 RRG 指标
    """
    # 基准日收益率（pct_chg 已经是百分比，如 1.5 表示 1.5%）
    baseline_sorted = baseline_daily.sort_values('trade_date').reset_index(drop=True)
    baseline_dates = set(baseline_sorted['trade_date'].tolist())

    # 基准日收益（百分比→小数）
    baseline_ret_map = dict(zip(
        baseline_sorted['trade_date'],
        baseline_sorted['pct_chg'] / 100.0
    ))

    all_results = []

    for ts_code, group in sector_daily.groupby('ts_code'):
        group = group.sort_values('trade_date').reset_index(drop=True)

        # 确保数值类型
        group['pct_chg'] = pd.to_numeric(group['pct_chg'], errors='coerce') / 100.0

        min_required = rs_ema_period + momentum_ema_period + vel_long_lookback + 10
        if len(group) < min_required:
            continue

        # 对齐基准日期
        group = group[group['trade_date'].isin(baseline_dates)].copy()
        group = group.sort_values('trade_date').reset_index(drop=True)

        if len(group) < min_required:
            continue

        # 获取基准日收益
        group['base_ret'] = group['trade_date'].map(baseline_ret_map)

        # 计算累计 RS
        # RS(t) = RS(t-1) * (1 + sector_ret) / (1 + base_ret)
        rs_values = [1.0]
        for i in range(1, len(group)):
            s_ret = group.iloc[i]['pct_chg']
            b_ret = group.iloc[i]['base_ret']
            if pd.isna(s_ret) or pd.isna(b_ret):
                rs_values.append(rs_values[-1])
            else:
                rs_values.append(rs_values[-1] * (1 + s_ret) / (1 + b_ret))

        group['rs'] = rs_values

        # RS-Ratio = (RS / RS_EMA(N)) × 100
        group['rs_ema'] = group['rs'].ewm(
            span=rs_ema_period, min_periods=rs_ema_period, adjust=False
        ).mean()
        group['rs_ratio'] = (group['rs'] / group['rs_ema']) * 100

        # RS-Momentum = (RS-Ratio / RS-Ratio_EMA(N)) × 100
        group['rs_ratio_ema'] = group['rs_ratio'].ewm(
            span=momentum_ema_period, min_periods=momentum_ema_period, adjust=False
        ).mean()
        group['rs_momentum'] = (group['rs_ratio'] / group['rs_ratio_ema']) * 100

        # 象限判断
        group['quadrant'] = 0  # 领涨
        group.loc[(group['rs_ratio'] > 100) & (group['rs_momentum'] <= 100), 'quadrant'] = 1  # 转弱
        group.loc[(group['rs_ratio'] <= 100) & (group['rs_momentum'] <= 100), 'quadrant'] = 2  # 落后
        group.loc[(group['rs_ratio'] <= 100) & (group['rs_momentum'] > 100), 'quadrant'] = 3  # 改善

        # 辅助指标
        group['distance_to_center'] = np.sqrt(
            (group['rs_ratio'] - 100) ** 2 + (group['rs_momentum'] - 100) ** 2
        )

        # 轨迹角度和速度（日频版）
        group['angle'] = np.nan
        group['velocity'] = np.nan
        group['acceleration'] = np.nan

        for i in range(angle_lookback, len(group)):
            x1, y1 = group.iloc[i - angle_lookback]['rs_ratio'], group.iloc[i - angle_lookback]['rs_momentum']
            x2, y2 = group.iloc[i]['rs_ratio'], group.iloc[i]['rs_momentum']
            dx, dy = x2 - x1, y2 - y1
            if abs(dx) > 0.001:
                angle = np.degrees(np.arctan2(dy, dx))
            else:
                angle = 90.0 if dy > 0 else -90.0
            velocity = np.sqrt(dx ** 2 + dy ** 2) / angle_lookback
            group.iloc[i, group.columns.get_loc('angle')] = round(angle, 2)
            group.iloc[i, group.columns.get_loc('velocity')] = round(velocity, 4)

        # 加速度
        if len(group) > vel_long_lookback:
            for i in range(vel_long_lookback, len(group)):
                x1, y1 = group.iloc[i - vel_long_lookback]['rs_ratio'], group.iloc[i - vel_long_lookback]['rs_momentum']
                x2, y2 = group.iloc[i]['rs_ratio'], group.iloc[i]['rs_momentum']
                dx, dy = x2 - x1, y2 - y1
                vel_long = np.sqrt(dx ** 2 + dy ** 2) / vel_long_lookback
                vel_short = group.iloc[i]['velocity']
                if not np.isnan(vel_short):
                    acc = vel_short - vel_long
                    group.iloc[i, group.columns.get_loc('acceleration')] = round(acc, 4)

        # 象限持续时间
        group['quadrant_duration'] = 0
        dur = 0
        prev_q = None
        for i in range(len(group)):
            q = group.iloc[i]['quadrant']
            if q == prev_q:
                dur += 1
            else:
                dur = 1
                prev_q = q
            group.iloc[i, group.columns.get_loc('quadrant_duration')] = dur

        # 只保留有效行
        valid = group.dropna(subset=['rs_ratio', 'rs_momentum']).copy()

        results = valid[['ts_code', 'trade_date', 'close', 'amount', 'vol',
                         'rs_ratio', 'rs_momentum', 'rs',
                         'quadrant', 'quadrant_duration',
                         'angle', 'velocity', 'acceleration',
                         'distance_to_center', 'pct_chg', 'base_ret']].copy()

        all_results.append(results)

    if not all_results:
        logger.warning("没有计算到有效的日频 RRG 数据")
        return pd.DataFrame()

    df = pd.concat(all_results, ignore_index=True)
    logger.info(f"日频 RRG 计算完成: {len(df)} 条, {df['ts_code'].nunique()} 板块, "
                f"{df['trade_date'].nunique()} 天")
    return df


def compute_rrg_daily_full(start_date='20240101', end_date=None,
                           rs_ema_period=50, momentum_ema_period=50):
    """
    完整日频 RRG 计算流程。
    """
    params = DAILY_PARAMS.copy()

    logger.info(f"开始日频 RRG 计算: {start_date} ~ {end_date or '最新'}")
    logger.info(f"参数: rs_ema={rs_ema_period}, momentum_ema={momentum_ema_period}")

    sector_daily, baseline_daily = load_daily_prices(start_date, end_date)
    rrg_df = compute_rrg_daily(
        sector_daily, baseline_daily,
        rs_ema_period=rs_ema_period,
        momentum_ema_period=momentum_ema_period,
        angle_lookback=params['angle_lookback'],
        vel_long_lookback=params['vel_long_lookback'],
    )

    if not rrg_df.empty:
        logger.info(f"日频 RRG 数据范围: {rrg_df['trade_date'].min()} ~ {rrg_df['trade_date'].max()}")

    return rrg_df


def save_rrg_daily_to_db(rrg_df, db_path=None):
    """保存日频 RRG 数据到 factors.db 的 rrg_daily 表"""
    if db_path is None:
        db_path = FACTORS_DB

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rrg_daily (
            ts_code TEXT,
            trade_date TEXT,
            close REAL,
            amount REAL,
            vol REAL,
            rs_ratio REAL,
            rs_momentum REAL,
            rs REAL,
            quadrant INTEGER,
            quadrant_duration INTEGER,
            angle REAL,
            velocity REAL,
            acceleration REAL,
            distance_to_center REAL,
            pct_chg REAL,
            base_ret REAL,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)

    conn.execute("DELETE FROM rrg_daily")

    records = rrg_df[['ts_code', 'trade_date', 'close', 'amount', 'vol',
                       'rs_ratio', 'rs_momentum', 'rs',
                       'quadrant', 'quadrant_duration',
                       'angle', 'velocity', 'acceleration',
                       'distance_to_center', 'pct_chg', 'base_ret']].values.tolist()

    conn.executemany("""
        INSERT OR REPLACE INTO rrg_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, records)

    conn.commit()
    conn.close()
    logger.info(f"日频 RRG 数据已保存: {len(records)} 条 -> {db_path}")


# ==================== 周频接口（保留兼容） ====================

def load_weekly_prices(start_date='20200101', end_date=None):
    """加载板块和基准的周频价格数据（兼容旧接口）。"""
    conn_sector = sqlite3.connect(INDUSTRY_DB)
    conn_idx = sqlite3.connect(INDEX_DAILY_DB)

    if end_date is None:
        end_date = conn_sector.execute(
            "SELECT MAX(trade_date) FROM ths_daily"
        ).fetchone()[0]

    sector_daily = pd.read_sql_query(f"""
        SELECT ts_code, trade_date, close, amount, pct_chg
        FROM ths_daily
        WHERE trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        ORDER BY ts_code, trade_date
    """, conn_sector)
    conn_sector.close()

    baseline_code = WEEKLY_PARAMS['baseline_code']
    baseline_daily = pd.read_sql_query(f"""
        SELECT trade_date, close
        FROM daily
        WHERE ts_code = '{baseline_code}'
          AND trade_date >= '{start_date}' AND trade_date <= '{end_date}'
        ORDER BY trade_date
    """, conn_idx)
    conn_idx.close()

    sector_daily['trade_date'] = pd.to_datetime(sector_daily['trade_date'])
    baseline_daily['trade_date'] = pd.to_datetime(baseline_daily['trade_date'])

    sector_daily['year_week'] = sector_daily['trade_date'].dt.isocalendar().year.astype(str) + \
                                 sector_daily['trade_date'].dt.isocalendar().week.astype(str).str.zfill(2)
    baseline_daily['year_week'] = baseline_daily['trade_date'].dt.isocalendar().year.astype(str) + \
                                   baseline_daily['trade_date'].dt.isocalendar().week.astype(str).str.zfill(2)

    sector_weekly = sector_daily.sort_values('trade_date').groupby(
        ['ts_code', 'year_week']
    ).agg({
        'trade_date': 'last',
        'close': 'last',
        'amount': 'sum',
        'pct_chg': 'sum',
    }).reset_index()

    baseline_weekly = baseline_daily.sort_values('trade_date').groupby('year_week').agg({
        'trade_date': 'last',
        'close': 'last',
    }).reset_index()

    sector_weekly = sector_weekly.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    sector_weekly['weekly_ret'] = sector_weekly.groupby('ts_code')['close'].pct_change()

    baseline_weekly = baseline_weekly.sort_values('trade_date').reset_index(drop=True)
    baseline_weekly['weekly_ret'] = baseline_weekly['close'].pct_change()

    return sector_weekly, baseline_weekly


def compute_rrg_ratios(sector_weekly, baseline_weekly, rs_period=10, momentum_period=10):
    """周频 RRG 计算（兼容旧接口）。"""
    baseline_sorted = baseline_weekly.sort_values('trade_date').reset_index(drop=True)
    baseline_dates = set(baseline_sorted['trade_date'].tolist())
    baseline_ret_map = dict(zip(baseline_sorted['trade_date'], baseline_sorted['weekly_ret']))

    all_results = []

    for ts_code, group in sector_weekly.groupby('ts_code'):
        group = group.sort_values('trade_date').copy()
        group['close'] = pd.to_numeric(group['close'], errors='coerce')
        group['weekly_ret'] = pd.to_numeric(group['weekly_ret'], errors='coerce')

        n = len(group)
        if n < rs_period + momentum_period + 5:
            continue

        group = group[group['trade_date'].isin(baseline_dates)].copy()
        group = group.sort_values('trade_date').reset_index(drop=True)

        if len(group) < rs_period + momentum_period + 5:
            continue

        group['base_ret'] = group['trade_date'].map(baseline_ret_map)
        group['base_ret'] = pd.to_numeric(group['base_ret'], errors='coerce')

        rs_values = [1.0]
        for i in range(1, len(group)):
            s_ret = group.iloc[i]['weekly_ret']
            b_ret = group.iloc[i]['base_ret']
            if pd.isna(s_ret) or pd.isna(b_ret):
                rs_values.append(rs_values[-1])
            else:
                rs_values.append(rs_values[-1] * (1 + s_ret) / (1 + b_ret))

        group['rs'] = rs_values
        group['rs_ma'] = group['rs'].rolling(window=rs_period, min_periods=rs_period).mean()
        group['rs_ratio'] = (group['rs'] / group['rs_ma']) * 100
        group['rs_ratio_ma'] = group['rs_ratio'].rolling(window=momentum_period, min_periods=momentum_period).mean()
        group['rs_momentum'] = (group['rs_ratio'] / group['rs_ratio_ma']) * 100

        group['quadrant'] = 0
        group.loc[(group['rs_ratio'] > 100) & (group['rs_momentum'] <= 100), 'quadrant'] = 1
        group.loc[(group['rs_ratio'] <= 100) & (group['rs_momentum'] <= 100), 'quadrant'] = 2
        group.loc[(group['rs_ratio'] <= 100) & (group['rs_momentum'] > 100), 'quadrant'] = 3

        group['distance_to_center'] = np.sqrt(
            (group['rs_ratio'] - 100) ** 2 + (group['rs_momentum'] - 100) ** 2
        )

        group['angle_4w'] = np.nan
        group['velocity_4w'] = np.nan
        group['acceleration'] = np.nan

        for i in range(4, len(group)):
            x1, y1 = group.iloc[i - 4]['rs_ratio'], group.iloc[i - 4]['rs_momentum']
            x2, y2 = group.iloc[i]['rs_ratio'], group.iloc[i]['rs_momentum']
            dx, dy = x2 - x1, y2 - y1
            if abs(dx) > 0.001:
                angle = np.degrees(np.arctan2(dy, dx))
            else:
                angle = 90.0 if dy > 0 else -90.0
            velocity = np.sqrt(dx ** 2 + dy ** 2) / 4
            group.iloc[i, group.columns.get_loc('angle_4w')] = round(angle, 2)
            group.iloc[i, group.columns.get_loc('velocity_4w')] = round(velocity, 4)

        if len(group) > 8:
            for i in range(8, len(group)):
                x1, y1 = group.iloc[i - 8]['rs_ratio'], group.iloc[i - 8]['rs_momentum']
                x2, y2 = group.iloc[i]['rs_ratio'], group.iloc[i]['rs_momentum']
                dx, dy = x2 - x1, y2 - y1
                vel_8w = np.sqrt(dx ** 2 + dy ** 2) / 8
                vel_4w = group.iloc[i]['velocity_4w']
                if not np.isnan(vel_4w):
                    acc = vel_4w - vel_8w
                    group.iloc[i, group.columns.get_loc('acceleration')] = round(acc, 4)

        group['quadrant_duration'] = 0
        dur = 0
        prev_q = None
        for i in range(len(group)):
            q = group.iloc[i]['quadrant']
            if q == prev_q:
                dur += 1
            else:
                dur = 1
                prev_q = q
            group.iloc[i, group.columns.get_loc('quadrant_duration')] = dur

        valid = group.dropna(subset=['rs_ratio', 'rs_momentum']).copy()
        results = valid[['ts_code', 'trade_date', 'close', 'amount',
                         'rs_ratio', 'rs_momentum', 'rs',
                         'quadrant', 'quadrant_duration',
                         'angle_4w', 'velocity_4w', 'acceleration',
                         'distance_to_center', 'weekly_ret', 'base_ret']].copy()
        results['week_end_date'] = results['trade_date'].dt.strftime('%Y%m%d')
        results['trade_date'] = results['trade_date'].dt.strftime('%Y%m%d')

        all_results.append(results)

    if not all_results:
        return pd.DataFrame()

    return pd.concat(all_results, ignore_index=True)


def compute_rrg_full(start_date='20200101', end_date=None,
                     rs_period=10, momentum_period=10):
    """完整周频 RRG 计算流程（兼容旧接口）。"""
    sector_weekly, baseline_weekly = load_weekly_prices(start_date, end_date)
    rrg_df = compute_rrg_ratios(sector_weekly, baseline_weekly, rs_period, momentum_period)
    return rrg_df


def save_rrg_to_db(rrg_df, db_path=None):
    """保存周频 RRG 数据到 factors.db（兼容旧接口）。"""
    if db_path is None:
        db_path = FACTORS_DB

    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rrg_weekly (
            ts_code TEXT,
            trade_date TEXT,
            close REAL,
            amount REAL,
            rs_ratio REAL,
            rs_momentum REAL,
            rs REAL,
            quadrant INTEGER,
            quadrant_duration INTEGER,
            angle_4w REAL,
            velocity_4w REAL,
            acceleration REAL,
            distance_to_center REAL,
            weekly_ret REAL,
            base_ret REAL,
            week_end_date TEXT,
            PRIMARY KEY (ts_code, trade_date)
        )
    """)

    conn.execute("DELETE FROM rrg_weekly")
    records = rrg_df[['ts_code', 'trade_date', 'close', 'amount',
                       'rs_ratio', 'rs_momentum', 'rs',
                       'quadrant', 'quadrant_duration',
                       'angle_4w', 'velocity_4w', 'acceleration',
                       'distance_to_center', 'weekly_ret', 'base_ret',
                       'week_end_date']].values.tolist()
    conn.executemany("""
        INSERT OR REPLACE INTO rrg_weekly VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, records)
    conn.commit()
    conn.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['daily', 'weekly'], default='daily')
    parser.add_argument('--start', default='20240101')
    parser.add_argument('--ema', type=int, default=50)
    args = parser.parse_args()

    if args.mode == 'daily':
        print("=== 日频 RRG 计算 ===\n")
        rrg_df = compute_rrg_daily_full(start_date=args.start, rs_ema_period=args.ema)

        if not rrg_df.empty:
            print(f"\n日频 RRG 结果: {len(rrg_df)} 条, {rrg_df['ts_code'].nunique()} 板块")
            print(f"日期范围: {rrg_df['trade_date'].min()} ~ {rrg_df['trade_date'].max()}")

            # 象限分布
            q_names = ['领涨', '转弱', '落后', '改善']
            print("\n象限分布:")
            for q, cnt in rrg_df['quadrant'].value_counts().sort_index().items():
                print(f"  {q_names[q]}: {cnt} ({cnt/len(rrg_df)*100:.1f}%)")

            # 最新一天
            latest = rrg_df['trade_date'].max()
            latest_df = rrg_df[rrg_df['trade_date'] == latest].copy()
            print(f"\n最新一天 ({latest}) 各象限板块数:")
            for q in range(4):
                n = len(latest_df[latest_df['quadrant'] == q])
                print(f"  {q_names[q]}: {n}")

            # 改善区 Top10
            improving = latest_df[latest_df['quadrant'] == 3].sort_values('velocity', ascending=False)
            if len(improving) > 0:
                print(f"\n改善区板块 Top10 (按速度排序):")
                conn_ind = sqlite3.connect(INDUSTRY_DB)
                for _, row in improving.head(10).iterrows():
                    name = conn_ind.execute(
                        f"SELECT name FROM ths_index WHERE ts_code='{row['ts_code']}'"
                    ).fetchone()
                    name = name[0] if name else row['ts_code']
                    print(f"  {name:15s} RS-R={row['rs_ratio']:.1f} RS-M={row['rs_momentum']:.1f} "
                          f"angle={row['angle']:.1f}° vel={row['velocity']:.2f}")
                conn_ind.close()

            save_rrg_daily_to_db(rrg_df)
            print("\n已保存到 factors.db (rrg_daily)")
    else:
        print("=== 周频 RRG 计算 ===\n")
        rrg_df = compute_rrg_full(start_date=args.start)
        if not rrg_df.empty:
            print(f"周频 RRG: {len(rrg_df)} 条, {rrg_df['trade_date'].min()} ~ {rrg_df['trade_date'].max()}")
            save_rrg_to_db(rrg_df)
            print("已保存到 factors.db (rrg_weekly)")
