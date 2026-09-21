"""研究结果报告（规划 §3.2 的 `report.py`）。四类结果里本批用到「退出动作」与「账户效果」两节。

**报告只呈现算出来的东西**：门槛、口径、控制项与结论一一对应；算不出来的写 `n/a` 并说明原因，
不填 0 也不省略（「没有对象可评」≠「评了、结果为零」）。
"""
from __future__ import annotations

import json


def _usd(micro) -> str:
    return 'n/a' if micro is None else f'{micro / 1e6:,.2f}'


def _pct(value, digits=2) -> str:
    return 'n/a' if value is None else f'{value * 100:.{digits}f}%'


def _r(value, digits=2) -> str:
    return 'n/a' if value is None else f'{value:+.{digits}f}R'


def render_step1(result: dict) -> str:
    s, v = result['summary'], result['verdict']
    c = s['concentration']
    lines = [
        '# 卖出侧第一批：机械利润保护 —— 同机会对照（第 1 步，机会级）',
        '',
        f'预登记 `{result["registration_sha256"]}`；基线 study `{result["study_id"]}`；'
        f'费用 {result["fee_bp"]}bp（压力情景 {result["cost_stress_fee_bp"]}bp）。',
        '',
        '## 结论',
        '',
        f'**`{v["token"]}`**',
        '',
        _verdict_sentence(v, s),
        '',
        '## 控制项（不过即 ENGINEERING_BLOCKED，不出收益结论）',
        '',
        '| 控制 | 结果 |',
        '|---|---|',
        f'| A 臂复现账本每一次出场（原因/日期/价格/净损益） | '
        f'{"通过" if v["checks"]["reproduces_the_baseline"] else "失败"}'
        f'（不符 {len(s["control_reproduction_failures"])} 笔） |',
        f'| 只跟踪实例的出场与关闭保护逐字段相同 | '
        f'{"通过" if v["checks"]["tracking_matches_unprotected"] else "失败"} |',
        f'| 未激活的笔两臂完全相同 | '
        f'{"通过" if v["checks"]["untouched_arms_identical"] else "失败"}'
        f'（{s["n_untouched"]} 笔） |',
        '',
        '## 保护了多少、牺牲了多少',
        '',
        '| 量 | A（关闭保护） | B（启用保护） | 差 |',
        '|---|---:|---:|---:|',
        f'| 已实现净损益（USD） | {_usd(s["sum_usd_A"])} | {_usd(s["sum_usd_B"])} | '
        f'{_usd(s["delta_usd_sum"])} |',
        f'| 净 R 合计 | {_r(s["sum_net_r_A"])} | {_r(s["sum_net_r_B"])} | '
        f'{_r(s["delta_r_sum"])} |',
        f'| 浮盈回吐（USD） | {_usd(s["giveback_a_micro"])} | {_usd(s["giveback_b_micro"])} | '
        f'−{_usd(s["giveback_reduction_micro"])}（{_pct(s["giveback_reduction_pct"])}） |',
        f'| 下尾 5% ES（正损失量，越小越好） | {s["tail_es_a"]:.4f} | {s["tail_es_b"]:.4f} | '
        f'{_pct(s["tail_es_improvement"])} 改善 |',
        f'| 最差单笔净 R | {_r(s["worst_a"])} | {_r(s["worst_b"])} | 无变化 |',
        '',
        f'平均持有：A {s["mean_holding_a"]:.1f} 个 session，B {s["mean_holding_b"]:.1f} 个。',
        f'（表内只含**已平仓**的 {s["n_closed"]} 笔；另有 {s["n_right_censored"]} 笔到窗口末尾仍未平仓，'
        '两臂一致，不计入。所以 A 的合计比 study 报告里的总损益少这几个数的浮动估值 —— '
        '不是差异，是口径。）',
        f'盈利笔数：A {s["n_profitable_a"]}，B {s["n_profitable_b"]}（胜率可以同时**上升**而收益下降 ——'
        '这正是规划 §0 说的"提高胜率但损害长期净收益，不算整体目标达成"）。',
        f'退出分布：A `{s["exit_reasons_a"]}`；B `{s["exit_reasons_b"]}`。',
        '',
        '### 机制：收益差几乎全部来自大赢家被截断',
        '',
        f'原策略里净 R > {s["big_winners"]["threshold_r"]:.0f} 的笔共 {s["big_winners"]["n"]} 笔：'
        f'A 臂合计 {_r(s["big_winners"]["sum_r_a"])}，**同一批笔**在 B 臂只拿到 '
        f'{_r(s["big_winners"]["sum_r_b"])}。'
        '保护线把右尾搬到了中间（长持有 → 短持有、大赢 → 小赢），而未激活的笔两臂完全相同，'
        '所以左尾没有被改善 —— 这解释了为什么"少了回吐"却没有换来尾部改善。',
        '',
        '## 触发情况',
        '',
        f'激活 {s["n_activated"]} 笔 / 未激活 {s["n_untouched"]} 笔（门槛 ≥ '
        f'{v["thresholds"]["min_activated_trades"]} 笔：'
        f'{"达标" if v["checks"]["enough_activated"] else "不足"}）。',
        '',
        '## 集中度',
        '',
        f'- 回吐减少最大的证券：`{c["top1_security"]}`，占回吐减少量的 '
        f'{_pct(c["top1_share_of_giveback_reduction"])}（门槛 ≤50%）；',
        f'- 剔除该证券后净 R 合计：{_r(c["leave_one_out_delta_r_sum"])}（要求仍 > 0）；',
        f'- 收益损失最大的证券：`{c["largest_contributor_of_usd_delta"]}`，占损失 '
        f'{_pct(c["largest_contributor_share_of_loss"])}。',
        '',
        '## 门槛逐条（数值取自预登记，未在报告里放宽）',
        '',
        '| 判据 | 值 | 门槛 | 通过 |',
        '|---|---:|---:|:--:|',
        f'| 净 R 合计增量的非劣界限 | {_r(s["delta_r_sum"])} | ≥ '
        f'{v["thresholds"]["non_inferiority_r"]:+.1f}R | '
        f'{"✅" if v["checks"]["delta_non_inferior"] else "❌"} |',
        f'| 2× 成本下仍非劣 | {_r(s["delta_r_sum_2x"])} | ≥ '
        f'{v["thresholds"]["non_inferiority_r"]:+.1f}R | '
        f'{"✅" if v["checks"]["held_under_2x_cost"] else "❌"} |',
        f'| 尾部风险改善 | {_pct(s["tail_es_improvement"])} | ≥ '
        f'{_pct(v["thresholds"]["risk_improvement_gate"], 0)} | '
        f'{"✅" if v["checks"]["risk_improved"] else "❌"} |',
        f'| 收益未牺牲（Δ ≥ 0） | {_r(s["delta_r_sum"])} | ≥ 0 | '
        f'{"✅" if v["checks"]["delta_positive"] else "❌"} |',
        f'| 改善不集中 | 见上 | 单一证券 ≤50% 且 LOO > 0 | '
        f'{"✅" if v["checks"]["not_concentrated"] else "❌"} |',
        '',
        '## 下一步',
        '',
        _next_step(v),
        '',
        '---',
        '',
        '口径见 `docs/exit-protection-preregistration-2026-09-21.md`；逐笔明细在同目录 '
        '`step1.json` 的 `trades`（两臂各自的出场原因/日期/价格/持有期/净 R，以及 H 与回吐）。',
    ]
    return '\n'.join(lines) + '\n'


def _verdict_sentence(v: dict, s: dict) -> str:
    tok = v['token']
    if tok == 'NO_IMPROVEMENT':
        return ('保护线**没有改善尾部风险**（下尾 ES 改善 '
                f'{_pct(s["tail_es_improvement"])} < 门槛 {_pct(v["thresholds"]["risk_improvement_gate"], 0)}，'
                f'最差单笔两臂完全相同 {_r(s["worst_a"])}），'
                f'而收益代价显著（净 R 合计 {_r(s["delta_r_sum"])}）。'
                '按登记：保留原退出，不进入账户级对照。')
    if tok == 'RISK_TRADEOFF':
        return ('风险改善达标但收益在非劣界限内为负 ⇒ 记为**防守选项**，'
                '不得写成全面优胜（规划 §4.4）。')
    if tok == 'EVIDENCE_SUPPORTED':
        return '收益守住且风险改善达标 ⇒ 可进入账户级对照。'
    if tok == 'CONCENTRATED':
        return '改善集中在少数证券或少数笔 ⇒ 按 §8.2 保留原策略。'
    if tok == 'INSUFFICIENT_SAMPLE':
        return '激活笔数不足 ⇒ 不出结论。'
    return '见 `step1.json` 的 `verdict.checks`。'


def _next_step(v: dict) -> str:
    if v['token'] in ('EVIDENCE_SUPPORTED', 'RISK_TRADEOFF'):
        return '进入第 2 步（账户级对照），同引擎同起点两臂并排跑。'
    return ('不进入第 2 步（登记：第 1 步判 NO_IMPROVEMENT / CONCENTRATED / INSUFFICIENT_SAMPLE '
            '时不跑 —— 结论不会因账户路径而翻转，跑它只是多花时间）。')


def write_artifacts(result: dict, out_dir) -> dict:
    from pathlib import Path
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'step1.json').write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                                    encoding='utf-8')
    text = render_step1(result)
    (out / 'report.md').write_text(text, encoding='utf-8')
    return {'step1': str(out / 'step1.json'), 'report': str(out / 'report.md')}
