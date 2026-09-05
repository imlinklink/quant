"""抄底扫描流水回填（P2 评估闭环 · 第 2 块）。

对 dip_scans.jsonl 里每条扫描，按“扫描时间之后的 5mK”计算前向收益：
  r12 / r24 / r48：扫描价 → 之后第 12 / 24 / 48 根完整 5mK 收盘的涨跌幅
                   （约 1h / 2h / 4h 交易时间；跳过扫描时所在的那根未完成K）
  max_fav_24：之后 24 根内的最大浮盈（评估“若反弹能吃到多少”）

幂等：已有 outcome 的 scan_id 自动跳过。用法：
    python scripts/live_trading/decision_ledger/backfill_scan_outcomes.py
    python scripts/live_trading/decision_ledger/backfill_scan_outcomes.py --days 3 --codes US.MU
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, BASE_DIR)

from scripts.live_trading.decision_ledger import scan_ledger  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger('backfill_scans')

ET = ZoneInfo('America/New_York')


def parse_scan_time(text) -> Optional[datetime]:
    """把扫描记录里的 et_time(带时区ISO) 转成 naive 美东时间，便于与Futu K线比较。"""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text))
        if dt.tzinfo is not None:
            dt = dt.astimezone(ET).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


def compute_outcome(scan_time: datetime, price: float, bars_df: pd.DataFrame) -> Dict:
    """
    纯函数：给定扫描时刻与 5mK 序列，算前向收益。
    bars_df 需含列 date(datetime, naive ET) / open / high / low / close / volume。
    """
    result = {
        'r12': None, 'r24': None, 'r48': None,
        'max_fav_24': None, 'bars_after': 0, 'complete': False,
    }
    if scan_time is None or not price or price <= 0:
        return result
    future = bars_df[bars_df['date'] > scan_time].reset_index(drop=True)
    result['bars_after'] = int(len(future))
    if future.empty:
        return result
    closes = future['close'].astype(float).values
    if len(closes) >= 12:
        result['r12'] = float(round((closes[11] / price - 1.0) * 100, 3))
    if len(closes) >= 24:
        result['r24'] = float(round((closes[23] / price - 1.0) * 100, 3))
        highs = future['high'].astype(float).values[:24]
        result['max_fav_24'] = float(round((float(highs.max()) / price - 1.0) * 100, 3))
    if len(closes) >= 48:
        result['r48'] = float(round((closes[47] / price - 1.0) * 100, 3))
        result['complete'] = True
    return result


def fetch_5m(code: str, start: str, end: str, ctx) -> Optional[pd.DataFrame]:
    from futu import KLType, RET_OK, Session
    try:
        ret, data, _ = ctx.request_history_kline(
            code=code, start=start, end=end, ktype=KLType.K_5M,
            extended_time=True, session=Session.ALL,
        )
        if ret != RET_OK or data is None or len(data) == 0:
            logger.warning(f'{code} 无5m数据 {start}~{end}')
            return None
        df = pd.DataFrame({
            'date': pd.to_datetime(data['time_key']),
            'open': data['open'].astype(float),
            'high': data['high'].astype(float),
            'low': data['low'].astype(float),
            'close': data['close'].astype(float),
            'volume': data['volume'].astype(float),
        }).sort_values('date').drop_duplicates(subset=['date']).reset_index(drop=True)
        return df
    except Exception as e:
        logger.warning(f'{code} 拉取失败: {e}')
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=7, help='只看最近 N 天的扫描（默认7）')
    ap.add_argument('--codes', default=None, help='逗号分隔，默认处理全部')
    args = ap.parse_args()

    cfg_path = os.path.join(BASE_DIR, 'config.yaml')
    with open(cfg_path, encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    futu_cfg = cfg.get('futu', {})
    codes_filter = {c.strip() for c in args.codes.split(',') if c.strip()} if args.codes else None

    scans = scan_ledger.load_scans()
    # 只有回填到 48 根（4h）的才算完成；部分结果下次继续补，避免丢失更长前向收益
    existing = {o['scan_id'] for o in scan_ledger.load_outcomes()
                if o.get('scan_id') and o.get('complete')}
    cutoff = datetime.now() - timedelta(days=args.days)
    todo = []
    for s in scans:
        if s.get('scan_id') in existing:
            continue
        ts = parse_scan_time(s.get('et_time'))
        if ts is None or ts < cutoff:
            continue
        if codes_filter and s.get('stock_code') not in codes_filter:
            continue
        todo.append(s)
    logger.info(f'待回填扫描: {len(todo)} 条（已存在 {len(existing)} 条）')
    if not todo:
        return

    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=str(futu_cfg.get('host', '127.0.0.1')),
                           port=int(futu_cfg.get('port', 11111)))
    try:
        # 按股票聚合，一次拉全区间
        by_code: Dict[str, List[Dict]] = {}
        for s in todo:
            by_code.setdefault(s.get('stock_code', ''), []).append(s)
        n_done = 0
        for code, items in by_code.items():
            if not code:
                continue
            ts_list = [parse_scan_time(s.get('et_time')) for s in items]
            ts_list = [t for t in ts_list if t is not None]
            if not ts_list:
                continue
            start = (min(ts_list) - timedelta(days=1)).strftime('%Y-%m-%d')
            end = datetime.now().strftime('%Y-%m-%d')
            df = fetch_5m(code, start, end, ctx)
            if df is None:
                continue
            for s, ts in zip(items, ts_list):
                price = float(s.get('price') or 0)
                outcome = compute_outcome(ts, price, df)
                scan_ledger.record_outcome(
                    s['scan_id'], stock_code=code, **outcome,
                )
                n_done += 1
        logger.info(f'完成回填: {n_done} 条')
    finally:
        ctx.close()


if __name__ == '__main__':
    main()
