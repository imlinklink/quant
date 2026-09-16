# P2 候选漏斗复核与修复（A4 · 2026-09-14）

> 复核范围：`scripts/medium_term/p2_selection_check.py` 及依赖的选股/择时/特征模块。
> 结论：三处已知问题全部定位坐实并已修复；另发现一处 A2 引入的回归（F0，已修）。
> 验证：全量测试 677 passed；P2 build-only 与完整 run（`P2-CHECK-20260914-001`）端到端跑通。

## F0（A2 引入回归，已修）P2 ImportError

- `p2_selection_check.py:24` 仍 import 已删除的 `_qqq_curve` → ImportError。
- 修复：改用同口径 `build_qqq_benchmark`；新增导入冒烟测试防复发。

## F1 跨拆股特征（已修）

- **B2 动量**：`generate_monthly_candidates` 改传 `actions` + 复权用 bar（`view_bars`），走
  `point_in_time_momentum_snapshot`（逐决策日复权）。NVDA 2024-06-28 的 6m 动量从假 −75% 修正为 +150%。
- **B3 择时**：`build_timed_entries` 新增 `actions`，按候选等待窗末日构建拆股复权 bar 再算周线门/突破/回踩。
- **行动范围过滤**：`point_in_time_momentum_snapshot` 与 `_adjusted_bars` 只保留 `ex_date` 落在该证券首根 bar 之后的行动，避免 `build_price_view` 因缺前收盘拒绝（历史行动表回填至 1987，面板始于 2015）。

## F2 候选缺失/过期分母（已修）

- `build_entries` 的 ATR 缺失/止损无效从 `KeyError`/`ValueError` 崩溃改为 `drop(ASOF_ATR_MISSING / INVALID_INITIAL_STOP)`。
- 新增 `FUNNEL_DENOMINATOR_MISMATCH` 对账断言：`candidates == prepared_entries + sum(excluded)`。
  实测 B2 `515 = 490 + 25`、B3 `461 = 442 + 19`。

## F3 共同评价窗（已修）

- `_account` 改为接收跨 B2/B3 的共同窗 `[min(entry_session), max(exit_session)]`，并显式传
  `evaluation_start`（晚入场的 B3 在窗首保留现金）。实测 B2/B3 全部口径同为
  `2016-04-01 → 2026-08-25`（2615 会话）。

## 产物

- 复核文档：本文件；基准产物：`data/medium_term/QQQ-ACTION-DERIVE-20260914/`；
  P2 验证 run：`backtests/medium_term/P2-CHECK-20260914-001/`。
- 新增测试：`test_qqq_benchmark.py`、`test_p2_selection.py::ModuleImportSmokeTests`、
  `test_momentum_baseline.py::PointInTimeMomentumTests`。
