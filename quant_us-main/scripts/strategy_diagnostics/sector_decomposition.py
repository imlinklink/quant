"""行业倾斜 vs 事后挑名字：把 B−A 拆开（登记 `SECTOR-SELECTION-SPLIT-20260921`）。

上游 = 归因实验（B−A = −6.04pp，**有**时点门）。本实验按登记把时点门在**全部**臂关闭
（变量是池子，不是门）⇒ 本文件的 `B_32_ref` 等于归因实验的 **C** 臂，故这里的
`B − A = −5.88pp`（与归因实验的 `C − A` 一致，不是 −6.04pp）。

**描述性分解，不是因果**（§8.3：容量与路径依赖产生交互）。四臂 + 第二套行业边界 = 六臂：

| 臂 | 池 | 读作 |
|---|---|---|
| A | 13 只硬编码 | 后视 + 科技 |
| T | 17 只（规则建立的科技子集） | 规则 + 科技 |
| N | 15 只（规则建立的非科技） | 规则 + 非科技 |
| B | 32 只（= T ∪ N） | 参照 |
| Tn | 13 只（GICS 口径 IT） | 第二套边界 |
| Nn | 19 只（非 IT） | 第二套边界 |

`T − A` = 事后挑名字的效应；`N − T` = 行业倾斜的效应。**两者不能相加成 `B − A`**
（`N − T` 比 `B − A` 还大 —— 池子小了损害不被稀释，见结果文档「不是加法的」一节）。

**复用**归因实验的 `harness_control` 与输入基（同一份实现与文件，不重写）；四臂的 PIT 门**全部关闭**
（本实验的变量是池子，不是门）。**不**复用 `run_arm` —— 它每臂重算最贵的两步，本文件用
`run_arms_shared` 共享之；等价性由「A 臂必须与归因实验逐字段相同」这条控制项在运行时把守。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from scripts.medium_term.p1_account_check import (QQQ_DIVIDENDS, _load_qqq_dividends,
                                                  _load_qqq_prices)
from scripts.medium_term.p2_selection_check import (PANELS, build_entries, build_matrix,
                                                    build_timed_entries, load_panels,
                                                    market_frame, trading_calendar)
from scripts.medium_term.portfolio_engine import simulate_multi_asset_portfolio
from scripts.medium_term.qqq_benchmark import build_qqq_benchmark
from scripts.medium_term.stock_cross_section import assemble_candidates, monthly_snapshots
from scripts.strategy_diagnostics.manifest import file_hash, read, write_json
from scripts.strategy_diagnostics.universe_attribution import (ACTIONS, ETF_RAW, QUALITY,
                                                               _plain, harness_control)
from scripts.strategy_diagnostics.forward_arms import arm_names

TOP_N, HORIZON = 5, 60
INITIAL_CASH, RISK_FRACTION, MAX_WEIGHT, MAX_POSITIONS, COST = 100_000., .01, .20, 5, .002

ROOT = Path(__file__).resolve().parents[2]
REGISTRATION = ROOT / 'docs/preregistrations/SECTOR-SELECTION-SPLIT-20260921.json'
ATTRIBUTION_RESULT = ROOT / 'docs/preregistrations/B3-UNIVERSE-ATTR-20260921.result.json'
WINDOW = ('2016-01-05', '2026-08-25')


def _check_subsets(reg) -> dict:
    """子集闭合：`T ∪ N == B` 且 `T ∩ N == ∅` —— 不闭合就不要往下算。"""
    broad = set(reg['sector_mapping']['tech_broad']['names'])
    narrow = set(reg['sector_mapping']['tech_narrow_gics']['names'])
    non_tech = set(reg['sector_mapping']['non_tech_in_both'])
    universe = set(arm_names()['B'])
    hardcoded = set(arm_names()['A'])
    if (broad | non_tech) != universe or (broad & non_tech):
        raise ValueError('SUBSET_CLOSURE_FAILED:分解不闭合')
    if narrow - broad:
        raise ValueError('NARROW_NOT_SUBSET_OF_BROAD')
    if hardcoded - broad:
        raise ValueError('A_NOT_INSIDE_TECH')
    return {'tech_broad': len(broad), 'tech_narrow': len(narrow),
            'non_tech': len(non_tech), 'universe': len(universe),
            'rule_tech_not_in_hindsight_13': sorted(broad - hardcoded),
            'gics_reclassified_out': sorted(broad - narrow)}


def static_mask(names, sessions) -> pd.DataFrame:
    """静态成员掩码：只把**本臂的池子**标为合格。

    多臂共享截面时这是必需的 —— 否则每臂都会在**并集**上排名、选出同一批前 5 ✗。
    （PIT 门是另一件事：那是"当日合格"，本实验各臂都关闭。）
    """
    return pd.DataFrame([{'session': pd.Timestamp(s), 'security_id': sid,
                          'eligible': sid in set(names)}
                         for s in sessions for sid in names])


def run_arms_shared(plan, *, quality, actions, etf_path, qqq_prices, qqq_dividends,
                    benchmark) -> dict:
    """多臂共享最贵的两步，其余逐臂。

    实测（32 只的臂 6.5 分钟）：`generate_monthly_candidates` **317s（83%）**、
    `build_timed_entries` **64s（16%）**，而这两步都是**按证券、与池子无关**的
    （前者按 (证券, as_of) 重建复权视图、后者 `bars.groupby('security_id')`）。
    6 个臂各自重算 = 把这两步做 6 遍。

    共享是**等价的**，不是"差不多"：掩码在排名之前标不合格，而 `rank_cross_section`
    的排序键是完整序 `(momentum_score, mom_6m, security_id)` ⇒ 成员名次与"只喂成员"
    逐位相同；`build_timed_entries` 按候选自己的等待窗算，也与池子无关。
    """
    start, end = (pd.Timestamp(w) for w in WINDOW)
    union = sorted({sid for names in plan.values() for sid in names})
    prices, _paths = load_panels(tech=tuple(union), panels=PANELS)
    market = market_frame(etf_path)
    sessions = trading_calendar(etf_path)
    # 截面那一步吃的是**改名后的视图**（open/high/low/close），
    # 而 `build_timed_entries` / `build_entries` 吃的是 raw_* 的原始帧 —— 两者不能混
    view = prices[['security_id', 'session', 'raw_open', 'raw_high', 'raw_low',
                   'raw_close', 'volume']].rename(columns={
        'raw_open': 'open', 'raw_high': 'high', 'raw_low': 'low', 'raw_close': 'close'})
    snapshots = monthly_snapshots(view, actions=actions)            # 317s，只做一次
    selected, masks = {}, {}
    for label, names in plan.items():
        masks[label] = static_mask(names, sessions)
        cand = assemble_candidates(snapshots, view, market, top_n=TOP_N,
                                   members=masks[label])
        sel = cand[cand.selected.astype(bool)].copy()
        selected[label] = sel[['security_id', 'decision_session', 'execution_session', 'rank']]
    # 候选去重后**一次**算择时（按候选自己的等待窗，与池子无关）
    unique = (pd.concat([f.assign(arm=label) for label, f in selected.items()], ignore_index=True)
              .drop_duplicates(['security_id', 'decision_session', 'rank']))
    timed = build_timed_entries(unique[['security_id', 'decision_session',
                                        'execution_session', 'rank']],
                                prices, sessions, actions=actions)  # 64s，只做一次
    out = {}
    for label, names in plan.items():
        # `timed` 覆盖了各臂入选候选的并集 ⇒ 每臂只需按**自己的候选键**取回那几行。
        # 键是 (证券, 决策日, rank)：候选身份，与池子无关。
        merged = timed.merge(selected[label][['security_id', 'decision_session', 'rank']],
                             on=['security_id', 'decision_session', 'rank'], how='inner')
        if len(merged) != len(selected[label]):
            raise ValueError(f'SHARED_TIMED_JOIN_MISMATCH:{label}:'
                             f'{len(merged)}!={len(selected[label])}')
        spec = merged[merged.entry_type.ne('EXPIRED') & merged.execution_session.notna()]
        prepared, funnel = build_entries(spec, prices, quality, {},
                                        atr_session_col='signal_session')
        matrix = (build_matrix(prepared, prices, actions) if not prepared.empty
                  else pd.DataFrame())
        if matrix.empty:
            out[label] = {'label': label, 'error': 'NO_MATRIX'}
            continue
        cell = matrix[matrix.holding_sessions.eq(HORIZON)].copy()
        portfolio = simulate_multi_asset_portfolio(
            prices[(prices.session >= start) & (prices.session <= end)], cell,
            initial_cash=INITIAL_CASH, risk_fraction=RISK_FRACTION, max_weight=MAX_WEIGHT,
            round_trip_cost=COST, actions=actions, allow_fractional=False,
            max_positions=MAX_POSITIONS, evaluation_start=start)
        from scripts.medium_term.performance import performance_metrics
        metrics = performance_metrics(portfolio.equity, benchmark=benchmark)
        buys = portfolio.trades[portfolio.trades.side.eq('BUY')]
        sells = portfolio.trades[portfolio.trades.side.eq('SELL')]
        out[label] = {'label': label, 'universe_size': len(names), 'pit_gate': False,
                      'selected_rows': int(len(selected[label])),
                      'prepared': int(len(prepared)), 'accepted_entries': int(len(buys)),
                      'round_trips': int(len(sells)),
                      'right_censored': int(len(buys) - len(sells)),
                      'final_equity': metrics.get('final_equity'),
                      'CAGR': metrics.get('CAGR'), 'max_drawdown': metrics.get('max_drawdown'),
                      'Calmar': metrics.get('Calmar'), 'Sharpe': metrics.get('Sharpe'),
                      'average_gross_exposure': metrics.get('average_gross_exposure')}
    return out


def execute(output: Path) -> dict:
    output = Path(output).resolve()
    if output.exists():
        raise ValueError('OUTPUT_EXISTS')
    reg = read(REGISTRATION)
    if reg.get('returns_examined'):
        raise ValueError('REGISTRATION_NOT_BLIND')
    closure = _check_subsets(reg)
    qqq_prices, qqq_dividends = _load_qqq_prices(ETF_RAW), _load_qqq_dividends(QQQ_DIVIDENDS)
    start, end = (pd.Timestamp(w) for w in WINDOW)
    benchmark = build_qqq_benchmark(qqq_prices, qqq_dividends, start, end)
    control = harness_control(qqq_prices, qqq_dividends)
    result = {'registration_sha256': file_hash(REGISTRATION), 'window': list(WINDOW),
              'subset_closure': closure, 'harness_control': control, 'returns_examined': False}
    if not control['passed']:
        result['status'] = 'HARNESS_CONTROL_FAILED'
        write_json(output, _plain(result))
        return result
    quality, actions = pd.read_csv(QUALITY), pd.read_csv(ACTIONS)
    actions['security_id'] = actions.security_id.astype(str)
    plan = {
        'A_13_hindsight_tech': sorted(set(arm_names()['A'])),
        'T_17_rule_tech': sorted(set(reg['sector_mapping']['tech_broad']['names'])),
        'N_15_rule_non_tech': sorted(set(reg['sector_mapping']['non_tech_in_both'])),
        'B_32_ref': sorted(set(arm_names()['B'])),
        # 第二套边界（GICS）：登记要求两套都算 —— 边界是判断，敏感性要看得见
        'Tn_13_gics_it': sorted(set(reg['sector_mapping']['tech_narrow_gics']['names'])),
        'Nn_19_non_it': sorted(set(arm_names()['B'])
                               - set(reg['sector_mapping']['tech_narrow_gics']['names'])),
    }
    arms = run_arms_shared(plan, quality=quality, actions=actions, etf_path=ETF_RAW,
                           qqq_prices=qqq_prices, qqq_dividends=qqq_dividends,
                           benchmark=benchmark)
    result['arms'] = arms
    done = {k: v for k, v in arms.items() if 'CAGR' in v}
    if len(done) == len(plan):
        a, t, n, b = (arms[k]['CAGR'] for k in ('A_13_hindsight_tech', 'T_17_rule_tech',
                                                'N_15_rule_non_tech', 'B_32_ref'))
        tn = arms['Tn_13_gics_it']['CAGR']
        nn = arms['Nn_19_non_it']['CAGR']
        result['decomposition_cagr'] = {
            'broad_boundary': {
                'T_minus_A_name_selection': t - a,
                'N_minus_T_sector': n - t,
                'B_minus_A_total': b - a,
                'check_additive': (t - a) + (b - t)},
            'gics_boundary': {
                'Tn_minus_A_name_selection': tn - a,
                'Nn_minus_Tn_sector': nn - tn,
                'B_minus_A_total': b - a},
            'note': '描述性分解；容量与路径依赖产生交互，不得读成逐笔因果（§8.3）。'
                    '两套边界都给，是因为**边界是我写的判断**，敏感性必须看得见。'}
        # 控制项：A 臂必须与归因实验的 A 臂逐字段相同（同输入同参数）
        prior = read(ATTRIBUTION_RESULT)['arms']['A']
        keys = ('CAGR', 'max_drawdown', 'final_equity', 'accepted_entries')
        drift = {k: [prior[k], arms['A_13_hindsight_tech'][k]] for k in keys
                 if prior[k] != arms['A_13_hindsight_tech'][k]}
        if drift:
            raise ValueError(f'ARM_A_MISMATCH_WITH_ATTRIBUTION:{drift}')
        result['arm_A_matches_attribution'] = True
        result['status'] = 'DIAGNOSTIC_COMPLETE'
    else:
        result['status'] = 'ARM_FAILED'
    write_json(output, _plain(result))
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    result = execute(Path(args.out))
    print(json.dumps({k: v for k, v in result.items() if k != 'arms'},
                     ensure_ascii=False, indent=2, default=str))
    return 0 if result['status'] == 'DIAGNOSTIC_COMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
