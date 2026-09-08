#!/usr/bin/env python3
"""模拟账户验收（任务 C）：只读检查 + 闭环时间线，输出脱敏 JSON。

只读：不触发 LLM、不批准、不下单、不写运行数据库。
三种模式严格区分：DRY-RUN=本地模拟不下单 / SIMULATE=券商模拟账户 / REAL=真实账户。

用法：
    python scripts/run_simulate_acceptance.py              # 只读检查 + 摘要
    python scripts/run_simulate_acceptance.py --trace      # 追加闭环时间线
    python scripts/run_simulate_acceptance.py --json-only  # 只输出脱敏 JSON（供 CI）
"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

CONFIG_PATH = BASE_DIR / 'config.yaml'
DB_PATH = BASE_DIR / 'data' / 'execution.sqlite3'

# event_type → 闭环阶段
STAGE_MAP = {
    'rule_candidate': 'signal',
    'rule_rejected': 'signal',
    'plan_created': 'plan',
    'llm_requested': 'review_requested',
    'llm_completed': 'review',
    'llm_failed': 'review',
    'proposal_created': 'proposal',
    'human_decision': 'human',
    'order_intent_created': 'order_intent',
    'order_submitted': 'broker_order',
    'order_unknown': 'broker_order',
    'order_rejected': 'broker_order',
    'fill_received': 'fill',
    'trade_accounted': 'trade',
}


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _sanitize_config(cfg):
    """脱敏：只返回非敏感字段，不泄露 api_key / base_url / 授权信息。"""
    lm = cfg.get('live_manager', {}) or {}
    ha = cfg.get('trading', {}).get('live_trading', {}).get('human_approval', {}) or {}
    llm = cfg.get('llm', {}) or {}
    return {
        'trd_env': str(lm.get('trd_env', 'SIMULATE')),
        'human_approval_enabled': bool(ha.get('enabled', False)),
        'llm_enabled': bool(llm.get('enabled', False)),
        'llm_model': str(llm.get('model', '')) or None,
        'dip_watch_count': len(cfg.get('dip_buy', {}).get('watch_list', []) or []),
        'trend_breakout_enabled': bool(cfg.get('trend_breakout', {}).get('enabled', False)),
    }


def _read_rows_readonly(path, table):
    """只读 SQLite 连接读取 body 列；数据库缺失返回 None，表缺失返回 []。"""
    if not Path(path).exists():
        return None
    uri = f'file:{path}?mode=ro'
    con = sqlite3.connect(uri, uri=True)
    try:
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if not has:
            return []
        return [json.loads(r[0]) for r in con.execute(
            f'SELECT body FROM {table} ORDER BY observed_at, event_id').fetchall()]
    finally:
        con.close()


def _read_proposals_readonly(path):
    if not Path(path).exists():
        return None
    uri = f'file:{path}?mode=ro'
    con = sqlite3.connect(uri, uri=True)
    try:
        has = con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='decision_proposals'").fetchone()
        if not has:
            return []
        return [json.loads(r[0]) for r in con.execute(
            'SELECT body FROM decision_proposals').fetchall()]
    finally:
        con.close()


def trace_loop(events):
    """从事件账本提取闭环时间线（按 signal_id 分组，阶段 → 时间/关键 id）。"""
    by_signal = {}
    for e in events:
        sid = e.get('signal_id')
        if sid:
            by_signal.setdefault(sid, []).append(e)
    loops = []
    for sid, evs in sorted(by_signal.items()):
        stages = {}
        for e in evs:
            key = STAGE_MAP.get(e['event_type'])
            if not key:
                continue
            p = e.get('payload') or {}
            entry = {'at': e.get('observed_at')}
            if key == 'signal':
                entry['passed'] = p.get('passed')
            elif key == 'plan':
                entry.update(plan_id=e.get('plan_id'), version=e.get('plan_version'))
            elif key == 'review':
                entry.update(review_id=e.get('review_id'), status=p.get('status'))
            elif key == 'proposal':
                entry['proposal_id'] = e.get('proposal_id')
            elif key == 'human':
                entry['action'] = p.get('status')
            elif key == 'order_intent':
                entry['order_intent_id'] = e.get('order_intent_id')
            elif key == 'broker_order':
                entry['status'] = p.get('status')
            elif key == 'trade':
                entry['trade_id'] = e.get('trade_id')
            stages[key] = entry
        loops.append({'signal_id': sid, 'stages': stages})
    return loops


def readonly_report(config_path=None, db_path=None):
    config_path = Path(config_path) if config_path else CONFIG_PATH
    db_path = Path(db_path) if db_path else DB_PATH

    cfg = {}
    try:
        import yaml
        with open(config_path, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        return {'ok': False, 'error': f'config 读取失败: {e}'}

    report = {
        'ok': True,
        'mode': 'readonly',
        'generated_at': _utc_now(),
        'config': _sanitize_config(cfg),
        'checks': [],
        'trace': {'loops': [], 'event_types': {}},
        'llm': {},
    }

    def check(name, status, detail=''):
        report['checks'].append({'name': name, 'status': status, 'detail': detail})

    trd_env = report['config']['trd_env']
    check('env', 'ok' if trd_env in ('SIMULATE', 'REAL') else 'warn',
          f'trd_env={trd_env}（DRY-RUN=本地不下单 / SIMULATE=券商模拟账户 / REAL=真实账户）')

    events = _read_rows_readonly(db_path, 'decision_events')
    proposals = _read_proposals_readonly(db_path)

    if events is None:
        if not db_path.exists():
            check('database', 'warn', f'{db_path} 不存在（系统尚未运行，不是零持仓证据）')
        else:
            check('database', 'fail', f'{db_path} 只读打开失败（查询失败不能当零持仓）')
    else:
        check('database', 'ok', f'{db_path} 只读读取成功（事件 {len(events)} 条）')
        scopes = sorted({e.get('account_scope') for e in events if e.get('account_scope')})
        if len(scopes) > 1:
            check('account_scope', 'warn', f'事件含多个账户作用域 {scopes}（应只含当前账户）')
        elif len(scopes) == 1:
            check('account_scope', 'ok', f'账户作用域唯一: {scopes[0]}')
        else:
            check('account_scope', 'warn', '无账户作用域事件（尚未产生决策事件）')

        report['trace']['event_types'] = dict(Counter(e['event_type'] for e in events))
        report['trace']['loops'] = trace_loop(events)

        try:
            from scripts.live_trading.decision_ledger.decision_health import llm_state
            report['llm'] = llm_state(events, report['config']['llm_enabled'])
            status = report['llm']['status']
            check('llm', 'ok' if status in ('success', 'never_called') else 'warn',
                  f"status={status}, requests={report['llm']['request_count']}")
        except Exception as e:
            check('llm', 'warn', f'LLM 状态读取失败: {e}')

    if proposals is not None:
        report['proposal_count'] = len(proposals)

    try:
        import urllib.request
        with urllib.request.urlopen('http://127.0.0.1:8890/api/decision-health', timeout=3) as resp:
            health = json.loads(resp.read().decode('utf-8'))
        check('web', 'ok', '确认页 /api/decision-health 在线')
        report['web_health'] = health.get('health', {})
    except Exception as e:
        check('web', 'info', f'确认页未运行（{type(e).__name__}）；闭环验收需先启动 run_all.py')

    return report


def main():
    parser = argparse.ArgumentParser(description='quant_us 模拟账户验收（只读）')
    parser.add_argument('--json-only', action='store_true', help='只输出脱敏 JSON')
    parser.add_argument('--trace', action='store_true', help='输出详细闭环时间线')
    args = parser.parse_args()

    report = readonly_report()
    if args.json_only:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get('ok') else 1

    print('=' * 64)
    print('  quant_us 模拟账户验收（只读）')
    print('=' * 64)
    print(f"  生成时间: {report['generated_at']}")
    c = report['config']
    print(f"  交易环境: {c['trd_env']}（DRY-RUN=本地模拟 / SIMULATE=券商模拟账户 / REAL=真实账户）")
    print(f"  人工确认: {'开' if c['human_approval_enabled'] else '关'} | "
          f"LLM: {'开' if c['llm_enabled'] else '关'}")
    print('-' * 64)
    for item in report['checks']:
        icon = {'ok': '✅', 'warn': '⚠️ ', 'fail': '❌', 'info': 'ℹ️ '}.get(item['status'], '•')
        print(f"  {icon} [{item['status']}] {item['name']}: {item['detail']}")
    if report.get('llm'):
        llm = report['llm']
        print('-' * 64)
        print(f"  LLM: status={llm['status']} 请求{llm['request_count']} 成功{llm['success_count']} "
              f"失败{llm['failure_count']} 资料不足{llm['insufficient_count']}")
        if llm.get('last_failure_reason'):
            print(f"        最近失败: {llm['last_failure_reason']}")
    if args.trace and report.get('trace', {}).get('loops'):
        print('-' * 64)
        print('  闭环时间线（signal → plan → review → proposal → order → fill → trade）:')
        for loop in report['trace']['loops']:
            print(f"    signal {loop['signal_id']}:")
            for k, v in loop['stages'].items():
                print(f"      {k}: {v}")
    print('-' * 64)
    fails = sum(1 for x in report['checks'] if x['status'] == 'fail')
    warns = sum(1 for x in report['checks'] if x['status'] == 'warn')
    print(f"  共 {len(report['checks'])} 项检查：失败 {fails}，警告 {warns}")
    print('  说明：本脚本不触发 LLM、不批准、不下单、不写运行数据库。')
    print('  mock 故障注入走离线测试；真实 SIMULATE 闭环由人工点单后回跑本脚本 --trace 核对。')
    print('=' * 64)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
