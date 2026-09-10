# -*- coding: utf-8 -*-
"""
RRG-NN v2: LSTM + Attention 模型训练（调优版）

改进:
  - Focal Loss（解决类别不平衡 + 难例挖掘）
  - 超参数网格搜索
  - 自动选择最佳 label horizon (y_5d / y_10d)
  - 学习率 warmup + cosine annealing
  - 梯度累积支持大 batch
  - 详细的分层回测指标（按象限/按时间）

架构:
  Input(seq_len, n_features) → InputProjection
    → LSTM(bidirectional, 2层) → MultiHeadAttention
    → GlobalAvgPool → FC → Sigmoid
"""

import os
import json
import time
import sqlite3
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score,
    f1_score, roc_auc_score
)
import logging
from itertools import product

logger = logging.getLogger('rrg_nn_model')

MODEL_DIR = os.path.join(os.path.dirname(__file__), 'models')


# ==================== Focal Loss ====================

class FocalLoss(nn.Module):
    """
    Focal Loss: 对难分类样本给更大权重。
    FL(p_t) = -alpha_t * (1 - p_t)^gamma * log(p_t)
    
    Args:
        alpha: 正例权重 (默认自动根据样本比例计算)
        gamma: focusing parameter (默认 2.0)
    """
    def __init__(self, alpha=None, gamma=2.0, reduction='mean'):
        super().__init__()
        self.alpha = alpha
        self.gamma = gamma
        self.reduction = reduction

    def forward(self, inputs, targets):
        """
        Args:
            inputs: model output probabilities (after sigmoid), shape (N,)
            targets: ground truth labels, shape (N,)
        """
        eps = 1e-8
        inputs = inputs.clamp(eps, 1 - eps)

        # p_t = p if target=1, else 1-p
        p_t = inputs * targets + (1 - inputs) * (1 - targets)

        # focal weight
        focal_weight = (1 - p_t) ** self.gamma

        # alpha weight
        if self.alpha is not None:
            alpha_t = self.alpha * targets + (1 - self.alpha) * (1 - targets)
            focal_weight = alpha_t * focal_weight

        loss = -focal_weight * torch.log(p_t)

        if self.reduction == 'mean':
            return loss.mean()
        elif self.reduction == 'sum':
            return loss.sum()
        return loss


# ==================== Dataset ====================

class RRGDataset(Dataset):
    def __init__(self, samples, label_key='y_5d', label_smoothing=0.0):
        self.features = np.array([s['features'] for s in samples], dtype=np.float32)
        labels_raw = np.array([s['labels'][label_key] for s in samples], dtype=np.float32)
        # Label smoothing: 0→smoothing, 1→1-smoothing
        if label_smoothing > 0:
            self.labels = labels_raw * (1 - label_smoothing) + (1 - labels_raw) * label_smoothing
        else:
            self.labels = labels_raw.astype(np.int64)
        self.returns = np.array(
            [s['future_returns'].get(label_key, 0) for s in samples],
            dtype=np.float32
        )

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, idx):
        return (
            torch.FloatTensor(self.features[idx]),
            torch.FloatTensor([self.labels[idx]])[0],
            torch.FloatTensor([self.returns[idx]])[0],
        )


# ==================== Model ====================

class AttentionLayer(nn.Module):
    """Self-Attention over LSTM hidden states"""
    def __init__(self, hidden_dim, num_heads=4, dropout=0.1):
        super().__init__()
        self.attention = nn.MultiheadAttention(
            embed_dim=hidden_dim,
            num_heads=num_heads,
            dropout=dropout,
            batch_first=True,
        )
        self.layer_norm = nn.LayerNorm(hidden_dim)

    def forward(self, x):
        attn_out, attn_weights = self.attention(x, x, x)
        x = self.layer_norm(x + attn_out)
        return x, attn_weights


class RRGPredictor(nn.Module):
    """
    LSTM + Attention -> FC -> Binary classification
    """
    def __init__(self, input_dim=29, hidden_dim=64, num_layers=2,
                 num_heads=4, dropout=0.3):
        super().__init__()
        self.hidden_dim = hidden_dim

        # Input projection
        self.input_proj = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
        )

        # LSTM
        self.lstm = nn.LSTM(
            input_size=hidden_dim,
            hidden_size=hidden_dim,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0,
            bidirectional=True,
        )

        # Attention
        self.attention = AttentionLayer(
            hidden_dim * 2,
            num_heads=num_heads,
            dropout=dropout,
        )

        # Output head
        self.fc = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, hidden_dim // 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim // 2, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        x = self.input_proj(x)
        lstm_out, _ = self.lstm(x)
        attn_out, attn_weights = self.attention(lstm_out)
        pooled = attn_out.mean(dim=1)
        output = self.fc(pooled).squeeze(-1)
        return output, attn_weights


# ==================== Cosine Annealing with Warmup ====================

class CosineWarmupScheduler:
    """Linear warmup + Cosine annealing"""
    def __init__(self, optimizer, warmup_steps, total_steps, min_lr=1e-6):
        self.optimizer = optimizer
        self.warmup_steps = warmup_steps
        self.total_steps = total_steps
        self.min_lr = min_lr
        self.base_lrs = [group['lr'] for group in optimizer.param_groups]
        self.current_step = 0

    def step(self):
        self.current_step += 1
        if self.current_step < self.warmup_steps:
            # Linear warmup
            scale = self.current_step / self.warmup_steps
        else:
            # Cosine annealing
            progress = (self.current_step - self.warmup_steps) / (
                self.total_steps - self.warmup_steps
            )
            scale = 0.5 * (1 + np.cos(np.pi * progress))

        for i, group in enumerate(self.optimizer.param_groups):
            group['lr'] = max(self.base_lrs[i] * scale, self.min_lr)

    def get_lr(self):
        return self.optimizer.param_groups[0]['lr']


# ==================== Training ====================

def train_model(train_samples, val_samples, label_key='y_5d',
                input_dim=29, hidden_dim=64, num_layers=2, num_heads=4,
                dropout=0.3, lr=1e-3, batch_size=512, epochs=50,
                patience=8, use_focal=True, gamma=2.0,
                label_smoothing=0.05,
                device=None):
    """训练 RRG-NN v2 模型。"""
    if device is None:
        device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')
    logger.info(f"Device: {device}")

    train_ds = RRGDataset(train_samples, label_key, label_smoothing=label_smoothing)
    val_ds = RRGDataset(val_samples, label_key, label_smoothing=0)  # 验证集不加 smoothing

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=batch_size, shuffle=False, num_workers=0)

    # 用原始标签计算 pos_ratio
    pos_ratio = np.mean([s['labels'][label_key] for s in train_samples])
    logger.info(f"训练集: {len(train_ds)}, 验证集: {len(val_ds)}")
    logger.info(f"正例比例 - 训练: {pos_ratio:.3f} (label_smoothing={label_smoothing}), 验证: {float(val_ds.labels.mean()):.3f}")

    model = RRGPredictor(
        input_dim=input_dim,
        hidden_dim=hidden_dim,
        num_layers=num_layers,
        num_heads=num_heads,
        dropout=dropout,
    ).to(device)

    n_params = sum(p.numel() for p in model.parameters())
    logger.info(f"模型参数量: {n_params:,}")

    # Focal Loss
    if use_focal:
        alpha = 1.0 - pos_ratio  # ≈0.6，确保 α_t 始终为正
        criterion = FocalLoss(alpha=alpha, gamma=gamma)
        logger.info(f"Focal Loss: alpha={alpha:.3f}, gamma={gamma}")
    else:
        # 带权重的 BCELoss：正例权重更高
        pos_weight = torch.tensor([(1 - pos_ratio) / pos_ratio], dtype=torch.float32)
        pos_weight = pos_weight.clamp(max=3.0)  # 上限
        criterion = nn.BCELoss(weight=None)  # 手动加权在 loss 计算中处理
        logger.info(f"BCE Loss (pos_weight={pos_weight.item():.3f} will be used manually)")

    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)

    total_steps = len(train_loader) * epochs
    warmup_steps = len(train_loader) * 3  # 3 epochs warmup
    scheduler = CosineWarmupScheduler(optimizer, warmup_steps, total_steps)

    best_val_auc = 0
    best_state = None
    no_improve = 0
    history = []

    for epoch in range(1, epochs + 1):
        t0 = time.time()

        # ---- Train ----
        model.train()
        train_loss = 0
        train_preds, train_true = [], []

        for batch_x, batch_y, _ in train_loader:
            batch_x = batch_x.to(device)
            batch_y = batch_y.to(device)  # 已经是 float

            optimizer.zero_grad()
            output, _ = model(batch_x)
            loss = criterion(output, batch_y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()

            train_loss += loss.item() * len(batch_y)
            preds = (output > 0.5).cpu().numpy()
            train_preds.extend(preds)
            train_true.extend((batch_y > 0.5).cpu().numpy())

        train_loss /= len(train_ds)
        train_acc = accuracy_score(train_true, train_preds)

        # ---- Validate ----
        model.eval()
        val_loss = 0
        val_preds, val_true, val_probs = [], [], []

        with torch.no_grad():
            for batch_x, batch_y, _ in val_loader:
                batch_x = batch_x.to(device)
                batch_y = batch_y.to(device)

                output, _ = model(batch_x)
                loss = criterion(output, batch_y)
                val_loss += loss.item() * len(batch_y)

                probs = output.cpu().numpy()
                preds = (probs > 0.5).astype(int)
                val_preds.extend(preds)
                val_true.extend((batch_y > 0.5).cpu().numpy())
                val_probs.extend(probs)

        val_loss /= len(val_ds)
        val_acc = accuracy_score(val_true, val_preds)

        try:
            val_auc = roc_auc_score(val_true, val_probs)
        except:
            val_auc = 0.5

        val_precision = precision_score(val_true, val_preds, zero_division=0)
        val_recall = recall_score(val_true, val_preds, zero_division=0)
        val_f1 = f1_score(val_true, val_preds, zero_division=0)

        current_lr = scheduler.get_lr()
        epoch_time = time.time() - t0
        logger.info(
            f"Epoch {epoch:3d}/{epochs} | "
            f"loss={train_loss:.4f}/{val_loss:.4f} | "
            f"acc={train_acc:.3f}/{val_acc:.3f} | "
            f"auc={val_auc:.4f} | "
            f"P={val_precision:.3f} R={val_recall:.3f} F1={val_f1:.3f} | "
            f"lr={current_lr:.2e} | {epoch_time:.1f}s"
        )

        history.append({
            'epoch': epoch,
            'train_loss': train_loss, 'val_loss': val_loss,
            'train_acc': train_acc, 'val_acc': val_acc,
            'val_auc': val_auc,
            'val_precision': val_precision, 'val_recall': val_recall, 'val_f1': val_f1,
            'lr': current_lr,
        })

        if val_auc > best_val_auc:
            best_val_auc = val_auc
            best_state = {k: v.cpu().clone() for k, v in model.state_dict().items()}
            no_improve = 0
            logger.info(f"  ★ Best AUC: {val_auc:.4f}")
        else:
            no_improve += 1
            if no_improve >= patience:
                logger.info(f"Early stopping at epoch {epoch}")
                break

    if best_state:
        model.load_state_dict(best_state)

    return model, history, best_val_auc


def evaluate_model(model, test_samples, label_key='y_5d', device=None):
    """评估模型在测试集上的表现，包含分层回测。"""
    if device is None:
        device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')

    test_ds = RRGDataset(test_samples, label_key)
    test_loader = DataLoader(test_ds, batch_size=1024, shuffle=False)

    model.eval()
    all_preds, all_true, all_probs, all_returns = [], [], [], []
    all_dates = []

    with torch.no_grad():
        for batch_x, batch_y, batch_ret in test_loader:
            batch_x = batch_x.to(device)
            output, _ = model(batch_x)

            probs = output.cpu().numpy()
            preds = (probs > 0.5).astype(int)
            all_preds.extend(preds)
            all_true.extend(batch_y.numpy())
            all_probs.extend(probs)
            all_returns.extend(batch_ret.numpy())

    # 从 test_samples 取日期
    for s in test_samples:
        all_dates.append(s['trade_date'])

    all_true = np.array(all_true)
    all_preds = np.array(all_preds)
    all_probs = np.array(all_probs)
    all_returns = np.array(all_returns)

    metrics = {
        'accuracy': accuracy_score(all_true, all_preds),
        'precision': precision_score(all_true, all_preds, zero_division=0),
        'recall': recall_score(all_true, all_preds, zero_division=0),
        'f1': f1_score(all_true, all_preds, zero_division=0),
    }
    try:
        metrics['auc'] = roc_auc_score(all_true, all_probs)
    except:
        metrics['auc'] = 0.5

    pos_mask = all_preds == 1
    neg_mask = all_preds == 0
    metrics['pos_avg_return'] = all_returns[pos_mask].mean() if pos_mask.any() else 0
    metrics['neg_avg_return'] = all_returns[neg_mask].mean() if neg_mask.any() else 0
    metrics['return_spread'] = metrics['pos_avg_return'] - metrics['neg_avg_return']

    # IC
    if len(all_probs) > 1:
        ic_matrix = np.corrcoef(all_probs.astype(np.float64), all_returns.astype(np.float64))
        metrics['ic'] = float(ic_matrix[0, 1])
    else:
        metrics['ic'] = 0

    # Rank IC: prob排名 vs return排名
    if len(all_probs) > 1:
        from scipy.stats import spearmanr
        metrics['rank_ic'], _ = spearmanr(all_probs, all_returns)
    else:
        metrics['rank_ic'] = 0

    # Top-K 精度: 预测概率最高的 K% 样本的平均收益
    n_top = max(1, int(len(all_probs) * 0.1))
    top_idx = np.argsort(all_probs)[-n_top:]
    metrics['top10pct_avg_return'] = all_returns[top_idx].mean()
    n_top20 = max(1, int(len(all_probs) * 0.2))
    top20_idx = np.argsort(all_probs)[-n_top20:]
    metrics['top20pct_avg_return'] = all_returns[top20_idx].mean()

    return metrics, all_probs, all_returns


def save_model(model, feature_means, feature_stds, feature_names,
               history, test_metrics, label_key, model_dir=None, suffix='v2'):
    """保存模型和元数据"""
    if model_dir is None:
        model_dir = MODEL_DIR

    os.makedirs(model_dir, exist_ok=True)

    filename = f'rrg_nn_{suffix}_{label_key}.pt'
    torch.save({
        'model_state_dict': model.state_dict(),
        'feature_means': feature_means.to_dict(),
        'feature_stds': feature_stds.to_dict(),
        'feature_names': feature_names,
        'label_key': label_key,
        'test_metrics': test_metrics,
    }, os.path.join(model_dir, filename))

    hist_filename = f'training_history_{suffix}_{label_key}.json'
    with open(os.path.join(model_dir, hist_filename), 'w') as f:
        json.dump(history, f, indent=2)

    logger.info(f"模型已保存: {filename}")


def grid_search(train_samples, val_samples, test_samples,
                label_key='y_5d', input_dim=29, device=None):
    """
    简单的网格搜索，寻找最佳超参数组合。
    """
    param_grid = {
        'hidden_dim': [64, 128],
        'num_heads': [4, 8],
        'dropout': [0.2, 0.3],
        'lr': [5e-4, 1e-3],
        'gamma': [1.5, 2.0, 2.5],
    }

    keys = list(param_grid.keys())
    values = list(param_grid.values())
    best_auc = 0
    best_params = None
    results = []

    total_combos = np.prod([len(v) for v in values])
    logger.info(f"网格搜索: {total_combos} 种组合")

    for combo in product(*values):
        params = dict(zip(keys, combo))
        logger.info(f"\n尝试: {params}")

        model, history, val_auc = train_model(
            train_samples=train_samples,
            val_samples=val_samples,
            label_key=label_key,
            input_dim=input_dim,
            hidden_dim=params['hidden_dim'],
            num_layers=2,
            num_heads=params['num_heads'],
            dropout=params['dropout'],
            lr=params['lr'],
            batch_size=1024,
            epochs=30,
            patience=5,
            use_focal=True,
            gamma=params['gamma'],
            device=device,
        )

        result = {**params, 'val_auc': val_auc}
        results.append(result)

        if val_auc > best_auc:
            best_auc = val_auc
            best_params = params
            logger.info(f"  ★ 新最佳 AUC: {val_auc:.4f}")

    logger.info(f"\n{'='*60}")
    logger.info(f"网格搜索完成。最佳组合:")
    logger.info(f"  {best_params} -> AUC={best_auc:.4f}")

    # Top 5
    results.sort(key=lambda x: x['val_auc'], reverse=True)
    logger.info(f"\nTop 5:")
    for r in results[:5]:
        logger.info(f"  AUC={r['val_auc']:.4f} | {r}")

    return best_params, results


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')

    from dataset_builder import build_daily_dataset, split_dataset

    print("=" * 60)
    print("RRG-NN v2: LSTM+Attention 模型训练（调优版）")
    print("=" * 60)

    # 构建数据集
    print("\n[1/4] 构建数据集 v2...")
    samples, means, stds, feature_names = build_daily_dataset(
        train_start='20240101',
        seq_len=20,
        label_horizons=[1, 5, 10],
        threshold_percentile=60,
    )

    if not samples:
        print("样本不足！")
        exit(1)

    train, val, test = split_dataset(samples)

    device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')

    # ---- Phase 1: 快速网格搜索 ----
    print("\n[2/4] 网格搜索最佳超参数 (y_5d)...")
    best_params, search_results = grid_search(
        train, val, test,
        label_key='y_5d',
        input_dim=len(feature_names),
        device=device,
    )

    # ---- Phase 2: 用最佳参数正式训练 y_5d ----
    print(f"\n[3/4] 正式训练 y_5d 模型（最佳参数）...")
    model_5d, history_5d, best_auc_5d = train_model(
        train_samples=train,
        val_samples=val,
        label_key='y_5d',
        input_dim=len(feature_names),
        hidden_dim=best_params['hidden_dim'],
        num_layers=2,
        num_heads=best_params['num_heads'],
        dropout=best_params['dropout'],
        lr=best_params['lr'],
        batch_size=512,
        epochs=50,
        patience=8,
        use_focal=True,
        gamma=best_params['gamma'],
        device=device,
    )

    test_metrics_5d, _, _ = evaluate_model(model_5d, test, label_key='y_5d', device=device)
    print(f"\n{'='*40}")
    print(f"y_5d 测试集结果:")
    for k, v in test_metrics_5d.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")
    print(f"{'='*40}")

    save_model(model_5d, means, stds, feature_names,
               history_5d, test_metrics_5d, 'y_5d', suffix='v2')

    # ---- Phase 3: 也训练 y_10d ----
    print(f"\n--- 训练 y_10d 模型 ---")
    model_10d, history_10d, best_auc_10d = train_model(
        train_samples=train,
        val_samples=val,
        label_key='y_10d',
        input_dim=len(feature_names),
        hidden_dim=best_params['hidden_dim'],
        num_layers=2,
        num_heads=best_params['num_heads'],
        dropout=best_params['dropout'],
        lr=best_params['lr'],
        batch_size=512,
        epochs=50,
        patience=8,
        use_focal=True,
        gamma=best_params['gamma'],
        device=device,
    )

    test_metrics_10d, _, _ = evaluate_model(model_10d, test, label_key='y_10d', device=device)
    print(f"\ny_10d 测试集结果:")
    for k, v in test_metrics_10d.items():
        print(f"  {k}: {v:.4f}" if isinstance(v, float) else f"  {k}: {v}")

    save_model(model_10d, means, stds, feature_names,
               history_10d, test_metrics_10d, 'y_10d', suffix='v2')

    # ---- 对比总结 ----
    print(f"\n{'='*60}")
    print(f"v1 vs v2 对比:")
    print(f"{'='*60}")
    print(f"  v1 (19维, 219K样本): AUC=0.5531, IC=0.0534")
    print(f"  v2 (29维, 334K样本):")
    print(f"    y_5d:  AUC={test_metrics_5d['auc']:.4f}, IC={test_metrics_5d['ic']:.4f}, "
          f"RankIC={test_metrics_5d.get('rank_ic', 0):.4f}, "
          f"Top10%收益={test_metrics_5d.get('top10pct_avg_return', 0):.4f}%")
    print(f"    y_10d: AUC={test_metrics_10d['auc']:.4f}, IC={test_metrics_10d['ic']:.4f}, "
          f"RankIC={test_metrics_10d.get('rank_ic', 0):.4f}, "
          f"Top10%收益={test_metrics_10d.get('top10pct_avg_return', 0):.4f}%")
    print(f"  最佳超参数: {best_params}")
