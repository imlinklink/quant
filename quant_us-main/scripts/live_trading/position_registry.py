"""SQLite 持仓和订单账本；交易环境/账户隔离，事务跨线程与进程互斥。"""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class PositionRegistry:
    def __init__(self, path=None, namespace='unconfigured'):
        self.path = Path(path or Path(__file__).resolve().parents[2] / 'data' / 'execution.sqlite3')
        self.namespace = namespace

    def configure(self, namespace):
        if self.namespace not in ('unconfigured', namespace):
            raise RuntimeError('禁止同一进程混用交易账户/环境')
        self.namespace = namespace


    @contextmanager
    def transaction(self, approval=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=15)
        try:
            from .decision_ledger.event_store import migrate, insert_event
            migrate(con, self.path)
            con.execute('CREATE TABLE IF NOT EXISTS books (namespace TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            con.execute('BEGIN IMMEDIATE')
            if approval is not None:
                from .decision_ledger.event_store import canonical
                row = con.execute('SELECT body FROM decision_proposals WHERE account_scope=? AND id=?',
                                  (self.namespace, approval['id'])).fetchone()
                if not row or canonical(json.loads(row[0])) != canonical(approval):
                    raise ValueError('持久化批准凭据与执行请求不匹配')
                from .decision_ledger.event_store import utc
                for event in con.execute("SELECT body FROM decision_events WHERE account_scope=? AND event_type='material_evidence' AND observed_at>?",
                                         (self.namespace, utc(approval['approved_at']))):
                    if json.loads(event[0])['payload']['stock_code'] == approval['stock_code']:
                        raise ValueError('批准后出现重大事件，必须重新评估和确认')
            row = con.execute('SELECT payload FROM books WHERE namespace=?', (self.namespace,)).fetchone()
            book = json.loads(row[0]) if row else {'positions': {}, 'orders': {}}
            yield book
            for event in book.pop('_events', []):
                insert_event(con, event)
            con.execute('INSERT OR REPLACE INTO books VALUES (?, ?)',
                        (self.namespace, json.dumps(book, allow_nan=False)))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def open(self, code, entry_mode, qty, entry_price, **metadata):
        with self.transaction() as book:
            if code not in book['positions']:
                book['positions'][code] = dict(metadata, code=code, entry_mode=entry_mode,
                    qty=float(qty), entry_price=float(entry_price), opened_at=time.time())
            return dict(book['positions'][code])

    def update(self, code, **fields):
        with self.transaction() as book:
            if code in book['positions']:
                book['positions'][code].update(fields)

    def close(self, code):
        with self.transaction() as book:
            return book['positions'].pop(code, None) is not None

    def get(self, code):
        return self.all().get(code)

    def mode(self, code):
        return (self.get(code) or {}).get('entry_mode', 'manual')

    def all(self):
        with self.transaction() as book:
            return {c: dict(p) for c, p in book['positions'].items()}

    def count(self):
        return len(self.all())

    def codes(self):
        return list(self.all())


REGISTRY = PositionRegistry()


def registry_for(config, path=None, scope=None):
    """按配置解析出账本的 namespace 再开注册表。

    **默认 namespace 是 `'unconfigured'`**，而事件实际写在
    `llm_decision.engine_v2.account_scope`（通常是 `DRY-RUN`）下。命令行工具若用默认值
    打开同一份 sqlite，会按错的 scope 过滤 —— 结果是**空清单，且看不出原因**。

    定义在**模块末尾**：放在类中间会把其后的方法变成它的嵌套函数（缩进恰好接得上，
    不报语法错，只表现为属性凭空消失）。
    """
    resolved = (scope
                or (((config or {}).get('llm_decision') or {}).get('engine_v2') or {}
                    ).get('account_scope'))
    if not resolved:
        raise ValueError('ACCOUNT_SCOPE_UNRESOLVED:'
                         '配置里没有 llm_decision.engine_v2.account_scope，请用 --scope 指定')
    return PositionRegistry(path, resolved)
