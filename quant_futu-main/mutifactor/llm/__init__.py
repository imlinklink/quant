"""
LLM 顾问模块 - 大模型决策覆盖层
================================

设计原则:
  1. LLM 只产出"降级/剔除/观望"，永远不能新增买入或放大仓位
  2. 超时、失败一律降级回纯规则
  3. 完全可开关，config 一关就回到纯量化模式
  4. 所有决策落日志，可回放可审计

模块结构:
  - advisor.py          统一 LLM API 调用接口
  - schemas.py          JSON Schema 定义（强制校验 LLM 输出）
  - prompts.py          Prompt 模板
  - context_builder.py  系统状态快照打包
  - decision_logger.py  决策日志（输入 + 输出 + 是否采纳）
"""
from mutifactor.llm.advisor import LLMAdvisor
from mutifactor.llm.decision_logger import DecisionLogger
from mutifactor.llm.context_builder import ContextBuilder

__all__ = [
    'LLMAdvisor',
    'DecisionLogger',
    'ContextBuilder',
]
