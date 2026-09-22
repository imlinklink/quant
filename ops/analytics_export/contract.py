"""快照契约：统一信封、状态词汇、以及「零与空值」的唯一判据。

需求 `docs/decision-visibility-product-requirements-2026-09-22.md`：

- §6 每个响应至少含 `scope_id` / `account_id|experiment_id` / `strategy_version` /
  `as_of` / `generated_at` / `source_ref` / `data_status` / `missing_reasons`；
- §7 **只有真实计算出来的零才显示 `0`**；「没有对象」「还没发生」「没有采集」各自有状态。
"""
from __future__ import annotations

# ---- 状态码（需求 §7 的九态 + 词表未覆盖）------------------------------------
# **用稳定 ASCII 码进 JSON，中文只在页面模板里映射一份**：需求给的中文串是**展示约定**，
# 当 JSON 枚举值用会让契约随文案漂移，而这个仓库因「同一件事两份定义」出过多次真 bug。
OK = 'OK'                                # 有效数值
NO_OBJECT = 'NO_OBJECT'                  # 暂无对象
PENDING_EXECUTION = 'PENDING_EXECUTION'  # 等待执行
PENDING_SETTLEMENT = 'PENDING_SETTLEMENT'  # 等待结算
INSUFFICIENT_SAMPLE = 'INSUFFICIENT_SAMPLE'  # 样本不足
NOT_COLLECTED = 'NOT_COLLECTED'          # 未采集
STALE = 'STALE'                          # 数据过期
READ_FAILED = 'READ_FAILED'              # 读取失败
NOT_APPLICABLE = 'NOT_APPLICABLE'        # 不适用
UNCLASSIFIED = 'UNCLASSIFIED'            # 词表未覆盖（同样是「未采集」，但不静默归桶）
# **「没做」必须与「没到」分开**：需求 §7 那九态描述的是数据的世界；但一块**功能尚未实现**
# 的展示如果只标 NOT_COLLECTED，读起来像「等数据到了就会自动出现」。用户 review 点名过这条，
# 故单列一态，页面上直说「未实现」。
NOT_IMPLEMENTED = 'NOT_IMPLEMENTED'

STATUSES = (OK, NO_OBJECT, PENDING_EXECUTION, PENDING_SETTLEMENT, INSUFFICIENT_SAMPLE,
            NOT_COLLECTED, STALE, READ_FAILED, NOT_APPLICABLE, UNCLASSIFIED,
            NOT_IMPLEMENTED)

# ---- 数据源整体状态 -----------------------------------------------------------
DS_OK = 'OK'
DS_NO_OBJECT = 'NO_OBJECT'
DS_STALE = 'STALE'
DS_PARTIAL = 'PARTIAL'
DS_READ_FAILED = 'READ_FAILED'
DS_INCONSISTENT = 'INCONSISTENT'          # 声明与账本互相矛盾 ⇒ 页面显示「结论待核对」

DATA_STATUSES = (DS_OK, DS_NO_OBJECT, DS_STALE, DS_PARTIAL, DS_READ_FAILED, DS_INCONSISTENT)

ENVELOPE_VERSION = 1
ENVELOPE_FIELDS = ('envelope_version', 'generation_id', 'scope_id', 'account_id',
                   'experiment_id', 'strategy_version', 'as_of', 'generated_at',
                   'source_ref', 'data_status', 'missing_reasons', 'sections')


def present(body: dict, key: str, unit: str | None = None, *, label: str | None = None) -> dict:
    """从账本 body 里取一个字段，**键在不在**决定它是「值」还是「未采集」。

    这是整个契约里最容易做错的一行：`body.get(key)` 会把「值为 0」与「没有这个键」
    合并成同一个结果，于是 schema 9 里不存在的保护位会显示成 `0`（一个从未测到的数
    被当成测到了）。判据只能是 `key in body`。

    唯一允许返回 `value == 0` 的情形，是**账本里确实写了 0**。
    """
    if not isinstance(body, dict) or key not in body:
        return {'label': label or key, 'value': None, 'unit': unit, 'status': NOT_COLLECTED,
                'why': f'该账本的持仓/状态体里没有 {key}'}
    return {'label': label or key, 'value': body[key], 'unit': unit, 'status': OK}


def metric(value, unit: str | None = None, *, status: str | None = None, label: str | None = None,
           sample_count=None, window=None, cost_basis=None, maturity=None, why: str = '') -> dict:
    """把一个指标包成需求 §6/§7 要求的形状。

    `status` 不给就按「有没有值」推断：`None` **不是** 0，默认落成「暂无可算」，
    绝不当成 0 —— 空值自动转零正是需求 §7 末条禁止的。
    """
    if status is None:
        status = OK if value is not None else NOT_COLLECTED
    out = {'label': label, 'value': value, 'unit': unit, 'status': status}
    if sample_count is not None:
        out['sample_count'] = sample_count
    if window is not None:
        out['window'] = window
    if cost_basis is not None:
        out['cost_basis'] = cost_basis
    if maturity is not None:
        out['maturity'] = maturity
    if why:
        out['why'] = why
    return out


def missing(field: str, why: str, *, status: str = NOT_COLLECTED, source: str = '') -> dict:
    """一条「缺什么、为什么缺」的记录，进信封的 `missing_reasons`（需求 §6 硬要求）。"""
    return {'field': field, 'status': status, 'why': why, 'source': source}


def envelope(*, generation_id: str, scope_id: str, scope_kind: str, experiment_id: str,
             strategy_version: dict, as_of, generated_at: str, source_ref: dict,
             data_status: str, missing_reasons: list, sections: dict,
             account_id: str | None = None) -> dict:
    """按 §6 拼统一信封。**字段名固定**，缺一个都会被信封测试抓住。"""
    if data_status not in DATA_STATUSES:
        raise ValueError(f'UNKNOWN_DATA_STATUS:{data_status}')
    return {
        'envelope_version': ENVELOPE_VERSION,
        'generation_id': generation_id,
        'scope_id': scope_id,
        'scope_kind': scope_kind,
        'account_id': account_id,
        'experiment_id': experiment_id,
        'strategy_version': strategy_version,
        'as_of': as_of,
        'generated_at': generated_at,
        'source_ref': source_ref,
        'data_status': data_status,
        'missing_reasons': list(missing_reasons),
        'sections': sections,
    }
