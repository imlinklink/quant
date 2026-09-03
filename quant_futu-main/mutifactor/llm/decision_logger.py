"""
Decision Logger - LLM 决策日志
==============================

每次 LLM 调用都记录：
  - timestamp + model + trigger（哪个接入点触发）
  - snapshot（当时的系统状态快照）
  - prompt + output（可回放）
  - adopted（是否被采纳，影子模式下固定 false）
  - latency_ms

存储格式: YAML（每日一个文件，便于人眼查看）
存储目录: data/llm_decisions/
"""
import hashlib
import logging
import os
import time
from datetime import datetime, date
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger('llm')


class DecisionLogger:
    """决策日志记录器"""

    def __init__(self, log_dir: str = None):
        """
        Args:
            log_dir: 日志目录，默认 data/llm_decisions/
        """
        if log_dir is None:
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            log_dir = os.path.join(project_root, 'data', 'llm_decisions')
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        logger.info(f"[DecisionLogger] 日志目录: {self.log_dir}")

    # ========== 记录方法 ==========

    def record(
        self,
        trigger: str,
        snapshot: str,
        prompt: str,
        llm_output: Optional[Dict[str, Any]],
        latency_ms: int,
        model: str = "deepseek-chat",
        adopted: bool = False,
        final_action: str = '',
    ) -> str:
        """
        记录一次 LLM 调用。

        Args:
            trigger: 触发源（candidate_review / buy_veto / market_status）
            snapshot: 系统状态快照文本
            prompt: 完整 prompt（用于回放）
            llm_output: LLM 原始输出（dict 或 None）
            latency_ms: 耗时毫秒
            model: 使用的模型名
            adopted: 影子模式下固定 False，Phase 2+ 才会 True
            final_action: 最终采取的行动（如 "vetoed HK.00700" / "按原计划执行"）

        Returns:
            记录 ID（prompt hash，便于查询）
        """
        record_id = hashlib.sha256(
            f"{trigger}|{prompt[:200]}|{time.time_ns()}".encode()
        ).hexdigest()[:12]

        entry = {
            'record_id': record_id,
            'timestamp': datetime.now().isoformat(timespec='seconds'),
            'model': model,
            'trigger': trigger,
            'latency_ms': latency_ms,
            'prompt_hash': hashlib.sha256(prompt.encode()).hexdigest()[:12],
            'snapshot': snapshot,
            'prompt': prompt,
            'llm_output': llm_output if llm_output else None,
            'adopted': adopted,
            'final_action': final_action,
        }

        try:
            filepath = self._get_today_path()
            # 追加模式
            existing = self._read_existing(filepath)
            existing.append(entry)
            with open(filepath, 'a', encoding='utf-8') as f:
                yaml.dump([entry], f, allow_unicode=True, sort_keys=False)
            logger.info(
                f"[DecisionLogger] 记录 #{record_id} "
                f"trigger={trigger} latency={latency_ms}ms adopted={adopted}"
            )
        except Exception as e:
            logger.error(f"[DecisionLogger] 记录失败: {e}")

        return record_id

    # ========== 内部方法 ==========

    def _get_today_path(self) -> str:
        today = date.today().isoformat()
        return os.path.join(self.log_dir, f"llm_{today}.yaml")

    @staticmethod
    def _read_existing(filepath: str) -> list:
        if not os.path.exists(filepath):
            return []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, list) else []
        except Exception:
            return []
