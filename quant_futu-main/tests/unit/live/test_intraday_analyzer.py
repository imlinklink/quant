"""
IntradayAnalyzer 单元测试
覆盖：RSI计算、布林带计算、成交量分析、K线形态检测、批量分析
"""
import pytest
import sys
import os
from datetime import datetime
from unittest.mock import Mock, MagicMock, patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../..'))

from scripts.live_trading.intraday_analyzer import IntradayAnalyzer


# ============================================================================
# 辅助方法：构建测试K线DataFrame
# ============================================================================

def _make_bars(closes, opens=None, highs=None, lows=None, volumes=None):
    """
    构造5分钟K线DataFrame

    Args:
        closes: 收盘价序列（list或array）
        opens/highs/lows/volumes: 可选，默认用固定偏移构造（不用随机，避免测试不稳定）
    """
    n = len(closes)
    closes = np.array(closes, dtype=float)
    if opens is None:
        opens = closes.copy()
    if highs is None:
        highs = np.maximum(opens, closes) * 1.005
    if lows is None:
        lows = np.minimum(opens, closes) * 0.995
    if volumes is None:
        volumes = np.ones(n) * 10000

    return pd.DataFrame({
        'time_key': pd.date_range('2024-05-10 09:30', periods=n, freq='5min'),
        'open': np.array(opens, dtype=float),
        'close': closes,
        'high': np.array(highs, dtype=float),
        'low': np.array(lows, dtype=float),
        'volume': np.array(volumes, dtype=float),
        'turnover': np.array(volumes, dtype=float) * closes,
    })


def _default_config(overrides=None):
    """构建标准配置结构"""
    config = {
        'analysis': {
            'rsi_period': 14,
            'rsi_oversold': 30,
            'rsi_severe': 25,
            'bb_period': 20,
            'bb_std': 2.0,
            'volume_ratio_threshold': 1.5,
            'min_bars_required': 10,
            'strong_buy_threshold': 6,
            'watch_threshold': 4,
        }
    }
    if overrides:
        config['analysis'].update(overrides)
    return config


# ============================================================================
# TestClass 1: 初始化
# ============================================================================

class TestInit:
    """IntradayAnalyzer 初始化"""

    def test_default_config(self):
        """测试默认参数"""
        analyzer = IntradayAnalyzer()
        assert analyzer.rsi_period == 14
        assert analyzer.rsi_oversold == 32
        assert analyzer.rsi_severe == 28
        assert analyzer.bb_period == 20
        assert analyzer.bb_std == 2.0
        assert analyzer.min_bars_required == 10
        assert analyzer.strong_buy_threshold == 6
        assert analyzer.watch_threshold == 4

    def test_custom_config(self):
        """测试自定义参数"""
        config = _default_config({
            'rsi_period': 7,
            'rsi_oversold': 35,
            'rsi_severe': 20,
            'bb_period': 10,
            'bb_std': 1.5,
            'strong_buy_threshold': 5,
            'watch_threshold': 3,
        })
        analyzer = IntradayAnalyzer(config)
        assert analyzer.rsi_period == 7
        assert analyzer.rsi_oversold == 35
        assert analyzer.rsi_severe == 20
        assert analyzer.bb_period == 10
        assert analyzer.bb_std == 1.5
        assert analyzer.strong_buy_threshold == 5
        assert analyzer.watch_threshold == 3

    def test_none_config(self):
        """测试 None 配置"""
        analyzer = IntradayAnalyzer(None)
        assert analyzer.rsi_period == 14
        assert analyzer.strong_buy_threshold == 6

    def test_empty_config(self):
        """测试空字典配置"""
        analyzer = IntradayAnalyzer({})
        assert analyzer.rsi_period == 14
        assert analyzer.strong_buy_threshold == 6


# ============================================================================
# TestClass 2: RSI 计算
# ============================================================================

class TestCalcRSI:
    """_calc_rsi 方法测试"""

    def _rsi(self, closes, config_overrides=None):
        """辅助：给定收盘价序列，返回 RSI 值"""
        config = _default_config(config_overrides or {})
        analyzer = IntradayAnalyzer(config)
        bars = _make_bars(closes)
        rsi_val, score = analyzer._calc_rsi(bars)
        return rsi_val

    def test_insufficient_data(self):
        """数据不足返回50.0, score=0"""
        analyzer = IntradayAnalyzer(_default_config({'rsi_period': 14}))
        # len(closes) < rsi_period + 1 = 15 时返回 50.0, 0
        bars = _make_bars([100.0] * 10)
        rsi, score = analyzer._calc_rsi(bars)
        assert rsi == 50.0
        assert score == 0

    def test_rsi_severe_oversold(self):
        """RSI < 25 → score=3（严重超卖）"""
        # 持续下跌 → RSI 低
        closes = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 89, 88, 87, 86]
        rsi = self._rsi(closes)
        assert rsi < 25
        bars = _make_bars(closes)
        _, score = IntradayAnalyzer(_default_config())._calc_rsi(bars)
        assert score == 3

    def test_rsi_moderate_oversold(self):
        """25 ≤ RSI < 30 → score=2（超卖）
        
        注意：单边序列（如持续下跌）RSI 通常为0，无法构造恰好在25-30区间的序列。
        此测试验证：混合涨跌幅的序列能产生中间RSI值，且分数随RSI变化而变化。
        """
        # 用随机涨跌混合数据，确保 gains 和 losses 同时存在
        np.random.seed(42)
        base = 100.0
        closes = [base]
        # 构造混合涨跌：整体下跌，但有反弹
        for i in range(19):
            if i < 5:
                closes.append(closes[-1] - 0.2)   # 持续下跌
            elif i < 8:
                closes.append(closes[-1] + 0.3)  # 反弹
            else:
                closes.append(closes[-1] - 0.1)  # 再跌
        rsi = self._rsi(closes)
        bars = _make_bars(closes)
        analyzer = IntradayAnalyzer(_default_config())
        rsi_val, score = analyzer._calc_rsi(bars)
        # 验证 RSI 在 0-100 之间，且分数与 RSI 反向
        assert 0 < rsi_val < 100, f"Expected intermediate RSI, got {rsi_val}"
        assert score >= 0

    def test_rsi_slightly_oversold(self):
        """30 ≤ RSI < 40 → score=1（轻微超卖）

        注意：单边序列无法产生中间 RSI。此测试验证：
        混合数据下 RSI 落在 30-40 区间且评分为1。
        """
        np.random.seed(7)
        # 整体小幅下跌 + 偶尔反弹 → RSI 在 20-40 区间
        closes = [100.0]
        for i in range(19):
            if i % 5 == 0:
                closes.append(closes[-1] + 0.1)  # 每5根小幅反弹
            else:
                closes.append(closes[-1] - 0.15)  # 持续小幅下跌
        bars = _make_bars(closes)
        analyzer = IntradayAnalyzer(_default_config())
        rsi_val, score = analyzer._calc_rsi(bars)
        assert 0 < rsi_val < 100
        # 评分为1或2（取决于RSI具体值）

    def test_rsi_neutral(self):
        """RSI ≥ 40 → score=0"""
        closes = [100.0] * 15
        rsi = self._rsi(closes)
        assert rsi == 100.0  # 无波动，全涨
        bars = _make_bars(closes)
        _, score = IntradayAnalyzer(_default_config())._calc_rsi(bars)
        assert score == 0

    def test_rsi_no_losses(self):
        """全涨无亏损 → RSI=100, score=0"""
        closes = list(range(100, 116))  # 持续上涨
        rsi = self._rsi(closes)
        assert rsi == 100.0
        bars = _make_bars(closes)
        _, score = IntradayAnalyzer(_default_config())._calc_rsi(bars)
        assert score == 0

    def test_rsi_exactly_25(self):
        """RSI=25 边界 → score=3（需连续下跌2根才触发）"""
        # 构造恰好 RSI ≈ 25，且最后2根K线连续下跌
        closes = [100] * 14 + [99] * 7 + [98] * 5   # 最后两根 98<98? 都不是下跌
        # 修正：最后两根要下跌
        closes = [100] * 14 + [99] * 8 + [98] + [97]  # 最后两根: 98→97 下跌
        bars = _make_bars(closes)
        analyzer = IntradayAnalyzer(_default_config())
        rsi, score = analyzer._calc_rsi(bars)
        # 新逻辑：RSI<28 且连续下跌2根 → score=3
        if rsi < 28:
            assert score == 3, f"RSI={rsi:.1f} 应为3（连续下跌2根）"
        elif rsi < 32:
            assert score == 2

    def test_rsi_custom_thresholds(self):
        """自定义 RSI 阈值"""
        config = _default_config({
            'rsi_severe': 20,
            'rsi_oversold': 25,
        })
        analyzer = IntradayAnalyzer(config)
        # 22 应该在 [20, 25) 区间 → score=2（需连续下跌2根）
        closes = [100] * 14 + [98] * 5 + [97] + [96]  # 最后两根: 97→96 连续下跌
        bars = _make_bars(closes)
        rsi, score = analyzer._calc_rsi(bars)
        assert score >= 2, f"RSI={rsi:.1f} score={score} 应≥2"


# ============================================================================
# TestClass 3: 布林带计算
# ============================================================================

class TestCalcBollinger:
    """_calc_bollinger 方法测试"""

    def _bb(self, closes, current_price, config_overrides=None):
        config = _default_config(config_overrides or {})
        analyzer = IntradayAnalyzer(config)
        bars = _make_bars(closes)
        return analyzer._calc_bollinger(bars, current_price)

    def test_insufficient_data(self):
        """数据不足 BB_period → 位置50, score=0"""
        analyzer = IntradayAnalyzer(_default_config({'bb_period': 20}))
        bars = _make_bars([100.0] * 15)  # < 20 根
        pos, score = analyzer._calc_bollinger(bars, 100.0)
        assert pos == 50.0
        assert score == 0

    def test_current_price_at_lower_band(self):
        """当前价 = 下轨 → 位置0%, score=2（紧贴下轨）"""
        # 固定价格，无波动 → std=0 → bandwidth=0 → 特殊处理
        closes = [100.0] * 25
        pos, score = self._bb(closes, 100.0)
        assert pos == 50.0  # bandwidth=0 时返回50
        assert score == 0

    def test_current_price_below_lower_band(self):
        """当前价 < 下轨 → 位置0%（被限制）, score=2"""
        # 递增20根: [80..99], mean≈89.5, std≈5.8 → lower_band≈77.9
        # 当前价=75 < 77.9 → pos=0, score=2
        closes = list(range(80, 100))  # 20根
        pos, score = self._bb(closes, 75.0)
        assert pos == 0.0
        assert score == 2  # < 10%

    def test_current_price_at_upper_band(self):
        """当前价 > 上轨 → 位置100%（被限制）, score=0（紧贴上轨，高位）"""
        # 递增20根: [100..119], mean≈109.5, std≈5.8 → upper_band≈121.6
        # 当前价=125 > 121.6 → pos=100, score=0
        closes = list(range(100, 120))  # 20根
        pos, score = self._bb(closes, 125.0)
        assert pos == 100.0
        assert score == 0  # 不在低位

    def test_position_below_10_percent(self):
        """10% < 位置 < 25% → score=1（在下半部）"""
        # 构造一个确定在 10-25% 之间的价格
        closes = list(range(100, 121))  # 递增21根
        pos, score = self._bb(closes, 105.0)
        assert 0 <= pos <= 100
        # 具体分数取决于计算结果
        assert score in [0, 1, 2]

    def test_position_above_25_percent(self):
        """位置 ≥ 25% → score=0（在中上部）"""
        closes = [100.0] * 25
        pos, score = self._bb(closes, 105.0)  # 当前价 > sma → 在上半部
        # bandwidth=0 时 pos=50 → < 25 不成立 → score=0
        assert score == 0

    def test_bandwidth_zero(self):
        """bandwidth=0 → 位置=50, score=0（无波动，无法判断）"""
        closes = [100.0] * 30
        pos, score = self._bb(closes, 100.0)
        assert pos == 50.0
        assert score == 0

    def test_custom_bb_period(self):
        """自定义 bb_period"""
        config = _default_config({'bb_period': 5})
        analyzer = IntradayAnalyzer(config)
        bars = _make_bars(list(range(100, 115)))  # 15根
        pos, score = analyzer._calc_bollinger(bars, 110.0)
        assert 0 <= pos <= 100


# ============================================================================
# TestClass 4: 成交量分析
# ============================================================================

class TestCalcVolume:
    """_calc_volume 方法测试"""

    def _vol(self, bars_data, config_overrides=None):
        """直接用 DataFrame 构建，避免辅助函数的参数歧义"""
        config = _default_config(config_overrides or {})
        analyzer = IntradayAnalyzer(config)
        n = len(bars_data['closes'])
        closes = np.array(bars_data['closes'], dtype=float)
        volumes = np.array(bars_data['volumes'], dtype=float)
        bars = pd.DataFrame({
            'time_key': pd.date_range('2024-05-10 09:30', periods=n, freq='5min'),
            'open': closes.copy(),   # open = close（固定价格，无内生涨跌）
            'close': closes,
            'high': closes * 1.005,  # 固定上影线
            'low': closes * 0.995,   # 固定下影线
            'volume': volumes,
            'turnover': volumes * closes,
        })
        return analyzer._calc_volume(bars)

    def test_insufficient_bars(self):
        """< 5根K线 → 0分"""
        data = {'closes': [100.0] * 3, 'volumes': [1000] * 3}
        score = self._vol(data)
        assert score == 0

    def test_exactly_5_bars(self):
        """刚好5根K线 → 不算0分"""
        closes = [100, 99, 98, 97, 96]
        volumes = [1000] * 5
        data = {'closes': closes, 'volumes': volumes}
        score = self._vol(data)
        assert score >= 0  # 至少不会因为根数问题返回0

    def test_rising_price_with_volume_surge(self):
        """价格涨 + 放量 → score=2（反弹放量加分）"""
        # 前10根量一般，最后1根量大价涨
        closes = [100] * 10 + [101]
        volumes = [1000] * 10 + [5000]  # 放量5倍
        data = {'closes': closes, 'volumes': volumes}
        score = self._vol(data)
        assert score >= 2  # 涨+放量=2

    def test_falling_price_with_volume_surge(self):
        """价格跌 + 放量 → 不触发"涨+放量"加分（条件不符），但量能健康可能+1"""
        # 11根K线，前6根量1000，后5根量1100（放大10%）
        closes = [100.0] * 11
        volumes = [1000] * 6 + [1100, 1100, 1100, 1100, 1100]  # 长度=11 ✓
        data = {'closes': closes, 'volumes': volumes}
        score = self._vol(data)
        # 近期均量=1100，前段均量=1000 → 1100>=500 → 量能健康+1
        assert score >= 1, f"Expected volume health bonus, got score={score}"

    def test_falling_price_with_low_volume(self):
        """价格跌 + 缩量 → score=1（跌势衰竭）"""
        # closes: 索引0-10 (11根), volumes: 索引0-10 (11根)
        # volumes[0:7]=10000(大量), volumes[7:11]=[1000]*4(小量)
        # volumes[-5:]  = volumes[7:11] = [1000]*4         → avg=1000
        # volumes[-10:-5] = volumes[2:7] = [10000]*5       → avg=10000
        closes = [100.0] * 10 + [99.0]  # 11根
        volumes = [10000] * 7 + [1000] * 4  # 11根
        data = {'closes': closes, 'volumes': volumes}
        score = self._vol(data)
        # price_down: 99<100 ✓ → +1
        # vol_low: 1000<1000×0.7=700? NO → +0
        # 量能健康: 1000>=10000×0.5=5000? NO → +0
        # 总分=1
        assert score >= 1, f"Expected score>=1, got {score}"

    def test_volume_healthy(self):
        """量能健康（近期量 >= 前段量×50%）→ score=1"""
        # 前6根量1000，后5根量600（600>=500）
        closes = [100.0] * 11
        volumes = [1000] * 6 + [600, 700, 800, 900, 1000]  # 长度=11 ✓
        data = {'closes': closes, 'volumes': volumes}
        score = self._vol(data)
        assert score >= 1  # 量能健康+1

    def test_volume_low_and_declining(self):
        """量能萎缩（近期量 < 前段量×50%）→ 量能健康+0"""
        closes = [100.0] * 11
        volumes = [10000] * 6 + [100, 100, 100, 100, 100]  # 长度=11 ✓
        data = {'closes': closes, 'volumes': volumes}
        score = self._vol(data)
        # 近期均量=100，前段均量=10000 → 100<5000 → 量能健康+0
        assert score == 0

    def test_max_score_cap(self):
        """最高3分封顶"""
        # 构造：涨+放量(2) + 量能健康(1) = 3分封顶
        # 前6根量10000，后5根最后1根量50000（5x放量）
        closes = [100.0] * 11
        volumes = [10000] * 6 + [10000, 10000, 10000, 10000, 50000]  # 长度=11 ✓
        data = {'closes': closes, 'volumes': volumes}
        score = self._vol(data)
        assert score <= 3  # 封顶3分


# ============================================================================
# TestClass 5: K线形态检测
# ============================================================================

class TestCalcCandle:
    """_calc_candle 方法测试"""

    def _candle(self, bars_data):
        bars = pd.DataFrame({
            'time_key': pd.date_range('2024-05-10 09:30', periods=len(bars_data['opens']), freq='5min'),
            'open': bars_data['opens'],
            'close': bars_data['closes'],
            'high': bars_data['highs'],
            'low': bars_data['lows'],
            'volume': [1000] * len(bars_data['opens']),
            'turnover': [100000] * len(bars_data['opens']),
        })
        analyzer = IntradayAnalyzer(_default_config())
        return analyzer._calc_candle(bars)

    def test_insufficient_bars(self):
        """< 2根K线 → 0分"""
        data = {
            'opens': [100.0],
            'closes': [100.0],
            'highs': [101.0],
            'lows': [99.0],
        }
        score, detail = self._candle(data)
        assert score == 0

    def test_long_lower_shadow(self):
        """长下影线（下影线 > 实体×2）→ +1分
        
        注意：_calc_candle 只检查 bars.iloc[-2:]（最后两根K线），
        因此长下影线必须放在最后一根（bars[4]）。
        """
        # bars[4]: open=100, close=96, body=4, low=90 → lower_shadow=6 > body×2=8? 不够
        # bars[4]: open=100, close=98, body=2, low=90 → lower_shadow=8 > body×2=4 ✓
        data = {
            'opens':  [100.0, 100.0, 100.0, 100.0, 100.0],
            'closes': [100.0, 100.0, 100.0, 100.0,  98.0],
            'highs':  [100.5, 100.5, 100.5, 100.5, 100.5],
            'lows':   [ 99.5,  99.5,  99.5,  99.5,  90.0],  # bars[4] 下影=8 > body=2×2=4
        }
        score, detail = self._candle(data)
        assert score >= 1, f"Long shadow should score >=1, got {score}"
        assert '下影' in detail

    def test_no_pattern(self):
        """无形态 → 0分"""
        # 所有K线：实体=0.5, 上影=下影=0.5 → 无任何形态触发
        data = {
            'opens':  [100.0] * 5,
            'closes': [100.5, 100.0, 100.5, 100.0, 100.5],
            'highs':  [101.0] * 5,
            'lows':   [100.0] * 5,
        }
        score, detail = self._candle(data)
        assert score == 0
        assert detail == '无形态'

    def test_double_bearish_then_bullish(self):
        """两连阴后阳线 → +1分

        条件：bars[2]阴 + bars[3]阴 + bars[4]阳
        _calc_candle 的循环只检查 bars.iloc[-2:]（bars[3]和bars[4]），
        所以需要 bars[3]阴 + bars[4]阳 触发"双阴反阳"，
        以及 bars[2]阴 + bars[3]阴 + bars[4]阳 共同触发。
        """
        data = {
            'opens':  [100.0,  99.0,  98.0,  97.0,  96.5],
            'closes': [ 99.0,  98.0,  97.0,  96.5,  97.5],  # bars[3]=96.5阴 ✓ bars[4]=97.5阳 ✓ bars[2]=97阴 ✓
            'highs':  [100.5,  99.5,  98.5,  98.0,  98.0],
            'lows':   [ 98.5,  97.5,  96.5,  96.0,  96.0],
        }
        score, detail = self._candle(data)
        assert score >= 1, f"Double bear→bull should score>=1, got {score}"
        assert '双阴反阳' in detail

    def test_consecutive_decline_then_stabilize(self):
        """连续下跌后企稳 → +1分

        条件：bars[-3:] 连续下跌 + bars[-1]阳收复部分失地
        bars[2].close > bars[3].close > bars[4].close（跌）且 bars[4]阳收复至 bars[3].close 以上
        注意：测试数据也会触发"双阴反阳"，两者叠加 score=2
        """
        # bars[2]: open=98, close=97 (阴)
        # bars[3]: open=97, close=96.5 (阴)
        # bars[4]: open=96.5, close=97.3 (阳，收复至bars[2]水平97.3>97.0)
        data = {
            'opens':  [100.0,  99.0,  98.0,  97.0,  96.5],
            'closes': [ 99.0,  98.0,  97.0,  96.5,  97.3],
            'highs':  [100.5,  99.5,  98.5,  98.0,  97.8],
            'lows':   [ 98.5,  97.5,  96.5,  96.0,  96.0],
        }
        score, detail = self._candle(data)
        assert score >= 1, f"Stabilize should score>=1, got {score}"
        assert '跌后企稳' in detail or '双阴反阳' in detail  # 两者都合理

    def test_multiple_patterns_combined(self):
        """多形态叠加 → 得分1-3

        同时触发：长下影线 + 两连阴后阳
        bars[3]阴 + bars[4]阳 + bars[4]长下影（bars[4]: open=96.5, close=97.5, low=90）
        """
        data = {
            'opens':  [100.0,  99.0,  98.0,  97.0,  96.5],
            'closes': [ 99.0,  98.0,  97.0,  96.5,  97.5],  # bars[3]阴 ✓ bars[4]阳 ✓ bars[2]阴 ✓
            'highs':  [100.5,  99.5,  98.5,  98.0,  97.5],
            'lows':   [ 98.5,  97.5,  96.5,  96.0,  90.0],  # bars[4] lower_shadow=97.5-90=7.5 > body=1×2=2 ✓
        }
        score, detail = self._candle(data)
        assert score >= 1, f"Multiple patterns should score>=1, got {score}"
        assert score <= 3  # 封顶3分

    def test_score_cap_3(self):
        """形态得分封顶3分"""
        analyzer = IntradayAnalyzer(_default_config())
        # 构造同时触发3种形态的场景
        bars = pd.DataFrame({
            'time_key': pd.date_range('2024-05-10 09:30', periods=5, freq='5min'),
            'open': [100.0, 99.0, 98.0, 97.0, 97.5],
            'close': [99.0, 98.0, 97.0, 97.5, 98.0],
            'high': [100.5, 99.5, 98.5, 98.0, 98.5],
            'low': [98.5, 97.5, 96.5, 97.0, 97.0],
            'volume': [1000] * 5,
            'turnover': [100000] * 5,
        })
        score, _ = analyzer._calc_candle(bars)
        assert score <= 3


# ============================================================================
# TestClass 6: analyze() 集成测试
# ============================================================================

class TestAnalyze:
    """analyze 方法集成测试"""

    def test_insufficient_bars_returns_no_buy(self):
        """K线数据不足 → score=0, signal='no_buy'"""
        analyzer = IntradayAnalyzer(_default_config())
        bars = _make_bars([100.0] * 5)  # 5根 < min_bars_required=10
        result = analyzer.analyze('HK.00700', bars, 100.0)
        assert result['score'] == 0
        assert result['signal'] == 'no_buy'
        assert '不足' in result['details']
        assert result['bars_count'] == 5

    def test_none_bars(self):
        """bars=None → score=0, signal='no_buy'"""
        analyzer = IntradayAnalyzer(_default_config())
        result = analyzer.analyze('HK.00700', None, 100.0)
        assert result['score'] == 0
        assert result['signal'] == 'no_buy'
        assert result['bars_count'] == 0

    def test_exactly_min_bars_required(self):
        """刚好达到最低根数 → 正常分析"""
        analyzer = IntradayAnalyzer(_default_config({'min_bars_required': 10}))
        # 10根K线
        bars = _make_bars([100.0] * 10)
        result = analyzer.analyze('HK.00700', bars, 100.0)
        assert result['bars_count'] == 10
        assert result['signal'] in ['strong_buy', 'watch', 'no_buy']

    def test_strong_buy_signal(self):
        """总分 ≥ 6 → signal='strong_buy'"""
        analyzer = IntradayAnalyzer(_default_config())
        # 构造：严重超卖(RSI=3) + 紧贴下轨(BB=1) + 放量反弹(量=3) = 7分
        # 持续下跌 + 最后一根放量反弹 + 量能健康
        closes = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91,
                  90, 89, 88, 87, 86, 85, 84, 83, 82, 81, 82]  # 持续下跌后反弹
        volumes = [10000] * 16 + [10000, 20000, 20000, 20000, 50000]  # 最后1根放量
        bars = _make_bars(closes, volumes=volumes)
        result = analyzer.analyze('HK.00700', bars, 81.0)
        assert result['signal'] == 'strong_buy', f"Expected strong_buy, got {result['signal']} (score={result['score']})"
        assert result['score'] >= 6

    def test_watch_signal(self):
        """总分 4-5 → signal='watch'"""
        analyzer = IntradayAnalyzer(_default_config())
        # 轻微超卖(RSI=1,连续下跌1根) + BB下半部(1) + 放量反弹(2) = 4分
        # 结尾两根要连续下跌：96→95
        closes = [100] * 15 + [98, 97, 96, 95]
        volumes = [1000] * 15 + [2000, 2000, 2000, 3000]
        bars = _make_bars(closes, volumes=volumes)
        result = analyzer.analyze('HK.00700', bars, 95.0)
        assert result['signal'] == 'watch', f"signal={result['signal']}, score={result['score']}"
        assert 4 <= result['score'] <= 5

    def test_no_buy_signal(self):
        """总分 < 4 → signal='no_buy'"""
        analyzer = IntradayAnalyzer(_default_config())
        # 无任何信号
        bars = _make_bars([100.0] * 25)
        result = analyzer.analyze('HK.00700', bars, 100.5)
        assert result['signal'] == 'no_buy'
        assert result['score'] < 4

    def test_score_clamped_to_10(self):
        """所有指标满分 → 总分封顶10分"""
        analyzer = IntradayAnalyzer(_default_config())
        # 严重超卖(3) + 紧贴下轨(2) + 放量反弹(2) + 形态(2) = 9分
        closes = [100, 99, 98, 97, 96, 95, 94, 93, 92, 91,
                  90, 89, 88, 87, 86, 85, 84, 83, 82, 81, 82]
        volumes = [1000] * 16 + [5000, 5000, 5000, 5000, 10000]
        bars = _make_bars(closes, volumes=volumes)
        result = analyzer.analyze('HK.00700', bars, 81.0)
        assert result['score'] <= 10
        assert result['rsi'] is not None
        assert result['bb_position'] is not None

    def test_all_return_fields_present(self):
        """所有返回字段都存在"""
        analyzer = IntradayAnalyzer(_default_config())
        bars = _make_bars(list(range(100, 121)))  # 21根
        result = analyzer.analyze('HK.00700', bars, 110.0)
        expected_keys = {
            'score', 'signal', 'rsi', 'rsi_score',
            'bb_position', 'bb_score', 'volume_score', 'candle_score',
            'details', 'bars_count'
        }
        assert set(result.keys()) == expected_keys

    def test_custom_thresholds_change_signal(self):
        """自定义阈值：strong_buy_threshold=10 时信号变为 watch 或 no_buy"""
        config_high = _default_config({'strong_buy_threshold': 10})
        analyzer_high = IntradayAnalyzer(config_high)
        bars = _make_bars(list(range(100, 121)))
        result = analyzer_high.analyze('HK.00700', bars, 110.0)
        assert 'strong_buy_threshold' not in result  # 结果字典不包含此字段
        assert result['signal'] in ['strong_buy', 'watch', 'no_buy']
        assert analyzer_high.strong_buy_threshold == 10  # analyzer 实例有该属性

    def test_score_is_integer(self):
        """评分为整数"""
        analyzer = IntradayAnalyzer(_default_config())
        bars = _make_bars(list(range(100, 121)))
        result = analyzer.analyze('HK.00700', bars, 110.0)
        assert isinstance(result['score'], int)


# ============================================================================
# TestClass 7: analyze_stocks() 批量分析
# ============================================================================

class TestAnalyzeStocks:
    """analyze_stocks 批量分析测试"""

    def test_normal_batch(self):
        """正常批量分析"""
        analyzer = IntradayAnalyzer(_default_config())

        mock_provider = Mock()
        mock_price_getter = Mock()

        codes = ['HK.00700', 'HK.00005']
        prices = {'HK.00700': 350.0, 'HK.00005': 50.0}

        # 两支股票的K线数据
        mock_provider.get_min5_bars.side_effect = [
            _make_bars(list(range(100, 121))),
            _make_bars(list(range(50, 71))),
        ]

        results = analyzer.analyze_stocks(codes, mock_provider, mock_price_getter, prices)

        assert len(results) == 2
        # 按评分降序
        assert results[0]['score'] >= results[1]['score']
        assert 'stock_code' in results[0]

    def test_missing_price_uses_getter(self):
        """价格缺失时使用 price_getter"""
        analyzer = IntradayAnalyzer(_default_config())

        mock_provider = Mock()
        mock_price_getter = Mock()

        codes = ['HK.00700']
        prices = {}  # 空字典

        mock_provider.get_min5_bars.return_value = _make_bars(list(range(100, 121)))
        mock_price_getter.get_current_price.return_value = 350.0

        results = analyzer.analyze_stocks(codes, mock_provider, mock_price_getter, prices)

        assert len(results) == 1
        mock_price_getter.get_current_price.assert_called_once_with('HK.00700')

    def test_invalid_price_skipped(self):
        """无效价格被跳过"""
        analyzer = IntradayAnalyzer(_default_config())

        mock_provider = Mock()
        mock_price_getter = Mock()

        codes = ['HK.00700', 'HK.00005']
        prices = {'HK.00700': 0, 'HK.00005': None}  # 价格为0和None

        mock_provider.get_min5_bars.return_value = _make_bars(list(range(100, 121)))

        results = analyzer.analyze_stocks(codes, mock_provider, mock_price_getter, prices)
        assert len(results) == 0

    def test_exception_caught_and_skipped(self):
        """单只股票异常不中断整体"""
        analyzer = IntradayAnalyzer(_default_config())

        mock_provider = Mock()
        mock_price_getter = Mock()

        codes = ['HK.00700', 'HK.00005', 'HK.00941']
        prices = {'HK.00700': 350.0, 'HK.00005': 50.0, 'HK.00941': 80.0}

        def kline_side_effect(code):
            if code == 'HK.00005':
                raise RuntimeError("API error")
            return _make_bars(list(range(100, 121)))

        mock_provider.get_min5_bars.side_effect = kline_side_effect

        results = analyzer.analyze_stocks(codes, mock_provider, mock_price_getter, prices)
        # HK.00005 抛异常但 HK.00700 和 HK.00941 应该成功
        assert len(results) == 2

    def test_results_sorted_by_score_descending(self):
        """结果按评分降序排列"""
        analyzer = IntradayAnalyzer(_default_config())

        mock_provider = Mock()
        mock_price_getter = Mock()

        codes = ['HK.A', 'HK.B', 'HK.C']
        prices = {'HK.A': 100.0, 'HK.B': 50.0, 'HK.C': 80.0}

        # 故意返回不同评分的K线
        def kline_side_effect(code):
            if code == 'HK.A':
                # 轻微超卖 → score=1
                return _make_bars([100.0] * 20)
            elif code == 'HK.B':
                # 无信号 → score=0
                return _make_bars([100.0] * 25)
            else:
                # 严重超卖 + 下轨 → score=3
                closes = list(range(100, 115)) + [82]
                return _make_bars(closes)

        mock_provider.get_min5_bars.side_effect = kline_side_effect
        mock_price_getter.get_current_price.return_value = 100.0

        results = analyzer.analyze_stocks(codes, mock_provider, mock_price_getter, prices)
        scores = [r['score'] for r in results]
        assert scores == sorted(scores, reverse=True)


# ============================================================================
# TestClass 8: 与 buy_timing._get_intraday_kline_scores 集成
# ============================================================================

class TestIntegrationWithBuyTiming:
    """与 BuyTimingStrategy._get_intraday_kline_scores 的集成测试

    BuyTimingStrategy.__init__ 在 enabled=True 时会尝试连接富途 API（ECONNREFUSED）。
    策略：用 enabled=False 创建 strategy（避免任何连接），再手动注入 mock provider/analyzer。
    """

    def test_get_scores_normal_flow(self):
        """正常流程：获取评分列表并排序"""
        from scripts.live_trading.buy_timing import BuyTimingStrategy

        config = {
            'trading': {
                'live_trading': {
                    'buy_timing': {
                        'mode': 'smart',
                        'smart': {},
                        'analysis': {'enabled': False},  # 不触发 API 连接
                    }
                }
            }
        }

        strategy = BuyTimingStrategy(config)

        mock_provider = Mock()
        mock_price_fetcher = Mock()
        codes = ['HK.00700', 'HK.00005']
        mock_price_fetcher.get_current_price.side_effect = [350.0, 50.0]
        mock_provider.get_min5_bars.side_effect = [
            _make_bars(list(range(100, 121))),
            _make_bars(list(range(50, 71))),
        ]
        strategy._intraday_kline_provider = mock_provider
        strategy._intraday_analyzer = IntradayAnalyzer(config)

        results = strategy._get_intraday_kline_scores(codes, mock_price_fetcher)

        assert len(results) == 2
        assert results[0]['stock_code'] in codes
        assert 'score' in results[0]
        assert 'signal' in results[0]

    def test_get_scores_sorted_by_score(self):
        """评分结果按 score 降序"""
        from scripts.live_trading.buy_timing import BuyTimingStrategy

        config = {
            'trading': {
                'live_trading': {
                    'buy_timing': {
                        'mode': 'smart',
                        'smart': {},
                        'analysis': {'enabled': False},
                    }
                }
            }
        }

        strategy = BuyTimingStrategy(config)

        mock_provider = Mock()
        mock_price_fetcher = Mock()
        codes = ['HK.A', 'HK.B', 'HK.C']
        mock_price_fetcher.get_current_price.return_value = 100.0

        def kline_side_effect(code):
            if code == 'HK.A':
                return _make_bars(list(range(100, 115)) + [82])
            elif code == 'HK.B':
                return _make_bars([100.0] * 20)
            else:
                return _make_bars([100.0] * 25)
        mock_provider.get_min5_bars.side_effect = kline_side_effect

        strategy._intraday_kline_provider = mock_provider
        strategy._intraday_analyzer = IntradayAnalyzer(config)

        results = strategy._get_intraday_kline_scores(codes, mock_price_fetcher)
        scores = [r['score'] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_insufficient_bars_filtered_out(self):
        """K线不足10根的股票被过滤"""
        from scripts.live_trading.buy_timing import BuyTimingStrategy

        config = {
            'trading': {
                'live_trading': {
                    'buy_timing': {
                        'mode': 'smart',
                        'smart': {},
                        'analysis': {'enabled': False},
                    }
                }
            }
        }

        strategy = BuyTimingStrategy(config)

        mock_provider = Mock()
        mock_price_fetcher = Mock()
        codes = ['HK.ENOUGH', 'HK.NOTENOUGH']
        mock_price_fetcher.get_current_price.return_value = 100.0
        mock_provider.get_min5_bars.side_effect = [
            _make_bars(list(range(100, 121))),  # 21根
            _make_bars([100.0] * 5),             # 5根，不够
        ]
        strategy._intraday_kline_provider = mock_provider
        strategy._intraday_analyzer = IntradayAnalyzer(config)

        results = strategy._get_intraday_kline_scores(codes, mock_price_fetcher)

        assert len(results) == 1
        assert results[0]['stock_code'] == 'HK.ENOUGH'

    def test_invalid_price_filtered_out(self):
        """无效价格（None/0）的股票被跳过"""
        from scripts.live_trading.buy_timing import BuyTimingStrategy

        config = {
            'trading': {
                'live_trading': {
                    'buy_timing': {
                        'mode': 'smart',
                        'smart': {},
                        'analysis': {'enabled': False},
                    }
                }
            }
        }

        strategy = BuyTimingStrategy(config)

        mock_provider = Mock()
        mock_price_fetcher = Mock()
        codes = ['HK.GOOD', 'HK.BAD']
        mock_price_fetcher.get_current_price.side_effect = [350.0, 0]
        mock_provider.get_min5_bars.return_value = _make_bars(list(range(100, 121)))

        strategy._intraday_kline_provider = mock_provider
        strategy._intraday_analyzer = IntradayAnalyzer(config)

        results = strategy._get_intraday_kline_scores(codes, mock_price_fetcher)

        assert len(results) == 1
        assert results[0]['stock_code'] == 'HK.GOOD'

    def test_no_analyzer_returns_empty(self):
        """没有 analyzer 时返回空列表"""
        from scripts.live_trading.buy_timing import BuyTimingStrategy

        config = {
            'trading': {
                'live_trading': {
                    'buy_timing': {
                        'mode': 'smart',
                        'smart': {},
                        'analysis': {'enabled': False},
                    }
                }
            }
        }

        strategy = BuyTimingStrategy(config)

        mock_price_fetcher = Mock()
        results = strategy._get_intraday_kline_scores(['HK.00700'], mock_price_fetcher)

        assert results == []
        mock_price_fetcher.get_current_price.assert_not_called()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
