"""建议清单存储（美股专用）：与港股分开，读写 us_latest.json。"""
import fcntl
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger('llm_suggestions')

_lock = threading.Lock()

# 两个工程共用一个父目录，但文件名按市场分开：
#   us_latest.json（本文件，美股系统读写）
#   hk_latest.json（港股系统读写）
SHARED_DIR = Path(__file__).resolve().parents[3].parent / '.quant_suggestions'
LATEST_PATH = SHARED_DIR / 'us_latest.json'


@contextmanager
def _file_lock():
    """跨进程文件锁：多进程读-改-写不互相覆盖。"""
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LATEST_PATH.with_name(LATEST_PATH.name + '.lock')
    with open(lock_path, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _default() -> Dict[str, Any]:
    return {
        'generated_at': None,
        'reports': {'pre': None, 'post': None},
        'summary': '',
        'candidates': [],
    }


def load_latest() -> Dict[str, Any]:
    try:
        if LATEST_PATH.exists():
            with open(LATEST_PATH, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict) and 'candidates' in data:
                return data
    except Exception as e:
        logger.warning(f'[LLM选股] 读取建议文件失败: {e}')
    return _default()


def _atomic_write(payload: Dict[str, Any]) -> None:
    """临时文件 + 原子替换，避免写入中断留下半个 JSON。"""
    tmp = LATEST_PATH.with_name(LATEST_PATH.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, LATEST_PATH)


def save_latest(payload: Dict[str, Any]) -> Path:
    with _lock, _file_lock():
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(payload)
    return LATEST_PATH


def update_item_status(suggestion_id: str, status: str) -> bool:
    """status: added / ignored"""
    if status not in ('added', 'ignored'):
        return False
    with _lock, _file_lock():
        data = load_latest()
        for item in data.get('candidates', []):
            if item.get('id') == suggestion_id:
                item['status'] = status
                item['updated_at'] = time.time()
                _atomic_write(data)
                return True
    return False


# ==================== 版本化研究批次（阶段 G）====================
# 与建议清单分开：研究批次是版本化、可回放的 LLM 选股排序快照，
# 含 research_batch_id / universe_hash / as_of / prompt_version / model。

RESEARCH_BATCH_PATH = SHARED_DIR / 'us_research_batches.jsonl'


def save_research_batch(batch: Dict[str, Any], overwrite_conflict: bool = False) -> None:
    """追加一条版本化研究批次（JSONL）。

    幂等：同一 research_batch_id 且内容一致 → 返回，不重复追加。
    冲突：同 id 但内容不一致 → 抛 ValueError（除非 overwrite_conflict=True，人工修复时覆盖）。
    """
    bid = (batch or {}).get('research_batch_id')
    with _lock, _file_lock():
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        existing, _ = load_research_batches_report()
        if bid:
            same_id = [e for e in existing if e.get('research_batch_id') == bid]
            if same_id:
                if all(_same_content(e, batch) for e in same_id):
                    return  # 幂等：已存在且内容一致
                if not overwrite_conflict:
                    raise ValueError(f'research_batch_id 冲突: {bid}（内容不一致）')
                # overwrite_conflict：移除旧行后重写
                existing = [e for e in existing if e.get('research_batch_id') != bid]
        existing.append(batch)
        _atomic_write_lines(existing)


def _atomic_write_lines(rows: List[Dict[str, Any]]) -> None:
    """整表原子重写：临时文件 + os.replace，避免写入中断留下半个 JSONL。"""
    tmp = RESEARCH_BATCH_PATH.with_name(RESEARCH_BATCH_PATH.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, RESEARCH_BATCH_PATH)


def _same_content(a, b):
    """两个批次是否为同一输入/输出内容。"""
    keys = ('research_batch_id', 'universe_hash', 'packets_hash', 'as_of', 'candidates', 'error')
    return all(a.get(k) == b.get(k) for k in keys)


def load_research_batches() -> List[Dict[str, Any]]:
    """读取全部研究批次；损坏行跳过（损坏行号经 load_research_batches_report 记录）。"""
    ok, _ = load_research_batches_report()
    return ok


def load_research_batches_report() -> tuple:
    """读取全部研究批次，返回 (有效列表, 损坏行号列表)。损坏不静默——报告行号。"""
    if not RESEARCH_BATCH_PATH.exists():
        return [], []
    out: List[Dict[str, Any]] = []
    corrupt: List[int] = []
    with open(RESEARCH_BATCH_PATH, encoding='utf-8') as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                corrupt.append(lineno)
    if corrupt:
        logger.warning(f'[LLM选股] 研究批次损坏行（行号）: {corrupt}')
    return out, corrupt


def load_latest_research_batch() -> Optional[Dict[str, Any]]:
    batches = load_research_batches()
    return batches[-1] if batches else None


def load_research_batch_for_session(session: str) -> Optional[Dict[str, Any]]:
    """返回 as_of 的纽约日期 == session 的那条 batch；找不到返回 None。

    不复用 load_latest_research_batch 的「最新」语义：今天 selection 在写 batch 前
    崩溃时，不能回退到历史 batch 冒充今天的结果。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ny = ZoneInfo('America/New_York')
    for batch in reversed(load_research_batches()):
        as_of = batch.get('as_of')
        if not as_of:
            continue
        try:
            ts = datetime.fromisoformat(str(as_of).replace('Z', '+00:00'))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=ZoneInfo('UTC'))
            if ts.astimezone(ny).date().isoformat() == session:
                return batch
        except (ValueError, TypeError):
            continue
    return None
