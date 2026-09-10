# -*- coding: utf-8 -*-
"""
策略因子平台 - FastAPI 后端服务
"""
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import sqlite3
import json
import os
import logging
import threading
from datetime import datetime

from ews_engine import run_full_calculation, PARAMS, compute_backtest, compute_backtest_rf
from rf_engine import RF_PARAMS, compute_rf_signals
from boci_engine import BOCI_PARAMS, run_boci_calculation, run_boci_incremental
from index_sentiment_engine import run_index_sentiment_calculation, INDEX_CONFIG
from lc_engine import compute_lc_signals_full, LC_PARAMS
from morphology_engine import run_full_scan as run_morphology_scan
from crowdiness_engine import run_crowdiness_calculation, run_crowdiness_incremental, CROWDINESS_PARAMS

INDEX_CODE = LC_PARAMS['index_code']  # 中证2000

app = FastAPI(title="策略因子平台")

# ==================== 路径配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 使用 local_dbs/ 目录（APFS 磁盘，支持 SQLite 文件锁）
# data/ 目录在 BEANPAPER (exFAT) 上，不支持文件锁
LOCAL_DB_DIR = os.path.join(BASE_DIR, 'local_dbs')
os.makedirs(LOCAL_DB_DIR, exist_ok=True)

# 挂载静态文件目录（plotly.min.js 等）
STATIC_DIR = os.path.join(BASE_DIR, 'static')
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

FACTORS_DB = os.path.join(LOCAL_DB_DIR, 'factors.db')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('strategy-platform')

# ==================== 计算状态 ====================
calc_state = {
    'running': False,
    'progress': 0,
    'total': 0,
    'current_date': '',
    'message': '',
    'start_time': None,
}

rf_calc_state = {
    'running': False,
    'progress': 0,
    'total': 0,
    'current_date': '',
    'message': '',
    'start_time': None,
}

crowd_calc_state = {
    'running': False,
    'progress': 0,
    'total': 0,
    'current_date': '',
    'message': '',
    'start_time': None,
}


def get_factors_db():
    conn = sqlite3.connect(FACTORS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate_db(conn):
    """数据库迁移：添加 sector_type 列"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ews_daily)").fetchall()]
    if 'sector_type' not in cols:
        conn.execute("ALTER TABLE ews_daily ADD COLUMN sector_type TEXT DEFAULT 'concept'")
        logger.info("数据库迁移: 添加 sector_type 列")

    cols_bt = [r[1] for r in conn.execute("PRAGMA table_info(ews_backtest)").fetchall()]
    if 'sector_type' not in cols_bt:
        conn.execute("ALTER TABLE ews_backtest ADD COLUMN sector_type TEXT DEFAULT 'all'")
        logger.info("数据库迁移: 回测表添加 sector_type 列")


def init_factors_db():
    """初始化因子数据库"""
    conn = get_factors_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ews_daily (
            trade_date TEXT NOT NULL,
            concept_code TEXT NOT NULL,
            concept_name TEXT NOT NULL,
            sector_type TEXT DEFAULT 'concept',
            momentum REAL,
            emotion_diff REAL,
            s_score REAL,
            final_score REAL,
            total_stocks INTEGER,
            up_count INTEGER,
            limit_up_count INTEGER,
            broken_count INTEGER,
            max_streak INTEGER,
            PRIMARY KEY (trade_date, concept_code)
        );

        CREATE INDEX IF NOT EXISTS idx_ews_date ON ews_daily(trade_date);
        CREATE INDEX IF NOT EXISTS idx_ews_score ON ews_daily(trade_date, final_score DESC);
        CREATE INDEX IF NOT EXISTS idx_ews_sector ON ews_daily(sector_type);

        CREATE TABLE IF NOT EXISTS ews_backtest (
            signal_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            sector_type TEXT DEFAULT 'all',
            daily_return REAL,
            cum_return REAL,
            top10_concepts TEXT,
            top10_codes TEXT,
            PRIMARY KEY (signal_date, return_date, sector_type)
        );

        CREATE TABLE IF NOT EXISTS calc_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)

    _migrate_db(conn)

    conn.execute("""
        INSERT OR REPLACE INTO calc_meta (key, value) VALUES ('ews_params', ?)
    """, (json.dumps(PARAMS),))

    conn.commit()
    conn.close()


# ==================== API 接口 ====================

@app.get('/api/status')
def api_status():
    conn = get_factors_db()
    try:
        stats = conn.execute("""
            SELECT
                COUNT(DISTINCT trade_date) as trade_days,
                MIN(trade_date) as min_date,
                MAX(trade_date) as max_date,
                COUNT(*) as total_records
            FROM ews_daily
        """).fetchone()

        has_data = stats['total_records'] > 0

        return {
            'calc_running': calc_state['running'],
            'calc_progress': calc_state['progress'],
            'calc_total': calc_state['total'],
            'calc_current_date': calc_state['current_date'],
            'calc_message': calc_state['message'],
            'has_data': has_data,
            'trade_days': stats['trade_days'] if has_data else 0,
            'min_date': stats['min_date'] if has_data else None,
            'max_date': stats['max_date'] if has_data else None,
            'total_records': stats['total_records'] if has_data else 0,
            'concepts': conn.execute("SELECT COUNT(DISTINCT concept_code) FROM ews_daily WHERE sector_type='concept'").fetchone()[0] if has_data else 0,
            'industries': conn.execute("SELECT COUNT(DISTINCT concept_code) FROM ews_daily WHERE sector_type='industry'").fetchone()[0] if has_data else 0,
        }
    finally:
        conn.close()


@app.get('/api/ews/dates')
def api_ews_dates():
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT trade_date FROM ews_daily
            GROUP BY trade_date ORDER BY trade_date DESC
        """).fetchall()
        return [r['trade_date'] for r in rows]
    finally:
        conn.close()


@app.get('/api/ews/ranking')
def api_ews_ranking(
    date: str = Query(..., description="交易日期 YYYYMMDD"),
    sector_type: str = Query(None, description="板块类型: concept / industry"),
    top_n: int = Query(None, description="只返回前N条")
):
    conn = get_factors_db()
    try:
        where = "WHERE trade_date = ?"
        params = [date]
        if sector_type:
            where += " AND sector_type = ?"
            params.append(sector_type)

        limit = f"LIMIT {top_n}" if top_n else ""
        rows = conn.execute(f"""
            SELECT * FROM ews_daily
            {where}
            ORDER BY final_score DESC
            {limit}
        """, params).fetchall()

        if not rows:
            raise HTTPException(404, f"日期 {date} 无数据")

        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/ews/backtest')
def api_ews_backtest(
    sector_type: str = Query('all', description="板块类型: all / concept / industry")
):
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, return_date, sector_type, daily_return, cum_return,
                   top10_concepts, top10_codes
            FROM ews_backtest
            WHERE sector_type = ?
            ORDER BY signal_date
        """, (sector_type,)).fetchall()

        if not rows:
            raise HTTPException(404, "暂无回测数据，请先计算因子")

        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/ews/concept/{concept_code}')
def api_ews_concept_history(concept_code: str):
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT * FROM ews_daily
            WHERE concept_code = ?
            ORDER BY trade_date
        """, (concept_code,)).fetchall()

        if not rows:
            raise HTTPException(404, f"板块 {concept_code} 无数据")

        return [dict(r) for r in rows]
    finally:
        conn.close()


def _save_results(conn, results, backtest_map):
    """将计算结果写入数据库
    
    results: 增量因子数据（只需覆盖对应日期）
    backtest_map: 全量回测数据（需要清空旧数据再全量插入，因为cum_return依赖连续计算）
    """
    dates_in_results = set(r['trade_date'] for r in results)
    for d in dates_in_results:
        conn.execute("DELETE FROM ews_daily WHERE trade_date = ?", (d,))

    for r in results:
        conn.execute("""
            INSERT INTO ews_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (r['trade_date'], r['concept_code'], r['concept_name'],
              r.get('sector_type', 'concept'),
              r['momentum'], r['emotion_diff'], r['s_score'], r['final_score'],
              r['total_stocks'], r['up_count'], r['limit_up_count'],
              r['broken_count'], r['max_streak']))

    if backtest_map:
        # 回测数据必须全量覆盖（cum_return 从第一天开始连续累加）
        conn.execute("DELETE FROM ews_backtest")
        for stype, bt_data in backtest_map.items():
            for b in bt_data:
                conn.execute("""
                    INSERT INTO ews_backtest VALUES (?,?,?,?,?,?,?)
                """, (b['signal_date'], b['return_date'], stype,
                      b['daily_return'], b['cum_return'],
                      json.dumps(b['top10_concepts'], ensure_ascii=False),
                      json.dumps(b['top10_codes'], ensure_ascii=False)))

    conn.execute("""
        INSERT OR REPLACE INTO calc_meta (key, value) VALUES ('last_calc', ?)
    """, (datetime.now().isoformat(),))

    conn.commit()


# ==================== RF-EWSDM API ====================

def _init_rf_tables(conn):
    """初始化 RF-EWSDM 相关数据库表"""
    # 先检查表结构是否需要迁移
    cols = [r[1] for r in conn.execute("PRAGMA table_info(rf_signals)").fetchall()]
    if cols and 'smooth_range' not in cols:
        conn.execute("DROP TABLE IF EXISTS rf_signals")
        conn.execute("DROP TABLE IF EXISTS rf_ewsdm_backtest")
        logger.info("RF 表结构迁移: 重建 rf_signals 和 rf_ewsdm_backtest")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rf_signals (
            trade_date TEXT PRIMARY KEY,
            close REAL,
            smooth_range REAL,
            filter_line REAL,
            upward INTEGER,
            signal INTEGER
        );

        CREATE TABLE IF NOT EXISTS rf_ewsdm_backtest (
            signal_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            sector_type TEXT DEFAULT 'all',
            daily_return REAL,
            cum_return REAL,
            rf_signal INTEGER,
            top10_concepts TEXT,
            top10_codes TEXT,
            PRIMARY KEY (signal_date, return_date, sector_type)
        );

        CREATE INDEX IF NOT EXISTS idx_rf_signal ON rf_signals(trade_date);
    """)


def _save_rf_results(conn, rf_df, backtest_map):
    """将 RF-EWSDM 计算结果写入数据库"""
    conn.execute("DELETE FROM rf_signals")
    for _, row in rf_df.iterrows():
        conn.execute("""
            INSERT OR REPLACE INTO rf_signals VALUES (?,?,?,?,?,?)
        """, (row['trade_date'], row['close'], row['smooth_range'],
              row['filter_line'], int(row['upward']), int(row['signal'])))

    # 回测数据全量覆盖
    conn.execute("DELETE FROM rf_ewsdm_backtest")
    for stype, bt_data in backtest_map.items():
        for b in bt_data:
            conn.execute("""
                INSERT INTO rf_ewsdm_backtest VALUES (?,?,?,?,?,?,?,?)
            """, (b['signal_date'], b['return_date'], stype,
                  b['daily_return'], b['cum_return'], b.get('rf_signal', 1),
                  json.dumps(b['top10_concepts'], ensure_ascii=False),
                  json.dumps(b['top10_codes'], ensure_ascii=False)))

    conn.execute("""
        INSERT OR REPLACE INTO calc_meta (key, value) VALUES ('rf_params', ?)
    """, (json.dumps(RF_PARAMS),))

    conn.commit()


@app.get('/api/rf/signals')
def api_rf_signals(
    start_date: str = Query(None),
    end_date: str = Query(None),
):
    """获取 Range Filter 信号数据"""
    conn = get_factors_db()
    try:
        where = ""
        params = []
        if start_date:
            where += " WHERE trade_date >= ?"
            params.append(start_date)
        if end_date:
            where += " AND trade_date <= ?" if where else " WHERE trade_date <= ?"
            params.append(end_date)

        rows = conn.execute(f"""
            SELECT trade_date, close, smooth_range, filter_line, upward, signal
            FROM rf_signals {where} ORDER BY trade_date
        """, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/rf/backtest')
def api_rf_backtest(
    sector_type: str = Query('all', description="板块类型: all / concept / industry")
):
    """获取 RF-EWSDM 回测数据"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, return_date, sector_type, daily_return, cum_return,
                   rf_signal, top10_concepts, top10_codes
            FROM rf_ewsdm_backtest
            WHERE sector_type = ? ORDER BY signal_date
        """, (sector_type,)).fetchall()

        if not rows:
            raise HTTPException(404, "暂无 RF-EWSDM 回测数据")

        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/rf/status')
def api_rf_status():
    """获取 RF-EWSDM 计算状态"""
    conn = get_factors_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(*) as total, MIN(trade_date) as min_date, MAX(trade_date) as max_date
            FROM rf_signals
        """).fetchone()

        buy_count = conn.execute("SELECT COUNT(*) FROM rf_signals WHERE signal=1").fetchone()[0]
        sell_count = conn.execute("SELECT COUNT(*) FROM rf_signals WHERE signal=-1").fetchone()[0]

        has_data = stats['total'] > 0

        return {
            'calc_running': rf_calc_state['running'],
            'calc_progress': rf_calc_state['progress'],
            'calc_total': rf_calc_state['total'],
            'calc_message': rf_calc_state['message'],
            'has_data': has_data,
            'total_days': stats['total'] if has_data else 0,
            'min_date': stats['min_date'] if has_data else None,
            'max_date': stats['max_date'] if has_data else None,
            'buy_days': buy_count if has_data else 0,
            'sell_days': sell_count if has_data else 0,
        }
    finally:
        conn.close()


@app.post('/api/rf/calculate')
def api_rf_calculate():
    """启动 RF-EWSDM 增量计算"""
    if rf_calc_state['running']:
        raise HTTPException(409, "RF-EWSDM 计算正在进行中")

    rf_calc_state['running'] = True
    rf_calc_state['progress'] = 0
    rf_calc_state['total'] = 0
    rf_calc_state['message'] = 'RF-EWSDM 计算中...'

    def run_calc():
        try:
            conn = get_factors_db()
            max_ews_date = conn.execute("SELECT MAX(trade_date) FROM ews_daily").fetchone()[0]
            max_rf_date = conn.execute("SELECT MAX(trade_date) FROM rf_signals").fetchone()[0]
            conn.close()

            # Step 1: 增量计算 EW-SDM 因子（从已有最新日期开始）
            ews_start = max_ews_date if max_ews_date else '20230101'
            is_incremental = bool(max_ews_date)
            if is_incremental:
                rf_calc_state['message'] = f'增量更新，EW-SDM 从 {ews_start} 开始'
            else:
                rf_calc_state['message'] = f'首次计算，EW-SDM 从 {ews_start} 开始'

            def progress_cb(current, total, date):
                rf_calc_state['progress'] = current
                rf_calc_state['total'] = total
                rf_calc_state['current_date'] = date
                rf_calc_state['message'] = f'因子计算 {current}/{total} ({date})'

            results, _ = run_full_calculation(start_date=ews_start, progress_cb=progress_cb)

            rf_calc_state['message'] = 'EW-SDM 因子计算完成，开始 RF 信号计算...'

            # Step 2: 计算 Range Filter 信号（必须从20230101全量计算，EMA状态依赖）
            rf_df = compute_rf_signals('20230101')

            # Step 3: RF-EWSDM 回测（三种类型）
            # 需要加载全部已有因子（增量只补充了新日期，回测需要连续数据）
            conn = get_factors_db()
            existing_dates = [r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM ews_daily ORDER BY trade_date"
            ).fetchall()]
            conn.close()

            # 合并已有因子和新因子，按日期排序
            # 对于增量模式，只加载用于回测的关键字段
            if is_incremental and existing_dates:
                # 从数据库加载全部已有因子用于回测
                conn_stock_db = sqlite3.connect('/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/stock_daily.db')
                conn_industry_db = sqlite3.connect('/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/industry.db')
                conn_limit_db = sqlite3.connect('/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/limit_data.db')

                all_trade_dates = sorted(set(existing_dates + dates))
                all_dates_str = "','".join(all_trade_dates)

                conn_factors = get_factors_db()
                all_results = []
                for d in all_trade_dates:
                    rows = conn_factors.execute("""
                        SELECT trade_date, concept_code, concept_name, sector_type,
                               momentum, emotion_diff, s_score, final_score,
                               total_stocks, up_count, limit_up_count, broken_count, max_streak
                        FROM ews_daily WHERE trade_date = ?
                    """, (d,)).fetchall()
                    for r in rows:
                        all_results.append(dict(r))
                conn_factors.close()

                bt_all = compute_backtest_rf(all_results, sector_type=None)
                bt_concept = compute_backtest_rf(all_results, sector_type='concept')
                bt_industry = compute_backtest_rf(all_results, sector_type='industry')
            else:
                bt_all = compute_backtest_rf(results, sector_type=None)
                bt_concept = compute_backtest_rf(results, sector_type='concept')
                bt_industry = compute_backtest_rf(results, sector_type='industry')

            rf_calc_state['message'] = f'计算完成：{len(results)} 条新因子，{len(bt_all)} 条 RF 回测'

            # Step 4: 保存（EW-SDM 因子增量追加，RF 信号和回测全量覆盖）
            conn = get_factors_db()
            _save_results(conn, results, None)  # 只保存因子，不保存 EW-SDM 回测
            _save_rf_results(conn, rf_df, {
                'all': bt_all,
                'concept': bt_concept,
                'industry': bt_industry,
            })
            conn.close()

            logger.info(rf_calc_state['message'])

        except Exception as e:
            rf_calc_state['message'] = f'RF-EWSDM 计算失败: {str(e)}'
            logger.error(rf_calc_state['message'], exc_info=True)
        finally:
            rf_calc_state['running'] = False

    thread = threading.Thread(target=run_calc, daemon=True)
    thread.start()
    return {'status': 'started', 'message': 'RF-EWSDM 增量计算已启动'}


@app.get('/api/rf/chart/backtest')
def api_rf_backtest_chart(
    sector_type: str = Query('all'),
    start_date: str = Query(None),
    end_date: str = Query(None),
):
    """RF-EWSDM 回测净值曲线（叠加上证指数 + Buy/Sell 背景色块）"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, return_date, sector_type, daily_return, cum_return,
                   rf_signal, top10_concepts
            FROM rf_ewsdm_backtest WHERE sector_type = ? ORDER BY signal_date
        """, (sector_type,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return HTMLResponse(content="<html><body style='color:#94a3b8;text-align:center;padding:60px'>暂无回测数据</body></html>")

    data = [dict(r) for r in rows]
    if start_date:
        data = [d for d in data if d['signal_date'] >= start_date]
    if end_date:
        data = [d for d in data if d['signal_date'] <= end_date]
    if not data:
        return HTMLResponse(content="<html><body style='color:#94a3b8;text-align:center;padding:60px'>所选日期范围无数据</body></html>")

    dates_fmt = [d['signal_date'][:4]+'-'+d['signal_date'][4:6]+'-'+d['signal_date'][6:8] for d in data]
    dates_raw = [d['signal_date'] for d in data]
    cum = [d['cum_return'] for d in data]
    daily_pct = [d['daily_return'] for d in data]
    rf_signals = [d['rf_signal'] for d in data]

    # ==================== 读取上证指数日线 ====================
    import pandas as pd
    INDEX_DB = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/index_daily.db'
    idx_conn = sqlite3.connect(INDEX_DB)
    try:
        idx_df = pd.read_sql_query(f"""
            SELECT trade_date, close FROM daily
            WHERE ts_code = '000001.SH'
              AND trade_date >= '{dates_raw[0]}'
              AND trade_date <= '{dates_raw[-1]}'
            ORDER BY trade_date
        """, idx_conn)
    finally:
        idx_conn.close()

    # 构建日期->收盘价映射，并归一化为净值（首日=1）
    idx_close_map = dict(zip(idx_df['trade_date'], idx_df['close']))
    idx_nv = []
    base_close = None
    for d in dates_raw:
        if d in idx_close_map:
            if base_close is None:
                base_close = idx_close_map[d]
            idx_nv.append(idx_close_map[d] / base_close)
        else:
            idx_nv.append(idx_nv[-1] if idx_nv else 1.0)

    label = {'all':'RF-EWSDM 全部','concept':'RF-EWSDM 概念','industry':'RF-EWSDM 行业'}.get(sector_type, sector_type)
    color = {'all':'#22c55e','concept':'#a78bfa','industry':'#f97316'}.get(sector_type, '#22c55e')

    traces = []

    # ==================== Buy/Sell 背景色块 ====================
    # A股惯例：Buy=红色（持仓）, Sell=绿色（空仓）
    i = 0
    while i < len(data):
        sig = rf_signals[i]
        j = i
        while j < len(data) and rf_signals[j] == sig:
            j += 1
        # 区间 [i, j)
        if sig == 1:
            bg_color = 'rgba(239,68,68,0.08)'   # 红色背景 Buy
            bg_line = 'rgba(239,68,68,0.25)'
            bg_name = 'Buy 区间'
        else:
            bg_color = 'rgba(34,197,94,0.08)'    # 绿色背景 Sell
            bg_line = 'rgba(34,197,94,0.25)'
            bg_name = 'Sell 区间（空仓）'

        # 背景矩形（Y 范围基于实际净值数据）
        y_min = min(min(cum), min(idx_nv)) * 0.98
        y_max = max(max(cum), max(idx_nv)) * 1.02

        traces.append(go.Scatter(
            x=[dates_fmt[i], dates_fmt[j-1], dates_fmt[j-1], dates_fmt[i], dates_fmt[i]],
            y=[y_min, y_min, y_max, y_max, y_min],
            fill='toself',
            fillcolor=bg_color,
            line=dict(color=bg_line, width=0.5),
            hoverinfo='skip',
            showlegend=False,
        ))

        i = j

    # ==================== 上证指数净值（同轴）====================
    traces.append(go.Scatter(
        x=dates_fmt, y=idx_nv, name='上证指数(净值)',
        mode='lines',
        line=dict(color='#f59e0b', width=1.5, dash='dot'),
        hovertemplate='<b>%{x}</b><br>上证净值: %{y:.4f}<extra></extra>',
    ))

    # ==================== 策略净值（同轴）====================
    # Sell区间净值保持水平
    nv_display = []
    for i in range(len(data)):
        if rf_signals[i] == -1:
            nv_display.append(cum[i-1] if i > 0 else 1.0)
        else:
            nv_display.append(cum[i])

    traces.append(go.Scatter(
        x=dates_fmt, y=nv_display, name=label,
        mode='lines',
        line=dict(color=color, width=2.5),
        hovertemplate='<b>%{x}</b><br>策略净值: %{y:.4f}<br>日收益: %{customdata:.2f}%<extra></extra>',
        customdata=daily_pct,
    ))

    # 基准线 y=1
    traces.append(go.Scatter(
        x=[dates_fmt[0], dates_fmt[-1]], y=[1, 1], mode='lines', name='',
        line=dict(color='#475569', width=1, dash='dash'),
        showlegend=False, hoverinfo='skip',
    ))

    layout = go.Layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#94a3b8', family='-apple-system, BlinkMacSystemFont, sans-serif'),
        margin=dict(l=60, r=40, t=40, b=60),
        legend=dict(orientation='h', y=1.08, x=0, font=dict(size=12)),
        hovermode='x unified',
        hoverlabel=dict(bgcolor='#1a2234', bordercolor='#2a3548', font=dict(color='#e2e8f0', size=12)),
        xaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=11), tickmode='auto', nticks=12, showgrid=True),
        yaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=11), title=dict(text='净值', font=dict(size=12)), zeroline=False),
    )

    fig = go.Figure(data=traces, layout=layout)
    return HTMLResponse(content=fig.to_html(full_html=True, include_plotlyjs='/static/plotly.min.js', config={
        'responsive': True, 'displayModeBar': True, 'displaylogo': False, 'scrollZoom': True,
        'modeBarButtonsToRemove': ['lasso2d','select2d','autoScale2d','toggleSpikelines'],
    }))


@app.get('/rf')
def serve_rf():
    return FileResponse(os.path.join(BASE_DIR, 'static', 'rf_ewsdm.html'))


@app.post('/api/ews/calculate')
def api_ews_calculate():
    if calc_state['running']:
        raise HTTPException(409, "计算正在进行中，请稍后")

    calc_state['running'] = True
    calc_state['progress'] = 0
    calc_state['total'] = 0
    calc_state['current_date'] = ''
    calc_state['message'] = '准备中...'
    calc_state['start_time'] = datetime.now().isoformat()

    def run_calc():
        try:
            conn = get_factors_db()
            max_date = conn.execute("SELECT MAX(trade_date) FROM ews_daily").fetchone()[0]
            conn.close()

            start_date = '20230101'
            if max_date:
                start_date = max_date
                calc_state['message'] = f'增量更新，从 {start_date} 开始'

            def progress_cb(current, total, date):
                calc_state['progress'] = current
                calc_state['total'] = total
                calc_state['current_date'] = date
                calc_state['message'] = f'计算中 {current}/{total} ({date})'

            # Step 1: 增量计算新日期的因子
            results, _ = run_full_calculation(
                start_date=start_date,
                progress_cb=progress_cb
            )

            # Step 2: 先保存增量因子到数据库（否则Step3加载全量数据时读不到新因子）
            calc_state['message'] = '因子计算完成，保存增量因子...'
            conn = get_factors_db()
            _save_results(conn, results, None)
            conn.close()

            # Step 3: 加载全量因子数据计算回测（回测必须从第一天开始连续计算）
            calc_state['message'] = '加载全量数据计算回测...'
            conn = get_factors_db()
            all_dates = [r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM ews_daily ORDER BY trade_date"
            ).fetchall()]
            conn.close()

            all_results = []
            conn = get_factors_db()
            for d in all_dates:
                rows = conn.execute("""
                    SELECT trade_date, concept_code, concept_name, sector_type,
                           momentum, emotion_diff, s_score, final_score,
                           total_stocks, up_count, limit_up_count, broken_count, max_streak
                    FROM ews_daily WHERE trade_date = ?
                """, (d,)).fetchall()
                for r in rows:
                    all_results.append(dict(r))
            conn.close()

            bt_all = compute_backtest(all_results, sector_type=None)
            bt_concept = compute_backtest(all_results, sector_type='concept')
            bt_industry = compute_backtest(all_results, sector_type='industry')
            backtest = {'all': bt_all, 'concept': bt_concept, 'industry': bt_industry}

            # Step 4: 保存回测数据（因子已在Step2保存，这里只写回测）
            conn = get_factors_db()
            _save_results(conn, [], backtest)
            conn.close()

            total_bt = sum(len(v) for v in backtest.values())
            calc_state['message'] = f'计算完成：{len(results)} 条因子，{total_bt} 条回测'
            logger.info(calc_state['message'])

        except Exception as e:
            calc_state['message'] = f'计算失败: {str(e)}'
            logger.error(calc_state['message'], exc_info=True)
        finally:
            calc_state['running'] = False

    thread = threading.Thread(target=run_calc, daemon=True)
    thread.start()
    return {'status': 'started', 'message': 'EW-SDM 因子计算已启动'}


@app.post('/api/ews/recalculate')
def api_ews_recalculate():
    if calc_state['running']:
        raise HTTPException(409, "计算正在进行中，请稍后")

    calc_state['running'] = True
    calc_state['progress'] = 0
    calc_state['total'] = 0
    calc_state['message'] = '全量重算中...'

    def run_calc():
        try:
            def progress_cb(current, total, date):
                calc_state['progress'] = current
                calc_state['total'] = total
                calc_state['current_date'] = date
                calc_state['message'] = f'计算中 {current}/{total} ({date})'

            results, backtest = run_full_calculation(
                start_date='20260101',
                progress_cb=progress_cb
            )

            conn = get_factors_db()
            conn.execute("DELETE FROM ews_daily")
            conn.execute("DELETE FROM ews_backtest")
            _save_results(conn, results, backtest)
            conn.close()

            calc_state['message'] = f'全量重算完成：{len(results)} 条因子'
            logger.info(calc_state['message'])

        except Exception as e:
            calc_state['message'] = f'重算失败: {str(e)}'
            logger.error(calc_state['message'], exc_info=True)
        finally:
            calc_state['running'] = False

    thread = threading.Thread(target=run_calc, daemon=True)
    thread.start()
    return {'status': 'started', 'message': 'EW-SDM 全量重算已启动'}


# ==================== 图表 API（Python Plotly 服务端渲染） ====================

import plotly.graph_objects as go
from fastapi.responses import HTMLResponse, Response
import io, base64

PLOTLY_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<script src="https://cdn.bootcdn.net/ajax/libs/plotly.js/2.27.0/plotly.min.js"></script>
<style>body{{margin:0;padding:0;overflow:hidden;background:transparent;}}</style>
</head><body><div id="chart" style="width:100%;height:100vh;"></div>
<script>
Plotly.newPlot('chart', {traces}, {layout}, {{responsive:true,displayModeBar:true,modeBarButtonsToRemove:['lasso2d','select2d','autoScale2d','toggleSpikelines'],displaylogo:false,scrollZoom:true}});
window.addEventListener('resize', function(){{Plotly.Plots.resize('chart')}});
</script></body></html>"""


def _backtest_chart_html(sector_type: str, start_date: str = None, end_date: str = None) -> str:
    """生成回测净值曲线的完整 HTML（叠加上证指数净值）"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, return_date, sector_type, daily_return, cum_return, top10_concepts
            FROM ews_backtest WHERE sector_type = ? ORDER BY signal_date
        """, (sector_type,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return "<html><body style='color:#94a3b8;text-align:center;padding:60px'>暂无回测数据</body></html>"

    data = [dict(r) for r in rows]
    if start_date:
        data = [d for d in data if d['signal_date'] >= start_date]
    if end_date:
        data = [d for d in data if d['signal_date'] <= end_date]
    if not data:
        return "<html><body style='color:#94a3b8;text-align:center;padding:60px'>所选日期范围无数据</body></html>"

    # 使用完整日期格式 YYYY-MM-DD，Plotly 会自动识别为时间轴
    dates_fmt = [d['signal_date'][:4]+'-'+d['signal_date'][4:6]+'-'+d['signal_date'][6:8] for d in data]
    dates_raw = [d['signal_date'] for d in data]
    cum = [d['cum_return'] for d in data]
    daily_pct = [d['daily_return'] for d in data]
    top10_text = [d['top10_concepts'] for d in data]

    # ==================== 读取上证指数日线 ====================
    import pandas as pd
    INDEX_DB = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/index_daily.db'
    idx_conn = sqlite3.connect(INDEX_DB)
    try:
        idx_df = pd.read_sql_query(f"""
            SELECT trade_date, close FROM daily
            WHERE ts_code = '000001.SH'
              AND trade_date >= '{dates_raw[0]}'
              AND trade_date <= '{dates_raw[-1]}'
            ORDER BY trade_date
        """, idx_conn)
    finally:
        idx_conn.close()

    # 构建日期->收盘价映射，并归一化为净值（首日=1）
    idx_close_map = dict(zip(idx_df['trade_date'], idx_df['close']))
    idx_nv = []
    base_close = None
    for d in dates_raw:
        if d in idx_close_map:
            if base_close is None:
                base_close = idx_close_map[d]
            idx_nv.append(idx_close_map[d] / base_close)
        else:
            idx_nv.append(idx_nv[-1] if idx_nv else 1.0)

    label = {'all':'全部Top10','concept':'概念Top10','industry':'行业Top10'}.get(sector_type, sector_type)
    colors = {'all':'#3b82f6','concept':'#a78bfa','industry':'#f97316'}

    traces = []

    # 上证指数净值（黄色虚线，同轴）
    traces.append(go.Scatter(
        x=dates_fmt, y=idx_nv, name='上证指数(净值)',
        mode='lines',
        line=dict(color='#f59e0b', width=1.5, dash='dot'),
        hovertemplate='<b>%{x}</b><br>上证净值: %{y:.4f}<extra></extra>',
    ))

    # 主净值曲线
    customdata = list(zip(top10_text, [f'{v:+.2f}%' for v in daily_pct]))
    traces.append(go.Scatter(
        x=dates_fmt, y=cum, name=label,
        mode='lines',
        line=dict(color=colors.get(sector_type,'#3b82f6'), width=2.5, shape='linear'),
        customdata=customdata,
        hovertemplate='<b>%{x}</b><br>净值: %{y:.4f}<br>日收益: %{customdata[1]}<br>Top10: %{customdata[0]}<extra></extra>',
    ))

    # 基准线 y=1
    traces.append(go.Scatter(
        x=[dates_fmt[0], dates_fmt[-1]], y=[1, 1], mode='lines', name='',
        line=dict(color='#475569', width=1, dash='dash'),
        showlegend=False, hoverinfo='skip',
    ))

    layout = go.Layout(
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#94a3b8', family='-apple-system, BlinkMacSystemFont, sans-serif'),
        margin=dict(l=60, r=40, t=40, b=60),
        legend=dict(orientation='h', y=1.08, x=0, font=dict(size=12)),
        hovermode='x unified',
        hoverlabel=dict(bgcolor='#1a2234', bordercolor='#2a3548', font=dict(color='#e2e8f0', size=12)),
        xaxis=dict(
            gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=11),
            tickmode='auto', nticks=12,
            showgrid=True,
        ),
        yaxis=dict(
            gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=11),
            title=dict(text='净值', font=dict(size=12)), zeroline=False,
        ),
    )

    fig = go.Figure(data=traces, layout=layout)
    return fig.to_html(full_html=True, include_plotlyjs='/static/plotly.min.js', config={
        'responsive': True, 'displayModeBar': True, 'displaylogo': False, 'scrollZoom': True,
        'modeBarButtonsToRemove': ['lasso2d','select2d','autoScale2d','toggleSpikelines'],
    })


@app.get('/api/ews/period_return')
def api_ews_period_return(
    start_date: str = Query(..., description="区间起始日期 YYYYMMDD"),
    end_date: str = Query(..., description="区间结束日期 YYYYMMDD"),
    sector_type: str = Query('all', description="板块类型: all / concept / industry"),
):
    """获取 EW-SDM 回测在指定区间内的策略和基准涨幅"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, cum_return FROM ews_backtest
            WHERE sector_type = ? AND signal_date >= ? AND signal_date <= ?
            ORDER BY signal_date
        """, (sector_type, start_date, end_date)).fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        raise HTTPException(404, "所选区间数据不足，至少需要2个交易日")

    # 策略涨幅：区间末 cum_return / 区间前一日 cum_return - 1
    # 区间前一日
    conn = get_factors_db()
    try:
        prev_row = conn.execute("""
            SELECT cum_return FROM ews_backtest
            WHERE sector_type = ? AND signal_date < ?
            ORDER BY signal_date DESC LIMIT 1
        """, (sector_type, start_date)).fetchone()
    finally:
        conn.close()

    start_cum = prev_row['cum_return'] if prev_row else rows[0]['cum_return']
    end_cum = rows[-1]['cum_return']
    strategy_return = (end_cum / start_cum - 1) * 100

    # 基准涨幅：上证指数
    import pandas as pd
    INDEX_DB = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/index_daily.db'
    idx_conn = sqlite3.connect(INDEX_DB)
    try:
        idx_df = pd.read_sql_query("""
            SELECT trade_date, close FROM daily
            WHERE ts_code = '000001.SH'
              AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date
        """, idx_conn, params=(start_date, end_date))
    finally:
        idx_conn.close()

    if len(idx_df) < 2:
        raise HTTPException(404, "所选区间无基准指数数据")

    benchmark_return = (idx_df.iloc[-1]['close'] / idx_df.iloc[0]['close'] - 1) * 100

    # 区间内最大回撤
    cum_values = [r['cum_return'] for r in rows]
    if prev_row:
        cum_values = [prev_row['cum_return']] + cum_values
    max_dd = 0
    peak = cum_values[0]
    for v in cum_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    # 区间内胜率
    conn = get_factors_db()
    try:
        daily_rows = conn.execute("""
            SELECT daily_return FROM ews_backtest
            WHERE sector_type = ? AND signal_date >= ? AND signal_date <= ?
        """, (sector_type, start_date, end_date)).fetchall()
    finally:
        conn.close()

    daily_rets = [r['daily_return'] for r in daily_rows]
    wins = sum(1 for r in daily_rets if r > 0)
    win_rate = (wins / len(daily_rets) * 100) if daily_rets else 0

    return {
        'start_date': start_date,
        'end_date': end_date,
        'sector_type': sector_type,
        'trading_days': len(rows),
        'strategy_return': round(strategy_return, 2),
        'benchmark_return': round(benchmark_return, 2),
        'excess_return': round(strategy_return - benchmark_return, 2),
        'max_drawdown': round(-max_dd * 100, 2),
        'win_rate': round(win_rate, 1),
    }


@app.get('/api/rf/period_return')
def api_rf_period_return(
    start_date: str = Query(..., description="区间起始日期 YYYYMMDD"),
    end_date: str = Query(..., description="区间结束日期 YYYYMMDD"),
    sector_type: str = Query('all', description="板块类型: all / concept / industry"),
):
    """获取 RF-EWSDM 回测在指定区间内的策略和基准涨幅"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, cum_return, rf_signal FROM rf_ewsdm_backtest
            WHERE sector_type = ? AND signal_date >= ? AND signal_date <= ?
            ORDER BY signal_date
        """, (sector_type, start_date, end_date)).fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        raise HTTPException(404, "所选区间数据不足，至少需要2个交易日")

    # 策略涨幅
    conn = get_factors_db()
    try:
        prev_row = conn.execute("""
            SELECT cum_return FROM rf_ewsdm_backtest
            WHERE sector_type = ? AND signal_date < ?
            ORDER BY signal_date DESC LIMIT 1
        """, (sector_type, start_date)).fetchone()
    finally:
        conn.close()

    start_cum = prev_row['cum_return'] if prev_row else rows[0]['cum_return']
    end_cum = rows[-1]['cum_return']
    strategy_return = (end_cum / start_cum - 1) * 100

    # 基准涨幅
    import pandas as pd
    INDEX_DB = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/index_daily.db'
    idx_conn = sqlite3.connect(INDEX_DB)
    try:
        idx_df = pd.read_sql_query("""
            SELECT trade_date, close FROM daily
            WHERE ts_code = '000001.SH'
              AND trade_date >= ? AND trade_date <= ?
            ORDER BY trade_date
        """, idx_conn, params=(start_date, end_date))
    finally:
        idx_conn.close()

    if len(idx_df) < 2:
        raise HTTPException(404, "所选区间无基准指数数据")

    benchmark_return = (idx_df.iloc[-1]['close'] / idx_df.iloc[0]['close'] - 1) * 100

    # 区间内最大回撤
    cum_values = [r['cum_return'] for r in rows]
    if prev_row:
        cum_values = [prev_row['cum_return']] + cum_values
    max_dd = 0
    peak = cum_values[0]
    for v in cum_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    # 区间 Buy 占比
    buy_days = sum(1 for r in rows if r['rf_signal'] == 1)
    buy_pct = (buy_days / len(rows) * 100) if rows else 0

    # 区间胜率
    conn = get_factors_db()
    try:
        daily_rows = conn.execute("""
            SELECT daily_return FROM rf_ewsdm_backtest
            WHERE sector_type = ? AND signal_date >= ? AND signal_date <= ?
        """, (sector_type, start_date, end_date)).fetchall()
    finally:
        conn.close()

    daily_rets = [r['daily_return'] for r in daily_rows]
    wins = sum(1 for r in daily_rets if r > 0)
    win_rate = (wins / len(daily_rets) * 100) if daily_rets else 0

    return {
        'start_date': start_date,
        'end_date': end_date,
        'sector_type': sector_type,
        'trading_days': len(rows),
        'strategy_return': round(strategy_return, 2),
        'benchmark_return': round(benchmark_return, 2),
        'excess_return': round(strategy_return - benchmark_return, 2),
        'max_drawdown': round(-max_dd * 100, 2),
        'win_rate': round(win_rate, 1),
        'buy_pct': round(buy_pct, 1),
    }


@app.get('/api/ews/chart/backtest')
def api_ews_backtest_chart(
    sector_type: str = Query('all'),
    start_date: str = Query(None),
    end_date: str = Query(None),
):
    html = _backtest_chart_html(sector_type, start_date, end_date)
    return HTMLResponse(content=html)


@app.get('/api/ews/chart/detail/{concept_code}')
def api_ews_detail_chart(concept_code: str):
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT trade_date, final_score, momentum, limit_up_count
            FROM ews_daily WHERE concept_code = ? ORDER BY trade_date
        """, (concept_code,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return HTMLResponse(content="<html><body style='color:#94a3b8;text-align:center;padding:60px'>无数据</body></html>")

    history = [dict(r) for r in rows]
    dates = [h['trade_date'][4:6]+'/'+h['trade_date'][6:8] for h in history]
    scores = [h['final_score'] for h in history]
    momentums = [h['momentum'] for h in history]
    score_colors = ['#22c55e' if v >= 0 else '#ef4444' for v in scores]

    traces = [
        go.Bar(x=dates, y=scores, name='EW-SDM', marker_color=score_colors,
               hovertemplate='%{x}<br>Score: %{y:.3f}<extra></extra>'),
        go.Scatter(x=dates, y=momentums, name='Momentum%', yaxis='y2',
                   line=dict(color='#f59e0b', width=1.5),
                   hovertemplate='%{x}<br>Mom: %{y:.3f}%<extra></extra>'),
    ]

    layout = go.Layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#94a3b8', family='-apple-system, BlinkMacSystemFont, sans-serif'),
        margin=dict(l=50, r=50, t=35, b=50),
        legend=dict(orientation='h', y=1.1, x=0, font=dict(size=11)),
        barmode='relative',
        xaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=10),
                   rangeslider=dict(visible=True, thickness=0.04, bgcolor='#111827', bordercolor='#2a3548')),
        yaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=10), zeroline=False),
        yaxis2=dict(overlaying='y', side='right', linecolor='#2a3548', tickfont=dict(size=10),
                    gridcolor='rgba(0,0,0,0)', zeroline=False),
    )

    fig = go.Figure(data=traces, layout=layout)
    return HTMLResponse(content=fig.to_html(full_html=True, include_plotlyjs='/static/plotly.min.js', config={
        'responsive': True, 'displayModeBar': True, 'displaylogo': False, 'scrollZoom': True,
    }))


# ==================== 增量刷新 API（前端刷新按钮调用） ====================

@app.post('/api/ews/refresh')
def api_ews_refresh():
    """EW-SDM 增量刷新：计算新日期的因子 + 回测，一步到位"""
    if calc_state['running']:
        raise HTTPException(409, "EW-SDM 计算正在进行中")

    calc_state['running'] = True
    calc_state['progress'] = 0
    calc_state['total'] = 0
    calc_state['current_date'] = ''
    calc_state['message'] = '增量刷新中...'
    calc_state['start_time'] = datetime.now().isoformat()

    def run_refresh():
        try:
            conn = get_factors_db()
            max_date = conn.execute("SELECT MAX(trade_date) FROM ews_daily").fetchone()[0]
            conn.close()

            start_date = max_date if max_date else '20230101'

            def progress_cb(current, total, date):
                calc_state['progress'] = current
                calc_state['total'] = total
                calc_state['current_date'] = date
                calc_state['message'] = f'增量计算 {current}/{total} ({date})'

            # Step 1: 增量计算新日期的因子
            results, _ = run_full_calculation(
                start_date=start_date,
                progress_cb=progress_cb
            )

            # Step 2: 先保存增量因子到数据库（否则Step3加载全量数据时读不到新因子）
            calc_state['message'] = '因子计算完成，保存增量因子...'
            conn = get_factors_db()
            _save_results(conn, results, None)  # 只保存因子，暂不写回测
            conn.close()

            # Step 3: 加载全量因子数据计算回测（回测必须从第一天开始连续计算）
            calc_state['message'] = '加载全量数据计算回测...'
            conn = get_factors_db()
            all_dates = [r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM ews_daily ORDER BY trade_date"
            ).fetchall()]
            conn.close()

            all_results = []
            conn = get_factors_db()
            for d in all_dates:
                rows = conn.execute("""
                    SELECT trade_date, concept_code, concept_name, sector_type,
                           momentum, emotion_diff, s_score, final_score,
                           total_stocks, up_count, limit_up_count, broken_count, max_streak
                    FROM ews_daily WHERE trade_date = ?
                """, (d,)).fetchall()
                for r in rows:
                    all_results.append(dict(r))
            conn.close()

            bt_all = compute_backtest(all_results, sector_type=None)
            bt_concept = compute_backtest(all_results, sector_type='concept')
            bt_industry = compute_backtest(all_results, sector_type='industry')
            backtest = {'all': bt_all, 'concept': bt_concept, 'industry': bt_industry}

            # Step 4: 保存回测数据（因子已在Step2保存，这里只写回测）
            conn = get_factors_db()
            _save_results(conn, [], backtest)
            conn.close()

            total_bt = sum(len(v) for v in backtest.values())
            calc_state['message'] = f'刷新完成：{len(results)} 条因子，{total_bt} 条回测'
            logger.info(calc_state['message'])

        except Exception as e:
            calc_state['message'] = f'刷新失败: {str(e)}'
            logger.error(calc_state['message'], exc_info=True)
        finally:
            calc_state['running'] = False

    thread = threading.Thread(target=run_refresh, daemon=True)
    thread.start()
    return {'status': 'started', 'message': 'EW-SDM 增量刷新已启动'}


@app.post('/api/rf/refresh')
def api_rf_refresh():
    """RF-EWSDM 增量刷新：更新 EW-SDM 因子 + RF 信号 + 回测，一步到位"""
    if rf_calc_state['running']:
        raise HTTPException(409, "RF-EWSDM 计算正在进行中")
    if calc_state['running']:
        raise HTTPException(409, "EW-SDM 计算正在进行中，请等待完成后再刷新")

    rf_calc_state['running'] = True
    rf_calc_state['progress'] = 0
    rf_calc_state['total'] = 0
    rf_calc_state['message'] = 'RF-EWSDM 增量刷新中...'

    def run_refresh():
        try:
            conn = get_factors_db()
            max_ews_date = conn.execute("SELECT MAX(trade_date) FROM ews_daily").fetchone()[0]
            conn.close()

            # Step 1: 增量计算 EW-SDM 因子
            ews_start = max_ews_date if max_ews_date else '20230101'
            rf_calc_state['message'] = f'增量更新 EW-SDM 从 {ews_start} 开始'

            def progress_cb(current, total, date):
                rf_calc_state['progress'] = current
                rf_calc_state['total'] = total
                rf_calc_state['current_date'] = date
                rf_calc_state['message'] = f'因子计算 {current}/{total} ({date})'

            results, _ = run_full_calculation(start_date=ews_start, progress_cb=progress_cb)

            # 先保存增量因子到数据库（否则后续加载全量数据时读不到新因子）
            conn = get_factors_db()
            _save_results(conn, results, None)
            conn.close()

            rf_calc_state['message'] = 'EW-SDM 完成，计算 RF 信号...'

            # Step 2: 计算 Range Filter 信号（必须从20230101全量计算，EMA状态依赖）
            rf_df = compute_rf_signals('20230101')

            # Step 3: RF-EWSDM 回测（从数据库加载全量因子）
            conn = get_factors_db()
            existing_dates = [r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM ews_daily ORDER BY trade_date"
            ).fetchall()]
            conn.close()

            if existing_dates:
                conn_factors = get_factors_db()
                all_results = []
                for d in existing_dates:
                    rows = conn_factors.execute("""
                        SELECT trade_date, concept_code, concept_name, sector_type,
                               momentum, emotion_diff, s_score, final_score,
                               total_stocks, up_count, limit_up_count, broken_count, max_streak
                        FROM ews_daily WHERE trade_date = ?
                    """, (d,)).fetchall()
                    for r in rows:
                        all_results.append(dict(r))
                conn_factors.close()

                bt_all = compute_backtest_rf(all_results, sector_type=None)
                bt_concept = compute_backtest_rf(all_results, sector_type='concept')
                bt_industry = compute_backtest_rf(all_results, sector_type='industry')
            else:
                bt_all = compute_backtest_rf(results, sector_type=None)
                bt_concept = compute_backtest_rf(results, sector_type='concept')
                bt_industry = compute_backtest_rf(results, sector_type='industry')

            rf_calc_state['message'] = f'刷新完成：{len(results)} 条新因子，{len(bt_all)} 条 RF 回测'

            # Step 4: 保存 RF 信号和回测（因子已在前面保存）
            conn = get_factors_db()
            _save_rf_results(conn, rf_df, {
                'all': bt_all,
                'concept': bt_concept,
                'industry': bt_industry,
            })
            conn.close()

            logger.info(rf_calc_state['message'])

        except Exception as e:
            rf_calc_state['message'] = f'刷新失败: {str(e)}'
            logger.error(rf_calc_state['message'], exc_info=True)
        finally:
            rf_calc_state['running'] = False

    thread = threading.Thread(target=run_refresh, daemon=True)
    thread.start()
    return {'status': 'started', 'message': 'RF-EWSDM 增量刷新已启动'}


# ==================== BOCI 行业情绪指标 API ====================

boci_calc_state = {
    'running': False,
    'progress': 0,
    'total': 0,
    'current_date': '',
    'message': '',
    'start_time': None,
}

index_sentiment_calc_state = {
    'running': False,
    'progress': 0,
    'total': 0,
    'message': '',
}

lc_calc_state = {
    'running': False,
    'progress': 0,
    'total': 0,
    'message': '',
}


def _init_index_sentiment_tables(conn):
    """初始化宽基指数情绪表"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS index_sentiment (
            index_code TEXT NOT NULL,
            index_name TEXT,
            trade_date TEXT NOT NULL,
            sentiment REAL,
            f1_ma20 REAL,
            f2_rsi REAL,
            f3_turnover REAL,
            f4_limit REAL,
            f5_amount REAL,
            PRIMARY KEY (index_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_is_date ON index_sentiment(trade_date);
        CREATE INDEX IF NOT EXISTS idx_is_code ON index_sentiment(index_code);
    """)


def _init_boci_tables(conn):
    """初始化 BOCI 相关数据库表"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS boci_sentiment (
            industry_code TEXT NOT NULL,
            industry_name TEXT,
            trade_date TEXT NOT NULL,
            f1_ma20_ratio REAL,
            f2_rsi_norm REAL,
            f3_turnover_strength REAL,
            f4_limit_diff REAL,
            f5_amount_ratio REAL,
            sentiment REAL,
            PRIMARY KEY (industry_code, trade_date)
        );

        CREATE INDEX IF NOT EXISTS idx_boci_date ON boci_sentiment(trade_date);
        CREATE INDEX IF NOT EXISTS idx_boci_code ON boci_sentiment(industry_code);

        CREATE TABLE IF NOT EXISTS boci_scores (
            industry_code TEXT NOT NULL,
            industry_name TEXT,
            trade_date TEXT NOT NULL,
            sentiment REAL,
            s1_slope REAL,
            s2_accel REAL,
            s3_relative REAL,
            s4_momentum REAL,
            s5_price_slope REAL,
            rank_s1 REAL,
            rank_s2 REAL,
            rank_s3 REAL,
            rank_s4 REAL,
            rank_s5 REAL,
            final_score REAL,
            PRIMARY KEY (industry_code, trade_date)
        );

        CREATE INDEX IF NOT EXISTS idx_boci_score_date ON boci_scores(trade_date);
        CREATE INDEX IF NOT EXISTS idx_boci_score_rank ON boci_scores(trade_date, final_score DESC);

        CREATE TABLE IF NOT EXISTS boci_backtest (
            signal_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            daily_return REAL,
            cum_return REAL,
            top_industries TEXT,
            PRIMARY KEY (signal_date, return_date)
        );
    """)


def _save_boci_results(conn, sub_indicators, score_df, backtest):
    """将 BOCI 计算结果写入数据库（全量覆盖）"""
    # 清空旧数据
    conn.execute("DELETE FROM boci_sentiment")
    conn.execute("DELETE FROM boci_scores")
    conn.execute("DELETE FROM boci_backtest")

    # 写入子指标
    for r in sub_indicators:
        conn.execute("""
            INSERT OR REPLACE INTO boci_sentiment VALUES (?,?,?,?,?,?,?,?,?)
        """, (r['industry_code'], r['industry_name'], r['trade_date'],
              r.get('f1_ma20_ratio'), r.get('f2_rsi_norm'),
              r.get('f3_turnover_strength'), r.get('f4_limit_diff'),
              r.get('f5_amount_ratio'), r.get('sentiment')))

    # 写入截面打分
    try:
        has_score = hasattr(score_df, '__len__') and len(score_df) > 0
    except:
        has_score = False
    if has_score:
        for _, row in score_df.iterrows():
            conn.execute("""
                INSERT OR REPLACE INTO boci_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (row['industry_code'], row.get('industry_name', ''),
                  row['trade_date'], row.get('sentiment'),
                  row.get('s1_slope'), row.get('s2_accel'),
                  row.get('s3_relative'), row.get('s4_momentum'),
                  row.get('s5_price_slope'),
                  row.get('rank_s1'), row.get('rank_s2'),
                  row.get('rank_s3'), row.get('rank_s4'),
                  row.get('rank_s5'), row.get('final_score')))

    # 写入回测
    for b in backtest:
        conn.execute("""
            INSERT OR REPLACE INTO boci_backtest VALUES (?,?,?,?,?)
        """, (b['signal_date'], b['return_date'],
              b['daily_return'], b['cum_return'],
              json.dumps(b['top_industries'], ensure_ascii=False)))

    conn.execute("""
        INSERT OR REPLACE INTO calc_meta (key, value) VALUES ('boci_params', ?)
    """, (json.dumps(BOCI_PARAMS, default=str),))

    conn.commit()


def _save_boci_incremental_results(conn, new_sub_indicators, new_score_df, full_backtest):
    """将 BOCI 增量计算结果写入数据库（只追加新数据，回测全量覆盖）"""
    
    # 增量写入子指标（INSERT OR REPLACE 自动去重）
    for r in new_sub_indicators:
        conn.execute("""
            INSERT OR REPLACE INTO boci_sentiment VALUES (?,?,?,?,?,?,?,?,?)
        """, (r['industry_code'], r['industry_name'], r['trade_date'],
              r.get('f1_ma20_ratio'), r.get('f2_rsi_norm'),
              r.get('f3_turnover_strength'), r.get('f4_limit_diff'),
              r.get('f5_amount_ratio'), r.get('sentiment')))

    # 增量写入截面打分
    try:
        has_score = hasattr(new_score_df, '__len__') and len(new_score_df) > 0
    except:
        has_score = False
    if has_score:
        for _, row in new_score_df.iterrows():
            conn.execute("""
                INSERT OR REPLACE INTO boci_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (row['industry_code'], row.get('industry_name', ''),
                  row['trade_date'], row.get('sentiment'),
                  row.get('s1_slope'), row.get('s2_accel'),
                  row.get('s3_relative'), row.get('s4_momentum'),
                  row.get('s5_price_slope'),
                  row.get('rank_s1'), row.get('rank_s2'),
                  row.get('rank_s3'), row.get('rank_s4'),
                  row.get('rank_s5'), row.get('final_score')))

    # 回测全量覆盖（cum_return 依赖连续计算）
    conn.execute("DELETE FROM boci_backtest")
    for b in full_backtest:
        conn.execute("""
            INSERT OR REPLACE INTO boci_backtest VALUES (?,?,?,?,?)
        """, (b['signal_date'], b['return_date'],
              b['daily_return'], b['cum_return'],
              json.dumps(b['top_industries'], ensure_ascii=False)))

    conn.commit()


@app.get('/api/boci/status')
def api_boci_status():
    """获取 BOCI 计算状态"""
    conn = get_factors_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(DISTINCT trade_date) as trade_days,
                   MIN(trade_date) as min_date,
                   MAX(trade_date) as max_date,
                   COUNT(*) as total_records
            FROM boci_sentiment
        """).fetchone()

        bt_stats = conn.execute("""
            SELECT COUNT(*) as total, MIN(signal_date) as min_date, MAX(signal_date) as max_date
            FROM boci_backtest
        """).fetchone()

        has_data = stats['total_records'] > 0
        has_bt = bt_stats['total'] > 0

        # 最新一期 Top3
        top3 = []
        if has_data:
            latest = stats['max_date']
            rows = conn.execute("""
                SELECT industry_code, industry_name, sentiment, final_score
                FROM boci_scores
                WHERE trade_date = ?
                ORDER BY final_score DESC LIMIT 3
            """, (latest,)).fetchall()
            top3 = [dict(r) for r in rows]

        return {
            'calc_running': boci_calc_state['running'],
            'calc_progress': boci_calc_state['progress'],
            'calc_total': boci_calc_state['total'],
            'calc_message': boci_calc_state['message'],
            'has_data': has_data,
            'trade_days': stats['trade_days'] if has_data else 0,
            'min_date': stats['min_date'] if has_data else None,
            'max_date': stats['max_date'] if has_data else None,
            'total_records': stats['total_records'] if has_data else 0,
            'has_backtest': has_bt,
            'bt_records': bt_stats['total'] if has_bt else 0,
            'latest_top3': top3,
        }
    finally:
        conn.close()


@app.get('/api/boci/ranking')
def api_boci_ranking(
    date: str = Query(..., description="交易日期 YYYYMMDD"),
    top_n: int = Query(None, description="只返回前N条")
):
    """获取某日 BOCI 截面排名"""
    conn = get_factors_db()
    try:
        limit = f"LIMIT {top_n}" if top_n else ""
        rows = conn.execute(f"""
            SELECT sc.*, se.f1_ma20_ratio, se.f2_rsi_norm,
                   se.f3_turnover_strength, se.f4_limit_diff, se.f5_amount_ratio
            FROM boci_scores sc
            LEFT JOIN boci_sentiment se ON sc.industry_code = se.industry_code AND sc.trade_date = se.trade_date
            WHERE sc.trade_date = ?
            ORDER BY sc.final_score DESC
            {limit}
        """, (date,)).fetchall()

        if not rows:
            raise HTTPException(404, f"日期 {date} 无数据")

        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/boci/dates')
def api_boci_dates():
    """获取所有有数据的交易日"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT trade_date FROM boci_scores
            GROUP BY trade_date ORDER BY trade_date DESC
        """).fetchall()
        return [r['trade_date'] for r in rows]
    finally:
        conn.close()


@app.get('/api/boci/backtest')
def api_boci_backtest():
    """获取 BOCI 回测数据"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, return_date, daily_return, cum_return, top_industries
            FROM boci_backtest ORDER BY signal_date
        """).fetchall()

        if not rows:
            raise HTTPException(404, "暂无 BOCI 回测数据")

        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/boci/industry/{industry_code}')
def api_boci_industry_history(industry_code: str):
    """获取某行业的历史情绪数据"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT * FROM boci_sentiment
            WHERE industry_code = ?
            ORDER BY trade_date
        """, (industry_code,)).fetchall()

        if not rows:
            raise HTTPException(404, f"行业 {industry_code} 无数据")

        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post('/api/boci/calculate')
def api_boci_calculate():
    """启动 BOCI 全量计算"""
    if boci_calc_state['running']:
        raise HTTPException(409, "BOCI 计算正在进行中")

    boci_calc_state['running'] = True
    boci_calc_state['progress'] = 0
    boci_calc_state['total'] = 0
    boci_calc_state['message'] = '准备中...'

    def run_calc():
        try:
            def progress_cb(current, total, msg):
                boci_calc_state['progress'] = current
                boci_calc_state['total'] = total
                boci_calc_state['message'] = msg

            sub_indicators, score_df, backtest = run_boci_calculation(
                start_date='20200102',
                progress_cb=progress_cb
            )

            conn = get_factors_db()
            _save_boci_results(conn, sub_indicators, score_df, backtest)
            conn.close()

            boci_calc_state['message'] = f'计算完成：{len(sub_indicators)} 条情绪，{len(backtest)} 条回测'
            logger.info(boci_calc_state['message'])

        except Exception as e:
            boci_calc_state['message'] = f'计算失败: {str(e)}'
            logger.error(boci_calc_state['message'], exc_info=True)
        finally:
            boci_calc_state['running'] = False

    thread = threading.Thread(target=run_calc, daemon=True)
    thread.start()
    return {'status': 'started', 'message': 'BOCI 全量计算已启动'}


@app.post('/api/boci/refresh')
def api_boci_refresh():
    """启动 BOCI 增量刷新（只计算新日期的数据）"""
    if boci_calc_state['running']:
        raise HTTPException(409, "BOCI 计算正在进行中")

    conn = get_factors_db()
    try:
        max_date = conn.execute(
            "SELECT MAX(trade_date) FROM boci_sentiment"
        ).fetchone()[0]
    finally:
        conn.close()

    if not max_date:
        raise HTTPException(400, "暂无数据，请先执行全量计算")

    boci_calc_state['running'] = True
    boci_calc_state['progress'] = 0
    boci_calc_state['total'] = 0
    boci_calc_state['message'] = f'增量刷新: 从 {max_date} 之后开始...'

    def run_refresh():
        try:
            import pandas as pd
            
            def progress_cb(current, total, msg):
                boci_calc_state['progress'] = current
                boci_calc_state['total'] = total
                boci_calc_state['message'] = msg

            # 加载已有的情绪数据
            conn = get_factors_db()
            existing_sentiment_df = pd.read_sql_query(
                "SELECT * FROM boci_sentiment", conn
            )
            
            # 加载已有的回测数据
            existing_backtest_rows = conn.execute(
                "SELECT signal_date, return_date, daily_return, cum_return, top_industries FROM boci_backtest ORDER BY signal_date"
            ).fetchall()
            existing_backtest = []
            for r in existing_backtest_rows:
                existing_backtest.append({
                    'signal_date': r[0],
                    'return_date': r[1],
                    'daily_return': r[2],
                    'cum_return': r[3],
                    'top_industries': json.loads(r[4]) if r[4] else [],
                })
            conn.close()
            
            logger.info(f"BOCI 增量: 已有情绪 {len(existing_sentiment_df)} 条, 回测 {len(existing_backtest)} 条")

            # 增量计算
            new_sub_indicators, new_score_df, full_backtest = run_boci_incremental(
                existing_sentiment_df, existing_backtest, progress_cb=progress_cb
            )

            # 增量保存
            conn = get_factors_db()
            _save_boci_incremental_results(conn, new_sub_indicators, new_score_df, full_backtest)
            conn.close()

            new_bt_count = len(full_backtest) - len(existing_backtest)
            boci_calc_state['message'] = f'增量完成：{len(new_sub_indicators)} 条新情绪，{len(new_score_df)} 条新打分，{new_bt_count} 条新回测'
            logger.info(boci_calc_state['message'])

        except Exception as e:
            boci_calc_state['message'] = f'刷新失败: {str(e)}'
            logger.error(boci_calc_state['message'], exc_info=True)
        finally:
            boci_calc_state['running'] = False

    thread = threading.Thread(target=run_refresh, daemon=True)
    thread.start()
    return {'status': 'started', 'message': f'增量刷新已启动，上次数据截止 {max_date}'}


@app.get('/api/boci/chart/backtest')
def api_boci_backtest_chart(
    start_date: str = Query(None),
    end_date: str = Query(None),
):
    """BOCI 回测净值曲线（叠加上证指数 + 基准行业等权）"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, return_date, daily_return, cum_return, top_industries
            FROM boci_backtest ORDER BY signal_date
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        return HTMLResponse(content="<html><body style='color:#94a3b8;text-align:center;padding:60px'>暂无回测数据</body></html>")

    data = [dict(r) for r in rows]
    if start_date:
        data = [d for d in data if d['signal_date'] >= start_date]
    if end_date:
        data = [d for d in data if d['signal_date'] <= end_date]
    if not data:
        return HTMLResponse(content="<html><body style='color:#94a3b8;text-align:center;padding:60px'>所选日期范围无数据</body></html>")

    dates_fmt = [d['signal_date'][:4]+'-'+d['signal_date'][4:6]+'-'+d['signal_date'][6:8] for d in data]
    dates_raw = [d['signal_date'] for d in data]
    cum = [d['cum_return'] for d in data]
    daily_pct = [d['daily_return'] for d in data]

    # 读取上证指数
    import pandas as pd
    INDEX_DB = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/index_daily.db'
    idx_conn = sqlite3.connect(INDEX_DB)
    try:
        idx_df = pd.read_sql_query(f"""
            SELECT trade_date, close FROM daily
            WHERE ts_code = '000001.SH'
              AND trade_date >= '{dates_raw[0]}'
              AND trade_date <= '{dates_raw[-1]}'
            ORDER BY trade_date
        """, idx_conn)
    finally:
        idx_conn.close()

    idx_close_map = dict(zip(idx_df['trade_date'], idx_df['close']))
    idx_nv = []
    base_close = None
    for d in dates_raw:
        if d in idx_close_map:
            if base_close is None:
                base_close = idx_close_map[d]
            idx_nv.append(idx_close_map[d] / base_close)
        else:
            idx_nv.append(idx_nv[-1] if idx_nv else 1.0)

    # 读取881行业等权基准
    BOCI_INDUSTRY_DB = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/industry.db'
    conn2 = sqlite3.connect(BOCI_INDUSTRY_DB)
    try:
        # 68个有效行业的日线数据
        industry_daily = pd.read_sql_query(f"""
            SELECT ts_code, trade_date, pct_chg
            FROM ths_daily
            WHERE ts_code LIKE '881%'
              AND trade_date >= '{dates_raw[0]}'
              AND trade_date <= '{dates_raw[-1]}'
        """, conn2)
    finally:
        conn2.close()

    # 计算行业等权净值
    avg_ret = industry_daily.groupby('trade_date')['pct_chg'].mean()
    avg_ret = avg_ret.sort_index()
    eq_nv = [1.0]
    for d in dates_raw[1:]:
        r = avg_ret.get(d, 0)
        eq_nv.append(eq_nv[-1] * (1 + r / 100))

    traces = []

    # 上证指数
    traces.append(go.Scatter(
        x=dates_fmt, y=idx_nv, name='上证指数(净值)',
        mode='lines',
        line=dict(color='#f59e0b', width=1.5, dash='dot'),
        hovertemplate='<b>%{x}</b><br>上证净值: %{y:.4f}<extra></extra>',
    ))

    # 行业等权基准
    traces.append(go.Scatter(
        x=dates_fmt, y=eq_nv, name='68行业等权',
        mode='lines',
        line=dict(color='#94a3b8', width=1.5, dash='dash'),
        hovertemplate='<b>%{x}</b><br>行业等权: %{y:.4f}<extra></extra>',
    ))

    # BOCI 策略
    top3_text = [d['top_industries'] for d in data]
    customdata = list(zip(top3_text, [f'{v:+.2f}%' for v in daily_pct]))
    traces.append(go.Scatter(
        x=dates_fmt, y=cum, name='BOCI Top3',
        mode='lines',
        line=dict(color='#3b82f6', width=2.5),
        customdata=customdata,
        hovertemplate='<b>%{x}</b><br>净值: %{y:.4f}<br>日收益: %{customdata[1]}<extra></extra>',
    ))

    # 基准线
    traces.append(go.Scatter(
        x=[dates_fmt[0], dates_fmt[-1]], y=[1, 1], mode='lines', name='',
        line=dict(color='#475569', width=1, dash='dash'),
        showlegend=False, hoverinfo='skip',
    ))

    layout = go.Layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#94a3b8', family='-apple-system, BlinkMacSystemFont, sans-serif'),
        margin=dict(l=60, r=40, t=40, b=60),
        legend=dict(orientation='h', y=1.08, x=0, font=dict(size=12)),
        hovermode='x unified',
        hoverlabel=dict(bgcolor='#1a2234', bordercolor='#2a3548', font=dict(color='#e2e8f0', size=12)),
        xaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=11), tickmode='auto', nticks=12, showgrid=True),
        yaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=11), title=dict(text='净值', font=dict(size=12)), zeroline=False),
    )

    fig = go.Figure(data=traces, layout=layout)
    return HTMLResponse(content=fig.to_html(full_html=True, include_plotlyjs='/static/plotly.min.js', config={
        'responsive': True, 'displayModeBar': True, 'displaylogo': False, 'scrollZoom': True,
        'modeBarButtonsToRemove': ['lasso2d','select2d','autoScale2d','toggleSpikelines'],
    }))


@app.get('/api/boci/chart/sentiment/{industry_code}')
def api_boci_sentiment_chart(industry_code: str):
    """某行业情绪时序图"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT trade_date, f1_ma20_ratio, f2_rsi_norm, f3_turnover_strength,
                   f4_limit_diff, f5_amount_ratio, sentiment
            FROM boci_sentiment
            WHERE industry_code = ?
            ORDER BY trade_date
        """, (industry_code,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return HTMLResponse(content="<html><body style='color:#94a3b8;text-align:center;padding:60px'>无数据</body></html>")

    data = [dict(r) for r in rows]
    dates = [d['trade_date'][:4]+'-'+d['trade_date'][4:6]+'-'+d['trade_date'][6:8] for d in data]

    traces = [
        go.Scatter(x=dates, y=[d['sentiment'] for d in data], name='综合情绪',
                   line=dict(color='#3b82f6', width=2),
                   hovertemplate='%{x}<br>情绪: %{y:.4f}<extra></extra>'),
        go.Scatter(x=dates, y=[d['f1_ma20_ratio'] for d in data], name='F1:MA20占比',
                   line=dict(color='#22c55e', width=1), opacity=0.6,
                   hovertemplate='%{x}<br>F1: %{y:.4f}<extra></extra>'),
        go.Scatter(x=dates, y=[d['f2_rsi_norm'] for d in data], name='F2:RSI',
                   line=dict(color='#f59e0b', width=1), opacity=0.6,
                   hovertemplate='%{x}<br>F2: %{y:.4f}<extra></extra>'),
        go.Scatter(x=dates, y=[d['f3_turnover_strength'] for d in data], name='F3:换手率',
                   line=dict(color='#a78bfa', width=1), opacity=0.6,
                   hovertemplate='%{x}<br>F3: %{y:.4f}<extra></extra>'),
        go.Scatter(x=dates, y=[d.get('f4_limit_diff') for d in data], name='F4:涨跌停',
                   line=dict(color='#ef4444', width=1), opacity=0.6,
                   hovertemplate='%{x}<br>F4: %{y:.4f}<extra></extra>'),
        go.Scatter(x=dates, y=[d['f5_amount_ratio'] for d in data], name='F5:成交额占比',
                   line=dict(color='#f97316', width=1), opacity=0.6,
                   hovertemplate='%{x}<br>F5: %{y:.4f}<extra></extra>'),
    ]

    # 85% 过热线
    traces.append(go.Scatter(
        x=[dates[0], dates[-1]], y=[0.85, 0.85], mode='lines', name='过热阈值(85%)',
        line=dict(color='#ef4444', width=1, dash='dash'), showlegend=True,
    ))

    layout = go.Layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#94a3b8', family='-apple-system, BlinkMacSystemFont, sans-serif'),
        margin=dict(l=50, r=30, t=35, b=50),
        legend=dict(orientation='h', y=1.1, x=0, font=dict(size=10)),
        hovermode='x unified',
        xaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=10),
                   rangeslider=dict(visible=True, thickness=0.04, bgcolor='#111827', bordercolor='#2a3548')),
        yaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=10), zeroline=False),
    )

    fig = go.Figure(data=traces, layout=layout)
    return HTMLResponse(content=fig.to_html(full_html=True, include_plotlyjs='/static/plotly.min.js', config={
        'responsive': True, 'displayModeBar': True, 'displaylogo': False, 'scrollZoom': True,
    }))


@app.get('/api/boci/period_return')
def api_boci_period_return(
    start_date: str = Query(..., description="区间起始日期"),
    end_date: str = Query(..., description="区间结束日期"),
):
    """获取 BOCI 回测在指定区间内的表现"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, cum_return, daily_return FROM boci_backtest
            WHERE signal_date >= ? AND signal_date <= ?
            ORDER BY signal_date
        """, (start_date, end_date)).fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        raise HTTPException(404, "区间数据不足")

    # 策略收益
    conn = get_factors_db()
    try:
        prev_row = conn.execute("""
            SELECT cum_return FROM boci_backtest
            WHERE signal_date < ? ORDER BY signal_date DESC LIMIT 1
        """, (start_date,)).fetchone()
    finally:
        conn.close()

    start_cum = prev_row['cum_return'] if prev_row else rows[0]['cum_return']
    end_cum = rows[-1]['cum_return']
    strategy_return = (end_cum / start_cum - 1) * 100

    # 基准收益
    import pandas as pd
    INDEX_DB = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/index_daily.db'
    idx_conn = sqlite3.connect(INDEX_DB)
    try:
        idx_df = pd.read_sql_query("""
            SELECT trade_date, close FROM daily
            WHERE ts_code = '000001.SH' AND trade_date >= ? AND trade_date <= ?
        """, idx_conn, params=(start_date, end_date))
    finally:
        idx_conn.close()

    benchmark_return = (idx_df.iloc[-1]['close'] / idx_df.iloc[0]['close'] - 1) * 100 if len(idx_df) >= 2 else 0

    # 最大回撤
    cum_values = [r['cum_return'] for r in rows]
    if prev_row:
        cum_values = [prev_row['cum_return']] + cum_values
    max_dd = 0
    peak = cum_values[0]
    for v in cum_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak
        if dd > max_dd:
            max_dd = dd

    # 胜率
    daily_rets = [r['daily_return'] for r in rows]
    wins = sum(1 for r in daily_rets if r > 0)
    win_rate = (wins / len(daily_rets) * 100) if daily_rets else 0

    return {
        'start_date': start_date,
        'end_date': end_date,
        'trading_days': len(rows),
        'strategy_return': round(strategy_return, 2),
        'benchmark_return': round(benchmark_return, 2),
        'excess_return': round(strategy_return - benchmark_return, 2),
        'max_drawdown': round(-max_dd * 100, 2),
        'win_rate': round(win_rate, 1),
    }


# ==================== 宽基指数情绪指数 API ====================

@app.get('/api/index-sentiment/status')
def api_index_sentiment_status():
    """获取宽基指数情绪计算状态"""
    conn = get_factors_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(*) as total_records,
                   COUNT(DISTINCT trade_date) as trade_days,
                   MIN(trade_date) as min_date,
                   MAX(trade_date) as max_date
            FROM index_sentiment
        """).fetchone()

        # 各指数最新情绪值
        latest_values = {}
        if stats['total_records'] > 0:
            for idx_code, idx_name in INDEX_CONFIG.items():
                row = conn.execute("""
                    SELECT trade_date, sentiment FROM index_sentiment
                    WHERE index_code = ?
                    ORDER BY trade_date DESC LIMIT 1
                """, (idx_code,)).fetchone()
                if row:
                    latest_values[idx_code] = {
                        'name': idx_name,
                        'date': row['trade_date'],
                        'sentiment': round(row['sentiment'], 4),
                    }

        has_data = stats['total_records'] > 0

        # 格式化日期为 YYYY-MM-DD
        def fmt_date(d):
            if d and len(d) == 8 and d.isdigit():
                return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            return d

        return {
            'calc_running': index_sentiment_calc_state['running'],
            'calc_progress': index_sentiment_calc_state['progress'],
            'calc_total': index_sentiment_calc_state['total'],
            'calc_message': index_sentiment_calc_state['message'],
            'has_data': has_data,
            'trade_days': stats['trade_days'] if has_data else 0,
            'min_date': fmt_date(stats['min_date']) if has_data else None,
            'max_date': fmt_date(stats['max_date']) if has_data else None,
            'total_records': stats['total_records'] if has_data else 0,
            'latest_values': latest_values,
        }
    finally:
        conn.close()


@app.post('/api/index-sentiment/calculate')
def api_index_sentiment_calculate():
    """启动宽基指数情绪全量计算"""
    if index_sentiment_calc_state['running']:
        raise HTTPException(409, "宽基指数情绪计算正在进行中")

    index_sentiment_calc_state['running'] = True
    index_sentiment_calc_state['progress'] = 0
    index_sentiment_calc_state['total'] = 0
    index_sentiment_calc_state['message'] = '准备中...'

    def run_calc():
        try:
            def progress_cb(current, total, msg):
                index_sentiment_calc_state['progress'] = current
                index_sentiment_calc_state['total'] = total
                index_sentiment_calc_state['message'] = msg

            result_df, index_daily_df = run_index_sentiment_calculation(
                start_date='20200102',
                progress_cb=progress_cb
            )

            # 保存到数据库
            conn = get_factors_db()
            conn.execute("DELETE FROM index_sentiment")

            if len(result_df) > 0:
                for _, row in result_df.iterrows():
                    conn.execute("""
                        INSERT OR REPLACE INTO index_sentiment VALUES (?,?,?,?,?,?,?,?,?)
                    """, (
                        row['index_code'], row['index_name'], row['trade_date'],
                        row.get('sentiment'), row.get('f1_ma20'),
                        row.get('f2_rsi'), row.get('f3_turnover'),
                        row.get('f4_limit'), row.get('f5_amount'),
                    ))

            conn.commit()
            conn.close()

            index_sentiment_calc_state['message'] = f'计算完成：{len(result_df)} 条记录'
            logger.info(index_sentiment_calc_state['message'])

        except Exception as e:
            index_sentiment_calc_state['message'] = f'计算失败: {str(e)}'
            logger.error(index_sentiment_calc_state['message'], exc_info=True)
        finally:
            index_sentiment_calc_state['running'] = False

    thread = threading.Thread(target=run_calc, daemon=True)
    thread.start()
    return {'status': 'started', 'message': '宽基指数情绪计算已启动'}


@app.post('/api/index-sentiment/refresh')
def api_index_sentiment_refresh():
    """增量刷新宽基指数情绪数据"""
    if index_sentiment_calc_state['running']:
        raise HTTPException(409, "宽基指数情绪计算正在进行中")

    conn = get_factors_db()
    try:
        max_date = conn.execute(
            "SELECT MAX(trade_date) FROM index_sentiment"
        ).fetchone()[0]
    finally:
        conn.close()

    if not max_date:
        raise HTTPException(400, "暂无数据，请先执行全量计算")

    index_sentiment_calc_state['running'] = True
    index_sentiment_calc_state['progress'] = 0
    index_sentiment_calc_state['total'] = 0
    index_sentiment_calc_state['message'] = f'增量刷新: 从 {max_date} 之后开始重算...'

    def run_refresh():
        try:
            def progress_cb(current, total, msg):
                index_sentiment_calc_state['progress'] = current
                index_sentiment_calc_state['total'] = total
                index_sentiment_calc_state['message'] = msg

            result_df, index_daily_df = run_index_sentiment_calculation(
                start_date='20200102',
                progress_cb=progress_cb
            )

            conn = get_factors_db()
            conn.execute("DELETE FROM index_sentiment")

            if len(result_df) > 0:
                for _, row in result_df.iterrows():
                    conn.execute("""
                        INSERT OR REPLACE INTO index_sentiment VALUES (?,?,?,?,?,?,?,?,?)
                    """, (
                        row['index_code'], row['index_name'], row['trade_date'],
                        row.get('sentiment'), row.get('f1_ma20'),
                        row.get('f2_rsi'), row.get('f3_turnover'),
                        row.get('f4_limit'), row.get('f5_amount'),
                    ))

            conn.commit()
            conn.close()

            index_sentiment_calc_state['message'] = f'刷新完成：{len(result_df)} 条记录'
            logger.info(index_sentiment_calc_state['message'])

        except Exception as e:
            index_sentiment_calc_state['message'] = f'刷新失败: {str(e)}'
            logger.error(index_sentiment_calc_state['message'], exc_info=True)
        finally:
            index_sentiment_calc_state['running'] = False

    thread = threading.Thread(target=run_refresh, daemon=True)
    thread.start()
    return {'status': 'started', 'message': f'增量刷新已启动，上次数据截止 {max_date}'}


@app.get('/api/index-sentiment/chart')
def api_index_sentiment_chart():
    """获取宽基指数情绪时序数据"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT index_code, index_name, trade_date, sentiment,
                   f1_ma20, f2_rsi, f3_turnover, f4_limit, f5_amount
            FROM index_sentiment
            ORDER BY index_code, trade_date
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        raise HTTPException(404, "暂无宽基指数情绪数据")

    # 按指数分组
    result = {}
    for row in rows:
        code = row['index_code']
        if code not in result:
            result[code] = {
                'name': row['index_name'],
                'code': code,
                'dates': [],
                'sentiment': [],
                'f1_ma20': [],
                'f2_rsi': [],
                'f3_turnover': [],
                'f4_limit': [],
                'f5_amount': [],
            }
        # 格式化日期为 YYYY-MM-DD，Plotly 才能正确识别为 date 类型
        td = row['trade_date']
        if len(td) == 8 and td.isdigit():
            td = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
        result[code]['dates'].append(td)
        result[code]['sentiment'].append(round(row['sentiment'], 4) if row['sentiment'] else None)
        result[code]['f1_ma20'].append(round(row['f1_ma20'], 4) if row['f1_ma20'] else None)
        result[code]['f2_rsi'].append(round(row['f2_rsi'], 4) if row['f2_rsi'] else None)
        result[code]['f3_turnover'].append(round(row['f3_turnover'], 4) if row['f3_turnover'] else None)
        result[code]['f4_limit'].append(round(row['f4_limit'], 4) if row['f4_limit'] else None)
        result[code]['f5_amount'].append(round(row['f5_amount'], 4) if row['f5_amount'] else None)

    return list(result.values())


@app.get('/api/index-sentiment/latest')
def api_index_sentiment_latest():
    """获取各指数最新情绪值"""
    conn = get_factors_db()
    try:
        result = {}
        for idx_code, idx_name in INDEX_CONFIG.items():
            row = conn.execute("""
                SELECT trade_date, sentiment, f1_ma20, f2_rsi, f3_turnover, f4_limit, f5_amount
                FROM index_sentiment
                WHERE index_code = ?
                ORDER BY trade_date DESC LIMIT 1
            """, (idx_code,)).fetchone()
            if row:
                td = row['trade_date']
                if len(td) == 8 and td.isdigit():
                    td = f"{td[:4]}-{td[4:6]}-{td[6:8]}"
                result[idx_code] = {
                    'name': idx_name,
                    'date': td,
                    'sentiment': round(row['sentiment'], 4) if row['sentiment'] else None,
                    'factors': {
                        'f1_ma20': round(row['f1_ma20'], 4) if row['f1_ma20'] else None,
                        'f2_rsi': round(row['f2_rsi'], 4) if row['f2_rsi'] else None,
                        'f3_turnover': round(row['f3_turnover'], 4) if row['f3_turnover'] else None,
                        'f4_limit': round(row['f4_limit'], 4) if row['f4_limit'] else None,
                        'f5_amount': round(row['f5_amount'], 4) if row['f5_amount'] else None,
                    }
                }
        return result
    finally:
        conn.close()


@app.get('/index-sentiment')
def serve_index_sentiment():
    return FileResponse(os.path.join(BASE_DIR, 'static', 'index_sentiment.html'))


@app.get('/boci')
def serve_boci():
    return FileResponse(os.path.join(BASE_DIR, 'static', 'boci.html'))


# ==================== Lorentzian Classification API ====================

def _init_lc_tables(conn):
    """初始化 LC 相关数据库表"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lc_signals (
            trade_date TEXT PRIMARY KEY,
            close REAL,
            rf_signal INTEGER,
            rf_filter REAL,
            rf_smooth_range REAL,
            prediction REAL,
            wt REAL,
            rsi14 REAL,
            rsi9 REAL DEFAULT 0,
            atr REAL,
            yhat_rbf REAL,
            yhat_gauss REAL,
            signal INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_lc_date ON lc_signals(trade_date);
    """)

    # 兼容旧数据库: 安全添加新列（列已存在时忽略错误）
    for alter in [
        "ALTER TABLE lc_signals ADD COLUMN rsi9 REAL DEFAULT 0",
        "ALTER TABLE lc_trades ADD COLUMN entry_rsi9 REAL DEFAULT 0",
    ]:
        try:
            conn.execute(alter)
        except Exception:
            pass

    conn.executescript("""

        CREATE TABLE IF NOT EXISTS lc_backtest (
            signal_date TEXT NOT NULL,
            return_date TEXT NOT NULL,
            daily_return REAL,
            cum_return REAL,
            lc_signal INTEGER,
            prediction REAL,
            PRIMARY KEY (signal_date, return_date)
        );

        CREATE TABLE IF NOT EXISTS lc_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_date TEXT NOT NULL,
            exit_date TEXT NOT NULL,
            entry_price REAL,
            exit_price REAL,
            profit_pct REAL,
            win INTEGER,
            hold_days INTEGER,
            exit_reason TEXT,
            entry_wt REAL,
            entry_rsi REAL,
            entry_rsi9 REAL DEFAULT 0,
            entry_pred REAL
        );
    """)


@app.get('/api/lc/status')
def api_lc_status():
    """获取 LC 计算状态"""
    conn = get_factors_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(*) as total, MIN(trade_date) as min_date, MAX(trade_date) as max_date
            FROM lc_signals
        """).fetchone()

        bt_stats = conn.execute("""
            SELECT COUNT(*) as total, MIN(signal_date) as min_date, MAX(signal_date) as max_date
            FROM lc_backtest
        """).fetchone()

        trade_stats = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN win=1 THEN 1 ELSE 0 END) as wins
            FROM lc_trades
        """).fetchone()

        has_data = stats['total'] > 0
        has_bt = bt_stats['total'] > 0
        n_trades = trade_stats['total'] if trade_stats else 0
        n_wins = trade_stats['wins'] if trade_stats else 0
        trade_wr = (n_wins / n_trades * 100) if n_trades > 0 else 0

        return {
            'calc_running': lc_calc_state['running'],
            'calc_message': lc_calc_state['message'],
            'has_data': has_data,
            'total_days': stats['total'] if has_data else 0,
            'min_date': stats['min_date'] if has_data else None,
            'max_date': stats['max_date'] if has_data else None,
            'has_backtest': has_bt,
            'bt_records': bt_stats['total'] if has_bt else 0,
            'n_trades': n_trades,
            'trade_win_rate': round(trade_wr, 1),
            'params': LC_PARAMS,
        }
    finally:
        conn.close()


@app.get('/api/lc/signals')
def api_lc_signals(
    start_date: str = Query(None),
    end_date: str = Query(None),
):
    """获取 LC 信号数据"""
    conn = get_factors_db()
    try:
        where = ""
        params = []
        if start_date:
            where += " WHERE trade_date >= ?"
            params.append(start_date)
        if end_date:
            where += " AND trade_date <= ?" if where else " WHERE trade_date <= ?"
            params.append(end_date)

        rows = conn.execute(f"""
            SELECT trade_date, close, rf_signal, rf_filter, rf_smooth_range,
                   prediction, wt, rsi14, atr, signal
            FROM lc_signals {where} ORDER BY trade_date
        """, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/lc/backtest')
def api_lc_backtest():
    """获取 LC 回测数据"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, return_date, daily_return, cum_return, lc_signal, prediction
            FROM lc_backtest ORDER BY signal_date
        """).fetchall()

        if not rows:
            raise HTTPException(404, "暂无 LC 回测数据")

        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/lc/trades')
def api_lc_trades():
    """获取 LC 交易明细"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT * FROM lc_trades ORDER BY entry_date
        """).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.post('/api/lc/calculate')
def api_lc_calculate():
    """启动 LC 全量计算"""
    if lc_calc_state['running']:
        raise HTTPException(409, "LC 计算正在进行中")

    lc_calc_state['running'] = True
    lc_calc_state['progress'] = 0
    lc_calc_state['message'] = 'LC 计算中...'

    def run_calc():
        try:
            df_result, bt_results, trades = compute_lc_signals_full(
                start_date=LC_PARAMS['backtest_start'])

            # 保存到数据库
            conn = get_factors_db()
            conn.execute("DELETE FROM lc_signals")
            conn.execute("DELETE FROM lc_backtest")
            conn.execute("DELETE FROM lc_trades")

            for _, row in df_result.iterrows():
                conn.execute("""
                    INSERT OR REPLACE INTO lc_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    row['trade_date'], row['close'], int(row['rf_signal']),
                    row.get('rf_filter', 0), row.get('rf_smooth_range', 0),
                    row.get('prediction', 0), row.get('wt', 0),
                    row.get('rsi14', 0), row.get('rsi9', 0), row.get('atr', 0),
                    row.get('yhat_rbf', 0), row.get('yhat_gauss', 0),
                    int(row['signal']),
                ))

            for b in bt_results:
                conn.execute("""
                    INSERT OR REPLACE INTO lc_backtest VALUES (?,?,?,?,?,?)
                """, (b['signal_date'], b['return_date'], b['daily_return'],
                      b['cum_return'], b['lc_signal'], b['prediction']))

            for t in trades:
                conn.execute("""
                    INSERT INTO lc_trades (entry_date, exit_date, entry_price, exit_price,
                                          profit_pct, win, hold_days, exit_reason,
                                          entry_wt, entry_rsi, entry_rsi9, entry_pred)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (t['entry_date'], t['exit_date'], t['entry_price'], t['exit_price'],
                      t['profit_pct'], 1 if t['win'] else 0, t['hold_days'], t['exit_reason'],
                      t.get('entry_wt', 0), t.get('entry_rsi', 0),
                      t.get('entry_rsi9', 0), t.get('entry_pred', 0)))

            conn.execute("""
                INSERT OR REPLACE INTO calc_meta (key, value) VALUES ('lc_params', ?)
            """, (json.dumps(LC_PARAMS, default=str),))

            conn.commit()
            conn.close()

            trade_wins = sum(1 for t in trades if t['win'])
            trade_wr = trade_wins / len(trades) * 100 if trades else 0
            lc_calc_state['message'] = f'计算完成：{len(trades)}笔交易，交易胜率{trade_wr:.1f}%'
            logger.info(lc_calc_state['message'])

        except Exception as e:
            lc_calc_state['message'] = f'LC 计算失败: {str(e)}'
            logger.error(lc_calc_state['message'], exc_info=True)
        finally:
            lc_calc_state['running'] = False

    thread = threading.Thread(target=run_calc, daemon=True)
    thread.start()
    return {'status': 'started', 'message': 'LC 全量计算已启动'}


@app.post('/api/lc/refresh')
def api_lc_refresh():
    """LC 增量刷新：检测新数据，全量重算信号但增量写入DB"""
    if lc_calc_state['running']:
        raise HTTPException(409, "LC 计算正在进行中")

    lc_calc_state['running'] = True
    lc_calc_state['progress'] = 0
    lc_calc_state['message'] = 'LC 增量刷新中...'

    def run_refresh():
        try:
            # Step 1: 检查数据库中当前最大日期
            conn = get_factors_db()
            max_signal_date = conn.execute("SELECT MAX(trade_date) FROM lc_signals").fetchone()[0]
            conn.close()

            # Step 2: 检查指数日线最新日期
            idx_conn = sqlite3.connect('/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/index_daily.db')
            max_idx_date = idx_conn.execute(
                f"SELECT MAX(trade_date) FROM daily WHERE ts_code = '{LC_PARAMS['index_code']}'"
            ).fetchone()[0]
            idx_conn.close()

            if not max_idx_date:
                lc_calc_state['running'] = False
                lc_calc_state['message'] = '指数日线数据为空，请先采集数据'
                return

            # 数据已是最新
            if max_signal_date and max_signal_date >= max_idx_date:
                lc_calc_state['running'] = False
                lc_calc_state['message'] = f'数据已是最新（{max_signal_date}），无需刷新'
                logger.info(lc_calc_state['message'])
                return

            start_hint = max_signal_date if max_signal_date else LC_PARAMS['backtest_start']
            lc_calc_state['message'] = f'检测到新数据：{start_hint} → {max_idx_date}，开始计算...'

            # Step 3: 全量重算信号（指标是序列依赖的，无法只算增量）
            # 但只需保存新增日期的信号到 DB
            df_result, bt_results, trades = compute_lc_signals_full(
                start_date=LC_PARAMS['backtest_start'])

            # Step 4: 增量保存信号（只 INSERT OR REPLACE 新增日期的行）
            conn = get_factors_db()
            new_signal_count = 0
            for _, row in df_result.iterrows():
                td = row['trade_date']
                # 只写入 >= 数据库最大日期的记录（增量部分）
                if not max_signal_date or td >= max_signal_date:
                    conn.execute("""
                        INSERT OR REPLACE INTO lc_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (
                        row['trade_date'], row['close'], int(row['rf_signal']),
                        row.get('rf_filter', 0), row.get('rf_smooth_range', 0),
                        row.get('prediction', 0), row.get('wt', 0),
                        row.get('rsi14', 0), row.get('rsi9', 0), row.get('atr', 0),
                        row.get('yhat_rbf', 0), row.get('yhat_gauss', 0),
                        int(row['signal']),
                    ))
                    new_signal_count += 1
            conn.commit()
            conn.close()

            # Step 5: 全量覆盖回测数据（cum_return 需从第一天连续计算）
            conn = get_factors_db()
            conn.execute("DELETE FROM lc_backtest")
            for b in bt_results:
                conn.execute("""
                    INSERT OR REPLACE INTO lc_backtest VALUES (?,?,?,?,?,?)
                """, (b['signal_date'], b['return_date'], b['daily_return'],
                      b['cum_return'], b['lc_signal'], b['prediction']))
            conn.commit()
            conn.close()

            # Step 6: 全量覆盖交易明细（交易可能跨越新旧数据）
            conn = get_factors_db()
            conn.execute("DELETE FROM lc_trades")
            for t in trades:
                conn.execute("""
                    INSERT INTO lc_trades (entry_date, exit_date, entry_price, exit_price,
                                          profit_pct, win, hold_days, exit_reason,
                                          entry_wt, entry_rsi, entry_rsi9, entry_pred)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (t['entry_date'], t['exit_date'], t['entry_price'], t['exit_price'],
                      t['profit_pct'], 1 if t['win'] else 0, t['hold_days'], t['exit_reason'],
                      t.get('entry_wt', 0), t.get('entry_rsi', 0),
                      t.get('entry_rsi9', 0), t.get('entry_pred', 0)))

            conn.execute("""
                INSERT OR REPLACE INTO calc_meta (key, value) VALUES ('lc_params', ?)
            """, (json.dumps(LC_PARAMS, default=str),))

            conn.commit()
            conn.close()

            trade_wins = sum(1 for t in trades if t['win'])
            trade_wr = trade_wins / len(trades) * 100 if trades else 0
            lc_calc_state['message'] = (
                f'刷新完成：新增{new_signal_count}条信号，'
                f'{len(trades)}笔交易，胜率{trade_wr:.1f}%'
            )
            logger.info(lc_calc_state['message'])

        except Exception as e:
            lc_calc_state['message'] = f'LC 增量刷新失败: {str(e)}'
            logger.error(lc_calc_state['message'], exc_info=True)
        finally:
            lc_calc_state['running'] = False

    thread = threading.Thread(target=run_refresh, daemon=True)
    thread.start()
    return {'status': 'started', 'message': 'LC 增量刷新已启动'}


@app.get('/api/lc/chart/backtest')
def api_lc_backtest_chart(
    start_date: str = Query(None),
    end_date: str = Query(None),
):
    """LC 回测净值曲线（叠加中证2000指数 + Buy/Flat 背景色块）"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT signal_date, return_date, daily_return, cum_return, lc_signal, prediction
            FROM lc_backtest ORDER BY signal_date
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        return HTMLResponse(content="<html><body style='color:#94a3b8;text-align:center;padding:60px'>暂无回测数据</body></html>")

    data = [dict(r) for r in rows]
    if start_date:
        data = [d for d in data if d['signal_date'] >= start_date]
    if end_date:
        data = [d for d in data if d['signal_date'] <= end_date]
    if not data:
        return HTMLResponse(content="<html><body style='color:#94a3b8;text-align:center;padding:60px'>所选日期范围无数据</body></html>")

    dates_fmt = [d['signal_date'][:4]+'-'+d['signal_date'][4:6]+'-'+d['signal_date'][6:8] for d in data]
    dates_raw = [d['signal_date'] for d in data]
    cum = [d['cum_return'] for d in data]
    daily_pct = [d['daily_return'] for d in data]
    lc_signals = [d['lc_signal'] for d in data]

    # 读取中证2000指数
    import pandas as pd
    INDEX_DB_PATH = '/Users/beanpaper/WorkBuddy/20260316111015/quant/strategy-platform/local_dbs/index_daily.db'
    idx_conn = sqlite3.connect(INDEX_DB_PATH)
    try:
        idx_df = pd.read_sql_query(f"""
            SELECT trade_date, close FROM daily
            WHERE ts_code = '{INDEX_CODE}'
              AND trade_date >= '{dates_raw[0]}'
              AND trade_date <= '{dates_raw[-1]}'
            ORDER BY trade_date
        """, idx_conn)
    finally:
        idx_conn.close()

    idx_close_map = dict(zip(idx_df['trade_date'], idx_df['close']))
    idx_nv = []
    base_close = None
    for d in dates_raw:
        if d in idx_close_map:
            if base_close is None:
                base_close = idx_close_map[d]
            idx_nv.append(idx_close_map[d] / base_close)
        else:
            idx_nv.append(idx_nv[-1] if idx_nv else 1.0)

    traces = []

    # Buy/Flat 背景色块（A股惯例：持仓=红色，空仓=绿色）
    i = 0
    while i < len(data):
        sig = lc_signals[i]
        j = i
        while j < len(data) and lc_signals[j] == sig:
            j += 1
        if sig == 1:
            bg_color = 'rgba(239,68,68,0.08)'
            bg_line = 'rgba(239,68,68,0.25)'
        else:
            bg_color = 'rgba(34,197,94,0.08)'
            bg_line = 'rgba(34,197,94,0.25)'

        y_min = min(min(cum), min(idx_nv)) * 0.98
        y_max = max(max(cum), max(idx_nv)) * 1.02

        traces.append(go.Scatter(
            x=[dates_fmt[i], dates_fmt[j-1], dates_fmt[j-1], dates_fmt[i], dates_fmt[i]],
            y=[y_min, y_min, y_max, y_max, y_min],
            fill='toself', fillcolor=bg_color,
            line=dict(color=bg_line, width=0.5),
            hoverinfo='skip', showlegend=False,
        ))
        i = j

    # 中证2000指数净值
    traces.append(go.Scatter(
        x=dates_fmt, y=idx_nv, name='中证2000(净值)',
        mode='lines',
        line=dict(color='#f59e0b', width=1.5, dash='dot'),
        hovertemplate='<b>%{x}</b><br>指数净值: %{y:.4f}<extra></extra>',
    ))

    # 策略净值（空仓期保持水平）
    nv_display = []
    for i in range(len(data)):
        if lc_signals[i] == 0:
            nv_display.append(cum[i-1] if i > 0 else 1.0)
        else:
            nv_display.append(cum[i])

    traces.append(go.Scatter(
        x=dates_fmt, y=nv_display, name='LC 策略',
        mode='lines',
        line=dict(color='#3b82f6', width=2.5),
        hovertemplate='<b>%{x}</b><br>策略净值: %{y:.4f}<br>日收益: %{customdata:.2f}%<extra></extra>',
        customdata=daily_pct,
    ))

    # 基准线
    traces.append(go.Scatter(
        x=[dates_fmt[0], dates_fmt[-1]], y=[1, 1], mode='lines', name='',
        line=dict(color='#475569', width=1, dash='dash'),
        showlegend=False, hoverinfo='skip',
    ))

    layout = go.Layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#94a3b8', family='-apple-system, BlinkMacSystemFont, sans-serif'),
        margin=dict(l=60, r=40, t=40, b=60),
        legend=dict(orientation='h', y=1.08, x=0, font=dict(size=12)),
        hovermode='x unified',
        hoverlabel=dict(bgcolor='#1a2234', bordercolor='#2a3548', font=dict(color='#e2e8f0', size=12)),
        xaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=11), tickmode='auto', nticks=12, showgrid=True),
        yaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=11), title=dict(text='净值', font=dict(size=12)), zeroline=False),
    )

    fig = go.Figure(data=traces, layout=layout)
    return HTMLResponse(content=fig.to_html(full_html=True, include_plotlyjs='/static/plotly.min.js', config={
        'responsive': True, 'displayModeBar': True, 'displaylogo': False, 'scrollZoom': True,
        'modeBarButtonsToRemove': ['lasso2d','select2d','autoScale2d','toggleSpikelines'],
    }))


@app.get('/lc')
def serve_lc():
    return FileResponse(os.path.join(BASE_DIR, 'static', 'lc.html'))


# ==================== 形态学监控 API ====================

morphology_calc_state = {
    'running': False,
    'progress': 0,
    'total': 0,
    'message': '',
}


def _init_morphology_tables(conn):
    """初始化形态学监控相关数据库表"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS morphology_scan (
            trade_date TEXT,
            ts_code TEXT,
            name TEXT,
            daily_return REAL,
            close REAL,
            tg_high REAL, tg_low REAL, tg_ultra REAL, tg_comp REAL, tg_label TEXT,
            sharpe REAL,
            me_5 REAL, me_20 REAL, me_20_ma10 REAL,
            structure_ratio REAL, ratio_strength REAL, ratio_improve REAL,
            signals TEXT, kline_states TEXT,
            PRIMARY KEY (trade_date, ts_code)
        );
        CREATE INDEX IF NOT EXISTS idx_morph_date ON morphology_scan(trade_date);
        CREATE INDEX IF NOT EXISTS idx_morph_signal ON morphology_scan(signals);
    """)


def _init_crowdiness_tables(conn):
    """初始化拥挤度监测相关数据库表"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS crowdiness_daily (
            trade_date TEXT NOT NULL,
            industry_code TEXT NOT NULL,
            industry_name TEXT,
            total_score INTEGER,
            score1_deviation INTEGER DEFAULT 0,
            score2_momentum INTEGER DEFAULT 0,
            score3_turnover INTEGER DEFAULT 0,
            score4_tr_bias INTEGER DEFAULT 0,
            alert_level INTEGER DEFAULT 0,
            ind1_n40 REAL, ind1_n60 REAL, ind1_n120 REAL,
            ind2_n20 REAL, ind2_n40 REAL, ind2_n60 REAL,
            ind3_n5 REAL, ind3_n10 REAL, ind3_n20 REAL, ind3_n40 REAL, ind3_n60 REAL,
            ind4_n120 REAL, ind4_n250 REAL,
            q1_n40 REAL, q1_n60 REAL, q1_n120 REAL,
            q2_n20 REAL, q2_n40 REAL, q2_n60 REAL,
            q3_n5 REAL, q3_n10 REAL, q3_n20 REAL, q3_n40 REAL, q3_n60 REAL,
            q4_n120 REAL, q4_n250 REAL,
            PRIMARY KEY (trade_date, industry_code)
        );
        CREATE INDEX IF NOT EXISTS idx_crowd_date ON crowdiness_daily(trade_date);
        CREATE INDEX IF NOT EXISTS idx_crowd_score ON crowdiness_daily(total_score DESC);
        CREATE INDEX IF NOT EXISTS idx_crowd_alert ON crowdiness_daily(alert_level DESC);
    """)


@app.get('/api/morphology/status')
def api_morphology_status():
    """获取形态学监控状态"""
    conn = get_factors_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(DISTINCT trade_date) as scan_days,
                   MAX(trade_date) as latest_date,
                   COUNT(*) as total_records,
                   SUM(CASE WHEN signals != '-' THEN 1 ELSE 0 END) as signal_count,
                   SUM(CASE WHEN kline_states != '-' THEN 1 ELSE 0 END) as kline_count
            FROM morphology_scan
        """).fetchone()

        has_data = stats['total_records'] > 0

        # 温度分布
        temp_dist = {}
        if has_data:
            rows = conn.execute("""
                SELECT tg_label, COUNT(*) as cnt FROM morphology_scan
                WHERE trade_date = ? GROUP BY tg_label
            """, (stats['latest_date'],)).fetchall()
            temp_dist = {r['tg_label']: r['cnt'] for r in rows}

        return {
            'calc_running': morphology_calc_state['running'],
            'calc_progress': morphology_calc_state['progress'],
            'calc_total': morphology_calc_state['total'],
            'calc_message': morphology_calc_state['message'],
            'has_data': has_data,
            'scan_days': stats['scan_days'] if has_data else 0,
            'latest_date': stats['latest_date'] if has_data else None,
            'total_records': stats['total_records'] if has_data else 0,
            'signal_count': stats['signal_count'] if has_data else 0,
            'kline_count': stats['kline_count'] if has_data else 0,
            'temp_dist': temp_dist,
        }
    finally:
        conn.close()


@app.get('/api/morphology/scan')
def api_morphology_scan(
    date: str = Query(None, description="交易日期，不传则取最新"),
    signal: str = Query(None, description="策略信号筛选"),
    tg_label: str = Query(None, description="温度标签筛选"),
    kline: str = Query(None, description="K线状态筛选"),
    sort_by: str = Query('tg_comp', description="排序字段"),
    sort_order: str = Query('asc', description="排序方向 asc/desc"),
):
    """获取形态学扫描结果"""
    conn = get_factors_db()
    try:
        # 确定日期
        if not date:
            date = conn.execute("SELECT MAX(trade_date) FROM morphology_scan").fetchone()[0]
            if not date:
                raise HTTPException(404, "暂无扫描数据，请先运行扫描")

        rows = conn.execute("""
            SELECT * FROM morphology_scan WHERE trade_date = ?
        """, (date,)).fetchall()

        if not rows:
            raise HTTPException(404, f"日期 {date} 无数据")

        data = [dict(r) for r in rows]

        # 筛选
        if signal and signal != '全部':
            data = [d for d in data if signal in (d.get('signals') or '')]
        if tg_label and tg_label != '全部':
            data = [d for d in data if d.get('tg_label') == tg_label]
        if kline and kline != '全部':
            data = [d for d in data if kline in (d.get('kline_states') or '')]

        # 排序
        reverse = sort_order == 'desc'
        key_map = {
            'tg_comp': 'tg_comp', 'sharpe': 'sharpe', 'me_20_ma10': 'me_20_ma10',
            'structure_ratio': 'structure_ratio', 'daily_return': 'daily_return',
        }
        sort_key = key_map.get(sort_by, 'tg_comp')
        data.sort(key=lambda x: (x.get(sort_key) is None, x.get(sort_key) or 0), reverse=reverse)

        return {'date': date, 'total': len(data), 'industries': data}
    finally:
        conn.close()


@app.get('/api/morphology/dates')
def api_morphology_dates():
    """获取所有扫描日期"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT DISTINCT trade_date FROM morphology_scan ORDER BY trade_date DESC
        """).fetchall()
        return [r['trade_date'] for r in rows]
    finally:
        conn.close()


@app.get('/api/morphology/industry/{ts_code}')
def api_morphology_industry_history(ts_code: str):
    """获取某行业的历史形态学数据"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT * FROM morphology_scan
            WHERE ts_code = ? ORDER BY trade_date
        """, (ts_code,)).fetchall()

        if not rows:
            raise HTTPException(404, f"行业 {ts_code} 无数据")

        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/morphology/chart/{ts_code}')
def api_morphology_chart(ts_code: str):
    """某行业形态学因子时序图（叠加行业指数走势）"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT trade_date, tg_comp, tg_low, me_20_ma10, sharpe,
                   structure_ratio, daily_return, close, signals
            FROM morphology_scan
            WHERE ts_code = ? ORDER BY trade_date
        """, (ts_code,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return HTMLResponse(content="<html><body style='color:#94a3b8;text-align:center;padding:60px'>无数据</body></html>")

    data = [dict(r) for r in rows]
    dates = [d['trade_date'][:4]+'-'+d['trade_date'][4:6]+'-'+d['trade_date'][6:8] for d in data]

    traces = []

    # 温度计（左轴）
    traces.append(go.Scatter(
        x=dates, y=[d['tg_comp'] for d in data], name='温度计',
        line=dict(color='#3b82f6', width=2),
        hovertemplate='%{x}<br>温度: %{y:.1f}<extra></extra>',
    ))
    traces.append(go.Scatter(
        x=dates, y=[d['tg_low'] for d in data], name='TG_Low',
        line=dict(color='#60a5fa', width=1, dash='dot'), opacity=0.6,
        hovertemplate='%{x}<br>TG_Low: %{y:.1f}<extra></extra>',
    ))

    # 赚钱效应 MA10（右轴）
    traces.append(go.Scatter(
        x=dates, y=[d['me_20_ma10'] for d in data], name='ME20_MA10',
        yaxis='y2',
        line=dict(color='#22c55e', width=1.5),
        hovertemplate='%{x}<br>ME20_MA10: %{y:.1f}<extra></extra>',
    ))

    # 行业指数走势（右轴2）
    closes = [d['close'] for d in data]
    if closes and closes[0] and closes[0] > 0:
        nv = [c / closes[0] * 100 for c in closes]
    else:
        nv = [100] * len(closes)
    traces.append(go.Scatter(
        x=dates, y=nv, name='指数(归一化)',
        yaxis='y3',
        line=dict(color='#f59e0b', width=1, dash='dash'), opacity=0.5,
        hovertemplate='%{x}<br>指数: %{y:.1f}<extra></extra>',
    ))

    # 过热线 80 和 冷线 20
    traces.append(go.Scatter(
        x=[dates[0], dates[-1]], y=[80, 80], mode='lines', name='过热(80)',
        line=dict(color='#ef4444', width=1, dash='dash'), showlegend=True,
    ))
    traces.append(go.Scatter(
        x=[dates[0], dates[-1]], y=[20, 20], mode='lines', name='极冷(20)',
        line=dict(color='#22c55e', width=1, dash='dash'), showlegend=True,
    ))

    layout = go.Layout(
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)',
        font=dict(color='#94a3b8', family='-apple-system, BlinkMacSystemFont, sans-serif'),
        margin=dict(l=50, r=50, t=35, b=50),
        legend=dict(orientation='h', y=1.1, x=0, font=dict(size=10)),
        hovermode='x unified',
        xaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=10),
                   rangeslider=dict(visible=True, thickness=0.04, bgcolor='#111827', bordercolor='#2a3548')),
        yaxis=dict(gridcolor='#1a2234', linecolor='#2a3548', tickfont=dict(size=10),
                   title=dict(text='温度', font=dict(size=10)), zeroline=False, domain=[0, 1]),
        yaxis2=dict(overlaying='y', side='right', linecolor='#2a3548', tickfont=dict(size=10),
                    title=dict(text='ME', font=dict(size=10)), gridcolor='rgba(0,0,0,0)', zeroline=False),
        yaxis3=dict(overlaying='y', side='right', anchor='free', position=0.98,
                    linecolor='#2a3548', tickfont=dict(size=10, color='#f59e0b'),
                    title=dict(text='指数', font=dict(size=10, color='#f59e0b')),
                    gridcolor='rgba(0,0,0,0)', zeroline=False),
    )

    fig = go.Figure(data=traces, layout=layout)
    return HTMLResponse(content=fig.to_html(full_html=True, include_plotlyjs='/static/plotly.min.js', config={
        'responsive': True, 'displayModeBar': True, 'displaylogo': False, 'scrollZoom': True,
    }))


@app.post('/api/morphology/calculate')
def api_morphology_calculate():
    """启动形态学扫描"""
    if morphology_calc_state['running']:
        raise HTTPException(409, "形态学扫描正在进行中")

    morphology_calc_state['running'] = True
    morphology_calc_state['progress'] = 0
    morphology_calc_state['message'] = '扫描中...'

    def run_calc():
        try:
            def progress_cb(current, total, msg):
                morphology_calc_state['progress'] = current
                morphology_calc_state['total'] = total
                morphology_calc_state['message'] = msg

            result = run_morphology_scan(progress_cb=progress_cb)
            if not result:
                morphology_calc_state['message'] = '扫描失败：无行业数据'
                return

            # 保存到数据库
            conn = get_factors_db()
            for r in result['industries']:
                conn.execute('''INSERT OR REPLACE INTO morphology_scan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
                    r['date'], r['ts_code'], r['name'], r['daily_return'], r['close'],
                    r['tg_high'], r['tg_low'], r['tg_ultra'], r['tg_comp'], r['tg_label'],
                    r['sharpe'], r['me_5'], r['me_20'], r['me_20_ma10'],
                    r['structure_ratio'], r['ratio_strength'], r['ratio_improve'],
                    r['signal_str'], r['kline_str'],
                ))

            conn.execute("""
                INSERT OR REPLACE INTO calc_meta (key, value) VALUES ('morphology_last_scan', ?)
            """, (datetime.now().isoformat(),))
            conn.commit()
            conn.close()

            morphology_calc_state['message'] = (
                f'扫描完成：{result["total"]}个行业，'
                f'{result["signal_count"]}个策略信号，'
                f'{result["kline_count"]}个K线状态，'
                f'耗时{result["elapsed"]}s'
            )
            logger.info(morphology_calc_state['message'])

        except Exception as e:
            morphology_calc_state['message'] = f'扫描失败: {str(e)}'
            logger.error(morphology_calc_state['message'], exc_info=True)
        finally:
            morphology_calc_state['running'] = False

    thread = threading.Thread(target=run_calc, daemon=True)
    thread.start()
    return {'status': 'started', 'message': '形态学扫描已启动'}


# ==================== 拥挤度监测 API ====================

@app.get('/api/crowdiness/status')
def api_crowdiness_status():
    """获取拥挤度监测状态"""
    conn = get_factors_db()
    try:
        stats = conn.execute("""
            SELECT COUNT(DISTINCT trade_date) as trade_days,
                   MIN(trade_date) as min_date,
                   MAX(trade_date) as max_date,
                   COUNT(*) as total_records
            FROM crowdiness_daily
        """).fetchone()

        has_data = stats['total_records'] > 0

        # 最新日期的拥挤度分布
        alert_dist = {}
        latest_scores = {}
        if has_data:
            latest_date = stats['max_date']
            rows = conn.execute("""
                SELECT alert_level, COUNT(*) as cnt
                FROM crowdiness_daily
                WHERE trade_date = ?
                GROUP BY alert_level
            """, (latest_date,)).fetchall()
            alert_dist = {str(r['alert_level']): r['cnt'] for r in rows}

            # 得分分布
            score_rows = conn.execute("""
                SELECT total_score, COUNT(*) as cnt
                FROM crowdiness_daily
                WHERE trade_date = ?
                GROUP BY total_score
            """, (latest_date,)).fetchall()
            latest_scores = {str(r['total_score']): r['cnt'] for r in score_rows}

        return {
            'has_data': has_data,
            'trade_days': stats['trade_days'],
            'min_date': stats['min_date'],
            'max_date': stats['max_date'],
            'total_records': stats['total_records'],
            'alert_dist': alert_dist,
            'score_dist': latest_scores,
            'calc_running': crowd_calc_state['running'],
            'calc_progress': crowd_calc_state['progress'],
            'calc_total': crowd_calc_state['total'],
            'calc_message': crowd_calc_state['message'],
        }
    finally:
        conn.close()


@app.get('/api/crowdiness/ranking')
def api_crowdiness_ranking(
    trade_date: str = Query(default=None),
    sort_by: str = Query(default='total_score'),
    alert_level: int = Query(default=None),
):
    """获取某日行业拥挤度排名"""
    conn = get_factors_db()
    try:
        if trade_date is None:
            row = conn.execute("SELECT MAX(trade_date) FROM crowdiness_daily").fetchone()
            trade_date = row[0] if row else None

        if trade_date is None:
            return []

        query = """
            SELECT * FROM crowdiness_daily
            WHERE trade_date = ?
        """
        params = [trade_date]

        if alert_level is not None:
            query += " AND alert_level >= ?"
            params.append(alert_level)

        # 排序
        sort_map = {
            'total_score': 'total_score DESC',
            'score1': 'score1_deviation DESC',
            'score2': 'score2_momentum DESC',
            'score3': 'score3_turnover DESC',
            'score4': 'score4_tr_bias DESC',
            'alert_level': 'alert_level DESC',
        }
        order = sort_map.get(sort_by, 'total_score DESC')
        query += f" ORDER BY {order}"

        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/crowdiness/dates')
def api_crowdiness_dates():
    """获取所有有数据的日期"""
    conn = get_factors_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM crowdiness_daily ORDER BY trade_date DESC"
        ).fetchall()
        return [r['trade_date'] for r in rows]
    finally:
        conn.close()


@app.get('/api/crowdiness/industry/{industry_code}')
def api_crowdiness_industry_history(industry_code: str):
    """获取某行业的历史拥挤度数据"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT trade_date, total_score, score1_deviation, score2_momentum,
                   score3_turnover, score4_tr_bias, alert_level
            FROM crowdiness_daily
            WHERE industry_code = ?
            ORDER BY trade_date
        """, (industry_code,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get('/api/crowdiness/chart/{industry_code}')
def api_crowdiness_chart(industry_code: str):
    """生成某行业拥挤度时序图(Plotly HTML) - 含分位数曲线"""
    conn = get_factors_db()
    try:
        rows = conn.execute("""
            SELECT * FROM crowdiness_daily
            WHERE industry_code = ?
            ORDER BY trade_date
        """, (industry_code,)).fetchall()

        if not rows:
            return HTMLResponse("<p>无数据</p>")

        import plotly.graph_objects as go
        from plotly.subplots import make_subplots

        dates = [r['trade_date'] for r in rows]
        scores = [r['total_score'] for r in rows]
        alerts = [r['alert_level'] for r in rows]
        s1 = [r['score1_deviation'] for r in rows]
        s2 = [r['score2_momentum'] for r in rows]
        s3 = [r['score3_turnover'] for r in rows]
        s4 = [r['score4_tr_bias'] for r in rows]

        # 分位数值
        q1_40 = [r['q1_n40'] for r in rows]
        q1_60 = [r['q1_n60'] for r in rows]
        q1_120 = [r['q1_n120'] for r in rows]
        q2_20 = [r['q2_n20'] for r in rows]
        q2_40 = [r['q2_n40'] for r in rows]
        q2_60 = [r['q2_n60'] for r in rows]
        q3_5 = [r['q3_n5'] for r in rows]
        q3_10 = [r['q3_n10'] for r in rows]
        q3_20 = [r['q3_n20'] for r in rows]
        q3_40 = [r['q3_n40'] for r in rows]
        q3_60 = [r['q3_n60'] for r in rows]
        q4_120 = [r['q4_n120'] for r in rows]
        q4_250 = [r['q4_n250'] for r in rows]

        # 95%阈值线数据
        threshold_line = [0.95] * len(dates)

        name = rows[0]['industry_name'] if rows[0]['industry_name'] else industry_code

        fig = make_subplots(
            rows=3, cols=1, shared_xaxes=True,
            row_heights=[0.4, 0.3, 0.3],
            vertical_spacing=0.05,
            subplot_titles=(f'{name} 拥挤度得分', '子指标得分', '滚动分位数 (1250日)')
        )

        # ---- Row 1: 总分 ----
        fig.add_trace(go.Scatter(
            x=dates, y=scores, mode='lines', name='总分',
            line=dict(color='#ef4444', width=2),
            fill='tozeroy', fillcolor='rgba(239,68,68,0.1)'
        ), row=1, col=1)

        # 高危标注 - 按阶段分色
        stage_labels = {0: '安全', 1: '起势', 2: '上升', 3: '加速', 4: '高潮'}
        stage_colors = {0: '#22c55e', 1: '#60a5fa', 2: '#f59e0b', 3: '#f97316', 4: '#ef4444'}
        stage_sizes = {0: 4, 1: 5, 2: 6, 3: 7, 4: 8}
        for lvl in [4, 3, 2, 1]:
            lvl_dates = [d for d, a in zip(dates, alerts) if a == lvl]
            lvl_scores = [s for s, a in zip(scores, alerts) if a == lvl]
            if lvl_dates:
                fig.add_trace(go.Scatter(
                    x=lvl_dates, y=lvl_scores, mode='markers',
                    name=f'{stage_labels[lvl]}',
                    marker=dict(color=stage_colors[lvl], size=stage_sizes[lvl], symbol='circle')
                ), row=1, col=1)

        # ---- Row 2: 子指标得分 ----
        fig.add_trace(go.Scatter(x=dates, y=s1, mode='lines', name='S1乖离率',
                                 line=dict(color='#3b82f6', width=1)), row=2, col=1)
        fig.add_trace(go.Scatter(x=dates, y=s2, mode='lines', name='S2动量',
                                 line=dict(color='#22c55e', width=1)), row=2, col=1)
        fig.add_trace(go.Scatter(x=dates, y=s3, mode='lines', name='S3换手率',
                                 line=dict(color='#f59e0b', width=1)), row=2, col=1)
        fig.add_trace(go.Scatter(x=dates, y=s4, mode='lines', name='S4换手乖离',
                                 line=dict(color='#a855f7', width=1)), row=2, col=1)

        # ---- Row 3: 分位数曲线 ----
        # Ind1 分位数 (蓝)
        fig.add_trace(go.Scatter(x=dates, y=q1_40, mode='lines', name='Q1-n40',
                                 line=dict(color='#3b82f6', width=1, dash='solid'),
                                 opacity=0.8), row=3, col=1)
        fig.add_trace(go.Scatter(x=dates, y=q1_60, mode='lines', name='Q1-n60',
                                 line=dict(color='#3b82f6', width=1, dash='dash'),
                                 opacity=0.6), row=3, col=1)
        fig.add_trace(go.Scatter(x=dates, y=q1_120, mode='lines', name='Q1-n120',
                                 line=dict(color='#3b82f6', width=1, dash='dot'),
                                 opacity=0.5), row=3, col=1)

        # Ind2 分位数 (绿)
        fig.add_trace(go.Scatter(x=dates, y=q2_20, mode='lines', name='Q2-n20',
                                 line=dict(color='#22c55e', width=1, dash='solid'),
                                 opacity=0.8), row=3, col=1)
        fig.add_trace(go.Scatter(x=dates, y=q2_40, mode='lines', name='Q2-n40',
                                 line=dict(color='#22c55e', width=1, dash='dash'),
                                 opacity=0.6), row=3, col=1)
        fig.add_trace(go.Scatter(x=dates, y=q2_60, mode='lines', name='Q2-n60',
                                 line=dict(color='#22c55e', width=1, dash='dot'),
                                 opacity=0.5), row=3, col=1)

        # Ind3 分位数 (黄)
        fig.add_trace(go.Scatter(x=dates, y=q3_5, mode='lines', name='Q3-n5',
                                 line=dict(color='#f59e0b', width=1, dash='solid'),
                                 opacity=0.8), row=3, col=1)
        fig.add_trace(go.Scatter(x=dates, y=q3_20, mode='lines', name='Q3-n20',
                                 line=dict(color='#f59e0b', width=1, dash='dash'),
                                 opacity=0.6), row=3, col=1)
        fig.add_trace(go.Scatter(x=dates, y=q3_60, mode='lines', name='Q3-n60',
                                 line=dict(color='#f59e0b', width=1, dash='dot'),
                                 opacity=0.5), row=3, col=1)

        # Ind4 分位数 (紫)
        fig.add_trace(go.Scatter(x=dates, y=q4_120, mode='lines', name='Q4-n120',
                                 line=dict(color='#a855f7', width=1, dash='solid'),
                                 opacity=0.8), row=3, col=1)
        fig.add_trace(go.Scatter(x=dates, y=q4_250, mode='lines', name='Q4-n250',
                                 line=dict(color='#a855f7', width=1, dash='dash'),
                                 opacity=0.6), row=3, col=1)

        # 95% 阈值线
        fig.add_trace(go.Scatter(
            x=dates, y=threshold_line, mode='lines', name='95%阈值',
            line=dict(color='#ef4444', width=2, dash='dashdot'),
            opacity=0.7
        ), row=3, col=1)

        fig.update_layout(
            template='plotly_dark',
            height=700, margin=dict(l=50, r=20, t=40, b=30),
            paper_bgcolor='#111827', plot_bgcolor='#111827',
            font=dict(color='#94a3b8', size=11),
            showlegend=True, legend=dict(orientation='h', y=1.02, font=dict(size=9)),
        )
        fig.update_yaxes(dtick=1, range=[0, 4.5], row=1, col=1)
        fig.update_yaxes(dtick=1, range=[-0.2, 1.5], row=2, col=1)
        fig.update_yaxes(range=[0, 1.05], tickformat='.0%', row=3, col=1)

        html = fig.to_html(include_plotlyjs=False, full_html=False)
        return HTMLResponse(f'<script src="/static/plotly.min.js"></script>{html}')
    finally:
        conn.close()


@app.post('/api/crowdiness/calculate')
def api_crowdiness_calculate():
    """触发全量拥挤度计算"""
    if crowd_calc_state['running']:
        return {'status': 'already_running'}

    def progress_cb(step, total, msg):
        crowd_calc_state['progress'] = step
        crowd_calc_state['total'] = total
        crowd_calc_state['message'] = msg

    def run():
        crowd_calc_state['running'] = True
        crowd_calc_state['start_time'] = datetime.now().isoformat()
        try:
            records = run_crowdiness_calculation(progress_cb=progress_cb)
            # 保存到数据库
            conn = get_factors_db()
            # 全量覆盖
            conn.execute("DELETE FROM crowdiness_daily")
            for r in records:
                cols = ', '.join(r.keys())
                vals = ', '.join(['?'] * len(r))
                conn.execute(
                    f"INSERT OR REPLACE INTO crowdiness_daily ({cols}) VALUES ({vals})",
                    list(r.values())
                )
            conn.commit()
            conn.close()
            logger.info(f"拥挤度计算完成, 保存 {len(records)} 条")
        except Exception as e:
            logger.error(f"拥挤度计算失败: {e}", exc_info=True)
            crowd_calc_state['message'] = f'错误: {str(e)}'
        finally:
            crowd_calc_state['running'] = False

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return {'status': 'started', 'message': '拥挤度计算已启动'}


@app.post('/api/crowdiness/refresh')
def api_crowdiness_refresh():
    """增量刷新拥挤度数据"""
    if crowd_calc_state['running']:
        return {'status': 'already_running'}

    def progress_cb(step, total, msg):
        crowd_calc_state['progress'] = step
        crowd_calc_state['total'] = total
        crowd_calc_state['message'] = msg

    def run():
        crowd_calc_state['running'] = True
        crowd_calc_state['start_time'] = datetime.now().isoformat()
        try:
            records = run_crowdiness_incremental(progress_cb=progress_cb)
            conn = get_factors_db()
            for r in records:
                cols = ', '.join(r.keys())
                vals = ', '.join(['?'] * len(r))
                conn.execute(
                    f"INSERT OR REPLACE INTO crowdiness_daily ({cols}) VALUES ({vals})",
                    list(r.values())
                )
            conn.commit()
            conn.close()
            logger.info(f"拥挤度增量刷新完成, 新增 {len(records)} 条")
        except Exception as e:
            logger.error(f"拥挤度增量刷新失败: {e}", exc_info=True)
            crowd_calc_state['message'] = f'错误: {str(e)}'
        finally:
            crowd_calc_state['running'] = False

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return {'status': 'started', 'message': '拥挤度增量刷新已启动'}


@app.get('/crowdiness')
def serve_crowdiness():
    return FileResponse(os.path.join(BASE_DIR, 'static', 'crowdiness.html'))


@app.get('/morphology')
def serve_morphology():
    return FileResponse(os.path.join(BASE_DIR, 'static', 'morphology.html'))


# ==================== 静态文件 ====================

@app.get('/')
def serve_index():
    return FileResponse(os.path.join(BASE_DIR, 'static', 'index.html'))

@app.get('/factor/ews')
def serve_ews():
    return FileResponse(os.path.join(BASE_DIR, 'static', 'ews.html'))


@app.on_event("startup")
def startup():
    init_factors_db()
    conn = get_factors_db()
    _init_rf_tables(conn)
    _init_boci_tables(conn)
    _init_index_sentiment_tables(conn)
    _init_lc_tables(conn)
    _init_morphology_tables(conn)
    _init_crowdiness_tables(conn)
    conn.close()
    logger.info("策略因子平台启动完成")

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=5801)
