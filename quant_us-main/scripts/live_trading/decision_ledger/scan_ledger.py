"""抄底扫描账本（P2 评估闭环 · 第 1 块）。

与 signals.jsonl（提案/人工决策/成交）分开存：
  <project>/data/decision_ledger/dip_scans.jsonl

记录的是“每次评分检查”的横截面：
  组件分、时段、60m环境、各闸门结果、最终 outcome；
  之后由 backfill_scan_outcomes.py 回填 12/24/48 根 5mK 的前向收益，
  再由 scan_attribution.py 归因“哪个组件/闸门真的预测反弹”。

写入失败不影响交易主流程（与决策账本同原则）。
"""
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

_LOCK = threading.Lock()


def _scan_dir() -> Path:
    # scan_ledger.py 位于 <project>/scripts/live_trading/decision_ledger/
    return Path(__file__).resolve().parents[3] / 'data' / 'decision_ledger'


def scans_path() -> Path:
    return _scan_dir() / 'dip_scans.jsonl'


def record_scan(**fields: Any) -> Optional[Dict[str, Any]]:
    """追加一条扫描记录，返回含 scan_id 的 entry（写失败返回 None）。"""
    entry = {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'scan_id': uuid.uuid4().hex[:12],
        'event_type': 'dip_scan',
        **fields,
    }
    try:
        with _LOCK:
            d = _scan_dir()
            d.mkdir(parents=True, exist_ok=True)
            with open(d / 'dip_scans.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
        return entry
    except Exception:
        # 评估流水不能影响交易
        return None


def iter_scans() -> Iterator[Dict[str, Any]]:
    path = scans_path()
    if not path.exists():
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def load_scans() -> list:
    return list(iter_scans())


def outcomes_path() -> Path:
    return _scan_dir() / 'dip_scan_outcomes.jsonl'


def record_outcome(scan_id: str, **fields: Any) -> bool:
    """追加一条回填结果（以 scan_id 关联），幂等由调用方负责。"""
    if not scan_id:
        return False
    entry = {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'scan_id': scan_id,
        **fields,
    }
    try:
        with _LOCK:
            d = _scan_dir()
            d.mkdir(parents=True, exist_ok=True)
            with open(d / 'dip_scan_outcomes.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
        return True
    except Exception:
        return False


def iter_outcomes() -> Iterator[Dict[str, Any]]:
    path = outcomes_path()
    if not path.exists():
        return
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def load_outcomes() -> list:
    return list(iter_outcomes())
