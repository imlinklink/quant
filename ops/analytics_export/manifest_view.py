"""manifest.json → `report.py` 算式需要的属性形状（**不 import `scripts.*`**，见包 docstring）。

只有一处转换需要小心：**`initial_cash` 在文件里是美元 float，在内存里是整数微美元**。
`schema.py` 写着 `initial_cash: int  # 微美元`，而 `cli.manifest_from_dict` 用
`to_micro(d['initial_cash'])` 转换。M1 的 manifest.json 里存的是 `100000.0`。

`paired_performance` 拿它当收益分母，而净值来自 nav body（微美元）—— 单位差 1e6 会让
收益率差一百万倍。本仓库历史上正是一次 1e6 单位错让「试算账户以 1000 亿起步、2631/2631
全对不上」。所以这里照抄同一个转换，并由
`tests/unit/ops/test_analytics_manifest_units.py` 与 `cli.manifest_from_dict` 直接对拍。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MICRO = 1_000_000


def to_micro(value) -> int:
    """美元 → int 微美元（四舍五入到 1e-6）。与 `schema.to_micro` 同义，有对拍测试。"""
    return int((Decimal(str(value)) * MICRO).to_integral_value(rounding=ROUND_HALF_UP))


class ManifestView:
    """只读视图：只暴露 `report.py` 用到的 6 个字段。

    刻意**不做** `Manifest.validate()` —— 那一层由运行的作业负责（它才是写入方）。
    导出器是投影，不是第二套校验：在这里再校验一遍，只会制造「同一件事两份定义」。
    """

    __slots__ = ('experiment_id', 'status', 'account_scopes', 'initial_cash', 'llm_policy',
                 'execution_policy', 'evaluation_protocol', 'risk_policy', 'raw')

    def __init__(self, d: dict):
        self.raw = dict(d)
        self.experiment_id = d['experiment_id']
        self.status = d.get('status', 'DRAFT')
        self.account_scopes = tuple(d['account_scopes'])
        # ↑ 文件是美元、内存是微美元：见模块 docstring
        self.initial_cash = to_micro(d['initial_cash'])
        self.llm_policy = d.get('llm_policy') or {}
        self.execution_policy = d.get('execution_policy') or {}
        self.evaluation_protocol = d.get('evaluation_protocol') or {}
        self.risk_policy = d.get('risk_policy') or {}
