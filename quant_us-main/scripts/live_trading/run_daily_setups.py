#!/usr/bin/env python3
"""收盘后生成中期买入 setup shadow；不创建 proposal，不调用 LLM，不下单。"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from scripts.live_trading.position_registry import PositionRegistry
from scripts.live_trading.setup_scanner import SetupScanner


def run(config, fetcher, as_of=None):
    as_of = as_of or datetime.now(timezone.utc).isoformat()
    cfg = config.get('buy_strategy_v2', {})
    codes = list(config.get('dip_buy', {}).get('watch_list') or [])
    group_map = config.get('risk_budget', {}).get('code_groups', {})
    proxies = config.get('pullback', {}).get('sector_proxies', {})
    sector_map = {code: proxies.get(group_map.get(code), 'US.SPY') for code in codes}
    all_codes = list(dict.fromkeys(codes + list(sector_map.values()) + ['US.SPY']))
    end = str(as_of)[:10]
    start = (datetime.fromisoformat(str(as_of).replace('Z', '+00:00')) -
             timedelta(days=500)).strftime('%Y-%m-%d')
    bars = fetcher.fetch_multiple_stocks(all_codes, start, end)
    from scripts.live_trading.decision_runtime import engine_v2_config
    registry = PositionRegistry(namespace=engine_v2_config(config).get('account_scope', 'DRY-RUN'))
    selection_id = ''
    try:
        from scripts.live_trading.llm_suggestions.store import load_latest_research_batch
        selection_id = (load_latest_research_batch() or {}).get('decision_id', '')
    except Exception:
        pass
    scanner = SetupScanner(registry, config)
    return scanner.scan({c: bars.get(c) for c in codes if bars.get(c) is not None},
                        {c: bars.get(c) for c in set(sector_map.values()) if bars.get(c) is not None},
                        bars.get('US.SPY'), as_of, sector_map, selection_id)


def main(argv=None):
    parser = argparse.ArgumentParser(description='生成日线中期 setup shadow')
    parser.add_argument('--config', default=str(BASE_DIR / 'config.yaml'))
    parser.add_argument('--as-of', help='带时区 ISO 时间；默认当前 UTC')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    import yaml
    with open(args.config, encoding='utf-8') as f:
        config = yaml.safe_load(f) or {}
    if config.get('buy_strategy_v2', {}).get('mode') != 'shadow':
        print(json.dumps({'error': 'buy_strategy_v2 必须为 shadow'}, ensure_ascii=False))
        return 2
    from mutifactor.data.us_fetcher import FutuUSDataFetcher
    futu = config.get('futu', {})
    fetcher = FutuUSDataFetcher(host=futu.get('host', '127.0.0.1'),
                                port=int(futu.get('port', 11111)))
    try:
        if not fetcher.connect():
            print(json.dumps({'error': '无法连接富途 OpenD'}, ensure_ascii=False))
            return 1
        result = run(config, fetcher, args.as_of)
    finally:
        fetcher.disconnect()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if all(r.get('quality', {}).get('status') == 'pass' for r in result) else 1


if __name__ == '__main__':
    sys.exit(main())
