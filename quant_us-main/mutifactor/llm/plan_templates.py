"""离散计划模板与仓位档位（阶段 H2）。

程序先算好「可选的入场模板」与「允许的仓位档位」；LLM 只能在其中选择，
不能自由生成数量/止损/加档。1.0x 是现有风险上限（= 程序计算的满仓量）。
默认影子：仅当权限层允许时才应用缩放，且只允许降档（<1.0x），不允许加档。
"""
import math

from mutifactor.llm.trade_review import ALLOWED_POSITION_SCALES, PLAN_TEMPLATES

TEMPLATE_LABELS = {
    'standard': '标准入场',
    'wait_for_confirmation': '等待确认（暂不执行）',
}

MAX_SCALE = 1.0


def build_plan_templates(plan):
    """程序预计算可选模板（含每档含义），供 LLM 选择并写进 review。

    返回 {template_name: {label, allowed_position_scale}}。
    """
    return {
        name: {
            'label': TEMPLATE_LABELS[name],
            # 每个模板都只允许从 0/0.5/1.0 档位中选
            'allowed_position_scale': list(ALLOWED_POSITION_SCALES),
        }
        for name in PLAN_TEMPLATES
    }


def validate_scale(scale):
    """档位必须落在预定义集合内（且 ≤1.0，不开放加档）。"""
    if scale is None:
        return True
    if not isinstance(scale, (int, float)) or not math.isfinite(scale):
        return False
    # 只允许精确档位；浮点用 round 归一后比较
    for allowed in ALLOWED_POSITION_SCALES:
        if abs(float(scale) - allowed) < 1e-9:
            return True
    return False


def apply_position_scale(base_quantity, scale):
    """按档位缩放入场数量。scale=1.0 → 原量；0.5 → 半仓；0 → 0。

    只允许降档（scale ≤1.0），非法档位抛 ValueError（fail-closed）。
    """
    if not validate_scale(scale):
        raise ValueError(f'非法仓位档位: {scale}（仅允许 {ALLOWED_POSITION_SCALES}）')
    if base_quantity is None or base_quantity < 0:
        raise ValueError('入场数量无效')
    if scale == 1.0:
        return int(base_quantity)
    if scale == 0.0:
        return 0
    return int(math.floor(float(base_quantity) * scale))


def resolve_entry_intent(review):
    """从 review 提取模型建议的模板与档位（影子用，不做权限判断）。

    Returns:
        (template, position_scale)；两者都可为 None（模型未建议）。
    """
    template = (review or {}).get('plan_template')
    scale = (review or {}).get('position_scale')
    if template is not None and template not in PLAN_TEMPLATES:
        raise ValueError(f'非法计划模板: {template}（仅允许 {PLAN_TEMPLATES}）')
    if scale is not None and not validate_scale(scale):
        raise ValueError(f'非法仓位档位: {scale}')
    return template, scale
