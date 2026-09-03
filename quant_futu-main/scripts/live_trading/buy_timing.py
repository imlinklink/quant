"""
买入时机策略模块 - 负责买入时机判断
已优化：
1. 移除无用大盘指数判断
2. RLock + 锁超时，防死锁
3. 所有网络IO移出锁 + 超时控制
4. 锁粒度最小化，早盘不卡顿
"""
import logging
import threading
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


class BuyTimingStrategy:
    """
    买入时机策略
    1. simple: 固定时间买入
    2. smart: 智能K线模式
    """

    def __init__(self, config: Dict):
        self.config = config
        self.buy_timing_config = config.get('trading', {}).get('live_trading', {}).get('buy_timing', {})

        self.mode = self.buy_timing_config.get('mode', 'smart')
        self._init_config()

        # ====================== 核心优化 ======================
        self._status_lock = threading.RLock()
        self._lock_timeout = 5.0      # 锁超时 5 秒
        self._network_timeout = 10.0   # 网络超时 10 秒

        self.open_status: Dict[str, any] = {
            'checked': False,
            'open_price': {},
            'prev_close': {},
            'price_history': {},
            'volume_history': [],
            'stabilize_count': 0,
            'v_shape_detected': {},
            'intraday_low': {},
            'intraday_high': {},
        }

        self.volatility_cache: Dict[str, float] = {}
        self.volatility_map: Dict[str, float] = {}

        # K线
        self._intraday_kline_provider = None
        self._intraday_analyzer = None
        self._use_hybrid = False
        self._init_kline_analyzer()

        # 行情判断（追涨/抄底分支）—— 使用 TrendDetector
        self._trend_detector = None
        self._regime_cache: Dict[str, str] = {}  # 每只股票的行情类型，每日重置

        self._first_check_after_restart = True

    def _init_config(self):
        if self.mode == 'simple':
            simple_config = self.buy_timing_config.get('simple', {})
            buy_time_str = simple_config.get('buy_time', '13:30')
            self.buy_hour, self.buy_minute = map(int, buy_time_str.split(':'))
            logger.info(f"买入策略: 简单模式, 固定时间 {buy_time_str}")
        else:
            smart_config = self.buy_timing_config.get('smart', {})

            self.strong_buy_only_windows: List[Dict] = []
            raw_windows = smart_config.get('strong_buy_only_windows', [])
            for w in raw_windows:
                parts = w.split('-')
                if len(parts) == 2:
                    self.strong_buy_only_windows.append({
                        'start': self._parse_time(parts[0]),
                        'end': self._parse_time(parts[1]),
                    })

            self.kline_enable_time = self._parse_time(smart_config.get('kline_enable_time', '11:00'))
            self.strong_buy_threshold = smart_config.get('strong_buy_threshold', 8)
            self.normal_buy_threshold = smart_config.get('normal_buy_threshold', 6)
            logger.info(f"买入策略: 纯K线评分模式")

    def _init_kline_analyzer(self):
        kline_cfg = self.buy_timing_config.get('analysis', {})
        if not kline_cfg.get('enabled', False):
            logger.info("日内K线分析未启用")
            return

        try:
            from .intraday_kline import IntradayKlineProvider
            from .intraday_analyzer import IntradayAnalyzer

            trading_cfg = self.config.get('trading', {})
            host = trading_cfg.get('host', '127.0.0.1')
            port = trading_cfg.get('port', 11111)

            self._intraday_kline_provider = IntradayKlineProvider(
                host=host, port=port, cache_ttl=kline_cfg.get('cache_ttl', 25)
            )
            if not self._intraday_kline_provider.connect():
                logger.warning("日内K线Provider连接失败")
                self._intraday_kline_provider = None
                return

            self._intraday_analyzer = IntradayAnalyzer(self.buy_timing_config)
            self._use_hybrid = kline_cfg.get('use_hybrid', True)

            # 初始化行情趋势检测器
            from .intraday_analyzer import TrendDetector
            self._trend_detector = TrendDetector(self.buy_timing_config)

            logger.info(f"日内K线分析器初始化成功 (含趋势检测)")
        except Exception as e:
            logger.warning(f"日内K线分析器初始化失败: {e}")
            self._intraday_kline_provider = None
            self._intraday_analyzer = None

    @staticmethod
    def _parse_time(time_str: str) -> dt_time:
        hour, minute = map(int, time_str.split(':'))
        return dt_time(hour, minute)

    # ====================== 带超时的安全重置 ======================
    def reset_daily_state(self):
        acquired = False
        try:
            acquired = self._status_lock.acquire(timeout=self._lock_timeout)
            if not acquired:
                logger.error("重置状态：锁超时")
                return

            self.open_status = {
                'checked': False,
                'open_price': {},
                'prev_close': {},
                'price_history': {},
                'volume_history': [],
                'stabilize_count': 0,
                'v_shape_detected': {},
                'intraday_low': {},
                'intraday_high': {},
            }
            self.volatility_cache = {}
        finally:
            if acquired:
                self._status_lock.release()

        if self._intraday_kline_provider:
            self._intraday_kline_provider.clear_cache()

        self._regime_cache = {}  # 每日重置行情判断
        self._first_check_after_restart = True
        logger.info("买入策略每日状态已重置")

    # ====================== 主入口：无锁IO + 安全锁 ======================
    def should_buy_now(self, selected_stocks: List[str], price_fetcher, _data_fetcher=None) -> Tuple[bool, str, str]:
        _ = _data_fetcher
        current_time = datetime.now().time()

        if self.mode == 'simple':
            buy_start = dt_time(self.buy_hour, self.buy_minute)
            buy_end = dt_time(self.buy_hour, self.buy_minute, 59)
            if buy_start <= current_time <= buy_end:
                return True, "简单模式-定时买入", "simple"
            return False, "未到买入时间", "none"

        in_strong_window = self._is_in_strong_buy_window(current_time)
        threshold = self.strong_buy_threshold if in_strong_window else self.normal_buy_threshold
        threshold_label = "强买" if in_strong_window else "正常"

        force_refresh = self._first_check_after_restart
        if force_refresh:
            logger.info("[买入检查] 服务重启首次检查，强制刷新K线")

        candidate_pool_size = self.config.get('momentum', {}).get('candidate_pool_size', 5)
        for stock_code in selected_stocks[:candidate_pool_size]:
            # ========== 网络IO：完全在锁外面 ==========
            try:
                price = price_fetcher.get_current_price(stock_code)
            except Exception as e:
                logger.error(f"[{stock_code}] 获取价格失败: {e}")
                price = None

            if price is None:
                continue

            score, details = self._get_kline_score(stock_code, price, force_refresh=force_refresh,
                                                     price_fetcher=price_fetcher)
            logger.info(f"[{threshold_label}检查] {stock_code}: {score}分 (阈值{threshold}) | {details}")

            if score >= threshold:
                self._first_check_after_restart = False
                entry_mode = self._detect_entry_mode(stock_code, price_fetcher=price_fetcher)
                return True, f"K线买入{score}分 - {details}", entry_mode

        self._first_check_after_restart = False
        return False, f"无买入信号(需>={threshold}分)", "none"

    # ====================== 行情判断（追涨/抄底分支）======================
    def _determine_regime(self, stock_code: str, bars_1m: Optional[pd.DataFrame] = None,
                          price_fetcher=None) -> str:
        """
        判断当前行情类型：uptrend(上涨) / sideways(震荡) / downtrend(下跌)

        使用 TrendDetector 多维度判断（收益率以昨收为基准，正确识别高开/低开），
        每只股票每日只判断一次并缓存结果。
        上涨行情 → 追涨打分系统(analyze_momentum)
        震荡/下跌 → 抄底打分系统(analyze_hybrid)

        Args:
            stock_code: 股票代码
            bars_1m: 已获取的1分钟K线（可选，避免重复IO）
            price_fetcher: 价格获取器（可选，用于获取昨日收盘价）
        """
        if stock_code in self._regime_cache:
            return self._regime_cache[stock_code]

        # 未启用趋势检测或检测器未初始化，默认走抄底
        if not self._trend_detector or not self._trend_detector.enabled:
            return 'sideways'

        # W-2修复：优先使用传入的bars_1m，避免重复网络请求
        if bars_1m is None:
            if not self._intraday_kline_provider:
                return 'sideways'
            try:
                bars_1m = self._intraday_kline_provider.get_min1_bars(stock_code)
            except Exception as e:
                logger.warning(f"[{stock_code}] 行情判断获取1分钟K线失败: {e}")
                return 'sideways'

        # 获取昨日收盘价（用于趋势检测的收益率基准）
        prev_close = None
        if price_fetcher is not None:
            try:
                prev_close = price_fetcher.get_previous_close(stock_code)
            except Exception:
                pass

        regime, details = self._trend_detector.detect(bars_1m, prev_close=prev_close)
        self._regime_cache[stock_code] = regime

        logger.info(
            f"[{stock_code}] 趋势检测结果: {regime} | "
            f"涨幅={details.get('return_pct', '?')}% | "
            f"阳线比={details.get('bull_ratio', '?')}% | "
            f"价位={details.get('price_position', '?')}% | "
            f"↑{details.get('score_up', 0)}/↓{details.get('score_down', 0)}"
        )
        return regime

    # ====================== 入仓模式检测 ======================
    def _detect_entry_mode(self, stock_code: str, price_fetcher=None) -> str:
        """
        检测本次入仓的交易模式：momentum(追涨) / bottom_fish(抄底) / simple(简单)
        用于追涨止损保护判断
        """
        try:
            bars_1m = self._intraday_kline_provider.get_min1_bars(stock_code) if self._intraday_kline_provider else None
        except Exception:
            bars_1m = None
        regime = self._determine_regime(stock_code, bars_1m=bars_1m, price_fetcher=price_fetcher)
        if regime == 'uptrend':
            return 'momentum'
        return 'bottom_fish'

    # ====================== K线评分：追涨/抄底分支 ======================
    def _get_kline_score(self, stock_code: str, price: float, force_refresh: bool = False,
                         price_fetcher=None) -> Tuple[int, str]:
        if not self._intraday_kline_provider or not self._intraday_analyzer:
            return 0, "K线分析器未就绪"

        try:
            bars = self._intraday_kline_provider.get_min5_bars(stock_code, force_refresh=force_refresh)
        except Exception as e:
            logger.error(f"[{stock_code}] 5分钟K线获取失败: {e}")
            bars = None

        if bars is None or len(bars) < 10:
            return 0, "K线数据不足"

        try:
            bars_1m = self._intraday_kline_provider.get_min1_bars(stock_code, force_refresh=force_refresh)
        except Exception as e:
            logger.error(f"[{stock_code}] 1分钟K线获取失败: {e}")
            bars_1m = None

        # 行情判断 → 选择追涨或抄底评分（W-2修复：传入已获取的bars_1m避免重复IO）
        regime = self._determine_regime(stock_code, bars_1m=bars_1m,
                                        price_fetcher=price_fetcher)

        if regime == 'uptrend' and self._use_hybrid:
            # 追涨模式
            if bars_1m is None or len(bars_1m) < 5:
                return 0, "1分钟K线数据不足(追涨)"
            result = self._intraday_analyzer.analyze_momentum(stock_code, bars_1m, bars, price)
            regime_label = f"{regime}-追涨"
        elif self._use_hybrid:
            # 抄底模式（震荡/下跌行情）
            if bars_1m is None or len(bars_1m) < 5:
                return 0, "1分钟K线数据不足"
            result = self._intraday_analyzer.analyze_hybrid(stock_code, bars_1m, bars, price)
            regime_label = f"{regime}-抄底"
        else:
            result = self._intraday_analyzer.analyze(stock_code, bars, price)
            regime_label = "标准"

        score = result.get('score', 0)
        details = result.get('details', '')
        return score, f"[{regime_label}]{details}"

    def _is_in_strong_buy_window(self, current_time: dt_time) -> bool:
        for window in self.strong_buy_only_windows:
            start_t, end_t = window['start'], window['end']
            if start_t <= end_t:
                if start_t <= current_time < end_t:
                    return True
            else:
                if current_time >= start_t or current_time < end_t:
                    return True
        return False

    # ====================== 下面是保留的兼容接口 ======================
    def _check_intraday_kline(self, stock_code: str, price: float) -> Tuple[bool, str]:
        if not self._intraday_kline_provider or not self._intraday_analyzer:
            return False, "K线分析器未就绪"

        current_time = datetime.now().time()
        if current_time < self.kline_enable_time:
            return False, f"K线分析未启用"

        try:
            bars = self._intraday_kline_provider.get_min5_bars(stock_code, timeout=self._network_timeout)
        except Exception as e:
            return False, "K线获取失败"

        if bars is None or len(bars) < 10:
            return False, "K线数据不足"

        if self._use_hybrid:
            try:
                bars_1m = self._intraday_kline_provider.get_min1_bars(stock_code, timeout=self._network_timeout)
            except Exception:
                return False, "1分钟K线异常"
            result = self._intraday_analyzer.analyze_hybrid(stock_code, bars_1m, bars, price)
        else:
            result = self._intraday_analyzer.analyze(stock_code, bars, price)

        if result['signal'] == 'strong_buy':
            return True, f"K线强买({result['score']}分)"
        return False, f"信号不足({result['score']}分)"

    def _get_intraday_kline_scores(self, stock_codes: List[str], price_fetcher) -> List[Dict]:
        if not self._intraday_analyzer or not self._intraday_kline_provider:
            return []

        current_time = datetime.now().time()
        if current_time < self.kline_enable_time:
            return []

        results = []
        for code in stock_codes[:5]:
            try:
                price = price_fetcher.get_current_price(code)
            except Exception:
                continue
            if not price or price <= 0:
                continue

            try:
                bars = self._intraday_kline_provider.get_min5_bars(code)
            except Exception:
                continue
            if bars is None or len(bars) < 10:
                continue

            if self._use_hybrid:
                try:
                    bars_1m = self._intraday_kline_provider.get_min1_bars(code)
                except Exception:
                    continue
                r = self._intraday_analyzer.analyze_hybrid(code, bars_1m, bars, price)
            else:
                r = self._intraday_analyzer.analyze(code, bars, price)

            r['stock_code'] = code
            results.append(r)

        results.sort(key=lambda x: x['score'], reverse=True)
        return results