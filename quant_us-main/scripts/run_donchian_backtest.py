"""
唐奇安（海龟）双通道突破 回测验证 - 美股观察池

目的：验证“第二条策略线”的历史表现：
    价格收盘突破 N 日最高（快通道 20 / 慢通道 55），可选放量/MA50 过滤，
    出场用 2×ATR 吊灯（与现网 chandelier 风控同源，固定 -5% 保底），
    另含“经典海龟 S1：20 日突破 + 10 日反向通道出场”作对照。

用法（需 Futu OpenD 运行）：
    python scripts/run_donchian_backtest.py --start 2023-06-01 --output-dir backtests
    python scripts/run_donchian_backtest.py --codes US.MU,US.SOXL --no-plot

输出：
    控制台汇总表；backtests/donchian_breakout_report.md
    backtests/donchian_breakout_trades.csv
    backtests/donchian_breakout_equity.png
"""
import argparse
import os
import sys
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

plt.rcParams["font.sans-serif"] = [
    "Arial Unicode MS", "PingFang SC", "Heiti SC", "SimHei", "DejaVu Sans",
]
plt.rcParams["axes.unicode_minus"] = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("donchian_bt")

# ============ 回测假设（与现网尽量一致，并显式标注） ============
ATR_PERIOD = 14          # 与现网 chandelier.atr_period 一致
STOP_MULT = 2.0          # 2×ATR 吊灯（现网 atr_trailing_mult=2.0）
FIXED_STOP_PCT = 0.05    # 固定保底止损 -5%（现网 fixed_stop_pct）
COST_ROUND_TRIP_PCT = 0.20  # 含佣金+平台费+滑点，双向约 0.2%
POSITION_USD = 5000.0    # 单笔名义本金，与 dip_buy.position_size_usd 一致


def load_config():
    cfg_path = os.path.join(PROJECT_ROOT, "config.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        return __import__("yaml").safe_load(f)


def fetch_daily_data(codes: List[str], start: str, end: str) -> Dict[str, pd.DataFrame]:
    """通过富途 OpenD 拉取日 K（前复权），返回 {code: df}。"""
    from mutifactor.data.us_fetcher import FutuUSDataFetcher
    from mutifactor.data.base_fetcher import KLineType

    cfg = load_config()
    futu_cfg = cfg.get("futu", {})
    fetcher = FutuUSDataFetcher(
        host=futu_cfg.get("host", "127.0.0.1"),
        port=int(futu_cfg.get("port", 11111)),
    )
    if not fetcher.connect():
        logger.error("无法连接富途 OpenD，请确认已启动")
        sys.exit(2)
    out: Dict[str, pd.DataFrame] = {}
    try:
        for i, code in enumerate(codes, 1):
            logger.info(f"[{i}/{len(codes)}] 拉取日K {code} {start}~{end}")
            df = fetcher.fetch_stock_kline(code, start, end, ktype=KLineType.DAY)
            if df is None or df.empty:
                logger.warning(f"  {code} 无数据，跳过")
                continue
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"]).dt.normalize()
            df = (
                df.sort_values("date")
                .drop_duplicates(subset=["date"])
                .reset_index(drop=True)
            )
            out[code] = df
    finally:
        fetcher.disconnect()
    return out


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """为单只股票一次性计算所有变体共用的指标（全部只用当日及之前数据）。"""
    d = df.copy()
    high = d["high"]
    low = d["low"]
    close = d["close"]
    vol = d["volume"].astype(float)

    # 唐奇安通道：以“此前 N 日”的最高/最低为基准（shift(1)，不含当日）
    d["hh20"] = high.shift(1).rolling(20).max()
    d["hh55"] = high.shift(1).rolling(55).max()
    d["ll10"] = low.shift(1).rolling(10).min()
    d["ll20"] = low.shift(1).rolling(20).min()

    # ATR：与现网一致，TR 的 14 日简单均值
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    d["atr"] = tr.rolling(ATR_PERIOD).mean()

    # 放量：当日量 / 此前 20 日均量
    d["vma20"] = vol.shift(1).rolling(20).mean()
    d["vol_ratio"] = vol / d["vma20"]

    # 中期趋势过滤
    d["ma50"] = close.rolling(50).mean()
    return d


VARIANT_DEFS = [
    {
        "key": "dc20",
        "label": "唐奇安20突破 + 2ATR吊灯",
        "entry_n": 20,
        "vol_ratio": None,
        "ma_filter": None,
        "exit": "chandelier",
    },
    {
        "key": "dc55",
        "label": "唐奇安55突破 + 2ATR吊灯",
        "entry_n": 55,
        "vol_ratio": None,
        "ma_filter": None,
        "exit": "chandelier",
    },
    {
        "key": "dc20_vol",
        "label": "唐奇安20突破 + 放量1.5× + 2ATR吊灯",
        "entry_n": 20,
        "vol_ratio": 1.5,
        "ma_filter": None,
        "exit": "chandelier",
    },
    {
        "key": "dc55_vol",
        "label": "唐奇安55突破 + 放量1.5× + 2ATR吊灯",
        "entry_n": 55,
        "vol_ratio": 1.5,
        "ma_filter": None,
        "exit": "chandelier",
    },
    {
        "key": "dc20_ma50",
        "label": "唐奇安20突破 + 站上MA50 + 2ATR吊灯",
        "entry_n": 20,
        "vol_ratio": None,
        "ma_filter": "ma50",
        "exit": "chandelier",
    },
    {
        "key": "turtle_s1",
        "label": "经典海龟S1: 20突破 + 10日反向通道出场",
        "entry_n": 20,
        "vol_ratio": None,
        "ma_filter": None,
        "exit": "reverse",
    },
]


def run_variant(df: pd.DataFrame, vdef: Dict) -> List[Dict]:
    """单股票单变体回测。返回交易 dict 列表。"""
    trades: List[Dict] = []
    arr = df.reset_index(drop=True)
    n = len(arr)
    entry_n = vdef["entry_n"]
    hh_col = f"hh{entry_n}"
    vol_ratio_min = vdef["vol_ratio"]
    ma_filter = vdef["ma_filter"]
    exit_mode = vdef["exit"]

    i = 0
    while i < n:
        row = arr.iloc[i]
        if (
            not np.isfinite(row[hh_col])
            or np.isnan(row[hh_col])
            or not (row["close"] > row[hh_col])
        ):
            i += 1
            continue
        # 放量过滤（在信号日判断；量比需有限）
        if vol_ratio_min is not None:
            vr = row.get("vol_ratio")
            if not np.isfinite(vr) or vr < vol_ratio_min:
                i += 1
                continue
        # 中期趋势过滤
        if ma_filter == "ma50":
            m50 = row.get("ma50")
            if not np.isfinite(m50) or not (row["close"] > m50):
                i += 1
                continue

        # 信号日 = i，次日开盘入场（无前视）
        if i + 1 >= n:
            i += 1
            continue
        entry_idx = i + 1
        entry_px = float(arr.iloc[entry_idx]["open"])
        if entry_px <= 0 or not np.isfinite(entry_px):
            i += 1
            continue

        entry_date = arr.iloc[entry_idx]["date"].date()
        highest = max(entry_px, float(arr.iloc[entry_idx]["high"]))
        if exit_mode == "chandelier":
            stop = entry_px * (1 - FIXED_STOP_PCT)
        else:  # reverse: 经典海龟，初始 2×ATR(入场日) 波动止损
            atr_entry = arr.iloc[entry_idx]["atr"]
            stop = (
                entry_px - STOP_MULT * atr_entry
                if np.isfinite(atr_entry)
                else entry_px * (1 - FIXED_STOP_PCT)
            )

        exit_date = None
        exit_px = None
        exit_reason = None
        j = entry_idx
        while j < n:
            bar = arr.iloc[j]
            # 反向通道出场（经典海龟）：止损线 = max(2×ATR初始止损, 此前10日低点)
            # 盘中跌破即出场，跳空低开按开盘价成交（保守）
            if exit_mode == "reverse" and j > entry_idx:
                ch_low = bar.get("ll10")
                trigger = stop
                if np.isfinite(ch_low):
                    trigger = max(stop, float(ch_low))
                if np.isfinite(trigger) and bar["low"] < trigger:
                    exit_px = min(float(bar["open"]), float(trigger))
                    exit_reason = (
                        "跌破10日低点"
                        if np.isfinite(ch_low) and float(ch_low) >= stop
                        else "2ATR初始止损"
                    )
                    exit_date = bar["date"].date()
                    break
            # 更新吊灯止损（基于当日最高与当日收盘后 ATR，ratchet 只升不降）
            if exit_mode == "chandelier":
                highest = max(highest, float(bar["high"]))
                atr_j = bar.get("atr")
                if np.isfinite(atr_j):
                    stop = max(stop, highest - STOP_MULT * float(atr_j))

            # 检查“次日”是否触发止损
            if j + 1 >= n:
                break
            nxt = arr.iloc[j + 1]
            if float(nxt["open"]) <= stop:
                exit_px = float(nxt["open"])
                exit_reason = "跳空低开穿止损"
                exit_date = nxt["date"].date()
                break
            if float(nxt["low"]) <= stop:
                exit_px = float(stop)
                exit_reason = "吊灯止损"
                exit_date = nxt["date"].date()
                break
            j += 1

        if exit_px is None:
            # 数据结尾仍持仓：按最后收盘标记（统计为 open）
            last = arr.iloc[-1]
            exit_px = float(last["close"])
            exit_date = last["date"].date()
            exit_reason = "期末未平仓"

        gross_pct = (exit_px / entry_px - 1.0) * 100.0
        net_pct = gross_pct - COST_ROUND_TRIP_PCT
        trades.append(
            {
                "entry_date": str(entry_date),
                "exit_date": str(exit_date),
                "entry_price": round(entry_px, 4),
                "exit_price": round(exit_px, 4),
                "reason": exit_reason,
                "gross_pnl_pct": round(gross_pct, 3),
                "net_pnl_pct": round(net_pct, 3),
                "net_pnl_usd": round(net_pct / 100.0 * POSITION_USD, 2),
                "open": exit_reason == "期末未平仓",
                "holding_days": (arr.iloc[-1]["date"].date() - entry_date).days
                if exit_reason == "期末未平仓"
                else (exit_date - entry_date).days,
            }
        )
        # 持仓期间不重复开仓；出场后继续扫描
        i = j + 1 if exit_reason != "期末未平仓" else n

    return trades


def summarize_trades(trades: List[Dict], label: str) -> Dict:
    if not trades:
        return {
            "label": label,
            "n": 0,
            "n_open": 0,
            "win_rate": np.nan,
            "avg_net_pct": np.nan,
            "total_net_usd": 0.0,
            "profit_factor": np.nan,
            "max_loss_pct": np.nan,
            "avg_hold_days": np.nan,
            "largest_win_pct": np.nan,
        }
    closed = [t for t in trades if not t["open"]]
    nets = np.array([t["net_pnl_pct"] for t in trades])
    wins = nets[nets > 0]
    losses = nets[nets <= 0]
    pf = (
        (wins.sum() / abs(losses.sum()))
        if len(losses) and abs(losses.sum()) > 1e-9
        else (np.inf if len(wins) else np.nan)
    )
    total = sum(t["net_pnl_usd"] for t in trades)
    return {
        "label": label,
        "n": len(trades),
        "n_open": len([t for t in trades if t["open"]]),
        "win_rate": round((nets > 0).mean() * 100, 1) if len(nets) else np.nan,
        "avg_net_pct": round(nets.mean(), 3) if len(nets) else np.nan,
        "total_net_usd": round(total, 2),
        "profit_factor": round(pf, 2) if np.isfinite(pf) else np.nan,
        "max_loss_pct": round(nets.min(), 3) if len(nets) else np.nan,
        "avg_hold_days": round(
            np.mean([t["holding_days"] for t in closed]), 1
        )
        if closed
        else np.nan,
        "largest_win_pct": round(nets.max(), 3) if len(nets) else np.nan,
    }


def fmt(v, suffix=""):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    if isinstance(v, float):
        return f"{v:.2f}{suffix}"
    return str(v)


def render_table(rows: List[List[str]]) -> str:
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for r_i, row in enumerate(rows):
        lines.append(
            " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(row))
        )
        if r_i == 0:
            lines.append("-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def plot_equity(all_series: Dict[str, pd.Series], out_path: str):
    """累计已平仓净盈亏（每笔 $5000）曲线。"""
    fig, ax = plt.subplots(figsize=(12, 6))
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
    label_map = {v["key"]: v["label"] for v in VARIANT_DEFS}
    for (key, ser), color in zip(all_series.items(), colors):
        if ser is None or len(ser) < 2:
            continue
        ax.plot(
            ser.index, ser.values,
            label=label_map.get(key, key), linewidth=1.6, color=color,
        )
    ax.axhline(0, color="gray", linewidth=0.8, linestyle="--")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.set_title("美股观察池 唐奇安突破回测 - 累计净盈亏（每笔本金 $5000，含0.2%成本）")
    ax.set_ylabel("累计净盈亏 (USD)")
    ax.legend(loc="upper left", fontsize=9)
    ax.grid(alpha=0.25)
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def count_candidates(df: pd.DataFrame, vdef: Dict) -> int:
    """统计满足入场条件的信号日个数（不计持仓互斥，用于核对成交转化）。"""
    entry_n = vdef["entry_n"]
    hh_col = f"hh{entry_n}"
    vol_ratio_min = vdef["vol_ratio"]
    ma_filter = vdef["ma_filter"]
    cnt = 0
    for _, row in df.iterrows():
        if not np.isfinite(row[hh_col]) or not (row["close"] > row[hh_col]):
            continue
        if vol_ratio_min is not None:
            vr = row.get("vol_ratio")
            if not np.isfinite(vr) or vr < vol_ratio_min:
                continue
        if ma_filter == "ma50":
            m50 = row.get("ma50")
            if not np.isfinite(m50) or not (row["close"] > m50):
                continue
        cnt += 1
    return cnt


def main():
    ap = argparse.ArgumentParser(description="唐奇安突破回测（美股观察池）")
    ap.add_argument("--start", default="2023-06-01", help="数据起始日")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument(
        "--codes", default=None, help="逗号分隔，默认取 config dip_buy.watch_list"
    )
    ap.add_argument("--output-dir", default="backtests")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    codes = (
        [c.strip() for c in args.codes.split(",") if c.strip()]
        if args.codes
        else list(cfg.get("dip_buy", {}).get("watch_list", []))
    )
    if not codes:
        logger.error("未找到观察池代码")
        sys.exit(2)

    print(f"\n回测范围: {args.start} ~ {args.end}")
    print(f"股票: {', '.join(codes)}")
    print(f"假设: 2×ATR吊灯(14日ATR) + {FIXED_STOP_PCT*100:.0f}%保底 | "
          f"单笔${POSITION_USD:.0f} | 往返成本{COST_ROUND_TRIP_PCT:.2f}%")

    data = fetch_daily_data(codes, args.start, args.end)
    if not data:
        logger.error("没有任何股票数据，退出")
        sys.exit(3)
    print(f"获取数据成功: {len(data)}/{len(codes)} 只")

    ind_data = {c: add_indicators(df) for c, df in data.items()}
    by_variant: Dict[str, Dict[str, List[Dict]]] = {
        v["key"]: {c: [] for c in ind_data} for v in VARIANT_DEFS
    }

    for c, df in ind_data.items():
        first = df["date"].iloc[0].date()
        last = df["date"].iloc[-1].date()
        bh = (float(df["close"].iloc[-1]) / float(df["close"].iloc[0]) - 1) * 100
        print(
            f"\n  {c}: {len(df)}根日K {first}~{last} | "
            f"区间买入持有 {bh:+.1f}%"
        )
        for vdef in VARIANT_DEFS:
            trades = run_variant(df, vdef)
            by_variant[vdef["key"]][c] = trades
            net = sum(t["net_pnl_pct"] for t in trades)
            cand = count_candidates(df, vdef)
            print(
                f"    {vdef['label']:<34} 候选{cand:>3}次 → 成交{len(trades):>2}笔 "
                f"净合计 {net:+.2f}%"
            )

    # ---------- 汇总 ----------
    print("\n" + "=" * 92)
    print("按变体汇总（全部股票）")
    print("=" * 92)
    summary_rows = [
        ["变体", "交易数", "胜率%", "均净盈亏%", "累计净$", "盈亏比",
         "最大单亏%", "平均持仓天", "盈利标的/有效", "每票平均净$"]
    ]
    all_series: Dict[str, Optional[pd.Series]] = {}
    for vdef in VARIANT_DEFS:
        v = vdef["key"]
        all_tr = []
        for trades in by_variant[v].values():
            all_tr += trades
        s = summarize_trades(all_tr, vdef["label"])
        sym_usd = {
            c: round(sum(t["net_pnl_usd"] for t in trades), 2)
            for c, trades in by_variant[v].items()
        }
        eff_syms = {c: u for c, u in sym_usd.items() if c in by_variant[v] and by_variant[v][c]}
        n_pos = sum(1 for u in eff_syms.values() if u > 0)
        avg_sym = (
            round(sum(eff_syms.values()) / len(eff_syms), 0)
            if eff_syms else 0.0
        )
        summary_rows.append(
            [
                vdef["label"],
                str(s["n"]),
                fmt(s["win_rate"]),
                fmt(s["avg_net_pct"]),
                f"{s['total_net_usd']:,.0f}",
                fmt(s["profit_factor"]),
                fmt(s["max_loss_pct"]),
                fmt(s["avg_hold_days"]),
                f"{n_pos}/{len(eff_syms)}",
                f"{avg_sym:,.0f}",
            ]
        )
        # 累计已平仓盈亏曲线
        closed = sorted(
            [t for t in all_tr if not t["open"]],
            key=lambda t: t["exit_date"],
        )
        if closed:
            idx = pd.to_datetime([t["exit_date"] for t in closed])
            vals = np.cumsum([t["net_pnl_usd"] for t in closed])
            all_series[v] = pd.Series(vals, index=idx)
    print(render_table(summary_rows))

    # 每股票明细表
    print("\n" + "=" * 92)
    print("按股票 × 变体 交易数与累计净盈亏($)")
    print("=" * 92)
    grid_rows = [["股票"] + [v["label"] for v in VARIANT_DEFS]]
    for c in ind_data:
        row = [c]
        for vdef in VARIANT_DEFS:
            tr = by_variant[vdef["key"]][c]
            usd = sum(t["net_pnl_usd"] for t in tr)
            row.append(f"{len(tr)}笔/{usd:+,.0f}$")
        grid_rows.append(row)
    print(render_table(grid_rows))

    # ---------- 输出文件 ----------
    out_dir = os.path.join(PROJECT_ROOT, args.output_dir)
    os.makedirs(out_dir, exist_ok=True)
    md_path = os.path.join(out_dir, "donchian_breakout_report.md")
    csv_path = os.path.join(out_dir, "donchian_breakout_trades.csv")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# 唐奇安双通道突破回测报告\n\n")
        f.write(f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"- 数据源: 富途日K(前复权) {args.start} ~ {args.end}\n")
        f.write(f"- 标的: {', '.join(codes)}\n")
        f.write(f"- 假设: 2×ATR吊灯(14日ATR)+{FIXED_STOP_PCT*100:.0f}%保底 | "
                f"单笔本金 ${POSITION_USD:.0f} | 往返成本 {COST_ROUND_TRIP_PCT:.2f}%\n\n")
        f.write("> 入场规则：收盘价突破此前 N 日最高价（不含当日），次日开盘买入；"
                "未平仓交易按期末收盘计价（标注“期末未平仓”）。\n\n")
        f.write("## 汇总\n\n")
        md_table = ["| " + " | ".join(summary_rows[0]) + " |"]
        md_table.append("|" + "|".join(["---"] * len(summary_rows[0])) + "|")
        for r in summary_rows[1:]:
            md_table.append("| " + " | ".join(r) + " |")
        f.write("\n".join(md_table) + "\n")
        f.write("\n## 各股票 × 变体\n\n")
        md_table = ["| " + " | ".join(grid_rows[0]) + " |"]
        md_table.append("|" + "|".join(["---"] * len(grid_rows[0])) + "|")
        for r in grid_rows[1:]:
            md_table.append("| " + " | ".join(r) + " |")
        f.write("\n".join(md_table) + "\n")
        f.write("\n## 说明\n\n")
        f.write("- 慢通道(55)与快通道(20)均为标准海龟/唐奇安设置；放量定义为当日成交量≥此前20日均量的1.5倍。\n")
        f.write("- 周期内新股（如 SNDK）历史短，交易数少属正常；回测结论以数据充足的标的为主。\n")
        f.write("- 成本按每笔往返 0.2%（佣金+平台费+滑点）粗估，实盘确认页会再校验价格漂移。\n")
        f.write("- 本回测只验证“入场信号族”的历史统计，不等同于实盘推荐；"
                "下一步按现网流程接确认页并影子运行评估。\n")

    all_trade_rows = []
    for vdef in VARIANT_DEFS:
        for c, trades in by_variant[vdef["key"]].items():
            for t in trades:
                all_trade_rows.append(
                    {
                        "stock": c,
                        "variant": vdef["key"],
                        "variant_label": vdef["label"],
                        **t,
                    }
                )
    pd.DataFrame(all_trade_rows).to_csv(csv_path, index=False, encoding="utf-8-sig")

    if not args.no_plot:
        png_path = os.path.join(out_dir, "donchian_breakout_equity.png")
        plot_equity(all_series, png_path)
        print(f"\n图表已保存: {png_path}")
    print(f"报告已保存: {md_path}")
    print(f"交易明细: {csv_path}")


if __name__ == "__main__":
    main()
