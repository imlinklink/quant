"""
抄底买入监控器

功能：
1. 订阅一组股票（watch list）
2. 每分钟检查K线
3. 出现抄底信号（RSI超卖+布林带下轨+成交量）自动买入
4. 买入后转入 ChandelierExitManager 的止盈止损管理

用法：
    python scripts/live_trading/dip_buy_monitor.py --codes US.MU,US.AAPL,US.TSLA --dry-run
"""
import sys
import os
import time
import logging
import argparse
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, Set, Tuple
import yaml
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE_DIR)

logger = logging.getLogger(__name__)


def _us_ledger(event_type: str, **fields):
    """写入美股评估账本（写失败不影响交易）。"""
    try:
        from scripts.live_trading.decision_ledger import ledger
        ledger.record(event_type, market_type='US', **fields)
    except Exception:
        pass


def _parse_earnings_date(date_str) -> Optional[datetime]:
    """解析 Yahoo earningsDate 的 fmt 字符串，失败返回 None。"""
    if not date_str:
        return None
    text = str(date_str).strip()
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M:%S',
                '%b %d, %Y', '%B %d, %Y', '%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


class DipBuyMonitor:
    """抄底买入监控器"""
    
    def __init__(
        self,
        watch_codes: List[str],
        config: Dict,
        dry_run: bool = True,
        approval_store=None,
    ):
        """
        Args:
            watch_codes: 监控股票列表 ['US.MU', 'US.AAPL', ...]
            config: 配置字典（来自 config.yaml）
            dry_run: 模拟模式（不实际下单）
            approval_store: 人工确认提案存储（由 run_all.py 注入，与 Flask 共用）
        """
        self.watch_codes = watch_codes
        self.config = config
        self.dry_run = dry_run
        
        # 抄底参数（美股只一档买入阈值）
        dip_cfg = config.get('dip_buy', {})
        self.check_interval = dip_cfg.get('check_interval', 60)  # 检查间隔（秒）
        self.buy_threshold = dip_cfg.get('buy_threshold', dip_cfg.get('strong_buy_threshold', 8))
        self.min_bars = dip_cfg.get('min_bars', 30)  # 最少K线根数
        self.max_positions = dip_cfg.get('max_positions', 3)
        
        # 仓位配置
        self.position_size_usd = dip_cfg.get('position_size_usd', 4000)  # 单只仓位（美元）
        
        # 冷却期（避免同一只股票频繁买入）
        self.cooldown_minutes = dip_cfg.get('cooldown_minutes', 30)
        self.last_buy_time: Dict[str, datetime] = {}

        # ===== P0 抄底加强：规则层保护（均可由 config.yaml 关闭） =====
        self.one_position_per_code = bool(dip_cfg.get('one_position_per_code', True))

        sess = dip_cfg.get('session_filter', {})
        self.session_filter_enabled = bool(sess.get('enabled', True))
        self.session_allow = {
            'pre_market': bool(sess.get('allow_pre_market', False)),
            'regular': bool(sess.get('allow_regular', True)),
            'after_hours': bool(sess.get('allow_after_hours', True)),
            'overnight': bool(sess.get('allow_overnight', False)),
        }

        tg = dip_cfg.get('daily_trend_gate', {})
        self.daily_trend_gate_enabled = bool(tg.get('enabled', True))
        self.daily_ma = int(tg.get('daily_ma', 20))
        self.trend_block_below_ma = bool(tg.get('block_below_ma', True))
        self.trend_require_ma_slope_up = bool(tg.get('require_ma_slope_up', False))

        eg = dip_cfg.get('earnings_gate', {})
        self.earnings_gate_enabled = bool(eg.get('enabled', True))
        self.earnings_skip_days = int(eg.get('skip_days_before', 2))

        # ===== P1 抄底加强：信号质量闸（均可由 config.yaml 关闭） =====
        rg = dip_cfg.get('reversal_gate', {})
        self.reversal_gate_enabled = bool(rg.get('enabled', True))
        rrg = dip_cfg.get('rr_gate', {})
        self.rr_gate_enabled = bool(rrg.get('enabled', True))
        self.rr_min = float(rrg.get('min_rr', 1.5))
        htf = dip_cfg.get('higher_tf_filter', {})
        self.higher_tf_enabled = bool(htf.get('enabled', True))
        self.higher_tf_cache_seconds = int(htf.get('cache_seconds', 300))

        sh = dip_cfg.get('shadow_signals', {})
        self.shadow_signals_enabled = bool(sh.get('enabled', True))

        # ===== P2 评估闭环：扫描流水（组件分/闸门/outcome） =====
        ev = dip_cfg.get('evaluation', {})
        self.eval_log_min_score = int(ev.get('log_min_score', 4))

        # ===== P3 大盘/指数门（板块代理；默认影子验证）=====
        igc = dip_cfg.get('index_gate') or {}
        if not isinstance(igc, dict):
            igc = {}
        self.index_gate_enabled = bool(igc.get('enabled', False))
        self.index_gate_enforce = bool(igc.get('enforce', False))
        self.index_gate = None
        
        # 状态
        self._running = False
        self._stop_event = threading.Event()
        self._positions_cache: Optional[Set[str]] = None
        self._positions_cache_ts: float = 0.0
        self._sim_held_codes: Set[str] = set()
        self._trend_gate_cache: Dict[str, Dict] = {}
        self._env_cache: Dict[str, Dict] = {}
        self._session_block_label: Optional[str] = None
        self._session_block_date: Optional[str] = None

        # ===== 人工确认（Human-in-the-loop）=====
        # 启用后：抄底信号只推送到确认页，用户点「下单」才真正执行
        approval_cfg = config.get('trading', {}).get('live_trading', {}).get('human_approval', {})
        self.approval_cfg = approval_cfg
        self.approval_enabled = True
        self.approval_store = approval_store
        self.approval_max_drift = float(approval_cfg.get('max_price_drift_pct', 0.03))
        self._approval_rejected_codes: Set[str] = set()
        self._approval_reject_date: Optional[str] = None
        self._approval_processed_reject_ids: Set[str] = set()

        # LLM 判定（仅用于页面展示，最终由人工点单决定）
        self.llm_advisor = None
        self.llm_enabled = False
        self.llm_mode_label = 'shadow'

        if self.approval_enabled:
            if self.approval_store is None:
                # 独立运行没有 Flask 页面：自建存储，提案只记录不执行（安全）
                from scripts.live_trading.approval.proposal_store import ProposalStore
                self.approval_store = ProposalStore(
                    ttl_seconds=float(approval_cfg.get('proposal_ttl_seconds', 180))
                )
                logger.warning(
                    "[人工确认] 未检测到 Web 确认页注入，提案只记录不执行；"
                    "请通过 run_all.py 启动（页面 http://127.0.0.1:8890/approvals）"
                )
            self._setup_llm()
        
        # 数据源
        self.pool = None
        self.analyzer = None

    def _env_label(self) -> str:
        """页面/提案上展示的环境标签"""
        if self.dry_run:
            return 'DRY-RUN'
        return str(self.config.get('live_manager', {}).get('trd_env', 'SIMULATE'))

    def _setup_llm(self):
        """初始化 LLM 顾问（可选，页面展示判定结果用）"""
        llm_cfg = self.config.get('llm', {})
        if not llm_cfg.get('enabled', False):
            return
        self.llm_mode_label = 'shadow' if llm_cfg.get('shadow_mode', True) else 'real_veto'
        try:
            from mutifactor.llm import LLMAdvisor
            self.llm_advisor = LLMAdvisor(llm_cfg)
            self.llm_enabled = bool(getattr(self.llm_advisor, 'enabled', False))
            if not self.llm_enabled:
                logger.warning("[LLM] 已配置但不可用（API key 缺失/未展开），页面将显示无判定")
        except Exception as e:
            logger.warning(f"[LLM] 初始化失败，页面显示无判定: {e}")
            self.llm_advisor = None
            self.llm_enabled = False

    def _ask_llm_verdict(self, code: str, price: float,
                         context_text: str = '') -> Optional[Dict]:
        """获取大模型对本笔买入的市场风险判定（失败返回 None，不阻塞）"""
        if self.llm_advisor is None or not self.llm_enabled:
            return None
        try:
            et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))
            t = et_now.hour * 60 + et_now.minute
            if t >= 20 * 60 or t < 4 * 60:
                session = 'overnight(夜盘)'
            elif t < 9 * 60 + 30:
                session = 'pre_market(盘前)'
            elif t < 16 * 60:
                session = 'regular(盘中)'
            else:
                session = 'after_hours(盘后)'

            result = self.llm_advisor.veto_buy(
                buy_list=[code],
                holdings=[],
                cash=float(self.position_size_usd),
                market_context=(
                    f'US 个股 {code}，当前时段 {session}，信号价 ${price:.2f}\n'
                    f'{context_text}'
                ),
            )
            if not result:
                return {
                    'verdict': None,
                    'confidence': None,
                    'reason': '本轮 LLM 未返回结果（调用失败），纯规则信号',
                    'mode': self.llm_mode_label,
                }
            return {
                'model': getattr(self.llm_advisor, 'model', ''),
                'mode': self.llm_mode_label,
                'verdict': result.get('verdict', 'allow'),
                'risk_level': result.get('risk_level', 'LOW'),
                'confidence': result.get('confidence'),
                'reason': result.get('reason', ''),
            }
        except Exception as e:
            logger.warning(f"[LLM] 判定调用异常: {e}")
            return {
                'verdict': None,
                'confidence': None,
                'reason': f'LLM 调用异常: {e}',
                'mode': self.llm_mode_label,
            }

    def _effective_position_size_usd(self) -> float:
        """
        应用盘前市场状态简报的建议单票仓位比例。

        美股没有组合资金模型，这里按"配置 5000 对应基准比例 0.2"缩放，
        缩放范围限制在 0.5x ~ 1.0x，即防御时减半、绝不超过配置基准。
        """
        try:
            from scripts.live_trading import market_brief as mb
            brief = mb.load_brief()
            ratio = brief.get('suggested_position_ratio')
            if not ratio:
                return float(self.position_size_usd)
            ratio = float(ratio)
            scale = max(0.5, min(1.0, ratio / 0.2))
            if abs(scale - 1.0) > 1e-9:
                key = (brief.get('date'), 'us_size')
                if getattr(self, '_brief_size_logged', None) != key:
                    self._brief_size_logged = key
                    logger.warning(
                        f"[市场状态] 建议仓位比例 {ratio:.2f} -> "
                        f"单票规模 {self.position_size_usd:.0f} x {scale:.2f} = "
                        f"{self.position_size_usd * scale:.0f} USD"
                    )
            return float(self.position_size_usd) * scale
        except Exception:
            return float(self.position_size_usd)

    def _queue_approval(self, code: str, price: float, result: Dict,
                        signal_ctx: Optional[Dict] = None) -> bool:
        """把抄底信号推送到确认页（不自动下单）"""
        if self.approval_store is None:
            return False

        # 每日重置拒绝记录
        today = datetime.now().strftime('%Y-%m-%d')
        if self._approval_reject_date != today:
            self._approval_rejected_codes.clear()
            self._approval_reject_date = today

        if code in self._approval_rejected_codes:
            logger.info(f"[人工确认] {code} 今日已被拒绝，跳过")
            return False
        if self.approval_store.has_active_for_code(code):
            logger.info(f"[人工确认] {code} 已有待确认提案，跳过")
            return False

        score = int(result.get('score', 0))
        size_usd = self._effective_position_size_usd()
        qty = int(size_usd / price) if price > 0 else 0
        if qty <= 0:
            logger.warning(f"[人工确认] {code} 数量计算为0，无法推送")
            return False

        details = str(result.get('details', '')).strip()
        reason = f"抄底信号触发: 评分 {score} ≥ 阈值 {self.buy_threshold}（{result.get('signal', '-')}）"
        if details:
            reason += f"；{details}"
        env_text = str(result.get('higher_tf_detail') or '').strip()
        if env_text:
            reason += f"；60m环境: {env_text}"
        rr_val = result.get('rr')
        if rr_val is not None:
            reason += f"；盈亏比≈{float(rr_val):.2f}"
        fq = result.get('flow_quality') or {}
        if fq.get('label') and fq['label'] != 'no_data':
            net_txt = f"（净{float(fq['main_net']):+,.0f}）" if fq.get('main_net') is not None else ''
            reason += f"；资金流: {fq['label']}{net_txt}"
        op = result.get('option_pressure') or {}
        if op.get('label') and op['label'] != 'no_data':
            iv_txt = f"，IV≈{float(op['avg_iv']) * 100:.0f}%" if op.get('avg_iv') is not None else ''
            pc_txt = f"，P/C OI={float(op['put_call_oi']):.2f}" if op.get('put_call_oi') is not None else ''
            reason += f"；期权: {op['label']}{iv_txt}{pc_txt}"
        reason += f"；单票仓位 ${size_usd:.0f}，信号价约 {qty} 股"
        ig = result.get('index_gate') or {}
        if ig.get('action'):
            mode_txt = '影子' if not self.index_gate_enforce else '生效'
            reason += f"；指数门[{mode_txt}]: {ig.get('reason', ig.get('action'))}"
            if result.get('index_shadow_block'):
                reason += "（影子规则：本单应拦截或需更高分，仅供参考）"

        # 信息层：打包消息面上下文（新闻 + 下次财报），失败降级为空；
        # 财报闸已拉过 signal_ctx 时直接复用，避免同一信号重复请求 Yahoo
        context_text = ''
        try:
            from scripts.live_trading import signal_context
            if signal_ctx is not None:
                context_text = signal_context.format_context(code, signal_ctx)
            else:
                context_text = signal_context.get_context_text(code)
        except Exception as e:
            logger.debug(f"消息面上下文失败 {code}: {e}")

        self.approval_store.create(
            stock_code=code,
            stock_name=code,
            market_type='US',
            env=self._env_label(),
            price=price,
            quantity=qty,
            estimated_cost=round(price * qty, 2),
            per_stock_capital=float(size_usd),
            entry_mode='dip_buy',
            trade_plan={'initial_stop': result.get('stop_ref'), 'target': result.get('target_price'),
                        'min_rr': self.rr_min, 'time_exit_bars': int(self.config.get('dip_buy', {}).get('time_exit_bars', 8)),
                        'signal_time': datetime.now(ZoneInfo('America/New_York')).isoformat()},
            trigger_reason='抄底评分 ≥ 阈值',
            kline_score=score,
            kline_signal=result.get('signal'),
            reason=reason,
            context=context_text,
            llm=self._ask_llm_verdict(code, price, context_text=context_text),
        )
        logger.warning(
            f"[人工确认] {code} 推送待确认: 评分={score}/{self.buy_threshold} "
            f"@{price:.2f} 约{qty}股 —— 请在确认页点「下单」"
        )
        return True

    def _execute_approved(self, item: Dict):
        from scripts.live_trading.execution import execute_approved_buy
        execute_approved_buy(self, item)

    def _process_approvals(self):
        """处理确认页点击结果（每个监控循环调用一次）"""
        if self.approval_store is None:
            return

        today = datetime.now().strftime('%Y-%m-%d')
        if self._approval_reject_date != today:
            self._approval_rejected_codes.clear()
            self._approval_reject_date = today

        from scripts.live_trading.execution import service_for
        service_for(self).reconcile()
        self.approval_store.expire_old()

        # 只处理抄底线（entry_mode=dip_buy）的点击结果；
        # 突破线（donchian）提案由 TrendBreakoutMonitor 自己处理，互不误伤。
        for item in self.approval_store.rejected_items():
            if item.get('side', 'buy') != 'buy':
                continue
            if item.get('entry_mode', 'dip_buy') != 'dip_buy':
                continue
            if item.get('id') in self._approval_processed_reject_ids:
                continue
            self._approval_processed_reject_ids.add(item.get('id', ''))
            self._approval_rejected_codes.add(item.get('stock_code', ''))
            # 同股票其它提案一并取消（仅限抄底线，不动突破线提案）
            for other in self.approval_store.get_all():
                if (
                    other.get('stock_code') == item.get('stock_code')
                    and other.get('side', 'buy') == 'buy'
                    and other.get('entry_mode', 'dip_buy') == 'dip_buy'
                    and other['status'] in ('pending', 'approved')
                ):
                    self.approval_store.mark(other['id'], 'expired', note='同一股票已被拒绝，取消')

        # 用户确认 → 执行
        for item in self.approval_store.approved_items():
            if item.get('side', 'buy') != 'buy':
                continue
            if item.get('entry_mode', 'dip_buy') != 'dip_buy':
                continue
            self._execute_approved(item)
        
    def _setup(self):
        """初始化连接和分析器"""
        # 直接用 chandelier_exit_manager.py 里已验证的连接池
        from scripts.live_trading.chandelier_exit_manager import FutuConnectionPool
        
        from scripts.live_trading.intraday_analyzer import IntradayAnalyzer
        
        # 连接池
        futu_cfg = self.config.get('futu', {})
        self.pool = FutuConnectionPool(
            host=futu_cfg.get('host', '127.0.0.1'),
            port=futu_cfg.get('port', 11111),
            quote_pool_size=2,
            trade_pool_size=1,
            market='US'
        )
        
        # 分析器（canonical 评分读取 dip_buy 段的 buy_threshold；
        # 修复此前误传 buy_timing 段导致阈值落到默认 13 的错配）
        dip_cfg = self.config.get('dip_buy', {})
        self.analyzer = IntradayAnalyzer({'dip_buy': dip_cfg})

        try:
            from scripts.live_trading.index_gate import DipIndexGate
            self.index_gate = DipIndexGate(dip_cfg, pool=self.pool)
        except Exception as e:
            logger.warning(f"[指数门] 初始化失败（不影响抄底）: {e}")
            self.index_gate = None
        
        logger.info(f"✅ 初始化完成: 监控 {len(self.watch_codes)} 只股票")
        
    def _get_kline_5m(self, code: str) -> Optional[pd.DataFrame]:
        """拉取已收盘15分钟K线（含盘前盘后夜盘）
        
        注意：Futu 的 time_key 是美东时间（ET），需要用美东时间筛选今日数据
        """
        from futu import KLType, RET_OK, Session
        
        # 使用美东时间
        et_now = datetime.now(ZoneInfo('America/New_York'))
        
        # 用美东时间计算日期范围
        et_date = et_now.strftime('%Y-%m-%d')
        et_start = et_now - timedelta(days=7)
        
        with self.pool.get_quote_ctx() as ctx:
            # request_history_kline 支持 extended_time + Session.ALL 获取全时段数据
            ret, data, _ = ctx.request_history_kline(
                code,
                start=et_start.strftime('%Y-%m-%d'),
                end=et_now.strftime('%Y-%m-%d'),
                ktype=KLType.K_15M,
                extended_time=True,
                session=Session.ALL  # 获取全时段（盘前+盘中+盘后+夜盘）
            )
            
            if ret == RET_OK and data is not None and len(data) > 0:
                # 使用滚动窗口：最多取 60 根（真底背离需要 41+ 根），
                # 不足 min_bars 时视为数据不够（夜盘刚开时也兼容）
                from scripts.live_trading.strategy_rules import completed_bars
                data = completed_bars(data, et_now, 15).tail(max(self.min_bars, 60))
                if len(data) >= self.min_bars:
                    logger.info(f"  📊 {code} 最近{len(data)}根K线（含盘前盘后夜盘）")
                    return data.tail(max(self.min_bars, 60))
                else:
                    logger.info(f"  📊 {code} K线不足({len(data)}根，需≥{self.min_bars}根)")
                    return None
            else:
                logger.warning(f"获取K线失败 {code}: {data if ret != RET_OK else '无数据'}")
        
        return None
    
    def _get_current_price(self, code: str) -> Optional[float]:
        """获取当前价格
        
        根据美东时间判断市场时段，返回对应价格：
        - 夜盘 (20:00-04:00 ET): overnight_price
        - 盘前 (04:00-09:30 ET): pre_price
        - 盘中 (09:30-16:00 ET): last_price
        - 盘后 (16:00-20:00 ET): after_price
        """
        from futu import SubType, RET_OK
        
        # 计算美东时间（自动处理夏令时/冬令时）
        bj_now = datetime.now()
        et_now = bj_now.astimezone(ZoneInfo("America/New_York"))
        et_time = et_now.hour * 60 + et_now.minute
        
        # 判断时段
        PRE_MARKET_START = 4 * 60    # 04:00 ET
        PRE_MARKET_END = 9 * 60 + 30 # 09:30 ET
        REGULAR_END = 16 * 60        # 16:00 ET
        AFTER_HOURS_END = 20 * 60    # 20:00 ET
        
        with self.pool.get_quote_ctx() as ctx:
            # 订阅
            ret, err = ctx.subscribe([code], [SubType.QUOTE], subscribe_push=False)
            if ret != RET_OK:
                logger.warning(f"订阅失败 {code}: {err}")
                return None
            
            # 拉快照
            ret, snapshot = ctx.get_market_snapshot([code])
            if ret == RET_OK and not snapshot.empty:
                r = snapshot.iloc[0]
                
                # 根据时段选择价格（优先级：当前时段专属字段 > 兜底）
                if et_time < PRE_MARKET_START or et_time >= AFTER_HOURS_END:
                    # 夜盘时段 (20:00-04:00 ET)
                    price = r.get('overnight_price', 0)
                elif PRE_MARKET_START <= et_time < PRE_MARKET_END:
                    # 盘前时段
                    price = r.get('pre_price', 0)
                elif PRE_MARKET_END <= et_time < REGULAR_END:
                    # 盘中时段
                    price = r.get('last_price', 0)
                else:
                    # 盘后时段 (16:00-20:00 ET)
                    price = r.get('after_price', 0)
                
                if price and price > 0:
                    return float(price)
        return None
    
    def _check_cooldown(self, code: str) -> bool:
        """检查是否在冷却期内"""
        if code not in self.last_buy_time:
            return True
        elapsed = (datetime.now() - self.last_buy_time[code]).total_seconds() / 60
        return elapsed >= self.cooldown_minutes

    # ==================== P0 抄底加强：时段 / 日线 / 财报 / 持仓闸 ====================

    def _session_label(self, et_now) -> str:
        t = et_now.hour * 60 + et_now.minute
        PRE_MARKET_START, PRE_MARKET_END = 4 * 60, 9 * 60 + 30
        REGULAR_END, AFTER_HOURS_END = 16 * 60, 20 * 60
        if t < PRE_MARKET_START or t >= AFTER_HOURS_END:
            label = 'overnight'
        elif t < PRE_MARKET_END:
            label = 'pre_market'
        elif t < REGULAR_END:
            label = 'regular'
        else:
            label = 'after_hours'
        return label

    def _session_allowed(self, et_now) -> bool:
        """时段白名单：默认只允许盘中+盘后（config dip_buy.session_filter）。"""
        if not self.session_filter_enabled:
            return True
        label = self._session_label(et_now)
        allowed = self.session_allow.get(label, True)
        if not allowed:
            today = et_now.strftime('%Y-%m-%d')
            if self._session_block_label != label or self._session_block_date != today:
                self._session_block_label = label
                self._session_block_date = today
                logger.info(f"⏭️  时段过滤: 当前为 {label}（美东），已关闭抄底")
        return allowed

    def _fetch_daily_tail(self, code: str) -> Optional[pd.DataFrame]:
        """拉日K（前复权，约400天），计算日线MA供趋势门使用。"""
        from futu import KLType, RET_OK
        et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))
        start = (et_now - timedelta(days=400)).strftime('%Y-%m-%d')
        end = et_now.strftime('%Y-%m-%d')
        try:
            with self.pool.get_quote_ctx() as ctx:
                ret, data, _ = ctx.request_history_kline(
                    code=code, start=start, end=end,
                    ktype=KLType.K_DAY, autype='qfq',
                )
            if ret != RET_OK or data is None or len(data) == 0:
                return None
            df = pd.DataFrame({
                'date': pd.to_datetime(data['time_key']).dt.normalize(),
                'close': data['close'].astype(float),
            })
            df = df.sort_values('date').drop_duplicates(subset=['date']).reset_index(drop=True)
            ma_col = f'ma{self.daily_ma}'
            df[ma_col] = df['close'].rolling(self.daily_ma).mean()
            df['ma_slope_ref'] = df[ma_col].shift(10)
            return df
        except Exception as e:
            logger.warning(f"日线门数据获取失败 {code}: {e}")
            return None

    def _daily_trend_gate(self, code: str) -> Tuple[bool, Optional[str]]:
        """
        P0-1 日线趋势门：最近一根已收盘日K 收盘低于日线MA 时禁止抄底。
        结果按（代码 × 美东日期）缓存，每个交易日只拉一次日K。
        返回 (allowed, reason)。
        """
        if not self.daily_trend_gate_enabled:
            return True, None
        et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))
        et_date = et_now.strftime('%Y-%m-%d')
        cached = self._trend_gate_cache.get(code)
        if cached and cached.get('date') == et_date:
            return bool(cached.get('allowed')), cached.get('reason')

        # 数据拿不到时保守放行（只靠其他闸门），并避免每轮重试
        allowed, reason = True, None
        df = self._fetch_daily_tail(code)
        if df is not None and len(df) >= max(self.daily_ma + 30, 60):
            completed = df[df['date'] < pd.Timestamp(et_now.date())]
            if len(completed) >= 30:
                row = completed.iloc[-1]
                ma_col = f'ma{self.daily_ma}'
                ma_val = row.get(ma_col)
                close = float(row['close'])
                if ma_val is not None and pd.notna(ma_val):
                    ma_val = float(ma_val)
                    sig_date = row['date'].date()
                    if self.trend_block_below_ma and close < ma_val:
                        allowed = False
                        reason = (f"日线门: {sig_date} 收盘 ${close:.2f} < "
                                  f"MA{self.daily_ma} ${ma_val:.2f}")
                    elif self.trend_require_ma_slope_up:
                        slope_ref = row.get('ma_slope_ref')
                        if slope_ref is not None and pd.notna(slope_ref) and \
                                float(ma_val) <= float(slope_ref):
                            allowed = False
                            reason = (f"日线门: MA{self.daily_ma} ${ma_val:.2f} 走平/下行 "
                                      f"(10根前 ${float(slope_ref):.2f})")
                        elif not self.trend_block_below_ma and close < ma_val:
                            reason = (f"⚠️ 日线门(仅提示): {sig_date} 收盘低于 "
                                      f"MA{self.daily_ma}（block_below_ma=false）")
                    elif not self.trend_block_below_ma and close < ma_val:
                        reason = (f"⚠️ 日线门(仅提示): {sig_date} 收盘低于 "
                                  f"MA{self.daily_ma}（block_below_ma=false）")
        elif df is not None:
            reason = f"日线数据不足({len(df)}根，需≥{max(self.daily_ma + 30, 60)})，放行"
        else:
            reason = "日线数据获取失败，放行（依赖其余闸门）"
        self._trend_gate_cache[code] = {
            'date': et_date, 'allowed': allowed, 'reason': reason,
        }
        return allowed, reason

    def _get_60m_env(self, code: str) -> Tuple[int, str]:
        """
        P1-2 60分钟环境（缓存 cache_seconds 秒，只在评分达标后调用）：
        返回 (env_score, 展示文本)。-2=60m强下行（禁止），-1/0/1 仅提示。
        """
        from futu import KLType, RET_OK
        from mutifactor.utils.intraday_scoring import score_higher_tf_env
        now = time.time()
        cache = self._env_cache.get(code)
        if cache and now - cache.get('ts', 0) < self.higher_tf_cache_seconds:
            return int(cache.get('env', 0)), str(cache.get('detail', '60m环境中性'))
        et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))
        start = (et_now - timedelta(days=14)).strftime('%Y-%m-%d')
        end = et_now.strftime('%Y-%m-%d')
        env_score, text = 0, '60m环境中性'
        try:
            with self.pool.get_quote_ctx() as ctx:
                ret, data, _ = ctx.request_history_kline(
                    code=code, start=start, end=end,
                    ktype=KLType.K_60M, autype='qfq', extended_time=True,
                )
            if ret == RET_OK and data is not None and len(data) > 0:
                df = pd.DataFrame({
                    'date': pd.to_datetime(data['time_key']),
                    'open': data['open'].astype(float),
                    'high': data['high'].astype(float),
                    'low': data['low'].astype(float),
                    'close': data['close'].astype(float),
                    'volume': data['volume'].astype(float),
                }).sort_values('date').reset_index(drop=True)
                env_score, detail = score_higher_tf_env(df)
                text = str(detail.get('detail', text))
            else:
                text = '60m数据获取失败'
        except Exception as e:
            logger.warning(f"60m环境获取失败 {code}: {e}")
            text = '60m数据获取失败'
        self._env_cache[code] = {'ts': time.time(), 'env': env_score, 'detail': text}
        return env_score, text

    def _earnings_blackout(self, earnings) -> Tuple[bool, Optional[str]]:
        """
        P0-2 财报窗口闸：下次财报在 skip_days 个自然日内 → 禁止抄底提案。
        财报日期解析失败/缺失时视为未知，放行（依赖其他闸门）。
        """
        if not self.earnings_gate_enabled or not earnings:
            return False, None
        dt = _parse_earnings_date(earnings.get('date'))
        if dt is None:
            return False, None
        et_today = datetime.now().astimezone(ZoneInfo("America/New_York")).date()
        delta = (dt.date() - et_today).days
        if 0 <= delta <= self.earnings_skip_days:
            return True, (f"财报窗口: 下次财报 {dt.date()}（还有 {delta} 天），"
                          f"前 {self.earnings_skip_days} 天不抄底")
        return False, None

    # ==================== 资金流/期权影子字段（不改分、不拦截） ====================

    def _apply_shadow_fields(self, result: Dict, signal_ctx: Optional[Dict]):
        """
        从 signal_context 已拉取的数据里提取两个结构化影子字段：
          result['flow_quality']   资金流：主力(超大+大单)净流入/流出方向
          result['option_pressure']期权：近月ATM 平均IV + put/call OI 压力
        仅用于确认页展示与 dip_scans 归因，不参与评分与拦截。
        """
        ctx = signal_ctx or {}
        cap = ctx.get('capital') or {}
        opt = ctx.get('options') or {}

        # ---- 资金流 ----
        try:
            super_v = cap.get('super')
            big_v = cap.get('big')
            total_v = cap.get('in_flow')
            super_f = float(super_v) if super_v is not None else None
            big_f = float(big_v) if big_v is not None else None
            total_f = float(total_v) if total_v is not None else None
            main = None
            if super_f is not None or big_f is not None:
                main = (super_f or 0.0) + (big_f or 0.0)
        except (TypeError, ValueError):
            main = None
            total_f = None
        if main is None:
            flow = {'label': 'no_data', 'main_net': None}
        else:
            share = None
            if total_f not in (None, 0):
                share = main / abs(total_f)
            if main > 0:
                label = '主力净流入' if share is None or share < 0.3 else '主力强净流入'
            elif main < 0:
                label = '主力净流出' if share is None or share > -0.3 else '主力强净流出'
            else:
                label = '主力中性'
            flow = {'label': label, 'main_net': round(main, 0)}
        result['flow_quality'] = flow

        # ---- 期权近月 ATM 抽样 ----
        legs = opt.get('legs') or []
        ivs = []
        call_oi = 0.0
        put_oi = 0.0
        for leg in legs:
            iv = leg.get('iv')
            if isinstance(iv, (int, float)) and iv > 0:
                ivs.append(float(iv))
            oi = leg.get('open_interest')
            oi_f = float(oi) if isinstance(oi, (int, float)) else 0.0
            if leg.get('kind') == 'CALL':
                call_oi += oi_f
            elif leg.get('kind') == 'PUT':
                put_oi += oi_f
        avg_iv = float(sum(ivs) / len(ivs)) if ivs else None
        put_call = (put_oi / call_oi) if call_oi and call_oi > 0 else None
        if avg_iv is None:
            label = 'no_data'
        elif avg_iv >= 0.80:
            label = 'IV极端'
        elif avg_iv >= 0.50:
            label = 'IV偏高'
        else:
            label = 'IV正常'
        if put_call is not None and put_call >= 1.5:
            label += '+PUT压力' if label != 'no_data' else 'PUT压力'
        result['option_pressure'] = {
            'label': label,
            'avg_iv': round(avg_iv, 4) if avg_iv is not None else None,
            'put_call_oi': round(put_call, 2) if put_call is not None else None,
            'expiry': opt.get('expiry'),
        }

    def _refresh_position_cache(self, force: bool = False):
        """每20秒只查一次券商持仓（整表），供 总数/单代码 检查共用。"""
        if self.dry_run:
            return
        now = time.time()
        if not force and self._positions_cache is not None and now - self._positions_cache_ts < 20:
            return
        from futu import RET_OK, TrdEnv
        try:
            trd_env_str = self.config.get('live_manager', {}).get('trd_env', 'SIMULATE')
            trd_env = TrdEnv.SIMULATE if trd_env_str == 'SIMULATE' else TrdEnv.REAL
            with self.pool.get_trade_ctx() as ctx:
                ret, data = ctx.position_list_query(trd_env=trd_env)
            if ret != RET_OK:
                logger.error(f"持仓查询失败，错误码: {ret}")
                return
            held = set()
            if data is not None and len(data) > 0:
                held = {str(r['code']) for r in data.to_dict('records') if float(r.get('qty') or 0) != 0}
            self._positions_cache = held
            self._positions_cache_ts = time.time()
        except Exception as e:
            logger.warning(f"持仓查询异常: {e}")
            self._positions_cache_ts = time.time()  # 避免每30s重试打爆

    def _has_position(self, code: str) -> bool:
        """是否已持有该代码（dry-run 用共享模拟持仓登记簿）。"""
        if self.dry_run:
            try:
                from scripts.live_trading.position_registry import REGISTRY
                return REGISTRY.get(code) is not None
            except Exception:
                return code in self._sim_held_codes
        self._refresh_position_cache()
        return bool(self._positions_cache) and code in self._positions_cache
    
    def _execute_buy(self, code, price, score=0, proposal_id=None):
        # 所有成交统一经过审批执行器，禁用旧的直接买入入口。
        if not proposal_id or not self.approval_store:
            return False
        from scripts.live_trading.execution import service_for
        item = self.approval_store.get(proposal_id)
        return service_for(self).submit(item, price, self._effective_position_size_usd()) == 'filled'

    def _get_position_count(self) -> int:
        """持仓总数：dry-run 统计共享模拟登记簿；实盘用缓存的券商持仓。"""
        if self.dry_run:
            try:
                from scripts.live_trading.position_registry import REGISTRY
                return REGISTRY.count()
            except Exception:
                return len(self._sim_held_codes)
        self._refresh_position_cache()
        return len(self._positions_cache or set())

    def _log_scan(self, code: str, price: float, et_now, result: Dict,
                  outcome: str = '', env_score=None):
        """
        P2 评估闭环：把一次“评分检查”记入 dip_scans.jsonl。
        只记评分 ≥ eval_log_min_score 的检查（低于的忽略，控制文件量）；
        outcome 记录最终去向：below_threshold / blocked_* / passed。
        """
        score = float(result.get('score') or 0)
        if self.eval_log_min_score > 0 and score < self.eval_log_min_score:
            return None
        try:
            from scripts.live_trading.decision_ledger import scan_ledger
            trend_cache = self._trend_gate_cache.get(code, {})
            fq = result.get('flow_quality') or {}
            op = result.get('option_pressure') or {}
            ig = result.get('index_gate') or {}
            return scan_ledger.record_scan(
                market_type='US',
                env=self._env_label(),
                stock_code=code,
                et_time=et_now.isoformat(),
                session=self._session_label(et_now),
                price=price,
                bars_count=result.get('bars_count'),
                score=score,
                raw_score=result.get('raw_score'),
                signal=result.get('signal'),
                buy_threshold=self.buy_threshold,
                rsi_score=result.get('rsi_score'),
                bb_score=result.get('bb_score'),
                bb_position=result.get('bb_position'),
                volume_score=result.get('volume_score'),
                divergence_score=result.get('volume_divergence_score'),
                drawdown_score=result.get('drawdown_score'),
                atr_pct=result.get('atr_pct'),
                dd_atr=result.get('dd_atr'),
                trend_adj=result.get('trend_adj'),
                trend_name=(result.get('trend') or {}).get('trend'),
                reversal_ok=bool((result.get('reversal') or {}).get('ok')),
                rr=result.get('rr'),
                htf_env_score=env_score,
                flow_label=fq.get('label'),
                flow_main_net=fq.get('main_net'),
                option_label=op.get('label'),
                avg_iv=op.get('avg_iv'),
                put_call_oi=op.get('put_call_oi'),
                option_expiry=op.get('expiry'),
                daily_gate_reason=trend_cache.get('reason'),
                index_proxy=ig.get('proxy'),
                index_cluster=ig.get('cluster'),
                index_action=ig.get('action') or '',
                index_below_ma20=ig.get('below_ma20'),
                index_drop_pct=ig.get('drop_pct'),
                index_ma20=ig.get('ma20'),
                index_shadow_block=bool(result.get('index_shadow_block')),
                outcome=outcome,
            )
        except Exception:
            return None

    def _check_one(self, code: str):
        """检查单只股票"""
        try:
            # 盘前市场状态闸门：LLM 说 avoid 时今天不产生新买入信号
            try:
                from scripts.live_trading import market_brief as mb
                brief = mb.load_brief()
                # 只允许“今天的简报”触发 avoid 闸门：过期简报不得静默禁用当天买入
                today_str = datetime.now().strftime('%Y-%m-%d')
                if brief.get('date') == today_str and not mb.buy_allowed(brief):
                    key = brief.get('date') or today_str
                    if getattr(self, '_brief_avoid_date', None) != key:
                        self._brief_avoid_date = key
                        logger.warning(
                            f"[市场状态] 今日档位={brief.get('risk_level')} "
                            f"buy_frequency=avoid，暂停产生买入信号: {brief.get('risk_note', '')}"
                        )
                    return
            except Exception:
                pass

            # 计算美东时间（用于日志显示时段）
            et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))

            # 0. 时段过滤：默认禁夜盘/盘前，只在盘中+盘后抄底
            if not self._session_allowed(et_now):
                return

            # 0.5 P0-1 日线趋势门：最近已收盘日K 低于日线MA → 禁抄（防接飞刀）
            trend_ok, trend_reason = self._daily_trend_gate(code)
            if not trend_ok:
                logger.info(f"⏭️  {code} {trend_reason}，跳过抄底")
                return
            if trend_reason and self._trend_gate_cache.get(code, {}).get('logged') != trend_reason:
                self._trend_gate_cache[code]['logged'] = trend_reason
                logger.warning(f"  {code} {trend_reason}")

            # 1. 检查冷却期
            if not self._check_cooldown(code):
                logger.info(f"⏭️  {code} 在冷却期内，跳过")
                return

            # 1.5 检查最大持仓数（避免无限加仓，整表缓存每20秒刷新一次）
            pos_count = self._get_position_count()
            if pos_count >= self.max_positions:
                logger.info(f"⏭️  持仓已满({pos_count}/{self.max_positions})，跳过买入")
                return

            # 1.6 P0 单代码一仓：已持有该代码时不再重复抄底
            if self.one_position_per_code and self._has_position(code):
                logger.info(f"⏭️  {code} 已持有（单代码一仓），跳过重复抄底")
                return

            # 2. 拉K线
            bars = self._get_kline_5m(code)
            if bars is None or len(bars) < self.min_bars:
                logger.info(f"📊 {code} K线不足({len(bars) if bars is not None else 0}根，需≥{self.min_bars}根)")
                return
            
            # 3. 获取当前价
            price = self._get_current_price(code)
            if price is None:
                logger.info(f"💰 {code} 无法获取当前价")
                return
            
            logger.info(f"✅ {code} 数据正常: {len(bars)}根K线 当前价=${price:.2f} (ET {et_now.strftime('%H:%M')})")
            
            # 4. 分析
            result = self.analyzer.analyze(code, bars, price)
            
            # 5. 判断是否买入（美股只一档阈值）
            if result['score'] >= self.buy_threshold:
                env_score = None
                # 5.1 P1-1 反转确认硬门：超卖分够了还不够，要看到止跌/反弹迹象
                if self.reversal_gate_enabled:
                    rev = result.get('reversal') or {}
                    if not rev.get('ok'):
                        self._log_scan(code, price, et_now, result,
                                       outcome='blocked_reversal')
                        logger.info(
                            f"⏭️  {code} 评分达标但无反转确认"
                            f"（{rev.get('detail', '无反转确认')}），继续观察"
                        )
                        return

                # 5.2 P1-2 60分钟环境：60m 强下行禁抄；其余状态展示在提案里
                if self.higher_tf_enabled:
                    env_score, env_text = self._get_60m_env(code)
                    result['higher_tf_detail'] = env_text
                    if env_score <= -2:
                        self._log_scan(code, price, et_now, result,
                                       outcome='blocked_60m', env_score=env_score)
                        logger.info(f"⏭️  {code} {env_text}，跳过抄底")
                        return

                # 5.3 P1-4 盈亏比门槛：目标(布林中轨)到入场 vs 风险(止损/2ATR)
                if self.rr_gate_enabled:
                    rr_val = float(result.get('rr') or 0)
                    if rr_val < self.rr_min:
                        self._log_scan(code, price, et_now, result,
                                       outcome='blocked_rr', env_score=env_score)
                        logger.info(
                            f"⏭️  {code} 盈亏比 {rr_val:.2f} < {self.rr_min}，"
                            f"反弹空间不够，跳过"
                        )
                        return

                # 5.4 财报窗口闸 + 资金流/期权影子字段：
                #     只在评分达标后拉一次消息面（避免每轮请求），一次拉取两处复用
                signal_ctx = None
                need_ctx = self.earnings_gate_enabled or self.shadow_signals_enabled
                if need_ctx:
                    try:
                        from scripts.live_trading import signal_context as sc
                        signal_ctx = sc.fetch_signal_context(code) or {}
                    except Exception:
                        signal_ctx = {}
                    if self.earnings_gate_enabled:
                        blocked, earn_reason = self._earnings_blackout(signal_ctx.get('earnings'))
                        if blocked:
                            self._log_scan(code, price, et_now, result,
                                           outcome='blocked_earnings', env_score=env_score)
                            logger.info(f"⏭️  {code} {earn_reason}，跳过抄底")
                            return
                    if self.shadow_signals_enabled:
                        self._apply_shadow_fields(result, signal_ctx)

                # 5.5 大盘/指数门（板块代理）：默认影子验证，只记录不拦截
                index_shadow_block = False
                if self.index_gate_enabled and self.index_gate is not None:
                    try:
                        ig_state = self.index_gate.get_state(code)
                        result['index_gate'] = ig_state
                        ig_action = str(ig_state.get('action') or '')
                        eff_threshold = int(self.buy_threshold) + int(
                            ig_state.get('strict_bonus') or 0)
                        if ig_action == 'pause':
                            index_shadow_block = True
                        elif ig_action == 'stricter':
                            index_shadow_block = (
                                int(result.get('score') or 0) < eff_threshold)
                        result['index_shadow_block'] = bool(index_shadow_block)
                        if self.index_gate_enforce and ig_action == 'pause':
                            self._log_scan(code, price, et_now, result,
                                           outcome='blocked_index_pause',
                                           env_score=env_score)
                            logger.info(
                                f"⏭️  {code} {ig_state.get('reason', '指数门pause')}，"
                                f"暂停抄底"
                            )
                            return
                        if (self.index_gate_enforce and ig_action == 'stricter'
                                and index_shadow_block):
                            self._log_scan(code, price, et_now, result,
                                           outcome='blocked_index_stricter',
                                           env_score=env_score)
                            logger.info(
                                f"⏭️  {code} 指数弱势(stricter)：评分 "
                                f"{result.get('score')} < {eff_threshold}，跳过抄底"
                            )
                            return
                    except Exception as e:
                        logger.warning(f"[指数门] {code} 状态计算失败，放行: {e}")

                if self.approval_enabled:
                    # 人工确认模式：只推送，不自动下单
                    acted = self._queue_approval(code, price, result, signal_ctx=signal_ctx)
                else:
                    acted = self._execute_buy(code, price, result['score'])
                outcome = 'passed' if acted else 'queue_skipped'
                if index_shadow_block and not self.index_gate_enforce:
                    outcome = 'shadow_index_would_block'
                self._log_scan(code, price, et_now, result,
                               outcome=outcome, env_score=env_score)
                logger.info(f"  📊 {code} {result.get('details', '')}")
            else:
                self._log_scan(code, price, et_now, result,
                               outcome='below_threshold')
                logger.info(f"  ℹ️  {code} 评分={result['score']} 未达阈值({self.buy_threshold})")
        
        except Exception as e:
            logger.error(f"检查 {code} 异常: {e}")
    
    def _monitor_loop(self):
        """监控循环"""
        logger.info(f"🔍 开始监控，检查间隔 {self.check_interval}s")
        
        while self._running:
            try:
                # 人工确认模式：先处理页面上的「下单 / 拒绝」结果
                if self.approval_enabled:
                    self._process_approvals()

                for code in self.watch_codes:
                    if self._stop_event.is_set():
                        break
                    self._check_one(code)
                    time.sleep(0.5)  # 避免限频
                
                # 等待下一轮
                self._stop_event.wait(self.check_interval)
                
            except Exception as e:
                logger.error(f"监控循环异常: {e}", exc_info=True)
                time.sleep(5)
    
    def start(self):
        """启动监控"""
        if self._running:
            return
        
        logger.info("=" * 60)
        logger.info("  🚀 抄底买入监控器启动")
        logger.info("=" * 60)
        logger.info(f"  监控股票: {', '.join(self.watch_codes)}")
        logger.info(f"  买入阈值: {self.buy_threshold}")
        logger.info(f"  冷却期:   {self.cooldown_minutes} 分钟")
        logger.info(f"  模式:     {'DRY-RUN' if self.dry_run else '实盘'}")
        
        self._setup()
        self._running = True
        
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        
        logger.info("✅ 监控已启动")
    
    def stop(self):
        """停止监控"""
        if not self._running:
            return
        
        logger.info("🛑 停止监控...")
        self._running = False
        self._stop_event.set()
        
        if self._thread:
            self._thread.join(timeout=10)
        
        if self.pool:
            self.pool.close()
        
        logger.info("✅ 已停止")


def main():
    # 日志配置
    log_format = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_format)
    
    parser = argparse.ArgumentParser(description="抄底买入监控器")
    parser.add_argument("--codes", required=True, help="监控股票列表，逗号分隔，如 US.MU,US.AAPL")
    parser.add_argument("--dry-run", action="store_true", help="模拟模式")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--interval", type=int, default=15, help="检查间隔（秒）")
    args = parser.parse_args()
    
    # 加载配置
    config_path = os.path.join(BASE_DIR, args.config)
    with open(config_path, encoding='utf-8') as f:
        config = yaml.safe_load(f)
    
    # 补充默认配置
    if 'dip_buy' not in config:
        config['dip_buy'] = {
            'check_interval': args.interval,
            'buy_threshold': 8,
            'min_bars': 30,
            'max_positions': 3,
            'cooldown_minutes': 30,
        }
    else:
        config['dip_buy']['check_interval'] = args.interval
    
    # 解析股票列表
    codes = [c.strip().upper() for c in args.codes.split(',') if c.strip()]
    if not codes:
        logger.error("请指定监控股票列表")
        sys.exit(1)
    
    # 启动监控
    monitor = DipBuyMonitor(codes, config, dry_run=args.dry_run)
    
    try:
        monitor.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\n👋 用户退出")
        monitor.stop()


if __name__ == "__main__":
    main()
