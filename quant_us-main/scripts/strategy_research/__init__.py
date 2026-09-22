"""买入/卖出/LLM 增量的隔离研究（规划 `next-system-implementation-plan-2026-09-21.md`）。

与 `strategy_diagnostics` 的分工：那边是**基线诊断**（只描述、不改规则），
这边是**单项改动的研究与对照**（一次只改一处，预登记固定参数与门槛）。

隔离要求（规划 §3.1）：本包只在独立 checkout/worktree 里改引擎。现行三臂前向观察
（`B3-FORWARD-20260921`）的冻结哈希包含 `paper_engine.py`/`schema.py` 等文件 ——
**那些文件在本分支被改动，因此本分支的代码不得用于推进已开始的前向观察**。
"""
