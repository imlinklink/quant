"""人工确认下单（Human-in-the-loop）模块 —— quant_us 版。

quant_us 用自带的 Flask Web 提供确认页（web/app.py），
所以这里只导出提案存储，不包含独立 HTTP 服务。
"""

from .proposal_store import ProposalStore

__all__ = ['ProposalStore']
