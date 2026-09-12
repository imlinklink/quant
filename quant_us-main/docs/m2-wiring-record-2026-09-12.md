# M2 接线记录：原始执行价 + 逐日 as-of 特征（2026-09-12，进行中）

依据交接方案 §5。**本文只记录接线构件；在 M1 放行前不发布新的收益结论。**

## 已实现的构件

| 构件 | 作用 | 提交 |
|---|---|---|
| `scripts/data/download_market_history.py --autype none` | 下载**不复权**日线（原始执行价来源） | `69de079` |
| `scripts/data/asof_features.py` | 单决策日的 as-of 特征 + 前视量化 | `21c4978` |
| `scripts/data/asof_feature_panel.py` | **逐日面板**：每交易日的原始价 + as-of 特征 + 跨日尺度因子 | `0252701` |
| `scripts/data/price_views.py`（复核修正） | `asof_adjusted` 必须显式 `--as-of`，且只保留 `session <= as_of` | `12c06a7` |

## 关键不变量（真实数据已验证）

- **`asof_close(d) == raw_close(d)`**：as-of 序列锚定在决策日 d，故当日的 as-of 价**等于原始价**。
  含义：由 as-of 特征导出的**止损/高开门等价格水平本身就是原始价尺度**，可直接与原始执行价比较。
- 在 39 只、**105,545 行**上逐行成立。
- **跨行动日（T+1 恰为除权日）共 838 处**（HON/NVDA/AAPL/NEE 各约 48–49）。这些日子须把 d 日水平乘以
  `scale_to_next` 再与 T+1 原始价比较，否则除权跳空会被误判为高开或跌破止损。
- 窗口外行动（如 2012 年分红而价格窗口自 2015 起）**不影响本段序列**；缺前收的股息因子已跳过并可在审计中标注。

## 尚未完成（M2 的集成步骤）

仍待把上述构件接到实验链，使 A/B/C 重建为：
1. `generate_historical_setups`：特征取 **as-of 面板**（而非 QFQ 全快照）；`next_open_price`/`signal_close` 取**不复权原始价**；高开/止损门用 `level × scale_to_next` 与 T+1 原始价比较。
2. `buy_strategy_experiment_runner` / `exit_matrix`：执行价与退出价改用**不复权**日线。
3. 另建实验编号（如 `BUY-WD-ABC-SURVIVOR-RAW-001`）冻结重跑；与 `SURVIVOR-003` 分离，不覆盖。

## 放行与结论纪律

- **M1 未放行 → M2 `blocked`**：18 只上市日未核验、旧实验用 QFQ 执行价与全快照特征。
- 在本轮修正口径重跑完成**之前**，不得发布新的 A/B/C 收益结论；`1526/11290` 的前视影响面须由修正后的**配对重跑**检验，不得沿用"影响很小"的推测。

全量测试：**558 passed**。面板产物：`data/survivor_sample_audit/asof_panels/*.csv.gz` 与 `asof_panel_summary.csv`（本机，gitignore）。
