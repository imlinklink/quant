"""港股评估扫描账本（评估闭环）。

记录两类“决策前检查”：
  kind=buy_check   每次买入评分检查（score/组件/是否达阈值）
  kind=exit_check  每次止盈止损/卖出决策检查（should_exit/原因/价位）

回填：backfill_hk_checks.py 按检查时间补 1/3/5 个交易日后收益；
归因：hk_scan_attribution.py 输出分组报告。
"""
import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator

_LOCK = threading.Lock()


def _scan_dir() -> Path:
    return Path(__file__).resolve().parents[3] / 'data' / 'decision_ledger'


def checks_path() -> Path:
    return _scan_dir() / 'hk_checks.jsonl'


def outcomes_path() -> Path:
    return _scan_dir() / 'hk_checks_outcomes.jsonl'


def record_check(kind: str, **fields: Any) -> Dict[str, Any]:
    """追加一条扫描检查。失败不影响交易主流程。"""
    entry = {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'scan_id': uuid.uuid4().hex[:12],
        'kind': kind,
        **fields,
    }
    try:
        with _LOCK:
            d = _scan_dir()
            d.mkdir(parents=True, exist_ok=True)
            with open(d / 'hk_checks.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
        return entry
    except Exception:
        return entry


def record_outcome(scan_id: str, **fields: Any) -> bool:
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
            with open(d / 'hk_checks_outcomes.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
        return True
    except Exception:
        return False


def _iter_file(path: Path) -> Iterator[Dict[str, Any]]:
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


def load_checks() -> list:
    return list(_iter_file(checks_path()))


def load_outcomes() -> list:
    return list(_iter_file(outcomes_path()))
