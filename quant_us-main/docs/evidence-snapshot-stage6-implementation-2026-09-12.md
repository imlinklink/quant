# 历史证据快照（工作流 B 阶段 6）落地记录（2026-09-12）

依据 `historical-universe-and-evidence-handoff-technical-design-2026-09-12.md` §3.1–§3.6。提交：`7874f1b`。状态：**engineering_pass**（契约与质量门通过；无真实历史来源，不产出标签、不调用模型）。

## 交付物

| 文件 | 作用 |
|---|---|
| `scripts/evidence/evidence_store.py` | 核心库：双时间过滤、拒绝原因、事件簇去重/版本选择、`build_packet`、`packet_hash`、`validate_labels`、`audit_source`、`market_time/market_close` |
| `scripts/evidence/import_historical_evidence.py` | CLI：来源目录 → `evidence.jsonl`（不可覆盖，`--dry-run`） |
| `scripts/evidence/audit_historical_evidence.py` | CLI：来源可用性审计 → `source_quality.json`；未通过退出码 1 |
| `scripts/evidence/build_historical_packet.py` | CLI：setups → `packets/<setup_id>.json` + `.sha256` + `index.json` + `exclusions.json` |
| `scripts/evidence/replay_historical_selection.py` | CLI：离线标签校验骨架；不给标签只产出 `job.json`，**不生成标签、不调用模型** |
| `tests/unit/live/test_evidence_store.py` | §3.6 的 12 项确定性测试 |
| `tests/unit/live/test_evidence_cli.py` | 导入→审计→建包→回放 端到端冒烟 |

## 双时间与拒绝原因

严格层要求 `published_at <= decision_cutoff` **且** `observed_at <= decision_cutoff`。拒绝逐条记录原因：`FUTURE_PUBLICATION / FUTURE_OBSERVATION / OBSERVED_AT_UNPROVEN / REVISED_AFTER_CUTOFF / SOURCE_UNVERIFIED / LICENSE_RESTRICTED / SYMBOL_AMBIGUOUS / STALE / CONFLICT`，另加 **`DUPLICATE_EVENT`**（同一源事件的重复转载合并计一次；此为对设计 §3.4 原因枚举的一处显式扩展，已在此记录）。

## §3.6 检查项对应

1. 16:05 ET 公告不入当日 16:00 ET 决策包，可入下一时点；2. `event_at` 在过去但 `published_at` 在未来仍拒绝；3. `published_at` 在过去但 `observed_at` 在未来仍拒绝；4. 缺 `observed_at` 严格层拒绝（诊断层可放行）；5. 修订版本不能覆盖原版本（早期时点只见 v1，后期 v1 记 `REVISED_AFTER_CUTOFF`）；6. ticker 按 `resolver` 正确归属，歧义记 `SYMBOL_AMBIGUOUS`；7. 多转载只算一个事件簇；8. 时区/夏令时/半日市用例（16:00 EST=21:00Z、16:00 EDT=20:00Z、半日市 13:00 ET）；9. packet 哈希稳定且对内容/时间敏感；10. 引用包外 ID 校验失败；11. `missing` 不得当决策；12. 决策时点前移一分钟不新增未来事件（单调性）。

## 复现命令（本地 fixture）

```bash
python3 scripts/evidence/import_historical_evidence.py --source <dir含evidence.csv> --output-dir <store>
python3 scripts/evidence/audit_historical_evidence.py --evidence <store>/evidence.jsonl --output-dir <qa>
python3 scripts/evidence/build_historical_packet.py --setups <setups.csv> --evidence <store>/evidence.jsonl --output-dir <packets>
python3 scripts/evidence/replay_historical_selection.py --packets <packets> [--labels <labels.jsonl>] --output-dir <out>
```

全量测试：533 passed。

## 边界（重要）

- **不调用模型、不联网**：`replay_historical_selection` 只校验外部产出的标签；未给标签时仅产出待标注清单。
- **无真实历史来源**：仓库内没有历史公告/财报/新闻/期权快照来源，本阶段只验证契约与质量门，**不能**产出可用于 D 组的标签。
- **模型后见知识限制**：即便 prompt 只含历史证据，2026 年模型参数仍可能记得未来；历史 D 组只能称"受限证据的回放探索"，验证 LLM 真实增量的最强证据来自样本冻结后的**前向 shadow**。
- 首轮 D 的研究级门槛应**预先登记**（严格层时间完整率、可追溯原文比例、packet 构建成功率、有效引用率、缺失率、覆盖度）；达不到就保持 D=`inconclusive`。
