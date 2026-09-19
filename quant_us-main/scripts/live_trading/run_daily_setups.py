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
    codes = list(cfg.get('watch_list') or config.get('dip_buy', {}).get('watch_list') or [])
    group_map = config.get('risk_budget', {}).get('code_groups', {})
    proxies = config.get('pullback', {}).get('sector_proxies', {})
    sector_map = {code: proxies.get(group_map.get(code), 'US.SPY') for code in codes}
    all_codes = list(dict.fromkeys(codes + list(sector_map.values()) + ['US.SPY']))
    from scripts.data.trading_calendar import sessions
    import pandas as pd
    from zoneinfo import ZoneInfo
    instant = datetime.fromisoformat(str(as_of).replace('Z', '+00:00'))
    if instant.tzinfo is None:
        raise ValueError('AS_OF_TIMEZONE_REQUIRED')
    local = instant.astimezone(ZoneInfo('America/New_York'))
    dates = sessions(local.date() - timedelta(days=14), local.date()).session_date
    completed = [d for d in dates if d.tz_localize('America/New_York') + pd.Timedelta(hours=16) <= local]
    if not completed:
        raise ValueError('NO_COMPLETED_SESSION')
    anchor = completed[-1]
    end = str(anchor.date())
    start = (anchor - pd.Timedelta(days=500)).strftime('%Y-%m-%d')
    bars = fetcher.fetch_multiple_stocks(all_codes, start, end)
    from scripts.live_trading.decision_runtime import engine_v2_config
    registry = PositionRegistry(namespace=engine_v2_config(config).get('account_scope', 'DRY-RUN'))
    selection_id = ''
    try:
        from scripts.live_trading.llm_suggestions.store import load_research_batch_for_session
        batch = load_research_batch_for_session(local.date().isoformat())
        selection_id = (batch or {}).get('decision_id', '')
    except Exception:
        pass
    # 回看锚点改变属于输入协议新版本；不能覆盖旧浮动窗口快照。
    scan_config = dict(config, buy_strategy_v2=dict(cfg,
                       input_window_version='completed-session-500d-v1'))
    scanner = SetupScanner(registry, scan_config)
    return scanner.scan({c: bars.get(c) for c in codes},
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
    # **退出码只表示"这次运行有没有做完"，不表示"每个标的都过了质量门"。**
    #
    # 原先是 `0 if all(quality == 'pass') else 1`：只要有一个标的不是 pass 就 exit 1，
    # 而出口 `ShadowJobs` 把非零当**作业失败** ⇒ 重试 3 次（同一标的、同一原因，必然
    # 同样失败）⇒ **之后永久不再补**。2026-09-19 实测 `US.RAM` 就是这样把
    # `daily_setup_shadow` 钉死的，而且它的"失败原因"根本不是数据质量 ——
    # 是 `SetupScanner.scan` 对 `不可覆盖历史快照` 的**正当拒绝**
    # （`IMMUTABLE_SNAPSHOT_CONFLICT`：重跑过去 session 时不能覆盖已冻结的快照）。
    #
    # 每个标的的质量门结果与拒绝原因都在输出的 `quality` / `reason_codes` 里，
    # 那是**数据**，不是进程状态。真正的进程级故障（连不上 OpenD、配置不对、
    # `run()` 抛异常）仍然走 return 1/2 或异常退出，不受此影响。
    failed = [str(r.get('code')) for r in (result or [])
              if (r.get('quality') or {}).get('status') != 'pass']
    if failed:
        # 只作提示；`_run_subprocess` 会把它连同 stdout 一起落日志。
        print(f'# 质量门未通过（不影响退出码）：{failed}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
