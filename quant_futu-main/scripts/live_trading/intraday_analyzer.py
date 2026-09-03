"""
日内K线技术分析器
基于5分钟K线计算RSI、布林带等指标，输出买入信号评分
支持混合模式：RSI/布林带用1分钟K线（反应快），量/形态用5分钟（更稳）

Phase 2 增强：
- TrendDetector: 用前N根1分钟K线判定上涨/震荡/下跌行情
- 追涨打分系统(analyze_breakout): 上涨行情时使用，与抄底系统对称设计
"""
import logging
from typing import Dict, Optional, List, Tuple
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class TrendDetector:
    """
    行情趋势检测器

    利用前N根1分钟K线的多维度特征，判断当前行情性质：
    - uptrend:   上涨行情 → 使用追涨打分系统
    - sideways:  震荡行情 → 使用抄底打分系统（现有逻辑）
    - downtrend: 下跌行情 → 使用抄底打分系统（现有逻辑）

    判定维度：
    1. 区间收益率：观察窗口内价格涨跌幅
    2. 阳线比例：阳线数量占总K线数的比例
    3. 价格区间位置：当前价在观察区间内的相对位置
    4. 均线斜率：短期均线的方向性
    """

    def __init__(self, config: Optional[Dict] = None):
        cfg = (config or {}).get('analysis', {}).get('trend_detection', {})
        self.enabled = cfg.get('enabled', True)
        self.observe_bars = cfg.get('observe_bars', 30)

        # 区间收益率阈值
        self.uptrend_return_pct = cfg.get('uptrend_return_pct', 0.005)
        self.sideways_range_pct = cfg.get('sideways_range_pct', 0.003)

        # K线形态
        self.bull_bar_ratio_uptrend = cfg.get('bull_bar_ratio_uptrend', 0.60)
        self.bull_bar_ratio_downtrend = cfg.get('bull_bar_ratio_downtrend', 0.40)

        # 价格位置
        self.position_high_pct = cfg.get('position_high_pct', 65)
        self.position_low_pct = cfg.get('position_low_pct', 35)

        # 均线斜率
        self.ma_slope_positive = cfg.get('ma_slope_positive', 0.0001)

    def detect(self, bars_1m: Optional[pd.DataFrame],
            prev_close: Optional[float] = None) -> Tuple[str, Dict]:
        """
        实战版趋势判定：强势股专用
        只要：涨得多 + 阳线多 + 价格在均线上 = 直接判定 uptrend
        """
        if not self.enabled or bars_1m is None or len(bars_1m) < 10:
            return 'sideways', {'reason': '数据不足或未启用'}

        obs_bars = bars_1m.iloc[-self.observe_bars:] if len(
            bars_1m) >= self.observe_bars else bars_1m
        n = len(obs_bars)
        if n < 10:
            return 'sideways', {'reason': f'K线不足({n}根)'}

        closes = obs_bars['close'].values.astype(float)
        opens = obs_bars['open'].values.astype(float)
        highs = obs_bars['high'].values.astype(float)
        lows = obs_bars['low'].values.astype(float)

        # ========== 实战核心：以窗口起点为基准，看真正涨幅 ==========
        first_close = closes[0]
        last_close = closes[-1]
        change = (last_close - first_close) / first_close

        # 阳线比例
        bull_ratio = np.sum(closes > opens) / n

        # 均价位置
        avg = np.mean(closes)
        above_avg = 1 if last_close > avg else 0

        # 创新高
        is_high = 1 if last_close >= np.max(highs) else 0

        # ========== 实战打分：宽松，抓牛股 ==========
        score_up = 0
        score_down = 0

        # 1. 上涨加分
        if change > 0.001:
            score_up += 2
        elif change < -0.002:
            score_down += 2

        # 2. 阳线多
        if bull_ratio >= 0.5:
            score_up += 1
        else:
            score_down += 1

        # 3. 在均线上
        if above_avg:
            score_up += 1

        # 4. 创新高
        if is_high:
            score_up += 1

        # ========== 最终判定（超级宽松） ==========
        if score_up >= 2:
            trend = 'uptrend'
        elif score_down >= 3:
            trend = 'downtrend'
        else:
            trend = 'sideways'

        details = {
            'return_pct': round(change * 100, 2),
            'bull_ratio': round(bull_ratio * 100, 1),
            'price_position': round((last_close - np.min(lows)) / (
                        np.max(highs) - np.min(lows) + 1e-8) * 100, 1),
            'ma_slope': 0.0,
            'score_up': score_up,
            'score_down': score_down,
        }

        return trend, details


class IntradayAnalyzer:
    """
    日内K线技术分析器

    Phase 1 指标：
    1. RSI(14) - 超卖检测（含连续下跌过滤，减少假信号）
    2. 布林带(20, 2σ) - 支撑/压力检测
    3. 成交量配合 - 缩量下跌/放量反弹

    评分规则：
    - 6分以上 → 强买入信号
    - 4-5分 → 观望
    - 4分以下 → 不买
    """

    def __init__(self, config: Optional[Dict] = None):
        self.cfg = (config or {}).get('analysis', {})

        # RSI参数
        self.rsi_period = self.cfg.get('rsi_period', 14)
        self.rsi_oversold = self.cfg.get('rsi_oversold', 32)   # 原30，放宽更快捕捉
        self.rsi_severe = self.cfg.get('rsi_severe', 28)       # 原25，放宽更快捕捉

        # 布林带参数
        self.bb_period = self.cfg.get('bb_period', 20)
        self.bb_std = self.cfg.get('bb_std', 2.0)
        self.bb_tight = self.cfg.get('bb_tight_pct', 20)    # 原10%，放宽更易触发
        self.bb_low = self.cfg.get('bb_low_pct', 30)        # 原25%

        # 成交量参数
        self.volume_ratio_threshold = self.cfg.get('volume_ratio_threshold', 1.5)
        self.min_bars_required = self.cfg.get('min_bars_required', 10)

        # 评分阈值
        self.strong_buy_threshold = self.cfg.get('strong_buy_threshold', 6)
        self.watch_threshold = self.cfg.get('watch_threshold', 4)

        # ─── 追涨打分参数（Phase 2）─────────────────────────────
        bo_cfg = (config or {}).get('momentum', {})
        self.bo_rsi_strong = bo_cfg.get('rsi_strong', 65)
        self.bo_rsi_moderate = bo_cfg.get('rsi_moderate', 55)
        self.bo_rsi_weak = bo_cfg.get('rsi_weak', 50)
        self.bo_bb_breakout_pct = bo_cfg.get('bb_breakout_pct', 80)
        self.bo_bb_upper_pct = bo_cfg.get('bb_upper_pct', 55)
        self.bo_volume_surge_ratio = bo_cfg.get('volume_surge_ratio', 1.5)
        self.bo_max_rally_pct = bo_cfg.get('max_rally_pct', 0.03)
        self.bo_strong_buy_threshold = bo_cfg.get('strong_buy_threshold', 6)
        self.bo_watch_threshold = bo_cfg.get('watch_threshold', 4)

    # ─── 主分析入口 ─────────────────────────────────────────────────────

    def analyze(self, stock_code: str, bars: pd.DataFrame,
                current_price: float) -> Dict:
        """
        分析一只股票的5分钟K线，返回信号评分

        Args:
            stock_code: 股票代码
            bars: 5分钟K线 DataFrame（当日日内）
            current_price: 当前价格

        Returns:
            dict: {
                'score': int,           # 总评分 0-10
                'signal': str,           # 'strong_buy' | 'watch' | 'no_buy'
                'rsi': float,            # RSI(14) 值
                'rsi_score': int,        # RSI得分
                'bb_position': float,    # 布林带位置（0=下轨，100=上轨）
                'bb_score': int,         # 布林带得分
                'volume_score': int,     # 成交量得分
                'candle_score': int,     # K线形态得分
                'details': str,          # 详细描述
                'bars_count': int,       # K线根数
            }
        """
        bars_count = len(bars) if bars is not None else 0

        if bars_count < self.min_bars_required:
            return {
                'score': 0,
                'signal': 'no_buy',
                'rsi': None,
                'rsi_score': 0,
                'bb_position': None,
                'bb_score': 0,
                'volume_score': 0,
                'candle_score': 0,
                'details': f'K线数据不足({bars_count}根)，跳过分析',
                'bars_count': bars_count,
            }

        bars = bars.copy()

        # 1. 计算RSI
        rsi_value, rsi_score = self._calc_rsi(bars)
        logger.debug(f"{stock_code} RSI={rsi_value:.1f} score={rsi_score}")

        # 2. 计算布林带
        bb_position, bb_score = self._calc_bollinger(bars, current_price)
        logger.debug(f"{stock_code} BB位置={bb_position:.1f}% score={bb_score}")

        # 3. 成交量分析
        volume_score = self._calc_volume(bars)
        logger.debug(f"{stock_code} 成交量得分={volume_score}")

        # 4. K线形态
        candle_score, candle_detail = self._calc_candle(bars)
        logger.debug(f"{stock_code} 形态得分={candle_score} {candle_detail}")

        # 总分
        total = rsi_score + bb_score + volume_score + candle_score
        total = max(0, min(10, total))

        if total >= self.strong_buy_threshold:
            signal = 'strong_buy'
        elif total >= self.watch_threshold:
            signal = 'watch'
        else:
            signal = 'no_buy'

        details = (
            f"RSI={rsi_value:.1f}(+{rsi_score}) "
            f"BB={bb_position:.0f}%(+{bb_score}) "
            f"量(+{volume_score}) "
            f"形(+{candle_score}) "
            f"{candle_detail}"
        )

        return {
            'score': total,
            'signal': signal,
            'rsi': rsi_value,
            'rsi_score': rsi_score,
            'bb_position': bb_position,
            'bb_score': bb_score,
            'volume_score': volume_score,
            'candle_score': candle_score,
            'details': details,
            'bars_count': bars_count,
        }

    # ─── RSI（含连续下跌过滤） ────────────────────────────────────────────

    def _calc_rsi(self, bars: pd.DataFrame) -> Tuple[float, int]:
        """
        计算RSI(14)及得分

        增强逻辑：RSI超卖时，必须连续下跌至少2根才给分
        避免在刚开始下跌时就触发（假信号），只捕捉真正触底的反弹
        """
        closes = bars['close'].values
        if len(closes) < self.rsi_period + 1:
            return 50.0, 0

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # Wilder's 平滑 RSI（SMA 初始化 + alpha 平滑）
        alpha = 1.0 / self.rsi_period
        period = self.rsi_period
        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))
        for i in range(period, len(gains)):
            avg_gain = alpha * gains[i] + (1 - alpha) * avg_gain
            avg_loss = alpha * losses[i] + (1 - alpha) * avg_loss

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # RSI得分（简化逻辑，与verify_intraday_analyzer一致）
        score = 0
        if rsi < self.rsi_severe:
            score = 3
        elif rsi < self.rsi_oversold:
            score = 2
        elif rsi < 40:
            score = 1

        return float(rsi), score

    # ─── 布林带 ──────────────────────────────────────────────────────────

    def _calc_bollinger(self, bars: pd.DataFrame, current_price: float) -> Tuple[float, int]:
        """
        计算布林带位置及得分

        布林带位置：当前价格在布林带中的相对位置
        0% = 下轨，50% = 中轨，100% = 上轨
        """
        closes = bars['close'].values
        if len(closes) < self.bb_period:
            return 50.0, 0

        recent = closes[-self.bb_period:]
        sma = np.mean(recent)
        std = np.std(recent, ddof=1) if len(recent) > 1 else 0

        lower_band = sma - self.bb_std * std
        upper_band = sma + self.bb_std * std
        bandwidth = upper_band - lower_band

        if bandwidth == 0:
            bb_pos = 50.0
        else:
            bb_pos = (current_price - lower_band) / bandwidth * 100.0
        bb_pos = max(0.0, min(100.0, bb_pos))

        # 评分：价格在低位（靠近下轨）得分高
        score = 0
        if bb_pos < self.bb_tight:   # 紧贴下轨（参数化，原10%→现20%）
            score = 2
        elif bb_pos < self.bb_low:   # 在下半部（参数化，原25%→现30%）
            score = 1

        return float(bb_pos), score

    # ─── 成交量 ──────────────────────────────────────────────────────────

    def _calc_volume(self, bars: pd.DataFrame) -> int:
        """计算成交量得分"""
        if len(bars) < 5:
            return 0

        volumes = bars['volume'].values
        closes = bars['close'].values

        avg_vol = np.mean(volumes[-5:])
        if len(volumes) >= 10:
            prev_avg_vol = np.mean(volumes[-10:-5])
        else:
            prev_avg_vol = avg_vol

        current_vol = volumes[-1]
        score = 0

        if len(closes) >= 2:
            # 反弹时放量
            if closes[-1] > closes[-2] and current_vol > avg_vol * self.volume_ratio_threshold:
                score += 2
            # 下跌时缩量
            elif closes[-1] < closes[-2] and current_vol < avg_vol * 0.7:
                score += 1

        # 整体量能健康
        if prev_avg_vol > 0 and avg_vol >= prev_avg_vol * 0.5:
            score += 1

        return min(score, 3)

    # ─── K线形态 ────────────────────────────────────────────────────────

    def _calc_candle(self, bars: pd.DataFrame) -> Tuple[int, str]:
        """
        简单K线形态检测
        - 长下影线：下影线 > 实体 × 2
        - 两连阴后阳线：下跌动能衰竭
        - 连续下跌后出现小阳线（收复部分失地）
        """
        if len(bars) < 2:
            return 0, ""

        score = 0
        details = []
        last = bars.iloc[-1]
        prev = bars.iloc[-2]

        # 长下影线检测
        body = abs(last['close'] - last['open'])
        lower_shadow = min(last['open'], last['close']) - last['low']
        if body > 0 and lower_shadow > body * 2:
            score += 1
            details.append("下影")

        # 两连阴后阳线
        if (prev['close'] < prev['open'] and
            len(bars) >= 3 and
            bars.iloc[-3]['close'] < bars.iloc[-3]['open'] and
            last['close'] > last['open']):
            score += 1
            details.append("双阴反阳")

        # 连续下跌后出现小阳线
        if len(bars) >= 3:
            recent_closes = bars['close'].values[-3:]
            if all(recent_closes[i] <= recent_closes[i-1] for i in range(1, 3)):
                if last['close'] > last['open'] and last['close'] > bars.iloc[-2]['close']:
                    score += 1
                    details.append("跌后企稳")

        detail_str = ",".join(details) if details else "无形态"
        return min(score, 3), detail_str

    # ─── 追涨模式（开盘上涨行情专用）────────────────────────────────────

    def analyze_momentum(self, stock_code: str,
                         bars_1m: Optional[pd.DataFrame],
                         bars_5m: pd.DataFrame,
                         current_price: float,
                         prev_close: Optional[float] = None) -> Dict:
        """
        追涨模式分析：适用于开盘后持续上涨的行情

        Args:
            stock_code: 股票代码
            bars_1m: 1分钟K线
            bars_5m: 5分钟K线
            current_price: 当前价格
            prev_close: 昨日收盘价（已弃用：风控改用窗口内首根K线，保留参数仅兼容）

        评分维度（满分10分）：
        - RSI动量(max 3): RSI > 50 且处于强势区间
        - 布林带突破(max 2): 价格在中轨上方或突破上轨
        - 量价配合(max 3): 价涨量增，无量上涨扣分
        - K线形态(max 3): 连续阳线/创新高/大阳线

        风控：以观察窗口首根K线为基准，涨幅超过 max_rally_pct 则直接拒绝（防止追高接盘）
                 注意：prev_close 仅用于趋势检测(外层TrendDetector)，风控这里用窗口内首根价更合理，
                       避免高开强势股被一刀切拒绝
        """
        bars_5m_count = len(bars_5m) if bars_5m is not None else 0

        # ─── 风控检查：涨幅超限不追（以观察窗口内首根K线为基准）─────────
        if bars_1m is not None and len(bars_1m) >= 5:
            obs_bars = bars_1m.iloc[-min(30, len(bars_1m)):]  # 取最近30根
            first_close = float(obs_bars['close'].iloc[0])
            last_close = float(obs_bars['close'].iloc[-1])
            if first_close > 0:
                rally_pct = (last_close - first_close) / first_close
                if rally_pct > self.bo_max_rally_pct:
                    return {
                        'score': 0, 'signal': 'no_buy',
                        'rsi': None, 'rsi_score': 0, 'rsi_source': None,
                        'bb_position': None, 'bb_score': 0, 'bb_source': None,
                        'volume_score': 0, 'candle_score': 0,
                        'details': f'涨幅{rally_pct:.1%}超限(>{self.bo_max_rally_pct:.0%})',
                        'bars_count': bars_5m_count,
                    }

        if bars_5m_count < self.min_bars_required:
            return {
                'score': 0, 'signal': 'no_buy',
                'rsi': None, 'rsi_score': 0, 'rsi_source': None,
                'bb_position': None, 'bb_score': 0, 'bb_source': None,
                'volume_score': 0, 'candle_score': 0,
                'details': f'K线数据不足({bars_5m_count}根)，跳过分析',
                'bars_count': bars_5m_count,
            }

        bars_5m = bars_5m.copy()

        # 1. RSI动量：用1分钟K线
        if bars_1m is not None and len(bars_1m) >= self.rsi_period + 1:
            rsi_value, rsi_score = self._calc_rsi_momentum(bars_1m)
            rsi_source = '1m'
        else:
            rsi_value, rsi_score = self._calc_rsi_momentum(bars_5m)
            rsi_source = '5m'
        logger.debug(f"{stock_code} [追涨] RSI={rsi_value:.1f}({rsi_source}) score={rsi_score}")

        # 2. 布林带突破：用1分钟K线
        if bars_1m is not None and len(bars_1m) >= self.bb_period:
            bb_position, bb_score = self._calc_bb_breakout(bars_1m, current_price)
            bb_source = '1m'
        else:
            bb_position, bb_score = self._calc_bb_breakout(bars_5m, current_price)
            bb_source = '5m'
        logger.debug(f"{stock_code} [追涨] BB={bb_position:.1f}%({bb_source}) score={bb_score}")

        # 3. 量价配合：用5分钟K线
        volume_score = self._calc_volume_surge(bars_5m)
        logger.debug(f"{stock_code} [追涨] 量价得分={volume_score}")

        # 4. K线形态：用5分钟K线
        candle_score, candle_detail = self._calc_candle_momentum(bars_5m)
        logger.debug(f"{stock_code} [追涨] 形态得分={candle_score} {candle_detail}")

        # 总分
        total = rsi_score + bb_score + volume_score + candle_score
        total = max(0, min(10, total))

        if total >= self.strong_buy_threshold:
            signal = 'strong_buy'
        elif total >= self.watch_threshold:
            signal = 'watch'
        else:
            signal = 'no_buy'

        details = (
            f"RSI={rsi_value:.1f}({rsi_source},+{rsi_score}) "
            f"BB={bb_position:.0f}%({bb_source},+{bb_score}) "
            f"量(+{volume_score}) "
            f"形(+{candle_score}) "
            f"{candle_detail}"
        )

        return {
            'score': total, 'signal': signal,
            'rsi': rsi_value, 'rsi_score': rsi_score, 'rsi_source': rsi_source,
            'bb_position': bb_position, 'bb_score': bb_score, 'bb_source': bb_source,
            'volume_score': volume_score, 'candle_score': candle_score,
            'details': details, 'bars_count': bars_5m_count,
        }

    def _calc_rsi_momentum(self, bars: pd.DataFrame) -> Tuple[float, int]:
        """
        RSI动量评分（追涨用）：RSI在强势区间得分高
        使用配置中的 bo_rsi_strong / bo_rsi_moderate / bo_rsi_weak 阈值
        """
        closes = bars['close'].values
        if len(closes) < self.rsi_period + 1:
            return 50.0, 0

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        alpha = 1.0 / self.rsi_period
        period = self.rsi_period
        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))
        for i in range(period, len(gains)):
            avg_gain = alpha * gains[i] + (1 - alpha) * avg_gain
            avg_loss = alpha * losses[i] + (1 - alpha) * avg_loss

        if avg_loss == 0:
            rsi = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        # 追涨评分：使用配置参数的三级评分（C-1 修复）
        score = 0
        if rsi > self.bo_rsi_strong:       # > 65 强动量
            score = 3
        elif rsi > self.bo_rsi_moderate:   # > 55 中等动量
            score = 2
        elif rsi > self.bo_rsi_weak:       # > 50 偏强
            score = 1

        return float(rsi), score

    def _calc_bb_breakout(self, bars: pd.DataFrame, current_price: float) -> Tuple[float, int]:
        """
        布林带突破评分（追涨用）：价格在中上轨得分高
        与抄底逻辑相反：抄底是触下轨得分，追涨是突破上轨得分
        """
        closes = bars['close'].values
        if len(closes) < self.bb_period:
            return 50.0, 0

        recent = closes[-self.bb_period:]
        sma = np.mean(recent)
        std = np.std(recent, ddof=1) if len(recent) > 1 else 0

        lower_band = sma - self.bb_std * std
        upper_band = sma + self.bb_std * std
        bandwidth = upper_band - lower_band

        if bandwidth == 0:
            bb_pos = 50.0
        else:
            bb_pos = (current_price - lower_band) / bandwidth * 100.0
        bb_pos = max(0.0, min(100.0, bb_pos))

        # 追涨评分：价格在中上轨得分（W-3 修复：使用配置参数）
        score = 0
        if bb_pos > self.bo_bb_breakout_pct:   # 突破上轨区域（默认80%）
            score = 2
        elif bb_pos > self.bo_bb_upper_pct:    # 中轨上方（默认55%）
            score = 1

        return float(bb_pos), score

    def _calc_volume_surge(self, bars: pd.DataFrame) -> int:
        """
        追涨量价评分：上涨放量得分高
        与抄底不同：抄底看重缩量跌+放量反弹，追涨看重放量涨+量能递增
        """
        if len(bars) < 5:
            return 0

        volumes = bars['volume'].values
        closes = bars['close'].values

        avg_vol = np.mean(volumes[-5:])
        if len(volumes) >= 10:
            prev_avg_vol = np.mean(volumes[-10:-5])
        else:
            prev_avg_vol = avg_vol

        current_vol = volumes[-1]
        score = 0

        if len(closes) >= 2:
            # 上涨放量（主力进攻信号）
            if closes[-1] > closes[-2] and current_vol > avg_vol * self.volume_ratio_threshold:
                score += 2
            elif closes[-1] > closes[-2] and current_vol > avg_vol * 1.2:
                score += 1  # 温和放量上涨

        # 连续上涨且量能递增（趋势确认）
        if len(closes) >= 3 and len(volumes) >= 3:
            recent_up = closes[-1] > closes[-2] > closes[-3]
            vol_increasing = volumes[-1] > volumes[-2] > volumes[-3]
            if recent_up and vol_increasing:
                score += 1

        # 整体量能健康
        if prev_avg_vol > 0 and avg_vol >= prev_avg_vol * 0.5:
            score += 1

        return min(score, 3)

    def _calc_candle_momentum(self, bars: pd.DataFrame) -> Tuple[int, str]:
        """
        追涨K线形态评分：连续阳线/创新高/大阳线
        与抄底不同：抄底找下影线/双阴反阳，追涨找连阳/突破/大阳
        """
        if len(bars) < 2:
            return 0, ""

        score = 0
        details = []
        last = bars.iloc[-1]

        # 连续3根阳线（趋势延续）
        if len(bars) >= 3:
            recent_3 = bars.iloc[-3:]
            if all(recent_3.iloc[i]['close'] > recent_3.iloc[i]['open'] for i in range(3)):
                score += 1
                details.append("三连阳")

        # 创近期新高（突破过去20根K线最高价）
        lookback = min(20, len(bars) - 1)
        if lookback > 0:
            prev_high = bars.iloc[-lookback - 1:-1]['high'].max()
            if last['close'] > prev_high:
                score += 1
                details.append("创新高")

        # 大阳线（实体 > 近期平均实体 × 2）
        if len(bars) >= 5:
            bodies = [abs(bars.iloc[i]['close'] - bars.iloc[i]['open']) for i in range(-5, 0)]
            avg_body = np.mean(bodies)
            current_body = abs(last['close'] - last['open'])
            if avg_body > 0 and current_body > avg_body * 2 and last['close'] > last['open']:
                score += 1
                details.append("大阳线")

        detail_str = ",".join(details) if details else "无形态"
        return min(score, 3), detail_str

    # ─── 批量分析 ───────────────────────────────────────────────────────

    def analyze_stocks(self, stock_codes: List[str],
                       kline_provider,
                       price_getter,
                       current_prices: Dict[str, float],
                       use_hybrid: bool = False) -> List[Dict]:
        """
        批量分析多只股票的K线信号

        Args:
            stock_codes: 股票列表
            kline_provider: IntradayKlineProvider 实例
            price_getter: 价格获取器（get_current_price方法）
            current_prices: {stock_code: price} 字典
            use_hybrid: 是否启用混合模式（RSI/布林带用1分钟，量/形态用5分钟）

        Returns:
            分析结果列表，按score降序排列
        """
        results = []

        for code in stock_codes:
            try:
                bars_5m = kline_provider.get_min5_bars(code)
                price = current_prices.get(code)
                if price is None:
                    price = price_getter.get_current_price(code)
                if price is None or price <= 0:
                    continue

                if use_hybrid:
                    bars_1m = kline_provider.get_min1_bars(code)
                    result = self.analyze_hybrid(code, bars_1m, bars_5m, price)
                else:
                    result = self.analyze(code, bars_5m, price)
                result['stock_code'] = code
                results.append(result)

            except Exception as e:
                logger.debug(f"分析 {code} 异常: {e}")
                continue

        results.sort(key=lambda x: x['score'], reverse=True)
        return results

    # ─── 混合模式（RSI/布林带用1分钟，量/形态用5分钟）───────────────────

    def analyze_hybrid(self, stock_code: str,
                      bars_1m: Optional[pd.DataFrame],
                      bars_5m: pd.DataFrame,
                      current_price: float) -> Dict:
        """
        混合模式分析：用1分钟K线计算RSI和布林带（反应更快），
        用5分钟K线计算量和形态（更稳、过滤噪音）

        Returns:
            同 analyze() 的 dict，但多两个字段：
                'rsi_source': '1m' | '5m'
                'bb_source': '1m' | '5m'
        """
        bars_5m_count = len(bars_5m) if bars_5m is not None else 0

        if bars_5m_count < self.min_bars_required:
            return {
                'score': 0,
                'signal': 'no_buy',
                'rsi': None,
                'rsi_score': 0,
                'rsi_source': None,
                'bb_position': None,
                'bb_score': 0,
                'bb_source': None,
                'volume_score': 0,
                'candle_score': 0,
                'details': f'K线数据不足({bars_5m_count}根)，跳过分析',
                'bars_count': bars_5m_count,
            }

        bars_5m = bars_5m.copy()

        # 1. RSI：用1分钟K线（反应更快）
        if bars_1m is not None and len(bars_1m) >= self.rsi_period + 1:
            rsi_value, rsi_score = self._calc_rsi(bars_1m)
            rsi_source = '1m'
        else:
            rsi_value, rsi_score = self._calc_rsi(bars_5m)
            rsi_source = '5m'
        logger.debug(f"{stock_code} [混合] RSI={rsi_value:.1f}({rsi_source}) score={rsi_score}")

        # 2. 布林带：用1分钟K线（反应更快）
        if bars_1m is not None and len(bars_1m) >= self.bb_period:
            bb_position, bb_score = self._calc_bollinger(bars_1m, current_price)
            bb_source = '1m'
        else:
            bb_position, bb_score = self._calc_bollinger(bars_5m, current_price)
            bb_source = '5m'
        logger.debug(f"{stock_code} [混合] BB={bb_position:.1f}%({bb_source}) score={bb_score}")

        # 3. 成交量：用5分钟K线（更稳）
        volume_score = self._calc_volume(bars_5m)
        logger.debug(f"{stock_code} [混合] 成交量得分={volume_score}")

        # 4. K线形态：用5分钟K线（过滤噪音）
        candle_score, candle_detail = self._calc_candle(bars_5m)
        logger.debug(f"{stock_code} [混合] 形态得分={candle_score} {candle_detail}")

        # 总分
        total = rsi_score + bb_score + volume_score + candle_score
        total = max(0, min(10, total))

        if total >= self.strong_buy_threshold:
            signal = 'strong_buy'
        elif total >= self.watch_threshold:
            signal = 'watch'
        else:
            signal = 'no_buy'

        details = (
            f"RSI={rsi_value:.1f}({rsi_source},+{rsi_score}) "
            f"BB={bb_position:.0f}%({bb_source},+{bb_score}) "
            f"量(+{volume_score}) "
            f"形(+{candle_score}) "
            f"{candle_detail}"
        )

        return {
            'score': total,
            'signal': signal,
            'rsi': rsi_value,
            'rsi_score': rsi_score,
            'rsi_source': rsi_source,
            'bb_position': bb_position,
            'bb_score': bb_score,
            'bb_source': bb_source,
            'volume_score': volume_score,
            'candle_score': candle_score,
            'details': details,
            'bars_count': bars_5m_count,
        }
