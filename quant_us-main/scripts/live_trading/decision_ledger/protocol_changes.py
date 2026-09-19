"""协议变更候选（设计 §6.5 / §10）。

Review 的输出只能走到这里：一个 append-only 的 `protocol_change_candidate` 事件。
「人工确认后生成新版本」在实现上是**批准事件 + 由人改配置并新建版本**，本模块不提供
任何写配置、改权限、切换等级的入口 —— 这是 §6.5「不得直接修改配置」与 §13 P2
「模型提出的改进不能自动进入生产」的构造性保证。

事件：
  `protocol_change_candidate`  模型提出的候选（幂等键含 packet 与变量）
  `protocol_version_approved`  人工批准（记录批准人；仍不改配置）
  `protocol_version_rejected`  人工拒绝（记录理由）
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from scripts.live_trading.decision_ledger.event_store import EventStore, stable_id


def _candidate_key(candidate: Dict[str, Any]) -> str:
    """候选身份 = 冻结包 + 变量 + 目标值。同一包重复提出同一个改动是幂等的。"""
    return stable_id('protocol_candidate', candidate.get('packet_id') or '',
                     str(candidate.get('variable') or ''),
                     repr(candidate.get('to_value')))


def record_candidate(registry, candidate: Dict[str, Any]) -> str:
    """落一条候选事件。返回候选 id。**只写事件，不触碰任何配置。**"""
    if not candidate.get('variable'):
        raise ValueError('CANDIDATE_WITHOUT_VARIABLE')
    if not candidate.get('validation_plan'):
        # 没有验证计划的候选不该进账本：它无法被检验，也就无法被证伪
        raise ValueError('CANDIDATE_WITHOUT_VALIDATION_PLAN')
    candidate_id = _candidate_key(candidate)
    events = EventStore(registry)
    events.record('protocol_change_candidate', candidate_id,
                  {'candidate_id': candidate_id, 'status': 'proposed', **candidate})
    return candidate_id


def _decide(registry, candidate_id: str, event_type: str, *, approver: str,
            note: str = '', target_version: str = '') -> None:
    if not str(approver or '').strip():
        # 批准人不能为空：匿名批准等于没有人工确认
        raise ValueError('APPROVER_REQUIRED')
    events = EventStore(registry)
    events.record(event_type, candidate_id,
                  {'candidate_id': candidate_id, 'approver': approver, 'note': note,
                   'target_protocol_version': target_version})


def approve(registry, candidate_id: str, *, approver: str, target_version: str,
            note: str = '') -> None:
    """人工批准。**仍然不改配置** —— 新版协议由人另行创建。

    `target_version` 必填且必须**不同于该候选的基线版本**：§6.5 说「人确认后生成新版本」，
    批准而不指向一个新版本，事后就无法判断配置到底有没有改、改成了哪一版 ——
    "已批准"会变成一张无法核对空头支票。
    """
    if not str(target_version or '').strip():
        raise ValueError('TARGET_VERSION_REQUIRED:批准必须指向一个新版本')
    record = _find(registry, candidate_id)
    if record is None:
        raise ValueError(f'CANDIDATE_NOT_FOUND:{candidate_id}')
    base = str(record.get('base_protocol_version') or '')
    if base and str(target_version) == base:
        raise ValueError(f'TARGET_VERSION_NOT_NEW:{target_version}'
                         '（与候选的基线版本相同，等于没生成新版本）')
    _decide(registry, candidate_id, 'protocol_version_approved', approver=approver,
            note=note, target_version=str(target_version))


def reject(registry, candidate_id: str, *, approver: str, note: str) -> None:
    """人工拒绝。**必须给出理由** —— 无理由的拒绝让同一个假设会被反复提出。"""
    if not str(note or '').strip():
        raise ValueError('REJECT_REASON_REQUIRED:拒绝必须给出理由')
    _decide(registry, candidate_id, 'protocol_version_rejected', approver=approver,
            note=note)


def _find(registry, candidate_id: str) -> Optional[dict]:
    for row in candidates(registry):
        if row.get('candidate_id') == candidate_id:
            return row
    return None


def candidates(registry) -> list:
    """全部候选及其最新人工裁决（用于报告与审计）。"""
    events = EventStore(registry).events()
    proposed: Dict[str, dict] = {}
    decisions: Dict[str, dict] = {}
    for event in events:
        payload = event.get('payload') or {}
        if event.get('event_type') == 'protocol_change_candidate':
            proposed[payload['candidate_id']] = payload
        elif event.get('event_type') in ('protocol_version_approved',
                                        'protocol_version_rejected'):
            decisions[payload['candidate_id']] = {
                'decision': event['event_type'].rsplit('_', 1)[-1],
                'approver': payload.get('approver'), 'note': payload.get('note', ''),
                # 必须带上目标版本：`adjudication` 靠它判断"批准了但是否真的改了"，
                # 漏掉它会让「已兑现」恒为 False。
                'target_protocol_version': payload.get('target_protocol_version', ''),
                'at': event.get('observed_at')}
    return [{**payload, 'decision': decisions.get(cid)}
            for cid, payload in sorted(proposed.items())]


def pending(registry) -> list:
    """尚未人工裁决的候选。**它们不会被自动应用**，这里只是把它们列出来。"""
    return [c for c in candidates(registry) if not c.get('decision')]


def adjudication(candidate: Dict[str, Any], *, current_version: str = '') -> Dict[str, Any]:
    """候选的裁决状态，以及**是否已兑现**。

    「已批准」与「已生效」是两回事：本系统不改配置，批准之后需要人真的去改。
    不把这个差距显式报出来，「已批准」就会被读成「已经改了」—— 于是协议迭代看起来
    在推进，实际一次都没落地。
    """
    decision = candidate.get('decision') or {}
    status = decision.get('decision') or 'pending'
    target = str(decision.get('target_protocol_version') or '')
    carried = None
    if status == 'approved':
        carried = bool(current_version) and str(current_version) == target
    return {'candidate_id': candidate.get('candidate_id'), 'status': status,
            'approver': decision.get('approver'), 'note': decision.get('note', ''),
            'at': decision.get('at'), 'target_protocol_version': target,
            'base_protocol_version': candidate.get('base_protocol_version'),
            # True=配置已推进到批准的目标版本；False=批准了但没改；None=不适用
            'carried_out': carried}


def report(registry, *, current_version: str = '') -> Dict[str, Any]:
    """全部候选 + 裁决 + 兑现情况；供 CLI 与审计使用。**只读。**"""
    rows = []
    for candidate in candidates(registry):
        rows.append({**candidate,
                     'adjudication': adjudication(candidate,
                                                  current_version=current_version)})
    return {'current_protocol_version': current_version or '(未声明)',
            'candidates': rows,
            'pending': [r for r in rows if r['adjudication']['status'] == 'pending'],
            'approved_not_carried_out': [
                r for r in rows if r['adjudication']['status'] == 'approved'
                and r['adjudication']['carried_out'] is False],
            'note': '本系统不会自动应用任何协议变更；批准只表示人同意去创建新版本'}


def render(report_data: Dict[str, Any]) -> str:
    lines = [f"# 协议变更候选（当前协议版本：{report_data['current_protocol_version']}）",
             '', f"— {report_data['note']}", '']
    if not report_data['candidates']:
        lines.append('（账本里还没有任何候选）')
        return '\n'.join(lines)
    if report_data['pending']:
        lines.append(f"## 待裁决（{len(report_data['pending'])}）")
        for row in report_data['pending']:
            lines.extend(_render_candidate(row))
    if report_data['approved_not_carried_out']:
        lines.append(f"## ⚠️ 已批准但**未兑现**（{len(report_data['approved_not_carried_out'])}）")
        lines.append('（批准只表示同意创建新版本；配置里的协议版本还没改到目标版本）')
        for row in report_data['approved_not_carried_out']:
            lines.extend(_render_candidate(row))
    done = [r for r in report_data['candidates']
            if r['adjudication']['status'] in ('approved', 'rejected')
            and r not in report_data['approved_not_carried_out']]
    if done:
        lines.append(f"## 已裁决（{len(done)}）")
        for row in done:
            info = row['adjudication']
            mark = '✅' if info['status'] == 'approved' else '❌'
            lines.append(f"- {mark} {row['variable']} → {row['to_value']} "
                         f"（{info['status']} by {info['approver']}"
                         + (f"，目标版本 {info['target_protocol_version']}"
                            if info['target_protocol_version'] else '')
                         + f"）{info['note']}")
    return '\n'.join(lines)


def _render_candidate(row: Dict[str, Any]) -> list:
    info = row['adjudication']
    lines = [f"- `{row['candidate_id']}` **{row['variable']}**："
             f"{row.get('from_value')} → {row.get('to_value')}（{row.get('direction')}）",
             f"  - 基线协议版本：{row.get('base_protocol_version') or '未声明'}；"
             f"预期改善：{(row.get('expected_improvement') or {}).get('metric')}"
             f"（{(row.get('expected_improvement') or {}).get('direction')}）",
             f"  - 可能恶化：{[r.get('metric') for r in row.get('possible_regression') or []]}",
             f"  - 验证计划：{row.get('validation_plan')}"]
    for pattern in row.get('failure_patterns') or []:
        lines.append(f"  - 失败模式（样本组 {pattern.get('sample_group')}）："
                     f"{pattern.get('pattern')}")
    if info['status'] == 'pending':
        lines.append(f"  - 裁决：`approve {row['candidate_id']} "
                     f"--approver <人> --target-version <新版本>`")
    elif info['status'] == 'approved':
        # 「已批准但未兑现」那一节存在的意义就是说明"批准了哪一版、配置还停在哪一版"，
        # 不写出目标版本，这一节就没有信息量。
        lines.append(f"  - 已批准：{info['approver']} 于 {info['at']} 批准创建版本 "
                     f"**{info['target_protocol_version']}**"
                     f"（配置当前仍是 {info['base_protocol_version'] or '未声明'}）")
    else:
        lines.append(f"  - 已拒绝：{info['approver']} —— {info['note']}")
    return lines


def main(argv=None) -> int:
    import argparse
    import json as _json
    from pathlib import Path as _Path
    import yaml
    from scripts.live_trading.position_registry import registry_for
    parser = argparse.ArgumentParser(
        description='协议变更候选的人工裁决入口（只写事件，**不改配置**）')
    parser.add_argument('action', nargs='?', default='list',
                        choices=('list', 'approve', 'reject'))
    parser.add_argument('candidate_id', nargs='?')
    parser.add_argument('--approver', default='', help='裁决人（必填）')
    parser.add_argument('--note', default='', help='备注；拒绝时必填')
    parser.add_argument('--target-version', default='',
                        help='批准时必填：将要创建的新协议版本')
    parser.add_argument('--config', default=None)
    parser.add_argument('--registry', default=None)
    parser.add_argument('--scope', default=None,
                        help='账本 namespace；缺省取配置的 account_scope')
    parser.add_argument('--output', default=None, help='list 时写出 JSON')
    args = parser.parse_args(argv)
    root = _Path(__file__).resolve().parents[3]
    config = yaml.safe_load(_Path(args.config or root / 'config.yaml').read_text()) or {}
    current = str(((config.get('llm_decision') or {}).get('protocol_review') or {}
                   ).get('protocol_version') or '')
    registry = registry_for(config, args.registry, args.scope)
    if args.action == 'approve':
        if not args.candidate_id:
            raise SystemExit('approve 需要 candidate_id')
        approve(registry, args.candidate_id, approver=args.approver,
                target_version=args.target_version, note=args.note)
    elif args.action == 'reject':
        if not args.candidate_id:
            raise SystemExit('reject 需要 candidate_id')
        reject(registry, args.candidate_id, approver=args.approver, note=args.note)
    data = report(registry, current_version=current)
    print(render(data))
    if args.output:
        _Path(args.output).write_text(
            _json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
