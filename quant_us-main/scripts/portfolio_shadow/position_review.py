"""持仓评审（影子侧）：角色差异面 + 主体定义。

编排序列全部在 `overlay_review.OverlayReviewer`（与入场共用租约/恢复/冻结语义）；
这里只声明持仓角色与入场不同的三处：主体键（含执行日）、主体身份字段、以及
输出契约（Position v2 + packet 绑定）。

**评审主体是 R 账户的持仓。** R 是规则决定的，与 L 的历史无关，因此决策流可独立统计；
动作应用到 L 时按档位相对计算，L 已平掉的代码记 `ignored_not_held`。
"""
from __future__ import annotations

from dataclasses import dataclass

from .overlay_review import OverlayReviewer, OverlaySpec
from .position_overlay import (POSITION_ACTION_SCHEMA_VERSION, decide_position_overlay,
                               subject_key_for, validate_position_output)

# prompt 与输出契约一起构成「同一个问题」的一部分；两者都与实盘 position-v2 评审不同
# （那条路走 DecisionEngine，输出里没有 packet 绑定字段），故各用独立版本号。
POSITION_ACTION_PROMPT_VERSION = 'position-action-v1'

POSITION_ACTION_SPEC = OverlaySpec(role='position',
                                   prompt_version=POSITION_ACTION_PROMPT_VERSION,
                                   schema_version=POSITION_ACTION_SCHEMA_VERSION,
                                   validate=validate_position_output,
                                   decide=decide_position_overlay)


@dataclass(frozen=True)
class PositionSubject:
    """一次持仓评审的主体：某个入场机会所持的仓位，在某个执行日接受评审。"""
    opportunity_id: str
    security_id: str
    reviewed_session: str        # 评审日 T（信号在 T 收盘后冻结）
    execution_session: str       # 执行日 T+1（动作在 T+1 开盘应用）

    def key(self) -> str:
        """账本主体键。按执行日定键：结算时按执行日直接取回，无需再推前置会话。"""
        return subject_key_for(self.opportunity_id, self.execution_session)


class PositionReviewer(OverlayReviewer):
    """L 侧持仓评审的编排者。R 账户不经过这里（照常服从规则退出）。"""

    spec = POSITION_ACTION_SPEC

    def subject_key(self, subject: PositionSubject) -> str:
        return subject.key()

    def subject_identity(self, subject: PositionSubject) -> dict:
        return {'position_key': subject.key(),
                'opportunity_id': subject.opportunity_id,
                'security_id': subject.security_id,
                'reviewed_session': subject.reviewed_session,
                'execution_session': subject.execution_session}


__all__ = ['POSITION_ACTION_PROMPT_VERSION', 'POSITION_ACTION_SPEC', 'PositionReviewer',
           'PositionSubject']
