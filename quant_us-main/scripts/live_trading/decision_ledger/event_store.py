"""Versioned, append-only decision journal in the execution database.

The outbox and execution book commit together. JSONL is a derived export;
consumers deduplicate event_id (a crash after append may repeat a line).
"""
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def stable_id(kind, *parts):
    return kind + '_' + digest(parts)[:32]


def utc(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('时间必须明确时区')
    return value.astimezone(timezone.utc).isoformat()


def make_event(scope, event_type, key, payload, **links):
    now = utc()
    return dict(event_id=stable_id('event', scope, event_type, key), event_type=event_type,
                account_scope=scope, schema_version=SCHEMA_VERSION,
                event_time=now, observed_at=now, payload_hash=digest(payload),
                payload=payload, **links)


def enqueue(book, scope, event_type, key, payload, **links):
    event = make_event(scope, event_type, key, payload, **links)
    book.setdefault('_events', []).append(event)
    return event


def insert_event(con, event):
    row = con.execute('SELECT payload_hash FROM decision_events WHERE event_id=?',
                      (event['event_id'],)).fetchone()
    if row:
        if row[0] != event['payload_hash']:
            raise ValueError('同一事件ID出现冲突内容')
        return False
    con.execute('INSERT INTO decision_events VALUES (?,?,?,?,?,?)',
                (event['event_id'], event['account_scope'], event['event_type'],
                 event['observed_at'], event['payload_hash'], canonical(event)))
    con.execute('INSERT INTO decision_outbox(event_id) VALUES (?)', (event['event_id'],))
    return True


def _current_schema(con):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='decision_schema'").fetchone():
        return False
    version = con.execute('SELECT MAX(version) FROM decision_schema').fetchone()[0]
    if version and version > SCHEMA_VERSION:
        raise RuntimeError('数据库版本高于代码版本，禁止降级打开')
    return version == SCHEMA_VERSION


def migrate(con, path):
    """Serialize first migration across processes; back up before touching schema."""
    if _current_schema(con):
        return
    with open(str(path)+'.migration.lock','a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not _current_schema(con):
            _migrate(con, path)


def _migrate(con, path):
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='books'").fetchone():
        backup = Path(str(path) + '.pre-decision-v1.bak')
        # Backup is made before modifying the old book, and is never overwritten.
        if not backup.exists():
            fd, temporary = tempfile.mkstemp(prefix=backup.name+'.', dir=backup.parent)
            os.close(fd)
            try:
                with sqlite3.connect(temporary) as dest:
                    con.backup(dest)
                    if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('迁移备份校验失败')
                os.replace(temporary, backup)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        else:
            with sqlite3.connect(backup.resolve().as_uri()+'?mode=ro', uri=True) as dest:
                if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('已有迁移备份损坏')
    con.executescript('''
        CREATE TABLE IF NOT EXISTS decision_schema(version INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS decision_events (
            event_id TEXT PRIMARY KEY, account_scope TEXT NOT NULL,
            event_type TEXT NOT NULL, observed_at TEXT NOT NULL,
            payload_hash TEXT NOT NULL, body TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS decision_scope ON decision_events(account_scope, observed_at);
        CREATE TABLE IF NOT EXISTS decision_outbox (
            event_id TEXT PRIMARY KEY REFERENCES decision_events(event_id),
            exported INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT);
        CREATE TABLE IF NOT EXISTS decision_snapshots (
            account_scope TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL,
            version INTEGER NOT NULL, payload_hash TEXT NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(account_scope,kind,id,version));
        CREATE TABLE IF NOT EXISTS decision_proposals (
            account_scope TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
            PRIMARY KEY(account_scope,id));
    ''')
    con.execute('INSERT OR IGNORE INTO decision_schema VALUES (?)', (SCHEMA_VERSION,))
    con.commit()


class EventStore:
    def __init__(self, registry):
        self.registry = registry

    @property
    def scope(self):
        return self.registry.namespace

    @contextmanager
    def transaction(self):
        self.registry.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.registry.path), timeout=15)
        try:
            migrate(con, self.registry.path)
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def record(self, event_type, key, payload, **links):
        event = make_event(self.scope, event_type, key, payload, **links)
        with self.transaction() as con:
            if not insert_event(con, event):
                event = json.loads(con.execute('SELECT body FROM decision_events WHERE event_id=?',
                                               (event['event_id'],)).fetchone()[0])
        return event

    def snapshot(self, con, kind, id, version, payload):
        old = con.execute('SELECT payload_hash FROM decision_snapshots WHERE account_scope=? AND kind=? AND id=? AND version=?',
                          (self.scope, kind, id, version)).fetchone()
        if old:
            if old[0] != digest(payload):
                raise ValueError('不可覆盖历史快照')
            return
        con.execute('INSERT INTO decision_snapshots VALUES (?,?,?,?,?,?)',
                    (self.scope, kind, id, version, digest(payload), canonical(payload)))

    def get_snapshot(self, kind, id, version=1):
        with self.transaction() as con:
            row = con.execute('SELECT body FROM decision_snapshots WHERE account_scope=? AND kind=? AND id=? AND version=?',
                              (self.scope, kind, id, version)).fetchone()
            return json.loads(row[0]) if row else None

    def save_proposal(self, item, event_type, payload=None, key=None, snapshots=()):
        with self.transaction() as con:
            row = con.execute('SELECT body FROM decision_proposals WHERE account_scope=? AND id=?',
                              (self.scope, item['id'])).fetchone()
            old_revision = json.loads(row[0]).get('_revision', 0) if row else 0
            if item.get('_revision', 0) != old_revision:
                raise ValueError('提案已被另一进程更新，请刷新')
            saved = dict(item, _revision=old_revision + 1)
            for kind, id, version, body in snapshots:
                self.snapshot(con, kind, id, version, body)
                if kind in ('plan', 'input'):
                    insert_event(con, make_event(self.scope, 'plan_created' if kind == 'plan' else 'evidence_snapshot',
                        [id, version], body, plan_id=item.get('plan_id'), plan_version=item.get('plan_version'),
                        signal_id=item.get('signal_id'), proposal_id=item['id']))
            con.execute('INSERT OR REPLACE INTO decision_proposals VALUES (?,?,?)',
                        (self.scope, item['id'], canonical(saved)))
            links = {k: item[k] for k in ('signal_id','plan_id','plan_version','review_id','trade_id') if k in item}
            insert_event(con, make_event(self.scope, event_type, key or item['id'],
                                        payload if payload is not None else item,
                                        proposal_id=item['id'], **links))
        item['_revision'] = saved['_revision']

    def proposals(self):
        with self.transaction() as con:
            return [json.loads(r[0]) for r in con.execute(
                'SELECT body FROM decision_proposals WHERE account_scope=?', (self.scope,))]

    def events(self):
        with self.transaction() as con:
            return [json.loads(r[0]) for r in con.execute(
                'SELECT body FROM decision_events WHERE account_scope=? ORDER BY observed_at,event_id', (self.scope,))]

    def export(self, path=None, limit=500):
        """Only export; no callback here can submit/retry a broker order."""
        path = Path(path or self.registry.path.parent / 'decision_ledger' / 'events-v1.jsonl')
        try:
            with self.transaction() as con:
                rows = con.execute('SELECT e.event_id,e.body FROM decision_outbox o JOIN decision_events e USING(event_id) '
                                   'WHERE o.exported=0 AND e.account_scope=? ORDER BY e.observed_at,e.event_id LIMIT ?',
                                   (self.scope, limit)).fetchall()
                if rows:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with path.open('a', encoding='utf-8') as f:
                            for _, body in rows:
                                f.write(body + '\n')
                            f.flush()
                            import os
                            os.fsync(f.fileno())
                        con.executemany('UPDATE decision_outbox SET exported=1,last_error=NULL WHERE event_id=?',
                                        [(id,) for id, _ in rows])
                    except Exception as exc:
                        con.executemany('UPDATE decision_outbox SET attempts=attempts+1,last_error=? WHERE event_id=?',
                                        [(type(exc).__name__, id) for id, _ in rows])
                        logger.exception('决策导出失败，事件保留在outbox等待重试')
                return con.execute('SELECT COUNT(*) FROM decision_outbox o JOIN decision_events e USING(event_id) '
                                   'WHERE exported=0 AND account_scope=?', (self.scope,)).fetchone()[0]
        except Exception:
            logger.exception('无法读取决策导出队列')
            return None
