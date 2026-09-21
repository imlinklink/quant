"""R/L 双影子账户 schema：金额（微美元整数）、manifest、opportunity、账户状态、动作。

金额统一用 **int 微美元**（1 USD = 1_000_000），价格用 **int 微美元/股**，全部整数运算、
JSON 不输出 NaN。`hash_*` 复用 event_store 的 canonical/digest/stable_id 保证确定性。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_HALF_UP

from mutifactor.llm.contracts.position_v2 import POSITION_ACTIONS

from scripts.live_trading.decision_ledger.event_store import digest, stable_id

MICRO = 1_000_000  # 1 USD = 1e6 微美元

# 状态枚举
MANIFEST_STATUSES = ('DRAFT', 'FROZEN', 'RUNNING', 'PAUSED', 'CLOSED')
# 候选总账终态（账户资格检查之前）
CANDIDATE_TERMINAL = ('RULE_REJECTED', 'DATA_BLOCKED', 'WAITING', 'EXPIRED', 'READY')
# READY 之后的账户级动作
ACCOUNT_ACTIONS = ('RISK_REJECTED', 'VETOED', 'INTENT_CREATED', 'MISSED_EXECUTION')
# 机会在影子账本里的全部可能状态（投影 terminal 列取值）
SHADOW_TERMINALS = CANDIDATE_TERMINAL + ('VETOED', 'MISSED_EXECUTION', 'EXECUTED')

# 模型尝试状态机（设计 §7）：PREPARED → CALL_STARTED → 终态。
# UNKNOWN = 已领取但无回复（进程在发送后崩溃）；不盲目重发，截止时按 ABSTAIN 冻结。
ATTEMPT_STATUSES = ('PREPARED', 'CALL_STARTED', 'COMPLETED', 'FAILED', 'TIMED_OUT',
                    'UNKNOWN')
ATTEMPT_TERMINAL = ('COMPLETED', 'FAILED', 'TIMED_OUT', 'UNKNOWN')

# 证据等级。strict = 点对点可追溯（要求 observed_at + 来源已核实）；diagnostic = 只做
# published_at 过滤。诊断级证据上的 VETO 与严格级上的不是同一个东西，故必须冻结进实验。
EVIDENCE_MODES = ('strict', 'diagnostic')

# 持仓 overlay 开关。默认关闭（键缺省即 off）：这样已有的冻结实验继续通过校验与哈希比对
# —— 开启会改变 manifest_hash，必须新建 experiment_id，不能顶着旧身份改尺子。
POSITION_OVERLAYS = ('off', 'position_action')

# 容量分配（Portfolio）开关。同样默认关闭：开启会引入一次模型评审；且由于 Portfolio
# 的权限等级是 shadow，其 effective_action 是父策略，结果与不开启相同。
PORTFOLIO_OVERLAYS = ('off', 'portfolio_action')

# 各策略字典的已知键。未知键必须报错：拼错一个字母（如 use_real_modle/knowledge_cutof）
# 会被 `dict.get` 静默忽略，让本该生效的安全门无声失效 —— 然后 freeze 门报「缺失」，
# 操作者按提示补上另一个拼写，漏洞就这么留下了。
POLICY_KEYS = {
    'risk_policy': ('single_position_risk_bp', 'max_weight_bp', 'max_positions', 'top_n',
                    'drawdown_ladder'),
    'execution_policy': ('entry_rule', 'exit_policy_id', 'horizon', 'max_wait_sessions'),
    'llm_policy': ('overlay', 'use_real_model', 'knowledge_cutoff', 'evidence_mode',
                   'evidence_window_days', 'evidence_max_events', 'position_overlay',
                   'portfolio_review', 'technical_packet', 'open_actions',
                   'model_budget_micro'),
    'evaluation_protocol': ('main_metric', 'enrollment_window', 'review_date',
                            'cost_allocation'),
}
# 与 risk_policy._DEFAULTS 的键一致，由测试钉死
LADDER_KEYS = ('limit_breach', 'review_required', 'paused_entry', 'reduced',
               'normal_recover', 'reduced_recover', 'recover_sessions')


def to_micro(value) -> int:
    """把美元金额/价格转成 int 微美元（四舍五入到 1e-6）。"""
    d = Decimal(str(value))
    return int((d * MICRO).to_integral_value(rounding=ROUND_HALF_UP))


def micro_to_decimal(value: int) -> Decimal:
    return Decimal(value) / MICRO


def fee_micro(notional_micro: int, rate) -> int:
    """notional × rate，四舍五入到微美元；rate 为每腿费率（如 0.001）。"""
    return int((Decimal(notional_micro) * Decimal(str(rate))).to_integral_value(rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class Manifest:
    """实验 manifest（设计 §3）。freeze 前必须完整，缺关键字段拒绝。"""
    experiment_id: str
    status: str
    parent_strategy_id: str
    parent_version: str
    parent_code_hash: str
    universe_id: str
    universe_hash: str
    account_scopes: tuple  # (SHADOW:<id>:R, SHADOW:<id>:L)
    initial_cash: int  # 微美元
    currency: str = 'USD'
    risk_policy: dict = field(default_factory=dict)
    execution_policy: dict = field(default_factory=dict)
    llm_policy: dict = field(default_factory=dict)
    calendar_version: str = ''
    data_hashes: dict = field(default_factory=dict)
    evaluation_protocol: dict = field(default_factory=dict)
    start_session: str | None = None

    def validate(self) -> list[str]:
        """返回缺失/非法字段清单；空表示可 freeze。"""
        errors = []
        if self.status not in MANIFEST_STATUSES:
            errors.append(f'status 非法: {self.status}')
        if not self.experiment_id:
            errors.append('experiment_id 缺失')
        if not self.parent_strategy_id or not self.parent_version or not self.parent_code_hash:
            errors.append('parent_strategy 缺失（id/version/code_hash）')
        if not self.universe_id or not self.universe_hash:
            errors.append('universe 缺失（id/hash）')
        if not self.account_scopes or len(set(self.account_scopes)) != len(self.account_scopes):
            errors.append('account_scopes 缺失或重复')
        if not isinstance(self.initial_cash, int) or self.initial_cash <= 0:
            errors.append('initial_cash 非法')
        if not self.risk_policy.get('single_position_risk_bp'):
            errors.append('risk_policy.single_position_risk_bp 缺失')
        if not self.risk_policy.get('max_weight_bp') or not self.risk_policy.get('max_positions'):
            errors.append('risk_policy.max_weight_bp/max_positions 缺失')
        if not self.execution_policy.get('entry_rule') or not self.execution_policy.get('exit_policy_id'):
            errors.append('execution_policy.entry_rule/exit_policy_id 缺失')
        if not self.execution_policy.get('horizon'):
            errors.append('execution_policy.horizon 缺失')
        if not self.llm_policy.get('overlay'):
            errors.append('llm_policy.overlay 缺失（本轮须为 fixed_pass/entry_veto）')
        if self.llm_policy.get('use_real_model') and not self.llm_policy.get('knowledge_cutoff'):
            # 真实模型的训练数据截止必须显式声明。决策时点早于它时，as-of 证据过滤修不好
            # 泄漏（模型知道当时不可能知道的事），这是对 L−R 指标的一阶威胁，不能默认无事。
            # 确实未知就填 'unknown'，让它在评审里可见，而不是留空悄悄跳过。
            errors.append('llm_policy.knowledge_cutoff 缺失（use_real_model=true 时必填；'
                          '未知请填 "unknown"）')
        position_overlay = self.llm_policy.get('position_overlay', 'off')
        if position_overlay not in POSITION_OVERLAYS:
            errors.append(f'llm_policy.position_overlay 非法：{position_overlay!r}'
                          f'（允许：{list(POSITION_OVERLAYS)}；缺省即 off）')
        portfolio_review = self.llm_policy.get('portfolio_review', 'off')
        if portfolio_review not in PORTFOLIO_OVERLAYS:
            errors.append(f'llm_policy.portfolio_review 非法：{portfolio_review!r}'
                          f'（允许：{list(PORTFOLIO_OVERLAYS)}；缺省即 off）')
        # 只要有一个角色真正调用模型判断，证据等级与窗口容量就必须冻结 ——
        # 两者都不靠运行时默认值决定：等级决定结论的适用范围，窗口决定取样范围。
        needs_evidence = (self.llm_policy.get('overlay') == 'entry_veto'
                          or position_overlay == 'position_action'
                          or portfolio_review == 'portfolio_action')
        if needs_evidence:
            mode = self.llm_policy.get('evidence_mode')
            if mode not in EVIDENCE_MODES:
                errors.append(f'llm_policy.evidence_mode 非法或缺省：{mode!r}'
                              f'（需要模型判断的角色必填，允许：{list(EVIDENCE_MODES)}）')
            # 事件窗口与容量固定在 manifest（设计 §5.2）：不冻结就能在看不到更多证据时
            # 悄悄放宽窗口，把「扩大了取样」伪装成「发现了风险」。
            for key in ('evidence_window_days', 'evidence_max_events'):
                value = self.llm_policy.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    errors.append(f'llm_policy.{key} 缺失或非法：{value!r}'
                                  f'（需要模型判断的角色必填正整数）')
        # 调用预算（规划 §4.1）：用真实模型的实验**必须**显式给出上限，否则一次失控的
        # 重试循环就是一张没有封顶的账单。缺省（None）表示未启用真实模型，不报错。
        budget = self.llm_policy.get('model_budget_micro')
        if self.llm_policy.get('use_real_model'):
            if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
                errors.append(f'llm_policy.model_budget_micro 缺失或非法：{budget!r}'
                              f'（use_real_model=true 时必填正整数，单位微美元）')
        if not self.calendar_version:
            errors.append('calendar_version 缺失')
        # 技术包（规划 §6.2）与开放动作集（§6.1）：两者必须**一起**出现在持仓角色上。
        # 只开技术包不给动作集 ⇒ 模型仍可返回 reduce/post_exit_review，本轮的限定角色失效；
        # 只给动作集不开技术包 ⇒ 门仍按新闻判充分性，模型永远不被调用。都要 fail-closed。
        if self.llm_policy.get('technical_packet'):
            if self.llm_policy.get('position_overlay') != 'position_action':
                errors.append('llm_policy.technical_packet 需要 position_overlay=position_action')
            actions = self.llm_policy.get('open_actions')
            if not isinstance(actions, (list, tuple)) or not actions:
                errors.append(f'llm_policy.open_actions 缺失或非法：{actions!r}')
            else:
                unknown_actions = sorted(set(actions) - set(POSITION_ACTIONS))
                if unknown_actions:
                    errors.append(f'llm_policy.open_actions 含未知动作 {unknown_actions}'
                                  f'（允许：{sorted(POSITION_ACTIONS)}）')
        # 未知键：拼错的键会被 `dict.get` 静默忽略，让安全门无声失效
        for name, allowed in POLICY_KEYS.items():
            unknown = sorted(set(getattr(self, name) or {}) - set(allowed))
            if unknown:
                errors.append(f'{name} 含未知键 {unknown}（允许：{sorted(allowed)}）')
        ladder = (self.risk_policy or {}).get('drawdown_ladder') or {}
        unknown_ladder = sorted(set(ladder) - set(LADDER_KEYS))
        if unknown_ladder:
            errors.append(f'risk_policy.drawdown_ladder 含未知键 {unknown_ladder}'
                          f'（允许：{sorted(LADDER_KEYS)}）')
        # 前瞻协议（设计 §3：缺完整研究协议拒绝冻结）
        for key in ('main_metric', 'enrollment_window', 'review_date', 'cost_allocation'):
            if not self.evaluation_protocol.get(key):
                errors.append(f'evaluation_protocol.{key} 缺失')
        return errors

    def freeze(self, start_session: str) -> 'Manifest':
        errors = self.validate()
        if errors:
            raise ValueError('MANIFEST_FREEZE_FAILED:' + ';'.join(errors))
        if self.status not in ('DRAFT', 'FROZEN'):
            raise ValueError(f'只能从 DRAFT/FROZEN 冻结，当前 {self.status}')
        return replace(self, status='FROZEN', start_session=start_session)

    def manifest_hash(self) -> str:
        """冻结身份的哈希。**只含不可变字段** —— `status` 是运行状态（暂停/恢复/关闭），
        单独校验与记录，不参与身份：否则暂停一次就会让实验看起来"被改过"。

        `start_session` 必须在内：起始日决定实验从哪一天开始积累，改了它实验身份却没变，
        等于同一把尺子换了刻度。`initial_cash` 也在内 —— 它不会给已有账户充值，但**新账户
        初始化**与所有以 manifest 为分母的绩效计算都依赖它。
        """
        return digest({
            'experiment_id': self.experiment_id, 'parent_strategy_id': self.parent_strategy_id,
            'parent_version': self.parent_version, 'parent_code_hash': self.parent_code_hash,
            'universe_id': self.universe_id, 'universe_hash': self.universe_hash,
            'account_scopes': list(self.account_scopes), 'initial_cash': self.initial_cash,
            'currency': self.currency, 'risk_policy': self.risk_policy,
            'execution_policy': self.execution_policy, 'llm_policy': self.llm_policy,
            'calendar_version': self.calendar_version, 'data_hashes': self.data_hashes,
            'evaluation_protocol': self.evaluation_protocol,
            'start_session': self.start_session,
        })


@dataclass(frozen=True)
class Opportunity:
    """共同机会流（设计 §4.2），账户资格检查之前。

    设计 §4 要求程序保存「规则为何产生机会、初始保护线如何计算及退出规则」，
    模型不自行计算 ATR、下单量或止损 —— 故 rule_reason_codes / stop_reference /
    exit_policy_id 都由规则侧填入并冻结。
    """
    experiment_id: str
    security_id: str
    source_candidate_id: str
    parent_version: str
    signal_session: str
    observed_at: str
    planned_execution_session: str
    rank: int
    entry_rule: str
    stop_reference: dict  # 用于入场的止损基准（原始 ATR/距离），非预成交
    exit_policy_id: str
    input_hash: str
    terminal: str = 'WAITING'  # CANDIDATE_TERMINAL 之一
    parent_strategy_id: str = ''
    signal_generated_at: str = ''  # 信号实际生成时刻（可信时钟）
    decision_deadline: str = ''  # 动作最晚冻结时刻 = 执行日开盘前
    rule_reason_codes: tuple = ()  # 规则侧的入场原因（可读、可审计）
    market_snapshot_id: str = ''  # 生成信号时所用的行情快照标识

    def opportunity_id(self) -> str:
        return stable_id('opportunity', self.experiment_id, self.security_id,
                         self.source_candidate_id, self.parent_version,
                         self.signal_session, self.planned_execution_session,
                         self.entry_rule, self.input_hash)


@dataclass
class Position:
    security_id: str
    shares: int
    entry_price_micro: int
    entry_session: str
    initial_stop_micro: int
    stop_micro: int  # 当前有效保护线（含移动保护）
    exit_policy_id: str
    opportunity_id: str
    holding_sessions: int = 0
    # ---- 机械利润保护的状态（规划 §4.3；未启用时全部保持中性值）----
    # 成交时**冻结**的单笔风险金额（净 R 的分母，不随浮盈变化，§7）。拆股不改它：
    # 它是一次成交的风险额，不是按当前股数重算的口径。
    initial_risk_micro: int = 0
    # H = 持仓期内**已完成收盘价**的最高值。拆股按同一比例缩放、除息按每股分红下调
    # （与 stop_micro 同一会计规则），否则公司行动会让保护线相对价格失真。
    highest_completed_close_micro: int = 0
    # **粘性**：除息下调 H 后不得退回未激活（否则已保本的持仓会突然失去保护）。
    protection_activated: bool = False
    # T 收盘算出、T+1 生效的保护线；None 表示没有待生效的更新。
    pending_stop_micro: int | None = None
    pending_stop_effective_session: str = ''


@dataclass
class AccountState:
    """账户状态（设计 §6.3）。金额单位：微美元；dividend_receivable 按 pay_date 记录。"""
    scope: str
    sequence: int = 0
    cash_available: int = 0
    cash_reserved: int = 0
    unsettled_cash: int = 0  # T+1 未结算卖出款
    dividend_receivable: dict = field(default_factory=dict)  # pay_date -> 微美元
    positions: dict = field(default_factory=dict)  # security_id -> Position
    fees: int = 0
    model_cost: int = 0
    # 成本不可知、已挂账待补记的模型尝试 id（金额尚未计入 model_cost）。
    # 「不在这里」= 已结清或从未发生；计数由它派生，避免两个字段漂移。
    model_cost_unsettled: tuple = ()
    initial_equity: int = 0
    high_water: int = 0
    last_session: str | None = None
    valuation_status: str = 'OK'  # OK / PROVISIONAL
    risk_state: str = 'NORMAL'  # 回撤阶梯状态
    recovery_streak: int = 0

    @property
    def model_cost_uncertain_count(self) -> int:
        return len(self.model_cost_unsettled)

    @property
    def cost_status(self) -> str:
        """PROVISIONAL = 尚有非负费用未扣，full_cost_equity 只是上界。"""
        return 'PROVISIONAL' if self.model_cost_unsettled else 'OK'

    def equity(self, mark_prices: dict[str, int]) -> int:
        mv = sum(p.shares * mark_prices[p.security_id] for p in self.positions.values())
        return self.cash_available + self.cash_reserved + self.unsettled_cash + \
            sum(self.dividend_receivable.values()) + mv

    def invariants(self) -> list[str]:
        errs = []
        if self.cash_available < 0 or self.cash_reserved < 0 or self.unsettled_cash < 0:
            errs.append('现金/保留/未结算不能为负')
        for k, v in self.dividend_receivable.items():
            if v < 0:
                errs.append(f'应收分红 {k} 不能为负')
        for sid, p in self.positions.items():
            if p.shares <= 0:
                errs.append(f'{sid} 持仓数量非正')
            if p.entry_price_micro <= 0 or p.stop_micro <= 0:
                errs.append(f'{sid} 价格/止损非法')
        return errs

    def state_hash(self) -> str:
        return digest({
            'scope': self.scope, 'sequence': self.sequence,
            'cash_available': self.cash_available, 'cash_reserved': self.cash_reserved,
            'unsettled_cash': self.unsettled_cash,
            'dividend_receivable': {k: v for k, v in sorted(self.dividend_receivable.items())},
            'fees': self.fees, 'model_cost': self.model_cost,
            'model_cost_unsettled': list(self.model_cost_unsettled),
            'initial_equity': self.initial_equity, 'high_water': self.high_water,
            'last_session': self.last_session, 'valuation_status': self.valuation_status,
            'risk_state': self.risk_state, 'recovery_streak': self.recovery_streak,
            'positions': {sid: {'shares': p.shares, 'entry_price_micro': p.entry_price_micro,
                                'entry_session': p.entry_session,
                                'initial_stop_micro': p.initial_stop_micro,
                                'stop_micro': p.stop_micro, 'exit_policy_id': p.exit_policy_id,
                                'opportunity_id': p.opportunity_id,
                                'holding_sessions': p.holding_sessions,
                                # 利润保护状态进哈希：不进的话「重放一致」对保护线无感，
                                # 崩溃恢复后的保护线错位不会被任何断言发现。
                                'initial_risk_micro': p.initial_risk_micro,
                                'highest_completed_close_micro': p.highest_completed_close_micro,
                                'protection_activated': p.protection_activated,
                                'pending_stop_micro': p.pending_stop_micro,
                                'pending_stop_effective_session':
                                    p.pending_stop_effective_session}
                          for sid, p in sorted(self.positions.items())},
        })


@dataclass(frozen=True)
class Application:
    """每账户对某 opportunity 的最终动作（LLM 动作 PASS/VETO/ABSTAIN 或账户级动作）。

    `decision_frozen` 与 `execution_applied` 必须分开（设计 §7）：动作在决策时点冻结，
    成交要到执行日结算才发生。把两者合成一个布尔会让「已决策未成交」看起来像「已成交」，
    崩溃恢复时也就分不清该跳过还是该补做。
    """
    scope: str
    opportunity_id: str
    action: str  # PASS/VETO/ABSTAIN/BLOCK 或 ACCOUNT_ACTIONS 之一
    reason_code: str
    decision_id: str
    as_of: str
    decision_frozen: bool = True
    execution_applied: bool = False
    model_cost: int = 0
    raw_action: str = ''
    late_response_observed: bool = False
    cost_uncertain: bool = False  # 调用发生过但成本不可知 → 待补记
    attempt_id: str = ''  # 绑定该次模型尝试，供补记事件关联
