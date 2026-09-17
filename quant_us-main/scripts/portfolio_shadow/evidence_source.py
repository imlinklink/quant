"""证据来源薄适配器（设计 §5.2）。

统一接口：`load_events(security_id, cutoff) -> EvidenceFetchResult`。
优先连接已有公告/财报/事件存储，不另建全市场新闻平台；首版提供 JSONL 导入适配器。

**核心条款（设计 §5.2）**：`observed_at` 是**系统实际入库时间**，不由导入文件追溯指定。
文件若声称历史观测时间，作为 `claimed_observed_at` 独立元数据保留。理由：点对点可得性
要证明的是「我们当时确实看得到」，这件事只有我们的采集管道知道；来源文件说的时间是
它的自述，不能拿来当我们的观测证据。

因此导入与读取分成两步：`import_evidence_jsonl` 在**入库那一刻**把 `observed_at` 写死，
`JsonlEvidenceSource` 每次读取都拿到同一个值。若在读取时才取当前时刻，每次重跑都会得到
不同的观测时间，等于让证据随重跑而"变得可得"。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from scripts.evidence.evidence_store import normalize_evidence

from .evidence import events_from_records, policy_for_mode

# 默认事件窗口与容量。设计 §5.2 要求二者固定在 manifest；这里的默认值只用于
# 直接调用适配器的场景，正式运行必须由 manifest 显式给定。
DEFAULT_WINDOW_DAYS = 30
DEFAULT_MAX_EVENTS = 50

# 导入时保留的附加列（normalize_evidence 只返回 REQUIRED_COLUMNS，会剥掉其余列）
EXTRA_COLUMNS = ('summary', 'summary_text', 'text', 'headline', 'title', 'excerpt',
                 'claimed_observed_at')

FETCH_STATUSES = ('OK', 'EMPTY', 'FAILED', 'NOT_CONFIGURED')


@dataclass(frozen=True)
class EvidenceFetchResult:
    """一次证据采集的结果（设计 §5.2 的返回值契约）。"""
    security_id: str
    cutoff: str
    status: str = 'OK'  # FETCH_STATUSES 之一
    events: tuple = ()
    exclusions: tuple = ()
    meta: dict = field(default_factory=dict)
    source: str = ''
    error: str = ''

    @property
    def is_usable(self) -> bool:
        """是否具备「语义证据」—— 空事件不算失败，但也不能声称已完成语义评审。"""
        return self.status == 'OK' and bool(self.events)


def _ingest_stamp(value=None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    stamp = pd.to_datetime(value, errors='coerce', utc=True)
    if pd.isna(stamp):
        raise ValueError(f'INGESTED_AT_INVALID:{value}')
    return stamp.tz_convert(timezone.utc).isoformat()


def import_evidence_jsonl(source, destination, *, ingested_at=None,
                          default_license_tag: str = 'research-use-only') -> dict:
    """把真实事件 JSONL 导入为规范证据存储，**入库时刻在此写死**。

    输入每行（设计 §5.1 的单条事件字段）：
        security_id, event_type, summary, excerpt, source_url, source_type,
        published_at, quality_status(可选), license_tag(可选),
        claimed_observed_at(可选，仅作元数据保留)

    输出：evidence_store schema 的 CSV.GZ，附加 EXTRA_COLUMNS。
    """
    rows = [r for r in (_read_jsonl(Path(source)))]
    if not rows:
        raise ValueError('EVIDENCE_SOURCE_EMPTY')
    stamp = _ingest_stamp(ingested_at)
    records = []
    for row in rows:
        if not row.get('security_id'):
            raise ValueError('EVIDENCE_SECURITY_ID_MISSING')
        published = row.get('published_at')
        if not published:
            raise ValueError(f'EVIDENCE_PUBLISHED_AT_MISSING:{row.get("security_id")}')
        summary = str(row.get('summary') or row.get('excerpt') or '')
        records.append({
            'security_id': str(row['security_id']),
            'symbol_as_published': row.get('symbol_as_published') or '',
            'kind': str(row.get('event_type') or row.get('kind') or 'event'),
            'source_id': str(row.get('source_type') or row.get('source_id') or 'jsonl_import'),
            'source_record_id': str(row.get('source_record_id')
                                    or f'{row["security_id"]}|{published}|{row.get("event_type", "")}'),
            'source_url_or_archive_path': str(row.get('source_url') or ''),
            'event_at': published,
            'published_at': published,
            # ↓ 关键：入库时间，不是文件自述
            'observed_at': stamp,
            'ingested_at': stamp,
            'version_id': str(row.get('version_id') or 'v1'),
            'supersedes_id': row.get('supersedes_id'),
            'content_hash': row.get('content_hash'),
            'summary_hash': row.get('summary_hash'),
            'quality_status': str(row.get('quality_status') or 'unverified'),
            'availability_proof': f'imported_at:{stamp}',
            'license_tag': str(row.get('license_tag') or default_license_tag),
            'summary': summary,
            'excerpt': str(row.get('excerpt') or ''),
            'claimed_observed_at': row.get('claimed_observed_at'),
        })
    frame = pd.DataFrame(records)
    normalized = normalize_evidence(frame)
    for column in EXTRA_COLUMNS:                 # normalize 只返回 REQUIRED_COLUMNS
        if column in frame.columns:
            normalized[column] = list(frame[column])
    out = Path(destination)
    out.parent.mkdir(parents=True, exist_ok=True)
    normalized.to_csv(out, index=False)
    return {'destination': str(out), 'rows': len(normalized), 'observed_at': stamp,
            'source': str(source)}


def _read_jsonl(path: Path) -> list[dict]:
    import json
    out = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


class JsonlEvidenceSource:
    """从规范证据存储读取（由 `import_evidence_jsonl` 产出）。

    事件窗口与容量固定在 manifest（设计 §5.2），由调用方传入；容量截断不静默 ——
    被截掉的数量记进 `exclusion_reasons`，避免「预算不足」被伪装成「没有风险」。
    """

    def __init__(self, path, *, window_days: int = DEFAULT_WINDOW_DAYS,
                 max_events: int = DEFAULT_MAX_EVENTS):
        self.path = Path(path)
        self.window_days = int(window_days)
        self.max_events = int(max_events)
        if self.window_days <= 0 or self.max_events <= 0:
            raise ValueError(f'EVIDENCE_POLICY_INVALID:{self.window_days}/{self.max_events}')

    def _records(self) -> pd.DataFrame:
        return pd.read_csv(self.path)

    def load_events(self, security_id: str, cutoff: str, *,
                    evidence_mode: str = 'strict') -> EvidenceFetchResult:
        """截至 `cutoff` 已观察到的该证券事件。

        `cutoff` 是**证据采集时刻**（见 evidence.entry_collection_time），不是信号日收盘。
        """
        try:
            records = self._records()
        except Exception as exc:                      # 采集失败要显式留痕，不能装作没有事件
            return EvidenceFetchResult(security_id=str(security_id), cutoff=str(cutoff),
                                       status='FAILED', source=str(self.path),
                                       error=f'{type(exc).__name__}:{exc}')
        cutoff_dt = pd.to_datetime(cutoff, errors='coerce', utc=True)
        if pd.isna(cutoff_dt):
            return EvidenceFetchResult(security_id=str(security_id), cutoff=str(cutoff),
                                       status='FAILED', source=str(self.path),
                                       error=f'CUTOFF_INVALID:{cutoff}')
        window_start = cutoff_dt - timedelta(days=self.window_days)
        published = pd.to_datetime(records.get('published_at'), errors='coerce', utc=True)
        in_window = records[(published.notna()) & (published >= window_start)]
        events, exclusions, meta = events_from_records(
            in_window, security_id, cutoff, policy=policy_for_mode(evidence_mode))

        # 容量截断：保留最近的事件，被截掉的量记进包内，不静默丢弃
        if len(events) > self.max_events:
            ordered = sorted(events, key=lambda e: (str(e.get('published_at')), e['evidence_id']))
            truncated = len(ordered) - self.max_events
            events = ordered[-self.max_events:]
            meta = {**meta, 'exclusion_reasons': {
                **meta.get('exclusion_reasons', {}), 'CAPACITY_TRUNCATED': truncated}}
            meta['included_event_count'] = len(events)
        return EvidenceFetchResult(
            security_id=str(security_id), cutoff=cutoff_dt.isoformat(),
            status='OK' if events else 'EMPTY', events=tuple(events),
            exclusions=tuple(exclusions), meta=meta, source=str(self.path))
