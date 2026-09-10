#!/usr/bin/env python3
"""Outcome 定时结算 runner（技术设计 §16 + PR7）。

把 selection / entry / position 的固定窗口结果写入 decision_outcomes_v2。
纯结算逻辑不依赖券商；行情源可注入（futu / 测试 DataFrame）。

用法：
    python -m scripts.live_trading.run_outcomes            # 回填所有研究批次
    python -m scripts.live_trading.run_outcomes --latest   # 只回填最新一批
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from scripts.live_trading.decision_ledger.outcome_jobs import OutcomeSettlement
from scripts.live_trading.decision_ledger.selection_outcomes import CLOSE_HOUR_UTC

HORIZONS = (1, 3, 5, 10, 20)


def code_close_series(bars: pd.DataFrame, code: str, as_of, max_bars: int = 21,
                      require_future: bool = True):
    """从 bars（含 code/date/close，date=UTC）提取该股票自决策基准收盘价起的一段 close 序列。

    返回 [base_close, future_close_1, ...]；序列不足 2 根（无未来数据）返回 []。
    """
    if bars is None or len(bars) == 0 or 'date' not in bars.columns:
        return []
    df = bars[bars['code'] == code].sort_values('date')
    if df.empty:
        return []
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize('UTC')
    else:
        as_of = as_of.tz_convert('UTC')
    closed = df[df['date'] + pd.Timedelta(hours=CLOSE_HOUR_UTC) <= as_of]
    if closed.empty:
        return []
    base_date = closed.iloc[-1]['date']
    base = float(closed.iloc[-1]['close'])
    future = df[df['date'] > base_date]
    closes = [base] + [float(c) for c in future['close'].head(max_bars - 1).tolist()]
    return closes if len(closes) >= (2 if require_future else 1) else []


def settle_selection_batch(batch: dict, bars: pd.DataFrame, settlement: OutcomeSettlement,
                           benchmark_bars: pd.DataFrame = None,
                           benchmark_code: str = 'US.SPY') -> int:
    """结算一个研究批次：universe 每只股票写 1/3/5/10/20d outcome。返回写入条数。"""
    universe = list(batch.get('universe') or [])
    as_of = batch.get('as_of')
    # v2 决策必须以 DecisionRun ID 为外键；旧批次才退回 research_batch_id。
    decision_id = batch.get('decision_id') or batch.get('research_batch_id')
    if not universe or not as_of or not decision_id:
        return 0
    n = 0
    for code in universe:
        closes = code_close_series(bars, code, as_of, require_future=False)
        if not closes:
            continue
        bench = None
        if benchmark_bars is not None:
            bench = code_close_series(benchmark_bars, benchmark_code, as_of,
                                      require_future=False)
        written = settlement.settle_selection(decision_id, code, closes,
                                               benchmark_closes=bench, data_quality='good')
        n += written
        completed = {f'{h}d' for h in HORIZONS if len(closes) > h}
        for horizon in (f'{h}d' for h in HORIZONS):
            if horizon in completed:
                continue
            settlement.write_outcome(decision_id, {
                'horizon': horizon, 'data_quality': 'pending_future_bars',
                'body': {'code': code, 'decision_id': decision_id,
                         'available_future_bars': max(0, len(closes) - 1),
                         'status': 'pending'},
            }, subject_key=code)
            n += 1
    return n


def settle_entry_signal(decision_id: str, entry_price: float, templates: list,
                        closes: list, settlement: OutcomeSettlement) -> int:
    """结算一个 entry 信号的多模板反事实路径（§16.2）。"""
    if not closes:
        return 0
    return settlement.settle_entry(decision_id, entry_price, templates, closes)


def settle_position_decision(decision_id: str, trade: dict, actual_exit: float,
                             mechanical_exit: float, first_llm_exit: float,
                             closes: list, settlement: OutcomeSettlement) -> int:
    """结算一条持仓决策相对机械退出的对照（§16.3）。"""
    return settlement.settle_position(decision_id, trade, actual_exit, mechanical_exit,
                                      first_llm_exit, closes)


def run(registry, batches, bars, benchmark_bars=None) -> int:
    """对多个研究批次批量结算。返回写入 outcome 条数。"""
    settlement = OutcomeSettlement(registry)
    total = 0
    for batch in batches:
        total += settle_selection_batch(batch, bars, settlement,
                                        benchmark_bars=benchmark_bars)
    return total


def main():
    parser = argparse.ArgumentParser(description='结算研究批次固定窗口结果到 decision_outcomes_v2')
    parser.add_argument('--latest', action='store_true', help='只结算最新一批')
    parser.add_argument('--config', default=str(BASE_DIR / 'config.yaml'))
    args = parser.parse_args()

    import yaml
    with open(args.config, encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}

    from scripts.live_trading.llm_suggestions.store import load_research_batches
    batches = load_research_batches()
    if args.latest:
        batches = batches[-1:]

    from mutifactor.data.us_fetcher import FutuUSDataFetcher
    from scripts.live_trading.run_selection_outcomes import merge_bars
    futu_cfg = config.get('futu', {})
    fetcher = FutuUSDataFetcher(host=futu_cfg.get('host', '127.0.0.1'),
                                port=int(futu_cfg.get('port', 11111)))
    from scripts.live_trading.position_registry import REGISTRY
    try:
        if not fetcher.connect():
            print('无法连接富途 OpenD')
            return 1
        bars_map = {}
        for batch in batches:
            universe = batch.get('universe') or []
            as_of = batch.get('as_of')
            if not universe or not as_of:
                continue
            from datetime import timedelta
            start = (pd.Timestamp(as_of) - timedelta(days=40)).strftime('%Y-%m-%d')
            end = (pd.Timestamp(as_of) + timedelta(days=30)).strftime('%Y-%m-%d')
            bars_map.update(fetcher.fetch_multiple_stocks(list(dict.fromkeys(universe + ['US.SPY'])), start, end))
        bars = merge_bars(bars_map)
        benchmark_bars = bars[bars['code'] == 'US.SPY'] if not bars.empty else bars
        runtime_cfg = (config.get('llm_decision', {}).get('engine_v2') or {})
        from scripts.live_trading.position_registry import PositionRegistry
        registry = PositionRegistry(namespace=runtime_cfg.get('account_scope', 'DRY-RUN'))
        total = run(registry, batches, bars, benchmark_bars=benchmark_bars)
    finally:
        fetcher.disconnect()
    print(f'settled {total} outcomes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
