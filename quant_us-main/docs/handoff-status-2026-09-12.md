# 交接状态总览（2026-09-12）

一页看清：**已完成 / 待数据 / 待前向**。项目目录 `quant_us-main`；Git 仓库根在其父目录 `quant`。

- 当前 HEAD：`65b0b15`
- 全量测试：**540 passed**
- 总体状态：**engineering_pass**（契约与质量门通过；不产出策略结论）
- 数据源：**富途 OpenD**；范围 = **固定存续普通股样本，不纳入退市**（存在幸存者偏差）

> 结论边界（务必保留）：历史回测只说明策略在**所选存续股票**中的表现，不代表当时全市场，也不能消除幸存者偏差；更强的验证应从样本冻结后**前瞻运行**。历史 D 组只能称"受限证据的回放探索"。

## 工作流 A：证券主数据与存续普通股样本 —— 工程代码已完成

| 阶段 | 交付物 | 关键文件 | 测试 |
|---|---|---|---|
| 1 | 数据源评估与样本框架登记（模板已填，含 Futu 能力与局限） | `docs/source-assessment-template-2026-09-12.md`、`docs/sample-frame-registration-2026-09-12.md` | — |
| 2 | 主数据 v2（security_id / ticker 历史 / 公司行动 / 冲突审计） | `scripts/data/security_master_v2.py`、`source_archive.py`、`import_security_master_v2.py`、`import_symbol_history.py`、`import_corporate_actions.py`、`audit_security_master_v2.py` | 7 项 |
| 3 | 双价格视图（原始可交易价 / as-of 特征价）+ 公司行动一致性 | `scripts/data/price_views.py`、`build_price_views.py` | 11 项 |
| 4 | 历史时点 universe v2（security_id 贯穿、T−1 流动性门、拒绝原因） | `scripts/historical_universe.py`（v2 函数）、`scripts/data/build_historical_universe_v2.py` | 8 项 |
| 5 | 正式实验 manifest 元数据（主数据/价格版本、样本选择日、存续限定、验收标准） | `scripts/experiment_manifest.py` | 5 项 |
| 富途接入 | 富途当前目录 → v2 主数据；QFQ/不复权反推公司行动；能力探针 | `scripts/data/import_security_master_v2_from_futu.py`、`derive_corporate_actions.py`、`probe_futu_source.py` | 10 项 |
| 桥接层 | symbol→security_id 映射（复用窗口歧义不静默任选）；`run_daily_pipeline` 已核验主数据模式（不覆盖真实上市日） | `scripts/data/id_bridge.py`、`bridge_to_security_id.py`、`run_daily_pipeline.py` | 4 项 |
| 富途直取公司行动 | `get_corporate_actions_stock_splits`/`_dividends` 直接生成 `corporate_actions.csv`（含 `pub_date`；不伪造 `observed_at`），优于 QFQ 反推 | `scripts/data/import_corporate_actions_from_futu.py` | 3 项 |

> **沙箱连 OpenD**：本机（Mac）的 `127.0.0.1` 在沙箱里是 VM 自己的回环，连不上；沙箱出口主机 `172.16.10.254` 即本机，OpenD 在其 `11111`。跑富途脚本加 `--host 172.16.10.254`（实测可连）。

**富途命令链（需本机 OpenD）**

```bash
# 1) v2 主数据与 ticker 历史（当前存续证券）
python3 scripts/data/import_security_master_v2_from_futu.py \
  --output-dir data/security_master_runs/<ver> --run-id <run>

# 2) 下载日线/流动性（现有管道，symbol 键；--verified-master 不用首根 K 线覆盖真实上市日）
python3 scripts/data/run_daily_pipeline.py --master <v1 master> --verified-master \
  --start ... --end ... --universe-start ... --universe-end ... --run-id <run>

# 3) 桥接为 security_id 键
python3 scripts/data/bridge_to_security_id.py --run-dir data/market_history/runs/<run> \
  --symbols data/security_master_runs/<ver>/symbol_history.csv --output-dir <bridge>

# 4) 由 QFQ 与不复权价差反推公司行动（标 unverified，需人工复核）
python3 scripts/data/derive_corporate_actions.py --raw <不复权> --adjusted <QFQ> \
  --security-id <SEC-...> --output <actions.csv>

# 5) 双价格视图 -> 历史时点 universe v2
python3 scripts/data/build_price_views.py --bars <bridge>/daily_v2.csv.gz --actions <actions> \
  --as-of <日期> --output-dir <双视图>
python3 scripts/data/build_historical_universe_v2.py --master ... --symbols ... \
  --liquidity <bridge>/daily_liquidity_v2.csv.gz --calendar ... --output-dir <universe>

# 6) 冻结正式实验（--formal 强制 run-id/主数据版本/双价格版本/样本选择日/存续限定/验收标准）
python3 scripts/experiment_manifest.py ... --formal --run-id <run> --master-version <ver> \
  --raw-price-version <v> --asof-price-version <v> --sample-selection-date <YYYY-MM-DD> --survivor-scope
python3 scripts/buy_strategy_experiment_runner.py --manifest ... --setups ... --universe ... --output-dir .../entries
python3 scripts/exit_matrix.py --manifest ... --entries .../signals.csv --daily ... --groups ABC --output .../exit_matrix.csv
python3 scripts/buy_strategy_report.py --matrix .../exit_matrix.csv --manifest ... --groups ABC --output-dir .../report
```

正式实验编号建议 `BUY-WD-ABC-SURVIVOR-001`（与旧 `EXP-001/002` 分离，不得互相覆盖）。

## 工作流 B：历史证据快照 —— 契约已完成，真实来源待接入

| 阶段 | 状态 | 关键文件 |
|---|---|---|
| 6 证据快照 | **已完成（engineering_pass）** | `scripts/evidence/evidence_store.py` + `import_historical_evidence.py`、`audit_historical_evidence.py`、`build_historical_packet.py`、`replay_historical_selection.py`；12 项 + CLI 冒烟 |
| 7 真实来源导入与离线标签 | **待数据源** | 需历史公告/财报/新闻来源（含 `published_at`/`observed_at` 可审计时间） |
| 8 ABCD 探索 + 前向 shadow | **待前向运行** | 需样本冻结后累积独立决策与到期结果 |

**证据命令链**

```bash
python3 scripts/evidence/import_historical_evidence.py --source <dir含evidence.csv> --output-dir <store>
python3 scripts/evidence/audit_historical_evidence.py --evidence <store>/evidence.jsonl --output-dir <qa>
python3 scripts/evidence/build_historical_packet.py --setups <setups.csv> --evidence <store>/evidence.jsonl --output-dir <packets>
python3 scripts/evidence/replay_historical_selection.py --packets <packets> [--labels <labels.jsonl>] --output-dir <out>
```

## 待使用者决策 / 待外部条件

1. **历史证据来源**（工作流 B 阶段 7）：仓库内没有可用来源；决定是否接入并核对其研究/本地保存许可。
2. **本机跑富途链路**：启动 OpenD（`127.0.0.1:11111`，需美股历史行情权限）后按上面命令链生成真实扩池数据。
3. **前向 shadow**（阶段 8）：样本冻结后至少两个独立交易日、三个有效批次才算管道联调底线；收益评估需更长时间。
4. 首轮 D 的研究级门槛应**预先登记**（严格层时间完整率、可追溯原文比例、packet 构建成功率、有效引用率、缺失率、覆盖度），达不到就保持 D=`inconclusive`。

## 已知运维注意

- 脚本可直接 `python3 scripts/X.py` 运行（已加 `sys.path` bootstrap），无需 `PYTHONPATH=.`。
- `.git/index.lock` 曾多次出现陈旧锁；提交报 "Another git process" 时先清理 `.git/*.lock`。
- 任何代码或文档提交都会让既有实验 manifest 出现 `GIT_COMMIT_MISMATCH`（设计使然）；**不要**改旧 manifest 的 commit，复现应在独立工作树检出记录的 commit。
