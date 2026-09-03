#!/usr/bin/env python3
"""
每日次新股动量扫描脚本
扫描近150个交易日上市的次新股，计算动量和加速斜率，
只将动量最强的N只加入股票池。
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from futu import OpenQuoteContext, RET_OK, Market, KLType
from mutifactor.infra.yaml_storage import YAMLStorage


def calculate_momentum(prices: np.ndarray, window: int = 20) -> float:
    """计算动量：(现价 - N日前价) / N日前价 * 100"""
    if len(prices) < window:
        return -999.0
    return (prices[-1] - prices[-window]) / prices[-window] * 100

def calculate_momentum_acceleration(prices: np.ndarray, short_window: int = 5) -> float:
    """用指数加权最小二乘法计算动量加速斜率"""
    if len(prices) < short_window + 5:
        return -999.0
    
    n = len(prices)
    # 短期(5天)vs长期(20天)斜率对比
    x = np.arange(n)
    
    # 长期线性回归
    if len(prices) >= 20:
        x_long = np.arange(20)
        A_long = np.vstack([x_long, np.ones(20)]).T
        try:
            slope_long, _ = np.linalg.lstsq(A_long, prices[-20:], rcond=None)[0]
        except:
            slope_long = 0
    else:
        A = np.vstack([x, np.ones(n)]).T
        try:
            slope_long, _ = np.linalg.lstsq(A, prices, rcond=None)[0]
        except:
            slope_long = 0
    
    # 短期线性回归
    x_short = np.arange(short_window)
    A_short = np.vstack([x_short, np.ones(short_window)]).T
    try:
        slope_short, _ = np.linalg.lstsq(A_short, prices[-short_window:], rcond=None)[0]
    except:
        slope_short = 0
    
    # 加速斜率 = 短期斜率 - 长期斜率（都除以均价归一化）
    avg_price = np.mean(prices)
    if avg_price == 0:
        return -999.0
    
    return (slope_short - slope_long) / avg_price * 100


def calculate_rsi(prices: np.ndarray, period: int = 14) -> float:
    """计算 RSI（相对强弱指数）"""
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def _calc_rs_single(stock_prices: np.ndarray, hsi_prices: np.ndarray, rs_period: int = 20) -> float:
    """
    计算个股相对恒指的相对强弱
    
    返回：个股 rs_period 日收益 - 恒指 rs_period 日收益（超额收益，百分比）
    > 0 → 跑赢恒指
    < 0 → 跑输恒指
    """
    if len(stock_prices) < rs_period + 1 or len(hsi_prices) < rs_period + 1:
        return 0.0  # 数据不足，不惩罚
    s = stock_prices[-(rs_period + 1):]
    h = hsi_prices[-(rs_period + 1):]
    stock_ret = (s[-1] / s[0] - 1) * 100
    hsi_ret = (h[-1] / h[0] - 1) * 100
    return stock_ret - hsi_ret  # 超额收益（百分比）


def scan_new_ipos():
    """扫描次新股并用动量过滤"""
    quote_ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    db = YAMLStorage()
    
    SECTOR_MAP = {
        '银行': '金融业', '保险': '金融业', '其他金融': '金融业',
        '数码解决方案服务': '资讯科技业', '应用软件': '资讯科技业',
        '游戏软件': '资讯科技业', '互动媒体及服务': '资讯科技业',
        '互联网服务及基础设施': '资讯科技业', '半导体': '资讯科技业',
        '线上零售商': '非必需性消费', '汽车': '非必需性消费',
        '服装': '非必需性消费', '珠宝钟表': '非必需性消费',
        '影视娱乐': '非必需性消费', '玩具及消闲用品': '非必需性消费',
        '旅游及观光': '非必需性消费', '地产发展商': '地产建筑业',
        '地产投资': '地产建筑业', '重型基建': '地产建筑业',
        '生物技术': '医疗保健业', '药品': '医疗保健业',
        '药品分销': '医疗保健业', '铜': '原材料业', '铝': '原材料业',
        '黄金及贵金属': '原材料业', '其他金属及矿物': '原材料业',
        '油气生产商': '能源业', '煤炭': '能源业',
        '航运及港口': '工业', '轨道与列车设备': '工业',
        '公共运输': '工业', '常规电力': '公用事业',
        '包装食品': '必需性消费', '乳制品': '必需性消费',
        '非酒精饮料': '必需性消费', '酒精饮料': '必需性消费',
        '餐饮': '必需性消费', '其他零售商': '必需性消费',
        '综合企业': '综合企业', '能源储存装置': '公用事业',
        '电子零件': '原材料业', '半导体设备与材料': '资讯科技业',
    }
    
    MAX_NEW_STOCKS = 3  # 每次最多加3只
    RSI_ENTRY_CEILING = 65  # RSI 入场上限，超过65不追高
    RS_MIN = 0.0             # 相对强弱最低要求（超额收益>0，即跑赢恒指）
    
    try:
        # 获取所有港股基础信息
        print("[新股扫描] 获取港股列表...")
        ret, data = quote_ctx.get_stock_basicinfo(market=Market.HK, stock_type='STOCK')
        if ret != RET_OK:
            print(f"[错误] 获取港股列表失败: {data}")
            return
        
        # 【新】获取恒生指数历史数据用于相对强弱计算
        # 注意：富途恒指K线起始日期需要和个股K线对齐，取最近60条最新数据
        hsi_prices = None
        ret_hsi, hsi_kline, _ = quote_ctx.request_history_kline(
            'HK.800000', start='', end='', max_count=60, ktype=KLType.K_DAY
        )
        if ret_hsi == RET_OK and hsi_kline is not None and not hsi_kline.empty:
            hsi_prices = hsi_kline['close'].values.astype(float)
            print(f"[新股扫描] 恒生指数数据: {len(hsi_prices)}条 ({hsi_kline['time_key'].iloc[-1]} ~ {hsi_kline['time_key'].iloc[0]})")
        else:
            print(f"[警告] 恒生指数K线获取失败: {hsi_kline}")
        
        existing_codes = set(db.get_all_stock_codes(market='HK', enabled_only=False))
        
        # 过滤近90天日历上市的次新股
        cutoff_date = datetime.now() - timedelta(days=90)
        candidates = []
        
        for _, row in data.iterrows():
            code = row['code']
            if code in existing_codes:
                continue
            
            listing_date_str = row.get('listing_date')
            if listing_date_str and listing_date_str != 'N/A':
                try:
                    listing_date = pd.to_datetime(listing_date_str).date()
                    if listing_date >= cutoff_date.date() and listing_date <= datetime.now().date():
                        candidates.append({
                            'code': code,
                            'name': row['name'],
                            'lot_size': row.get('lot_size', 100),
                            'listing_date': listing_date
                        })
                except:
                    pass
        
        print(f"[新股扫描] 候选次新股: {len(candidates)} 只")
        
        # 获取K线和动量数据
        stocks_with_momentum = []
        
        for i, c in enumerate(candidates):
            code = c['code']
            print(f"[{i+1}/{len(candidates)}] 扫描 {code} {c['name']}...", end=" ")
            
            # 先查数据库，已有数据直接用，避免浪费富途API额度
            df_db = db.get_kline_data(code, None, None, 'DAY')
            if df_db is not None and not df_db.empty:
                kline = df_db.copy()
                kline.rename(columns={'date': 'time_key'}, inplace=True)
                print(f"[DB已有 {len(kline)}条]", end=" ")
            else:
                # 数据库没有才请求富途API（注意：ret=-1 时错误信息在 kline 字符串里！）
                ret, kline, _ = quote_ctx.request_history_kline(
                    code=code, start='', end='', ktype=KLType.K_DAY, max_count=100
                )
                if ret != RET_OK or kline is None or kline.empty:
                    # 富途错误信息可能藏在 kline 字符串里
                    err_msg = kline if isinstance(kline, str) else '获取失败'
                    print(f"K线获取失败: {err_msg}")
                    time.sleep(0.1)
                    continue
            
            trading_days = len(kline)
            if trading_days > 150:
                print(f"已超过150交易日({trading_days})")
                continue
            
            prices = kline['close'].values.astype(float)
            
            # 计算动量
            mom = calculate_momentum(prices, window=20)
            accel = calculate_momentum_acceleration(prices)
            
            # 计算 RSI
            rsi = calculate_rsi(prices, period=14)
            
            # 计算相对强弱 vs 恒生指数
            if hsi_prices is not None:
                rs = _calc_rs_single(prices, hsi_prices, rs_period=20)
            else:
                rs = 1.0
            
            # RSI 超买过滤：RSI > 65 不追高
            if mom > 0 and rsi > RSI_ENTRY_CEILING:
                print(f"动量={mom:.2f}, 加速={accel:.4f}, RSI={rsi:.1f}>65(超买) {trading_days}天")
                time.sleep(0.1)
                continue
            
            # 动量非正过滤
            if mom <= 0:
                print(f"动量={mom:.2f}, 加速={accel:.4f}, RSI={rsi:.1f}, RS={rs:.3f} {trading_days}天")
                time.sleep(0.1)
                continue
            
            # 获取行业
            ret2, plates_data = quote_ctx.get_owner_plate([code])
            sector = '综合企业'
            if ret2 == RET_OK and not plates_data.empty:
                industry_row = plates_data[plates_data['plate_type'] == 'INDUSTRY']
                if not industry_row.empty:
                    sector = SECTOR_MAP.get(industry_row.iloc[0]['plate_name'], '综合企业')
            
            stocks_with_momentum.append({
                'code': code,
                'name': c['name'],
                'lot_size': c['lot_size'],
                'listing_date': c['listing_date'],
                'sector': sector,
                'trading_days': trading_days,
                'momentum': mom,
                'accel': accel,
                'rsi': rsi,
                'rs': rs
            })
            
            print(f"动量={mom:.2f}, 加速={accel:.4f}, RSI={rsi:.1f}, RS={rs:.3f} {trading_days}天")
            time.sleep(0.1)
        
        if not stocks_with_momentum:
            print("[新股扫描] 没有符合条件的次新股")
            return
        
        # 按动量排序，取最强的
        stocks_with_momentum.sort(key=lambda x: (x['momentum'], x['accel']), reverse=True)
        
        top_stocks = stocks_with_momentum[:MAX_NEW_STOCKS]
        
        print(f"\n=== 动量最强的 {len(top_stocks)} 只新股 ===")
        for rank, s in enumerate(top_stocks, 1):
            print(f"{rank}. {s['code']} {s['name']} ({s['sector']}) "
                  f"动量={s['momentum']:.2f} 加速={s['accel']:.4f} {s['trading_days']}天")
        
        # 入库
        print("\n=== 入库 ===")
        for s in top_stocks:
            try:
                with db._get_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("""
                            INSERT INTO stock_info (stock_code, stock_name, lot_size, market, sector, listing_date)
                            VALUES (%s, %s, %s, 'HK', %s, %s)
                            ON DUPLICATE KEY UPDATE 
                                stock_name = VALUES(stock_name),
                                lot_size = VALUES(lot_size),
                                sector = VALUES(sector),
                                listing_date = VALUES(listing_date)
                        """, (s['code'], s['name'], s['lot_size'], s['sector'], s['listing_date']))
                print(f"入库: {s['code']} {s['name']}")
            except Exception as e:
                print(f"❌ 入库失败 {s['code']}: {type(e).__name__}: {e}")
        
        print(f"\n✅ 入库完成，共 {len(top_stocks)} 只动量新股")
        
        # 打印完整排名
        print("\n=== 全部候选新股动量排名 ===")
        for rank, s in enumerate(stocks_with_momentum, 1):
            marker = " ✅已入库" if rank <= MAX_NEW_STOCKS else ""
            print(f"{rank:2d}. {s['code']} {s['name']:<10} 动量={s['momentum']:7.2f} 加速={s['accel']:+.4f}{marker}")
        
    finally:
        quote_ctx.close()


if __name__ == '__main__':
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始次新股动量扫描")
    scan_new_ipos()
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 扫描完成")
