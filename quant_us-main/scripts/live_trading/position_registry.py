"""持仓登记簿（P0：按策略线分出场 + DRY-RUN 模拟持仓簿）。

买入成交时（dip_buy / donchian）登记一笔：
    code -> {entry_mode, qty, entry_price, opened_at}
出场层用它：
  1) 区分“这笔持仓来自哪条策略线”，套对应出场参数（profiles）；
  2) DRY-RUN 下把登记簿当作“券商持仓”喂给 ChandelierExitManager，
     让模拟买入也能完整演练 2×ATR 吊灯/阶段止盈；
  3) 券商持仓查询不到的代码（手动开仓）默认 manual。

进程内存储即可：重启后无法回溯的历史持仓按 manual 参数管理（保守）。
"""
import threading
import time
from typing import Dict, Optional


class PositionRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._book: Dict[str, Dict] = {}

    def open(self, code: str, entry_mode: str, qty: float,
             entry_price: float) -> Optional[Dict]:
        """登记一笔买入；同代码已有登记时不覆盖（单代码一仓）。"""
        if not code:
            return None
        with self._lock:
            if code in self._book:
                return dict(self._book[code])
            rec = {
                'code': code,
                'entry_mode': str(entry_mode or 'manual'),
                'qty': float(qty or 0),
                'entry_price': float(entry_price or 0),
                'opened_at': time.time(),
            }
            self._book[code] = rec
            return dict(rec)

    def close(self, code: str) -> bool:
        with self._lock:
            return self._book.pop(code, None) is not None

    def get(self, code: str) -> Optional[Dict]:
        with self._lock:
            rec = self._book.get(code)
            return dict(rec) if rec else None

    def mode(self, code: str) -> str:
        rec = self.get(code)
        return str(rec.get('entry_mode', 'manual')) if rec else 'manual'

    def count(self) -> int:
        with self._lock:
            return len(self._book)

    def codes(self) -> list:
        with self._lock:
            return list(self._book.keys())

    def all(self) -> Dict[str, Dict]:
        with self._lock:
            return {c: dict(r) for c, r in self._book.items()}


REGISTRY = PositionRegistry()
