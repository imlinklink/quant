"""Deterministic gate observations. The sink never makes trading decisions."""
from collections import Counter, defaultdict
import math
import pandas as pd
import sqlite3
import json
from pathlib import Path

from scripts.live_trading.decision_ledger.event_store import digest


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if pd.isna(value):
        return None
    if hasattr(value, 'item'):
        return clean(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


class Funnel:
    """§5.1 的 `gate_observation` 收集器。

    三条纪律：

    1. **sink 不做任何交易决定**，也不改变生成器状态（§14「观察器旁路」由
       `experiments._run` 逐 session 对拍带 sink 与不带 sink 的两个生成器）。
    2. **墙钟审计字段不参与身份**（§5.3）。`observed_at`/`generated_at` 由**生产方**给出
       （`candidate_adapter.audit_stamps`），sink 不自己取当前时间 —— 一个悄悄调 `now()`
       的字段在历史重建里等于伪造，且会让每次重跑产出不同的行。
    3. **同键异内容冲突，但审计字段除外**：重跑时**首次写入的值胜出**，于是
       `observations()` 在多次确定性重放之间逐字节相同。
    """

    AUDIT_FIELDS = ('observed_at', 'generated_at')
    REQUIRED = ('candidate_id', 'security_id', 'candidate_round', 'session', 'stage',
                'result', 'all_reasons', 'observed_at', 'generated_at', 'rule_version')
    # 必须**存在**但允许为空：`primary_reason` 在通过/未执行的行上就是空串，
    # `candidate_state` 只在终态观察上有值（门观察为 None）。用存在性区分"如实为空"
    # 与"生产者忘了给"。'all_reasons' 允许为空列表，故它留在 REQUIRED 里。
    REQUIRED_PRESENT = ('primary_reason', 'candidate_state')

    def __init__(self, study_id, path=None):
        self.study_id = study_id
        self.rows = {}
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path) as con:
                con.execute('CREATE TABLE IF NOT EXISTS observations '
                            '(id TEXT PRIMARY KEY, body TEXT NOT NULL)')

    @classmethod
    def _business(cls, row):
        return {k: v for k, v in row.items() if k not in cls.AUDIT_FIELDS}

    def __call__(self, row):
        row = clean(dict(row, study_id=self.study_id,
                         source_kind='historical_reconstruction'))
        if missing := [k for k in self.REQUIRED if row.get(k) in (None, '')]:
            raise ValueError(f'GATE_OBSERVATION_MISSING_FIELDS:{sorted(missing)}')
        if absent := [k for k in self.REQUIRED_PRESENT if k not in row]:
            raise ValueError(f'GATE_OBSERVATION_MISSING_FIELDS:{sorted(absent)}')
        if row['result'] not in ('pass', 'reject', 'unknown', 'not_evaluated'):
            raise ValueError('INVALID_GATE_RESULT')
        # §5.3 的两条一致性，做成**防线**而不只是生产者的自觉：
        # · 未执行的条件标 not_evaluated，不能当失败 ⇒ 该行没有主原因；
        # · 主原因必须是 all_reasons 的首项（它们本来就是同一份优先级序列）。
        # 一旦不一致，读的人会把"这个阶段没轮到它"读成"这个阶段否掉了它" —— 两份数据都在库里，
        # 都看起来合理，只有这条断言能挡住。
        if row['result'] == 'not_evaluated' and row['primary_reason']:
            raise ValueError('GATE_OBSERVATION_REASON_ON_UNEVALUATED')
        if row['primary_reason'] and row['all_reasons'][:1] != [row['primary_reason']]:
            raise ValueError('GATE_OBSERVATION_PRIMARY_NOT_FIRST')
        row['input_snapshot_hash'] = digest(row.get('feature_values', {}))
        key = digest([row[k] for k in ('study_id', 'candidate_id', 'session', 'stage',
                                       'rule_version')])
        if key in self.rows:
            if self._business(self.rows[key]) != self._business(row):
                raise ValueError('GATE_OBSERVATION_CONFLICT')
            return  # 审计字段以首次为准
        if self.path:
            body = json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
            with sqlite3.connect(self.path) as con:
                con.execute('BEGIN IMMEDIATE')
                old = con.execute('SELECT body FROM observations WHERE id=?', (key,)).fetchone()
                if old is not None:
                    stored = json.loads(old[0])
                    if self._business(stored) != self._business(row):
                        raise ValueError('GATE_OBSERVATION_CONFLICT')
                    # 复用首次写入的整行（含审计戳）：重跑不得产生第二个"业务身份"
                    self.rows[key] = stored
                    return
                con.execute('INSERT INTO observations VALUES (?,?)', (key, body))
        self.rows[key] = row

    def observations(self):
        return sorted(self.rows.values(), key=lambda r: (r['session'], r['candidate_id'], r['stage']))

    def summary(self):
        stages, candidates = defaultdict(Counter), {}
        for r in self.rows.values():
            stages[r['stage']][r['result']] += 1
            if r.get('candidate_state'):
                prev = candidates.get(r['candidate_id'])
                if prev is None or r['session'] > prev['session']:
                    candidates[r['candidate_id']] = r
                elif r['session'] == prev['session'] and r['candidate_state'] != prev['candidate_state']:
                    raise ValueError('MULTIPLE_CANDIDATE_STATES')
        states = Counter(r['candidate_state'] for r in candidates.values())
        return {
            'candidate_count': len(candidates), 'candidate_states': dict(states),
            'ready_fraction': states['READY'] / len(candidates) if candidates else None,
            'stages': {stage: {**dict(c), 'evaluated': c['pass'] + c['reject'],
                              'conditional_pass_rate': (c['pass'] / (c['pass'] + c['reject'])
                                                        if c['pass'] + c['reject'] else None)}
                       for stage, c in sorted(stages.items())},
            'candidates': [candidates[k] for k in sorted(candidates)],
            'note': '候选按轮次去重；阶段为逐日观察。历史重建不证明当时调度实际执行。'}
