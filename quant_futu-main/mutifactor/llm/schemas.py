"""
JSON Schema 定义 - 强制校验 LLM 输出
====================================

每个接入点的 LLM 输出必须通过对应 schema 校验，
校验失败视为 LLM 失败 → 降级回纯规则。
"""

# ========== 接入点 A: 选股候选复核 ==========
# LLM 对候选股做否决，只能 veto，不能新增
CANDIDATE_VERDICT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "vetoed", "reason"],
    "properties": {
        "verdict": {
            "enum": ["pass", "veto", "watch"],
            "description": "pass=全部通过, veto=有要否决的, watch=观望但不否决"
        },
        "vetoed": {
            "type": "array",
            "items": {"type": "string", "pattern": "^HK\\.\\d{5}$|^US\\.[A-Z]{1,5}$|^[A-Z]{1,5}$"},
            "description": "建议否决的股票代码列表"
        },
        "reason": {
            "type": "string",
            "maxLength": 200,
            "description": "简短理由，说明否决原因（负面新闻/行业政策/业绩暴雷等）"
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "信心度 0-1，低于 0.5 的 veto 不生效（保护误判）"
        }
    },
    "additionalProperties": False,
}

# ========== 接入点 B: 买入前市场风险否决 ==========
BUY_VETO_SCHEMA = {
    "type": "object",
    "required": ["verdict", "reason"],
    "properties": {
        "verdict": {
            "enum": ["allow", "delay", "block"],
            "description": "allow=允许买入, delay=暂缓(可轻仓), block=否决买入"
        },
        "reason": {
            "type": "string",
            "maxLength": 200,
        },
        "risk_level": {
            "enum": ["LOW", "MEDIUM", "HIGH", "EXTREME"],
            "description": "市场整体风险等级"
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "信心度 0-1，低于 min_confidence_to_veto 的 verdict 不生效"
        },
    },
    "additionalProperties": False,
}

# ========== 接入点 C: 盘前市场状态判断 ==========
MARKET_STATUS_SCHEMA = {
    "type": "object",
    "required": ["market_type", "risk_note"],
    "properties": {
        "market_type": {
            "enum": ["trending_bull", "trending_bear", "range", "volatile", "unknown"],
            "description": "趋势市/熊市/震荡/高波动"
        },
        "risk_note": {
            "type": "string",
            "maxLength": 300,
        },
        "suggested_max_positions": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "建议持仓数（规则层会硬边界校验，LLM 说 5 也会被限制）"
        },
        "suggested_stop_multiplier": {
            "type": "number",
            "minimum": 1.0,
            "maximum": 4.0,
        },
    },
    "additionalProperties": False,
}
