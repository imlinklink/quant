# M0 里程碑记录：冻结本轮复核代码（2026-09-12）

依据 `next-stage-handoff-plan-2026-09-12.md` §3。

## 执行记录

| 项目 | 值 |
|---|---|
| 执行日期 | 2026-09-12 |
| Commit | **`12c06a7`** |
| 工作区 | 干净（`git status --short` 无输出） |
| `git diff --check` | 无输出 |
| 全量测试 | **548 passed, 16 warnings**（富途库可写本机日志的受控环境） |
| 前序冻结实验 | `BUY-WD-ABC-SURVIVOR-001/002/003` 保持**只读**，未改动 Manifest 或产物 |

## 本次冻结的内容（复核修正）

| 文件 | 修正 |
|---|---|
| `scripts/data/price_views.py` | `asof_adjusted` 视图**必须显式 `--as-of`**（否则 `AS_OF_REQUIRED_FOR_ADJUSTED_VIEW`），且**只保留 `session <= as_of` 的 bar**（快照不得含未来 bar） |
| `scripts/data/build_price_views.py` | `--as-of` 改为必填 |
| `scripts/evidence/evidence_store.py` | `_normalize_digest`：来源已给 SHA-256 时**不再二次哈希**；`availability_proof` 逐行校验（含 NaN/''/'none'）；`_resolve` 以 ticker 为准，**ticker 与记录 ID 冲突判 `SYMBOL_AMBIGUOUS`**；packet 记录 `evidence_mode`（strict/diagnostic） |
| `scripts/evidence/replay_historical_selection.py` | **诊断包禁止按严格层回放**（`STRICT_REPLAY_REQUIRES_STRICT_PACKETS`） |
| `tests/unit/live/*` | 新增/调整 4 项测试（诊断包禁严格回放、hash 不重复、ticker 冲突、availability_proof） |

## 变更清单（本会话，供 M1 参考）

- 首个提交 `acdbc85`；本轮起点 HEAD `909d485`。
- 工作流 A：主数据 v2、双价格视图、universe v2、manifest 元数据、桥接层、富途接入。
- 工作流 B：证据快照（阶段 6）+ D 诊断。
- 详细清单见 `handoff-work-summary-2026-09-12.md`。

## 下一步

**M1 数据真实性审计**（39 只候选证券）——与 M3 前向 shadow 可并行；M3 必须在**拥有账本 `data/execution.sqlite3` 的本机**执行（单写者，沙箱并发会 I/O 报错）。

放行前提醒（§4.3）：上市日未知的历史区间不得标 `verified`；执行价须来自**不复权**序列，特征价按**每个决策时点**构造；未解析公司行动与缺口须计数并阻断。
