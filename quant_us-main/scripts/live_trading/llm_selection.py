"""LLM 选股影子排序（首个迭代）：基础池 → Evidence Packet → 结构化 Top-N 排名。

LLM 只输出候选与证据，不下单、不改交易状态。失败/超时/越权/无有效引用时只记录失败。
"""
import json
import time

from mutifactor.llm.selection_review import (
    SELECTION_PROMPT_VERSION, SELECTION_SCHEMA, SELECTION_SCHEMA_VERSION,
    SELECTION_SYSTEM, validate_selection,
)
from scripts.live_trading.decision_ledger.event_store import digest, stable_id, utc


def build_selection_prompt(universe, packets):
    """组装 prompt：基础池 + 每只股票的 evidence_packet。"""
    return json.dumps({
        'decision_type': 'selection_ranking',
        'universe': list(universe),
        'evidence_packets': packets,
        'output_schema': SELECTION_SCHEMA,
    }, ensure_ascii=False)


def freeze_input(universe, packets):
    """冻结模型输入：返回 (packet_ids, packets_hash)。

    packets_hash 覆盖完整 evidence_packet 内容（含价格/事件/时间），
    两组输入即使 code 相同、内容不同也会产生不同 hash；纳入批次 id，
    使 research_batch_id 能唯一对应一组冻结输入。
    """
    universe = sorted(universe)
    packet_ids = sorted(p['packet_id'] for p in packets)
    packets_hash = digest({'universe': universe,
                           'packets': [{k: p.get(k) for k in ('packet_id', 'quote', 'events', 'as_of')}
                                       for p in packets]})
    return packet_ids, packets_hash


def rank(advisor, universe, packets, now=None):
    """调用 LLM 生成结构化 Top-N 研究排名。

    返回研究批次 dict：research_batch_id / universe_hash / packets_hash /
    packet_ids / as_of / prompt_version / schema_version / model / candidates / error。
    research_batch_id 覆盖 packets_hash → 同 id 必然对应同组冻结输入。
    失败或校验失败只记录 error，不产生 proposal / approval / order。
    """
    now = now if now is not None else time.time()
    universe = list(universe)
    packet_ids, packets_hash = freeze_input(universe, packets)
    base = {
        'universe_hash': digest(universe),
        'packets_hash': packets_hash,
        'packet_ids': packet_ids,
        'as_of': utc(now),
        'prompt_version': SELECTION_PROMPT_VERSION,
        'schema_version': SELECTION_SCHEMA_VERSION,
        'model': getattr(advisor, 'model', ''),
        'universe': universe,
    }

    if advisor is None or not getattr(advisor, 'enabled', False):
        return dict(base, candidates=[], error='llm_disabled',
                    research_batch_id=stable_id('research_batch', base, 'llm_disabled'))

    raw = advisor.chat(build_selection_prompt(universe, packets), system=SELECTION_SYSTEM)
    if raw is None:
        return dict(base, candidates=[], error='llm_failed',
                    research_batch_id=stable_id('research_batch', base, 'llm_failed'))

    try:
        candidates = validate_selection(raw, universe, packets)
    except Exception as exc:
        return dict(base, candidates=[], error=f'validate_failed: {exc}',
                    research_batch_id=stable_id('research_batch', base, 'validate_failed', str(exc)))

    batch = dict(base, candidates=candidates, error=None)
    batch['research_batch_id'] = stable_id('research_batch', batch)
    return batch
