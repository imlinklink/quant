#!/usr/bin/env python3
"""读宏观日报(盘前+盘后) → 大模型生成美股/港股候选 → 存共享建议文件。

用法：
    export DEEPSEEK_API_KEY=sk-xxx   # 或已写进 config.yaml
    python scripts/live_trading/llm_suggestions/run_suggestions.py
    python scripts/live_trading/llm_suggestions/run_suggestions.py --dirs /path/to/reports
    python scripts/live_trading/llm_suggestions/run_suggestions.py --demo  # 不调 API 的自测
"""
import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BASE_DIR))

from scripts.live_trading.llm_suggestions import picker, report_reader, save_latest

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('run_suggestions')

DEFAULT_DIRS = [
    '/Users/wh1817w/WorkBuddy/2026-08-16-13-46-46/output',
    '/Users/wh1817w/Documents',
    '/Users/wh1817w/Documents/github/mySkill/report-result',
]


def main():
    parser = argparse.ArgumentParser(description='LLM 选股建议生成器')
    parser.add_argument('--dirs', nargs='*', default=DEFAULT_DIRS, help='日报所在目录')
    parser.add_argument('--demo', action='store_true', help='自测模式：不调 LLM，生成示例候选')
    args = parser.parse_args()

    logger.info('查找最近的盘前/盘后宏观日报...')
    pre_path = report_reader.find_latest_report(args.dirs, 'pre')
    post_path = report_reader.find_latest_report(args.dirs, 'post')
    logger.info(f'盘前日报: {pre_path}')
    logger.info(f'盘后日报: {post_path}')
    if pre_path is None and post_path is None:
        logger.error('没有找到任何日报 HTML，请用 --dirs 指定目录')
        return 1

    pre_text = report_reader.html_to_text(pre_path) if pre_path else ''
    post_text = report_reader.html_to_text(post_path) if post_path else ''
    logger.info(f'提取文本: 盘前 {len(pre_text)} 字符 / 盘后 {len(post_text)} 字符')

    if args.demo:
        result = {
            'ok': True,
            'data': {
                'summary': '（demo）日报显示风险偏好回升，关注顺周期与AI链。',
                'candidates': [
                    {'id': 'sug-1', 'market': 'US', 'code': 'US.SOXL', 'name': '半导体3x',
                     'direction': '多头', 'rationale': 'demo: 日报提到AI资本开支超预期',
                     'catalyst': '财报/订单', 'risks': '高波动', 'confidence': 0.6, 'horizon': '数日',
                     'status': 'pending'},
                    {'id': 'sug-2', 'market': 'HK', 'code': 'HK.00981', 'name': '中芯国际',
                     'direction': '观察', 'rationale': 'demo: 半导体链景气',
                     'catalyst': '国产替代', 'risks': '估值', 'confidence': 0.55, 'horizon': '数周',
                     'status': 'pending'},
                ],
            },
        }
    else:
        result = picker.generate(pre_text, post_text)

    if not result.get('ok'):
        logger.error(f"生成失败: {result.get('error')}")
        if result.get('data'):
            logger.error(f"原始返回: {str(result['data'])[:500]}")
        return 1

    data = result['data']
    payload = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'reports': {
            'pre': str(pre_path) if pre_path else None,
            'post': str(post_path) if post_path else None,
        },
        'summary': data.get('summary', ''),
        'candidates': data.get('candidates', []),
    }
    path = save_latest(payload)

    print()
    print('=' * 60)
    print('📋 大模型选股建议（已保存到共享建议文件）')
    print(f'   文件: {path}')
    print(f'   宏观结论: {payload["summary"]}')
    print('=' * 60)
    for c in payload['candidates']:
        print(f"[{c['market']}] {c['code']} {c.get('name', '')} | {c.get('direction', '')} "
              f"| conf={c.get('confidence', 0):.2f} | {c.get('horizon', '')}")
        print(f"   理由: {c.get('rationale', '')[:120]}")
    print()
    print('打开确认页查看：')
    print('  http://127.0.0.1:8899/suggestions   (美股系统)')
    print('  http://127.0.0.1:8899/suggestions   (港股系统运行时)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
