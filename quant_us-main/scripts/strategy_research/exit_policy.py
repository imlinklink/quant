"""机械利润保护规则 —— **唯一一份定义**（规划 §4.1，预登记 `EXIT-PROTECT-20260921`）。

规则（全程整数微美元，无浮点）：

  激活    H ≥ 入场价 + activation × R0/股      R0/股 = 入场价 − 初始硬止损价（成交时冻结）
  保护线  P = H − k × ATR14                    k = 3.0（经典 chandelier 倍数）
  生效    stop ← max(初始硬止损, 既有 stop, P)   —— **只收紧，绝不放宽**
  时点    T 收盘算、T+1 生效

为什么是这套参数（**不是网格搜出来的**，预登记里逐条写了理由）：

  · ATR14：与既有 `medium_initial_stop` 同一波动率口径，不引入第二套度量；
  · k = 3.0：> 既有硬止损的 2.5 倍 ATR ⇒ 保证保护线只在确有浮盈之后才有意义，
    避免一有浮盈就贴着价格收紧；
  · 激活门槛 1.0 × R0：与系统全程以 R 计价的约定一致；且因 R0 ≥ 2.5×ATR14，
    激活时保护线约在盈亏平衡附近 —— 语义是「先保本、再逐日跟踪」。

**参数改动 = 另立登记**。把它写成 dataclass 而不是散落的常量，是为了让"这一份定义"
可以被引擎（`paper_engine.step`）、机会级反事实、以及测试同时引用同一处。
"""
from __future__ import annotations

from dataclasses import dataclass

VERSION = 'PROFIT_PROTECTION_CHANDELIER_V1'
ATR_PERIOD = 14
ATR_WARMUP_BARS = 14


@dataclass(frozen=True)
class ProtectionDecision:
    """一次评估的结果。`stop_micro=None` 表示**不安排更新**（未激活 / 线未上升 / ATR 不可用）。"""
    activated: bool
    stop_micro: int | None
    activation_price_micro: int
    line_micro: int | None
    reason: str  # 'NOT_ACTIVATED' / 'NO_CHANGE' / 'ATR_UNAVAILABLE' / 'RAISED'


@dataclass(frozen=True)
class ProfitProtection:
    """获利激活 + ATR 跟踪保护线。分数用整数 num/den，避免浮点进入会计。"""

    version: str = VERSION
    activation_num: int = 1
    activation_den: int = 1
    atr_num: int = 3
    atr_den: int = 1
    atr_period: int = ATR_PERIOD

    def as_dict(self) -> dict:
        return {'version': self.version, 'activation_num': self.activation_num,
                'activation_den': self.activation_den, 'atr_num': self.atr_num,
                'atr_den': self.atr_den, 'atr_period': self.atr_period}

    def risk_per_share_micro(self, entry_price_micro: int, initial_stop_micro: int) -> int:
        """R0/股 = 入场价 − 初始硬止损价（成交时冻结，不随浮盈变化）。"""
        return entry_price_micro - initial_stop_micro

    def activation_price_micro(self, entry_price_micro: int, initial_stop_micro: int) -> int:
        """激活价 = 入场价 + activation × R0/股（向下取整，整数微）。"""
        r0 = self.risk_per_share_micro(entry_price_micro, initial_stop_micro)
        return entry_price_micro + (r0 * self.activation_num) // self.activation_den

    def line_micro(self, high_close_micro: int, atr14_micro: int) -> int:
        """保护线 = H − k × ATR14。"""
        return high_close_micro - (atr14_micro * self.atr_num) // self.atr_den

    def evaluate(self, *, entry_price_micro: int, initial_stop_micro: int,
                 current_stop_micro: int, high_close_micro: int,
                 atr14_micro: int | None, activated: bool) -> ProtectionDecision:
        """用**截至 T 收盘**的已知量算 T+1 生效的保护线。

        `activated` 是**粘性的**（已激活就保持激活）：除息会把 H 按每股分红下调，
        非粘性实现会在除息后「取消激活」，把一个已经保过本的持仓退回无保护状态。
        """
        activation_price = self.activation_price_micro(entry_price_micro, initial_stop_micro)
        if not activated and high_close_micro >= activation_price:
            activated = True
        if not activated:
            return ProtectionDecision(False, None, activation_price, None, 'NOT_ACTIVATED')
        if atr14_micro is None or atr14_micro <= 0:
            # 数据不足：保持已生效保护线，不放宽硬止损（§4.2）
            return ProtectionDecision(True, None, activation_price, None, 'ATR_UNAVAILABLE')
        line = self.line_micro(high_close_micro, atr14_micro)
        if line <= current_stop_micro:
            # 只收紧：不高于既有保护线时**不安排更新**，事件流里不出现空转
            return ProtectionDecision(True, None, activation_price, line, 'NO_CHANGE')
        return ProtectionDecision(True, line, activation_price, line, 'RAISED')


#: 本轮唯一使用的策略实例。引擎与机会级反事实必须引用**这一个**。
DEFAULT_PROTECTION = ProfitProtection()
