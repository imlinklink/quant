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
from typing import Dict, List, Optional, Set
import yaml
import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BASE_DIR)

logger = logging.getLogger(__name__)


class DipBuyMonitor:
    """抄底买入监控器"""
    
    def __init__(self, watch_codes: List[str], config: Dict, dry_run: bool = True):
        """
        Args:
            watch_codes: 监控股票列表 ['US.MU', 'US.AAPL', ...]
            config: 配置字典（来自 config.yaml）
            dry_run: 模拟模式（不实际下单）
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
        
        # 状态
        self._running = False
        self._stop_event = threading.Event()
        
        # 数据源
        self.pool = None
        self.analyzer = None
        
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
        
        # 分析器
        buy_timing_cfg = self.config.get('trading', {}).get('live_trading', {}).get('buy_timing', {})
        self.analyzer = IntradayAnalyzer(buy_timing_cfg)
        
        logger.info(f"✅ 初始化完成: 监控 {len(self.watch_codes)} 只股票")
        
    def _get_kline_5m(self, code: str) -> Optional[pd.DataFrame]:
        """拉取5分钟K线（含盘前盘后夜盘）
        
        注意：Futu 的 time_key 是美东时间（ET），需要用美东时间筛选今日数据
        """
        from futu import KLType, RET_OK, Session
        
        # 使用美东时间
        bj_now = datetime.now()
        et_now = bj_now - timedelta(hours=12)
        
        # 用美东时间计算日期范围
        et_date = et_now.strftime('%Y-%m-%d')
        et_start = et_now - timedelta(days=2)
        
        with self.pool.get_quote_ctx() as ctx:
            # request_history_kline 支持 extended_time + Session.ALL 获取全时段数据
            ret, data, _ = ctx.request_history_kline(
                code,
                start=et_start.strftime('%Y-%m-%d'),
                end=et_now.strftime('%Y-%m-%d'),
                ktype=KLType.K_5M,
                extended_time=True,
                session=Session.ALL  # 获取全时段（盘前+盘中+盘后+夜盘）
            )
            
            if ret == RET_OK and data is not None and len(data) > 0:
                # 使用滚动窗口：最近 N 根 K 线（不限当天，夜盘刚开时也能评分）
                data = data.sort_values('time_key').tail(self.min_bars * 2)  # 取足够多的数据
                if len(data) >= self.min_bars:
                    logger.info(f"  📊 {code} 最近{len(data)}根K线（含盘前盘后夜盘）")
                    return data.tail(self.min_bars)  # 返回最近 min_bars 根
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
    
    def _execute_buy(self, code: str, price: float, score: int) -> bool:
        """执行买入"""
        from futu import RET_OK, OrderType, TimeInForce, TrdSide, TrdEnv
        
        # 计算买入数量（根据单只仓位和当前价格）
        qty = int(self.position_size_usd / price)
        
        # 美股最小下单量为1股
        if qty <= 0:
            logger.warning(f"❌ 买入数量计算为0: {code} @ ${price:.2f} (仓位${self.position_size_usd})")
            return False
        
        actual_value = qty * price
        logger.info(f"🎯 触发买入: {code} x {qty}股 @ ${price:.2f} = ${actual_value:.2f} (评分={score})")
        
        if self.dry_run:
            logger.info(f"  [DRY-RUN] 模拟买入 {code} x {qty}股 @ ${price:.2f}")
            self.last_buy_time[code] = datetime.now()
            return True
        
        # 实盘买入
        try:
            with self.pool.get_trade_ctx() as ctx:
                ret, order = ctx.place_order(
                    price=0,  # 市价单
                    qty=qty,
                    code=code,
                    trd_side=TrdSide.BUY,
                    order_type=OrderType.MARKET,
                    trd_env=TrdEnv.REAL if self.config.get('live_manager', {}).get('trd_env') == 'REAL' else TrdEnv.SIMULATE,
                    time_in_force=TimeInForce.DAY,  # 当日有效，避免隔夜残留
                    fill_outside_rth=True,  # 允许盘前/盘后/夜盘成交（抄底监控设计上就是全时段）
                )
                
                if ret == RET_OK:
                    logger.info(f"✅ 买入成功: {code} x {qty} @ ${price:.2f}")
                    self.last_buy_time[code] = datetime.now()
                    return True
                else:
                    logger.error(f"❌ 买入失败: {order}")
                    return False
        except Exception as e:
            logger.error(f"买入异常 {code}: {e}")
            return False
    
    def _get_position_count(self) -> int:
        """查询当前持仓数（实盘用 position_list_query，dry-run 返回 0）"""
        if self.dry_run:
            return 0
        from futu import RET_OK, TrdEnv
        try:
            trd_env_str = self.config.get('live_manager', {}).get('trd_env', 'SIMULATE')
            trd_env = TrdEnv.SIMULATE if trd_env_str == 'SIMULATE' else TrdEnv.REAL
            with self.pool.get_trade_ctx() as ctx:
                ret, data = ctx.position_list_query(trd_env=trd_env)
                if ret != RET_OK:
                    logger.error(f"持仓查询失败，错误码: {ret}")
                    return 0
                # 只统计数量 > 0 的持仓
                return int((data['qty'] != 0).sum()) if data is not None and len(data) > 0 else 0
        except Exception as e:
            logger.warning(f"持仓查询异常: {e}")
            return 0

    def _check_one(self, code: str):
        """检查单只股票"""
        try:
            # 计算美东时间（用于日志显示时段）
            et_now = datetime.now().astimezone(ZoneInfo("America/New_York"))
            
            # 1. 检查冷却期
            if not self._check_cooldown(code):
                logger.info(f"⏭️  {code} 在冷却期内，跳过")
                return
            
            # 1.5 检查最大持仓数（避免无限加仓）
            pos_count = self._get_position_count()
            if pos_count >= self.max_positions:
                logger.info(f"⏭️  持仓已满({pos_count}/{self.max_positions})，跳过买入")
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
                self._execute_buy(code, price, result['score'])
                logger.info(f"  📊 {code} {result['details']}")
            else:
                logger.info(f"  ℹ️  {code} 评分={result['score']} 未达阈值({self.buy_threshold})")
        
        except Exception as e:
            logger.error(f"检查 {code} 异常: {e}")
    
    def _monitor_loop(self):
        """监控循环"""
        logger.info(f"🔍 开始监控，检查间隔 {self.check_interval}s")
        
        while self._running:
            try:
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
