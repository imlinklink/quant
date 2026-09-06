# -*- coding: utf-8 -*-
"""
A/B/C 回测：v1 阶段低点抄底 + 大盘闸 是否值得加入现有日线动量引擎

变体说明（日线代理口径，见 docstring 末尾的假设清单）：
  A  : 原动量引擎基线（MomentumStrategy 原样）
  B  : 动量 + v1 阶段低点信号 + 大盘闸（退出仍走统一 ATR，与 A 一致）
  B0 : 动量 + v1 阶段低点信号、但大盘闸关闭（用于拆解大盘闸贡献）
  C  : B + bottom_fish 专属退出（结构止损 + 观察窗时间止损），
        动量持仓退出仍与 A 一致

日线代理与实盘的差异（本脚本在 README 层面明示，不假装与 live 全等）：
  1. live 用日内 K 线在“追涨/抄底”模式间切换；日线回测改为：
     - 动量信号 = 引擎原有 momentum 选股；
     - 阶段低点信号 = 每只候选在日线收盘满足
       analyze_bottom_fish_daily（回撤/RSI/拐头/止损距离全套门槛）。
     二者同日都出现时动量优先、阶段低点补剩余仓位。
  2. 大盘闸按“信号日收盘”的恒指 vs MA20 计算：
     - 恒指 < MA20 且当日跌 ≥ hsi_drop_pct → pause（本轮全部不买）；
     - 恒指 < MA20（below_ma20_action=stricter）→
       阶段低点最低分 +stage_strict_bonus。
     实盘里 stricter 还会把“日内K线阈值 +2”作用于动量轮，
     日线引擎两侧都没有日内K线层，故此处动量不变（对 B 偏保守）。
  3. 成交与 A 完全同一约定：信号日 D 基于 D-1 收盘数据，买入按 D+1 开盘。
  4. bottom_fish 候选也复用动量池的质量/上市门槛（质量检查、非新股、
     解禁期前跳过），避免对垃圾标的抄底。

用法：
  python3 scripts/backtest/ab_bottomfish_gate.py \
      --start 2024-09-06 --end 2026-09-05 --variants A,B,B0,C
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

project_root = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from mutifactor.data import FutuHKDataFetcher
from mutifactor.data.base_fetcher import KLineType, MarketType
from mutifactor.framework import BacktestRunner
from mutifactor.strategies.momentum import MomentumStrategy

try:
    from scripts.live_trading.hk_position_manager import _bottom_time_stop_hit
except Exception:  # pragma: no cover - 纯防御
    from scripts.live_trading import hk_position_manager
    _bottom_time_stop_hit = hk_position_manager._bottom_time_stop_hit

try:
    from scripts.live_trading.intraday_analyzer import IntradayAnalyzer
except Exception as e:  # pragma: no cover
    print(f"[FATAL] 无法导入 IntradayAnalyzer: {e}")
    sys.exit(2)


logger = logging.getLogger("ab_bottomfish_gate")

INDEX_CODES = {"HK.800000"}


def load_config(config_file: str) -> dict:
    import yaml
    with open(config_file, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_universe() -> List[str]:
    """从 data/stock_info.yaml 读启用股票（与回测引擎一致），失败给空。"""
    import yaml
    try:
        path = os.path.join(project_root, "data", "stock_info.yaml")
        with open(path, "r", encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
        rows = d.get("stock_info") or []
        codes = [
            r["stock_code"] for r in rows
            if r.get("market") == "HK"
            and str(r.get("enabled", 1)) in ("1", "True", "true")
        ]
        return codes
    except Exception as e:
        logger.warning("读取 stock_info.yaml 失败: %s", e)
        return []


def fetch_price_data(codes: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
    """从富途拉日 K（数据与 backtest_engine 同源）。"""
    out: Dict[str, pd.DataFrame] = {}
    with FutuHKDataFetcher() as fetcher:
        out = fetcher.fetch_multiple_stocks(codes, start, end, ktype=KLineType.DAY)
    for code, df in out.items():
        if df is None or len(df) == 0:
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = df[col].astype(float)
        out[code] = df
    return {k: v for k, v in out.items() if v is not None and len(v) > 0}


class HybridMomentumStrategy(MomentumStrategy):
    """动量 + v1 阶段低点 + 大盘闸 的日线回测子类。

    通过覆写 select_stocks / execute_long_trade / check_exit_signal 挂进
    BaseStrategy.run_backtest，不修改任何引擎代码。
    """

    # C 变体的 bottom_fish 专属止损也进入冷却期（与实盘 _should_cooldown 对齐）
    STOP_LOSS_REASONS = MomentumStrategy.STOP_LOSS_REASONS | {
        "bottomfish_structure_stop",
        "bottomfish_time_stop",
    }

    def __init__(self, initial_capital: float = None, config: Dict = None,
                 market_type=MarketType.HK, variant: str = "B"):
        super().__init__(initial_capital, config=config, market_type=market_type)
        self.variant = variant
        self._use_gate = variant not in ("B0", "C0")
        self._live_exits = variant in ("C", "C0")

        smart = ((config or {}).get("trading", {}).get("live_trading", {})
                 .get("buy_timing", {}).get("smart", {}) or {})
        self._bf_cfg = dict(smart.get("bottom_fish_daily") or {})
        self._gate_cfg = dict(smart.get("market_gate") or {})
        self._bf_enabled = bool(self._bf_cfg.get("enabled", False))
        self._gate_enabled = bool(self._gate_cfg.get("enabled", False)) and self._use_gate

        # 规范位置：buy_timing.smart.bottom_fish_daily（analyzer 现已支持，
        # 与 live_manager/hk_position_manager 读取一致）。
        self._analyzer = IntradayAnalyzer(
            {"smart": {"bottom_fish_daily": self._bf_cfg}}
        ) if self._bf_enabled else None

        self._pending_buy_mode: Dict[str, dict] = {}
        self._fills: List[dict] = []
        self._dbg_bf_signal: List[dict] = []
        self.gate_stats = {
            "days": 0, "normal": 0, "stricter": 0,
            "pause": 0, "pause_drop": 0,
            "momentum_sel": 0, "bottomfish_sel": 0,
            "momentum_fill": 0, "bottomfish_fill": 0,
        }

    # ---------- 大盘闸（日线代理） ----------
    def _daily_gate_state(self, stocks_data) -> Tuple[bool, str]:
        if not self._gate_enabled:
            return True, ""
        df = stocks_data.get("HK.800000")
        if df is None or len(df) < 25:
            return True, ""
        try:
            closes = df["close"].astype(float).values
            ma20 = float(closes[-20:].mean())
            last = float(closes[-1])
            prev = float(closes[-2]) if len(closes) > 1 else last
            drop = (last / prev - 1.0) if prev > 0 else 0.0
            below = last < ma20
            drop_pct = float(self._gate_cfg.get("hsi_drop_pct", 0.01))
            action = str(self._gate_cfg.get("below_ma20_action", "stricter")).lower()
            if below and drop <= -drop_pct:
                return False, "pause_drop"
            if below and action == "pause":
                return False, "pause"
            if below:
                return True, "stricter"
        except Exception as e:
            logger.warning("大盘闸计算失败，放行: %s", e)
        return True, ""

    # ---------- 选股：动量优先，阶段低点补位 ----------
    def select_stocks(self, stocks_data: Dict, current_date: str,
                      pool_size: int = None) -> List[str]:
        self._pending_buy_mode = {}
        slots = max(0, self.max_positions - len(self.positions))
        if slots == 0:
            return []
        gate_allowed, gate_action = self._daily_gate_state(stocks_data)
        self.gate_stats["days"] += 1
        self.gate_stats[gate_action if gate_action else "normal"] += 1
        if not gate_allowed:
            return []

        try:
            momentum_sel = super().select_stocks(
                stocks_data, current_date,
                pool_size=pool_size if pool_size is not None else slots,
            ) or []
        except Exception as e:
            logger.warning("动量选股异常 %s: %s", current_date, e)
            momentum_sel = []
        picked = list(momentum_sel)
        self.gate_stats["momentum_sel"] += len(picked)
        for c in picked:
            self._pending_buy_mode[c] = {"mode": "momentum", "structure_stop": 0.0}

        if (not self._bf_enabled or self._analyzer is None
                or len(picked) >= slots):
            return picked

        strict_bonus = 0.0
        if gate_action == "stricter":
            strict_bonus = float(self._gate_cfg.get("stage_strict_bonus", 1))
        min_score = float(self._bf_cfg.get("confirm_min_score", 3)) + strict_bonus

        for code, df in stocks_data.items():
            if code in INDEX_CODES or code in self.positions or code in picked:
                continue
            if df is None or len(df) < 22:
                continue
            # 与动量池对齐的基本门槛：质量合格、非新股、非解禁期前
            try:
                listing_date = self.get_stock_listing_date(code)
                new_params = self.get_new_stock_params(listing_date, current_date)
                if new_params.get("is_new_stock"):
                    continue
                if listing_date:
                    from datetime import datetime as _dt
                    if isinstance(listing_date, str):
                        listing_date = _dt.strptime(listing_date, "%Y-%m-%d").date()
                    if isinstance(current_date, str):
                        cur = _dt.strptime(current_date, "%Y-%m-%d").date()
                    else:
                        cur = current_date
                    days_listed = (cur - listing_date).days
                    if 150 <= days_listed < 180:
                        continue
                quality_ok, _reason = self.check_stock_quality(code, df)
                if not quality_ok:
                    continue
            except Exception:
                continue
            try:
                px = float(df["close"].iloc[-1])
                if px <= 0:
                    continue
                res = self._analyzer.analyze_bottom_fish_daily(df, px)
            except Exception as e:
                logger.debug("阶段低点评分异常 %s: %s", code, e)
                continue
            if not (res and res.get("ok")):
                continue
            if float(res.get("score", 0)) < min_score:
                continue
            picked.append(code)
            self.gate_stats["bottomfish_sel"] += 1
            self._dbg_bf_signal.append({
                "date": str(current_date), "code": code,
                "score": float(res.get("score", 0)),
                "strict": gate_action == "stricter",
                "slots": slots,
            })
            self._pending_buy_mode[code] = {
                "mode": "bottom_fish",
                "structure_stop": float(res.get("structure_stop") or 0.0),
                "ref_low": float(res.get("ref_low") or 0.0),
            }
            if len(picked) >= slots:
                break
        return picked

    # ---------- 成交时给持仓打标签（仅统计，不影响引擎） ----------
    def execute_long_trade(self, selected_stocks: List[str],
                           stocks_data: Dict, current_date: str,
                           historical_data: Dict = None,
                           next_day_data: Dict = None) -> List[Dict]:
        before = set(self.positions)
        orders = super().execute_long_trade(
            selected_stocks, stocks_data, current_date,
            historical_data=historical_data, next_day_data=next_day_data,
        ) or []
        pending = getattr(self, "_pending_buy_mode", {}) or {}
        for o in orders:
            code = o.get("stock_code")
            if not code or code not in self.positions or code in before:
                continue
            info = pending.get(code) or {"mode": "momentum"}
            mode = info.get("mode", "momentum")
            self.positions[code]["entry_mode"] = mode
            self.positions[code]["structure_stop"] = float(
                info.get("structure_stop") or 0.0)
            self.gate_stats["bottomfish_fill" if mode == "bottom_fish"
                            else "momentum_fill"] += 1
            self._fills.append({
                "stock_code": code,
                "buy_date": str(current_date),
                "entry_mode": mode,
            })
        return orders

    # ---------- C：bottom_fish 专属退出（结构止损 + 观察窗时间止损） ----------
    def check_exit_signal(self, stock_code: str, position: dict,
                          current_price: float, current_date: str):
        mode = position.get("entry_mode", "momentum")
        if self._live_exits and mode == "bottom_fish":
            try:
                ss = float(position.get("structure_stop") or 0.0)
                if ss > 0 and current_price > 0 and current_price <= ss:
                    return "bottomfish_structure_stop"
                if bool(self._bf_cfg.get("short_time_stop_enabled", True)):
                    window = int(self._bf_cfg.get("short_time_stop_days", 5))
                    rebound = float(
                        self._bf_cfg.get("short_time_stop_rebound_pct", 0.03))
                    df = getattr(self, "_stock_data_cache", {}).get(stock_code)
                    if df is not None and len(df) > 0:
                        hit, _held = _bottom_time_stop_hit(
                            position.get("buy_date"), df, current_price,
                            position.get("highest_price"),
                            position.get("cost_price"), window, rebound,
                        )
                        if hit:
                            return "bottomfish_time_stop"
            except Exception as e:
                logger.debug("bottom_fish 专属退出计算失败 %s: %s", stock_code, e)
        return super().check_exit_signal(
            stock_code, position, current_price, current_date)


def make_strategy(variant: str, config: dict, capital: float):
    if variant == "A":
        return MomentumStrategy(initial_capital=capital, config=config,
                                market_type=MarketType.HK)
    return HybridMomentumStrategy(
        initial_capital=capital, config=config,
        market_type=MarketType.HK, variant=variant)


def run_one(variant: str, config: dict, prepared: Dict[str, pd.DataFrame],
            start: str, end: str, capital: float) -> dict:
    strategy = make_strategy(variant, config, capital)
    results = strategy.run_backtest(prepared, start, end)
    if not results:
        return {"variant": variant, "empty": True}

    th = results.get("trade_history")
    if isinstance(th, pd.DataFrame) and len(th) > 0:
        fills_map = {}
        for f in getattr(strategy, "_fills", []):
            fills_map[(f["stock_code"], str(f["buy_date"]))] = f["entry_mode"]
        th = th.copy()
        th["entry_mode"] = th.apply(
            lambda r: fills_map.get(
                (r.get("stock_code"), str(r.get("buy_date"))), "momentum"),
            axis=1,
        )
        results["trade_history"] = th
    results["variant"] = variant
    results["strategy"] = strategy
    return results


def fmt_metrics(res: dict) -> dict:
    return {
        "variant": res.get("variant"),
        "final": float(res.get("final_value", 0)),
        "total_return": float(res.get("total_return", 0)) * 100,
        "annual": float(res.get("annual_return", 0)) * 100,
        "max_dd": float(res.get("max_drawdown", 0)) * 100,
        "sharpe": float(res.get("sharpe_ratio", 0)),
        "trades": int(res.get("total_trades", 0)),
        "win_rate": float(res.get("win_rate", 0)) * 100,
        "cost": float(res.get("total_cost", 0)),
    }


def mode_stats(res: dict) -> Dict[str, dict]:
    th = res.get("trade_history")
    out: Dict[str, dict] = {}
    if not isinstance(th, pd.DataFrame) or len(th) == 0:
        return out
    for mode, grp in th.groupby("entry_mode"):
        wins = int((grp.get("profit_pct", 0) > 0).sum())
        out[mode] = {
            "trades": int(len(grp)),
            "wins": wins,
            "win_rate": round(wins / len(grp) * 100, 1) if len(grp) else 0,
            "avg_profit_pct": round(float(grp.get("profit_pct", 0).mean()), 2)
            if "profit_pct" in grp else 0,
            "reasons": grp["reason"].value_counts().to_dict()
            if "reason" in grp else {},
        }
    return out


def main():
    parser = argparse.ArgumentParser(description="v1 阶段低点 + 大盘闸 A/B 回测")
    parser.add_argument("--start", default="2024-09-06")
    parser.add_argument("--end", default="2026-09-05")
    parser.add_argument("--capital", type=float, default=300000.0)
    parser.add_argument("--variants", default="A,B,C")
    parser.add_argument("--config", default=os.path.join(project_root, "config.yaml"))
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    if args.capital <= 0:
        args.capital = float(config.get("strategy", {}).get("initial_capital", 300000))

    codes = load_universe()
    codes = [c for c in codes if c not in INDEX_CODES]
    if not codes:
        print("股票池为空，请先在 data/stock_info.yaml 启用股票")
        return
    fetch_codes = ["HK.800000"] + codes
    warm_start = (datetime.strptime(args.start, "%Y-%m-%d")
                  - timedelta(days=100)).strftime("%Y-%m-%d")

    print(f"数据拉取: {len(fetch_codes)} 只（含恒指） {warm_start} ~ {args.end}")
    raw = fetch_price_data(fetch_codes, warm_start, args.end)
    prepared = BacktestRunner("momentum").prepare_data(raw)
    print(f"有效数据: {len(prepared)} 只 | "
          f"样本区间: {[c for c in codes if c in prepared]}")

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    all_results = []
    rows = []
    for v in variants:
        print(f"\n===== 运行变体 {v} =====")
        res = run_one(v, config, prepared, args.start, args.end, args.capital)
        if res.get("empty"):
            print(f"  {v}: 无结果")
            continue
        m = fmt_metrics(res)
        rows.append(m)
        all_results.append(res)
        print(f"  {v}: 终值 {m['final']:,.0f} | "
              f"总收益 {m['total_return']:.1f}% | 年化 {m['annual']:.1f}% | "
              f"回撤 {m['max_dd']:.1f}% | Sharpe {m['sharpe']:.2f} | "
              f"交易 {m['trades']} | 胜率 {m['win_rate']:.1f}%")
        for mode, st in mode_stats(res).items():
            print(f"    [{mode}] {st['trades']}笔 胜率{st['win_rate']}% "
                  f"均值{st['avg_profit_pct']}% "
                  f"退出{ {k: v for k, v in st['reasons'].items()} }")
        strat = res.get("strategy")
        if hasattr(strat, "gate_stats"):
            gs = strat.gate_stats
            print(f"    大盘闸天数: normal={gs['normal']} stricter={gs['stricter']} "
                  f"pause={gs['pause'] + gs['pause_drop']} "
                  f"(含急跌pause_drop={gs['pause_drop']}) | "
                  f"动量入选/成交={gs['momentum_sel']}/{gs['momentum_fill']} "
                  f"阶段低点入选/成交={gs['bottomfish_sel']}/"
                  f"{gs['bottomfish_fill']}")
            if getattr(strat, "_dbg_bf_signal", []):
                print("    阶段低点信号明细:")
                for s in strat._dbg_bf_signal:
                    print(f"      {s['date']} {s['code']} score={s['score']} "
                          f"stricter={s['strict']} slots={s['slots']}")
        eq = res.get("equity_curve")
        if isinstance(eq, pd.DataFrame):
            eq.to_csv(os.path.join(project_root, "logs",
                                   f"ab_{v}_equity.csv"), index=False)
        th = res.get("trade_history")
        if isinstance(th, pd.DataFrame):
            th.to_csv(os.path.join(project_root, "logs",
                                   f"ab_{v}_trades.csv"), index=False)

    print("\n===== A/B/C 汇总 =====")
    head = ["variant", "final", "total_return", "annual", "max_dd",
            "sharpe", "trades", "win_rate", "cost"]
    summary = pd.DataFrame(rows)
    print(summary[head].to_string(index=False))


if __name__ == "__main__":
    main()
