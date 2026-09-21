"""描述性统计：容量与资金使用、集中度、稳健性。

**这里没有"配对区间"** —— §11.1 把它列在本模块名下，但配对比较需要 challenger；
P0/P1 只有基线，算不出 L−R 之类的配对差异。等 §8.2 冻结一个单项 challenger 后再补，
**不先造一个永远返回空的东西**（那正是本项目反复出现的"声明了但不干活"）。

三条纪律：

1. **算不出来就是 `None`，不是 0。** 零分母（没有已平仓交易、没有盈利交易）与
   "算了但结果是零"是两件事，报告侧靠 `None` 区分（§14「零分母为 unavailable」）。
2. **不把描述性统计说成结论。** 现金闲置 ≠ 损失、卖后上涨 ≠ 卖早了（§3 的
   "不足以证明它的现象"一列）。
3. **阈值是标签不是发现。** `idle_threshold` 只是给分布起个名字，报告里要写清它是标签。
"""
from collections import Counter, defaultdict


def _micro(value):
    return None if value is None else value / 1e6


def capacity_summary(rows, risk_policy):
    """§7.2：现金闲置、暴露、槽位占用、风险预算使用、拒绝原因计数。

    `rows` 是 `experiments._run` 逐日采集的 capacity 记录。原先它们**只被采集、从未聚合**
    （2026-09-20 实测：19 个 session 的 7 个字段都在 comparison.json 里，而报告那一段是空的）
    —— 采集了不等于报出来了。
    """
    # 空样本**不换形状**：`{'sessions': 0, 'note': …}` 缺了 cash/exposure/slots/risk_budget，
    # report 取 `stat['cash']` 会 KeyError。这是本模块第一条纪律（算不出来是 None）的反面 ——
    # 非空分支做到了，空分支却换了形状。故这里不留早退，分母一律带守卫。
    total = len(rows)
    exposures = [r['exposure'] for r in rows if r.get('exposure') is not None]
    cash = [r['cash_fraction'] for r in rows if r.get('cash_fraction') is not None]
    held = [r['held_positions'] for r in rows]
    max_positions = risk_policy.get('max_positions') or None
    rejected = Counter()
    for r in rows:
        for reason, count in (r.get('rejections') or {}).items():
            rejected[reason] += count
    # 风险预算：每仓风险上限 × 最大仓位数（与 portfolio 侧的 max_total_risk_bp 同一推导）。
    budget_bp = None
    per_name = risk_policy.get('single_position_risk_bp')
    if per_name and max_positions:
        budget_bp = per_name * max_positions
    used_bp = []
    for r in rows:
        equity = r.get('equity_micro')
        if equity and r.get('risk_used_micro') is not None:
            used_bp.append(r['risk_used_micro'] / equity * 10_000)
    flat_days = sum(1 for r in rows if r['held_positions'] == 0)
    high_cash_days = sum(1 for r in rows if (r.get('cash_fraction') or 0) >= 0.5)
    return {
        'sessions': total,
        'cash': {
            'mean_fraction': sum(cash) / len(cash) if cash else None,
            'days_without_positions': flat_days,
            'share_without_positions': flat_days / total if total else None,
            # 0.5 只是给分布起个名字，**不是**"闲置过多"的判定（§7.2：现金闲置不自动是损失）
            'days_cash_fraction_ge_half': high_cash_days,
            'share_cash_fraction_ge_half': high_cash_days / total if total else None,
        },
        'exposure': {
            'mean': sum(exposures) / len(exposures) if exposures else None,
            'max': max(exposures) if exposures else None,
            'days_nonzero': sum(1 for e in exposures if e > 0),
        },
        'slots': {
            'max_positions': max_positions,
            'mean_held': sum(held) / total if total else None,
            'mean_occupancy': (sum(held) / total / max_positions) if max_positions and total else None,
            'days_at_capacity': sum(1 for h in held if max_positions and h >= max_positions),
        },
        'risk_budget': {
            'budget_bp': budget_bp,
            'mean_used_bp': sum(used_bp) / len(used_bp) if used_bp else None,
            'max_used_bp': max(used_bp) if used_bp else None,
            'mean_utilization': (sum(used_bp) / len(used_bp) / budget_bp) if used_bp and budget_bp else None,
        },
        'rejections': dict(rejected),
        'note': '现金闲置与低暴露是描述，不是损失结论（§7.2）。',
    }


def concentration(trades, initial_cash_micro):
    """集中度：盈利是否由个别交易/证券决定（§6.2、§11.2「前三笔、前两只利润集中度」）。

    **口径**：分母是全部**盈利**之和；分子是**盈利**里最大的前 N 个（按证券时=先按证券
    汇总其**盈利**，再取前 N）。亏损一律不进分子。

    原先分子取的是"净损益最大的前 N 笔/只"，其中可以含亏损 —— 于是"前三笔盈利占比"
    在一笔赚 100、一笔亏 90 时返回 **10%**（(100−90)/100），而全部盈利都来自那一笔，
    应当是 **100%**。分子含亏损会把集中度**系统性低估**，而集中度正是用来判断
    "这点收益是不是靠个别标的"的指标。

    只统计**有净损益**的交易；右删失且无估值的不计入分母（算不出来就是 `None`）。
    """
    valued = [t for t in trades if t.get('net_pnl_micro') is not None]
    gains = [t['net_pnl_micro'] for t in valued if t['net_pnl_micro'] > 0]
    total_gain = sum(gains)
    gain_by_security = defaultdict(int)
    net_by_security = defaultdict(int)
    for t in valued:
        net_by_security[t['security_id']] += t['net_pnl_micro']
        if t['net_pnl_micro'] > 0:
            gain_by_security[t['security_id']] += t['net_pnl_micro']
    top_trades = sorted(gains, reverse=True)
    top_securities = sorted(gain_by_security.values(), reverse=True)
    net_securities = sorted(net_by_security.items(), key=lambda kv: kv[1], reverse=True)

    def share(values, n):
        if not values or total_gain <= 0:
            return None
        return sum(values[:n]) / total_gain

    # 一笔都不可计值时，净损益是 **None 而不是 0** —— 右删失且拿不到估值时"不知道"
    # 与"算出来是零"是两件事，写成 0.00 会被读成"这批交易白做了"。
    net = sum(t['net_pnl_micro'] for t in valued) if valued else None
    return {
        'trades': len(trades), 'valued': len(valued),
        'gross_gain_micro': total_gain if total_gain > 0 else (0 if valued else None),
        'gross_loss_micro': (sum(t['net_pnl_micro'] for t in valued if t['net_pnl_micro'] < 0)
                             if valued else None),
        'net_micro': net,
        'share_of_account_return': (_micro(net) / (initial_cash_micro / 1e6)
                                    if initial_cash_micro and net is not None else None),
        # None（没有盈利交易）与 0.0 是两件事
        'top1_trade_share_of_gain': share(top_trades, 1),
        'top3_trade_share_of_gain': share(top_trades, 3),
        'top1_security_share_of_gain': share(top_securities, 1),
        'top2_security_share_of_gain': share(top_securities, 2),
        'by_security_gain_micro': dict(gain_by_security),
        'by_security_net_micro': dict(net_securities),
        'note': '分子=盈利中最大的前N（亏损不入分子），分母=全部盈利之和；'
                '`top*_share_of_gain` 与 `by_security_gain_micro` 同口径（只含盈利），'
                '`by_security_net_micro` 是净损益（含亏损），两者不可混读。'
                '没有可计值交易时集中度为 unavailable，描述性而非结论。',
    }


def annual_returns(navs, initial_micro):
    """**按逐年逐日净值**算年度账户收益。

    这是"年度表现"该有的口径；`robustness` 里那个按退出年归集的交易损益**不是**它 ——
    跨年持仓会把整笔盈亏压到退出那一年（实测：2024-12-20 入场、2025-01-05 出场的一笔，
    50 万全记在 2025，2024 一分没有）。两者必须分开呈现，不能拿后者当"年度收益"。

    首年以**初始资金**为分母（不是当年第一行净值），否则会漏掉第一年的收益。
    """
    if not navs:
        return {'years': {}, 'note': '没有逐日净值。'}
    frames = sorted(({'session': str(n['session']), 'equity': n['full_cost_equity']}
                     for n in navs), key=lambda r: r['session'])
    by_year = defaultdict(list)
    for row in frames:
        by_year[row['session'][:4]].append(row['equity'])
    years, previous = {}, float(initial_micro)
    for year in sorted(by_year):
        values = by_year[year]
        # 年内首个完整估值的上一条：首年用初始资金，其余年用上一年最后一条
        start = previous
        end = values[-1]
        years[year] = {'start_micro': int(start), 'end_micro': int(end),
                       'return': (end / start - 1) if start > 0 else None,
                       'sessions': len(values)}
        previous = end
    positive = [y for y, row in years.items() if (row['return'] or 0) > 0]
    return {'years': years, 'count': len(years), 'positive': len(positive),
            'positive_share': (len(positive) / len(years)) if years else None,
            'note': '年度账户收益 = 年末全成本权益 / 上年末（首年用初始资金）− 1，'
                    '按逐日净值算；与 `robustness.by_exit_year` 的交易损益归集不同口径。'}


def robustness(trades):
    """稳健性：把已平仓交易按**退出年**归集，看结论是否只靠某一段（§12 walk-forward 的精神）。

    **这不是"年度账户收益"** —— 它把整笔盈亏记在退出那一年，跨年持仓会因此错位；
    年度收益另见 `annual_returns`（按逐日净值）。故这里把口径写进字段名与 note，
    并只统计**已平仓**交易（右删失的没有退出年，混进来会让同一张表出现两种归集基准）。
    """
    valued = [t for t in trades if t.get('net_pnl_micro') is not None]
    by_year = defaultdict(lambda: {'count': 0, 'net_micro': 0})
    by_reason = defaultdict(lambda: {'count': 0, 'net_micro': 0})
    for t in valued:
        if t.get('exit_session'):
            row = by_year[str(t['exit_session'])[:4]]
            row['count'] += 1
            row['net_micro'] += t['net_pnl_micro']
        row = by_reason[t.get('exit_reason') or 'RIGHT_CENSORED']
        row['count'] += 1
        row['net_micro'] += t['net_pnl_micro']
    years = dict(sorted(by_year.items()))
    positive = [y for y, row in years.items() if row['net_micro'] > 0]
    return {'by_exit_year': years, 'by_exit_reason': dict(by_reason),
            'years': len(years), 'years_positive': len(positive),
            'years_positive_share': (len(positive) / len(years)) if years else None,
            'censored_excluded': sum(1 for t in valued if not t.get('exit_session')),
            'basis': '交易净损益按**退出年**归集（只含已平仓）；不是年度账户收益',
            'note': '年度账户收益见 `annual`（按逐日净值）。分年只作描述，'
                    '单一小样本下的"最赚的一笔"不构成规则缺陷。'}
