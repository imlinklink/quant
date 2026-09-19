"""持久化日任务状态；模型任务失败后不自动重试。"""
import json
import time

from .decision_ledger.event_store import make_event, insert_event, stable_id


class ShadowJobs:
    def __init__(self, events):
        self.events = events

    def claim(self, job, session, *, max_attempts=1, now=None, force=False):
        """领取一次作业；返回 claim 载荷，或 None 表示不该跑。

        `force=True` 是**人工补跑出口**（照 `protocol_review --retry` 的先例）：
        只放开「重试已耗尽」与 300 秒退避两条闸，**不放开「已经成功过」** ——
        重跑一个成功过的作业会重复写事件，那不是补跑、是制造重复。
        为什么需要它：`max_attempts` 用完后 `claim` 恒返回 None ⇒ **该 session 永久不再补**，
        而"三次都撞上同一个瞬时故障"完全可能（2026-09-19 的 09-18 session 就是这样）。
        """
        now = time.time() if now is None else now
        key = stable_id('shadow_job', self.events.scope, job, session)
        with self.events.transaction() as con:
            rows = con.execute(
                "SELECT body FROM decision_events WHERE account_scope=? AND event_type IN "
                "('shadow_job_started','shadow_job_finished') ORDER BY rowid",
                (self.events.scope,)).fetchall()
            states = [json.loads(row[0])['payload'] for row in rows]
            states = [s for s in states if s.get('job_key') == key]
            if states:
                last = states[-1]
                # 成功过、或仍在跑：一律不重跑（force 也不例外）
                if last['status'] != 'failed':
                    return None
                if not force:
                    if last['attempt'] >= max_attempts:
                        return None
                    if now - last['time'] < 300:
                        return None
            attempt = states[-1]['attempt'] + 1 if states else 1
            payload = dict(job_key=key, job=job, session=session, attempt=attempt,
                           status='running', time=now, forced=bool(force and states))
            insert_event(con, make_event(self.events.scope, 'shadow_job_started',
                                          f'{key}:{attempt}', payload))
        return payload

    def finish(self, claim, code, reason=''):
        payload = dict(claim, status='succeeded' if code == 0 else 'failed',
                       exit_code=code, reason=reason, time=time.time())
        self.events.record('shadow_job_finished',
                           f"{claim['job_key']}:{claim['attempt']}", payload)

    def succeeded(self, job, session):
        key = stable_id('shadow_job', self.events.scope, job, session)
        with self.events.transaction() as con:
            rows = con.execute("SELECT body FROM decision_events WHERE account_scope=? "
                               "AND event_type='shadow_job_finished'", (self.events.scope,)).fetchall()
        return any((p := json.loads(r[0])['payload']).get('job_key') == key and
                   p['status'] == 'succeeded' for r in rows)

    def execute(self, job, session, runner, *, max_attempts=1, force=False):
        claim = self.claim(job, session, max_attempts=max_attempts, force=force)
        if claim is None:
            return False
        try:
            result = runner()
        except Exception:
            self.finish(claim, -1, 'exception')
            raise
        if isinstance(result, tuple):
            code, reason = result
        else:
            code, reason = result, ''
        self.finish(claim, code, reason)
        return True


# 每个交易日应当跑完的三个作业（`OutcomeSchedulerThread._integration_tick`）。
EXPECTED_JOBS = ('selection_and_reconcile', 'daily_setup_shadow', 'selection_outcomes')


def incomplete_jobs(events, sessions, *, jobs=EXPECTED_JOBS):
    """在给定的交易日里，哪些 `(session, job)` **没有成功过**。返回 `[(session, job, 终态)]`。

    终态：`'succeeded'` / `'failed'`（含重试耗尽）/ `'running'`（可能崩在中间）/ `None`（无记录）。

    **`None`（完全没有记录）也要报**，而且那一类最容易被漏掉 —— 服务当时没在跑就没有任何事件，
    而"没有事件"这件事本身**不会出现在任何报表里**（2026-09-16/17 就是这样：服务没运行，
    那两个 session 一条记录都没有，直到 09-19 才被人看见）。

    这个函数存在的前提是：`claim` 在 `max_attempts` 用完后恒返回 None ⇒
    **失败的 session 会永久停在那里**，而在此之前没有任何地方会说出这件事。
    """
    by = {}
    with events.transaction() as con:
        rows = con.execute(
            "SELECT body FROM decision_events WHERE account_scope=? AND event_type IN "
            "('shadow_job_started','shadow_job_finished') ORDER BY rowid",
            (events.scope,)).fetchall()
    for (body,) in rows:
        payload = json.loads(body).get('payload') or {}
        by[(payload.get('session'), payload.get('job'))] = payload.get('status')
    out = []
    for session in sorted(set(sessions)):
        for job in jobs:
            status = by.get((session, job))
            if status != 'succeeded':
                out.append((session, job, status))
    return out
