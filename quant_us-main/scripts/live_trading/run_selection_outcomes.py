#!/usr/bin/env python3
"""回填研究批次的固定窗口结果（1/3/5/10 会话收益、rank IC、Top-N 超额）。

每个批次 as_of 之后的数据需要未来行情，所以每天收盘后跑一次，回填已过窗口的结果。
用法：
    python scripts/live_trading/run_selection_outcomes.py          # 回填所有批次
    python scripts/live_trading/run_selection_outcomes.py --latest # 只回填最新一批
    python scripts/live_trading/run_selection_outcomes.py --output outcomes.json
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from scripts.live_trading.llm_suggestions.store import load_research_batches
from scripts.live_trading.decision_ledger.selection_outcomes import evaluate_selection


def _as_of_dt(as_of):
    if isinstance(as_of, str):
        return datetime.fromisoformat(as_of.replace('Z', '+00:00'))
    return as_of


def merge_bars(bars_map):
    """把 {code: DataFrame} 合并成带 code 列的 DataFrame（date 归一化为 UTC）。"""
    frames = []
    for code, df in (bars_map or {}).items():
        if df is None or len(df) == 0:
            continue
        d = df.copy()
        if 'date' not in d.columns:
            continue
        d['date'] = pd.to_datetime(d['date'])
        if d['date'].dt.tz is None:
            d['date'] = d['date'].dt.tz_localize('UTC')
        else:
            d['date'] = d['date'].dt.tz_convert('UTC')
        d['code'] = code
        frames.append(d)
    if not frames:
        return pd.DataFrame(columns=['code', 'date', 'open', 'high', 'low', 'close'])
    return pd.concat(frames, ignore_index=True)


def run_outcomes(config, fetcher, latest_only=False):
    """回填所有（或最新）研究批次的固定窗口结果。返回 report 列表。"""
    batches = load_research_batches()
    if latest_only:
        batches = batches[-1:]

    results = []
    for batch in batches:
        universe = batch.get('universe') or []
        as_of = batch.get('as_of')
        if not universe or not as_of:
            continue
        as_of_dt = _as_of_dt(as_of)
        start = (as_of_dt - timedelta(days=40)).strftime('%Y-%m-%d')
        end = (as_of_dt + timedelta(days=20)).strftime('%Y-%m-%d')
        bars_map = fetcher.fetch_multiple_stocks(universe, start, end) if fetcher else {}
        bars = merge_bars(bars_map)
        results.append(evaluate_selection(batch, bars))
    return results


def main():
    parser = argparse.ArgumentParser(description='回填研究批次固定窗口结果')
    parser.add_argument('--latest', action='store_true', help='只回填最新一批')
    parser.add_argument('--output', default=None, help='结果 JSON 输出路径')
    parser.add_argument('--config', default=str(BASE_DIR / 'config.yaml'))
    args = parser.parse_args()

    import yaml
    with open(args.config, encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}

    from mutifactor.data.us_fetcher import FutuUSDataFetcher
    futu_cfg = config.get('futu', {})
    fetcher = FutuUSDataFetcher(host=futu_cfg.get('host', '127.0.0.1'),
                                port=int(futu_cfg.get('port', 11111)))
    try:
        if not fetcher.connect():
            print('无法连接富途 OpenD')
            return 1
        results = run_outcomes(config, fetcher, latest_only=args.latest)
    finally:
        fetcher.disconnect()

    payload = json.dumps(results, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(payload + '\n', encoding='utf-8')
    print(payload)
    return 0


if __name__ == '__main__':
    sys.exit(main())
