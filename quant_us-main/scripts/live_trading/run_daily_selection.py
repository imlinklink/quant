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

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

# 日 K 收盘时点（UTC）：覆盖美东冬令时 21:00 / 夏令时 20:00 收盘后的安全标记
CLOSE_HOUR_UTC = 22.0

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


def _is_daily_bar_closed(bar_date, as_of, close_hour_utc=CLOSE_HOUR_UTC):
    """日 K 以交易日零点(UTC)表示；该交易日收盘约在 UTC 21-22 点（对应美东 16-17 点）。
    判断某根日 K 在 as_of 时点是否已收盘（避免把当日在途 K 当已完成）。
    """
    bar_dt = pd.Timestamp(bar_date)
    if bar_dt.tzinfo is None:
        bar_dt = bar_dt.tz_localize('UTC')
    as_of_dt = pd.Timestamp(as_of)
    if as_of_dt.tzinfo is None:
        as_of_dt = as_of_dt.tz_localize('UTC')
    return as_of_dt >= bar_dt + pd.Timedelta(hours=close_hour_utc)


def build_packet_from_bars(code, bars, *, name=None, sector=None, risk_group=None, events=None, now=None):
    """从日 K 生成 evidence_packet。行情指标由程序计算，LLM 只解释。数据不足返回 None。

    只使用 as_of 前「已收盘」的日 K：先按 now 截断、再剔除当日在途未收盘 K。
    observed_at 取最后一根已完成 K 线的自身时间（而非执行时间），
    并保存 data_cutoff_at（数据截止时间），避免旧行情被标成刚取得。
    """
    import numpy as np
    import pandas as pd

    from scripts.live_trading.decision_ledger.event_store import utc
    from scripts.live_trading.decision_ledger.evidence_packet import build_evidence_packet

    if bars is None or len(bars) < 2:
        return None
    now_dt = pd.Timestamp(now, unit='s') if isinstance(now, (int, float)) else pd.Timestamp(now)
    if now_dt.tzinfo is None:
        now_dt = now_dt.tz_localize('UTC')
    else:
        now_dt = now_dt.tz_convert('UTC')

    # 1) 只保留不晚于 now 的 K（历史重放安全）；2) 只保留已收盘的日 K（剔除当日在途）
    bars = bars.copy()
    bars['date'] = pd.to_datetime(bars['date'])
    if bars['date'].dt.tz is None:
        bars['date'] = bars['date'].dt.tz_localize('UTC')
    else:
        bars['date'] = bars['date'].dt.tz_convert('UTC')
    bars = bars[bars['date'] <= now_dt]
    bars = bars[[_is_daily_bar_closed(d, now_dt) for d in bars['date']]].reset_index(drop=True)
    if len(bars) < 2:
        return None

    closes = bars['close'].values.astype(float)
    price = float(closes[-1])
    if not np.isfinite(price) or price <= 0:
        return None
    # 会话收盘标记：日 K 以交易日零点表示，收盘约在 UTC 22 点（覆盖美东冬/夏令时收盘）。
    # observed_at 存「收盘后」时点，避免盘中运行把当日未收盘价当已完成。
    bar_date = bars['date'].iloc[-1]
    bar_end = bar_date + pd.Timedelta(hours=CLOSE_HOUR_UTC)   # D 22:00 UTC = 已收盘
    bar_end_iso = utc(bar_end)

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
        'observed_at': bar_end_iso,          # 行情自身时间 = 最后一根已完成 K 的收盘后时点
        'data_cutoff_at': utc(now_dt),       # 数据截止（执行）时间
        'bar_date': str(bars['date'].iloc[-1]),
        'bar_end': bar_end_iso,
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

    summary = (f'程序行情快照：最新价 {price:.2f}（bar_end {bar_end_iso}）；'
               f'1日收益 {_fmt(ret(1))}；5日收益 {_fmt(ret(5))}；'
               f'20日收益 {_fmt(ret(20))}；ATR {_fmt(atr)}；趋势 {trend or "N/A"}')
    snapshot_evidence = evidence(summary, 'internal:quote-snapshot', bar_end_iso, kind='rule')

    # 程序行情快照 + 外部事件证据（财报/公告/新闻），让 LLM 有真实事件可引用
    all_events = [snapshot_evidence] + list(events or [])
    return build_evidence_packet(code, name=name, sector=sector, risk_group=risk_group,
                                 quote=quote, events=all_events, now=bar_end_iso)


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

    from scripts.live_trading import signal_context as sc
    from scripts.live_trading import option_view as ov
    risk_group = (config.get('risk_budget', {}).get('code_groups') or {})
    packets = []
    opt_ok = 0
    opt_fail = 0
    option_cfg = config.get('option_view') or {}
    for code in universe:
        events = sc.fetch_event_evidence(code)
        # P1 期权市场视角：作为参考证据并入 packet（失败降级为空，不影响选股）
        try:
            oview = (ov.fetch_option_view(
                code, cache_seconds=int(option_cfg.get('cache_seconds', 300)),
                quality_config=option_cfg.get('quality'))
                if option_cfg.get('enabled', True) else {'error': '配置已关闭'})
            quality_status = ((oview or {}).get('quality') or {}).get('status')
            if oview and not oview.get('error') and quality_status != 'unusable':
                opt_ev = ov.make_option_evidence(code, oview, now=now)
                events = list(events) + [opt_ev]
                opt_ok += 1
                logger.info(f"[期权视角] {code} 已并入: {ov.option_view_summary(oview)}")
            else:
                opt_fail += 1
                logger.warning(f"[期权视角] {code} 无数据/失败: {oview}")
        except Exception as e:
            opt_fail += 1
            logger.warning(f"[期权视角] {code} 异常，跳过（不影响选股）: {type(e).__name__}: {e}")
        p = build_packet_from_bars(code, bars_map.get(code), risk_group=risk_group.get(code),
                                   events=events, now=now)
        if p is not None:
            packets.append(p)
    # warning 级汇总：root 默认 WARNING，info 看不到，用这条一眼确认
    logger.warning(f"[期权视角] 汇总: 并入 {opt_ok}/{len(universe)}, 失败/跳过 {opt_fail}")

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
