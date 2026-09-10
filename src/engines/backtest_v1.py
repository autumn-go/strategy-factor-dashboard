# -*- coding: utf-8 -*-
"""
用 rrg_nn_best.pt (v1) 回测 2025 年以来的效果
旧模型结构: input_proj 是 nn.Linear（非 Sequential）
"""
import sqlite3
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader


class AttentionLayer(nn.Module):
    def __init__(self, hidden_dim, num_heads=4, dropout=0.1):
        super().__init__()
        self.attention = nn.MultiheadAttention(
            embed_dim=hidden_dim, num_heads=num_heads,
            dropout=dropout, batch_first=True
        )
        self.layer_norm = nn.LayerNorm(hidden_dim)

    def forward(self, x):
        attn_out, attn_weights = self.attention(x, x, x)
        x = self.layer_norm(x + attn_out)
        return x, attn_weights


class RRGPredictorV1(nn.Module):
    """旧版模型: input_proj 是单个 Linear"""
    def __init__(self, input_dim=19, hidden_dim=64, num_layers=2, num_heads=4, dropout=0.3):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.input_proj = nn.Linear(input_dim, hidden_dim)
        self.lstm = nn.LSTM(
            input_size=hidden_dim, hidden_size=hidden_dim,
            num_layers=num_layers, batch_first=True,
            dropout=dropout if num_layers > 1 else 0, bidirectional=True
        )
        self.attention = AttentionLayer(hidden_dim * 2, num_heads=num_heads, dropout=dropout)
        self.fc = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim), nn.GELU(), nn.Dropout(dropout),
            nn.Linear(hidden_dim, hidden_dim // 2), nn.GELU(), nn.Dropout(dropout),
            nn.Linear(hidden_dim // 2, 1), nn.Sigmoid(),
        )

    def forward(self, x):
        x = self.input_proj(x)
        lstm_out, _ = self.lstm(x)
        attn_out, attn_weights = self.attention(lstm_out)
        pooled = attn_out.mean(dim=1)
        return self.fc(pooled).squeeze(-1), attn_weights


if __name__ == '__main__':
    # ========== 1. 加载模型 ==========
    ckpt = torch.load('models/rrg_nn_best.pt', map_location='cpu', weights_only=False)
    feature_names = ckpt['feature_names']
    feature_means = ckpt['feature_means']
    feature_stds = ckpt['feature_stds']

    model = RRGPredictorV1(input_dim=19, hidden_dim=64, num_layers=2, num_heads=4, dropout=0.3)
    model.load_state_dict(ckpt['model_state_dict'])
    model.eval()
    device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')
    model.to(device)
    print(f"模型加载成功, device={device}")

    # ========== 2. 读取数据 ==========
    conn_rrg = sqlite3.connect('data/factors.db')
    rrg = pd.read_sql_query("""
        SELECT ts_code, trade_date, rs_ratio, rs_momentum, rs,
               quadrant, quadrant_duration, angle, velocity, acceleration,
               distance_to_center, pct_chg, close, vol
        FROM rrg_daily WHERE trade_date >= '20240901'
        ORDER BY ts_code, trade_date
    """, conn_rrg)
    conn_rrg.close()

    conn_ind = sqlite3.connect('data/factors.db')
    ews = pd.read_sql_query("""
        SELECT concept_code as ts_code, trade_date,
               final_score, momentum, emotion_diff, s_score,
               total_stocks, up_count, limit_up_count, broken_count, max_streak
        FROM ews_daily WHERE trade_date >= '20240901'
        ORDER BY ts_code, trade_date
    """, conn_ind)
    conn_ind.close()

    for col in ['final_score', 'momentum', 'emotion_diff', 's_score',
                'total_stocks', 'up_count', 'limit_up_count', 'broken_count', 'max_streak']:
        ews[col] = pd.to_numeric(ews[col], errors='coerce')
    ews['up_ratio'] = (ews['up_count'] / ews['total_stocks'].replace(0, np.nan)).clip(0, 1)

    merged = rrg.merge(ews, on=['ts_code', 'trade_date'], how='inner')
    merged = merged.sort_values(['ts_code', 'trade_date']).reset_index(drop=True)
    print(f"合并: {len(merged)} 条, {merged['ts_code'].nunique()} 板块, "
          f"{merged['trade_date'].min()}~{merged['trade_date'].max()}")

    # ========== 3. 标准化 + 序列 ==========
    SEQ_LEN = 20
    for f in feature_names:
        mean_val = feature_means.get(f, 0)
        std_val = feature_stds.get(f, 1)
        if std_val < 1e-8:
            std_val = 1
        merged[f] = (merged[f].fillna(0).replace([np.inf, -np.inf], 0) - mean_val) / std_val
        merged[f] = merged[f].clip(-5, 5)

    samples = []
    for ts_code, grp in merged.groupby('ts_code'):
        grp = grp.sort_values('trade_date').reset_index(drop=True)
        feat_vals = grp[feature_names].values.astype(np.float32)
        for i in range(SEQ_LEN, len(grp)):
            seq = feat_vals[i - SEQ_LEN:i]
            if np.any(np.isnan(seq)) or np.any(np.isinf(seq)):
                continue
            row = grp.iloc[i]
            samples.append({
                'ts_code': ts_code,
                'trade_date': row['trade_date'],
                'features': seq,
                'pct_chg': row['pct_chg'],
            })

    samples_2025 = [s for s in samples if s['trade_date'] >= '20250101']
    print(f"总样本: {len(samples)}, 2025年: {len(samples_2025)}")

    # ========== 4. 批量预测 ==========
    features = np.array([s['features'] for s in samples_2025], dtype=np.float32)
    loader = DataLoader(torch.FloatTensor(features), batch_size=1024, shuffle=False)

    all_probs = []
    with torch.no_grad():
        for batch_x in loader:
            batch_x = batch_x.to(device)
            output, _ = model(batch_x)
            all_probs.extend(output.cpu().numpy())

    results = pd.DataFrame({
        'ts_code': [s['ts_code'] for s in samples_2025],
        'trade_date': [s['trade_date'] for s in samples_2025],
        'prob': np.array(all_probs),
        'pct_chg': [s['pct_chg'] for s in samples_2025],
    })

    print(f"\n预测概率: mean={results['prob'].mean():.4f}, std={results['prob'].std():.4f}, "
          f">0.5占{(results['prob'] > 0.5).mean() * 100:.1f}%")

    # ========== 5. 回测 ==========
    dates = sorted(results['trade_date'].unique())
    daily_returns = []

    for d in dates:
        day_df = results[results['trade_date'] == d]
        n = len(day_df)
        if n < 5:
            continue

        benchmark = day_df['pct_chg'].mean()
        top10 = day_df.nlargest(max(1, n // 10), 'prob')['pct_chg'].mean()
        top20 = day_df.nlargest(max(1, n // 5), 'prob')['pct_chg'].mean()
        top30 = day_df.nlargest(max(1, int(n * 0.3)), 'prob')['pct_chg'].mean()

        confident_mask = day_df['prob'] > 0.5
        confident = day_df[confident_mask]['pct_chg'].mean() if confident_mask.sum() > 0 else 0

        daily_returns.append({
            'date': d, 'n_sectors': n, 'n_confident': confident_mask.sum(),
            'benchmark': benchmark, 'top10': top10, 'top20': top20, 'top30': top30,
            'confident': confident,
        })

    dr = pd.DataFrame(daily_returns)
    dr['date'] = pd.to_datetime(dr['date'])

    for col in ['benchmark', 'top10', 'top20', 'top30', 'confident']:
        dr[f'{col}_nav'] = (1 + dr[col] / 100).cumprod()

    # ========== 6. 输出结果 ==========
    print(f"\n{'=' * 70}")
    print(f"RRG-NN v1 回测 (2025 ~ {dr['date'].max().strftime('%Y-%m-%d')})")
    print(f"{len(dr)} 个交易日, 平均 {dr['n_sectors'].mean():.0f} 板块/日")
    print(f"{'=' * 70}")

    for col in ['benchmark', 'top10', 'top20', 'top30', 'confident']:
        nav = dr[f'{col}_nav']
        total_ret = (nav.iloc[-1] - 1) * 100
        peak = nav.cummax()
        max_dd = ((nav - peak) / peak).min() * 100
        daily_mean = dr[col].mean()
        win_rate = (dr[col] > 0).mean() * 100
        n_years = len(dr) / 242
        annual_ret = ((nav.iloc[-1]) ** (1 / n_years) - 1) * 100
        print(f"{col:12s}: 累积={total_ret:+7.2f}%, 年化={annual_ret:+5.2f}%, "
              f"回撤={max_dd:5.2f}%, 日均={daily_mean:+.4f}%, 胜率={win_rate:.1f}%")

    # 月度明细
    print(f"\n{'=' * 70}")
    print(f"月度收益:")
    print(f"{'=' * 70}")
    dr['month'] = dr['date'].dt.to_period('M')
    monthly = dr.groupby('month').agg(
        n_days=('date', 'count'),
        benchmark=('benchmark', 'sum'),
        top10=('top10', 'sum'),
        top20=('top20', 'sum'),
        confident=('confident', 'sum'),
    ).round(4)
    monthly['top10_excess'] = (monthly['top10'] - monthly['benchmark']).round(4)
    print(monthly.to_string())

    excess_mean = dr['top10'].mean() - dr['benchmark'].mean()
    excess_win = (dr['top10'] > dr['benchmark']).mean() * 100
    print(f"\nTop10 超额: 日均={excess_mean:+.4f}%, 超额胜率={excess_win:.1f}%")
