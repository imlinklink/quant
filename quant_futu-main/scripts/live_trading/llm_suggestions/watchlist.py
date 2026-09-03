"""观察池写入：把 LLM 建议（人工确认后）追加到对应 config 的 watch_list。"""
import logging
from pathlib import Path
from typing import List, Optional

import yaml

logger = logging.getLogger('llm_suggestions')

# 两个工程是兄弟目录（…/Documents/quant/quant_*-main）
_QUANT_ROOT = Path(__file__).resolve().parents[3].parent
US_CONFIG = _QUANT_ROOT / 'quant_us-main' / 'config.yaml'
HK_CONFIG = _QUANT_ROOT / 'quant_futu-main' / 'config.yaml'


def _yaml_list(cfg: dict, keys: List[str]) -> list:
    node = cfg
    for k in keys:
        if not isinstance(node, dict):
            return []
        node = node.get(k)
    return node if isinstance(node, list) else []


def _normalize_us(code: str) -> Optional[str]:
    c = str(code).strip().upper()
    if c.startswith('US.'):
        return c
    if c and '.' not in c:
        return f'US.{c}'
    return None


def _normalize_hk(code: str) -> Optional[str]:
    c = str(code).strip().upper()
    if c.startswith('HK.'):
        digits = c[3:]
    elif '.' not in c:
        digits = c
    else:
        return None
    if not digits.isdigit() or not (1 <= len(digits) <= 5):
        return None
    return f'HK.{digits.zfill(5)}'


def _insert_after_key(path: Path, section: str, key: str, new_line: str) -> bool:
    """文本级插入：保留注释，在 section > key 行后插入一行。"""
    lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
    in_section = False
    out = []
    inserted = False
    for line in lines:
        stripped = line.lstrip()
        if line.startswith(section + ':'):
            in_section = True
        elif in_section and stripped and not line[0].isspace():
            in_section = False
        out.append(line)
        if in_section and not inserted:
            body = stripped.rstrip()
            empty_inline = body.startswith(key + ':') and body[len(key) + 1:].strip() == '[]'
            if body == key + ':' or empty_inline:
                # 空列表写成 "key: []" 时，需要先展开成多行再追加
                if empty_inline:
                    indent = line[: len(line) - len(line.lstrip())]
                    out[-1] = f'{indent}{key}:\n'
                out.append(new_line)
                inserted = True
    if not inserted:
        return False
    path.write_text(''.join(out), encoding='utf-8')
    return True


def add_us_watch(code: str) -> bool:
    """加入美股观察池（dip_buy.watch_list）。返回是否新增。"""
    c = _normalize_us(code)
    if not c:
        return False
    cfg = yaml.safe_load(US_CONFIG.read_text(encoding='utf-8')) or {}
    if c in _yaml_list(cfg, ['dip_buy', 'watch_list']):
        return False
    ok = _insert_after_key(
        US_CONFIG, 'dip_buy', 'watch_list', f'    - {c}\n'
    )
    if ok:
        logger.warning(f'[LLM选股] 已加入美股观察池: {c}（{US_CONFIG}）')
    return ok


def add_hk_watch(code: str) -> bool:
    """加入港股观察池（hk.watch_list）。返回是否新增。"""
    c = _normalize_hk(code)
    if not c:
        return False
    cfg = yaml.safe_load(HK_CONFIG.read_text(encoding='utf-8')) or {}
    if c in _yaml_list(cfg, ['hk', 'watch_list']):
        return False
    ok = _insert_after_key(HK_CONFIG, 'hk', 'watch_list', f'    - {c}\n')
    if ok:
        logger.warning(f'[LLM选股] 已加入港股观察池: {c}（{HK_CONFIG}）')
    return ok


def current_us_watch() -> List[str]:
    cfg = yaml.safe_load(US_CONFIG.read_text(encoding='utf-8')) or {}
    return list(_yaml_list(cfg, ['dip_buy', 'watch_list']))


def current_hk_watch() -> List[str]:
    cfg = yaml.safe_load(HK_CONFIG.read_text(encoding='utf-8')) or {}
    return list(_yaml_list(cfg, ['hk', 'watch_list']))
