#!/usr/bin/env python3
"""每日冻结基础池 + LLM 选股影子排序（首个迭代真实接入）。

只读：不产生 proposal / approval / order，只保存版本化研究批次。
用法：
    python scripts/live_trading/run_daily_selection.py --dry-run  # 只拉行情生成 packet，不调 LLM
    python scripts/live_trading/run_daily_selection.py            # 调 LLM 生成研究排名并落批次
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger('run_daily_selection')


def build_universe(config):
    """冻结基础池：dip_buy.watch_list ∪ trend_breakout.watch_list，去重、规范 US. 前缀。"""
    codes = []
    for section in ('dip_buy', 'trend_breakout'):
        for c in (config.get(section, {}).get('watch_list') or []):
            c = str(c).strip().upper()
            norm = c if c.startswith('US.') else f'US.{c}'
            if norm.startswith('US.') and norm not in codes and len(norm) > 3:
                codes.append(norm)
    return codes


def build_packet_from_bars(code, bars, *, name=None, sector=None, risk_group=None, now=None):
    """从日 K 生成 evidence_packet。行情指标由程序计算，LLM 只解释。数据不足返回 None。"""
    import numpy as np

    from scripts.live_trading.decision_ledger.event_store import utc
    from scripts.live_trading.decision_ledger.evidence_packet import build_evidence_packet

    if bars is None or len(bars) < 2:
        return None
    closes = bars['close'].values.astype(float)
    price = float(closes[-1])
    if not np.isfinite(price) or price <= 0:
        return None

    def ret(n):
        if len(closes) > n and closes[-1 - n] > 0:
            return float(closes[-1] / closes[-1 - n] - 1.0)
        return None

    high = bars['high'].values.astype(float)
    low = bars['low'].values.astype(float)
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - closes[:-1]), np.abs(low[1:] - closes[:-1])))
    atr = float(np.mean(tr[-14:])) if len(tr) >= 14 else None
    ma20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else None
    ma50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else None

    trend = None
    if ma50 is not None:
        trend = 'above_ma50' if price > ma50 else 'below_ma50'

    quote = {
        'price': price,
        'observed_at': utc(now),
        'ret_1d': ret(1),
        'ret_5d': ret(5),
        'ret_20d': ret(20),
        'atr': atr,
        'ma20': ma20,
        'ma50': ma50,
        'trend': trend,
    }

    # 程序行情快照证据：给 LLM 一个可引用的 evidence_id（events[].evidence_id），
    # 避免它把 packet_id（evidence_packet_ 前缀）误当证据引用。
    from mutifactor.llm.trade_review import evidence

    def _fmt(v):
        return 'N/A' if v is None else f'{v:.4f}' if isinstance(v, float) else str(v)

    summary = (f'程序行情快照：最新价 {price:.2f}；1日收益 {_fmt(ret(1))}；'
               f'5日收益 {_fmt(ret(5))}；20日收益 {_fmt(ret(20))}；'
               f'ATR {_fmt(atr)}；趋势 {trend or "N/A"}')
    snapshot_evidence = evidence(summary, 'internal:quote-snapshot', utc(now), kind='rule')

    return build_evidence_packet(code, name=name, sector=sector, risk_group=risk_group,
                                 quote=quote, events=[snapshot_evidence], now=now)


def run_selection(config, advisor, fetcher, now=None, dry_run=False):
    """执行每日选股：冻结基础池 → 拉行情 → 生成 packet → LLM 排名 → 返回研究批次。"""
    from scripts.live_trading.llm_selection import rank
    from scripts.live_trading.llm_suggestions.store import save_research_batch

    now = now if now is not None else time.time()
    universe = build_universe(config)
    if not universe:
        return {'error': 'empty_universe', 'universe': []}

    end = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    start = (datetime.now(timezone.utc) - timedelta(days=90)).strftime('%Y-%m-%d')
    bars_map = fetcher.fetch_multiple_stocks(universe, start, end) if fetcher else {}

    risk_group = (config.get('risk_budget', {}).get('code_groups') or {})
    packets = []
    for code in universe:
        p = build_packet_from_bars(code, bars_map.get(code), risk_group=risk_group.get(code), now=now)
        if p is not None:
            packets.append(p)

    if dry_run:
        return {'dry_run': True, 'universe': universe, 'packet_count': len(packets),
                'codes_with_data': [p['code'] for p in packets]}

    batch = rank(advisor, universe, packets, now=now)
    save_research_batch(batch)
    return batch


def main():
    parser = argparse.ArgumentParser(description='每日冻结基础池 + LLM 选股影子排序')
    parser.add_argument('--dry-run', action='store_true', help='只拉行情生成 packet，不调 LLM')
    parser.add_argument('--config', default=str(BASE_DIR / 'config.yaml'))
    args = parser.parse_args()

    import yaml
    with open(args.config, encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}

    from mutifactor.llm import LLMAdvisor
    advisor = LLMAdvisor(config.get('llm', {}))

    from mutifactor.data.us_fetcher import FutuUSDataFetcher
    futu_cfg = config.get('futu', {})
    fetcher = FutuUSDataFetcher(host=futu_cfg.get('host', '127.0.0.1'),
                                port=int(futu_cfg.get('port', 11111)))
    try:
        if not fetcher.connect():
            print('无法连接富途 OpenD，请先启动并登录行情权限')
            return 1
        result = run_selection(config, advisor, fetcher, dry_run=args.dry_run)
    finally:
        fetcher.disconnect()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
