#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日对账脚本（SIMULATE 验证期用，只读）

核对三件事：
  1) 本地 YAML 持仓 vs 券商（SIMULATE）持仓：代码/数量差异
  2) 资金账本一致性（capital/used_capital 与持仓成本是否吻合）
  3) 今日评估流水与简报新鲜度

用法：
    python3 scripts/live_trading/daily_reconcile.py
    python3 scripts/live_trading/daily_reconcile.py --env SIMULATE

退出码：0=一致 / 1=有差异（便于 cron/看护告警）
"""
import argparse
import os
import sys
import json
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import yaml


def _env_config():
    cfg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'config.yaml',
    )
    cfg = yaml.safe_load(open(cfg_path, encoding='utf-8')) or {}
    env = str((cfg.get('trading') or {}).get('env', 'SIMULATE')).upper()
    return cfg, env


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--env', choices=['SIMULATE', 'REAL'], default=None)
    args = ap.parse_args()
    cfg, cfg_env = _env_config()
    env = args.env or cfg_env

    from mutifactor.infra.yaml_storage import yaml_storage, TradingEnv
    trading_env = TradingEnv.REAL if env == 'REAL' else TradingEnv.SIMULATE
    issues = []

    # 1) 本地持仓 vs 券商持仓
    local_state = yaml_storage.load_trading_state(trading_env) or {}
    local_pos = local_state.get('positions', {}) or {}
    broker_pos = {}
    try:
        from mutifactor.trading import FutuTrader
        from futu import TrdEnv
        trd_env = TrdEnv.REAL if env == 'REAL' else TrdEnv.SIMULATE
        futu_cfg = (cfg.get('trading') or {}).get('futu', {})
        trader = FutuTrader(
            host=futu_cfg.get('host', '127.0.0.1'),
            port=futu_cfg.get('port', 11111),
            env=trd_env,
        )
        if trader.connect():
            for p in trader.get_positions():
                broker_pos[p['stock_code']] = int(p.get('quantity') or 0)
            trader.disconnect()
        else:
            issues.append('无法连接券商（OpenD/SIMULATE 未就绪）')
    except Exception as e:
        issues.append(f'券商持仓查询失败: {e}')

    for code, pos in local_pos.items():
        bq = broker_pos.get(code, 0)
        lq = int(pos.get('quantity') or 0)
        if lq != bq:
            issues.append(f'数量不一致 {code}: 本地 {lq} / 券商 {bq}')
    for code in broker_pos:
        if code not in local_pos:
            issues.append(f'券商有、本地无: {code} ({broker_pos[code]} 股)')

    # 2) 资金口径：used_capital 应约等于非 manual 持仓成本
    used = float(local_state.get('used_capital') or 0)
    manual_cost = 0.0
    calc_used = 0.0
    for pos in local_pos.values():
        q = float(pos.get('quantity') or 0)
        c = float(pos.get('cost_price') or 0)
        if pos.get('manual'):
            manual_cost += q * c
        else:
            calc_used += q * c
    if abs(used - calc_used) > 1.0 and local_pos:
        issues.append(f'资金口径不一致: used_capital={used:.2f} vs 策略持仓成本 {calc_used:.2f}'
                      f'（手动仓 {manual_cost:.2f} 不计）')

    # 3) 评估流水与简报
    today = date.today().isoformat()
    try:
        ledger_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'data', 'decision_ledger', 'signals.jsonl',
        )
        n = 0
        if os.path.exists(ledger_path):
            with open(ledger_path, encoding='utf-8') as f:
                for line in f:
                    if today in line:
                        n += 1
        print(f'📈 今日评估事件: {n} 条')
    except Exception as e:
        issues.append(f'评估流水读取失败: {e}')
    try:
        from scripts.live_trading import market_brief
        brief = market_brief.load_brief()
        if brief.get('date') != today:
            issues.append(f'市场简报不是今日生成（date={brief.get("date")}）')
        else:
            print(f'📋 今日简报: {brief.get("risk_level")} / '
                  f'buy_frequency={brief.get("buy_frequency")}')
    except Exception as e:
        issues.append(f'简报读取失败: {e}')

    print(f'🏦 env={env} 本地持仓 {len(local_pos)} 只，券商 {len(broker_pos)} 只，'
          f'used_capital={used:,.2f}')
    if issues:
        print('\n❌ 发现差异:')
        for i in issues:
            print('  -', i)
        return 1
    print('\n✅ 每日对账通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
