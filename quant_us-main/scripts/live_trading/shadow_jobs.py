"""持久化日任务状态；模型任务失败后不自动重试。"""
import json
import time

from .decision_ledger.event_store import make_event, insert_event, stable_id


class ShadowJobs:
    def __init__(self, events):
        self.events = events

    def claim(self, job, session, *, max_attempts=1, now=None):
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
                if last['status'] != 'failed' or last['attempt'] >= max_attempts:
                    return None
                if now - last['time'] < 300:
                    return None
            attempt = states[-1]['attempt'] + 1 if states else 1
            payload = dict(job_key=key, job=job, session=session, attempt=attempt,
                           status='running', time=now)
            insert_event(con, make_event(self.events.scope, 'shadow_job_started',
                                          f'{key}:{attempt}', payload))
        return payload

    def finish(self, claim, code):
        payload = dict(claim, status='succeeded' if code == 0 else 'failed',
                       exit_code=code, time=time.time())
        self.events.record('shadow_job_finished',
                           f"{claim['job_key']}:{claim['attempt']}", payload)

    def succeeded(self, job, session):
        key = stable_id('shadow_job', self.events.scope, job, session)
        with self.events.transaction() as con:
            rows = con.execute("SELECT body FROM decision_events WHERE account_scope=? "
                               "AND event_type='shadow_job_finished'", (self.events.scope,)).fetchall()
        return any((p := json.loads(r[0])['payload']).get('job_key') == key and
                   p['status'] == 'succeeded' for r in rows)

    def execute(self, job, session, runner, *, max_attempts=1):
        claim = self.claim(job, session, max_attempts=max_attempts)
        if claim is None:
            return False
        try:
            code = runner()
        except Exception:
            self.finish(claim, -1)
            raise
        self.finish(claim, code)
        return True
