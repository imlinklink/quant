"""H3 诊断：固定止损是否过紧 —— 第 1 步（机会级）。

**先登记再计算**：运行前必须存在已提交的预登记文件，脚本会把它连同 sha256 一起写进产物，
使"登记先于结果"可核对（`docs/preregistrations/EXIT-STOP-20260921.json`）。

**口径**（逐条对齐登记，不在这里改）：
· 配对总体 = study **实际成交的全部入场**（从自己的账本读 BUY 成交，含引擎当时的价与止损）；
· 两臂共用**同一条价格路径**：A = 含止损（引擎现有规则）；B = **关闭止损触发**，持有到 H60 收盘；
· **股数不重算**（§5.4「固定单位风险的机会级结果」）—— 两臂入场规模相同，差异只来自出场；
· 复用 `simulate_fixed_horizon_exits`（批处理矩阵的同一实现），它已处理拆股/分红并返回
  `mae_pct` / `mfe_pct`；不另写第二个价格路径模拟器。

**B 臂"关闭止损"是构造出来的**：把初始止损设为 `入场价 × 1e-6`（满足 `0 < stop < price`），
并**验证** B 臂从不出现止损类退出原因 —— 是证明，不是指望。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from scripts.medium_term.exit_matrix import MAX_HOLD, simulate_fixed_horizon_exits

from .exit_attribution import STOP_REASONS
from .inputs import load
from .manifest import write_json

ROOT = Path(__file__).resolve().parents[2]
REGISTRATION = ROOT / 'docs/preregistrations/EXIT-STOP-20260921.json'
NEGLIGIBLE_STOP_FRACTION = 1e-6


@dataclass(frozen=True)
class Arm:
    exit_reason: str
    exit_session: str | None
    pnl_pct: float | None
    net_r: float | None
    mae_pct: float | None
    mfe_pct: float | None
    holding_sessions: int | None
    data_quality: str


def registration_digest() -> str:
    if not REGISTRATION.exists():
        raise ValueError(f'PREREGISTRATION_MISSING:{REGISTRATION}')
    return hashlib.sha256(REGISTRATION.read_bytes()).hexdigest()


def entries_from_ledger(study_dir: Path) -> pd.DataFrame:
    """study **实际成交**的入场（含引擎当时的价与止损），从它自己的账本读。

    用成交而不是重新推导，是为了让配对总体与"实际发生了什么"逐笔一致；重算会引入
    "我以为它买了什么"与"它实际买了什么"的差异。
    """
    ledger = Path(study_dir) / 'variants/baseline/ledger.sqlite3'
    con = sqlite3.connect(ledger)
    rows = []
    for (body,) in con.execute("SELECT body FROM decision_events WHERE event_type='shadow:step'"):
        payload = json.loads(body)['payload']
        if payload.get('type') == 'fill' and payload.get('side') == 'BUY':
            rows.append({'security_id': str(payload['security_id']),
                         'entry_session': str(payload['session']),
                         'shares': int(payload['shares']),
                         'entry_price': payload['price_micro'] / 1e6,
                         'stop': payload['stop_micro'] / 1e6})
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError('NO_ENTRIES_IN_LEDGER')
    return frame.sort_values(['entry_session', 'security_id']).reset_index(drop=True)


def _arm(bars: pd.DataFrame, entry: dict, actions: pd.DataFrame, horizon: int,
         stop: float) -> dict:
    spec = {'security_id': entry['security_id'], 'entry_session': entry['entry_session'],
            'entry_price': entry['entry_price'], 'initial_stop': stop}
    return simulate_fixed_horizon_exits(spec, bars, (horizon,), actions=actions)[horizon]


def _to_arm(result: dict, entry: dict, round_trip_cost: float) -> dict:
    gross = result.get('gross_pnl_pct')
    distance_pct = (entry['entry_price'] - entry['stop']) / entry['entry_price']
    net = None if gross is None else gross - round_trip_cost
    # net_R 的分母是**冻结的初始风险金额**（每股口径）：止损距离 / 入场价 ⇒ 两臂同一分母
    net_r = None if net is None or distance_pct <= 0 else net / distance_pct
    # 返回 dict 而不是 dataclass 实例：产物要直接写 JSON，且摘要里按字段取值更直白
    return asdict(Arm(exit_reason=str(result.get('exit_reason')),
                      exit_session=(str(pd.Timestamp(result['exit_session']).date())
                                    if result.get('exit_session') is not None else None),
                      pnl_pct=net, net_r=net_r,
                      mae_pct=result.get('mae_pct'), mfe_pct=result.get('mfe_pct'),
                      holding_sessions=result.get('holding_sessions'),
                      data_quality=str(result.get('data_quality'))))


def step1(study_dir: Path, *, horizon: int = 60) -> dict:
    """机会级两臂对照。返回可直接进报告的记录。"""
    study_dir = Path(study_dir)
    digest = registration_digest()
    data = json.loads((study_dir / 'study_manifest.json').read_text(encoding='utf-8'))
    # 用既有加载器，不自己从文件名推证券 id（那正是冻结前缀陷阱的来源）
    prices, _market, _calendar, _quality, actions = load(data, study_dir)
    round_trip_cost = data['cost_policy']['fee_bp'] / 10000 * 2

    entries = entries_from_ledger(study_dir)
    by_security = {sid: g for sid, g in actions.groupby('security_id')}
    rows = []
    for entry in entries.to_dict('records'):
        stock = (prices[prices.security_id.eq(entry['security_id'])]
                 .sort_values('session').reset_index(drop=True))
        started = stock[stock.session >= pd.Timestamp(entry['entry_session'])].head(MAX_HOLD)
        if started.empty or started.session.iloc[0] != pd.Timestamp(entry['entry_session']):
            raise ValueError(f'ENTRY_SESSION_PRICE_MISSING:{entry["security_id"]}:{entry["entry_session"]}')
        acts = by_security.get(entry['security_id'], pd.DataFrame())
        actual = _to_arm(_arm(started, entry, acts, horizon, entry['stop']), entry, round_trip_cost)
        no_stop = _to_arm(_arm(started, entry, acts, horizon,
                               entry['entry_price'] * NEGLIGIBLE_STOP_FRACTION),
                          entry, round_trip_cost)
        rows.append({**entry, 'actual': actual, 'no_stop': no_stop,
                     'stopped': actual['exit_reason'] in STOP_REASONS,
                     'delta_r': (None if actual['net_r'] is None or no_stop['net_r'] is None
                                 else no_stop['net_r'] - actual['net_r'])})
    summary = _summarise(rows)
    return {'registration_sha256': digest, 'horizon': horizon,
            'round_trip_cost': round_trip_cost, 'trades': rows,
            'summary': summary, 'verdict': step1_verdict(summary)}


def _tail(rows: list[dict], arm: str) -> float | None:
    values = [r[arm]['net_r'] for r in rows if r[arm]['net_r'] is not None]
    return min(values) if values else None


def step1_verdict(summary: dict) -> dict:
    """按**登记文件**里的门槛判第 1 步。阈值写在这里是为了可核对，不是为了放宽。

    登记的两条口径：`单一证券贡献 ≤ 50% 的增量`（明确数值门槛）与
    "改善只集中在少数股票或少数交易 ⇒ 保留原策略"（§8.2 的 CONCENTRATED）。
    前者可判定；后者按登记的数值门槛（≤50%）与 leave-one-out 共同裁决，
    top-2/top-5 占比只作**描述性**呈现，不另设阈值（避免事后发明门槛）。
    """
    c, d = summary['concentration'], summary['by_security_ordered']
    checks = {
        'delta_positive': summary['delta_r_sum'] > 0,
        'no_single_security_over_50pct': (c['top1_share_of_delta'] or 1.0) <= 0.5,
        'leave_one_out_positive': bool(c['leave_one_out_still_positive']),
        'survivors_arms_identical': bool(summary['control_survivors_arms_identical']),
        'no_stop_never_leaked_a_stop_exit': not summary['no_stop_leaked_a_stop_exit'],
    }
    gate = ('no_single_security_over_50pct', 'leave_one_out_positive')
    if not (checks['survivors_arms_identical'] and checks['no_stop_never_leaked_a_stop_exit']):
        token = 'ENGINEERING_BLOCKED'      # 控制项不过 ⇒ 实现有误，先修（登记的停止条件）
    elif not checks['delta_positive']:
        token = 'NO_IMPROVEMENT'
    elif not all(checks[k] for k in gate):
        token = 'CONCENTRATED'
    else:
        token = 'STEP1_SUPPORTED'
    return {'token': token, 'checks': checks,
            'top2_share_of_delta': d['top2_share'], 'top5_trade_share_of_delta': d['top5_share'],
            'note': 'top2/top5 占比为描述性；判定用登记的两条数值门槛。'}


def _summarise(rows: list[dict]) -> dict:
    """登记里要求的三条分解 + 集中度/成本压力。"""
    delta = [r['delta_r'] for r in rows if r['delta_r'] is not None]
    stopped = [r for r in rows if r['stopped']]
    survivors = [r for r in rows if not r['stopped']]
    # 控制项：未触发止损的交易两臂必须**完全相同**（登记 §8 的停止条件）
    mismatched = [{'security_id': r['security_id'], 'entry_session': r['entry_session'],
                   'actual': r['actual']['exit_reason'], 'no_stop': r['no_stop']['exit_reason']}
                  for r in survivors
                  if (r['actual']['exit_reason'], r['actual']['exit_session'])
                  != (r['no_stop']['exit_reason'], r['no_stop']['exit_session'])]
    # B 臂的构造校验：关闭止损后不得再出现止损类退出
    leaked = [r['security_id'] for r in rows if r['no_stop']['exit_reason'] in STOP_REASONS]

    recovered = [r for r in stopped if (r['no_stop']['net_r'] or 0) > 0]      # 反事实下转盈
    avoided = [r for r in stopped if (r['no_stop']['net_r'] or 0) <= 0]       # 反事实下仍亏
    by_security = defaultdict(float)
    for r in rows:
        if r['delta_r'] is not None:
            by_security[r['security_id']] += r['delta_r']
    ordered = sorted(by_security.items(), key=lambda kv: kv[1], reverse=True)
    total_delta = sum(delta)
    worst = sorted((r for r in rows if r['actual']['net_r'] is not None),
                   key=lambda r: r['actual']['net_r'])[:5]
    return {
        'count': len(rows), 'stopped': len(stopped), 'survivors': len(survivors),
        'delta_r_sum': total_delta,
        'delta_r_mean': (total_delta / len(delta)) if delta else None,
        'actual_net_r_sum': sum(r['actual']['net_r'] for r in rows if r['actual']['net_r'] is not None),
        'no_stop_net_r_sum': sum(r['no_stop']['net_r'] for r in rows if r['no_stop']['net_r'] is not None),
        'stopped_breakdown': {
            'would_have_recovered': len(recovered),
            'recovered_delta_r': sum(r['delta_r'] for r in recovered if r['delta_r'] is not None),
            'avoided_further_loss': len(avoided),
            'avoided_delta_r': sum(r['delta_r'] for r in avoided if r['delta_r'] is not None)},
        'control_survivors_arms_identical': not mismatched,
        'control_mismatches': mismatched[:5],
        'no_stop_leaked_a_stop_exit': leaked,
        'concentration': {
            'top1_security': ordered[0][0] if ordered else None,
            'top1_share_of_delta': ((ordered[0][1] / total_delta)
                                    if ordered and total_delta else None),
            'leave_one_out_still_positive': ((total_delta - ordered[0][1]) > 0
                                             if ordered and total_delta else None),
            'by_security_micro_r': dict(ordered)},
        'by_security_ordered': {
            'descending': [{'security_id': k, 'delta_r': v} for k, v in ordered],
            'top2_share': (sum(v for _, v in ordered[:2]) / total_delta) if total_delta else None,
            'top5_share': (sum(v for _, v in ordered[:5]) / total_delta) if total_delta else None},
        # 左尾对照：关闭止损后最差单笔有多大 —— 登记里 step2 的"最差单笔 net_R 不劣于基线"
        # 在机会级就已经能看到结论。
        'tail': {
            'worst_net_r': {'actual': _tail(rows, 'actual'), 'no_stop': _tail(rows, 'no_stop')},
            'below_minus_1r': {arm: sum(1 for r in rows
                                        if r[arm]['net_r'] is not None and r[arm]['net_r'] < -1)
                               for arm in ('actual', 'no_stop')},
            'below_minus_2r': {arm: sum(1 for r in rows
                                        if r[arm]['net_r'] is not None and r[arm]['net_r'] < -2)
                               for arm in ('actual', 'no_stop')}},
        'worst_actual_net_r': [{'security_id': r['security_id'], 'net_r': r['actual']['net_r']}
                               for r in worst],
        'mae_pct_mean': (sum(r['actual']['mae_pct'] for r in rows) / len(rows)),
        'mfe_pct_mean': (sum(r['actual']['mfe_pct'] for r in rows) / len(rows)),
    }


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    result = step1(Path(args.study).resolve())
    write_json(Path(args.out), result)
    summary = result['summary']
    print(json.dumps({k: summary[k] for k in (
        'count', 'stopped', 'survivors', 'delta_r_sum', 'delta_r_mean',
        'stopped_breakdown', 'control_survivors_arms_identical',
        'no_stop_leaked_a_stop_exit', 'concentration', 'tail')}, ensure_ascii=False, indent=1))
    print('verdict:', json.dumps(result['verdict'], ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
