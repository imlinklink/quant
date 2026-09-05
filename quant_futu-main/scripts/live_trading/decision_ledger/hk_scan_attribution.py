"""港股扫描归因（评估闭环）：查看买入评分与卖出决策的前向收益表现。

用法：
    python scripts/live_trading/decision_ledger/hk_scan_attribution.py
输出：控制台 + data/decision_ledger/hk_scan_attribution.md
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

BASE_DIR = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BASE_DIR))

import pandas as pd  # noqa: E402

from scripts.live_trading.decision_ledger import scan_ledger  # noqa: E402


def _fmt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return '-'
    return f'{v:.2f}'


def main():
    checks = scan_ledger.load_checks()
    if not checks:
        print('暂无扫描数据（交易时段买入/持仓检查才会产生）')
        return
    outcomes: Dict[str, Dict] = {}
    for o in scan_ledger.load_outcomes():
        outcomes[o['scan_id']] = o
    rows = []
    for c in checks:
        o = outcomes.get(c.get('scan_id'))
        if not o or int(o.get('bars_after') or 0) < 3:
            continue
        rows.append({**c, **o})
    if not rows:
        print('暂无足够回填样本，先运行 backfill_hk_checks.py')
        return
    df = pd.DataFrame(rows)
    df['r3'] = pd.to_numeric(df.get('r3'), errors='coerce')
    df = df[df['r3'].notna()].copy()
    if df.empty:
        print('3日后收益尚无样本')
        return
    df['__all__'] = '__all__'  # 供“整体”分组使用

    def stats(g):
        return pd.Series({
            'n': int(len(g)),
            '均值%': round(float(g['r3'].mean()), 3),
            '中位%': round(float(g['r3'].median()), 3),
            '上涨%': round(float((g['r3'] > 0).mean() * 100), 1),
        })

    lines = [f'# 港股扫描归因（3日后收益）\n',
             f'- 生成: {datetime.now().strftime("%Y-%m-%d %H:%M")}',
             f'- 有效样本: {len(df)}\n']

    def add(title, group_col, mapping=None):
        g = df.groupby(group_col).apply(stats).reset_index()
        if mapping:
            g['组'] = g[group_col].map(mapping).fillna(g[group_col].astype(str))
        else:
            g['组'] = g[group_col].astype(str)
        g = g[['组', 'n', '均值%', '中位%', '上涨%']]
        lines.append(f'## {title}\n')
        lines.append('| 组 | n | 均值% | 中位% | 上涨% |')
        lines.append('|---|---|---|---|---|')
        for _, r in g.iterrows():
            lines.append(f"| {r['组']} | {r['n']} | {_fmt(r['均值%'])} | "
                         f"{_fmt(r['中位%'])} | {_fmt(r['上涨%'])} |")
        lines.append('')
        print(f'\n{title}\n{g.to_string(index=False)}')

    buy = df[df['kind'] == 'buy_check']
    exit_ = df[df['kind'] == 'exit_check']
    if not buy.empty:
        add('买入检查-整体', '__all__', mapping={'__all__': '全部'})
        add('买入检查-是否达阈值', 'outcome',
            mapping={'above': '达阈值(会买)', 'below': '未达阈值'})
        for col, name in (('score', '总分'), ('rsi_score', 'RSI分'),
                          ('bb_score', '布林分'), ('volume_score', '量分'),
                          ('candle_score', '形态分')):
            if col in buy.columns and buy[col].notna().any():
                add(f'买入检查-{name}', col)
    if not exit_.empty:
        add('卖出检查-是否触发卖出', 'should_exit',
            mapping={True: '触发卖出(待确认)', False: '继续持有'})
        top = (exit_[exit_['should_exit'] == True]['reason']  # noqa: E712
               .value_counts().head(6))
        if not top.empty:
            lines.append('## 触发卖出原因 Top\n')
            lines.append('| 原因 | 次数 |')
            lines.append('|---|---|')
            for reason, cnt in top.items():
                lines.append(f'| {str(reason)[:80]} | {cnt} |')
            lines.append('')
            print('\n触发卖出原因 Top\n', top.to_string())

    out_path = BASE_DIR / 'data' / 'decision_ledger' / 'hk_scan_attribution.md'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text('\n'.join(lines), encoding='utf-8')
    print(f'\n报告已保存: {out_path}')


if __name__ == '__main__':
    main()
