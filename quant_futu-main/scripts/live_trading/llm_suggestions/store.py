"""建议清单存储：两套系统共享同一份 JSON，页面读取/更新。"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger('llm_suggestions')

_lock = threading.Lock()

# 两个工程共享的目录：…/Documents/quant/.quant_suggestions/latest.json
SHARED_DIR = Path(__file__).resolve().parents[3].parent / '.quant_suggestions'
LATEST_PATH = SHARED_DIR / 'latest.json'


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


def save_latest(payload: Dict[str, Any]) -> Path:
    with _lock:
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        with open(LATEST_PATH, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    return LATEST_PATH


def update_item_status(suggestion_id: str, status: str) -> bool:
    """status: added / ignored"""
    if status not in ('added', 'ignored'):
        return False
    with _lock:
        data = load_latest()
        for item in data.get('candidates', []):
            if item.get('id') == suggestion_id:
                item['status'] = status
                item['updated_at'] = time.time()
                with open(LATEST_PATH, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                return True
    return False
