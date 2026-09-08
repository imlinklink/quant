"""强势回调/突破回踩监控：默认关闭，复用审批与统一风险执行入口。"""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from .trend_breakout_monitor import TrendBreakoutMonitor
from .strategy_rules import ExitData, pullback_signal, breakout_retest_signal

logger=logging.getLogger(__name__)


class PullbackMonitor(TrendBreakoutMonitor):
    def __init__(self, watch_codes, config, dry_run=False, approval_store=None, mode='pullback'):
        super().__init__(watch_codes, config, dry_run=dry_run, approval_store=approval_store)
        self.entry_mode=mode
        self.strategy_cfg=config.get(mode,{})
        self.check_interval=float(self.strategy_cfg.get('check_interval',60))
        self.shadow_only=bool(self.strategy_cfg.get('shadow_only',True))
        self.approval_enabled=True

    def _scan_signal(self,code):
        if not hasattr(self,'_signal_data'):
            self._signal_data=ExitData(self.pool)
        now=datetime.now(ZoneInfo('America/New_York'))
        daily=self._signal_data.get(code)
        intraday=self._signal_data.get(code,15)
        if self.entry_mode=='breakout_retest':
            return breakout_retest_signal(daily,intraday,now,self.config.get('trend_breakout',{}))
        group=self.config.get('risk_budget',{}).get('code_groups',{}).get(code)
        proxy=self.strategy_cfg.get('sector_proxies',{}).get(group)
        if not proxy:
            return None
        sector=self._signal_data.get(proxy)
        source=self.strategy_cfg.get('underlying_sources',{}).get(code)
        underlying=self._signal_data.get(source) if source else None
        if source and (underlying is None or underlying.empty):
            return None
        return pullback_signal(daily,sector,intraday,now,self.strategy_cfg,underlying)

    def _queue_proposal(self,code,sig,price):
        if self._proposed_signal_date.get(code)==sig['signal_id']:
            return False
        if self.shadow_only:
            logger.info('[影子信号] %s %s %s',self.entry_mode,code,sig)
            self._proposed_signal_date[code]=sig['signal_id']
            return False
        if not self.approval_store or self.approval_store.has_active_for_code(code) or code in self._approval_rejected_codes:
            return False
        if price<=sig['initial_stop'] or price-sig['initial_stop']>2*sig['signal_atr']:
            return False
        size=self._effective_position_size_usd()
        quantity=int(size/price)
        if quantity<1:
            return False
        self.approval_store.create(stock_code=code,stock_name=code,side='buy',market_type='US',env=self._env_label(),
            price=price,quantity=quantity,estimated_cost=price*quantity,per_stock_capital=size,
            entry_mode=self.entry_mode,trade_plan=sig,trigger_reason=self.entry_mode,
            reason=f"{self.entry_mode} 已收盘15分钟确认；初始止损 {sig['initial_stop']:.2f}",
            llm=self._ask_llm_verdict(code,price,context_text=str(sig)),expires_at=time.time()+180)
        self._proposed_signal_date[code]=sig['signal_id']
        return True
