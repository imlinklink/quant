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
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=15)
        try:
            con.execute('CREATE TABLE IF NOT EXISTS books (namespace TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT payload FROM books WHERE namespace=?', (self.namespace,)).fetchone()
            book = json.loads(row[0]) if row else {'positions': {}, 'orders': {}}
            yield book
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
