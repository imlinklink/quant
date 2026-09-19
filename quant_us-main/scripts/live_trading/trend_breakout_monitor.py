"""
唐奇安（海龟）日线突破监控器 - 美股第二条买入策略线

与抄底线（dip_buy）互补：抄底买超卖，突破买强势。

信号规则（与 scripts/run_donchian_backtest.py 同口径，无前视）：
    取最近一根“已收盘”日K（美东时间今天之前），若其收盘价 >
    此前 entry_n 日的最高价（不含当日），即为突破信号；
    可选放量确认：当日成交量 ≥ 前20日均量 × volume_ratio。

风控设计：
    1. 本策略线强制走人工确认：信号只进确认页（entry_mode=donchian），
       页面显示规则理由 + LLM 判定，你点「下单」才执行；
    2. 即使全局 human_approval.enabled=false，本线也不自动下单（fail-closed）；
    3. 与抄底线各自独立拒绝：拒绝“突破提案”只停突破线当日该股，
       不取消“抄底提案”，反之亦然；
    4. 受盘前市场简报门控：buy_frequency=avoid 当天不产生信号；
    5. 买入后由 ChandelierExitManager 扫描券商真实持仓自动接管止损止盈。

用法：
    python scripts/live_trading/trend_breakout_monitor.py --check-once  # 冒烟：只跑一轮
    python scripts/live_trading/trend_breakout_monitor.py --codes US.MU,US.SOXL --dry-run
"""
import argparse
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yaml

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


class TrendBreakoutMonitor:
    """唐奇安日线突破监控器：扫描已收盘日K → 推送确认页提案。"""

    def __init__(
        self,
        watch_codes: List[str],
        config: Dict,
        dry_run: bool = True,
        approval_store=None,
    ):
        tb = config.get('trend_breakout', {})
        self.config = config
        self.dry_run = dry_run

        self.watch_codes = [
            c for c in (watch_codes or config.get('dip_buy', {}).get('watch_list', []))
            if c and not c.startswith('HK.')
        ]
        self.check_interval = int(tb.get('check_interval', 300))
        self.entry_n = int(tb.get('entry_n', 55))
        self.volume_ratio = float(tb.get('volume_ratio', 0) or 0)
        self.position_size_usd = float(tb.get('position_size_usd', 5000))
        self.max_positions = int(tb.get('max_positions', 3))
        self.one_position_per_code = bool(
            tb.get('one_position_per_code',
                  config.get('dip_buy', {}).get('one_position_per_code', True))
        )
        self.min_daily_bars = int(tb.get('min_daily_bars', 60))
        self.proposal_ttl_hours = float(tb.get('proposal_ttl_hours', 12))

        # 人工确认
        approval_cfg = config.get('trading', {}).get('live_trading', {}).get('human_approval', {})
        self.approval_enabled = bool(approval_cfg.get('enabled', False))
        self.approval_store = approval_store
        self.approval_max_drift = float(approval_cfg.get('max_price_drift_pct', 0.03))
        self._approval_rejected_codes = set()
        self._approval_reject_date = None
        self._approval_processed_reject_ids = set()

        if self.approval_enabled and self.approval_store is None:
            # 独立运行没有 Web 页面：提案只记录不执行（安全）
            from scripts.live_trading.approval.proposal_store import ProposalStore
            self.approval_store = ProposalStore(
                ttl_seconds=float(approval_cfg.get('proposal_ttl_seconds', 180))
            )
            logger.warning(
                "[突破线] 未检测到 Web 确认页注入，提案只记录不执行；"
                "请通过 run_all.py 启动（页面 http://127.0.0.1:8890/approvals）"
            )
        if not self.approval_enabled:
            logger.warning(
                "[突破线] human_approval 未启用：本策略线 fail-closed，"
                "只记录不自动下单（防新增策略裸奔）"
            )

        # LLM 判定（仅展示用，最终人工决定）
        self.llm_advisor = None
        self.llm_enabled = False
        self.llm_mode_label = 'shadow'
        self._setup_llm()

        # 防止同一根日K重复提案
        self._proposed_signal_date: Dict[str, str] = {}

        self.entry_mode = 'donchian'
        self.pool = None
        self._running = False
        self._stop_event = threading.Event()
        self._thread = None
        self._next_scan_ts: float = 0.0

    # ==================== 初始化 ====================
    def _env_label(self) -> str:
        if self.dry_run:
            return 'DRY-RUN'
        return str(self.config.get('live_manager', {}).get('trd_env', 'SIMULATE'))

    def _setup_llm(self):
        llm_cfg = self.config.get('llm', {})
        if not llm_cfg.get('enabled', False):
            return
        self.llm_mode_label = 'shadow' if llm_cfg.get('shadow_mode', True) else 'real_veto'
        try:
            from mutifactor.llm import LLMAdvisor
            self.llm_advisor = LLMAdvisor(llm_cfg)
            self.llm_enabled = bool(getattr(self.llm_advisor, 'enabled', False))
        except Exception as e:
            logger.warning(f"[突破线] LLM 初始化失败，页面显示无判定: {e}")

    def _setup(self):
        from scripts.live_trading.chandelier_exit_manager import FutuConnectionPool
        futu_cfg = self.config.get('futu', {})
        self.pool = FutuConnectionPool(
            host=futu_cfg.get('host', '127.0.0.1'),
            port=int(futu_cfg.get('port', 11111)),
            quote_pool_size=1,
            trade_pool_size=1,
            market='US',
        )
        logger.info(
            f"✅ 突破线初始化: 监控 {len(self.watch_codes)} 只 | "
            f"{self.entry_n}日通道 放量{self.volume_ratio or '关'}× | "
            f"间隔{self.check_interval}s"
        )

    # ==================== 日K信号 ====================
    def _get_daily_df(self, code: str) -> Optional[pd.DataFrame]:
        """拉日K（前复权），返回标准化列并预计算通道指标。"""
        from futu import KLType, RET_OK
        et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))
        start = (et_now - timedelta(days=300)).strftime('%Y-%m-%d')
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
                'open': data['open'].astype(float),
                'high': data['high'].astype(float),
                'low': data['low'].astype(float),
                'close': data['close'].astype(float),
                'volume': data['volume'].astype(float),
            })
            df = df.sort_values('date').drop_duplicates(subset=['date']).reset_index(drop=True)
            n = self.entry_n
            df[f'hh{n}'] = df['high'].shift(1).rolling(n).max()
            df['vma20'] = df['volume'].shift(1).rolling(20).mean()
            df['vol_ratio'] = df['volume'] / df['vma20']
            prev_close = df['close'].shift(1)
            tr = pd.concat(
                [df['high'] - df['low'],
                 (df['high'] - prev_close).abs(),
                 (df['low'] - prev_close).abs()],
                axis=1,
            ).max(axis=1)
            df['atr'] = tr.rolling(14).mean()
            return df
        except Exception as e:
            logger.warning(f"[突破线] {code} 日K获取异常: {e}")
            return None

    def _scan_signal(self, code: str) -> Optional[Dict]:
        """
        返回最近一根“已收盘”日K是否触发突破。
        signal_date 用该日K的日期，避免同一根K重复提案。
        """
        et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))
        df = self._get_daily_df(code)
        if df is None or len(df) < self.min_daily_bars:
            logger.info(f"[突破线] {code} 日K不足（{0 if df is None else len(df)}<{self.min_daily_bars}）")
            return None
        # 只用美东“今天之前”已收盘的K（不含当日半根）
        completed = df[df['date'] < pd.Timestamp(et_now.date())]
        if len(completed) < max(self.min_daily_bars, self.entry_n + 20):
            return None
        row = completed.iloc[-1]
        sig_date = row['date'].date()
        hh_col = f'hh{self.entry_n}'
        hh = row.get(hh_col)
        close = float(row['close'])
        if hh is None:
            return None
        try:
            hh = float(hh)
        except (TypeError, ValueError):
            return None
        if not np.isfinite(hh):
            return None
        from scripts.live_trading.decision_ledger.workflow import candidate
        volume = row.get('vol_ratio')
        passed = close > hh and (self.volume_ratio <= 1 or (
            volume is not None and np.isfinite(float(volume)) and float(volume) >= self.volume_ratio))
        candidate(self, code, 'donchian', sig_date, passed,
                  {'signal_close': close, 'channel_high': hh, 'price': close,
                   'initial_stop': max(close-2*float(row['atr']),close*.95) if pd.notna(row.get('atr')) else None,
                   'breakout_level': hh, 'signal_atr':float(row['atr']) if pd.notna(row.get('atr')) else None,
                   'max_chase_atr': float(self.config.get('trend_breakout',{}).get('max_chase_atr',.5)),
                   'failure_sessions': int(self.config.get('trend_breakout',{}).get('failure_sessions',3))})
        if not (close > float(hh)):
            return None
        vr = row.get('vol_ratio')
        if self.volume_ratio > 1:
            if vr is None:
                return None
            try:
                vr = float(vr)
            except (TypeError, ValueError):
                return None
            if not np.isfinite(vr) or vr < self.volume_ratio:
                return None
        atr = row.get('atr')
        if atr is not None:
            try:
                atr = float(atr)
            except (TypeError, ValueError):
                atr = None
            if atr is not None and not np.isfinite(atr):
                atr = None
        return {
            'signal_date': sig_date,
            'close': close,
            'channel_high': float(hh),
            'vol_ratio': vr,
            'atr': atr,
        }

    # ==================== 提案与下单 ====================
    def _ask_llm_verdict(self, code: str, price: float, context_text: str = '') -> Optional[Dict]:
        if self.llm_advisor is None or not self.llm_enabled:
            return None
        try:
            result = self.llm_advisor.veto_buy(
                buy_list=[code],
                holdings=[],
                cash=float(self.position_size_usd),
                market_context=(
                    f'US 个股 {code}，日线唐奇安{self.entry_n}日突破信号，'
                    f'信号价 ${price:.2f}\n{context_text}'
                ),
            )
            if not result:
                return {'verdict': None, 'confidence': None,
                        'reason': '本轮 LLM 未返回结果（调用失败），纯规则信号',
                        'mode': self.llm_mode_label}
            return {
                'model': getattr(self.llm_advisor, 'model', ''),
                'mode': self.llm_mode_label,
                'verdict': result.get('verdict', 'allow'),
                'risk_level': result.get('risk_level', 'LOW'),
                'confidence': result.get('confidence'),
                'reason': result.get('reason', ''),
            }
        except Exception as e:
            logger.warning(f"[突破线] LLM 判定异常: {e}")
            return {'verdict': None, 'confidence': None,
                    'reason': f'LLM 调用异常: {e}', 'mode': self.llm_mode_label}

    def _effective_position_size_usd(self) -> float:
        """与抄底线同款：按盘前简报建议仓位比例缩放（0.5x~1.0x）。"""
        try:
            from scripts.live_trading import market_brief as mb
            ratio = mb.load_brief().get('suggested_position_ratio')
            if not ratio:
                return float(self.position_size_usd)
            scale = max(0.5, min(1.0, float(ratio) / 0.2))
            return float(self.position_size_usd) * scale
        except Exception:
            return float(self.position_size_usd)

    def _queue_proposal(self, code: str, sig: Dict, price: float) -> bool:
        if self.approval_store is None:
            return False
        from scripts.live_trading.decision_ledger.workflow import candidate, enabled, start_review
        decision = candidate(self, code, 'donchian', sig['signal_date'], True,
                             {'signal_close': float(sig['close']), 'channel_high': float(sig['channel_high'])})
        today = datetime.now().strftime('%Y-%m-%d')
        if self._approval_reject_date != today:
            self._approval_rejected_codes.clear()
            self._approval_reject_date = today
        if code in self._approval_rejected_codes:
            logger.info(f"[突破线] {code} 今日已被拒绝（突破线），跳过")
            return False
        if self.approval_store.has_active_for_code(code):
            logger.info(f"[突破线] {code} 已有待确认/执行中的提案，跳过")
            return False

        sig_date = str(sig['signal_date'])
        if self._proposed_signal_date.get(code) == sig_date:
            return False

        size = self._effective_position_size_usd()
        qty = int(size / price) if price > 0 else 0
        if qty <= 0:
            logger.warning(f"[突破线] {code} 数量计算为0，无法推送")
            return False

        atr = sig.get('atr')
        suggested_stop = None
        if atr and np.isfinite(atr):
            suggested_stop = max(price - 2.0 * atr, price * 0.95)
        parts = [
            f"唐奇安{self.entry_n}日突破: {sig_date} 收盘 ${sig['close']:.2f} "
            f"> 前{self.entry_n}日最高 ${sig['channel_high']:.2f}"
        ]
        if sig.get('vol_ratio') is not None:
            parts.append(f"放量 {sig['vol_ratio']:.2f}×")
        parts.append(f"信号价约 ${price:.2f}，单票名义金额上限 ${size:.0f}，数量按风险预算核算")
        if suggested_stop:
            parts.append(f"初始参考止损 ≈ ${suggested_stop:.2f}（-5% 与 2×ATR 孰高，实盘由吊灯自动跟进）")
        reason = "；".join(parts)

        context_text = ''
        signal_ctx = {}
        try:
            from scripts.live_trading import signal_context
            signal_ctx = signal_context.fetch_signal_context(code)
            context_text = signal_context.format_context(code, signal_ctx)
        except Exception:
            pass

        from scripts.live_trading.decision_ledger.workflow import news_evidence
        if enabled(self):
            from scripts.live_trading.decision_ledger.workflow import risk_preview
            decision['risk_summary'] = risk_preview(self, code, price, suggested_stop, size, qty)
            qty = decision['risk_summary']['quantity']
        created = self.approval_store.create(
            stock_code=code,
            stock_name=code,
            market_type='US',
            env=self._env_label(),
            price=round(price, 4),
            quantity=qty,
            estimated_cost=round(price * qty, 2),
            per_stock_capital=float(size),
            entry_mode='donchian',
            trade_plan={'initial_stop': suggested_stop, 'breakout_level': sig['channel_high'],
                        'signal_atr': atr, 'signal_time': str(sig['signal_date']),
                        'max_chase_atr': float(self.config.get('trend_breakout', {}).get('max_chase_atr', .5)),
                        'failure_sessions': int(self.config.get('trend_breakout', {}).get('failure_sessions', 3))},
            trigger_reason=f'唐奇安{self.entry_n}日突破',
            kline_signal='donchian_breakout',
            reason=reason,
            context=context_text,
            evidence_items=news_evidence(signal_ctx, code),
            llm=None if enabled(self) else self._ask_llm_verdict(code, price, context_text=context_text),
            expires_at=time.time() + self.proposal_ttl_hours * 3600,
            **decision,
        )
        start_review(self, created)
        self._proposed_signal_date[code] = sig_date
        logger.warning(
            f"[突破线] {code} 推送待确认: {sig_date}收盘突破前{self.entry_n}日高 "
            f"@{price:.2f} 约{qty}股 —— 请在确认页点「下单」"
        )
        return True

    def _get_position_count(self) -> int:
        if self.dry_run:
            try:
                from scripts.live_trading.position_registry import REGISTRY
                return REGISTRY.count()
            except Exception:
                return 0
        from futu import RET_OK, TrdEnv
        try:
            trd_env_str = self.config.get('live_manager', {}).get('trd_env', 'SIMULATE')
            trd_env = TrdEnv.SIMULATE if trd_env_str == 'SIMULATE' else TrdEnv.REAL
            with self.pool.get_trade_ctx() as ctx:
                ret, data = ctx.position_list_query(trd_env=trd_env)
                if ret != RET_OK:
                    return 0
                return int((data['qty'] != 0).sum()) if data is not None and len(data) > 0 else 0
        except Exception as e:
            logger.warning(f"[突破线] 持仓查询异常: {e}")
            return 0

    def _reserved_slot_count(self) -> int:
        """容量已占的格数：持仓 ∪ 在途买单 ∪ 在途买入提案（与 `submit` 同口径）。

        与 `_get_position_count()` 的差别正是本方法存在的理由：后者**只数持仓**，
        在途提案完全看不见 —— 于是一个代码挂一个提案、各自按"容量全空"定仓，
        第 N 个要到提交时才被拒（**谁赢取决于轮询顺序，不是任何决策**）。
        三处判断（提案前、批准后、提交时）必须用同一个集合。
        """
        from scripts.live_trading.execution import reserved_slots, service_for
        # **事务外**取提案：`active_buys()` 自己会开事务，进了 registry.transaction() 再调会死锁。
        active = self.approval_store.active_buys() if self.approval_store else []
        service = service_for(self)
        with service.registry.transaction() as book:
            return len(reserved_slots(book, active))

    def _has_position(self, code: str) -> bool:
        """单代码一仓：dry-run 用共享模拟登记簿；实盘查券商。"""
        if self.dry_run:
            try:
                from scripts.live_trading.position_registry import REGISTRY
                return REGISTRY.get(code) is not None
            except Exception:
                return False
        from futu import RET_OK, TrdEnv
        try:
            trd_env_str = self.config.get('live_manager', {}).get('trd_env', 'SIMULATE')
            trd_env = TrdEnv.SIMULATE if trd_env_str == 'SIMULATE' else TrdEnv.REAL
            with self.pool.get_trade_ctx() as ctx:
                ret, data = ctx.position_list_query(trd_env=trd_env, code=code)
            if ret != RET_OK or data is None or len(data) == 0:
                return False
            return bool((data['qty'] != 0).any())
        except Exception as e:
            logger.warning(f"[突破线] 单代码持仓查询异常 {code}: {e}")
            return False

    def _get_current_price(self, code: str) -> Optional[float]:
        from futu import SubType, RET_OK
        et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))
        et_time = et_now.hour * 60 + et_now.minute
        PRE_MARKET_START, PRE_MARKET_END = 4 * 60, 9 * 60 + 30
        REGULAR_END, AFTER_HOURS_END = 16 * 60, 20 * 60
        try:
            with self.pool.get_quote_ctx() as ctx:
                ctx.subscribe([code], [SubType.QUOTE], subscribe_push=False)
                ret, snapshot = ctx.get_market_snapshot([code])
                if ret != RET_OK or snapshot is None or snapshot.empty:
                    return None
                r = snapshot.iloc[0]
                if et_time < PRE_MARKET_START or et_time >= AFTER_HOURS_END:
                    price = r.get('overnight_price', 0)
                elif et_time < PRE_MARKET_END:
                    price = r.get('pre_price', 0)
                elif et_time < REGULAR_END:
                    price = r.get('last_price', 0)
                else:
                    price = r.get('after_price', 0)
                price = float(price or 0)
                return price if price > 0 else None
        except Exception as e:
            logger.warning(f"[突破线] {code} 取价异常: {e}")
            return None

    def _execute_buy(self, code: str, price: float, proposal_id: Optional[str] = None) -> bool:
        # 所有成交统一经过审批执行器，禁用旧的直接买入入口。
        if not proposal_id or not self.approval_store:
            return False
        from scripts.live_trading.execution import service_for
        item = self.approval_store.get(proposal_id)
        return service_for(self).submit(item, price, self._effective_position_size_usd()) == 'filled'

    def _execute_approved(self, item: Dict):
        from scripts.live_trading.execution import execute_approved_buy
        execute_approved_buy(self, item)

    def _process_approvals(self):
        """只处理本策略线（entry_mode=donchian）的点击结果。"""
        if self.approval_store is None:
            return
        today = datetime.now().strftime('%Y-%m-%d')
        if self._approval_reject_date != today:
            self._approval_rejected_codes.clear()
            self._approval_reject_date = today
        from scripts.live_trading.execution import reconcile_capacity, service_for
        service_for(self).reconcile()
        self.approval_store.expire_old()
        # 容量对账：超出 max_positions 的在途买入提案按**规则序**标为 skipped（带位次与上限）。
        # 必须在处理批准**之前**跑 —— 否则会先执行一批、再把剩下的判超容量。
        try:
            dropped = reconcile_capacity(self)
            if dropped.get('skipped'):
                logger.warning(f"[突破线] 容量未分配，已跳过: "
                               f"{[d['code'] for d in dropped['skipped']]}")
        except Exception:
            logger.exception('[突破线] 容量对账失败（不影响已批准订单的执行）')
        for item in self.approval_store.rejected_items():
            if item.get('side', 'buy') != 'buy':
                continue
            if item.get('entry_mode', '') != self.entry_mode:
                continue
            if item.get('id') in self._approval_processed_reject_ids:
                continue
            self._approval_processed_reject_ids.add(item.get('id', ''))
            self._approval_rejected_codes.add(item.get('stock_code', ''))
            # 只取消本策略线同代码的其它提案
            for other in self.approval_store.get_all():
                if (other.get('stock_code') == item.get('stock_code')
                        and other.get('side', 'buy') == 'buy'
                        and other.get('entry_mode', '') == self.entry_mode
                        and other['status'] in ('pending', 'approved')):
                    self.approval_store.mark(other['id'], 'expired', note='突破线同代码已被拒绝，取消')
        for item in self.approval_store.approved_items():
            if item.get('side', 'buy') != 'buy':
                continue
            if item.get('entry_mode', '') != self.entry_mode:
                continue
            self._execute_approved(item)

    # ==================== 主循环 ====================
    def _check_one(self, code: str):
        try:
            # 市场简报门控（与抄底线一致）
            try:
                from scripts.live_trading import market_brief as mb
                brief = mb.load_brief()
                # 只允许“今天的简报”触发 avoid 闸门（与抄底线一致）
                today_str = datetime.now().strftime('%Y-%m-%d')
                if brief.get('date') == today_str and not mb.buy_allowed(brief):
                    key = brief.get('date') or today_str
                    if getattr(self, '_brief_avoid_date', None) != key:
                        self._brief_avoid_date = key
                        logger.warning(
                            f"[突破线] 今日简报 buy_frequency=avoid，暂停突破信号: "
                            f"{brief.get('risk_note', '')}"
                        )
                    return
            except Exception:
                pass
            sig = self._scan_signal(code)
            if not sig:
                return
            if self._reserved_slot_count() >= self.max_positions:
                logger.info(f"[突破线] {code} 容量已满（持仓/在途订单/在途提案），跳过")
                return
            if self.one_position_per_code and self._has_position(code):
                logger.info(f"[突破线] {code} 已持有（单代码一仓），跳过")
                return
            price = self._get_current_price(code)
            if not price:
                logger.info(f"[突破线] {code} 无法获取当前价")
                return
            if self.approval_enabled:
                self._queue_proposal(code, sig, price)
            else:
                # fail-closed：不自动下单，仅提示
                logger.warning(
                    f"[突破线] {code} 触发{self.entry_n}日突破但人工确认未启用，"
                    f"本线不自动下单（fail-closed）"
                )
        except Exception as e:
            logger.error(f"[突破线] 检查 {code} 异常: {e}", exc_info=True)

    def _scan_cycle(self):
        """扫描新信号（仅工作日；由 _monitor_loop 按 check_interval 调度）。"""
        et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))
        if et_now.weekday() >= 5:
            logger.info("[突破线] 周末（美东）不检查突破信号")
            return
        for code in self.watch_codes:
            if self._stop_event.is_set():
                break
            self._check_one(code)
            time.sleep(0.5)
        # 心跳：记录本轮扫描完成（决策健康面板据此区分扫描时间与信号事件时间）
        if self.approval_store is not None:
            try:
                from scripts.live_trading.decision_ledger.decision_health import record_scan_heartbeat
                record_scan_heartbeat(self.approval_store.events)
            except Exception:
                pass

    def _monitor_loop(self):
        logger.info(f"[突破线] 监控循环启动：点击处理每30s，信号扫描每 {self.check_interval}s")
        while self._running:
            try:
                # 用户点击“下单/拒绝”随时处理（如周五盘后提案、周末确认）
                if self.approval_enabled:
                    self._process_approvals()
                now = time.time()
                if now >= self._next_scan_ts:
                    self._scan_cycle()
                    self._next_scan_ts = now + self.check_interval
            except Exception as e:
                logger.error(f"[突破线] 监控循环异常: {e}", exc_info=True)
            self._stop_event.wait(30)

    def start(self):
        if self._running:
            return
        if not self.watch_codes:
            logger.warning("[突破线] 观察池为空，跳过启动")
            return
        self._setup()
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info(f"[突破线] 已启动，监控 {len(self.watch_codes)} 只")

    def run_once(self):
        """冒烟用：初始化并跑一轮后退出。"""
        self._setup()
        self._running = True
        try:
            if self.approval_enabled:
                self._process_approvals()
            self._scan_cycle()
        finally:
            self._running = False
            if self.pool:
                self.pool.close()

    def stop(self):
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        if self.pool:
            self.pool.close()
        logger.info("[突破线] 已停止")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
        datefmt='%H:%M:%S',
    )
    ap = argparse.ArgumentParser(description='唐奇安突破监控器（冒烟/独立运行）')
    ap.add_argument('--codes', default=None, help='逗号分隔，默认取 config')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--check-once', action='store_true', help='只跑一轮后退出')
    args = ap.parse_args()

    cfg_path = os.path.join(BASE_DIR, 'config.yaml')
    with open(cfg_path, encoding='utf-8') as f:
        config = yaml.safe_load(f)
    codes = [c.strip() for c in args.codes.split(',') if c.strip()] if args.codes else None
    monitor = TrendBreakoutMonitor(codes, config, dry_run=args.dry_run)
    if args.check_once:
        monitor.run_once()
        return
    monitor.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        monitor.stop()


if __name__ == '__main__':
    main()
