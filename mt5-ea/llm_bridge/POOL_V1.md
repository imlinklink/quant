# 策略池 v1 准备清单（EMA 回调 × 伦敦突破）

> 状态: 2026-09-03 代码已备好，**EA 部分尚未在 MT5 中编译验证**（当时 MT5 未运行）。
> 回来后的操作顺序见文末，不要跳步。

## 1. 目标
两套策略由 bridge 确定性路由互斥轮值，一次只激活一套入场规则：

| 时段(北京时间, 周一~周五) | 激活策略 | 说明 |
|---|---|---|
| 15:00-16:00(欧洲夏令时) / 16:00-17:00(冬令时) | 10-伦敦突破 | 需亚盘区间数据 |
| 其余时间 | 03-EMA趋势回调 | 默认策略 |

窗口按欧洲夏令时自动切换，无需手动改配置。

## 2. 本次新增/修改的文件
- `llm_bridge/bridge.py`: 路由 `choose_strategy()`、系统提示只注入激活策略、台账自动带策略标记、启动日志显示策略池。
- `llm_bridge/skills/10-伦敦突破.md`: 新增（描述式规则）。
- `llm_bridge/skills/01-交易纪律与风控.md`: 增加"结构突破除外"说明，避免与伦敦突破入场规则冲突。
- `llm_bridge/bridge_config.json(.example)`: 新增 `default_strategy`（当前 `ema_trend_pullback`）。
- `SimpleAutoTrader.mq5`: 快照新增 `UTC_EPOCH / ASIA_HIGH / ASIA_LOW / D1_HIGH / D1_LOW` 5 个字段 + `BuildAsiaRange()` 辅助函数。
- `POOL_V1.md`: 本文档。

## 3. EA 侧改了什么（需要你回来编译）
`WriteLLMSnapshot()` 在写 SPREAD 之后新增：
- UTC_EPOCH: `TimeGMT()` 秒数，bridge 用它算北京时间并做窗口路由；
- D1_HIGH / D1_LOW: 前一日高低点，给止盈参考；
- ASIA_HIGH / ASIA_LOW: 北京时间 07:00-14:59 的当日亚盘高低点，由 `BuildAsiaRange()` 每根新K线重算。

注意点:
- `BuildAsiaRange()` 扫描最近 1440 根 M1 已收盘K线，只统计当天北京 07:00-14:59 的K线；07:00 前或 16:00 后返回 0（路由在该时段也不需要它）。
- 服务器时区偏移用 `TimeCurrent() - TimeGMT()` 计算，随夏令时自动变化，不需要手工改。
- 代码未编译验证: 若 F7 编译报错，把 MetaEditor 错误日志贴回来，我来修。

## 4. 回来后的操作顺序（重要）
1. 重启 bridge（加载新代码）: 结束旧进程后 `cd ~/Documents/quant/mt5-ea/llm_bridge && python3 -u bridge.py`；
2. 把 `SimpleAutoTrader.mq5` 同步到你 MT5 的 Experts 目录（沿用之前的同步方式）；
3. MetaEditor 里 F7 全量编译，确认 0 错误；
4. 打开 MT5，XAUUSD M1 挂 EA（InpUseLLMBridge=true），开 Algo Trading 绿灯；
5. 观察 bridge 日志: 每根新K线先打印 `[路由] 激活策略: ...`；
6. 等一个伦敦窗口（15:00-16:00 北京时间），确认快照里有 `UTC_EPOCH/ASIA_HIGH/ASIA_LOW`，且窗口内路由到 10-伦敦突破、窗口外回到 03-EMA趋势回调；
7. 确认 ledger.csv 的 strategy 列自动区分两套策略。

## 5. 验证台账小抄
- 窗口外(EMA): `ledger.csv` 中 strategy=03-EMA趋势回调
- 窗口内(伦敦): strategy=10-伦敦突破
- 重启 bridge 不会重复记录同一快照（有去重逻辑）。

## 6. 已知边界
- 亚盘区间依赖 MT5 本机 M1 历史数据与 EA 持续在线；EA 重启当天的区间会重新从历史K线算出，不丢。
- 路由只认快照里的 UTC_EPOCH；旧 EA（未编译）没这个字段，永远走默认 EMA 策略——所以先编译再挂载。
- 当前正在跑的旧 bridge 不会自动加载这些改动，下次重启前如果 MT5 已经开始出快照，可能把 4 个技能都注入提示词，因此**先重启 bridge 再开 MT5**。
