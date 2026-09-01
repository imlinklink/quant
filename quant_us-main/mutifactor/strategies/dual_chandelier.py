"""
双吊灯通道止盈止损策略 - 支持做多和做空
混合版：固定止损 + 移动止盈(trailing) + ATR大盈利扩展

设计理念（4阶段）：
  Phase 1 (亏损/微利): 固定止损线，如 -6%
  Phase 2 (小利润):    止损上移到保本价 (breakeven), 如 +4%
  Phase 3 (中等利润):  Trailing Stop 跟踪最高价回落固定比例, 如 -4%
  Phase 4 (大利润):     切换到ATR-based Trailing, 让大利润充分奔跑, 如 ≥+20%

ATR吊灯模式 (trailing_enabled=False):
  不用固定%回撤的 moving trailing，改用纯 ATR 吊灯：
    - 固定止损底线 = entry × (1 - fixed_stop_pct)  [亏损段保护，永不更低]
    - 止损线(兼止盈线) = max(固定止损, 最高价 - atr_trailing_mult × ATR)
    - 随最高价上移 (ratchet 只升不降)，ATR 变大也不会下移
  只有一条 STOP 单随最高价上移，富途自动改单。
"""
import logging
from typing import Dict, Tuple, Optional

logger = logging.getLogger(__name__)


class PositionExitState:
    """
    持仓退出状态（混合版：固定% + trailing% + ATR）

    做多示例（入场$41）：
      Phase1: 价格<42.64(+4%) → 止损@38.54(-6%)
      Phase2: 价格≥42.64      → 止损移至@41.00(保本)
      Phase3: 价格≥43.46(+6%) → Trailing激活, 最高49→stop=47.04(↓4%)
      Phase4: 价格≥49.20(+20%)→ ★切换ATR trailing★, stop=最高-2×ATR
              如果继续涨到70:  ATR trailing跟随→给更多空间让利润奔跑

    做空示例（入场$100）镜像：
      Phase1: 价格>104        → 止损@106(+6%)
      Phase2: 价格≤96         → 止损移至@100(保本)
      Phase3: 价格≤94         → Trailing激活, 最低80→stop=83.2(↑4%)
      Phase4: 价格≤80(跌20%)  → ★切换ATR trailing★, stop=最低+2×ATR
    """

    def __init__(self, entry_price: float, direction: str = 'long',
                 # === Phase 1: 固定止损 ===
                 fixed_stop_pct: float = 0.06,
                 # === Phase 2: 保本 ===
                 breakeven_pct: float = 0.04,
                 # === Phase 3: 固定Trailing ===
                 trailing_activate_pct: float = 0.06,
                 trailing_pullback_pct: float = 0.04,
                 # === Phase 4: ATR Trailing (大盈利切换) ===
                 atr_threshold_pct: float = 0.20,   # 盈利≥此值切换ATR模式
                 atr_trailing_mult: float = 2.0,
                 # === 模式开关 ===
                 trailing_enabled: bool = True):     # False=固定括号单(开仓算定,不移动)
        self.entry_price = entry_price
        self.direction = direction

        self.fixed_stop_pct = fixed_stop_pct
        self.breakeven_pct = breakeven_pct
        self.trailing_activate_pct = trailing_activate_pct
        self.trailing_pullback_pct = trailing_pullback_pct
        # 新增：ATR参数
        self.atr_threshold_pct = atr_threshold_pct
        self.atr_trailing_mult = atr_trailing_mult

        # 跟踪价格极值
        self.highest_price = entry_price
        self.lowest_price = entry_price

        # 通道线
        self.stop_line: float = 0.0
        self.profit_line: Optional[float] = None
        self.profit_line_active: bool = False

        # 内部状态标记
        self._breakeven_moved: bool = False
        self._trailing_activated: bool = False   # Phase3是否已激活
        self._atr_mode_active: bool = False       # Phase4 ATR模式是否激活
        self._trailing_enabled = trailing_enabled

        self._init_lines()

    def _init_lines(self):
        """初始化固定止损线"""
        if self.direction == 'long':
            self.stop_line = round(self.entry_price * (1 - self.fixed_stop_pct), 4)
        else:
            self.stop_line = round(self.entry_price * (1 + self.fixed_stop_pct), 4)

    def recompute(self, atr: float, current_price: float) -> bool:
        """
        每tick重新计算通道线（4阶段混合策略）
        trailing_enabled=False 时：固定括号单，不移动任何线，直接返回
        """
        if not self._trailing_enabled:
            # ===== ATR 吊灯模式（不用固定%回撤 trailing）=====
            # 止损线 = max(固定初始止损, 最高价 - ATR×倍数)，随最高价上移，ratchet 只升不降
            # 固定止损作为底线，ATR 吊灯线兼为止盈线
            self.highest_price = max(self.highest_price, current_price)
            self.lowest_price = min(self.lowest_price, current_price)
            if self.direction == 'long':
                chandelier_stop = self.highest_price - atr * self.atr_trailing_mult
                if chandelier_stop > self.stop_line:
                    self.stop_line = round(chandelier_stop, 4)
            else:
                chandelier_stop = self.lowest_price + atr * self.atr_trailing_mult
                if chandelier_stop < self.stop_line:
                    self.stop_line = round(chandelier_stop, 4)
            self.profit_line = None
            self.profit_line_active = False
            return False

        just_activated = False

        if self.direction == 'long':
            self.highest_price = max(self.highest_price, current_price)
            self.lowest_price = min(self.lowest_price, current_price)

            floating_pnl = (current_price - self.entry_price) / self.entry_price

            # ======== Phase 4: ATR Trailing (盈利≥20%) ========
            if floating_pnl >= self.atr_threshold_pct:
                if not self._atr_mode_active:
                    self._atr_mode_active = True
                    logger.info(
                        f"  [LONG] ★ 切换ATR-Trailing! 浮盈{floating_pnl*100:.1f}% "
                        f"@ {current_price:.2f} 基准高={self.highest_price:.2f} ATR={atr:.4f}")

                # ATR模式下用全局highest，给更大空间让利润奔跑
                new_stop = round(self.highest_price - atr * self.atr_trailing_mult, 4)
                # ratchet只升不降
                if new_stop > self.stop_line:
                    old_stop = self.stop_line
                    self.stop_line = new_stop
                    logger.info(f"  [LONG] ATR-Trailing {old_stop:.2f}→{new_stop:.2f} "
                               f"(最高={self.highest_price:.2f}-{self.atr_trailing_mult}×{atr:.2f})")

                self.profit_line = self.stop_line
                self.profit_line_active = True

            # ======== Phase 3: 固定% Trailing (+6%~+19%) ========
            elif floating_pnl >= self.trailing_activate_pct:
                if not self._trailing_activated:
                    self._trailing_activated = True
                    just_activated = True
                    logger.info(
                        f"  [LONG] Trailing激活! 浮盈{floating_pnl*100:.1f}% "
                        f"@ {current_price:.2f} 最高={self.highest_price:.2f}")
                new_stop = round(self.highest_price * (1 - self.trailing_pullback_pct), 4)
                if new_stop > self.stop_line:
                    old_stop = self.stop_line
                    self.stop_line = new_stop
                    if abs(old_stop - new_stop) >= 0.01:
                        logger.debug(f"  [LONG] Trailing上移 {old_stop:.2f}→{new_stop:.2f}")

                self.profit_line = self.stop_line
                self.profit_line_active = True

            # ======== Phase 2: 保本 (+4%~+5%) ========
            elif floating_pnl >= self.breakeven_pct:
                if not self._breakeven_moved:
                    old_stop = self.stop_line
                    self.stop_line = round(self.entry_price, 4)
                    self._breakeven_moved = True
                    logger.info(
                        f"  [LONG] 止损移至保本 {old_stop:.2f}→{self.entry_price:.2f} "
                        f"(浮盈{floating_pnl*100:.1f}%)")

                self.profit_line = None
                self.profit_line_active = False

            else:
                # ======== Phase 1: 固定止损 (<+4%) ========
                self.profit_line = None
                self.profit_line_active = False

        else:  # short (做空镜像)
            self.highest_price = max(self.highest_price, current_price)
            self.lowest_price = min(self.lowest_price, current_price)

            floating_pnl = (self.entry_price - current_price) / self.entry_price

            # ======== Phase 4: ATR Trailing (盈利≥20%) ========
            if floating_pnl >= self.atr_threshold_pct:
                if not self._atr_mode_active:
                    self._atr_mode_active = True
                    logger.info(
                        f"  [SHORT] ★ 切换ATR-Trailing! 浮盈{floating_pnl*100:.1f}% "
                        f"@ {current_price:.2f} 基准低={self.lowest_price:.2f} ATR={atr:.4f}")

                new_stop = round(self.lowest_price + atr * self.atr_trailing_mult, 4)
                # ratchet只降不升（做空方向）
                if new_stop < self.stop_line:
                    old_stop = self.stop_line
                    self.stop_line = new_stop
                    logger.info(f"  [SHORT] ATR-Trailing {old_stop:.2f}→{new_stop:.2f} "
                               f"(最低={self.lowest_price:.2f}+{self.atr_trailing_mult}×{atr:.2f})")

                self.profit_line = self.stop_line
                self.profit_line_active = True

            elif floating_pnl >= self.trailing_activate_pct:
                # Phase 3: 固定% Trailing
                if not self._trailing_activated:
                    self._trailing_activated = True
                    just_activated = True
                    logger.info(
                        f"  [SHORT] Trailing激活! 浮盈{floating_pnl*100:.1f}% "
                        f"@ {current_price:.2f} 最低={self.lowest_price:.2f}")
                new_stop = round(self.lowest_price * (1 + self.trailing_pullback_pct), 4)
                if new_stop < self.stop_line:
                    old_stop = self.stop_line
                    self.stop_line = new_stop
                    if abs(old_stop - new_stop) >= 0.01:
                        logger.debug(f"  [SHORT] Trailing下移 {old_stop:.2f}→{new_stop:.2f}")

                self.profit_line = self.stop_line
                self.profit_line_active = True

            elif floating_pnl >= self.breakeven_pct:
                # Phase 2: 保本
                if not self._breakeven_moved:
                    old_stop = self.stop_line
                    self.stop_line = round(self.entry_price, 4)
                    self._breakeven_moved = True
                    logger.info(
                        f"  [SHORT] 止损移至保本 {old_stop:.2f}→{self.entry_price:.2f} "
                        f"(浮盈{floating_pnl*100:.1f}%)")

                self.profit_line = None
                self.profit_line_active = False

            else:
                # Phase 1: 固定止损
                self.profit_line = None
                self.profit_line_active = False

        return just_activated

    def check_exit(self, current_price: float) -> Tuple[bool, str, float]:
        """
        检查是否触发退出

        trailing_enabled=True (移动止盈): profit_line 是一条会随最高价上移的
            跟踪止损线，价格【跌回】该线即止盈。
        trailing_enabled=False (固定括号单): profit_line 是开仓时算定的目标价，
            价格【涨到】该线即止盈（做多）；做空则【跌到】该线即止盈。
        """
        if self.direction == 'long':
            if current_price <= self.stop_line:
                return True, "STOP_LOSS", self.stop_line
            if self.profit_line_active and self.profit_line:
                if self._trailing_enabled:
                    # 移动止盈：价格跌回止盈线
                    if current_price <= self.profit_line:
                        return True, "TAKE_PROFIT", self.profit_line
                else:
                    # 固定止盈：价格涨到止盈线
                    if current_price >= self.profit_line:
                        return True, "TAKE_PROFIT", self.profit_line
        else:
            if current_price >= self.stop_line:
                return True, "STOP_LOSS", self.stop_line
            if self.profit_line_active and self.profit_line:
                if self._trailing_enabled:
                    # 移动止盈：价格涨回止盈线
                    if current_price >= self.profit_line:
                        return True, "TAKE_PROFIT", self.profit_line
                else:
                    # 固定止盈：价格跌到止盈线
                    if current_price <= self.profit_line:
                        return True, "TAKE_PROFIT", self.profit_line

        return False, "HOLD", current_price

    def get_info(self, current_price: float = None) -> Dict:
        cp = current_price or self.highest_price
        if self.direction == 'long':
            pnl = (cp - self.entry_price) / self.entry_price
        else:
            pnl = (self.entry_price - cp) / self.entry_price

        return {
            'direction': self.direction,
            'entry_price': self.entry_price,
            'highest_price': self.highest_price,
            'lowest_price': self.lowest_price,
            'stop_line': self.stop_line,
            'profit_line': self.profit_line,
            'profit_line_active': self.profit_line_active,
            'floating_pnl': pnl,
        }


class DualChandelierExitStrategy:
    """双吊灯通道策略管理器（混合版：固定止损 + 移动止盈 + ATR大盈利）"""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.atr_period = self.config.get('atr_period', 14)

        # Phase 1-3: 固定百分比参数
        self.fixed_stop_pct = self.config.get('fixed_stop_pct', 0.06)
        self.breakeven_pct = self.config.get('breakeven_pct', 0.04)
        self.trailing_activate_pct = self.config.get('trailing_activate_pct', 0.06)
        self.trailing_pullback_pct = self.config.get('trailing_pullback_pct', 0.04)

        # Phase 4: ATR参数
        self.atr_threshold_pct = self.config.get('atr_threshold_pct', 0.20)
        self.atr_trailing_mult = self.config.get('atr_trailing_mult', 2.0)

        # 模式开关：移动止盈 or 固定括号单
        self.trailing_enabled = self.config.get('trailing_enabled', True)

        self.positions: Dict[str, PositionExitState] = {}
        mode = "ATR吊灯(随最高价上移)" if not self.trailing_enabled else "4阶段移动止盈"
        logger.info(
            f"模式: {mode} | 固定止损底线-{self.fixed_stop_pct*100:.0f}%"
            + (f" | 吊灯=最高价-{self.atr_trailing_mult}xATR" if not self.trailing_enabled else
               f" | 保本+{self.breakeven_pct*100:.0f}% | "
               f"Trailing+{self.trailing_activate_pct*100:.0f}%↓{self.trailing_pullback_pct*100:.0f}% | "
               f"★ATR模式≥{self.atr_threshold_pct*100:.0f}%({self.atr_trailing_mult}x)"))

    def on_entry(self, stock_code: str, entry_price: float,
                 current_atr: float, direction: str = 'long') -> PositionExitState:
        if stock_code in self.positions:
            del self.positions[stock_code]

        state = PositionExitState(
            entry_price=entry_price,
            direction=direction,
            fixed_stop_pct=self.fixed_stop_pct,
            breakeven_pct=self.breakeven_pct,
            trailing_activate_pct=self.trailing_activate_pct,
            trailing_pullback_pct=self.trailing_pullback_pct,
            atr_threshold_pct=self.atr_threshold_pct,
            atr_trailing_mult=self.atr_trailing_mult,
            trailing_enabled=self.trailing_enabled,
        )
        self.positions[stock_code] = state

        if direction == 'long':
            init_stop_pct = (1 - state.stop_line / entry_price) * 100
        else:
            init_stop_pct = (state.stop_line / entry_price - 1) * 100

        logger.info(f"[{stock_code}] 建仓 [{direction}] @ {entry_price:.2f}, "
                   f"初始止损={state.stop_line:.2f} ({init_stop_pct:.1f}%)")
        return state

    def on_tick(self, stock_code: str, current_price: float,
                current_atr: float) -> Tuple[bool, str, float]:
        if stock_code not in self.positions:
            return False, "NO_POSITION", current_price

        state = self.positions[stock_code]
        just_activated = state.recompute(atr=current_atr, current_price=current_price)

        if not just_activated:
            should_exit, reason, exit_price = state.check_exit(current_price)
        else:
            should_exit, reason, exit_price = False, "HOLD", current_price

        if should_exit:
            if state.direction == 'long':
                pnl_pct = (exit_price - state.entry_price) / state.entry_price * 100
            else:
                pnl_pct = (state.entry_price - exit_price) / state.entry_price * 100
            logger.info(f"[{stock_code}][{state.direction}] 退出 {reason}: "
                       f"价格={current_price:.2f}, 退出={exit_price:.2f}, 盈亏={pnl_pct:+.2f}%")
            del self.positions[stock_code]

        return should_exit, reason, exit_price

    def on_exit(self, stock_code: str):
        if stock_code in self.positions:
            del self.positions[stock_code]
            logger.info(f"[{stock_code}] 手动平仓，清理状态")

    def get_position_info(self, stock_code: str) -> Optional[Dict]:
        if stock_code not in self.positions:
            return None
        return self.positions[stock_code].get_info()

    def get_all_positions(self) -> Dict[str, Dict]:
        return {code: self.positions[code].get_info() for code in self.positions}

    def get_position_direction(self, stock_code: str) -> Optional[str]:
        if stock_code not in self.positions:
            return None
        return self.positions[stock_code].direction
