"""
YAML文件存储模块 - 替代MySQL数据库
使用YAML文件存储数据,便于版本控制和移植
"""
import os
import yaml
from yaml import FullLoader as YamlLoader, SafeLoader
from yaml.constructor import SafeConstructor
import json
from decimal import Decimal
from datetime import timedelta, datetime as dt_datetime
import logging
from typing import Optional, List, Dict
from datetime import datetime, date
import threading
from enum import Enum

logger = logging.getLogger(__name__)


# ============ 兼容Python对象序列化的YAML Loader ============
# 原有YAML文件使用了 !!python/object/apply:decimal.Decimal 等格式
# SafeLoader 无法解析,需要自定义 Loader


class CompatibleSafeLoader(SafeLoader):
    """兼容Python对象序列化的SafeLoader"""
    pass


def _construct_python_apply(loader, node):
    """构造 !!python/object/apply:xxx 格式的Python对象"""
    import importlib

    # 获取 tag: "tag:yaml.org,2002:python/object/apply:decimal.Decimal"
    tag = str(node.tag)
    prefix = 'tag:yaml.org,2002:python/object/apply:'
    class_path = tag[len(prefix):] if tag.startswith(prefix) else tag

    # 获取参数列表
    if isinstance(node, yaml.SequenceNode):
        args = loader.construct_sequence(node, deep=True)
    else:
        args = [loader.construct_object(node, deep=True)]

    # 动态构造对象
    if '.' in class_path:
        parts = class_path.rsplit('.', 1)
        module_name, class_name = parts[0], parts[1]
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, class_name)
            return cls(*args)
        except (ImportError, AttributeError):
            pass

    # 直接从 globals 查找
    return None


# 注册构造器
CompatibleSafeLoader.add_constructor('tag:yaml.org,2002:python/object/apply:decimal.Decimal', _construct_python_apply)
CompatibleSafeLoader.add_constructor('tag:yaml.org,2002:python/object/apply:datetime.timedelta', _construct_python_apply)
CompatibleSafeLoader.add_constructor('tag:yaml.org,2002:python/object/apply:datetime.datetime', _construct_python_apply)

class TradingEnv(Enum):
    """交易环境"""
    SIMULATE = "SIMULATE"  # 模拟仓
    REAL = "REAL"          # 实仓


def _construct_trading_env(loader, node):
    """解析 TradingEnv 标签"""
    from mutifactor.infra.yaml_storage import TradingEnv
    try:
        # node.value 可能是列表 [scalar] 或直接 scalar
        if isinstance(node.value, list):
            value = node.value[0].value if node.value else ''
        else:
            value = node.value
        return TradingEnv(value)
    except:
        return None

CompatibleSafeLoader.add_constructor('tag:yaml.org,2002:python/object/apply:mutifactor.infra.yaml_storage.TradingEnv', _construct_trading_env)


class YAMLStorage:
    """YAML文件存储类"""

    def __init__(self, data_dir: str = 'data'):
        self.data_dir = data_dir
        self._cache = {}  # 内存缓存 {table_name: data}
        self._cache_time = {}  # 缓存时间戳
        self._cache_ttl = 300  # 缓存5分钟
        self._lock = threading.Lock()  # 线程锁(读写YAML文件)

        # 确保数据目录存在
        os.makedirs(data_dir, exist_ok=True)

    def _get_filepath(self, table_name: str) -> str:
        """获取表对应的YAML文件路径"""
        return os.path.join(self.data_dir, f"{table_name}.yaml")

    def _load_table(self, table_name: str, use_cache: bool = True) -> List[Dict]:
        """加载表数据(带缓存)

        Args:
            table_name: 表名
            use_cache: 是否使用缓存
        """
        filepath = self._get_filepath(table_name)

        with self._lock:
            # 检查缓存
            if use_cache and table_name in self._cache:
                cache_time = self._cache_time.get(table_name, 0)
                if datetime.now().timestamp() - cache_time < self._cache_ttl:
                    return self._cache[table_name]

            # 从文件加载
            if not os.path.exists(filepath):
                logger.warning(f"YAML文件不存在: {filepath}")
                return []

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = yaml.load(f, Loader=CompatibleSafeLoader)
                    if data is None:
                        return []

                    # YAML格式: {table_name: [row1, row2, ...]}
                    table_data = data.get(table_name, [])

                    # 更新缓存
                    self._cache[table_name] = table_data
                    self._cache_time[table_name] = datetime.now().timestamp()

                    logger.info(f"✅ 已从 {filepath} 加载 {len(table_data)} 条记录")
                    return table_data
            except Exception as e:
                logger.error(f"❌ 加载YAML文件失败 {filepath}: {e}")
                return []

    def _save_table(self, table_name: str, data: List[Dict]):
        """保存表数据到YAML文件"""
        filepath = self._get_filepath(table_name)

        with self._lock:
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    yaml.dump(
                        {table_name: data},
                        f,
                        allow_unicode=True,
                        default_flow_style=False,
                        sort_keys=False
                    )

                # 更新缓存
                self._cache[table_name] = data
                self._cache_time[table_name] = datetime.now().timestamp()

                logger.info(f"✅ 已保存 {len(data)} 条记录到 {filepath}")
            except Exception as e:
                logger.error(f"❌ 保存YAML文件失败 {filepath}: {e}")
                raise

    def get_all_stock_codes(self, market: str = None) -> List[str]:
        """获取所有股票代码

        Args:
            market: 市场过滤('HK'/'SH'/'SZ'),None表示全部

        Returns:
            股票代码列表
        """
        data = self._load_table('stock_info')

        if market:
            data = [row for row in data if row.get('market') == market]

        # 只返回 enabled=1 的股票
        data = [row for row in data if row.get('enabled', 1) == 1]

        codes = [row.get('stock_code') for row in data]
        logger.info(f"✅ 获取到 {len(codes)} 只股票代码 (market={market})")
        return codes

    def get_stock_name(self, stock_code: str) -> str:
        """获取股票名称

        Args:
            stock_code: 股票代码

        Returns:
            股票名称,如果找不到返回股票代码本身
        """
        stock_info = self.get_stock_info(stock_code)
        if stock_info:
            return stock_info.get('stock_name', stock_code)
        return stock_code

    def get_stock_info(self, stock_code: str) -> Optional[Dict]:
        """获取股票信息

        Args:
            stock_code: 股票代码(如 HK.00700)
        """
        data = self._load_table('stock_info')
        for row in data:
            if row.get('stock_code') == stock_code:
                return row
        return None

    def get_all_stock_info(self, market: str = None) -> Dict[str, Dict]:
        """获取所有股票信息

        Args:
            market: 市场过滤('HK'/'SH'/'SZ'),None表示全部

        Returns:
            {股票代码: 股票信息字典}
        """
        data = self._load_table('stock_info')

        if market:
            data = [row for row in data if row.get('market') == market]

        # 转换为字典:{stock_code: row_dict}
        result = {}
        for row in data:
            code = row.get('stock_code')
            if code:
                result[code] = row

        logger.info(f"✅ 获取到 {len(result)} 只股票信息")
        return result

    def save_stock_info(self, stock_code: str, name: str, market: str,
                       lot_size: int = 100, listing_date: str = None,
                       sector: str = None, enabled: int = 1):
        """保存股票信息(不存在则插入,存在则更新)"""
        data = self._load_table('stock_info', use_cache=False)

        # 查找是否已存在
        for i, row in enumerate(data):
            if row.get('stock_code') == stock_code:
                # 更新
                data[i] = {
                    'stock_code': stock_code,
                    'stock_name': name,
                    'lot_size': lot_size,
                    'market': market,
                    'listing_date': listing_date,
                    'sector': sector,
                    'enabled': enabled,
                    'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                self._save_table('stock_info', data)
                logger.info(f"✅ 已更新股票信息: {stock_code}")
                return

        # 不存在,插入
        new_row = {
            'id': len(data) + 1,
            'stock_code': stock_code,
            'stock_name': name,
            'lot_size': lot_size,
            'market': market,
            'listing_date': listing_date,
            'sector': sector,
            'enabled': enabled,
            'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        data.append(new_row)
        self._save_table('stock_info', data)
        logger.info(f"✅ 已插入股票信息: {stock_code}")

    def get_lot_size(self, stock_code: str) -> int:
        """获取每手股数

        Args:
            stock_code: 股票代码
        """
        # 先从stock_info获取
        stock_info = self.get_stock_info(stock_code)
        if stock_info:
            return stock_info.get('lot_size', 100)

        # A股统一每手100股，直接返回
        if stock_code.startswith('SH.') or stock_code.startswith('SZ.'):
            return 100

        # 如果找不到,从富途API获取(需要调用者自己实现)
        logger.warning(f"⚠️  未找到股票 {stock_code} 的lot_size,返回默认值100")
        return 100

    def get_stock_sector(self, stock_code: str) -> str:
        """获取股票行业"""
        stock_info = self.get_stock_info(stock_code)
        if stock_info:
            return stock_info.get('sector', '')
        return ''

    def get_listing_date(self, stock_code: str) -> str:
        """获取上市日期"""
        stock_info = self.get_stock_info(stock_code)
        if stock_info:
            return stock_info.get('listing_date', '')
        return ''

    def save_position(self, stock_code: str, market: str = None, position_side: str = None,
                     qty: int = None, avg_price: float = None, init_qty: int = None,
                     init_cost: float = None, strategy: str = None,
                     strategy_instance: str = None, env: TradingEnv = None,
                     # state_persistence.py 参数名
                     stock_name: str = None, quantity: int = None,
                     cost_price: float = None, highest_price: float = None,
                     manual: bool = False):
        """保存持仓记录

        支持两种调用方式:
        1. 原生API: market + position_side + qty + avg_price
        2. state_persistence API: stock_name + quantity + cost_price + highest_price
        """
        # 兼容 state_persistence.py 参数名
        if qty is None and quantity is not None:
            qty = quantity
        if avg_price is None and cost_price is not None:
            avg_price = cost_price
        if market is None:
            market = 'HK'  # 默认港股
        if position_side is None:
            position_side = 'LONG'

        data = self._load_table('positions', use_cache=False)

        # 检查是否已存在
        for i, row in enumerate(data):
            if row.get('stock_code') == stock_code and row.get('position_side') == position_side:
                # 更新
                data[i].update({
                    'qty': qty,
                    'avg_price': avg_price,
                    'highest_price': highest_price,
                    'manual': manual,
                    'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })
                self._save_table('positions', data)
                logger.info(f"✅ 已更新持仓: {stock_code} {position_side}")
                return

        # 插入新持仓
        new_row = {
            'id': len(data) + 1,
            'stock_code': stock_code,
            'stock_name': stock_name,
            'market': market,
            'position_side': position_side,
            'qty': qty,
            'avg_price': avg_price,
            'highest_price': highest_price,
            'init_qty': init_qty or qty,
            'init_cost': init_cost or (qty * avg_price if qty and avg_price else 0),
            'strategy': strategy,
            'strategy_instance': strategy_instance,
            'manual': manual,
            'env': env.value if env else 'REAL',
            'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        data.append(new_row)
        self._save_table('positions', data)
        logger.info(f"✅ 已插入持仓: {stock_code} {position_side}")

    def get_positions(self, market: str = None, env: TradingEnv = None) -> List[Dict]:
        """获取持仓列表

        Args:
            market: 市场过滤('HK'/'SH'/'SZ')
            env: 交易环境过滤(REAL/SIMULATE)
        """
        data = self._load_table('positions')
        if market:
            data = [row for row in data if row.get('market') == market]
        if env:
            data = [row for row in data if row.get('env') == env.value]
        return data

    def clear_positions(self, env: TradingEnv = None):
        """清空持仓记录

        Args:
            env: 交易环境过滤,为None时清空所有
        """
        data = self._load_table('positions', use_cache=False)
        if env:
            data = [row for row in data if row.get('env') != env.value]
            self._save_table('positions', data)
            logger.info(f"✅ 已清空持仓 (env={env.value})")
        else:
            self._save_table('positions', [])
            logger.info("✅ 已清空所有持仓")

    def get_today_bought_stocks(self, env: TradingEnv) -> set:
        """获取今日已买入的股票代码集合

        Args:
            env: 交易环境(REAL/SIMULATE)

        Returns:
            股票代码集合
        """
        from datetime import date
        today = date.today().strftime('%Y-%m-%d')

        trades = self.get_trades(env=env)
        bought = set()

        for trade in trades:
            trade_time = trade.get('trade_time', '')
            if trade_time.startswith(today) and trade.get('trade_type', '').upper() in ('BUY', 'BUY'):
                bought.add(trade.get('stock_code'))

        logger.info(f"✅ 今日已买入 {len(bought)} 只股票 (env={env.value})")
        return bought

    def delete_position(self, stock_code: str, position_side: str = None):
        """删除持仓记录"""
        data = self._load_table('positions', use_cache=False)

        if position_side:
            # 删除指定方向的持仓
            data = [row for row in data
                    if not (row.get('stock_code') == stock_code
                            and row.get('position_side') == position_side)]
        else:
            # 删除所有该股票的持仓
            data = [row for row in data if row.get('stock_code') != stock_code]

        self._save_table('positions', data)
        logger.info(f"✅ 已删除持仓: {stock_code} {position_side or '(所有方向)'}")

    def save_trade(self, stock_code: str, market: str = None, trade_type: str = None,
                   position_side: str = None, qty: int = None, price: float = None,
                   amount: float = None, commission: float = 0, stamp_tax: float = 0,
                   strategy: str = None, strategy_instance: str = None,
                   pnl: float = None, pnl_pct: float = None,
                   trade_time: str = None, env: TradingEnv = None,
                   # state_persistence.py 传入的参数名
                   stock_name: str = None, quantity: int = None,
                   direction: str = None, order_id: str = None):
        """保存交易记录

        支持两种调用方式:
        1. 原生API: trade_type + position_side + qty + amount
        2. state_persistence API: direction + quantity + stock_name + order_id
        """
        data = self._load_table('trades', use_cache=False)

        # 兼容 state_persistence.py 的参数名
        if qty is None and quantity is not None:
            qty = quantity
        if trade_type is None and direction is not None:
            trade_type = direction.upper() if direction else None
        if position_side is None:
            position_side = 'LONG'  # 默认做多
        if amount is None and qty is not None and price is not None:
            amount = qty * price
        if trade_time is None:
            trade_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        elif isinstance(trade_time, datetime):
            trade_time = trade_time.strftime('%Y-%m-%d %H:%M:%S')

        new_row = {
            'id': len(data) + 1,
            'stock_code': stock_code,
            'market': market or '',
            'trade_type': trade_type or direction,
            'position_side': position_side,
            'qty': qty,
            'price': price,
            'amount': amount,
            'commission': commission,
            'stamp_tax': stamp_tax,
            'strategy': strategy,
            'strategy_instance': strategy_instance,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'trade_time': trade_time,
            'env': env.value if env else '',
            'stock_name': stock_name,
            'order_id': order_id,
            'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        data.append(new_row)
        self._save_table('trades', data)
        logger.info(f"✅ 已保存交易记录: {stock_code} {trade_type or direction} {qty}股")

    def get_trades(self, stock_code: str = None, start_date: str = None,
                   end_date: str = None, env: TradingEnv = None) -> List[Dict]:
        """获取交易记录

        Args:
            stock_code: 股票代码过滤
            start_date: 开始日期过滤
            end_date: 结束日期过滤
            env: 交易环境过滤(REAL/SIMULATE)
        """
        data = self._load_table('trades')

        if stock_code:
            data = [row for row in data if row.get('stock_code') == stock_code]

        if start_date:
            data = [row for row in data
                    if row.get('trade_time', '') >= start_date]

        if end_date:
            data = [row for row in data
                    if row.get('trade_time', '') <= end_date]

        if env:
            data = [row for row in data if row.get('env') == env.value]

        return data

    def save_backtest_record(self, strategy_name: str, start_date: str,
                           end_date: str, initial_capital: float,
                           final_capital: float, total_return: float,
                           annual_return: float, max_drawdown: float,
                           sharpe_ratio: float, win_rate: float,
                           trade_count: int, config: Dict = None,
                           details: Dict = None):
        """保存回测记录"""
        data = self._load_table('backtest_records', use_cache=False)

        new_row = {
            'id': len(data) + 1,
            'strategy_name': strategy_name,
            'start_date': start_date,
            'end_date': end_date,
            'initial_capital': initial_capital,
            'final_capital': final_capital,
            'total_return': total_return,
            'annual_return': annual_return,
            'max_drawdown': max_drawdown,
            'sharpe_ratio': sharpe_ratio,
            'win_rate': win_rate,
            'trade_count': trade_count,
            'config': json.dumps(config, ensure_ascii=False) if config else None,
            'details': json.dumps(details, ensure_ascii=False) if details else None,
            'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        data.append(new_row)
        self._save_table('backtest_records', data)
        logger.info(f"✅ 已保存回测记录: {strategy_name} 收益率{total_return:.2%}")

    def get_backtest_records(self, strategy_name: str = None,
                           limit: int = 10) -> List[Dict]:
        """获取回测记录"""
        data = self._load_table('backtest_records')

        if strategy_name:
            data = [row for row in data if row.get('strategy_name') == strategy_name]

        # 按创建时间倒序
        data = sorted(data, key=lambda x: x.get('create_time', ''), reverse=True)

        return data[:limit]

    def clear_cache(self):
        """清空缓存"""
        with self._lock:
            self._cache.clear()
            self._cache_time.clear()
        logger.info("✅ 已清空缓存")

    # ========================
    # 选股结果管理
    # ========================

    def get_selection_results(self, env: TradingEnv, limit: int = 100) -> List[Dict]:
        """获取选股结果

        Args:
            env: 交易环境(REAL/SIMULATE)
            limit: 返回数量限制

        Returns:
            选股结果列表 [{stock_code, stock_name, price, selection_date, selection_time, ...}, ...]
        """
        data = self._load_table('selection_results', use_cache=False)

        # 按环境过滤
        data = [row for row in data if row.get('env') == env.value]

        # 按日期时间倒序
        data = sorted(data, key=lambda x: (x.get('selection_date', ''), x.get('selection_time', '')), reverse=True)

        logger.info(f"✅ 获取到 {len(data)} 条选股结果 (env={env.value})")
        return data[:limit]

    def save_selection_results(self, stock_details: List[Dict], env: TradingEnv):
        """保存选股结果（覆盖模式：只保留当日最新结果）"""
        from datetime import date

        today = date.today()
        now = datetime.now()

        data = []
        for stock in stock_details:
            data.append({
                'id': len(data) + 1,
                'stock_code': stock.get('stock_code'),
                'stock_name': stock.get('stock_name'),
                'price': stock.get('price'),
                'momentum': stock.get('momentum'),
                'rsrs': stock.get('rsrs'),
                'score': stock.get('score'),
                'sector': stock.get('sector'),
                'in_position': stock.get('in_position', False),
                'selection_date': today.strftime('%Y-%m-%d'),
                'selection_time': now.strftime('%H:%M:%S'),
                'env': env.value,
                'create_time': now.strftime('%Y-%m-%d %H:%M:%S')
            })

        self._save_table('selection_results', data)
        logger.info(f"✅ 已保存 {len(stock_details)} 条选股结果 (env={env.value}, 覆盖模式)")

    # ========================
    # 交易状态管理
    # ========================

    def load_trading_state(self, env: TradingEnv) -> Optional[Dict]:
        """加载交易状态

        Args:
            env: 交易环境(REAL/SIMULATE)

        Returns:
            交易状态字典 {
                'positions': {stock_code: {qty, avg_price, ...}},
                'used_capital': float,
                'capital': float,
                'last_buy_execution': timestamp
            }
        """
        data = self._load_table('trading_state', use_cache=False)

        # 查找对应环境的状态
        for row in data:
            if row.get('env') == env.value:
                logger.info(f"✅ 已加载交易状态 (env={env.value}, positions={len(row.get('positions', {}))})")
                return row

        logger.warning(f"⚠️ 未找到交易状态,返回空状态 (env={env.value})")
        return {
            'positions': {},
            'used_capital': 0.0,
            'capital': 0.0,
            'last_buy_execution': 0,
            'env': env.value
        }

    def save_trading_state(self, positions: Dict, used_capital: float,
                           capital: float, last_buy_execution: int,
                           env: TradingEnv):
        """保存交易状态

        Args:
            positions: 持仓字典 {stock_code: {qty, avg_price, ...}}
            used_capital: 已用资金
            capital: 总资金
            last_buy_execution: 最后买入时间戳
            env: 交易环境(REAL/SIMULATE)
        """
        data = self._load_table('trading_state', use_cache=False)

        # 查找是否已存在该环境的状态
        found = False
        for i, row in enumerate(data):
            if row.get('env') == env.value:
                data[i] = {
                    'env': env.value,
                    'positions': positions,
                    'used_capital': used_capital,
                    'capital': capital,
                    'last_buy_execution': last_buy_execution,
                    'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                }
                found = True
                break

        if not found:
            new_row = {
                'id': len(data) + 1,
                'env': env.value,
                'positions': positions,
                'used_capital': used_capital,
                'capital': capital,
                'last_buy_execution': last_buy_execution,
                'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            data.append(new_row)

        self._save_table('trading_state', data)
        logger.info(f"✅ 已保存交易状态 (env={env.value}, positions={len(positions)}, used={used_capital})")

    def get_today_buy_quantity(self, env: TradingEnv) -> int:
        """获取今日已买入股数

        Args:
            env: 交易环境(REAL/SIMULATE)

        Returns:
            今日买入股数
        """
        from datetime import date
        today = date.today().strftime('%Y-%m-%d')

        trades = self.get_trades()
        total_qty = 0

        for trade in trades:
            if trade.get('env') == env.value:
                trade_time = trade.get('trade_time', '')
                if trade_time.startswith(today) and trade.get('trade_type') in ('BUY', 'buy'):
                    total_qty += trade.get('qty', 0)

        logger.info(f"✅ 今日买入股数: {total_qty} (env={env.value})")
        return total_qty

    # ========================
    # 资金记录管理
    # ========================

    def save_capital_record(self, total_capital: float, used_capital: float, env: TradingEnv):
        """保存资金记录（同一交易日覆盖，不重复追加）"""
        data = self._load_table('capital_records', use_cache=False)
        today = date.today().strftime('%Y-%m-%d')

        # 查找今日是否已有记录，有则覆盖
        updated = False
        for row in data:
            if row.get('record_date') == today and row.get('env') == env.value:
                row.update({
                    'total_capital': total_capital,
                    'used_capital': used_capital,
                    'free_capital': total_capital - used_capital,
                    'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                })
                updated = True
                break

        # 无今日记录则新增
        if not updated:
            new_row = {
                'id': len(data) + 1,
                'total_capital': total_capital,
                'used_capital': used_capital,
                'free_capital': total_capital - used_capital,
                'env': env.value,
                'record_date': today,
                'create_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            data.append(new_row)

        self._save_table('capital_records', data)
        logger.info(f"✅ 已保存资金记录 (env={env.value}, total={total_capital}, used={used_capital})")

    # ========================
    # 上市日期(补充方法)
    # ========================

    def get_all_listing_dates(self, market: str = None) -> Dict[str, date]:
        """获取所有股票的上市日期

        Args:
            market: 市场过滤('HK'/'SH'/'SZ'),None表示全部

        Returns:
            {股票代码: 上市日期} 字典
        """
        data = self._load_table('stock_info')

        if market:
            data = [row for row in data if row.get('market') == market]

        result = {}
        for row in data:
            code = row.get('stock_code')
            listing_date_str = row.get('listing_date', '')
            if code and listing_date_str:
                try:
                    listing_date = datetime.strptime(listing_date_str, '%Y-%m-%d').date()
                    result[code] = listing_date
                except:
                    pass

        logger.info(f"✅ 获取到 {len(result)} 只股票的上市日期")
        return result


# 全局单例
_yaml_storage = None

def get_yaml_storage() -> YAMLStorage:
    """获取YAML存储单例"""
    global _yaml_storage
    if _yaml_storage is None:
        _yaml_storage = YAMLStorage()
    return _yaml_storage


# 兼容旧代码(db_storage 单例)
yaml_storage = get_yaml_storage()
