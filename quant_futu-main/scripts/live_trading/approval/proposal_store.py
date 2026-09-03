"""
人工确认下单 - 提案存储（线程安全）

状态机:
    pending → approved → executing → executed
           ↘ rejected        ↘ failed / skipped / expired
           ↘ expired（超时未操作）
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


ACTIVE_STATUSES = {'pending', 'approved', 'executing'}
TERMINAL_STATUSES = {'rejected', 'expired', 'executed', 'failed', 'skipped'}

_ALLOWED_TRANSITIONS = {
    'pending': {'approved', 'rejected', 'expired'},
    'approved': {'executing', 'rejected', 'expired'},
    'executing': {'executed', 'failed', 'skipped', 'expired'},
}


class ProposalStore:
    """提案存储：线程安全，进程内保存 + 决策记录落盘。"""

    def __init__(self, ttl_seconds: float = 180, log_dir: Optional[str] = None):
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self._items: Dict[str, Dict[str, Any]] = {}

        if log_dir is None:
            # approval/ 在 scripts/live_trading/approval/ 下，向上 4 级到项目根
            log_dir = Path(__file__).resolve().parent.parent.parent.parent / 'data' / 'approvals'
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._decision_log = self.log_dir / 'decisions.jsonl'

    # ==================== 写操作 ====================

    def create(self, **kwargs) -> Dict[str, Any]:
        """创建一条待确认提案。kwargs 里放展示/执行所需业务字段。"""
        now = time.time()
        proposal_id = uuid.uuid4().hex[:12]
        with self._lock:
            item: Dict[str, Any] = {
                'id': proposal_id,
                'status': 'pending',
                'created_at': now,
                'expires_at': now + self.ttl_seconds,
                'updated_at': now,
                'note': '',
            }
            item.update(kwargs)
            self._items[proposal_id] = item
        self._log('created', proposal_id, item.get('stock_code', ''), '')
        return dict(item)

    def approve(self, proposal_id: str) -> bool:
        """用户点击「下单」。"""
        ok = self._transition(proposal_id, 'approved', '用户点击下单')
        if ok:
            self._log('approved', proposal_id, self._code(proposal_id), '用户点击下单')
        return ok

    def reject(self, proposal_id: str, note: str = '') -> bool:
        """用户点击「拒绝」。"""
        reason = note or '用户点击拒绝'
        ok = self._transition(proposal_id, 'rejected', reason)
        if ok:
            self._log('rejected', proposal_id, self._code(proposal_id), reason)
        return ok

    def mark(self, proposal_id: str, status: str, note: str = '') -> bool:
        """内部状态推进（executing / executed / failed / skipped / expired 等）。"""
        ok = self._transition(proposal_id, status, note)
        if ok:
            self._log(f'mark:{status}', proposal_id, self._code(proposal_id), note)
        return ok

    def expire_old(self, now: Optional[float] = None) -> int:
        """把超过 TTL 仍未操作的提案标记为 expired。"""
        now = time.time() if now is None else now
        expired = 0
        for pid in list(self._items.keys()):
            item = self.get(pid)
            if not item:
                continue
            if item['status'] in ('pending', 'approved') and now > item.get('expires_at', now):
                if self._transition(pid, 'expired', '超时未确认，自动过期'):
                    expired += 1
        return expired

    # ==================== 读操作 ====================

    def get(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._items.get(proposal_id)
            return dict(item) if item else None

    def get_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [dict(v) for v in self._items.values()]
        items.sort(key=lambda v: v.get('created_at', 0), reverse=True)
        return items

    def has_active(self) -> bool:
        with self._lock:
            return any(v['status'] in ACTIVE_STATUSES for v in self._items.values())

    def has_active_for_code(self, stock_code: str) -> bool:
        with self._lock:
            return any(
                v.get('stock_code') == stock_code and v['status'] in ACTIVE_STATUSES
                for v in self._items.values()
            )

    def approved_items(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._items.values() if v['status'] == 'approved']

    def rejected_items(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._items.values() if v['status'] == 'rejected']

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for v in self._items.values() if v['status'] in ACTIVE_STATUSES)

    # ==================== 内部 ====================

    def _transition(self, proposal_id: str, target: str, note: str) -> bool:
        with self._lock:
            item = self._items.get(proposal_id)
            if not item:
                return False
            current = item['status']
            allowed = _ALLOWED_TRANSITIONS.get(current, set())
            # 幂等：同状态重复标记不允许（避免重复下单）
            if target == current or target not in allowed:
                return False
            item['status'] = target
            item['updated_at'] = time.time()
            if note:
                item['note'] = note
            return True

    def _code(self, proposal_id: str) -> str:
        with self._lock:
            item = self._items.get(proposal_id)
            return item.get('stock_code', '') if item else ''

    def _log(self, action: str, proposal_id: str, stock_code: str, note: str):
        try:
            with open(self._decision_log, 'a', encoding='utf-8') as f:
                line = {
                    'ts': datetime.now().isoformat(timespec='seconds'),
                    'action': action,
                    'id': proposal_id,
                    'stock_code': stock_code,
                    'note': note,
                }
                f.write(json.dumps(line, ensure_ascii=False) + '\n')
        except Exception:
            pass
