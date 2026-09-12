# 交接文档执行总结（2026-09-11 ~ 2026-09-12）

本文汇总本会话依据三份交接文档所做的**全部变动**：改了什么、为什么、证据在哪、当前状态与未完成项。供交接与复核使用。

## 0. 起止状态

| 项目 | 起点 | 终点 |
|---|---|---|
| HEAD | `909d485`（有 3 个未提交改动） | `43d77f9` |
| 工作区 | 有未提交改动 | 干净 |
| 全量测试 | 461 passed（手册参考值） | **544 passed** |
| 提交数 | — | 本会话 **41 个**（`acdbc85..43d77f9`），56 文件改动（+5178 / −128），新增 45 文件 |

> 注：`cfe4587`（退出删失/路径/联合重采样修复）与 `70b2e8b`（加入技术设计文档）由**另一会话**提交，非本会话成果；本会话在其基础上继续。

## 1. 依据的交接文档

1. `weekly-daily-strategy-test-handoff-manual-2026-09-11.md`（测试入口）
2. `weekly-daily-strategy-operations-manual-2026-09-11.md`（操作手册）
3. `historical-universe-and-evidence-handoff-technical-design-2026-09-12.md`（技术设计，**后续工作以此为准**）

---

## 2. 退出矩阵与报告的缺陷修复（对应测试手册 §12/§13）

这一阶段的背景：原矩阵/报告有若干真实缺陷，导致结论不可信。逐项修复（提交见括号）：

| # | 问题 | 修复 | 提交 | 影响 |
|---|---|---|---|---|
| 1 | `simulate_daily` 用 `date >= entry_time`（bar 00:00 vs 入场 09:30 ET）比较，**丢弃成交当日整根 K 线**，退出从次日才开始 | 改为按交易日选取窗口，**含成交当日** | `037d4b1` | 结果改变 |
| 2 | 退出时刻记为当日 00:00，可能早于入场时刻 | 统一为交易日 09:30 ET，保证 `exit_time >= entry_time` | `fb8a34b` | 结果改变 |
| 3 | A−B/C−B/D−C "同 setup 配对"对**嵌套分组恒为 0**，无法回答增量 | 改为**两组聚合期望差**（独立组 bootstrap）+ **年份同向**计数 | `b9c7f67` | 分析有效 |
| 4 | 集中度按**净收益总额**（净额≈0/负时失真，曾出现 199%/1611%） | 改为占**毛利**（正贡献之和）比例 | `68b517d` | 指标可用 |
| 5 | 报告缺资产分层，混层会得出相反结论 | 新增**资产类型切片**（普通股/ETF/杠杆 ETF）+ 集中度列 | `7843a6c` | 暴露跨资产相反方向 |
| 6 | 报告强制 A/B/C/D 四组，D 为空跑不了 | 支持 `--groups ABC`，132 单元；D 缺失输出 `LLM_INCREMENT_STATUS=inconclusive` | `169aff2` | 解阻 |
| 7 | 矩阵性能：2161 笔约 28 分钟 | 预解析时间 + numpy 内核 + 三仓向量化 → 约 1 分钟，**逐单元 0 差异** | `be26cf3` | 可用 |
| 8 | 部分脚本需 `PYTHONPATH=.` | 加 `sys.path` bootstrap | `b9c7f67` | 易用 |
| 9 | 手册 §12 的确定性检查无测试 | 补单测：成交当日 GAP_STOP、日内 STOP、新保护线**次日**生效、数据不足标 `DATA_END`、三仓按单元独立 | `b9c7f67` | 可回归 |

**分层关键发现**（`7843a6c`）：可交易样本仅 8 只（5 普通股 + 3 杠杆 ETF，普通 ETF 无成交）；**周线门方向一致为正，日线确认在普通股为正、杠杆 ETF 为负**——混层看会得出相反结论。

两份手册已就地同步到上述修正（`aefa445`，交接手册开头新增「变更记录」表，§8.3/§10/§12/§13/§17 改写）。

---

## 3. 工作流 A：历史证券主数据与普通股样本

### 3.1 阶段 2 — 证券主数据 v2（`a3a77f5`）

- `scripts/data/security_master_v2.py`：三表 schema（`security_master_v2` / `symbol_history` / `corporate_actions`）、标准化、区间重叠校验、`resolve_symbol`（按当时有效 ticker）、**跨源冲突检测**、`record_hash`、`audit_master`。
- `scripts/data/source_archive.py`：来源归档（不可覆盖 + `source_manifest.json` 哈希）。
- 4 个 CLI：`import_security_master_v2` / `import_symbol_history` / `import_corporate_actions` / `audit_security_master_v2`。
- 覆盖手册 §2.6 检查项 1–7：退市、改名、复用（含冲突样例）、拆股/反向拆股、并购、**来源冲突不静默覆盖**（写 `master_conflicts.csv` 并标 `quality_status=conflict`）。7 项测试。

### 3.2 阶段 3 — 双价格视图（`e8c45c7`）

- `scripts/data/price_views.py`：`raw`（原始可交易价）与 `asof_adjusted`（研究特征价）两条视图；行动因子（拆股 `1/ratio`、反向拆股、现金股息 `1−cash/prev_close`、分拆 `1−ratio`）；`action_version`、`corporate_action_flags`。
- 关键口径：**未来行动一律不应用**（as_of 早于除权日时调整后等于原始）；成交量反向调整使**美元成交额守恒**。11 项测试。

### 3.3 阶段 4 — 历史时点 universe v2（`c6250ff`）

- `scripts/historical_universe.py` 新增 `build_point_in_time_universe_v2`（**v1 接口保留**）+ `build_historical_universe_v2.py` CLI。
- 键为 `security_id`；改名不产生两只股票；**T−1 流动性门**（`liquidity_as_of < universe_date` 强制）；拒绝原因枚举 + 计数；主样本 = `eligible & asset_type=='stock'`。8 项测试。

### 3.4 阶段 5 — 正式实验 manifest 元数据（`f7b288d`）

- 新增 manifest 字段：`schema_version`/`formal`/`run_id`/`master_version`/`price_versions{raw,asof_adjusted}`/`evidence_version`/`acceptance`。
- `formal=true` 时强制齐全，否则 `PROVENANCE_INCOMPLETE`；CLI 加对应参数。

### 3.5 桥接层与已核验主数据模式（`0641de7`、`233b1a7`、`dac99c7`）

- `scripts/data/id_bridge.py` + `bridge_to_security_id.py` + `attach_security_id.py`：symbol ↔ `security_id` 映射，**复用窗口歧义不静默任选**、未映射单列输出。
- `run_daily_pipeline.py` 新增 `--verified-master`：**不用首根 K 线覆盖真实上市日**。
- 修了一个真 bug：setups 的 `setup_time` 带时区与 `symbol_history` 朴素日期比较会抛 `TypeError`，已统一为无时区日期。
- `security_id` 现**贯穿** setups → signals → exit_matrix → report。

### 3.6 富途接入（`7081c84`、`df21707`、`65b0b15`）

- `import_security_master_v2_from_futu.py`：富途当前目录 → v2 主数据 / ticker 历史。
- `import_corporate_actions_from_futu.py`：**直取**拆合股/分红（含 `pub_date`），优于 QFQ 反推；**不伪造 `observed_at`**。
- `derive_corporate_actions.py`：QFQ 与不复权价差**反推**行动（保留作交叉校验）。
- `probe_futu_source.py`：能力探针（只读）。
- **沙箱连 OpenD 的地址**：本机 `127.0.0.1` 在沙箱里是 VM 回环，连不上；**出口主机 `172.16.10.254:11111` 即本机**，加 `--host 172.16.10.254` 可连（实测 `get_global_state` ret=0）。

### 3.7 契约对齐：不纳入退市（`4531b12`、`8f7d65b`、`dd5f8cf`）

使用者决定范围收窄为**固定存续普通股样本**后，代码对齐：

- 主表去掉 `delisted_at`/`delisting_reason`/`source_published_at`；
- 审计与价格视图改用 **`corporate_action_unresolved`**（并购/分拆无 ratio/cash → 排除绩效）；
- universe 原因去掉 `DELISTED`、新增 `CORPORATE_ACTION_UNRESOLVED`；并把排除**并入 `tradable`**（原先 `eligible` 会被 `np.where(eligible,'ELIGIBLE',…)` 覆盖，是个真缺陷）；
- `experiment_manifest --formal` 新增必填 **`sample_selection_date` + `survivor_scope`**。

---

## 4. 工作流 B 阶段 6：历史证据快照（`7874f1b`）+ D 诊断（`a838bf9`）

- `scripts/evidence/`：`evidence_store.py`（双时间过滤、9 个拒绝原因 + 扩展 `DUPLICATE_EVENT`、事件簇去重与版本选择、`build_packet`、`packet_hash`、`validate_labels`、`audit_source`）+ 4 个 CLI。
- 覆盖手册 §3.6 的 12 项确定性测试（16:05 ET 公告不入当日决策包、修订不覆盖原版、多转载算一簇、packet 哈希敏感、引用包外 ID 失败、`missing` 不当决策、决策时点前移一分钟不新增未来事件…）。
- **不调用模型、不联网**：`replay_historical_selection` 只校验外部产出的标签。
- **D 诊断（真实富途财报）**：1302 条证据（39 只，2010–2026），`observed_at` 全空。结果：**严格层 0 条可用**；放宽后 500 个 setup 中 **75% 有 ≥1 条可见证据**。结论：**D 维持 `inconclusive`**，富途财报只能进诊断层。

---

## 5. 真实数据运行与实验

样本登记：`docs/sample-frame-registration-2026-09-12.md`（17 只主题清单 + 22 只板块代表股）。

| 实验 | 代码版本 | 样本 | 运行量 | 结果（成本 0.2%） | 决策 |
|---|---|---|---|---|---|
| `BUY-WD-ABC-EXP-001` | `aefa445` | 13 只试点 | 132 单元 | — | `inconclusive` |
| `BUY-WD-ABC-EXP-002` | `cfe4587`（另一会话） | 13 只 | 132 单元 / 95,084 行 | — | `inconclusive` |
| `BUY-WD-ABC-SURVIVOR-001` | `a9423fc` | 17 只主题 | 入场 7,879 | E3 A=203/B=155/C=138；**B−A 4/44、C−B 8/44 为正** | `inconclusive` |
| `BUY-WD-ABC-SURVIVOR-002` | `dac99c7` | 17 只 | 入场 7,889 | E3 A=213/B=155/C=138；4/44、8/44 | `inconclusive` |
| `BUY-WD-ABC-SURVIVOR-003` | `eddd87b` | **39 只** | 入场 21,805 / 矩阵 959,420 行 | E3 A=128/B=135/C=83；**B−A 16/44、C−B 0/44** | `inconclusive` |

**较稳的读法**：**日线确认（C−B）在两种样本上都一致为负**；周线门方向不稳（17 只 4/44，39 只 16/44）。集中度 39 只时改善到 21%–83%。

数据落点（`data/` 已 gitignore，仅在本机）：
- 原始缓存 `data/market_history/raw/day/qfq/`（520 分区，6.9 MB，48 标的）
- run 快照 `data/market_history/runs/SURVIVOR39-QFQ-20260912-b/`（47 MB）
- 冻结实验 `backtests/buy_v2/BUY-WD-ABC-SURVIVOR-00{1,2,3}/`

---

## 6. 发现并修复的真实缺陷汇总

| 类别 | 缺陷 | 处理 |
|---|---|---|
| 时序 | 退出模拟丢弃成交当日 K 线 | 按交易日选取，含当日 |
| 时序 | 退出时刻早于入场时刻 | 统一 09:30 ET |
| 统计 | 嵌套分组配对增量恒为 0 | 改聚合期望差 + 年份同向 |
| 统计 | 集中度按净额，净额≈0 失真 | 改占毛利 |
| 混淆 | 混层报告掩盖跨资产相反方向 | 资产类型切片 |
| universe | 未解析公司行动仍 `eligible` | 并入 `tradable` |
| 桥接 | 时区比较 `TypeError` | 统一无时区日期 |
| 数据质量 | 富途把 NFLX/AMT/PLD 的 `stock_type` 误标 ETF | 名称含 fund 关键词才判 ETF（**需词边界**，'Netflix' 含子串 'etf'） |
| 性能 | 矩阵 28 分钟 | numpy 内核 + 向量化 → 1 分钟（等价） |
| 运维 | 脚本需 `PYTHONPATH` | 加 bootstrap |

---

## 7. 环境限制与未完成项

**环境限制**
- **Futu OpenD**：沙箱内 `127.0.0.1` 不可达，须用 `--host 172.16.10.254`。
- **富途不提供可信 US 上市日**：13 只里 8 只是 `1970-01-01` 占位 → "已核验上市日"模式不可用，只能推断并标 `unverified`。
- **决策账本 SQLite 单写者**：`data/execution.sqlite3` 与本机实时系统共用，沙箱并发访问会 `unable to open database file` / `disk I/O error` → **前向 shadow 必须在本机跑**。
- **QFQ 是快照**：未来拆股/分红会让 Futu 重算历史复权价；复用缓存可复现，重下不保证一致。

**未完成（需真实数据/时间，非代码）**
1. **含退市的历史主数据**（消除幸存者偏差）——使用者明确不做。
2. **D 组的真实历史标签**——富途无 `observed_at`，严格层 0 可用。
3. **前向 shadow**——需真实交易日累积（≥2 交易日、≥3 批次为联调底线）。
4. 样本 39 只虽达门槛下限，结论仍受幸存者偏差限制。

---

## 8. 关键命令速查

**A/B/C 历史实验（本机 OpenD）**
```bash
python3 scripts/data/import_security_master_v2_from_futu.py --output-dir data/security_master_runs/<ver> [--host 172.16.10.254]
python3 scripts/data/run_daily_pipeline.py --master <master> --verified-master --run-id <run> [--host 172.16.10.254]
python3 scripts/data/bridge_to_security_id.py --run-dir <run> --symbols <symbol_history> --output-dir <bridge>
python3 scripts/data/attach_security_id.py --frame <setups> --symbols <symbol_history> --symbol-col stock --date-col setup_time --output <setups_v2>
python3 scripts/data/build_historical_universe_v2.py --master ... --symbols ... --liquidity ... --calendar ... --output-dir <universe>
python3 scripts/experiment_manifest.py ... --formal --run-id ... --master-version ... --raw-price-version ... --asof-price-version ... --sample-selection-date ... --survivor-scope --acceptance <json>
python3 scripts/buy_strategy_experiment_runner.py --manifest ... --setups ... --universe ... --output-dir .../entries
python3 scripts/exit_matrix.py --manifest ... --entries .../signals.csv --daily ... --groups ABC --output .../exit_matrix.csv
python3 scripts/buy_strategy_report.py --matrix .../exit_matrix.csv --manifest ... --groups ABC --output-dir .../report
```

**证据/诊断层**
```bash
python3 scripts/evidence/import_earnings_from_futu.py --codes ... --host 172.16.10.254 --output-dir <dir>
python3 scripts/evidence/import_historical_evidence.py --source <dir> --output-dir <store>
python3 scripts/evidence/build_historical_packet.py --setups ... --evidence .../evidence.jsonl --diagnostic --output-dir <packets>
```

**前向 shadow（必须在本机）**：见 `docs/forward-shadow-runbook-2026-09-12.md`。

---

## 9. 新增文件清单（本会话）

**脚本（22）**：`security_master_v2.py`、`source_archive.py`、`price_views.py`、`id_bridge.py`、`attach_security_id.py`、`bridge_to_security_id.py`、`build_price_views.py`、`build_historical_universe_v2.py`、`import_security_master_v2.py`、`import_symbol_history.py`、`import_corporate_actions.py`、`audit_security_master_v2.py`、`import_security_master_v2_from_futu.py`、`import_corporate_actions_from_futu.py`、`derive_corporate_actions.py`、`probe_futu_source.py`、`scripts/evidence/*`（7 个）。

**文档（7）**：`historical-universe-and-evidence-handoff-technical-design-2026-09-12.md`、`security-master-v2-stage2-implementation-2026-09-12.md`、`evidence-snapshot-stage6-implementation-2026-09-12.md`、`sample-frame-registration-2026-09-12.md`、`source-assessment-template-2026-09-12.md`、`handoff-status-2026-09-12.md`、`forward-shadow-runbook-2026-09-12.md`。

**测试（11 个文件 + fixture）**：`test_security_master_v2`、`test_price_views`、`test_universe_v2`、`test_id_bridge`、`test_import_security_master_v2_from_futu`、`test_derive_corporate_actions`、`test_corporate_actions_from_futu`、`test_evidence_store`、`test_evidence_cli`、`test_earnings_evidence_from_futu`（+ `tests/fixtures/security_master_v2/`）。
