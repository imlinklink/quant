"""
LLM Advisor - 统一 LLM API 调用接口
====================================

设计要点:
  - 降级友好：任何异常都返回 None，不让主流程挂
  - JSON Schema 校验：LLM 输出必须过 schema 才能用
  - 超时保护：默认 30 秒，避免阻塞交易线程
  - 可开关：enabled=False 时所有方法立即返回 None
  - 环境变量取 key：安全，不写死在 config 里
"""
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from mutifactor.llm.schemas import (
    CANDIDATE_VERDICT_SCHEMA,
    BUY_VETO_SCHEMA,
    MARKET_STATUS_SCHEMA,
)
from mutifactor.llm.prompts import DEFAULT_SYSTEM_PROMPT

logger = logging.getLogger('llm')

# 尝试导入 jsonschema，没有就跳过校验
try:
    from jsonschema import validate as _jsonschema_validate, ValidationError
    _HAS_JSONSCHEMA = True
except ImportError:
    _HAS_JSONSCHEMA = False
    logger.debug("jsonschema 未安装，LLM 输出将只做 json.loads 校验（pip install jsonschema 可增强）")


SCHEMA_MAP = {
    'candidate_review': CANDIDATE_VERDICT_SCHEMA,
    'buy_veto': BUY_VETO_SCHEMA,
    'market_status': MARKET_STATUS_SCHEMA,
}


class LLMAdvisor:
    """大模型顾问 - 统一封装 API 调用"""

    def __init__(self, config: Dict[str, Any]):
        """
        Args:
            config: config.yaml 里的 llm 段
        """
        self.enabled: bool = bool(config.get('enabled', False))
        # 支持 ${VAR} 和 $VAR 两种环境变量写法
        self.api_key: str = self._expand_env(config.get('api_key', '')) or os.environ.get('DEEPSEEK_API_KEY', '')
        self.base_url: str = self._expand_env(config.get('base_url', 'https://api.deepseek.com'))
        self.model: str = config.get('model', 'deepseek-chat')
        self.timeout: int = config.get('timeout', 30)
        self.max_retries: int = config.get('max_retries', 2)

        if self.enabled and not self.api_key:
            logger.warning("⚠️ LLM 已启用但未配置 api_key（检查 config.yaml 或环境变量 DEEPSEEK_API_KEY），自动降级为禁用")
            self.enabled = False
        elif self.enabled and self.api_key.startswith('${'):
            logger.warning(f"⚠️ api_key 值未被展开（字面量 {self.api_key}），检查环境变量是否设置，自动降级为禁用")
            self.enabled = False

        logger.info(f"LLM Advisor 初始化完成: enabled={self.enabled}, model={self.model}")

    @staticmethod
    def _expand_env(value: str) -> str:
        """展开 ${VAR} 或 $VAR 形式的环境变量引用"""
        if not isinstance(value, str):
            return value
        import re
        # ${VAR}
        def _sub(m):
            return os.environ.get(m.group(1), '') or m.group(0)
        expanded = re.sub(r'\$\{(\w+)\}', _sub, value)
        # $VAR（简单名）
        expanded = re.sub(r'\$(\w+)', lambda m: os.environ.get(m.group(1), '') or m.group(0), expanded)
        return expanded

    # ==================== 基础接口 ====================

    def chat(
        self,
        prompt: str,
        schema_name: str = None,
        system: str = None,
        expect_json: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        统一 chat 接口。

        Args:
            prompt: 用户 prompt
            schema_name: 对应 SCHEMA_MAP 的 key（candidate_review / buy_veto / market_status）
            system: 自定义 system prompt，默认用 DEFAULT_SYSTEM_PROMPT
            expect_json: 是否要求 JSON 输出

        Returns:
            解析后的 dict（如果 expect_json=True）或原始文本，失败返回 None
        """
        if not self.enabled:
            return None

        system = system or DEFAULT_SYSTEM_PROMPT
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        if expect_json:
            payload["response_format"] = {"type": "json_object"}

        last_error = None
        for attempt in range(self.max_retries):
            try:
                raw = self._request(payload)
                logger.info(f"[LLM] OK [{self.model}] prompt={prompt[:80].replace(chr(10),' ') if len(prompt)>80 else prompt}...")

                if expect_json:
                    parsed = json.loads(raw)
                    # JSON Schema 校验（如果有 jsonschema 库且提供了 schema_name）
                    if schema_name and _HAS_JSONSCHEMA:
                        schema = SCHEMA_MAP.get(schema_name)
                        if schema:
                            try:
                                _jsonschema_validate(instance=parsed, schema=schema)
                            except ValidationError as e:
                                logger.warning(f"[LLM] Schema 校验失败: {e.message}，降级回纯规则")
                                return None
                    return parsed
                return raw

            except json.JSONDecodeError as e:
                logger.warning(f"[LLM] JSON 解析失败 (尝试 {attempt+1}): {e}")
                last_error = e
            except Exception as e:
                logger.warning(f"[LLM] 调用失败 (尝试 {attempt+1}): {type(e).__name__}: {e}")
                last_error = e

            if attempt < self.max_retries - 1:
                time.sleep(1 * (attempt + 1))

        logger.error(f"[LLM] 全部 {self.max_retries} 次尝试失败: {last_error}")
        return None

    def _request(self, payload: Dict) -> str:
        """发起 HTTP 请求"""
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode('utf-8'),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method='POST',
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            body = e.read().decode('utf-8', errors='replace') if e.fp else ''
            raise RuntimeError(f"HTTP {e.code}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}") from e

        # 提取内容（兼容不同模型返回格式）
        choices = data.get('choices', [])
        if not choices:
            raise RuntimeError(f"API 返回无 choices: {data}")
        message = choices[0].get('message', {})
        content = message.get('content', '')
        if content is None:
            content = ''
        return content.strip()

    # ==================== 业务便捷接口 ====================

    def assess_candidates(
        self,
        candidates: list,
        context_snapshot: str,
        date: str,
        holdings: list = None,
    ) -> Optional[Dict[str, Any]]:
        """接入点 A: 选股候选复核"""
        if not self.enabled:
            return None
        from mutifactor.llm.prompts import CANDIDATE_REVIEW_PROMPT
        prompt = CANDIDATE_REVIEW_PROMPT.format(
            candidate_snapshot=context_snapshot,
            date=date,
            holdings=holdings or [],
        )
        return self.chat(prompt, schema_name='candidate_review')

    def veto_buy(
        self,
        buy_list: list,
        holdings: list = None,
        cash: float = 0,
        market_context: str = '',
    ) -> Optional[Dict[str, Any]]:
        """接入点 B: 买入前市场风险否决"""
        if not self.enabled:
            return None
        from mutifactor.llm.prompts import BUY_VETO_PROMPT
        prompt = BUY_VETO_PROMPT.format(
            buy_list=buy_list,
            holdings=holdings or [],
            cash=f"{cash:.2f}",
            market_context=market_context or "无特殊事件",
        )
        return self.chat(prompt, schema_name='buy_veto')

    def judge_market_status(self, date: str, market_data: str = '') -> Optional[Dict[str, Any]]:
        """接入点 C: 盘前市场状态判断"""
        if not self.enabled:
            return None
        from mutifactor.llm.prompts import MARKET_STATUS_PROMPT
        prompt = MARKET_STATUS_PROMPT.format(date=date, market_data=market_data or "无特殊数据")
        return self.chat(prompt, schema_name='market_status')
