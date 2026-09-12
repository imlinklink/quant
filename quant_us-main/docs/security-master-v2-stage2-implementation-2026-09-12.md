# 证券主数据 v2 与双价格视图（阶段 2–3）落地记录（2026-09-12）

依据：`historical-universe-and-evidence-handoff-technical-design-2026-09-12.md` §2.2（数据契约）、§2.3（采集接口）、§2.6（质量门与测试）。本阶段状态：**engineering_pass**（数据契约与质量门通过，不作任何策略结论）。

## 交付物

| 文件 | 作用 |
|---|---|
| `scripts/data/security_master_v2.py` | 三表 schema、标准化、区间重叠校验、symbol 解析、跨源冲突检测、审计汇总、record_hash、`build_outputs` |
| `scripts/data/source_archive.py` | 来源归档（`<archive_root>/<source_id>/<run_id>/raw`）+ `source_manifest.json`，不可覆盖 |
| `scripts/data/import_security_master_v2.py` | CLI：多来源归档 → `security_master_v2.csv` / `symbol_history.csv` / `corporate_actions.csv` / `master_conflicts.csv` / `import_summary.json` |
| `scripts/data/import_symbol_history.py` | CLI：ticker 历史 |
| `scripts/data/import_corporate_actions.py` | CLI：公司行动 |
| `scripts/data/audit_security_master_v2.py` | CLI：审计，输出 `quality_by_security.csv` + `summary.json`；有阻断错误时退出码 1 |
| `tests/fixtures/security_master_v2/**` | 本地 fixture：退市、改名、复用（含冲突样例）、拆股/反向拆股、并购、来源冲突 |
| `tests/unit/live/test_security_master_v2.py` | 7 项契约与质量门测试 |

代码提交：`a3a77f5`。

## 覆盖的技术设计检查项（§2.6）

1. **已退市证券**：`SEC-000002` 有 `delisted_at` 且存在 `merger` 结算行动；无结算行动时审计报 `TERMINAL_OUTCOME_UNKNOWN`。
2. **ticker 改名**：`SEC-000001` 的 `US.OLD → US.NEW` 区间连续、互不重叠，`resolve_symbol` 按日期正确归属。
3. **ticker 复用**：`US.REUSE` 在不重叠窗口先属 `SEC-000003`、后属 `SEC-000004`（合法）；重叠样例触发 `SYMBOL_REUSE_CONFLICT` 并写入 `master_conflicts.csv`。
4. **拆股/反向拆股**：以 `corporate_actions` 记录（split ratio 2、reverse_split ratio 0.5）；价格口径属阶段 3。
5. **并购**：`merger` 行动 + `delisting_reason`。
6. **数据源冲突**：`SEC-000004` 的 `exchange` 在两来源不一致 → 不静默覆盖，输出冲突表并将该证券 `quality_status=conflict`。
7. **T−1 流动性**：属历史时点 universe（阶段 4），本阶段未涉及。
8. **扩池前后一致性**：需真实扩池数据，阶段 4。

## 复现命令（本地 fixture）

```bash
python3 scripts/data/import_security_master_v2.py \
  --source-archive source_a=tests/fixtures/security_master_v2/source_a \
                    source_b=tests/fixtures/security_master_v2/source_b \
  --run-id R1 --archive-root /tmp/arch --output-dir /tmp/master_v2

python3 scripts/data/audit_security_master_v2.py \
  --master /tmp/master_v2/security_master_v2.csv \
  --symbols /tmp/master_v2/symbol_history.csv \
  --actions /tmp/master_v2/corporate_actions.csv \
  --output-dir /tmp/master_v2/quality
```

全量测试：490 passed（含本阶段 7 项）。

## 明确边界与未完成

- **无真实历史来源**：仓库内没有覆盖退市/ticker 变更/公司行动的供应商数据，仅有 13 只试点 Futu 主数据。本阶段只验证契约、冲突处理与质量门，**不能**作为历史样本或策略结论的依据。
- **仍待实现**：原始可交易价与 as-of 特征价双视图（阶段 3）、历史时点 universe v2 与 20–50 只普通股工程扩池（阶段 4）、experiment_manifest 元数据扩展与正式 ABC 冻结（阶段 5）、证据快照与离线标签（阶段 6–7）。
- **需用户侧决策**：阶段 1 的 `source-assessment.md`（选定供应商并核对研究用途/本地保存许可）在获得数据源之前无法推进。

## 阶段 3：双价格视图与公司行动一致性（提交 `e8c45c7`）

依据 §2.4。交付物：

| 文件 | 作用 |
|---|---|
| `scripts/data/price_views.py` | `build_price_view`（raw / asof_adjusted）、`build_price_views`、`action_factor`、`action_version`、`terminal_outcome_flags` |
| `scripts/data/build_price_views.py` | CLI：输出 `price_views.csv.gz`、`terminal_outcome.csv`、`summary.json`，`--dry-run`、不可覆盖 |
| `tests/unit/live/test_price_views.py` | 11 项测试 |

要点：

- **原始可交易价**：不做任何调整，`adjustment_as_of=None`，用于 T+1 开盘成交/止损/价格门/费用。
- **as-of 特征价**：只应用 `ex_date <= as_of` 的行动，记录 `price_basis`/`adjustment_as_of`/`action_version`；**未来行动一律不应用**（as_of 早于 ex_date 时调整后等于原始，有测试）。
- 行动因子：拆股 `1/ratio`、反向拆股 `1/ratio`、现金股息 `1 - cash/prev_close`、分拆 `1 - ratio`；缺除息前收盘时报错而非静默。
- 一致性：拆股后 as-of 复权序列连续，且跨行动的总收益与原始价×股数变化一致；成交量按因子反向调整，**美元成交额守恒**。
- **终局结算**：`terminal_outcome_flags` 检查日线是否覆盖到最后可交易日、是否有 `merger/delisting_settlement`；缺失即 `terminal_outcome_unknown`，不得把最后一根收盘价当默认盈利退出。

## 阶段 4：历史时点 universe v2（提交 `c6250ff`）

依据 §2.5。按设计在现有 `scripts/historical_universe.py` 上新增 v2 函数，**v1 接口原样保留**供试点复现。

| 文件 | 作用 |
|---|---|
| `scripts/historical_universe.py` | 新增 `build_point_in_time_universe_v2`、`universe_reason_summary`、`_symbol_as_of` |
| `scripts/data/build_historical_universe_v2.py` | CLI：输出 `universe.csv.gz`、`universe_reasons.csv`、`summary.json` |
| `tests/unit/live/test_universe_v2.py` | 8 项测试 |

要点（输出列与 §2.5 一致）：`universe_date,security_id,symbol_as_of,asset_type,listed,tradable,previous_raw_close,adv20_usd,liquidity_as_of,eligible,reason,master_version,price_version,quality_status`。

- **键为 `security_id`**：ticker 改名不产生两只“新股票”，`symbol_as_of` 取当时有效 ticker。
- **退市窗口**：退市日之后 `eligible=False`、`reason=DELISTED`；最后可交易日仍保留。
- **T−1 流动性**：只用 `liquidity_as_of < universe_date` 的价格/流动性；违反直接 `LOOKAHEAD_LIQUIDITY`；改变 T 日成交额不改变 T 日 universe（有测试）。
- **拒绝记录全部保留**并标注 `reason`：`NOT_LISTED / DELISTED / MASTER_CONFLICT / SYMBOL_UNAVAILABLE / MISSING_BARS / ACTION_UNRESOLVED / PRICE_TOO_LOW / DOLLAR_VOLUME_TOO_LOW / ELIGIBLE`，并输出原因计数。
- **主样本分层**：主样本 = `eligible & asset_type=='stock'`；普通 ETF 与杠杆 ETF 输出独立切片，不提高普通股结论。

全量测试：509 passed。状态 **engineering_pass**。未做：20–50 只真实普通股扩池所需的真实数据源（阶段 1 待用户决策）、experiment_manifest 元数据扩展与正式 ABC 冻结（阶段 5）、证据快照（阶段 6–7）。

**阶段 1–5 的工程代码至此可全部用 fixture 验证；真实数据接入前不能产出历史样本或策略结论。**
