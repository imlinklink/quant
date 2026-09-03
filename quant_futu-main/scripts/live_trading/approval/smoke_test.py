#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
人工确认页 - 独立 smoke test（不依赖 OpenD、不依赖交易时段）

启动一个孤立的 ApprovalServer，推几条模拟提案，让你直接看页面效果。

用法:
  python3 scripts/live_trading/approval/smoke_test.py
  然后浏览器打开 http://127.0.0.1:8899
"""
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from scripts.live_trading.approval.proposal_store import ProposalStore
from scripts.live_trading.approval.server import ApprovalServer


def main():
    print("=" * 60)
    print("🧪 人工确认页 - Smoke Test")
    print("=" * 60)

    # 用独立的 ProposalStore（单独的 log 目录）
    log_dir = Path(__file__).resolve().parent / '_smoke_logs'
    store = ProposalStore(ttl_seconds=180, log_dir=str(log_dir))

    server = ApprovalServer(
        store=store,
        host='127.0.0.1',
        port=8899,
        env='SIMULATE',
        market_type='HK',
        llm_enabled=True,
    )
    server.start()

    url = f"http://127.0.0.1:{server.bound_port}"
    print(f"\n✅ 确认页已启动: {url}")

    # ===== 推几条模拟提案 =====

    # 1. 规则通过 + LLM 也通过（最理想情况）
    store.create(
        stock_code='HK.00700',
        stock_name='腾讯控股',
        market_type='HK',
        env='SIMULATE',
        price=385.20,
        quantity=100,
        estimated_cost=38520.00,
        per_stock_capital=50000.00,
        entry_mode='bottom_fish',
        reason='动量得分 +0.18 R²=0.85 RSRS 斜率为正 25 日新高 ATR 收缩至 2.1%',
        kline_score=7,
        kline_signal='追涨趋势，RSI 58 BB 向上突破',
        llm={
            'model': 'deepseek-chat',
            'mode': 'shadow',
            'verdict': 'allow',
            'risk_level': 'LOW',
            'confidence': 0.87,
            'reason': '腾讯控股近日无重大负面新闻，基本面稳健，短期动量符合预期。港股整体风险 LOW。',
        },
        note='模拟数据 · case1: 理想情况',
    )

    # 2. 规则通过 + LLM 建议观望
    store.create(
        stock_code='HK.02318',
        stock_name='中国平安',
        market_type='HK',
        env='SIMULATE',
        price=48.76,
        quantity=1000,
        estimated_cost=48760.00,
        per_stock_capital=50000.00,
        entry_mode='bottom_fish',
        reason='动量得分 +0.12 R²=0.72 放量突破 MA20',
        kline_score=5,
        kline_signal='震荡上行，BB 下轨企稳',
        llm={
            'model': 'deepseek-chat',
            'mode': 'shadow',
            'verdict': 'watch',
            'risk_level': 'MEDIUM',
            'confidence': 0.62,
            'reason': '平安昨日公告回购计划，但行业整体进入震荡区间，建议观望确认方向后再入场。',
        },
        note='模拟数据 · case2: LLM 建议观望',
    )

    # 3. 规则通过 + LLM 建议否决（高置信度）
    store.create(
        stock_code='HK.01888',
        stock_name='建滔积层板',
        market_type='HK',
        env='SIMULATE',
        price=42.15,
        quantity=1000,
        estimated_cost=42150.00,
        per_stock_capital=50000.00,
        entry_mode='bottom_fish',
        reason='动量得分 +0.22 R²=0.91 近 5 日量价齐升',
        kline_score=8,
        kline_signal='追涨强买，放量突破前高',
        llm={
            'model': 'deepseek-chat',
            'mode': 'shadow',
            'verdict': 'veto',
            'risk_level': 'HIGH',
            'confidence': 0.81,
            'reason': '建滔积层板近日连续 3 日涨停后放量开板，疑似游资出货阶段。行业层面 PCB 板块已连涨一周，短期过热。建议至少等回调 5% 以上再考虑。',
        },
        note='模拟数据 · case3: LLM 强烈建议否决',
    )

    # 4. LLM 未启用（verdict=null）时的降级情况
    store.create(
        stock_code='HK.06082',
        stock_name='壁仞科技',
        market_type='HK',
        env='SIMULATE',
        price=152.30,
        quantity=300,
        estimated_cost=45690.00,
        per_stock_capital=50000.00,
        entry_mode='bottom_fish',
        reason='动量得分 +0.15 R²=0.78 半导体板块共振上涨',
        kline_score=6,
        kline_signal='趋势向上，缩量回踩 MA10',
        llm=None,
        note='模拟数据 · case4: LLM 未启用',
    )

    print(f"📨 已推送 4 条模拟提案")
    print(f"   1. HK.00700 腾讯控股 — LLM allow（理想）")
    print(f"   2. HK.02318 中国平安 — LLM watch（观望）")
    print(f"   3. HK.01888 建滔积层板 — LLM veto（强烈否决）")
    print(f"   4. HK.06082 壁仞科技 — LLM 未启用（降级）")
    print()
    print("🧭 打开浏览器访问确认页，试试点「下单」和「拒绝」按钮")
    print("⌨️  Ctrl+C 退出服务")
    print()

    # 保持服务运行
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 正在停止...")
        server.stop()
        # 打印最终状态
        items = store.get_all()
        print(f"\n📊 最终状态（{len(items)} 条提案）:")
        for item in items:
            print(f"   {item['stock_code']} → {item['status']} ({item.get('note','')[:30]})")
        print(f"\n📂 决策日志: {store._decision_log}")
        print("✅ 退出")


if __name__ == '__main__':
    main()
