"""B3 宇宙归因三臂实验（登记 `B3-UNIVERSE-ATTR-20260921` + 工程修订 01）。

**只诊断，不选型**（§5/§7②）。三臂**只差宇宙**（修订 01 定的输入基）：

| 臂 | 候选池 | 时点门 |
|---|---|---|
| A | 13 只硬编码 | 无 |
| B | 32 只（NONE 宇宙 ∩ v4 verified） | 有（**排名前**过滤） |
| C | 同 B | 无 |

B−A = 后视挑选的总贡献；B−C = 时点门单独的效果。

**两道 fail-closed 控制**：
1. **harness 保真度**——用冻结基线当年的配置（v3 质量 + 旧行动表 + 13 只 + audited blocked）
   跑一次，必须复现 `final_equity 448722.3536857565` / `MDD −0.1390344329`。**它不参与三臂比较**，
   只证明这套 harness 忠实（修订 01 把这件控制从 A 臂里分了出来）。
2. **输入基共享**——A 与 B/C 的 quality/actions/market 与 blocked 算法必须同一份；输出记录哈希。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.medium_term.action_coverage import audit_action_coverage, blocked_sessions
from scripts.medium_term.p1_account_check import (ETF_RAW, QQQ_DIVIDENDS, _load_adjusted,
                                                  _load_qqq_dividends, _load_qqq_prices)
from scripts.medium_term.p2_selection_check import (PANELS, TECH, build_entries, build_matrix,
                                                    build_timed_entries, load_panels,
                                                    market_frame, trading_calendar)
from scripts.medium_term.performance import performance_metrics
from scripts.medium_term.portfolio_engine import simulate_multi_asset_portfolio
from scripts.medium_term.qqq_benchmark import build_qqq_benchmark
from scripts.medium_term.risk_rule_experiment import (_annual_stability, _concentration)
from scripts.medium_term.stock_cross_section import generate_monthly_candidates
from scripts.strategy_diagnostics.manifest import file_hash, read, write_json

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / 'docs/preregistrations'
REGISTRATION = DOCS / 'B3-UNIVERSE-ATTR-20260921.json'
AMENDMENT = DOCS / 'B3-UNIVERSE-ATTR-20260921.engineering-amendment.json'

# 修订 01 定的共同输入基
QUALITY = ROOT / 'data/survivor_sample_audit/research_quality_intervals-v4.csv'
ACTIONS = ROOT / 'data/corporate_actions_runs/futu-actions39-20260920c/corporate_actions.csv'
UNIVERSE = ROOT / 'data/market_history/runs/SURVIVOR39-NONE-20260920/universe_v2/universe.csv.gz'
# harness 保真度控制用的**当年**输入（不参与三臂）
FROZEN_QUALITY = ROOT / 'data/survivor_sample_audit/research_quality_intervals-v3.csv'
FROZEN_ACTIONS = ROOT / 'data/corporate_actions_runs/futu-survivor39-20260912/corporate_actions.csv'
FROZEN_FINAL_EQUITY = 448722.3536857565
FROZEN_MDD = -0.1390344329

TOP_N, HORIZON, MAX_WAIT = 5, 60, 20
RISK_FRACTION, MAX_WEIGHT, MAX_POSITIONS = .01, .20, 5
COST, INITIAL_CASH = .002, 100_000.
WINDOW = ('2016-01-05', '2026-08-25')          # 与冻结基线同窗，三臂共用


def _digest(path) -> str:
    return file_hash(Path(path))


def _plain(value):
    """numpy/pandas 标量 → Python 原生。

    `manifest.write_json` 用 `allow_nan=False` 且**不认 numpy 类型**，所以一个
    `numpy.int64` 就能让 18 分钟的计算在最后一行白跑（实测撞过）。转换放在写盘前一次做完。
    """
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def verified_names() -> list[str]:
    """v4 里 verified 且非 SPY 的证券（§3.1 要求 HON 保持排除 —— 它本就 unverified）。"""
    q = pd.read_csv(QUALITY)
    ok = set(q.loc[q.quality_status.eq('verified'), 'security_id'].astype(str))
    return sorted(ok - {'SEC-US-SPY'})


def members_mask(names, start, end) -> pd.DataFrame:
    """时点宇宙掩码：NONE run 的 `eligible`，限定到 `names` 与窗口。"""
    u = pd.read_csv(UNIVERSE, usecols=['universe_date', 'security_id', 'eligible'])
    u['session'] = pd.to_datetime(u.universe_date).dt.tz_localize(None).dt.normalize()
    u['security_id'] = u.security_id.astype(str)
    u = u[u.security_id.isin(set(names))]
    return u[(u.session >= pd.Timestamp(start)) & (u.session <= pd.Timestamp(end))][
        ['session', 'security_id', 'eligible']].reset_index(drop=True)


def action_gate(prices, names, actions) -> dict:
    """行动覆盖门（三臂**同一算法**）：verdict != ok 的证券阻断其区间。"""
    blocked = {}
    for sid in names:
        raw = (prices.loc[prices.security_id.eq(sid), ['session', 'raw_close']]
               .rename(columns={'raw_close': 'close'}))
        if raw.empty:
            blocked[sid] = ()
            continue
        audit = audit_action_coverage(raw, _load_adjusted(sid.replace('SEC-US-', 'US_')),
                                      actions, sid)
        blocked[sid] = blocked_sessions(audit)
    return blocked


def build_arm(names, prices, quality, actions, blocked, members, etf_path):
    """一条臂的矩阵。**除候选池与 members 外，与另两臂逐字相同。**"""
    view = prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                   'raw_close', 'volume']].rename(columns={
        'raw_open': 'open', 'raw_high': 'high', 'raw_low': 'low', 'raw_close': 'close'})
    cand = generate_monthly_candidates(view, market_frame(etf_path), top_n=TOP_N,
                                       actions=actions, members=members)
    selected = cand[cand.selected.astype(bool)].copy()
    timed = build_timed_entries(
        selected[['security_id', 'decision_session', 'execution_session', 'rank']],
        prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                'raw_close', 'volume']],
        trading_calendar(etf_path), actions=actions)
    spec = timed[timed.entry_type.ne('EXPIRED') & timed.execution_session.notna()]
    prepared, funnel = build_entries(spec, prices, quality, blocked,
                                     atr_session_col='signal_session')
    matrix = build_matrix(prepared, prices, actions) if not prepared.empty else pd.DataFrame()
    if not matrix.empty:
        matrix['portfolio_accepted'] = True     # 统一走运行时五仓拒绝（与 risk_rule 同口径）
    return matrix, funnel, cand


def run_arm(names, *, gate, label, quality, actions, etf_path, qqq_prices, qqq_dividends,
            benchmark, members=None):
    prices, _paths = load_panels(tech=tuple(names), panels=PANELS)
    blocked = action_gate(prices, names, actions) if gate else {}
    matrix, funnel, cand = build_arm(names, prices, quality, actions, blocked, members, etf_path)
    start, end = pd.Timestamp(WINDOW[0]), pd.Timestamp(WINDOW[1])
    window_prices = prices[(prices.session >= start) & (prices.session <= end)]
    if matrix.empty:
        return {'label': label, 'error': 'NO_MATRIX', 'candidate_rows': len(cand)}
    cell = matrix[matrix.holding_sessions.eq(HORIZON)].copy()
    if cell.empty:
        return {'label': label, 'error': f'NO_CELL:H{HORIZON}', 'candidate_rows': len(cand)}
    portfolio = simulate_multi_asset_portfolio(
        window_prices, cell, initial_cash=INITIAL_CASH, risk_fraction=RISK_FRACTION,
        max_weight=MAX_WEIGHT, round_trip_cost=COST, actions=actions,
        allow_fractional=False, max_positions=MAX_POSITIONS, evaluation_start=start)
    metrics = performance_metrics(portfolio.equity, benchmark=benchmark)
    bench = performance_metrics(benchmark)
    buys = portfolio.trades[portfolio.trades.side.eq('BUY')]
    sells = portfolio.trades[portfolio.trades.side.eq('SELL')]
    stability = _annual_stability(portfolio.equity)
    concentration = _concentration(portfolio.trades)
    exposure = metrics.get('average_gross_exposure')
    return {
        'label': label, 'universe_size': len(names), 'pit_gate': gate,
        'candidate_rows': int(len(cand)), 'selected_rows': int(cand.selected.sum()),
        'prepared': int(len(matrix)), 'accepted_entries': int(len(buys)),
        'round_trips': int(len(sells)), 'right_censored': int(len(buys) - len(sells)),
        'final_equity': metrics.get('final_equity'), 'CAGR': metrics.get('CAGR'),
        'max_drawdown': metrics.get('max_drawdown'), 'Calmar': metrics.get('Calmar'),
        'Sharpe': metrics.get('Sharpe'), 'average_gross_exposure': exposure,
        'return_per_unit_exposure': (metrics.get('CAGR') / exposure) if exposure else None,
        'turnover': metrics.get('turnover'),
        'benchmark_CAGR': metrics.get('benchmark_CAGR'), 'excess_CAGR': metrics.get('excess_CAGR'),
        'top1_winner_share': concentration['top1_winner_share'],
        'top5_winner_share': concentration['top5_winner_share'],
        'worst_year_return': stability['worst_year_return'],
        'down_year_ratio': stability['down_year_ratio'],
        'start_date': metrics.get('start_date'), 'end_date': metrics.get('end_date'),
    }


def harness_control(qqq_prices, qqq_dividends) -> dict:
    """冻结基线当年的配置跑一次 —— 只证明 harness 忠实，**不参与三臂比较**。"""
    prices, _ = load_panels(tech=TECH, panels=PANELS)
    quality = pd.read_csv(FROZEN_QUALITY)
    actions = pd.read_csv(FROZEN_ACTIONS)
    actions['security_id'] = actions.security_id.astype(str)
    blocked = action_gate(prices, list(TECH), actions)
    matrix, _funnel, _cand = build_arm(list(TECH), prices, quality, actions, blocked, None, ETF_RAW)
    start, end = pd.Timestamp(WINDOW[0]), pd.Timestamp(WINDOW[1])
    window_prices = prices[(prices.session >= start) & (prices.session <= end)]
    portfolio = simulate_multi_asset_portfolio(
        window_prices, matrix[matrix.holding_sessions.eq(HORIZON)],
        initial_cash=INITIAL_CASH, risk_fraction=RISK_FRACTION, max_weight=MAX_WEIGHT,
        round_trip_cost=COST, actions=actions, allow_fractional=False,
        max_positions=MAX_POSITIONS, evaluation_start=start)
    metrics = performance_metrics(portfolio.equity)
    got_equity, got_mdd = metrics.get('final_equity'), metrics.get('max_drawdown')
    ok = (got_equity is not None and abs(got_equity - FROZEN_FINAL_EQUITY) < 1e-6
          and got_mdd is not None and abs(got_mdd - FROZEN_MDD) < 1e-9)
    return {'final_equity': got_equity, 'expected_final_equity': FROZEN_FINAL_EQUITY,
            'max_drawdown': got_mdd, 'expected_max_drawdown': FROZEN_MDD, 'passed': bool(ok)}


def execute(output: Path) -> dict:
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('OUTPUT_EXISTS')
    reg = read(REGISTRATION)
    amendment = read(AMENDMENT)
    if _digest(REGISTRATION) != amendment['original_registration_sha256']:
        raise ValueError('REGISTRATION_CHANGED')
    if reg['returns_examined'] or amendment['returns_examined']:
        raise ValueError('REGISTRATION_NOT_BLIND')

    qqq_prices = _load_qqq_prices(ETF_RAW)
    qqq_dividends = _load_qqq_dividends(QQQ_DIVIDENDS)
    start, end = pd.Timestamp(WINDOW[0]), pd.Timestamp(WINDOW[1])
    benchmark = build_qqq_benchmark(qqq_prices, qqq_dividends, start, end)

    control = harness_control(qqq_prices, qqq_dividends)
    result = {'registration_sha256': _digest(REGISTRATION),
              'amendment_sha256': _digest(AMENDMENT),
              'window': {'start': WINDOW[0], 'end': WINDOW[1]},
              'inputs': {'quality': _digest(QUALITY), 'actions': _digest(ACTIONS),
                         'universe': _digest(UNIVERSE), 'market': _digest(ETF_RAW)},
              'input_basis_shared_by_all_arms': True,
              'excluded': {'spy': 'non_equity', 'hon': 'CORPORATE_ACTION_UNRESOLVED'},
              'harness_control': control}
    if not control['passed']:
        result['status'] = 'HARNESS_CONTROL_FAILED'
        write_json(output, _plain(result))
        return result

    names32 = verified_names()
    mask = members_mask(names32, WINDOW[0], WINDOW[1])
    quality = pd.read_csv(QUALITY)
    actions = pd.read_csv(ACTIONS)
    actions['security_id'] = actions.security_id.astype(str)
    result['universe'] = {
        'arm_A_names': list(TECH), 'arm_BC_count': len(names32),
        'arm_BC_names': names32,
        'listing_year_distribution': dict(Counter(
            pd.read_csv(QUALITY).set_index('security_id').loc[names32, 'from_session']
            .str[:4])),
        'eligible_sessions_per_security': dict(
            mask.groupby('security_id').eligible.sum())}
    arms = {
        'A': run_arm(list(TECH), gate=False, label='A_13_hardcoded', quality=quality,
                     actions=actions, etf_path=ETF_RAW, qqq_prices=qqq_prices,
                     qqq_dividends=qqq_dividends, benchmark=benchmark),
        'B': run_arm(names32, gate=True, label='B_32_pit_gate', quality=quality,
                     actions=actions, etf_path=ETF_RAW, qqq_prices=qqq_prices,
                     qqq_dividends=qqq_dividends, benchmark=benchmark, members=mask),
        'C': run_arm(names32, gate=False, label='C_32_no_gate', quality=quality,
                     actions=actions, etf_path=ETF_RAW, qqq_prices=qqq_prices,
                     qqq_dividends=qqq_dividends, benchmark=benchmark),
    }
    result['arms'] = arms
    done = {k: v for k, v in arms.items() if 'CAGR' in v}
    if len(done) == 3:
        result['attribution'] = {
            'B_minus_A_excess_CAGR': (arms['B']['CAGR'] - arms['A']['CAGR']),
            'B_minus_C_excess_CAGR': (arms['B']['CAGR'] - arms['C']['CAGR']),
            'note': '只能读作「后视挑选贡献了 X 个百分点」；不得读作策略优势、可放行、'
                    '样本外证据或未来收益预期（§5）。边界见 §6 四条。'}
    result['status'] = 'DIAGNOSTIC_COMPLETE' if len(done) == 3 else 'ARM_FAILED'
    result['account_level_started'] = False
    write_json(output, _plain(result))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    result = execute(Path(args.out))
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ('universe',)}, ensure_ascii=False, indent=2, default=str))
    return 0 if result['status'] == 'DIAGNOSTIC_COMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
