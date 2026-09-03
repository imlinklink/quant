#!/usr/bin/env python3
"""
港股动量分析脚本
用法:
    python3 scripts/analysis/momentum_analysis.py --codes HK.06181
    python3 scripts/analysis/momentum_analysis.py --codes HK.06181 HK.02513 --days 120
    python3 scripts/analysis/momentum_analysis.py --top-gainers 20 --days 180
    python3 scripts/analysis/momentum_analysis.py --search 黄金 --days 180

输出:
    - 终端彩色表格（每日动量分）
    - 可选保存 CSV 到 output/momentum_analysis/
"""

import argparse
import os
import sys
import unicodedata
import yaml


import re

_ansi_re = re.compile(r'\x1b\[[0-9;]*m')


def _strip_ansi(s: str) -> str:
    """去掉 ANSI 颜色码，只留纯文本。"""
    return _ansi_re.sub('', s)


def display_width(s: str) -> int:
    """返回字符串在终端的显示宽度（中文字符算2，其他算1，ANSI码不计）。"""
    return sum(2 if unicodedata.east_asian_width(c) in ('F', 'W') else 1
               for c in _strip_ansi(str(s)))


def ljust_d(s: str, width: int) -> str:
    """左对齐，智能处理中文宽度（等宽列宽用 display_width 补足）。"""
    return str(s) + ' ' * (width - display_width(s))


def rjust_d(s: str, width: int) -> str:
    """右对齐，智能处理中文宽度。"""
    return ' ' * (width - display_width(s)) + str(s)

import numpy as np
import pandas as pd
import pymysql

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    # 修复中文字体
    plt.rcParams['font.sans-serif'] = ['Heiti SC', 'PingFang SC', 'STHeiti', 'Arial Unicode MS', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False
    HAS_PLOT = True
except ImportError:
    HAS_PLOT = False

# ==================== 配置 ====================
DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'database': 'quant',
    'charset': 'utf8mb4',
}

# 动量计算参数（与 momentum.py 保持一致）
# 优先读 config.yaml，缺失时兜底硬编码默认值（保证独立运行也能工作）
_config_cache = None

def _load_config():
    global _config_cache
    if _config_cache is None:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            'config.yaml'
        )
        with open(config_path, encoding='utf-8') as f:
            _config_cache = yaml.safe_load(f)
    return _config_cache

def _get_int(name: str, default: int) -> int:
    parts = name.split('.')
    cfg = _load_config()
    for p in parts:
        cfg = cfg.get(p, default)
        if not isinstance(cfg, dict):
            return int(cfg) if cfg is not None else default
    return default

def _get_float(name: str, default: float) -> float:
    parts = name.split('.')
    cfg = _load_config()
    for p in parts:
        cfg = cfg.get(p, default)
        if not isinstance(cfg, dict):
            return float(cfg) if cfg is not None else default
    return default

# 从 config.yaml 读取
MOMENTUM_WINDOW = _get_int('momentum.momentum_window', 30)
RSRS_WINDOW      = _get_int('momentum.rsrs_window', 5)
MIN_DATA_RATIO   = _get_float('risk.min_data_ratio', 0.7)
MAX_POSITIONS    = _get_int('momentum.max_positions', 5)
RSRS_EARLY_EXEMPT_DAYS = _get_int('risk.rsrs_warn.early_exempt_days', 15)
EARLY_HARD_STOP_PCT    = _get_float('risk.early_hard_stop_pct', 0.12)

# 线性/指数权重（暂无config，用默认值）
LINEAR_WEIGHT_START = 1.0
LINEAR_WEIGHT_END   = 2.0
EXP_WEIGHT_START    = 1.0
EXP_WEIGHT_END      = 2.718  # e

# ==================== 数据库操作 ====================

def get_db_connection():
    return pymysql.connect(**DB_CONFIG)


def load_stock_info():
    """加载股票基本信息"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT stock_code, stock_name, listing_date
        FROM stock_info WHERE market = 'HK' AND listing_date IS NOT NULL
    """)
    result = {row[0]: {'name': row[1], 'listing_date': row[2]} for row in cursor.fetchall()}
    cursor.close()
    conn.close()
    return result


def get_stock_klines(stock_code: str, days: int = 120) -> pd.DataFrame:
    """从数据库拉取日线数据"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT kline_date, open, high, low, close, volume
        FROM kline_data
        WHERE stock_code = %s AND kline_type = 'DAY'
        ORDER BY kline_date DESC
        LIMIT %s
    """, (stock_code, days))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    
    if not rows:
        return pd.DataFrame()
    
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    for col in ['open', 'high', 'low', 'close']:
        df[col] = df[col].astype(float)
    df['volume'] = df['volume'].astype(float)
    return df


def get_top_gainers(days: int = 180, limit: int = 20) -> list:
    """找出近N天涨幅最大的股票，同时计算距期间高点回撤（判断是反弹还是新高）"""
    from mutifactor.infra.yaml_storage import YAMLStorage
    db = YAMLStorage()
    with db._get_connection() as conn:
        with conn.cursor() as cursor:
            # 主查询：获取所有有足够数据的港股，同时返回交易日数
            cursor.execute("""
                SELECT a.stock_code, a.stock_name, b.cnt, b.start_date, b.end_date
                FROM stock_info a
                JOIN (
                    SELECT stock_code, COUNT(*) as cnt,
                           MIN(kline_date) as start_date,
                           MAX(kline_date) as end_date
                    FROM kline_data
                    WHERE kline_type = 'DAY'
                      AND kline_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                      AND kline_date <= CURDATE()
                    GROUP BY stock_code
                    HAVING cnt >= 10
                ) b ON a.stock_code = b.stock_code
                WHERE a.market = 'HK' AND a.listing_date IS NOT NULL
            """, (days,))
            stock_rows = cursor.fetchall()

            if not stock_rows:
                return []

            # 批量查K线：用窗口函数分行，Python端算高低点
            codes = [r[0] for r in stock_rows]
            codes_str = "','".join(codes)
            cursor.execute(f"""
                SELECT stock_code, kline_date, close,
                       ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY kline_date DESC) as rn
                FROM kline_data
                WHERE kline_type = 'DAY'
                  AND stock_code IN ('{codes_str}')
                  AND kline_date >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
                ORDER BY stock_code, kline_date DESC
            """, (days,))
            klines = cursor.fetchall()

            stock_klines: dict = {}
            for kline in klines:
                code = kline[0]
                if code not in stock_klines:
                    stock_klines[code] = []
                stock_klines[code].append(kline)

            result = []
            for stock_row in stock_rows:
                code, name, cnt, sql_start_date, sql_end_date = stock_row
                kl = stock_klines.get(code, [])
                if len(kl) < 2:
                    continue
                closes = [float(k[2]) for k in kl]
                last_close = closes[0]
                period_low = min(closes)
                period_high = max(closes)
                gain = (last_close - period_low) / period_low * 100 if period_low else 0
                drawdown = (period_high - last_close) / period_high * 100 if period_high else 0
                # 用SQL区间的日期和交易日数，而不是Python取的len(kl)
                result.append((code, name, cnt, gain, drawdown, sql_start_date, sql_end_date))

            result.sort(key=lambda x: x[3], reverse=True)
            return result[:limit]
    return result


def search_stocks(keyword: str) -> list:
    """按名称或代码模糊搜索"""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT stock_code, stock_name FROM stock_info
        WHERE market = 'HK' AND listing_date IS NOT NULL
          AND (stock_code LIKE %s OR stock_name LIKE %s)
        ORDER BY stock_name
    """, (f'%{keyword}%', f'%{keyword}%'))
    result = [(row[0], row[1]) for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return result


# ==================== 动量计算 ====================

def calculate_momentum(df: pd.DataFrame, window: int = MOMENTUM_WINDOW) -> tuple:
    """
    计算指数加权动量得分（与 momentum.py 逻辑一致）
    Returns: (momentum_score, r2)
    """
    if len(df) < window:
        return 0.0, 0.0
    
    recent = df.tail(window).copy()
    closes = recent['close'].values
    
    if not np.all(np.isfinite(closes)) or np.any(closes <= 0):
        return 0.0, 0.0
    
    valid_mask = np.isfinite(closes) & (closes > 0)
    if np.sum(valid_mask) < window * MIN_DATA_RATIO:
        return 0.0, 0.0
    
    valid_prices = closes[valid_mask]
    n = len(valid_prices)
    weights = np.linspace(LINEAR_WEIGHT_START, LINEAR_WEIGHT_END, n)
    log_prices = np.log(valid_prices)
    x = np.arange(n)
    
    # 加权线性回归
    w_sum = weights.sum()
    w_x = (weights * x).sum()
    w_y = (weights * log_prices).sum()
    w_xx = (weights * x * x).sum()
    w_xy = (weights * x * log_prices).sum()
    
    denom = w_sum * w_xx - w_x * w_x
    if abs(denom) < 1e-10:
        return 0.0, 0.0
    
    slope = (w_sum * w_xy - w_x * w_y) / denom
    intercept = (w_y - slope * w_x) / w_sum
    
    # R²
    y_mean = log_prices.mean()
    ss_res = (weights * (log_prices - (slope * x + intercept)) ** 2).sum()
    ss_tot = (weights * (log_prices - y_mean) ** 2).sum()
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    
    # 年化动量得分
    daily_return = slope
    annual_return = daily_return * 252
    momentum_score = annual_return * r2
    
    return momentum_score, r2


def calculate_volatility(df: pd.DataFrame, window: int = 20) -> float:
    """计算历史波动率"""
    if len(df) < window:
        return 0.0
    returns = df['close'].tail(window).pct_change().dropna()
    return returns.std() * np.sqrt(252)


def calculate_volume_ratio(df: pd.DataFrame, window: int = 20) -> float:
    """计算成交量放大比"""
    if len(df) < window + 5:
        return 1.0
    recent_vol = df['volume'].tail(window).mean()
    prev_vol = df['volume'].tail(window + 5).head(window).mean()
    return recent_vol / prev_vol if prev_vol > 0 else 1.0


def calculate_rsrs(df: pd.DataFrame, window: int = RSRS_WINDOW) -> float:
    """计算RSRS斜率"""
    if len(df) < window:
        return 0.0
    recent = df.tail(window)
    try:
        x = np.arange(window)
        slope_high, _ = np.polyfit(x, recent['high'].values, 1)
        slope_low, _ = np.polyfit(x, recent['low'].values, 1)
        return (slope_high + slope_low) / 2
    except:
        return 0.0


def analyze_stock_daily(stock_code: str, days: int = 120) -> pd.DataFrame:
    """
    计算指定股票每一天的动量分
    """
    df = get_stock_klines(stock_code, days)
    if df.empty:
        return pd.DataFrame()
    
    records = []
    for i in range(MOMENTUM_WINDOW, len(df)):
        window_df = df.iloc[:i+1].copy()
        momentum, r2 = calculate_momentum(window_df)
        volatility = calculate_volatility(window_df)
        vol_ratio = calculate_volume_ratio(window_df)
        rsrs = calculate_rsrs(window_df)
        
        price = df.iloc[i]['close']
        prev_price = df.iloc[i-1]['close']
        daily_return = (price / prev_price - 1) * 100 if prev_price > 0 else 0
        
        records.append({
            'date': df.iloc[i]['date'].strftime('%Y-%m-%d'),
            'close': round(price, 2),
            'daily_ret%': round(daily_return, 2),
            'momentum': round(momentum, 4),
            'r2': round(r2, 4),
            'volatility': round(volatility, 4),
            'vol_ratio': round(vol_ratio, 2),
            'rsrs': round(rsrs, 4),
        })
    
    return pd.DataFrame(records)


# ==================== 输出格式 ====================

def color_momentum(val: float) -> str:
    """动量着色"""
    if val > 0.5:
        return f'\033[92m{val:+.4f}\033[0m'  # 绿
    elif val > 0.2:
        return f'\033[94m{val:+.4f}\033[0m'  # 蓝
    elif val > 0:
        return f'\033[90m{val:+.4f}\033[0m'  # 灰
    elif val > -0.2:
        return f'\033[93m{val:+.4f}\033[0m'  # 黄
    else:
        return f'\033[91m{val:+.4f}\033[0m'  # 红


def print_analysis(stock_code: str, df: pd.DataFrame, stock_name: str = ''):
    """终端输出分析结果"""
    name_tag = f" [{stock_name}]" if stock_name else ""
    print(f"\n{'='*80}")
    print(f"📊 {stock_code}{name_tag} - 每日动量分析")
    print(f"{'='*80}")
    
    if df.empty:
        print("❌ 无数据")
        return
    
    # 基本统计
    print(f"\n📈 基本统计 (共 {len(df)} 个交易日)")
    print(f"   日期范围: {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
    print(f"   收盘价: {df['close'].iloc[0]} → {df['close'].iloc[-1]}")
    total_ret = (df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100
    print(f"   期间收益: {total_ret:+.1f}%")
    print(f"   动量均值: {df['momentum'].mean():+.4f}")
    print(f"   动量最大值: {df['momentum'].max():+.4f}")
    print(f"   动量最小值: {df['momentum'].min():+.4f}")
    
    # 最近N天表格
    show_days = len(df)
    print(f"\n📋 最近 {show_days} 天动量 (最新在上)")

    col_w = [12, 8, 8, 12, 8, 8, 6, 10]
    headers = ['日期', '收盘', '日涨跌%', '动量分', 'R²', '波动率', '量比', 'RSRS斜率']

    sep = "  ".join(ljust_d(h, col_w[i]) for i, h in enumerate(headers))
    print(sep)
    print("-" * display_width(sep))

    for _, row in df.tail(show_days).iloc[::-1].iterrows():
        m = f"{row['momentum']:+.4f}"
        tag = ""
        if row['momentum'] > 0.5:
            tag = "▲"
        elif row['momentum'] < -0.1:
            tag = "▼"

        vals = [
            str(row['date']),
            f"{row['close']:.2f}",
            f"{row['daily_ret%']:+.2f}",
            tag + m,
            f"{row['r2']:.4f}",
            f"{row['volatility']:.4f}",
            f"{row['vol_ratio']:.2f}",
            f"{row['rsrs']:+.4f}",
        ]
        print("  ".join(' ' * (col_w[i] - display_width(v)) + str(v) for i, v in enumerate(vals)))
    
    # 动量分段统计
    print(f"\n📊 动量分段统计")
    bins = [-np.inf, 0, 0.2, 0.5, 1.0, np.inf]
    labels = ['<0 (弱)', '0~0.2', '0.2~0.5', '0.5~1.0', '>1.0 (强)']
    df['momentum_bin'] = pd.cut(df['momentum'], bins=bins, labels=labels)
    dist = df['momentum_bin'].value_counts().sort_index()
    for label, count in dist.items():
        bar = '█' * int(count / len(df) * 30)
        print(f"   {label:>12s}: {count:3d} ({count/len(df)*100:5.1f}%) {bar}")
    
    # 动量转折点分析
    print(f"\n🔄 动量显著变化点")
    if len(df) > 5:
        df['mom_change'] = df['momentum'].diff().abs()
        big_changes = df[df['mom_change'] > 0.5].head(5)
        for _, row in big_changes.iterrows():
            prev = df[df['date'] < row['date']].iloc[-1] if len(df[df['date'] < row['date']]) > 0 else None
            if prev is not None:
                print(f"   {row['date']}: {prev['momentum']:+.4f} → {row['momentum']:+.4f} (变化: {row['mom_change']:+.4f})")


def plot_momentum(df: pd.DataFrame, stock_code: str, output_dir: str):
    """生成动量图表"""
    if not HAS_PLOT:
        print("⚠️ matplotlib 未安装，跳过图表生成")
        return
    
    os.makedirs(output_dir, exist_ok=True)
    
    fig, axes = plt.subplots(3, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(f'{stock_code} - 动量分析', fontsize=14, fontweight='bold')
    
    dates = pd.to_datetime(df['date'])
    
    # 1. 收盘价
    axes[0].plot(dates, df['close'], 'b-', linewidth=1.5, label='Close')
    axes[0].fill_between(dates, df['close'], alpha=0.3)
    axes[0].set_ylabel('Price (HKD)')
    axes[0].legend(loc='upper left')
    axes[0].grid(True, alpha=0.3)
    
    # 2. 动量分
    colors = ['g' if m >= 0 else 'r' for m in df['momentum']]
    axes[1].bar(dates, df['momentum'], color=colors, alpha=0.7, width=1)
    axes[1].axhline(0, color='black', linewidth=0.5)
    axes[1].axhline(0.5, color='green', linewidth=0.5, linestyle='--', alpha=0.5)
    axes[1].axhline(-0.5, color='red', linewidth=0.5, linestyle='--', alpha=0.5)
    axes[1].set_ylabel('Momentum Score')
    axes[1].grid(True, alpha=0.3)
    
    # 3. R²
    axes[2].plot(dates, df['r2'], 'purple', linewidth=1, label='R²')
    axes[2].set_ylabel('R²')
    axes[2].set_xlabel('Date')
    axes[2].legend(loc='upper left')
    axes[2].grid(True, alpha=0.3)
    axes[2].set_ylim(0, 1)
    
    plt.tight_layout()
    path = os.path.join(output_dir, f'{stock_code.replace(".", "_")}_momentum.png')
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"   📈 图表已保存: {path}")


# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description='港股动量分析工具')
    parser.add_argument('--codes', nargs='+', help='股票代码，如 HK.06181')
    parser.add_argument('--top-gainers', type=int, metavar='N', help='分析近期涨幅最大的N只股票')
    parser.add_argument('--brief', action='store_true', help='简洁模式：只输出排行表，不做深度分析')
    parser.add_argument('--search', type=str, help='按名称搜索股票')
    parser.add_argument('--days', type=int, default=120, help='回溯天数（默认120天）')
    parser.add_argument('--output', type=str, default='output/momentum_analysis', help='CSV输出目录')
    parser.add_argument('--plot', action='store_true', help='生成图表')
    parser.add_argument('--limit', type=int, default=30, help='最多显示最近N天（默认30天）')
    
    args = parser.parse_args()
    
    # 加载股票信息
    stock_info = load_stock_info()
    
    target_codes = []
    
    if args.search:
        results = search_stocks(args.search)
        if not results:
            print(f"❌ 未找到包含 '{args.search}' 的股票")
            return
        print(f"🔍 搜索 '{args.search}' 结果:")
        for code, name in results:
            print(f"   {code:12s} {name}")
        print()
        target_codes = [r[0] for r in results]
    
    elif args.top_gainers:
        gainers = get_top_gainers(days=args.days, limit=args.top_gainers)
        if not gainers:
            print("❌ 未找到符合条件的数据")
            return
        print(f"📊 近 {args.days} 天涨幅排行 (共 {len(gainers)} 只)")

        # 表头（智能宽度对齐）
        def col(text: str, width: int) -> str:
            return ljust_d(text, width)

        head = f" {'#':>3}  {col('代码', 12)}  {col('名称', 14)}  {col('交易日', 6)}  {col('涨幅', 7)}  {col('距高点', 7)}  日期区间"
        print(head)
        print("-" * display_width(head))

        for i, (code, name, trading_days, gain, drawdown, start_date, end_date) in enumerate(gainers, 1):
            if drawdown > 15:
                flag = "⚠️反弹"
            elif drawdown < 3:
                flag = "✨新高"
            else:
                flag = ""

            row = (
                f" {i:>3}. "
                f"{col(code, 12)}  "
                f"{col(name, 14)}  "
                f"{trading_days:>4}天 "
                f"{gain:+6.1f}% "
                f"{drawdown:>6.1f}% "
                f"{flag:<5} "
                f"{start_date} ~ {end_date}"
            )
            print(row)
        print(f"\n💡 完整动量分析: python3 scripts/analysis/momentum_analysis.py --top-gainers {args.top_gainers} --days {args.days}")
        if not args.brief:
            target_codes = [g[0] for g in gainers]
        else:
            return
    
    elif args.codes:
        target_codes = args.codes
    
    else:
        parser.print_help()
        print("\n💡 示例:")
        print("   python3 scripts/analysis/momentum_analysis.py --codes HK.06181")
        print("   python3 scripts/analysis/momentum_analysis.py --codes HK.06181 HK.02513 --days 180")
        print("   python3 scripts/analysis/momentum_analysis.py --top-gainers 20 --days 180")
        print("   python3 scripts/analysis/momentum_analysis.py --search 黄金")
        return
    
    # 分析每只股票
    os.makedirs(args.output, exist_ok=True)
    
    for stock_code in target_codes:
        name = stock_info.get(stock_code, {}).get('name', '')
        print(f"\n⏳ 正在分析 {stock_code}...", end=' ')
        sys.stdout.flush()
        
        df = analyze_stock_daily(stock_code, days=args.days)
        
        if df.empty:
            print(f"❌ 无数据")
            continue
        
        print(f"✅ {len(df)} 个交易日")
        
        print_analysis(stock_code, df, stock_name=name)
        
        # 保存CSV
        csv_path = os.path.join(args.output, f'{stock_code.replace(".", "_")}_daily_momentum.csv')
        df.drop(columns=['momentum_bin', 'mom_change'] if 'momentum_bin' in df.columns else [], errors='ignore')
        df.to_csv(csv_path, index=False, encoding='utf-8-sig')
        print(f"   💾 CSV已保存: {csv_path}")
        
        # 生成图表
        if args.plot:
            plot_momentum(df, stock_code, args.output)


if __name__ == '__main__':
    main()
