# -*- coding: utf-8 -*-
"""
RRG-NN 训练数据集构建器（日频版 v2）

改进:
  - 数据起点扩展到 2023 年（RRG 回算到 20230601）
  - 新增交叉特征（RRG × EW-SDM 交互）
  - 新增滞后特征（前 N 日变化率）
  - 新增 y_10d 标签
  - Robust 标准化（中位数 + IQR）

特征组:
  A: RRG 核心特征 (10 dims)
  B: EW-SDM 情绪特征 (8 dims)
  C: 量价特征 (1 dim: vol)
  D: 交叉特征 (4 dims): rs_ratio×final_score, rs_momentum×momentum,
     quadrant×emotion_diff, velocity×s_score
  E: 滞后变化特征 (6 dims): rs_ratio/angle/velocity 的 1d/5d 变化率

总计: 29 维特征
"""

import sqlite3
import numpy as np
import pandas as pd
import logging
import os

logger = logging.getLogger('dataset_builder')

FACTORS_DB = os.path.join(os.path.dirname(__file__), 'data', 'factors.db')
INDUSTRY_DB = '/Volumes/BEANPAPER/data/databases/industry.db'


def load_ews_daily(start_date=None, end_date=None):
    """加载 EW-SDM 日频因子。"""
    conn = sqlite3.connect(FACTORS_DB)

    date_filter = ""
    if start_date:
        date_filter += f" AND trade_date >= '{start_date}'"
    if end_date:
        date_filter += f" AND trade_date <= '{end_date}'"

    ews = pd.read_sql_query(f"""
        SELECT concept_code as ts_code, trade_date,
               final_score, momentum, emotion_diff, s_score,
               total_stocks, up_count, limit_up_count, broken_count, max_streak
        FROM ews_daily
        WHERE 1=1 {date_filter}
        ORDER BY ts_code, trade_date
    """, conn)
    conn.close()

    if ews.empty:
        return pd.DataFrame()

    for col in ['final_score', 'momentum', 'emotion_diff', 's_score',
                'total_stocks', 'up_count', 'limit_up_count', 'broken_count', 'max_streak']:
        ews[col] = pd.to_numeric(ews[col], errors='coerce')

    ews['up_ratio'] = (ews['up_count'] / ews['total_stocks'].replace(0, np.nan)).clip(0, 1)

    return ews


def load_rrg_daily(start_date=None, end_date=None):
    """加载日频 RRG 指标。"""
    conn = sqlite3.connect(FACTORS_DB)

    date_filter = ""
    if start_date:
        date_filter += f" AND trade_date >= '{start_date}'"
    if end_date:
        date_filter += f" AND trade_date <= '{end_date}'"

    rrg = pd.read_sql_query(f"""
        SELECT ts_code, trade_date,
               rs_ratio, rs_momentum, rs,
               quadrant, quadrant_duration,
               angle, velocity, acceleration, distance_to_center,
               pct_chg, close, amount, vol
        FROM rrg_daily
        WHERE 1=1 {date_filter}
        ORDER BY ts_code, trade_date
    """, conn)
    conn.close()

    if rrg.empty:
        return pd.DataFrame()

    for col in ['rs_ratio', 'rs_momentum', 'rs', 'quadrant', 'quadrant_duration',
                'angle', 'velocity', 'acceleration', 'distance_to_center',
                'pct_chg', 'close', 'amount', 'vol']:
        rrg[col] = pd.to_numeric(rrg[col], errors='coerce')

    return rrg


def add_cross_features(merged):
    """
    添加交叉特征：RRG × EW-SDM 交互项。
    """
    # 标准化到 [0,1] 范围再相乘，避免量纲差异过大
    def safe_normalize(series):
        vmin, vmax = series.quantile(0.01), series.quantile(0.99)
        return (series - vmin) / (vmax - vmin + 1e-8)

    rs_r_norm = safe_normalize(merged['rs_ratio'])
    rs_m_norm = safe_normalize(merged['rs_momentum'])
    fs_norm = safe_normalize(merged['final_score'])
    mom_norm = safe_normalize(merged['momentum'])
    ed_norm = safe_normalize(merged['emotion_diff'])
    ss_norm = safe_normalize(merged['s_score'])
    vel_norm = safe_normalize(merged['velocity'])
    q_norm = safe_normalize(merged['quadrant'].astype(float))

    merged['cross_rs_ews'] = rs_r_norm * fs_norm          # 相对强度 × 情绪得分
    merged['cross_mom_mom'] = rs_m_norm * mom_norm        # 动量共振
    merged['cross_quad_emotion'] = q_norm * ed_norm       # 象限 × 情绪变化
    merged['cross_vel_score'] = vel_norm * ss_norm        # 速度 × 评分

    return merged


def add_lag_features(merged):
    """
    添加滞后变化特征：前 N 日的 RRG 特征变化率。
    """
    group = merged.groupby('ts_code')

    # 1日变化率
    for col in ['rs_ratio', 'angle', 'velocity']:
        merged[f'{col}_chg1d'] = group[col].pct_change(1)

    # 5日变化率
    for col in ['rs_ratio', 'angle', 'velocity']:
        merged[f'{col}_chg5d'] = group[col].pct_change(5)

    return merged


def build_daily_dataset(train_start='20240101', test_end=None,
                        seq_len=20, label_horizons=[1, 5, 10],
                        threshold_percentile=60):
    """
    构建日频训练数据集 v2。

    参数:
        train_start: 训练集开始日期
        test_end: 测试集结束日期
        seq_len: 序列长度
        label_horizons: 标签预测窗口
        threshold_percentile: 超额收益正例阈值分位数
    """
    # 数据起点：取 RRG 和 EW-SDM 的交集
    data_start = '20230601'
    rrg = load_rrg_daily(start_date=data_start, end_date=test_end)
    ews = load_ews_daily(start_date=data_start, end_date=test_end)

    if rrg.empty:
        logger.warning("RRG 数据为空")
        return [], None, None, []

    logger.info(f"RRG: {len(rrg)} 条, {rrg['ts_code'].nunique()} 板块, "
                f"{rrg['trade_date'].min()} ~ {rrg['trade_date'].max()}")
    logger.info(f"EW-SDM: {len(ews)} 条, {ews['ts_code'].nunique()} 板块, "
                f"{ews['trade_date'].min()} ~ {ews['trade_date'].max()}")

    # 合并 RRG + EW-SDM
    merged = rrg.merge(ews, on=['ts_code', 'trade_date'], how='inner')
    merged = merged.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    logger.info(f"合并后: {len(merged)} 条, {merged['ts_code'].nunique()} 板块, "
                f"{merged['trade_date'].nunique()} 天")

    # ---- 特征定义 ----
    rrg_features = [
        'rs_ratio', 'rs_momentum', 'rs',
        'quadrant', 'quadrant_duration',
        'angle', 'velocity', 'acceleration', 'distance_to_center',
        'pct_chg'
    ]

    ews_features = [
        'final_score', 'momentum', 'emotion_diff', 's_score',
        'limit_up_count', 'broken_count', 'max_streak', 'up_ratio'
    ]

    amount_features = ['vol']

    # 交叉特征
    cross_features = [
        'cross_rs_ews', 'cross_mom_mom', 'cross_quad_emotion', 'cross_vel_score'
    ]

    # 滞后特征
    lag_features = [
        'rs_ratio_chg1d', 'angle_chg1d', 'velocity_chg1d',
        'rs_ratio_chg5d', 'angle_chg5d', 'velocity_chg5d',
    ]

    all_features = rrg_features + ews_features + amount_features + cross_features + lag_features

    # ---- 工程化特征 ----
    logger.info("构建交叉特征...")
    merged = add_cross_features(merged)

    logger.info("构建滞后特征...")
    merged = add_lag_features(merged)

    # 检查列存在
    missing = [f for f in all_features if f not in merged.columns]
    if missing:
        logger.warning(f"缺失特征列: {missing}")
        all_features = [f for f in all_features if f in merged.columns]

    n_features = len(all_features)
    logger.info(f"特征维度: {n_features} ({len(rrg_features)} RRG + "
                f"{len(ews_features)} EW-SDM + {len(amount_features)} 量价 + "
                f"{len(cross_features)} 交叉 + {len(lag_features)} 滞后)")

    # ---- 计算标签 ----
    for h in label_horizons:
        label_ret_col = f'y_{h}d_return'
        merged[label_ret_col] = (
            merged.groupby('ts_code')['pct_chg']
            .rolling(window=h, min_periods=h)
            .sum()
            .shift(-h)
            .reset_index(level=0, drop=True)
        )

    # ---- 数据集划分 ----
    all_dates = sorted(merged['trade_date'].unique())
    n_dates = len(all_dates)

    # 找到 train_start 对应的索引
    train_start_idx = 0
    for i, d in enumerate(all_dates):
        if d >= train_start:
            train_start_idx = i
            break

    train_end_idx = int(n_dates * 0.7)
    val_end_idx = int(n_dates * 0.85)

    train_dates_set = set(all_dates[train_start_idx:train_end_idx])

    logger.info(f"日期划分: train={all_dates[train_start_idx]}~{all_dates[train_end_idx-1]}, "
                f"val={all_dates[train_end_idx]}~{all_dates[val_end_idx-1]}, "
                f"test={all_dates[val_end_idx]}~{all_dates[-1]}")
    logger.info(f"总天数: {n_dates}, 训练: {train_end_idx - train_start_idx}, "
                f"验证: {val_end_idx - train_end_idx}, 测试: {n_dates - val_end_idx}")

    # ---- 标准化（Robust: 中位数 + IQR） ----
    train_data = merged[merged['trade_date'].isin(train_dates_set)]

    # 先填充 NaN
    for f in all_features:
        merged[f] = merged[f].fillna(0)
        # 替换 inf
        merged[f] = merged[f].replace([np.inf, -np.inf], 0)

    # Robust 标准化: (x - median) / IQR
    feature_medians = train_data[all_features].median()
    q1 = train_data[all_features].quantile(0.25)
    q3 = train_data[all_features].quantile(0.75)
    feature_iqr = (q3 - q1).replace(0, 1)  # 避免 IQR=0

    # 兼容旧接口：means=medians, stds=iqr
    feature_means = feature_medians
    feature_stds = feature_iqr

    for f in all_features:
        merged[f'{f}_norm'] = (merged[f] - feature_means[f]) / feature_stds[f]
        # clip 极端值
        merged[f'{f}_norm'] = merged[f'{f}_norm'].clip(-5, 5)

    norm_features = [f'{f}_norm' for f in all_features]

    # ---- 标签阈值 ----
    for h in label_horizons:
        ret_col = f'y_{h}d_return'
        valid_rets = train_data[ret_col].dropna()
        if len(valid_rets) > 0:
            threshold = np.percentile(valid_rets, threshold_percentile)
        else:
            threshold = 0
        merged[f'y_{h}d'] = (merged[ret_col] > threshold).astype(int)
        pos_rate = merged[f'y_{h}d'].mean()
        logger.info(f"y_{h}d: 阈值={threshold:.4f}%, 正例比例={pos_rate:.3f}")

    # ---- 构建序列样本 ----
    samples = []
    max_h = max(label_horizons)

    for ts_code, group in merged.groupby('ts_code'):
        group = group.sort_values('trade_date').reset_index(drop=True)
        n = len(group)
        if n < seq_len + max_h:
            continue

        for i in range(seq_len, n - max_h):
            seq = group.iloc[i - seq_len:i][norm_features].values

            if np.any(np.isnan(seq.astype(np.float64))):
                continue

            labels_ok = True
            for h in label_horizons:
                if pd.isna(group.iloc[i][f'y_{h}d']):
                    labels_ok = False
                    break
            if not labels_ok:
                continue

            sample = {
                'ts_code': ts_code,
                'trade_date': group.iloc[i]['trade_date'],
                'features': seq.astype(np.float32),
                'labels': {f'y_{h}d': int(group.iloc[i][f'y_{h}d']) for h in label_horizons},
                'future_returns': {
                    f'y_{h}d': float(group.iloc[i][f'y_{h}d_return'])
                    if pd.notna(group.iloc[i][f'y_{h}d_return']) else 0.0
                    for h in label_horizons
                },
            }

            date = group.iloc[i]['trade_date']
            if date < all_dates[train_end_idx]:
                sample['split'] = 'train'
            elif date < all_dates[val_end_idx]:
                sample['split'] = 'val'
            else:
                sample['split'] = 'test'

            samples.append(sample)

    logger.info(f"构建样本: {len(samples)} 个 (seq_len={seq_len}, features={n_features})")
    if samples:
        splits = {'train': 0, 'val': 0, 'test': 0}
        for s in samples:
            splits[s['split']] += 1
        logger.info(f"  训练: {splits['train']}, 验证: {splits['val']}, 测试: {splits['test']}")

    return samples, feature_means, feature_stds, all_features


def split_dataset(samples):
    """按 split 字段划分数据集。"""
    train = [s for s in samples if s.get('split') == 'train']
    val = [s for s in samples if s.get('split') == 'val']
    test = [s for s in samples if s.get('split') == 'test']

    logger.info(f"数据集划分: 训练 {len(train)}, 验证 {len(val)}, 测试 {len(test)}")
    return train, val, test


def samples_to_arrays(samples, label_key='y_5d'):
    """将样本列表转换为 numpy 数组。"""
    X = np.array([s['features'] for s in samples])
    y = np.array([s['labels'][label_key] for s in samples])
    return X, y


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')

    print("=== 构建日频训练数据集 v2 ===\n")

    samples, means, stds, feature_names = build_daily_dataset(
        train_start='20240101',
        seq_len=20,
        label_horizons=[1, 5, 10],
        threshold_percentile=60,
    )

    if not samples:
        print("没有足够的样本！")
        exit(1)

    train, val, test = split_dataset(samples)

    for label_key in ['y_1d', 'y_5d', 'y_10d']:
        X_train, y_train = samples_to_arrays(train, label_key)
        X_val, y_val = samples_to_arrays(val, label_key)
        X_test, y_test = samples_to_arrays(test, label_key)

        print(f"\n--- {label_key} ---")
        print(f"特征矩阵形状: {X_train.shape}")
        print(f"标签分布 - 训练: {np.bincount(y_train)}, "
              f"验证: {np.bincount(y_val)}, 测试: {np.bincount(y_test)}")

    print(f"\n特征列表 ({len(feature_names)} 维):")
    for i, f in enumerate(feature_names):
        print(f"  {i+1:2d}. {f}: median={means[f]:.4f}, IQR={stds[f]:.4f}")
