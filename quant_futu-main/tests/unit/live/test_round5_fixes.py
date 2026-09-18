# -*- coding: utf-8 -*-
"""第五轮审查修复的行为测试：
1) 部分成交后本地保留余量、整仓才进冷却期
2) 卖出确认原因(卖出|atr_stop_loss)/追涨止损 进入冷却期，止盈不进
3) wait_for_confirmation 总超时遇部分成交必须撤剩余单
4) YAML 损坏时 _load_table 抛错且保存不覆盖原文件
5) ExitStrategy 能从 config['risk'] 读到 time_exit/early_hard_stop/rsrs 参数
"""
import os
import threading
import unittest
from unittest import mock

import pandas as pd
import pytest

from mutifactor.infra.yaml_storage import YAMLStorage
from mutifactor.strategies.exit_strategy import ExitStrategyFactory
from mutifactor.trading.futu_trader import FutuTrader, OrderError
from mutifactor.trading.exceptions import TimeoutError as FutuTimeoutError


# ---------- 1/2) HKPositionManager 卖出余量与冷却期 ----------

class _FakeState:
    def load_state(self, env=None):
        return {'positions': {}, 'cooldowns': {}}
    def get_positions(self, env=None):
        return []
    def save_state(self, **kw):
        pass
    def save_position(self, **kw):
        pass
    def save_capital(self, **kw):
        pass
    def clear_positions(self, env=None):
        pass
    def save_trade(self, **kw):
        pass


class _FakeFetcher:
    def get_current_price(self, code, force_refresh=False):
        return None
    def get_today_high_low(self, code):
        return (None, None)


class _FakeTrader:
    def __init__(self, dealt):
        self.dealt = dealt
        self.orders = 0
    def get_positions(self):
        return []
    def place_order(self, stock_code, quantity, order_type, side, timeout,
                    partial_timeout=30):
        self.orders += 1
        return ('O1', 55.0, self.dealt)


def _make_pm(dealt):
    from scripts.live_trading.hk_position_manager import HKPositionManager
    pm = HKPositionManager(
        {'strategy': {'initial_capital': 100000.0}, 'risk': {},
         'trading': {'env': 'SIMULATE', 'live_trading': {}}},
        _FakeTrader(dealt), _FakeState(), _FakeFetcher(),
    )
    pm._get_stock_name = lambda c: c
    pm.market_adapter = mock.Mock()
    pm.market_adapter.calculate_trading_cost.return_value = {
        'total': 0.0, 'commission': 0.0, 'stamp_duty': 0.0}
    pm.save_positions = mock.Mock()
    pm._save_trade_record = mock.Mock()
    return pm


class TestPartialExitAndCooldown(unittest.TestCase):
    def test_partial_fill_keeps_remaining(self):
        pm = _make_pm(dealt=300)
        pm.strategy_positions = {'HK.A': {'quantity': 1000, 'cost_price': 50.0,
                                          'highest_price': 60.0}}
        pm.strategy_used_capital = 50000.0
        pm.strategy_capital = 100000.0
        pm._execute_exit('HK.A', 1000, 50.0, 'atr_stop_loss')
        pos = pm.strategy_positions.get('HK.A')
        assert pos is not None, '部分成交必须保留余量'
        assert pos['quantity'] == 700
        assert '_selling' not in pos
        # 卖出 300 股的成本被释放，余量继续占用
        assert abs(pm.strategy_used_capital - 35000.0) < 1e-6
        assert abs(pm.strategy_capital - 101500.0) < 1e-6
        assert 'HK.A' not in pm.recently_stopped, '仍持有余量，不应进冷却期'

    def test_full_fill_release_and_cooldown_sell_approval_prefix(self):
        pm = _make_pm(dealt=200)
        pm.strategy_positions = {'HK.B': {'quantity': 200, 'cost_price': 10.0,
                                          'highest_price': 12.0}}
        pm.strategy_used_capital = 2000.0
        pm.strategy_capital = 100000.0
        pm._execute_exit('HK.B', 200, 10.0, '卖出|atr_stop_loss')
        assert 'HK.B' not in pm.strategy_positions
        assert 'HK.B' in pm.recently_stopped

    def test_momentum_stop_enters_cooldown(self):
        pm = _make_pm(dealt=100)
        pm.strategy_positions = {'HK.C': {'quantity': 100, 'cost_price': 10.0,
                                          'highest_price': 10.0}}
        pm.strategy_used_capital = 1000.0
        pm._execute_exit('HK.C', 100, 10.0, '追涨止损|固定9.8(-2%)')
        assert 'HK.C' in pm.recently_stopped

    def test_take_profit_not_in_cooldown(self):
        pm = _make_pm(dealt=100)
        pm.strategy_positions = {'HK.D': {'quantity': 100, 'cost_price': 10.0,
                                          'highest_price': 20.0}}
        pm.strategy_used_capital = 1000.0
        pm.strategy_capital = 100000.0
        pm._execute_exit('HK.D', 100, 10.0, 'atr_take_profit')
        assert 'HK.D' not in pm.recently_stopped


# ---------- 3) wait_for_confirmation 超时部分成交必须撤单 ----------

class _Ctx:
    def __init__(self, polls, cancel_fail=False):
        self.polls = list(polls)
        self.last = None
        self.cancel_calls = 0
        self.cancel_fail = cancel_fail
    def order_list_query(self, order_id=None, trd_env=None):
        if self.polls:
            self.last = self.polls.pop(0)
        row = self.last
        df = pd.DataFrame([{
            'order_status': row[0],
            'dealt_avg_price': row[1],
            'dealt_qty': row[2],
        }])
        return 0, df
    def modify_order(self, **kw):
        self.cancel_calls += 1
        if self.cancel_fail:
            return 1, 'cancel error'
        return 0, pd.DataFrame()


class TestWaitConfirmationCancelOnPartial(unittest.TestCase):
    def _make_trader(self, ctx):
        from futu import TrdEnv
        t = FutuTrader.__new__(FutuTrader)
        t._connected = True
        t.env = TrdEnv.SIMULATE
        t.trade_ctx = ctx
        t.quote_ctx = None
        return t

    def test_total_timeout_partial_cancels_and_returns_fill(self):
        from futu import OrderStatus
        # 持续 FILLED_PART，直到总超时
        ctx = _Ctx([(OrderStatus.FILLED_PART, 55.0, 300)] * 50)
        trader = self._make_trader(ctx)
        with mock.patch('mutifactor.trading.futu_trader.time.sleep'):
            avg, qty = trader.wait_for_confirmation('O1', timeout=0.5)
        assert ctx.cancel_calls == 1, '部分成交超时必须撤剩余单'
        assert qty == 300 and avg == 55.0

    def test_total_timeout_no_fill_cancels_then_raises(self):
        from futu import OrderStatus
        ctx = _Ctx([(OrderStatus.SUBMITTED, 0.0, 0)] * 50)
        trader = self._make_trader(ctx)
        with mock.patch('mutifactor.trading.futu_trader.time.sleep'):
            with pytest.raises(FutuTimeoutError):
                trader.wait_for_confirmation('O2', timeout=0.5)
        assert ctx.cancel_calls == 1

    def test_partial_fill_cancel_ordererror_returns_partial(self):
        from futu import OrderStatus
        # 部分成交后撤单失败（OrderError）：已成交部分必须返回，不能丢失
        ctx = _Ctx([(OrderStatus.FILLED_PART, 55.0, 300)] * 50, cancel_fail=True)
        trader = self._make_trader(ctx)
        with mock.patch('mutifactor.trading.futu_trader.time.sleep'):
            avg, qty = trader.wait_for_confirmation('O3', timeout=0.5,
                                                    partial_timeout=0)
        assert ctx.cancel_calls >= 1
        assert qty == 300 and avg == 55.0


# ---------- 4) YAML 损坏不覆盖 ----------

class TestYamlCorruptionFailSafe(unittest.TestCase):
    def test_corrupt_load_raises_and_save_does_not_wipe(self, tmp_path=None):
        import tempfile
        d = tempfile.mkdtemp(prefix='yaml_corrupt_')
        f = os.path.join(d, 'positions.yaml')
        original = 'positions:\n  - stock_code: HK.X\n    qty: 100\n'
        with open(f, 'w', encoding='utf-8') as fh:
            fh.write(original)
        # 制造损坏
        with open(f, 'w', encoding='utf-8') as fh:
            fh.write('positions:\n  - {broken\n')
        storage = YAMLStorage(data_dir=d)
        with pytest.raises(RuntimeError):
            storage._load_table('positions', use_cache=False)
        with pytest.raises(RuntimeError):
            storage.save_position(stock_code='HK.Y', quantity=10,
                                  cost_price=5.0, highest_price=5.0,
                                  stock_name='Y', env=type('E', (), {'value': 'SIMULATE'})())
        content = open(f, encoding='utf-8').read()
        assert 'broken' in content, '损坏文件不得被覆盖清空'
        assert not os.path.exists(f + '.tmp')


# ---------- 5) risk 配置接线 ----------

class TestRiskConfigWiring(unittest.TestCase):
    def test_full_config_risk_section_is_used(self):
        cfg = {'risk': {
            'exit_strategy': 'atr_dynamic',
            'time_exit': {'phase1_days': 120, 'phase2_days': 250,
                          'phase3_days': 350, 'phase2_multiplier': 1.0},
            'early_hard_stop_pct': 0.08,
            'rsrs_warn': {'early_exempt_days': 10},
            'decline_acceleration': {'enabled': True},
            'take_profit_multiplier': 3.0,
            'stop_loss_multiplier': 2.0,
        }, 'atr_period': 14}
        s = ExitStrategyFactory.create('atr_dynamic', cfg)
        assert s.phase3_days == 350
        assert s.early_hard_stop_pct == 0.08
        assert s.rsrs_early_exempt_days == 10
        assert s.phase1_days == 120
        assert s.take_profit_multiplier == 3.0


# ---------- 6) futu_trader 港美股识别（现金列/持仓过滤按 market） ----------

class _AccCtx:
    def __init__(self, df):
        self.df = df
    def accinfo_query(self, trd_env=None):
        return 0, self.df
    def position_list_query(self, trd_env=None):
        return 0, self.df


class TestFutuTraderMarketAware(unittest.TestCase):
    def _trader(self, market):
        from futu import TrdEnv
        t = FutuTrader.__new__(FutuTrader)
        t._connected = True
        t.env = TrdEnv.SIMULATE
        t.market = market
        t.trade_ctx = None
        t.quote_ctx = None
        return t

    def test_account_cash_picks_us_column_for_us_market(self):
        from futu import TrdMarket
        df = pd.DataFrame([{
            'hk_cash': 0.0,
            'us_cash': 12345.6,
            'total_assets': 20000.0,
            'market_val': 7000.0,
        }])
        t = self._trader(TrdMarket.US)
        t.trade_ctx = _AccCtx(df)
        info = t.get_account_info()
        assert abs(info['cash'] - 12345.6) < 1e-6, 'US 市场必须读 us_cash，而不是 hk_cash=0'

    def test_account_cash_picks_hk_column_for_hk_market(self):
        from futu import TrdMarket
        df = pd.DataFrame([{
            'hk_cash': 8888.0,
            'us_cash': 0.0,
            'total_assets': 10000.0,
            'market_val': 1000.0,
        }])
        t = self._trader(TrdMarket.HK)
        t.trade_ctx = _AccCtx(df)
        info = t.get_account_info()
        assert abs(info['cash'] - 8888.0) < 1e-6

    def test_positions_filtered_by_market(self):
        from futu import TrdMarket
        df = pd.DataFrame([
            {'code': 'US.AAPL', 'qty': 10, 'cost_price': 100.0, 'market_val': 1100.0},
            {'code': 'HK.00700', 'qty': 100, 'cost_price': 300.0, 'market_val': 31000.0},
        ])
        t = self._trader(TrdMarket.US)
        t.trade_ctx = _AccCtx(df)
        positions = t.get_positions()
        codes = {p['stock_code'] for p in positions}
        assert codes == {'US.AAPL'}


# ---------- 7) market_brief 老文件缺 date 归一化 ----------

class TestMarketBriefDateNormalize(unittest.TestCase):
    def test_legacy_brief_without_date_gets_defaults(self):
        import json
        import tempfile
        from pathlib import Path
        import scripts.live_trading.market_brief as mb
        d = tempfile.mkdtemp(prefix='brief_')
        p = Path(d) / 'latest.json'
        p.write_text(json.dumps({
                'risk_level': 'cautious',
                'buy_frequency': 'reduce',
                'suggested_position_ratio': 0.3,
            }), encoding='utf-8')
        with mock.patch.object(mb, 'BRIEF_PATH', p):
            brief = mb.load_brief()
        assert brief['date'] is None  # 老文件没有 date，但其他默认键已补齐
        assert brief['risk_level'] == 'cautious'
        assert brief['buy_frequency'] == 'reduce'
        assert brief['generated_at'] is None


# ---------- 8) 限流器窗口到期只删过期时间戳，不整表清空 ----------

class TestRateLimiterWindow(unittest.TestCase):
    def _check(self, limiter_mod):
        limiter = limiter_mod.RateLimiter(
            min_interval=0.0, max_requests_per_window=3, window_seconds=30)
        # 时间戳 75/85/95（相对假时钟 t0=100 均在 30s 窗口内），
        # 触发窗口限制：等待 5s 后最早一条(75)过期，其余两条应保留
        limiter.request_timestamps = [75.0, 85.0, 95.0]
        limiter.last_request_time = 95.0
        clock = [100.0]
        def _fake_time():
            return clock[0]
        def _fake_sleep(secs):
            clock[0] += secs
        with mock.patch.object(limiter_mod.time, 'time', side_effect=_fake_time):
            with mock.patch.object(limiter_mod.time, 'sleep', side_effect=_fake_sleep):
                limiter.wait_if_needed()
        # 只允许 75 过期；85/95 与本次(105)仍应在列表，绝不能清空后只剩 1 条
        assert len(limiter.request_timestamps) == 3, limiter.request_timestamps
        assert 75.0 not in limiter.request_timestamps
        assert 85.0 in limiter.request_timestamps and 95.0 in limiter.request_timestamps

    def test_hk_rate_limiter_keeps_in_window(self):
        import mutifactor.data.hk_fetcher as m
        self._check(m)

    def test_us_rate_limiter_keeps_in_window(self):
        import importlib.util
        from pathlib import Path
        # repo 根 = quant/（本文件在 quant/quant_futu-main/tests/unit/live/ 下）
        path = str(Path(__file__).resolve().parents[4]
                   / 'quant_us-main/mutifactor/data/futu_common.py')
        spec = importlib.util.spec_from_file_location('us_futu_common_test', path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self._check(m)


# ---------- 9) dual_chandelier：ATR 模式激活后止损线只升不降 ----------

class TestDualChandelierATRRatchet(unittest.TestCase):
    @staticmethod
    def _load():
        import importlib.util
        from pathlib import Path
        # repo 根 = quant/（本文件在 quant/quant_futu-main/tests/unit/live/ 下）
        path = str(Path(__file__).resolve().parents[4]
                   / 'quant_us-main/mutifactor/strategies/dual_chandelier.py')
        spec = importlib.util.spec_from_file_location('dual_chandelier_test', path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.PositionExitState

    def test_atr_mode_pullback_does_not_tighten(self):
        Pos = self._load()
        s = Pos(entry_price=100, direction='long', atr_trailing_mult=2.0)
        s.recompute(atr=10, current_price=130)      # +30% 激活 ATR，止损 110
        assert s._atr_mode_active and s.stop_line == 110.0
        s.recompute(atr=10, current_price=118)      # 回落到 +18%，不得退回固定%
        assert s.stop_line == 110.0
        hit, reason, _ = s.check_exit(118)
        assert not hit and reason == 'HOLD'

    def test_atr_mode_gap_keeps_ratcheted_stop(self):
        Pos = self._load()
        s = Pos(entry_price=100, direction='long', atr_trailing_mult=2.0)
        s.recompute(atr=10, current_price=130)
        s.recompute(atr=10, current_price=103)      # 跳空回落，不得把止损改回入场价
        assert s.stop_line == 110.0
        hit, reason, exit_p = s.check_exit(103)
        assert hit and reason == 'STOP_LOSS' and exit_p == 110.0

    def test_short_atr_mode_ratchet(self):
        Pos = self._load()
        s = Pos(entry_price=100, direction='short', atr_trailing_mult=2.0)
        s.recompute(atr=10, current_price=70)       # -30% 激活，止损 90
        assert s.stop_line == 90.0
        s.recompute(atr=10, current_price=82)       # 反弹，不得放松
        assert s.stop_line == 90.0
        hit, reason, _ = s.check_exit(82)
        assert not hit and reason == 'HOLD'

    def test_normal_phase2_3_still_work_before_atr(self):
        Pos = self._load()
        s = Pos(entry_price=100, direction='long')
        s.recompute(atr=10, current_price=105)
        assert s.stop_line == 100.0 and s._breakeven_moved
        s.recompute(atr=10, current_price=108)
        assert s._trailing_activated and s.stop_line > 100.0


# ---------- 10) ProposalStore 终态提案留存清理 ----------

class TestProposalPurge(unittest.TestCase):
    def test_terminal_purged_active_kept(self):
        from scripts.live_trading.approval.proposal_store import ProposalStore
        store = ProposalStore(ttl_seconds=3600)
        p1 = store.create(stock_code='HK.A', market_type='HK', side='buy')
        p2 = store.create(stock_code='HK.B', market_type='HK', side='buy')
        # p1 终态（rejected），p2 保持活跃
        assert store.reject(p1['id'])
        # 模拟 2 天后清理：终态且超过保留期 → 删除；活跃的一律保留
        removed = store.purge_terminal(now=p1['created_at'] + 172800,
                                       keep_seconds=86400)
        assert removed == 1
        assert store.get(p1['id']) is None
        assert store.get(p2['id']) is not None

    def test_terminal_within_retention_kept(self):
        from scripts.live_trading.approval.proposal_store import ProposalStore
        store = ProposalStore(ttl_seconds=3600)
        p1 = store.create(stock_code='HK.C', market_type='HK', side='buy')
        store.reject(p1['id'])
        removed = store.purge_terminal(now=p1['created_at'] + 3600,
                                       keep_seconds=86400)
        assert removed == 0
        assert store.get(p1['id']) is not None


# ---------- 11) 买入手续费必须从策略资金扣除 ----------

class TestBuyFeeDeducted(unittest.TestCase):
    def test_execute_buy_charges_fee_from_capital(self):
        pm = _make_pm(dealt=100)
        # 该 fake 卖出成交价 55；换成买入成交价 10 的假交易器
        class _BuyTrader:
            def place_order(self, stock_code, quantity, order_type, side, timeout,
                            partial_timeout=30):
                return ('B1', 10.0, quantity)
            def get_positions(self):
                return []
        pm.trader = _BuyTrader()
        pm.market_adapter.calculate_trading_cost.side_effect = (
            lambda **kw: {
                'total': 5.0 if kw.get('direction') == 'buy' else 0.0,
                'commission': 5.0, 'stamp_duty': 0.0,
            }
        )
        pm.strategy_capital = 100000.0
        pm.strategy_used_capital = 0.0
        pm.execute_buy([{'code': 'HK.F1', 'quantity': 100, 'price': 10.0}])
        assert 'HK.F1' in pm.strategy_positions
        assert abs(pm.strategy_used_capital - 1000.0) < 1e-6
        # 佣金等买入费用必须立刻从资本扣除，不能让账目虚增
        assert abs(pm.strategy_capital - 99995.0) < 1e-6, pm.strategy_capital


# ---------- 12) 选股结果按 env 覆盖，不互相抹掉 ----------

class TestSelectionResultsEnvScope(unittest.TestCase):
    def test_save_keeps_other_env_rows(self):
        import tempfile
        from pathlib import Path
        from mutifactor.infra.yaml_storage import YAMLStorage, TradingEnv
        d = tempfile.mkdtemp(prefix='selres_')
        s = YAMLStorage(data_dir=d)
        s.save_selection_results(
            [{'stock_code': 'HK.A', 'stock_name': 'A',
              'price': 10.0, 'in_position': False}],
            TradingEnv.SIMULATE,
        )
        s.save_selection_results(
            [{'stock_code': 'US.X', 'stock_name': 'X',
              'price': 20.0, 'in_position': True}],
            TradingEnv.REAL,
        )
        rows = s._load_table('selection_results', use_cache=False)
        envs = {r.get('env') for r in rows}
        assert envs == {TradingEnv.SIMULATE.value, TradingEnv.REAL.value}, rows
        # 覆盖 SIMULATE 为空不应清掉 REAL 行
        s.save_selection_results([], TradingEnv.SIMULATE)
        rows2 = s._load_table('selection_results', use_cache=False)
        assert [r for r in rows2 if r.get('env') == TradingEnv.REAL.value]
        assert not [r for r in rows2 if r.get('env') == TradingEnv.SIMULATE.value]

    def test_risk_only_config_still_works(self):
        cfg = {'time_exit': {'phase3_days': 350}, 'early_hard_stop_pct': 0.08}
        s = ExitStrategyFactory.create('atr_dynamic', cfg)
        assert s.phase3_days == 350
        assert s.early_hard_stop_pct == 0.08
