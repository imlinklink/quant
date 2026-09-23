#!/usr/bin/env python3
"""定时任务的排程规格 —— **单一事实来源**。

为什么单独一个模块：简报的产出时刻有**两个消费者**，而它们各自关心相反的一面 ——

- `install_cron.py` 用它渲染 crontab：**什么时候跑**；
- `watchdog.py` 用它判断简报该不该已经更新了：**什么时候该有产出**。

两处各写一份日期就会各说各话。反的那一面尤其坏：排程改了而判定没改，看护会
**每 5 分钟假报一次**（真问题被淹掉，"真停机与假警长得一模一样"）；判定改了而排程
没改，则是真的没产出却报告一切正常。本仓库已经因为"同一件事两份定义"出过真 bug
（`VALID_ROLES` 两份、角色基线手写三键），所以这里只留一份。

星期一律用 **cron 的约定**（0=周日 … 6=周六，7 也接受为周日）—— 与最终写进 crontab
的字段同一个约定，不做"存一套、渲染另一套"的换算。换算错过：launchd 的 `weekday: 5`
曾被当成"周五"，实际按纽约时间是周六（见 config.yaml 的 protocol_review）。
"""
from datetime import datetime, time, timedelta

# 盘前简报（连同选股建议与结果回填）的排程：**北京周一~周五 08:20**。
# 对应 crontab 的 `20 8 * * 1-5`。改这里就同时改了"什么时候跑"与"什么时候该有产出"。
BRIEF = {'hour': 8, 'minute': 20, 'weekdays': (1, 2, 3, 4, 5)}


def _fmt_run(run: list) -> str:
    if len(run) == 1:
        return str(run[0])
    if len(run) == 2:
        # `1-2` 也是合法 cron，但两段的连字符容易被读成笔误（`1-2` vs `1,2` 看不出差别）。
        return f'{run[0]},{run[1]}'
    return f'{run[0]}-{run[-1]}'


def cron_weekday_field(days) -> str:
    """星期集合 → cron 的星期字段：连续区间压缩（1,2,3,4,5 → `1-5`）。"""
    uniq = sorted({int(d) % 7 for d in days})   # 7 → 0：cron 允许 7 表示周日
    if not uniq:
        raise ValueError('weekdays 不能为空')
    parts, run = [], [uniq[0]]
    for d in uniq[1:]:
        if d == run[-1] + 1:
            run.append(d)
        else:
            parts.append(_fmt_run(run))
            run = [d]
    parts.append(_fmt_run(run))
    return ','.join(parts)


def launchd_weekday(day: int) -> int:
    """cron 的星期 → launchd 的 `Weekday`。

    **两套约定不一样**：cron 是 0=周日…6=周六，launchd 是 1=周一…7=周日。
    本文件里 `BRIEF['weekdays']` 用的是 **cron 约定**，所以渲染 launchd 时必须换算 ——
    只在 1…6 上两者数值恰好相同，**周日一个用 0 一个用 7**，不换算就是碰巧对一半。
    本仓库已经因为星期约定错过一次（`config.yaml` 的 `protocol_review.weekday` 把
    美东周六当成过周五），所以这里显式转换并带测试。
    """
    d = int(day) % 7
    return 7 if d == 0 else d


def launchd_calendar(spec: dict = None) -> list:
    """排程规格 → launchd 的 `StartCalendarInterval` 列表。"""
    spec = spec or BRIEF
    return [{'Weekday': launchd_weekday(d), 'Hour': int(spec['hour']),
             'Minute': int(spec['minute'])} for d in spec['weekdays']]


def latest_expected_date(now: datetime, spec: dict = None):
    """**≤ now 的最近一次排程**落在哪天 —— 「此刻最新可能存在的产出」应有的日期。

    判据必须是**时刻**，不能是"今天是不是工作日"：在排程当天、时刻之前
    （周一 07:00，而简报 08:20 才跑），今天那份**还不该存在**。
    只看"工作日"会在这段时间里假报，等于把周末的假警搬到周一早晨。

    返回 `date`；`spec` 不合法或 8 天内无排程时返回 `None`（调用方按 fail-closed 处理）。
    """
    spec = spec or BRIEF
    days = {str(int(d) % 7) for d in spec['weekdays']}
    at = time(int(spec['hour']), int(spec['minute']))
    for back in range(0, 8):                    # 最长空档 7 天（每周一次）也覆盖得到
        d = (now - timedelta(days=back)).date()
        if d.strftime('%w') not in days:        # `%w` 就是 cron 的约定：0=周日
            continue
        if datetime.combine(d, at) <= now:
            return d
    return None
