"""系统内评估账本：追加式 JSONL。

每个工程各一份：
  quant_us-main/data/decision_ledger/signals.jsonl
  quant_futu-main/data/decision_ledger/signals.jsonl

想跨系统看组合效果时，用周报工具的 --ledger 参数合并读取。

事件类型约定（骨架阶段）：
  - proposal_created   信号推送（含 LLM 判定、规则理由）
  - human_decision     你在确认页点的「下单 / 拒绝」
  - execution_status   执行结果：executing/executed/failed/skipped/expired
  - position_closed    持仓平仓（后续接入，用于真实盈亏统计）
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_LOCK = threading.Lock()


def _shared_dir() -> Path:
    # 该文件位于 <项目>/scripts/live_trading/decision_ledger/ledger.py
    # parents[3] = 项目根（quant_us-main / quant_futu-main）
    #
    # `QUANT_LEDGER_DIR` 是**给测试用的隔离口**：本账本写的是项目固定路径，与调用方用哪个
    # registry 无关 —— 于是"建了 ProposalStore 却没 patch `_record_ledger`"的测试会往
    # **生产账本**追加。实测 `signals.jsonl` 515 行里 466 行是测试代码 `US.A`（90%），
    # 而周报把它当"最近提案样例"读出来。**未设该变量时生产行为一字不变。**
    override = os.environ.get('QUANT_LEDGER_DIR')
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / 'data' / 'decision_ledger'


def ledger_path() -> Path:
    return _shared_dir() / 'signals.jsonl'


def _system_name() -> str:
    return Path(__file__).resolve().parents[3].name


def record(event_type: str, **fields: Any) -> Dict[str, Any]:
    """追加一条事件。线程安全，任何异常都不影响主流程。"""
    entry = {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'system': _system_name(),
        'event_type': event_type,
        **fields,
    }
    try:
        with _LOCK:
            d = _shared_dir()
            d.mkdir(parents=True, exist_ok=True)
            with open(d / 'signals.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
    except Exception:
        # 账本写失败不能影响交易主流程
        pass
    return entry


def iter_events(days: Optional[float] = None) -> Iterator[Dict[str, Any]]:
    """按时间顺序读取事件；days=None 读全部。"""
    path = ledger_path()
    if not path.exists():
        return
    cutoff = None
    if days is not None:
        cutoff = datetime.now() - timedelta(days=days)
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if cutoff is not None:
                try:
                    ts = datetime.fromisoformat(ev.get('ts', ''))
                    if ts < cutoff:
                        continue
                except Exception:
                    continue
            yield ev


def load_events(days: Optional[float] = None) -> List[Dict[str, Any]]:
    return list(iter_events(days=days))
