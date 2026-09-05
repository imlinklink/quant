#!/usr/bin/env python3
"""生成今日盘前市场状态简报（LLM 每日"市场会议"）。

用法（每天盘前日报出来后运行一次）：
    python scripts/live_trading/run_market_brief.py

会把结果写到本系统的 data/market_brief/latest.json，
确认页顶部会显示；buy_frequency=avoid 时当天暂停新买入提案。
"""
import argparse
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

from scripts.live_trading import market_brief
from scripts.live_trading.llm_suggestions import report_reader

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('run_market_brief')

DEFAULT_DIRS = [
    '/Users/wh1817w/WorkBuddy/2026-08-16-13-46-46/output',
    '/Users/wh1817w/Documents',
    '/Users/wh1817w/Documents/github/mySkill/report-result',
]


def main():
    parser = argparse.ArgumentParser(description='盘前市场状态简报生成器')
    parser.add_argument('--dirs', nargs='*', default=DEFAULT_DIRS, help='日报所在目录')
    args = parser.parse_args()

    logger.info('查找最新盘前/盘后日报...')
    pre = report_reader.find_latest_report(args.dirs, 'pre')
    post = report_reader.find_latest_report(args.dirs, 'post')
    logger.info(f'盘前: {pre}')
    logger.info(f'盘后: {post}')
    if pre is None and post is None:
        print('❌ 没找到日报，请用 --dirs 指定目录')
        return 1

    pre_text = report_reader.html_to_text(pre) if pre else ''
    post_text = report_reader.html_to_text(post) if post else ''
    brief = market_brief.generate_brief(pre_text, post_text)
    if brief.get('error'):
        logger.error(f"生成失败: {brief['error']}")
        return 1

    payload = {
        # 先展开 brief（包含 generated_at/date/risk_level/... 以及占位的 used_reports），
        # 再用真实报告路径覆盖 used_reports，避免被 brief 里的 None 占位覆盖。
        **brief,
        'used_reports': {'pre': str(pre) if pre else None, 'post': str(post) if post else None},
    }
    path = market_brief.save_brief(payload)

    # 写入本系统评估账本（供周报统计档位分布）
    try:
        from scripts.live_trading.decision_ledger import ledger
        ledger.record(
            'market_brief',
            date=payload['date'],
            risk_level=brief['risk_level'],
            buy_frequency=brief['buy_frequency'],
            suggested_position_ratio=brief['suggested_position_ratio'],
            risk_note=brief['risk_note'],
        )
    except Exception as e:
        logger.warning(f'账本记录失败: {e}')

    print()
    print('=' * 60)
    print('📋 今日市场状态简报（已保存）')
    print(f'   文件: {path}')
    print(f"   风险档位: {brief['risk_level']}")
    print(f"   买入频率: {brief['buy_frequency']}")
    print(f"   建议单票仓位: {brief['suggested_position_ratio'] or '不调整'}")
    print(f"   理由: {brief['risk_note']}")
    print('=' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
