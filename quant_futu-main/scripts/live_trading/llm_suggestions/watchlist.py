"""观察池写入：把 LLM 建议（人工确认后）追加到对应 config 的 watch_list。"""
import logging
import os
import threading
from pathlib import Path
from typing import List, Optional

import yaml

logger = logging.getLogger('llm_suggestions')
_lock = threading.Lock()

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
    # 原子写：临时文件 + os.replace，避免进程中断留下半写入的 config
    tmp_path = path.with_name(path.name + '.tmp')
    tmp_path.write_text(''.join(out), encoding='utf-8')
    os.replace(tmp_path, path)
    return True


def add_us_watch(code: str) -> bool:
    """加入美股观察池（dip_buy.watch_list）。返回是否新增。"""
    with _lock:
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
    with _lock:
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


def validate_futu_symbol(code: str) -> tuple:
    """
    加入观察池前校验富途是否识别该代码（有行情）。

    Returns:
        (ok: bool, message: str)
    """
    c = str(code or '').strip().upper()
    try:
        import yaml as _yaml
        from datetime import date, timedelta
        from futu import OpenQuoteContext, KLType, RET_OK

        if c.startswith('US.'):
            cfg = _yaml.safe_load(US_CONFIG.read_text(encoding='utf-8')) or {}
            futu_cfg = cfg.get('futu') or {}
        elif c.startswith('HK.'):
            cfg = _yaml.safe_load(HK_CONFIG.read_text(encoding='utf-8')) or {}
            futu_cfg = (cfg.get('hk') or {}).get('futu') or {}
        else:
            return False, '代码需以 US. 或 HK. 开头'

        host = str(futu_cfg.get('host', '127.0.0.1'))
        port = int(futu_cfg.get('port', 11111))
        start = (date.today() - timedelta(days=10)).strftime('%Y-%m-%d')
        end = date.today().strftime('%Y-%m-%d')
        with OpenQuoteContext(host=host, port=port) as ctx:
            ret, data, _ = ctx.request_history_kline(
                code=c, start=start, end=end,
                ktype=KLType.K_DAY, max_count=5,
            )
        if ret == RET_OK and data is not None and len(data) > 0:
            return True, ''
        return False, f'富途不识别该代码或无行情: {c}'
    except Exception as e:
        return False, f'校验失败（OpenD 未运行?）: {e}'
