"""人工确认下单（Human-in-the-loop）模块。

买入信号不再直接下单，而是推送到本地确认页，
由用户点击「下单 / 拒绝」后再执行。
"""

from .proposal_store import ProposalStore
from .server import ApprovalServer

__all__ = ['ProposalStore', 'ApprovalServer']
