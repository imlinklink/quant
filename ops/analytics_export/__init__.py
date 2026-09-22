"""只读决策可视化：导出层。

把散在四个账本里的事实导出成 **JSON 快照**，供 web 层只读消费。存在的理由是
`docs/decision-visibility-product-requirements-2026-09-22.md` §6 的那条硬约束：

> L1 与主 checkout 存在账本 schema 差异。优先由各自固定运行版本提供只读导出，
> Web 消费统一快照；**不得用主项目 ORM 强开不兼容账本**。

实测：`ShadowStore.transaction()` 的版本守卫**对读也生效**（`store.py` 的
`LEDGER_SCHEMA_MISMATCH`），而库里同时存在 schema 9（M1、三臂）与 schema 10（L1）的
账本 ⇒ **没有任何一个 checkout 能用 ORM 打开全部账本**。所以本包：

- 只用**原始 SQL + `mode=ro`** 读投影列与 `body` JSON（照 `ops/watchdog.py` 的
  `budget_check` 先例，那是本项目第一次跨 schema 只读）；
- **不 import 任何 `scripts.*`**（有一个测试钉死这条）—— 因为两个 checkout 的
  `llm_overlay` / `position_overlay` / `store` 词表与字段并不相同，从别的 checkout
  借词表去解释这份账本会把「预算耗尽」这类弃权静默算成「模型参与过判断」；
- 指标算法仍复用 `scripts/portfolio_shadow/report.py`，通过传入自带词表实现
  （`metrics_vocabulary`），**同一件事只有一份定义**。
"""
