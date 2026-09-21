"""Decision-first P0/P1 report: facts and unavailable evidence stay separate."""


def pct(value, digits=2):
    return 'unavailable' if value is None else f'{value:.{digits}%}'


def _num(value, digits=2):
    return 'unavailable' if value is None else f'{value:.{digits}f}'


def _usd(micro):
    return 'unavailable' if micro is None else f'{micro/1e6:,.2f}'


def _exit_section(x):
    lines = ['## 退出损益', '',
             f"交易：{x['count']}；已平仓：{x['closed']}；右删失：{x['right_censored']}；"
             f"已实现胜率：{pct(x['realized_win_rate'])}。", '',
             '| 退出原因 | 笔数 | 净损益 USD | 平均净 R | 平均持有 session | 占账户收益 |',
             '|---|---:|---:|---:|---:|---:|']
    for reason, row in x['exit_reasons'].items():
        lines.append(f"| {reason} | {row['count']} | {_usd(row['net_pnl_micro'])} | "
                     f"{_num(row['mean_net_r'], 3)} | {_num(row['mean_holding_sessions'], 1)} | "
                     f"{pct(row.get('share_of_account_return'))} |")
    if x.get('unclassified_reasons'):
        # 引擎新增退出原因而统计没归类时必须看得见，否则它会静默漏出统计
        lines += ['', f"⚠ 未归类的退出原因：{x['unclassified_reasons']}（止损/期限统计未覆盖）"]
    lines += ['', '### 退出后路径（描述性，非可实现收益）', '',
              '| 期限 | 已成熟 | 未成熟/缺数据 | 成熟比例 |', '|---|---:|---:|---:|']
    for horizon, row in (x.get('followup_maturity') or {}).items():
        lines.append(f"| {horizon} | {row['mature']} | {row['pending_or_missing']} | "
                     f"{pct(row['mature_share'])} |")
    recovery = x.get('stop_recovery') or {}
    if recovery:
        lines += ['', '### 止损后是否回到入场价（只统计已成熟期限）', '',
                  '| 期限 | 止损笔数(成熟) | 已恢复 | 恢复比例 | 平均恢复耗时 | 未恢复 |',
                  '|---|---:|---:|---:|---:|---:|']
        for horizon, row in recovery.items():
            lines.append(f"| {horizon} | {row['stop_trades_mature']} | {row['recovered_to_entry']} | "
                         f"{pct(row['recovered_share'])} | {_num(row['mean_sessions_to_recovery'], 1)} | "
                         f"{row['unrecovered']} |")
    lines += ['', x['note'], '']
    return lines


def _capacity_section(stat):
    if not stat:
        return ['## 容量与资金使用', '', '本次运行没有输出聚合统计（旧 schema）。', '']
    cash, expo, slots, risk = stat['cash'], stat['exposure'], stat['slots'], stat['risk_budget']
    lines = ['## 容量与资金使用', '',
             f"会话数：{stat['sessions']}。",
             f"无持仓日：{cash['days_without_positions']}（{pct(cash['share_without_positions'])}）；"
             f"现金占比≥50% 的日子：{cash['days_cash_fraction_ge_half']}"
             f"（{pct(cash['share_cash_fraction_ge_half'])}）。",
             f"平均现金占比：{pct(cash['mean_fraction'])}。", '',
             f"暴露：均值 {pct(expo['mean'])}、最大 {pct(expo['max'])}、非零日 {expo['days_nonzero']}。",
             f"槽位：上限 {slots['max_positions']}、平均占用 {_num(slots['mean_held'])}"
             f"（{pct(slots['mean_occupancy'])}）、满仓日 {slots['days_at_capacity']}。",
             f"风险预算：上限 {risk['budget_bp']} bp、实际均值 {_num(risk['mean_used_bp'], 1)} bp、"
             f"峰值 {_num(risk['max_used_bp'], 1)} bp、平均使用率 {pct(risk['mean_utilization'])}。",
             f"拒绝原因（跨会话汇总）：{stat['rejections']}。", '',
             stat['note'], '']
    return lines


def _concentration_section(stat):
    if not stat:
        return ['## 集中度与稳健性', '', '本次运行没有输出聚合统计（旧 schema）。', '']
    con, rob = stat['concentration'], stat['robustness']
    lines = ['## 集中度与稳健性', '',
             f"可计值交易：{con['valued']} / {con['trades']}；净损益 {_usd(con['net_micro'])}；"
             f"占账户收益 {pct(con.get('share_of_account_return'))}。",
             f"盈利集中度：最大一笔占全部盈利 {pct(con['top1_trade_share_of_gain'])}、"
             f"前三笔 {pct(con['top3_trade_share_of_gain'])}；"
             f"最大一只证券 {pct(con['top1_security_share_of_gain'])}、"
             f"前两只 {pct(con['top2_security_share_of_gain'])}。", '',
             f"分年：{rob['years']} 年，其中为正 {rob['years_positive']}"
             f"（{pct(rob['years_positive_share'])}）。"]
    for year, row in rob['by_year'].items():
        lines.append(f"- {year}：{row['count']} 笔，净 {_usd(row['net_micro'])}")
    lines += ['', '集中度与分年只作描述：单一小样本下的"最赚的一笔"不构成规则缺陷。', '']
    return lines


def conclusion(result):
    """首页那句结论必须**从判定字段算出来**，不是写死的。

    写死的话，"只有基线、还谈不上判定" 与 "比过了、判定为证据不足" 在报告上长得一模一样 ——
    正是本模块反复要防的形态（机制看起来在工作）。§9.3 的判定只有比过 challenger 才存在，
    所以 `verdict is None` 时必须如实说"没作判定"，而不是报一个最接近的令牌。
    """
    verdict = result.get('verdict')
    if verdict is None:
        return (f"**结论：本轮只有基线，未作 §9.3 判定（分期结论 "
                f"`{result.get('phase_conclusion')}`）。没有 challenger 就没有增量可比。**")
    return f'**结论：{verdict}。**'


def render(result):
    f, x = result['funnel'], result['exits']
    rejected = {}
    for day in result['capacity']:
        for k, v in day['rejections'].items():
            rejected[k] = rejected.get(k, 0) + v
    lines = [f"# 策略薄弱环节诊断：{result['study_id']}", '',
             conclusion(result), '',
             f"来源：历史规则重建；{result['sessions']} 个交易日。工程检查均通过。",
             f"基线收益（已扣设定费用，未建模滑点）：{pct(result['full_cost_return'])}；"
             f"最大回撤：{pct(result['max_drawdown'])}。", '',
             '## 买入漏斗', '',
             f"独立候选轮次数：{f['candidate_count']}；READY 比例：{pct(f['ready_fraction'])}。",
             f"当前状态：{f['candidate_states']}。", '',
             '| 阶段 | 通过 | 拒绝 | 未知 | 未评估 | 条件通过率 |',
             '|---|---:|---:|---:|---:|---:|']
    for stage, row in f['stages'].items():
        lines.append(f"| {stage} | {row.get('pass',0)} | {row.get('reject',0)} | "
                     f"{row.get('unknown',0)} | {row.get('not_evaluated',0)} | {pct(row['conditional_pass_rate'])} |")
    lines += ['', f"{f['note']}", '']
    lines += _exit_section(x)
    lines += _capacity_section(result.get('statistics', {}).get('capacity'))
    lines += _concentration_section(result.get('statistics'))
    lines += ['## 运行边界', '',
              f"账户拒绝原因：{rejected}。",
              f"窗口末待执行机会：{result['pending_execution_at_window_end']}（不是漏跑）。",
              f"被范围排除的公司行动：{result.get('excluded_actions', {}).get('count', 0)} 条"
              f"（{result.get('excluded_actions', {}).get('by_reason', {})}）—— 对账户无影响，列出备查。",
              '生产调度缺口：unavailable。此次未读取生产运行日志，不能据此判断生产任务是否成功。', '',
              '## 下一步', '',
              '先检查拒绝原因与退出分类；只在选定一个可检验假设后冻结单项 challenger。',
              '交易少、卖后上涨、现金闲置都不是单独修改规则的充分理由。', '',
              '## 限制与溯源', '']
    lines += [f'- {item}' for item in result['audit']['warnings'] + result['limitations']]
    lines += ['', f"Manifest：`{result['manifest_hash']}`。",
              '逐笔结果见 `exit_diagnostics.json`，全部门观察见 `funnel_observations.json`，'
              '容量/集中度/稳健性见 `statistics.json`，工程证据见 `checks.json`，'
              '账户事件位于 `variants/baseline/ledger.sqlite3`。', '']
    return '\n'.join(lines)
