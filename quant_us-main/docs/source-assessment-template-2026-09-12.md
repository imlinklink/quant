# 历史证券数据源评估：富途 OpenD（2026-09-12）

> 依据 `historical-universe-and-evidence-handoff-technical-design-2026-09-12.md` §2.3。使用者选定的来源为**富途 OpenD**。本文件区分「代码查证所得」与「需探针实测」；实测脚本：`scripts/data/probe_futu_source.py`。

状态：**已选定，范围收窄 —— 仅当前存续样本，不含退市样本（使用者决定 2026-09-12）**。可支撑工程验证与**当前存续样本**的 A/B/C；结论必须显式标注幸存者偏差。

## 0. 范围决定（使用者）

- **不引入退市样本**。样本 = 富途当前存续证券；`security_master_v2` 不要求 `delisted_at` 完整。
- 影响：结论**不是** survivorship-bias-free，禁止表述为"无偏/全历史总体"；正式报告须注明"当前存续样本、存在幸存者偏差"。
- 因此 §2.6 的"已退市证券"测试与 §2.5 的退市过滤在本轮**不作为放行条件**（代码与测试仍保留，备将来接入退市来源）。
- 公司行动仍可用 QFQ vs 不复权反推（标 `unverified`），不阻塞主流程。

## 1. 能力核对

| 能力 | 结论 | 依据 |
|---|---|---|
| 历史退市证券 | **不提供**（待探针确认） | `build_security_master_from_futu.py` 只取 `get_stock_basicinfo` 的**当前**目录 |
| ticker 变更历史 | **不提供** | 富途无 symbol-history 映射接口 |
| 历史上市/退市日期 | 上市日**提供**；退市日**不提供** | `listing_date`/`list_time` 可用；脚本把 `delisting_date` 写成空字符串 |
| 公司行动（拆股/股息/…） | **不直接提供**；可由 QFQ 与不复权价差**反推** | `derive_corporate_actions.py`；反推结果标 `unverified`，需人工复核 |
| 历史日线 | **提供** | `request_history_kline` 支持 `AuType.QFQ` 与不复权 |
| 原始可交易价 | **提供** | 取不复权（`AuType.NONE`）序列 |
| 下载权 / 长期保存权 / 研究用途许可 | **需使用者确认** | 由富途条款决定，非 API 能力 |
| 时间字段可审计性（`published_at/observed_at`） | **不可得** | 富途不提供历史首次可见时间，D 组严格层无法用富途构建 |

## 2. 结论与影响

- 富途可从「当前存续证券 + 历史日线 + QFQ/不复权」支撑：`security_master_v2`（仅当前存续）、`symbol_history`（仅当前 ticker）、`corporate_actions`（反推，未核验）、双价格视图、时点 universe。
- **退市证券缺失** → 存在幸存者偏差，`historical_delisted` 探针不通过即意味着：任何需要退市样本的正式结论必须标 **`inconclusive`**。
- 因此本来源可支撑 **`engineering_pass`** 与**显式标注幸存者偏差**的当前存续样本 A/B/C，不能支撑 `research_ready_abc` 的无偏等价结论。

## 3. 探针（在本机 OpenD 上运行，只读）

```bash
python3 scripts/data/probe_futu_source.py \
  --delisted-codes <你自己已知的已退市美股票代码...> \
  --corporate-codes US.AAPL US.MU \
  --output-dir data/source_archive/futu_probe/2026-09-12
```

输出 `source_assessment.json`，其中 `conclusion` 为 `partial` 或 `pass_candidate`。把该文件连同本节一并作为正式评估证据。

## 4. 冲突处理政策

- 单一来源（富途）时无跨源冲突；但**反推的公司行动**与后续任何外部行动表冲突时，以外部核验来源为准，富途反推行降级为诊断。
- 一旦引入第二个来源，优先级与 `master_conflicts.csv` 流程按 §2.3 执行。

## 5. 待使用者决策项

1. `--delisted-codes` 用哪些已知退市代码做探针？（决定 `partial` 还是 `pass_candidate`）
2. 富途条款是否允许**批量下载、本地长期保存、研究用途**？（决定归档是否合规）
3. 是否接受「富途 only → 结论限定为当前存续样本、显式标注幸存者偏差」；若是，退市补全仍需另找来源。
