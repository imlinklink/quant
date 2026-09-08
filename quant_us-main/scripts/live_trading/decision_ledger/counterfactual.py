"""反事实记录（阶段 H3）：为每个候选冻结规则计划、LLM 结果、人工动作、决策时行情、统一执行假设下的后续结果。

只记录，不改交易状态。用于判断 defer/oppose 是否真的避开了亏损，而非只看被买入的样本。
"""
from collections import defaultdict

from scripts.live_trading.decision_ledger.event_store import utc
from scripts.live_trading.decision_ledger.selection_outcomes import compute_window_outcomes


def freeze(proposal, review, human_action, quote, bars, horizons=(1, 3, 5, 10), now=None):
    """冻结一个候选的反事实记录。

    proposal: 规则计划 dict（含 stock_code/entry_mode/price/initial_stop/target）。
    review: LLM 结果 dict（recommendation/reasons/...），可为 None。
    human_action: 'approved' / 'rejected' / 'unhandled'。
    quote: 决策时行情 dict（price/observed_at）。
    bars: 历史行情 DataFrame（含 code/date/open/high/low/close，date 为 UTC）。
    """
    code = proposal.get('stock_code')
    observed = quote.get('observed_at') if quote else None
    outcomes = compute_window_outcomes(bars, [code], observed, horizons) if (code and observed) else {}
    return {
        'stock_code': code,
        'entry_mode': proposal.get('entry_mode'),
        'plan': {k: proposal.get(k) for k in ('price', 'initial_stop', 'target')
                 if proposal.get(k) is not None},
        'recommendation': (review or {}).get('recommendation'),
        'reasons': list((review or {}).get('reasons') or []),
        'human_action': human_action,
        'quote': quote or {},
        'outcomes': outcomes.get(code),
        'frozen_at': utc(now),
    }


def compare(records, horizon=5):
    """按 human_action × recommendation 分组统计反事实后续收益。

    返回 {group: {'n','mean_return','win_rate'}}，只含该 horizon 有完整结果的记录。
    """
    groups = defaultdict(list)
    for r in records:
        out = (r.get('outcomes') or {}).get(horizon) or {}
        if not out.get('complete'):
            continue
        action = r.get('human_action') or 'unhandled'
        rec = r.get('recommendation') or 'missing'
        groups[f'{action}|{rec}'].append(out['return'])

    result = {}
    for g, rets in sorted(groups.items()):
        result[g] = {
            'n': len(rets),
            'mean_return': round(sum(rets) / len(rets), 4),
            'win_rate': round(sum(1 for x in rets if x > 0) / len(rets), 4),
        }
    return result
