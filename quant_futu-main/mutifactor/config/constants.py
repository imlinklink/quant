"""
常量定义 - 项目中所有魔数的集中管理
"""
# 交易相关常量
class TradingConstants:
    # 最小佣金
    MIN_COMMISSION_HKD = 3.0

    # 港股费率
    HK_STAMP_DUTY_RATE = 0.0013     # 印花税 0.13%
    HK_TRADING_FEE_RATE = 0.00005   # 交易征费 0.005%
    HK_SETTLEMENT_FEE_RATE = 0.00002  # 结算费 0.002%
    HK_COMMISSION_RATE = 0.0025     # 港股佣金 0.25%
    HK_TRADING_LEVY_RATE = 0.000027  # 交易征费(个别交易所) 0.0027%
    HK_SLIPPAGE_RATE = 0.001       # 港股滑点 0.1%

    # 波动率范围
    MIN_VOLATILITY = 0.10           # 最小可接受波动率10%
    MAX_VOLATILITY = 0.80           # 最大可接受波动率80%

    # 流动性阈值
    MIN_AVG_VALUE = 1000000         # 最小日均成交额阈值


