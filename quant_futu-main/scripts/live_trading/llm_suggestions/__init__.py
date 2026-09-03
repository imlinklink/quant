"""LLM 选股建议（读宏观日报 → 生成美股/港股候选清单）。

建议永远只是"建议"：页面点「加入观察池」后才进入监控列表，
之后仍走抄底监控 + 买入确认页，大模型不直接下单。
"""

from .store import load_latest, save_latest, update_item_status
from .watchlist import add_us_watch, add_hk_watch

__all__ = ['load_latest', 'save_latest', 'update_item_status', 'add_us_watch', 'add_hk_watch']
