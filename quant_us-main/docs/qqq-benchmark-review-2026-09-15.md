# QQQ 同口径基准审查与修复（2026-09-15）

> 审查未提交改动（`qqq_benchmark.py` / `performance.py` / `p2_selection_check.py`），发现并修复 3 处基准正确性边界。全量 684 passed。

## 1. [P1] 基准漏计首日收益与买入费

- 现象：`build_qqq_benchmark` 只返回 session/equity，`performance_metrics` 把首日收盘净值当本金；比较分支也用首日收盘当分母。平盘 + 0.1% 买入费仍报告总收益 0%。
- 修复：基准返回 `initial_equity` 列；`performance_metrics` 比较分支的分母改用 `benchmark.initial_equity`（无该列时回退首行）。
- 验证：平盘 400 交易日，benchmark_CAGR 0% → −0.065%、自身 total_return/max_drawdown −0.1%。

## 2. [P2] 除息日首日误获分红

- 现象：开盘买入当天即入账除息分红，净值凭空增加。
- 修复：循环 `if i > 0` 跳过首日分红（与组合引擎「当天新开仓不享权益」一致）。
- 验证：新增 `test_buys_on_ex_div_date_skips_first_day_dividend`。

## 3. [P2] manifest 漏登记 QQQ 分红哈希

- 现象：P2 用 `QQQ_DIVIDENDS` 计算基准，但 `input_sha256` 未登记该文件。
- 修复：`input_sha256` 加入 `QQQ_DIVIDENDS`。

## 重跑（新 run-id，修正后口径）

- P1 `P1-ACCOUNT-20260915-001`：基准 CAGR 19.68% → 19.60%（−8bp，首日收益+费已计入）。
- P2 `P2-CHECK-20260915-001`：基准 CAGR 20.12%（窗口 2016-04-01 起，与 P1 的 2016-01-05 起不同，属正常）。

## 新增测试

`tests/unit/medium_term/test_qqq_benchmark.py` 3 条：平盘计费、首日收益计入、首日除息不获分红。
