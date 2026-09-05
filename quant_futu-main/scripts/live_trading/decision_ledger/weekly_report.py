#!/usr/bin/env python3
"""评估账本周报（方向1：评估闭环）。

默认读本系统流水账；也可用 --ledger 合并多个账本。聚合出：各系统信号量、LLM 判定分布、人工决定、执行结果，
以及（若已接入平仓事件）胜率/盈亏统计。

用法：
    python scripts/live_trading/decision_ledger/weekly_report.py            # 近7天
    python scripts/live_trading/decision_ledger/weekly_report.py --days 30
    python scripts/live_trading/decision_ledger/weekly_report.py \\
        --ledger data/decision_ledger/signals.jsonl \\
        --ledger ../quant_futu-main/data/decision_ledger/signals.jsonl   # 跨系统合并
    python scripts/live_trading/decision_ledger/weekly_report.py --no-save
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.live_trading.decision_ledger import ledger


def _pct(n, d):
    return f'{n / d * 100:.1f}%' if d else '-'


def _load_paths(paths, days):
    """从指定 JSONL 路径读取事件（按时间过滤）。"""
    events = []
    cutoff = None
    if days is not None:
        cutoff = datetime.now() - timedelta(days=days)
    for p in paths:
        path = Path(p)
        if not path.exists():
            continue
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if cutoff is not None:
                    try:
                        if datetime.fromisoformat(ev.get('ts', '')) < cutoff:
                            continue
                    except Exception:
                        continue
                events.append(ev)
    return events


def build_report(events, days):
    lines = []
    ap = lines.append

    total = len(events)
    by_system = Counter(e.get('system') for e in events)
    by_type = Counter(e.get('event_type') for e in events)
    proposals = [e for e in events if e.get('event_type') == 'proposal_created']

    ap(f'# 交易评估周报（近 {days:.0f} 天）')
    ap('')
    ap(f'- 生成时间: {datetime.now().isoformat(timespec="seconds")}')
    ap(f'- 流水事件总数: {total}')
    ap(f'- 按系统: ' + ', '.join(f'{k}={v}' for k, v in by_system.items()) or '无')
    ap('')

    ap('## 1. 信号量（推送确认页）')
    if proposals:
        mk = Counter(
            ('HK' if e.get('stock_code', '').startswith('HK.') else 'US')
            for e in proposals
        )
        ap('- ' + ', '.join(f'{k}: {v}' for k, v in mk.items()))
    else:
        ap('- 无（确认模式未启用或暂无信号）')
    ap('')

    ap('## 2. LLM 判定分布')
    llm_events = [e for e in proposals if e.get('llm')]
    if llm_events:
        def _vlabel(v):
            return '无判定' if v is None else str(v)

        verdicts = Counter(_vlabel(e['llm'].get('verdict', '未知')) for e in llm_events)
        confs = [e['llm'].get('confidence') for e in llm_events if isinstance(e['llm'].get('confidence'), (int, float))]
        avg_conf = sum(confs) / len(confs) if confs else None
        ap('- ' + ', '.join(f'{k}: {v}' for k, v in verdicts.items()))
        ap(f'- 平均置信度: {avg_conf:.2f}' if avg_conf is not None else '- 无置信度记录')
    else:
        ap('- 无 LLM 判定记录（llm 未启用/失败）')
    ap('')

    ap('## 3. 人工决定')
    decisions = [e for e in events if e.get('event_type') == 'human_decision']
    if decisions:
        acts = Counter(e.get('action') for e in decisions)
        ap('- ' + ', '.join(f'{k}: {v}' for k, v in acts.items()))
    else:
        ap('- 无记录')
    ap('')

    ap('## 4. 执行状态')
    execs = [e for e in events if e.get('event_type') == 'execution_status']
    if execs:
        st = Counter(e.get('status') for e in execs)
        ap('- ' + ', '.join(f'{k}: {v}' for k, v in st.items()))
    else:
        ap('- 无记录')
    ap('')

    ap('## 5. 开/平仓与盈亏')
    opened = [e for e in events if e.get('event_type') == 'position_opened']
    ap(f'- 开仓事件: {len(opened)} 笔')
    closes = [e for e in events if e.get('event_type') == 'position_closed' and isinstance(e.get('pnl_pct'), (int, float))]
    if closes:
        wins = sum(1 for e in closes if e['pnl_pct'] > 0)
        avg = sum(e['pnl_pct'] for e in closes) / len(closes)
        ap(f'- 平仓笔数: {len(closes)}，胜率: {_pct(wins, len(closes))}，平均盈亏: {avg * 100:+.2f}%')
        by_mkt = {}
        for e in closes:
            mkt = 'HK' if str(e.get('stock_code', '')).startswith('HK.') else 'US'
            by_mkt.setdefault(mkt, []).append(e['pnl_pct'])
        for mkt, pnls in by_mkt.items():
            w = sum(1 for p in pnls if p > 0)
            ap(f'  - {mkt}: {len(pnls)} 笔，胜率 {_pct(w, len(pnls))}，平均 {sum(pnls) / len(pnls) * 100:+.2f}%')
    else:
        ap('- 暂无平仓事件（后续把持仓平仓钩子接入后自动统计）')
    ap('')

    ap('## 6. 归因分析（谁的建议在赚钱）')
    # proposal_id -> proposal 事件 / 人工决定
    props = {e.get('proposal_id'): e for e in events if e.get('event_type') == 'proposal_created'}
    decisions = {}
    for e in events:
        if e.get('event_type') == 'human_decision' and e.get('proposal_id'):
            decisions.setdefault(e['proposal_id'], e.get('action'))

    def _group_stats(pnls):
        if not pnls:
            return '0 笔'
        w = sum(1 for p in pnls if p > 0)
        return f'{len(pnls)} 笔，胜率 {_pct(w, len(pnls))}，平均 {sum(pnls) / len(pnls) * 100:+.2f}%'

    # 已平仓中能关联到提案的
    by_verdict = {'allow': [], 'watch': [], 'delay/block': [], '无判定': [], '无提案/自动': []}
    by_decision = {'approve': [], '自动/无确认记录': []}
    for c in closes:
        pid = c.get('proposal_id')
        p = props.get(pid) if pid else None
        if p is None:
            by_verdict['无提案/自动'].append(c['pnl_pct'])
            by_decision['自动/无确认记录'].append(c['pnl_pct'])
            continue
        llm = p.get('llm') or {}
        verdict = llm.get('verdict')
        if verdict == 'allow':
            by_verdict['allow'].append(c['pnl_pct'])
        elif verdict == 'watch':
            by_verdict['watch'].append(c['pnl_pct'])
        elif verdict in ('delay', 'block'):
            by_verdict['delay/block'].append(c['pnl_pct'])
        else:
            by_verdict['无判定'].append(c['pnl_pct'])

        act = decisions.get(pid)
        if act == 'approve':
            by_decision['approve'].append(c['pnl_pct'])
        else:
            by_decision['自动/无确认记录'].append(c['pnl_pct'])

    ap('- 按大模型判定（已平仓）:')
    for k, v in by_verdict.items():
        ap(f'  - {k}: {_group_stats(v)}')
    ap('- 按人工决定（已平仓）:')
    for k, v in by_decision.items():
        ap(f'  - {k}: {_group_stats(v)}')

    # 还没出结果的确认单数量（供后续周报追踪）
    approved_ids = {pid for pid, act in decisions.items() if act == 'approve'}
    closed_prop_ids = {c.get('proposal_id') for c in closes if c.get('proposal_id')}
    pending = [pid for pid in approved_ids if pid not in closed_prop_ids]
    rejected = sum(1 for act in decisions.values() if act == 'reject')
    ap(f'- 待出结果（已点单未平仓）: {len(pending)} 笔；已拒绝: {rejected} 笔'
       f'（拒绝票的事后走势需另外回看行情）')
    ap('')

    ap('## 7. 反事实对照（含被拒绝的票，事后走势）')
    outs = [e for e in events if e.get('event_type') == 'candidate_outcome']
    if outs:
        # 每个提案选最接近 5 日的结果（5 > 3 > 1 > 10）
        rank = {1: 2, 3: 3, 5: 4, 10: 1}
        best_outcome = {}
        for e in outs:
            pid = e.get('proposal_id')
            try:
                off = int(e.get('offset_days'))
            except (TypeError, ValueError):
                continue
            if not pid or e.get('ret_pct') is None:
                continue
            r = rank.get(off, 0)
            if pid not in best_outcome or r > best_outcome[pid][0]:
                best_outcome[pid] = (r, float(e['ret_pct']))

        groups = {
            'decision': {'approve': [], 'reject': [], '未处理': []},
            'llm': {'allow': [], 'watch': [], 'delay/block': [], '无判定': []},
        }
        for pid, (_, ret) in best_outcome.items():
            p = props.get(pid)
            act = decisions.get(pid, '未处理')
            key = act if act in groups['decision'] else '未处理'
            groups['decision'][key].append(ret)
            if p is None:
                groups['llm']['无判定'].append(ret)
                continue
            llm = p.get('llm') or {}
            v = llm.get('verdict')
            if v == 'allow':
                groups['llm']['allow'].append(ret)
            elif v == 'watch':
                groups['llm']['watch'].append(ret)
            elif v in ('delay', 'block'):
                groups['llm']['delay/block'].append(ret)
            else:
                groups['llm']['无判定'].append(ret)

        ap('- 按人工决定（事后 N 日收益）:')
        for k, v in groups['decision'].items():
            ap(f'  - {k}: {_group_stats(v)}')
        ap('- 按大模型判定（事后 N 日收益）:')
        for k, v in groups['llm'].items():
            ap(f'  - {k}: {_group_stats(v)}')
    else:
        ap('- 暂无回填数据。每天收盘后运行：'
           'python scripts/live_trading/decision_ledger/backfill_outcomes.py')
    ap('')

    ap('## 8. 市场状态档位分布')
    briefs = [e for e in events if e.get('event_type') == 'market_brief']
    if briefs:
        risks = Counter(e.get('risk_level') for e in briefs)
        freqs = Counter(e.get('buy_frequency') for e in briefs)
        ap('- 风险档位: ' + ', '.join(f'{k}: {v}' for k, v in risks.items()))
        ap('- 买入频率: ' + ', '.join(f'{k}: {v}' for k, v in freqs.items()))
    else:
        ap('- 暂无记录（每天盘前运行 run_market_brief.py 后出现）')
    ap('')

    ap('## 9. 最近提案样例')
    for e in proposals[-5:]:
        code = e.get('stock_code', '?')
        verdict = (e.get('llm') or {}).get('verdict', '无')
        ap(f'- {e.get("ts")} {code} | LLM={verdict} | 理由: {str(e.get("reason", ""))[:80]}')

    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='评估账本周报')
    parser.add_argument('--days', type=float, default=7)
    parser.add_argument('--ledger', action='append', default=None,
                        help='账本 JSONL 路径（可多次指定；不指定则读本系统账本）')
    parser.add_argument('--no-save', action='store_true', help='只打印不保存文件')
    args = parser.parse_args()

    if args.ledger:
        paths = args.ledger
        events = _load_paths(paths, args.days)
    else:
        paths = [str(ledger.ledger_path())]
        events = ledger.load_events(days=args.days)
    text = build_report(events, args.days)
    print('账本来源: ' + '; '.join(paths))
    print('=' * 60)
    print(text)

    if not args.no_save:
        out_dir = ledger._shared_dir() / 'reports'
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f'weekly_{datetime.now().strftime("%Y%m%d")}.md'
        out.write_text(text, encoding='utf-8')
        print(f'\n💾 已保存: {out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
