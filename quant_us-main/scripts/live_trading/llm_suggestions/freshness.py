"""建议池时效与来源（任务 B）：时间解析、距今与 freshness 状态。

纯函数，不改原数据。区分「报告中的说法」与「已核实数据」：
报告生成时间新，不等于它引用的行情/新闻也新；来源时间缺失/未来/无效单独处理。
无时区时间不默认 UTC，按系统本地时区解释并标注「时区未知」。
"""
import time
from datetime import datetime, timezone


def _local_tz():
    return datetime.now().astimezone().tzinfo


def parse_time(value, now=None):
    """解析时间，返回 dict(status, epoch, label)。

    status ∈ {ok, unknown_tz, future, invalid, missing}
    - ok: 带时区、可比较
    - unknown_tz: 无时区，已按本地时区解释（不默认为 UTC）
    - future: 明显晚于 now（允许 60s 时钟偏差）
    - invalid: 无法解析
    - missing: 空/缺失
    """
    now = now if now is not None else time.time()
    if value is None or value == '':
        return {'status': 'missing', 'epoch': None, 'label': '缺失'}

    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
        status = 'ok'
    else:
        s = str(value)
        try:
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return {'status': 'invalid', 'epoch': None, 'label': '时间无效'}
        if dt.tzinfo is None or dt.utcoffset() is None:
            dt = dt.replace(tzinfo=_local_tz())
            status = 'unknown_tz'
        else:
            status = 'ok'

    epoch = dt.timestamp()
    if epoch > now + 60:
        return {'status': 'future', 'epoch': epoch, 'label': '未来时间'}
    return {'status': status, 'epoch': epoch, 'label': age_label(now - epoch)}


def age_label(seconds):
    """把秒数转成人类可读的「距今」标签。"""
    seconds = max(0, seconds)
    if seconds < 60:
        return f'{int(seconds)} 秒前'
    if seconds < 3600:
        return f'{int(seconds // 60)} 分钟前'
    if seconds < 86400:
        return f'{int(seconds // 3600)} 小时前'
    return f'{int(seconds // 86400)} 天前'


def _fresh_from_epoch(epoch, status, cfg, now):
    """根据 epoch 与配置阈值给 freshness 状态。"""
    if epoch is None:
        return status  # missing / invalid 原样透出
    if status == 'future':
        return 'future'
    fresh_sec = float(cfg.get('fresh_seconds', 24 * 3600))
    stale_sec = float(cfg.get('stale_seconds', 7 * 24 * 3600))
    age = max(0, now - epoch)
    if status == 'unknown_tz':
        return 'unknown_tz' if age <= stale_sec else 'stale'
    if age <= fresh_sec:
        return 'fresh'
    if age <= stale_sec:
        return 'stale'
    return 'stale'


def assess(data, cfg, now=None):
    """给建议清单附加时效字段（返回新 dict，不改原数据）。

    cfg 支持 llm_suggestions 段下的 fresh_seconds / stale_seconds 阈值。
    """
    now = now if now is not None else time.time()
    cfg = cfg or {}
    out = dict(data)

    gen = parse_time(data.get('generated_at'), now)
    out['_generated'] = {
        'status': gen['status'],
        'label': gen['label'],
        'epoch': gen['epoch'],
    }

    reports = data.get('reports') or {}
    out['_sources'] = {}
    for kind in ('pre', 'post'):
        key = f'{kind}_mtime'
        src = parse_time(reports.get(key), now) if reports.get(key) else {'status': 'missing', 'epoch': None, 'label': '缺失'}
        src['fresh'] = _fresh_from_epoch(src['epoch'], src['status'], cfg, now)
        out['_sources'][kind] = src

    out['_freshness'] = _fresh_from_epoch(gen['epoch'], gen['status'], cfg, now)
    return out
