"""LLM 效果报告（任务 E.6）：样本量、拒绝/资料不足/超时占比、费用覆盖率、净R、回撤、区间不确定性。

不把单次成功请求、模型信心或少量盈利交易当作有效性证明。
"""
import argparse
import json
from pathlib import Path


def _rate(n, d):
    return round(n / d, 4) if d else None


def build_effectiveness(events, comparison=None):
    """聚合 LLM 效果指标。comparison 为 comparison.replay 的输出（可选）。"""
    from scripts.live_trading.decision_ledger.funnel_report import build_funnel
    funnel = build_funnel(events)
    stages = funnel.get('stages', {})
    actual = funnel.get('actual', {})
    candidates = funnel.get('candidates', 0)
    with_proposal = stages.get('with_proposal', 0)
    closed = actual.get('closed', 0)
    total_trades = closed + actual.get('open', 0) + actual.get('partially_closed', 0)
    missing_fees = actual.get('missing_fees', 0)

    report = {
        'sample': {
            'candidates': candidates,
            'with_proposal': with_proposal,
            'with_llm_result': stages.get('with_llm_result', 0),
            'human_approved': stages.get('human_approved', 0),
            'human_rejected': stages.get('human_rejected', 0),
            'with_fill': stages.get('with_fill', 0),
            'closed_trades': closed,
            'total_trades': total_trades,
        },
        'rates': {
            'llm_failed_or_stale_rate': _rate(stages.get('llm_failed_or_stale', 0), candidates),
            'llm_insufficient_rate': _rate(stages.get('llm_insufficient', 0), candidates),
            'human_rejected_rate': _rate(stages.get('human_rejected', 0), with_proposal),
            'fee_coverage_rate': _rate(total_trades - missing_fees, total_trades),
        },
        'return': {
            'net_expectancy_r': actual.get('net_expectancy_r'),
            'r_samples': actual.get('r_samples'),
            'net_pnl': actual.get('net_pnl'),
            'missing_fees': missing_fees,
        },
        'llm_cost': funnel.get('llm_cost', {}),
        'linkage': funnel.get('linkage', {}),
    }

    if isinstance(comparison, dict):
        layers = comparison.get('layers', {})
        report['layers'] = {
            k: {'final_equity': v.get('final_equity'),
                'max_drawdown': v.get('max_drawdown'),
                'final_equity_less_llm_cost': v.get('final_equity_less_llm_cost')}
            for k, v in layers.items() if isinstance(v, dict)
        }
        report['assumptions'] = comparison.get('assumptions', [])
        report['data_gaps'] = comparison.get('data_gaps', [])

    report['conclusion'] = _conclusion(report)
    return report


def _conclusion(report):
    r_samples = report['return'].get('r_samples') or 0
    candidates = report['sample']['candidates']
    if candidates < 30 or r_samples < 10:
        return ('样本不足，无法判断 LLM 是否提高收益。需锁定实验版本、历史可得行情与资金约束的'
                '样本外 A/B/C/D 重放；不要以单次成功请求、模型信心或少量盈利交易作为有效性证明。')
    return '样本量满足最低门槛，但仍需在锁定的样本外测试期重放后，才可下有效性结论。'


def main():
    parser = argparse.ArgumentParser(description='LLM 效果报告')
    parser.add_argument('--events', required=True, help='events-v1.jsonl 导出')
    parser.add_argument('--comparison', default=None, help='comparison.replay 输出 JSON（可选）')
    args = parser.parse_args()

    events = [json.loads(line) for line in Path(args.events).read_text(encoding='utf-8').splitlines()
              if line.strip()]
    comparison = None
    if args.comparison:
        comparison = json.loads(Path(args.comparison).read_text(encoding='utf-8'))
    print(json.dumps(build_effectiveness(events, comparison), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
