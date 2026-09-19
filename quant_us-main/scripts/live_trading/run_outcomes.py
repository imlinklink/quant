#!/usr/bin/env python3
"""Outcome 定时结算 runner（技术设计 §16 + PR7）。

把 selection / entry / position 的固定窗口结果写入 decision_outcomes_v2。
纯结算逻辑不依赖券商；行情源可注入（futu / 测试 DataFrame）。

用法：
    python -m scripts.live_trading.run_outcomes            # 回填所有研究批次
    python -m scripts.live_trading.run_outcomes --latest   # 只回填最新一批
"""
import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from zoneinfo import ZoneInfo

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
                           benchmark_code: str = 'US.SPY') -> dict:
    """结算一个研究批次：universe 每只股票写 1/3/5/10/20d outcome。

    返回 {'settled': 已完成, 'pending': 未成熟占位}，区分「已结算」与「数据未成熟待后续补」。
    """
    universe = list(batch.get('universe') or [])
    as_of = batch.get('as_of')
    # v2 决策必须以 DecisionRun ID 为外键；旧批次才退回 research_batch_id。
    decision_id = batch.get('decision_id') or batch.get('research_batch_id')
    if not universe or not as_of or not decision_id:
        return {'settled': 0, 'pending': 0}
    settled = 0
    pending = 0
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
        settled += written
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
            pending += 1
    return {'settled': settled, 'pending': pending}


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


def load_position_counterfactuals(registry) -> list:
    """读取全部冻结实验；event_id 已保证每次评审只有一个冻结版本。"""
    from scripts.live_trading.decision_ledger.event_store import EventStore
    return [e['payload'] for e in EventStore(registry).events()
            if e.get('event_type') == 'position_counterfactual_frozen']


def load_entry_counterfactuals(registry) -> list:
    from scripts.live_trading.decision_ledger.event_store import EventStore
    return [e['payload'] for e in EventStore(registry).events()
            if e.get('event_type') == 'entry_counterfactual_frozen']


def load_selection_counterfactuals(registry) -> list:
    from scripts.live_trading.decision_ledger.event_store import EventStore
    return [e['payload'] for e in EventStore(registry).events()
            if e.get('event_type') == 'selection_counterfactual_frozen']


def position_future_bars(bars: pd.DataFrame, frozen: dict, max_bars: int = 20) -> list:
    """提取决策日之后的日线，首根开盘即 v1 的下一可成交时刻。

    Futu 日线 date 表示交易日而非精确开盘时刻，因此不能使用决策当日 bar；否则盘中
    决策会错误地使用已经发生的当日开盘价。
    """
    if bars is None or bars.empty or not frozen.get('code') or not frozen.get('as_of'):
        return []
    as_of = pd.Timestamp(frozen['as_of'])
    if as_of.tzinfo is None:
        raise ValueError('持仓反事实 as_of 必须明确时区')
    decision_day = as_of.tz_convert(ZoneInfo('America/New_York')).date()
    frame = bars[bars['code'] == frozen['code']].copy()
    if frame.empty:
        return []
    frame['date'] = pd.to_datetime(frame['date'], utc=True)
    frame = frame[frame['date'].dt.date > decision_day].sort_values('date').head(max_bars)
    required = ('open', 'low', 'close')
    if any(column not in frame.columns for column in required):
        return []
    return frame[['date', 'open', 'high', 'low', 'close']].to_dict('records')


def settle_position_counterfactuals(registry, frozen_items: list,
                                    bars: pd.DataFrame) -> dict:
    """增量结算所有已冻结持仓实验；未成熟期限留给下一次日任务。"""
    settlement = OutcomeSettlement(registry)
    experiments = horizons = pending = 0
    for frozen in frozen_items:
        future = position_future_bars(bars, frozen)
        if not future:
            pending += 1
            continue
        written = settlement.settle_position_counterfactual(frozen, future)
        experiments += 1
        horizons += written
        if len(future) < max(HORIZONS):
            pending += 1
    return {'experiments': experiments, 'settled_horizons': horizons, 'pending': pending}


def settle_entry_counterfactuals(registry, frozen_items: list,
                                 bars: pd.DataFrame, fee_rate: float = 0.0) -> dict:
    settlement = OutcomeSettlement(registry)
    experiments = horizons = pending = 0
    for frozen in frozen_items:
        future = position_future_bars(bars, frozen)
        if not future:
            pending += 1
            continue
        horizons += settlement.settle_entry_counterfactual(
            frozen, future, fee_rate=fee_rate)
        experiments += 1
        if len(future) < max(HORIZONS):
            pending += 1
    return {'experiments': experiments, 'settled_horizons': horizons, 'pending': pending}


def settle_selection_counterfactuals(registry, frozen_items: list,
                                     bars: pd.DataFrame) -> dict:
    """按同一冻结的 R/L 选股篮子做等权比较；成员未同时成熟则保持 pending。"""
    settlement = OutcomeSettlement(registry)
    experiments = horizons = pending = 0
    for frozen in frozen_items:
        paths = {'r': list(frozen.get('rule_selected') or []),
                 'l': list(frozen.get('llm_selected') or [])}
        if not paths['r'] or not paths['l']:
            pending += 1
            continue
        per_path = {}
        for name, codes in paths.items():
            per_path[name] = {
                code: position_future_bars(bars, {'code': code, 'as_of': frozen['as_of']})
                for code in codes}
        rows = []
        for h in HORIZONS:
            if any(len(series) < h for group in per_path.values() for series in group.values()):
                continue
            returns = {}
            for name, codes in paths.items():
                returns[name] = sum(
                    float(per_path[name][code][h - 1]['close']) /
                    float(per_path[name][code][0]['open']) - 1.0 for code in codes) / len(codes)
            rows.append({'horizon': f'{h}d', 'r_return_pct': returns['r'],
                         'l_return_pct': returns['l'],
                         'delta_return_pct': returns['l'] - returns['r'],
                         'rule_selected': paths['r'], 'llm_selected': paths['l'],
                         'replaced_out': frozen.get('replaced_out', []),
                         'replaced_in': frozen.get('replaced_in', [])})
        horizons += settlement.settle_selection_counterfactual(frozen, rows)
        experiments += 1
        if len(rows) < len(HORIZONS):
            pending += 1
    return {'experiments': experiments, 'settled_horizons': horizons, 'pending': pending}


def run(registry, batches, bars, benchmark_bars=None) -> dict:
    """对多个研究批次批量结算。返回 {'settled': 已完成, 'pending': 未成熟占位}。"""
    settlement = OutcomeSettlement(registry)
    settled = pending = 0
    for batch in batches:
        r = settle_selection_batch(batch, bars, settlement,
                                   benchmark_bars=benchmark_bars)
        settled += r['settled']
        pending += r['pending']
    return {'settled': settled, 'pending': pending}


def _emit(result: dict, exit_code: int) -> int:
    """输出结构化终态 JSON 并返回进程退出码。"""
    print(json.dumps(result, ensure_ascii=False, default=str))
    return exit_code


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

    runtime_cfg = (config.get('llm_decision', {}).get('engine_v2') or {})
    from scripts.live_trading.position_registry import PositionRegistry
    registry = PositionRegistry(namespace=runtime_cfg.get('account_scope', 'DRY-RUN'))
    frozen_items = load_position_counterfactuals(registry)
    entry_frozen_items = load_entry_counterfactuals(registry)
    selection_frozen_items = load_selection_counterfactuals(registry)

    from mutifactor.data.us_fetcher import FutuUSDataFetcher
    from scripts.live_trading.run_selection_outcomes import merge_bars
    futu_cfg = config.get('futu', {})
    fetcher = FutuUSDataFetcher(host=futu_cfg.get('host', '127.0.0.1'),
                                port=int(futu_cfg.get('port', 11111)))
    try:
        if not fetcher.connect():
            return _emit({'status': 'data_unavailable', 'reason_code': 'opend_connect_failed',
                          'retryable': True}, 1)
        frames = []
        for batch in batches:
            universe = batch.get('universe') or []
            as_of = batch.get('as_of')
            if not universe or not as_of:
                continue
            from datetime import timedelta
            start = (pd.Timestamp(as_of) - timedelta(days=40)).strftime('%Y-%m-%d')
            end = (pd.Timestamp(as_of) + timedelta(days=30)).strftime('%Y-%m-%d')
            frames.append(merge_bars(fetcher.fetch_multiple_stocks(
                list(dict.fromkeys(universe + ['US.SPY'])), start, end)))
        # 每只持仓按其全部冻结实验的最早/最晚日期一次拉取，避免后一次窄窗口覆盖前一次。
        by_code = {}
        for frozen in frozen_items + entry_frozen_items:
            if not frozen.get('code') or not frozen.get('as_of'):
                continue
            stamp = pd.Timestamp(frozen['as_of'])
            window = by_code.setdefault(frozen['code'], [stamp, stamp])
            window[0], window[1] = min(window[0], stamp), max(window[1], stamp)
        for frozen in selection_frozen_items:
            if not frozen.get('as_of'):
                continue
            stamp = pd.Timestamp(frozen['as_of'])
            for code in set(frozen.get('rule_selected') or []) | set(frozen.get('llm_selected') or []):
                window = by_code.setdefault(code, [stamp, stamp])
                window[0], window[1] = min(window[0], stamp), max(window[1], stamp)
        for code, (earliest, latest) in by_code.items():
            start = (earliest - pd.Timedelta(days=2)).strftime('%Y-%m-%d')
            end = (latest + pd.Timedelta(days=45)).strftime('%Y-%m-%d')
            frames.append(merge_bars(fetcher.fetch_multiple_stocks([code], start, end)))
        bars = (pd.concat([frame for frame in frames if not frame.empty], ignore_index=True)
                if any(not frame.empty for frame in frames)
                else merge_bars({}))
        if not bars.empty:
            bars = bars.sort_values('date').drop_duplicates(['code', 'date'], keep='last')
    except Exception as exc:
        return _emit({'status': 'data_unavailable', 'reason_code': 'fetch_error',
                      'retryable': True, 'error': str(exc)}, 1)
    finally:
        fetcher.disconnect()

    benchmark_bars = bars[bars['code'] == 'US.SPY'] if not bars.empty else bars
    try:
        result = run(registry, batches, bars, benchmark_bars=benchmark_bars)
        result['position_counterfactuals'] = settle_position_counterfactuals(
            registry, frozen_items, bars)
        fee_rate = float((config.get('llm_decision', {}).get('outcomes') or {}).get(
            'counterfactual_fee_rate', 0.0))
        result['entry_counterfactuals'] = settle_entry_counterfactuals(
            registry, entry_frozen_items, bars, fee_rate=fee_rate)
        result['selection_counterfactuals'] = settle_selection_counterfactuals(
            registry, selection_frozen_items, bars)
    except ValueError as exc:
        return _emit({'status': 'settlement_conflict', 'reason_code': 'event_conflict',
                      'retryable': False, 'error': str(exc)}, 1)
    except Exception as exc:
        return _emit({'status': 'settlement_error', 'reason_code': type(exc).__name__,
                      'retryable': False, 'error': str(exc)}, 1)
    status = 'succeeded' if result['pending'] == 0 else 'partial'
    return _emit({'status': status, 'settled': result['settled'],
                  'pending': result['pending']}, 0)


if __name__ == '__main__':
    sys.exit(main())
