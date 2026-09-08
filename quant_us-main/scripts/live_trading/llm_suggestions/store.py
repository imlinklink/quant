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
