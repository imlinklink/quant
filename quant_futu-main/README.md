# Quant Futu - 港/美股量化交易系统

基于**动量策略 + RSRS 趋势过滤**的量化交易系统，支持港股/美股回测和实盘交易。

---

## 核心特性

| 模块 | 功能 |
|------|------|
| **选股** | 加权动量得分 + RSRS 趋势过滤 + 波动率加权 + 行业分散 |
| **买入** | 智能低吸：低开检测、V型反转、分时企稳确认 |
| **风控** | ATR 动态止盈止损(3×/2×) + 渐进式时间退出 + 阴跌加速止损 |
| **回测** | 完整交易成本模拟 + 收益对比图表 |
| **实盘** | 富途 OpenD 直连，支持模拟/实盘双模式 |

---

## 快速开始

```bash
# 1. 安装依赖
make install

# 2. 配置数据库和富途连接
vim config.yaml

# 3. 运行回测
make hk-backtest
make hk-backtest-analyze    # 生成中文分析报告

# 4. 启动实盘（模拟模式）
make hk-live-start
```

---

## 项目结构

```
quant_futu/
├── mutifactor/              # 核心策略包
│   ├── strategies/
│   │   ├── momentum.py      # 动量策略 + RSRS 趋势过滤
│   │   └── exit_strategy.py # ATR 止盈止损
│   ├── data/
│   │   └──  hk_fetcher.py    # 港股数据源
│   ├── trading/
│   │   └── futu_trader.py   # 富途交易接口
│   └── infra/
│       └── yaml_storage.py    # 文件持久化
├── scripts/
│   ├── backtest/            # 回测脚本
│   ├── live_trading/        # 实盘交易
│   └── analysis/            # 分析工具
├── config.yaml              # 主配置
└── Makefile                 # 快捷命令
```

---

## Makefile 命令

### 港股
```bash
make hk-backtest              # 运行回测
make hk-backtest-analyze      # 分析回测结果
make hk-live-start            # 启动实盘
make hk-live-stop             # 停止实盘
make hk-live-status           # 查看状态
make hk-live-log              # 实时日志
make hk-live-view             # 查看选股结果
```
```

### 分析工具
```bash
make momentum-analyze         # 动量分析(默认老铺黄金)
make momentum-top             # 涨幅Top20分析
make momentum-search KEY=黄金  # 按名称搜索
```

### 测试
```bash
make test                     # 运行测试
make test-coverage            # 生成覆盖率报告
```

---

## 策略参数

关键配置项（`config.yaml`）：

```yaml
momentum:
  momentum_window: 25          # 动量计算窗口
  rsrs_window: 18              # RSRS短期窗口
  rsrs_long_window: 125        # RSRS长期窗口
  min_momentum_score: 0.01     # 最小动量得分
  min_r2: 0.5                  # 最小拟合度
  max_positions: 3             # 最大持仓数

risk:
  exit_strategy: atr_dynamic   # 止盈止损策略
  take_profit_multiplier: 4.0  # 止盈ATR倍数
  stop_loss_multiplier: 3.0    # 止损ATR倍数
  max_single_position_ratio: 0.6  # 单票最大仓位
```

---

## 数据流

```
选股流程:
股票池 → 数据质量检查 → 动量得分计算 → RSRS趋势过滤 → 波动率调整 → 行业分散 → 最终候选

买入流程:
候选股票 → 智能低吸检测 → 企稳确认 → 分批买入 → 持仓管理

风控流程:
持仓监控 → ATR止盈止损 → 时间退出检查 → 阴跌加速检测 → 自动平仓
```

---

## 日志

```
logs/
├── backtest.log              # 回测日志
└── hk_live_trading.log       # 港股实盘日志
```

---

## 未来功能规划 (Roadmap)

### 已实现功能 ✅
| 功能 | 状态 | 说明 |
|------|------|------|
| 港股回测/实盘 | ✅ | 完整支持 |
