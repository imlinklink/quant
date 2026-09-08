"""固定窗口结果（阶段 G3）：为研究候选计算 1/3/5/10 会话收益/MFE/MAE，比较排序质量。

纯函数，离线使用。不触发 LLM、不下单、不改交易状态。
"""
import math

import numpy as np
import pandas as pd


def compute_window_outcomes(bars, codes, as_of, horizons=(1, 3, 5, 10)):
    """对每个 code，从 as_of 起计算未来 N 会话的收益 / MFE / MAE。

    bars: DataFrame，含 code/date/open/high/low/close，date 为 UTC。
    返回 {code: {horizon: {'return','mfe','mae','complete'}}}。
    数据不足（未来不足 N 会话）标 complete=False，不伪造收益。
    """
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize('UTC')
    out = {}
    for code in codes:
        df = bars[bars['code'] == code].sort_values('date')
        prior = df[df['date'] <= as_of]
        if prior.empty:
            out[code] = {h: {'complete': False} for h in horizons}
            continue
        base = float(prior['close'].iloc[-1])
        if not math.isfinite(base) or base <= 0:
            out[code] = {h: {'complete': False} for h in horizons}
            continue
        future = df[df['date'] > as_of]
        per = {}
        for h in horizons:
            win = future.head(h)
            if len(win) < h:
                per[h] = {'complete': False}
                continue
            per[h] = {
                'complete': True,
                'return': float(win['close'].iloc[-1]) / base - 1.0,
                'mfe': float(win['high'].max()) / base - 1.0,
                'mae': float(win['low'].min()) / base - 1.0,
            }
        out[code] = per
    return out


def _spearman(x, y):
    """Spearman 秩相关。样本不足返回 None。"""
    n = len(x)
    if n < 2:
        return None
    rx = pd.Series(x).rank().values
    ry = pd.Series(y).rank().values
    if float(np.std(rx)) == 0 or float(np.std(ry)) == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def rank_ic(ranked_codes, outcome_map, horizon):
    """横截面 rank IC：LLM 排名与实际收益的 Spearman 秩相关。

    ranked_codes: 按 rank 升序（rank 1 在最前）的 code 列表。
    outcome_map: compute_window_outcomes 的输出。
    只取该 horizon 有完整结果（complete=True）的 code。
    """
    codes = [c for c in ranked_codes
             if c in outcome_map and outcome_map[c].get(horizon, {}).get('complete')]
    if len(codes) < 2:
        return None
    # LLM 排名分数：rank 1 给最高分（用负位置），rank 越靠前分数越高。
    # 这样「排在前面的股票收益也高」→ 正 rank IC。
    scores = {c: -i for i, c in enumerate(ranked_codes)}
    return _spearman([scores[c] for c in codes],
                     [outcome_map[c][horizon]['return'] for c in codes])


def topn_excess(top_codes, universe_codes, outcome_map, horizon):
    """Top-N 相对基础池平均收益的超额（百分比点）。"""
    def _ret(c):
        r = outcome_map.get(c, {}).get(horizon, {})
        return r.get('return') if r.get('complete') else None

    universe_rets = [r for c in universe_codes if (r := _ret(c)) is not None]
    top_rets = [r for c in top_codes if (r := _ret(c)) is not None]
    if not universe_rets or not top_rets:
        return None
    return float(np.mean(top_rets) - np.mean(universe_rets))


def evaluate_selection(batch, bars, horizons=(1, 3, 5, 10),
                       rule_ranking=None, rule_triggered=None):
    """汇总一次研究批次的固定窗口结果。

    batch: llm_selection.rank 的输出（含 universe + candidates，rank 升序）。
    rule_ranking: 规则排序的 code 列表（可选，用于 A vs B 对比）。
    rule_triggered: 规则实际触发的 code 集合（可选，用于 C：LLM 排序后仍须规则触发）。
    """
    universe = list(batch.get('universe') or [])
    candidates = batch.get('candidates') or []
    ranked = [c['code'] for c in sorted(candidates, key=lambda c: c.get('rank', 1))]
    as_of = batch.get('as_of')

    outcome_map = compute_window_outcomes(bars, universe, as_of, horizons) if as_of else {}
    top_codes = ranked[: min(5, len(ranked))]

    per_horizon = {}
    for h in horizons:
        per_horizon[h] = {
            'rank_ic': rank_ic(ranked, outcome_map, h),
            'topn_excess': topn_excess(top_codes, universe, outcome_map, h),
        }

    return {
        'research_batch_id': batch.get('research_batch_id'),
        'as_of': as_of,
        'universe_size': len(universe),
        'ranked_count': len(ranked),
        'missing_information_rate': (
            round(sum(1 for c in candidates if c.get('missing_information')) / len(candidates), 4)
            if candidates else None),
        'horizons': per_horizon,
        'rule_ranking': list(rule_ranking or []),
        'rule_triggered': sorted(rule_triggered or []),
        'note': '固定窗口收益按当时可得行情与统一价格规则计算；rank_ic 仅反映排序质量，不证明账户收益。',
    }
