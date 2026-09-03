#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 0: LLM 交易复盘脚本
==========================

零风险离线分析：把 trades.yaml 历史交易喂给 LLM，
让它解释每笔买卖逻辑、识别规则盲区、生成复盘总结。

修复记录 (2026-09-03):
  - 使用 CompatibleSafeLoader 正确解析 Decimal 标签
  - 自动配对 BUY/SELL 成交单计算盈亏（trades.yaml 是原始单，pnl_pct 为 None）
  - 复用 mutifactor.llm.LLMAdvisor，不再重复实现 client

用法:
  export DEEPSEEK_API_KEY=sk-xxx
  python3 scripts/review/llm_review_trades.py

可选参数:
  --limit 20          只复盘最近 20 笔
  --model doubao-pro-32k 换长上下文模型
  --only-losses       只复盘亏钱的交易
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mutifactor.infra.yaml_storage import yaml_storage, CompatibleSafeLoader
from mutifactor.llm.advisor import LLMAdvisor

import yaml

TRADES_FILE = PROJECT_ROOT / 'data' / 'trades.yaml'
OUTPUT_DIR = PROJECT_ROOT / 'data' / 'llm_decisions'


REVIEW_SYSTEM_PROMPT = """\
你是资深量化交易分析师，正在为自动化动量策略的历史交易做复盘分析。

规则策略使用：加权动量 + RSRS 趋势过滤 + ATR 动态止盈止损。
市场：港股。

你的任务：
1. 解释每笔交易为什么买、为什么卖（如果规则能解释，说清楚；如果不能，说盲区）
2. 亏钱的交易：有没有规则应该捕获但没捕获的信号？
3. 赚钱的交易：是策略有效还是运气好？能不能复制？
4. 总结规则体系的系统性优缺点

输出只给 JSON，分两部分:
{
  "per_trade": [
    {"trade_index": 1, "code": "...", "analysis": "...", "rule_covered": true/false}
  ],
  "summary": {
    "total_analyzed": 20,
    "win_rate": "45%",
    "key_findings": ["发现1", "发现2"],
    "suggested_improvements": ["建议1"]
  }
}
"""


def _d2f(v):
    """把 Decimal 或其他类型转 float"""
    if isinstance(v, Decimal):
        return float(v)
    return float(v) if v is not None else 0.0


def _side(t):
    """统一 direction/trade_type 两种字段，返回 BUY/SELL/None"""
    return (t.get('direction') or t.get('trade_type') or '').upper() or None


def load_and_pair_trades():
    """
    加载 trades.yaml 并按股票 FIFO 配对 BUY/SELL，计算每笔交易盈亏。

    修复 (2026-09-03):
      - field 兼容: 旧格式 trade_type / 新格式 direction
      - 时序约束: 卖单只能配它之前的买单，卖先于买则跳过（计入 unmatched_sells）
      - 统计未匹配买单（可能是还在持仓的买单）和未匹配卖单
    """
    raw_trades = yaml_storage.get_trades()
    if not raw_trades:
        print("⚠️ trades.yaml 为空")
        return []

    # 按股票分组，分 BUY 和 SELL，各自按 trade_time 升序
    by_code = defaultdict(list)
    for t in raw_trades:
        by_code[t.get('stock_code', '')].append(t)

    paired = []
    unmatched_sells = 0  # 卖单没有更早的买单可配
    unmatched_buys = 0    # 买单没被配完（= 当前持仓）

    for code, trades in by_code.items():
        # 分 side 并按时间升序
        all_buys = sorted(
            [t for t in trades if _side(t) == 'BUY'],
            key=lambda t: t.get('trade_time', '')
        )
        all_sells = sorted(
            [t for t in trades if _side(t) == 'SELL'],
            key=lambda t: t.get('trade_time', '')
        )

        # 维护一个买单池，存 (trade_time, qty, price, trade_dict)
        # 随时间推进，每个 sell 只从它之前的买单里取
        buy_pool = []
        sell_idx = 0

        # 按时间顺序扫所有成交单（BUY 和 SELL 混合排）
        all_trades_sorted = sorted(trades, key=lambda t: t.get('trade_time', ''))
        for trade in all_trades_sorted:
            side = _side(trade)
            if side == 'BUY':
                qty = _d2f(trade.get('quantity', trade.get('qty', 0)))
                buy_pool.append({
                    'time': trade.get('trade_time', ''),
                    'remaining': qty,
                    'price': _d2f(trade.get('price', 0)),
                    'trade': trade,
                })
            elif side == 'SELL':
                sell_qty = _d2f(trade.get('quantity', trade.get('qty', 0)))
                sell_price = _d2f(trade.get('price', 0))
                sell_time = trade.get('trade_time', '')

                # 只从 buy_pool 里选 time <= sell_time 的买单（保证时序正确）
                for buy in buy_pool:
                    if sell_qty <= 0:
                        break
                    if buy['remaining'] <= 0:
                        continue

                    matched_qty = min(buy['remaining'], sell_qty)
                    cost = buy['price'] * matched_qty
                    revenue = sell_price * matched_qty
                    pnl = revenue - cost
                    pnl_pct = pnl / cost if cost > 0 else 0

                    try:
                        d1 = datetime.strptime(buy['time'][:10], '%Y-%m-%d')
                        d2 = datetime.strptime(sell_time[:10], '%Y-%m-%d')
                        hold_days = (d2 - d1).days
                    except Exception:
                        hold_days = 0

                    paired.append({
                        'stock_code': code,
                        'stock_name': trade.get('stock_name', '') or buy['trade'].get('stock_name', ''),
                        'buy_date': buy['time'],
                        'sell_date': sell_time,
                        'buy_price': buy['price'],
                        'sell_price': sell_price,
                        'quantity': matched_qty,
                        'pnl': pnl,
                        'pnl_pct': pnl_pct,
                        'holding_days': hold_days,
                        'exit_reason': trade.get('exit_reason', trade.get('reason', '未记录')),
                    })

                    buy['remaining'] -= matched_qty
                    sell_qty -= matched_qty

                if sell_qty > 0:
                    unmatched_sells += 1

        # 剩下的买单 = 当前持仓
        for buy in buy_pool:
            if buy['remaining'] > 0:
                unmatched_buys += 1

    if unmatched_sells:
        print(f"⚠️ {unmatched_sells} 笔卖单找不到更早的买单（可能文件之外的持仓）")
    if unmatched_buys:
        print(f"📦 {unmatched_buys} 笔买单未配对（当前持仓）")

    paired.sort(key=lambda t: t.get('sell_date', ''), reverse=True)
    return paired


def build_review_prompt(trades):
    """把交易记录格式化给 LLM"""
    lines = []
    for i, t in enumerate(trades, 1):
        code = t.get('stock_code', '?')
        buy_date = t.get('buy_date', '?')
        sell_date = t.get('sell_date', '?')
        buy_price = t.get('buy_price', '?')
        sell_price = t.get('sell_price', '?')
        pnl_pct = t.get('pnl_pct', 0)
        hold_days = t.get('holding_days', '?')
        exit_reason = t.get('exit_reason', '未记录')

        pnl_str = f"{pnl_pct*100:+.2f}%" if isinstance(pnl_pct, (int, float)) else str(pnl_pct)

        lines.append(
            f"交易#{i} {code}: 买={buy_date}@{buy_price} "
            f"卖={sell_date}@{sell_price} 盈亏={pnl_str} "
            f"持仓={hold_days}天 卖出原因={exit_reason}"
        )

    return "以下是策略的历史已平仓交易记录（FIFO 配对）：\n\n" + "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='Phase 0: LLM 交易复盘')
    parser.add_argument('--limit', type=int, default=15, help='复盘最近 N 笔')
    parser.add_argument('--model', default='deepseek-chat', help='模型名')
    parser.add_argument('--only-losses', action='store_true', help='只复盘亏钱的交易')
    args = parser.parse_args()

    print("=" * 60)
    print("📊 Phase 0: LLM 交易复盘（独立脚本，不依赖实盘）")
    print(f"  模型: {args.model}")
    print(f"  复盘: 最近 {args.limit} 笔 {'(只亏钱)' if args.only_losses else '(全部)'}")
    print("=" * 60)

    paired = load_and_pair_trades()
    if not paired:
        print("❌ 没有配对到已平仓交易")
        return

    if args.only_losses:
        paired = [t for t in paired if t.get('pnl_pct', 0) < 0]

    trades = paired[:args.limit]
    print(f"\n📂 已平仓交易 {len(paired)} 笔，取最近 {len(trades)} 笔")

    # 打印一下让用户看到
    for i, t in enumerate(trades, 1):
        pnl = t.get('pnl_pct', 0)
        pnl_str = f"{pnl*100:+.2f}%" if isinstance(pnl, (int, float)) else str(pnl)
        print(f"  #{i} {t['stock_code']} 盈亏={pnl_str}")

    if not trades:
        print("⚠️ 过滤后无交易")
        return

    # 统计
    wins = sum(1 for t in trades if t.get('pnl_pct', 0) > 0)
    losses = len(trades) - wins
    win_rate = wins / len(trades) * 100 if trades else 0
    print(f"\n📈 胜率 {win_rate:.0f}% ({wins}胜 {losses}负)")

    # 调用 LLM
    print("\n🤖 调用 LLM 分析中...")
    prompt = build_review_prompt(trades)

    # 复用 LLMAdvisor —— _expand_env 会从 DEEPSEEK_API_KEY 环境变量取 key
    advisor = LLMAdvisor({'enabled': True, 'api_key': '${DEEPSEEK_API_KEY}', 'model': args.model, 'timeout': 120})

    if not advisor.enabled:
        print("❌ 未设置 DEEPSEEK_API_KEY 环境变量（或值未展开）")
        return

    result = advisor.chat(prompt, expect_json=True, system=REVIEW_SYSTEM_PROMPT)

    if result is None:
        print("❌ LLM 调用失败")
        return

    # 输出
    print("\n" + "=" * 60)
    print("📝 复盘结果")
    print("=" * 60)

    summary = result.get('summary', {})
    print(f"\n📈 总览: 分析 {summary.get('total_analyzed', len(trades))} 笔, "
          f"胜率 {summary.get('win_rate', f'{win_rate:.0f}%')}")

    findings = summary.get('key_findings', [])
    if findings:
        print("\n🔑 关键发现:")
        for f in findings:
            print(f"  • {f}")

    improvements = summary.get('suggested_improvements', [])
    if improvements:
        print("\n💡 改进建议:")
        for s in improvements:
            print(f"  • {s}")

    # 保存
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / f"review_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml"
    with open(out_file, 'w', encoding='utf-8') as f:
        yaml.dump({
            'timestamp': datetime.now().isoformat(),
            'model': args.model,
            'win_rate': f'{win_rate:.1f}%',
            'trade_count': len(trades),
            'trades': trades,
            'result': result,
        }, f, allow_unicode=True, sort_keys=False)

    print(f"\n💾 完整结果已保存: {out_file}")


if __name__ == '__main__':
    main()
