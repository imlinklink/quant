"""三臂前向观察 runner（登记 `B3-FORWARD-20260921`）。

**只记账，不做决策**：三臂跑同一个 B3，**只差宇宙**（A = 13 只硬编码；B = 32 只 + 时点门；
C = 32 只无门）。每臂一本独立账本、一个独立 `experiment_id`、**同日空仓起步**。

**无状态设计：每次调用从起点确定性重放**（`start_session` → 目标 session）。

为什么必须重放而不是"只跑今天"：增量生成器的 `pending`（等待窗状态）活在进程内，
按天调用会让月末生成的候选在进程退出时丢掉 —— 实测表现是**永远没有候选**（`due=0`）。
账户侧同理：状态若从账本 replay 到最新，再去步进更早的 session 会被引擎以
`OUT_OF_ORDER_SESSION` 拒绝。

所以三臂都从**空仓**起步、逐 session 重放到目标日；账本只作**幂等落点**
（`save_state` 的序号守卫：同序号同哈希跳过、同序号异哈希拒绝）。故按天调用、
崩溃后重入、重复跑同一天都不需要额外状态。

复用的部件（一律不重写）：
· 逐日步进 `experiments.step_account_session`（与回测同一处）
· 候选生成 `IncrementalCandidateGenerator`（掩码两条路径共用一处，见 `bb37836`）
· 到期还原 `cli._opportunity_from_dict`、认领 `candidate_adapter.intents_for_session`
· 记账与重放 `ShadowStore` / `replay`

**守卫（fail-closed）**：
1. 三臂必须同日起步；`start` 只跑一次，任一臂账本已存在即拒绝；
2. 同一 session 重跑是 no-op（引擎幂等 + `save_state` 序号守卫）；
3. session 不得倒退（引擎 `OUT_OF_ORDER_SESSION`）；
4. 收盘估值非 OK（缺持仓价格）⇒ 中止，不推进账户（登记的门槛写了"无缺失持仓价格"）；
5. 计划日已过而仍未成交的机会一律记 `MISSED_EXECUTION`，**绝不按历史开盘价补成交**。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pandas as pd

from scripts.portfolio_shadow.candidate_adapter import (IncrementalCandidateGenerator,
                                                        intents_for_session)
from scripts.portfolio_shadow.cli import _opportunity_from_dict, manifest_from_dict
from scripts.portfolio_shadow.paper_engine import new_account_state
from scripts.portfolio_shadow.replay import replay
from scripts.portfolio_shadow.store import ShadowStore

from .experiments import shadow_actions, step_account_session
from .manifest import file_hash, read, write_json
from .universe_attribution import (ACTIONS, ETF_RAW, PANELS, QUALITY, _plain,
                                   members_mask, verified_names)

ROOT = Path(__file__).resolve().parents[2]
FORWARD_REGISTRATION = ROOT / 'docs/preregistrations/B3-FORWARD-20260921.json'
# 三臂的**策略参数来源**：生产影子实验那份冻结 manifest（`portfolio_shadow.Manifest`，
# 只读引用）。B3 参数与登记里钉死的一致：b3 / H60 / 100bp / 20% / 5 仓 / top_n 5 / 等待窗 20。
# 注意不能用 `RISK-RULE-20260916-001/manifest.json` —— 那是**研究侧**的 manifest，
# 字段集不同（`manifest_from_dict` 会以 `MANIFEST_UNKNOWN_FIELDS` 拒绝）。
BASELINE_MANIFEST = ROOT / 'data/portfolio_shadow/M1-FORWARD-S-20260917/manifest.json'
COST_BP, HORIZON, MAX_WAIT, TOP_N = 10, 60, 20, 5
ARMS = ('A', 'B', 'C')
ARM_IDS = {arm: f'B3FWD-{arm}-20260921' for arm in ARMS}
# 观察起点由 `start` 写死；此后 `run-day` 只能向前推进
LEDGER_NAME = 'ledger.sqlite3'
# **冻结集**：只收"决定三臂决策与会计的东西"。不收数据面板 —— 它们是**增长型**的
# （每天追加新 session），冻进来第二天就会被自己的校验拒（这与 M1 manifest 里
# "`data_hashes` 不能装增长型数据"是同一条教训）。数据的历史段由刷新的
# `history_unchanged` 守，那才是它对的地方。
FROZEN_CODE = (
    'scripts/strategy_diagnostics/forward_arms.py',
    'scripts/strategy_diagnostics/experiments.py',
    'scripts/portfolio_shadow/paper_engine.py',
    'scripts/portfolio_shadow/candidate_adapter.py',
    'scripts/portfolio_shadow/schema.py',
    'scripts/medium_term/stock_cross_section.py',
    'scripts/medium_term/entry_risk.py',
    'scripts/medium_term/momentum_features.py',
    'scripts/medium_term/timed_entries.py',
    'scripts/medium_term/portfolio_engine.py',
    'scripts/medium_term/stock_cross_section.py',
)


def frozen_code() -> dict:
    """冻结集逐文件的 sha256。**配置（登记 + 策略 manifest）也进来** —— 改了它们，
    观察就不再是同一件事。"""
    out = {rel: file_hash(ROOT / rel) for rel in sorted(set(FROZEN_CODE))}
    out['docs/preregistrations/B3-FORWARD-20260921.json'] = file_hash(FORWARD_REGISTRATION)
    out['data/portfolio_shadow/M1-FORWARD-S-20260917/manifest.json'] = file_hash(BASELINE_MANIFEST)
    return out


def verify_frozen(root: Path) -> None:
    """每日开跑前核对冻结集。**不符即拒** —— 观察期内改了尺子，前向记录就不再可比。

    （要改就得另立登记、重开起点；登记里写了"任何一处变更 ⇒ 该臂的前向记录作废"。）
    """
    recorded = read(Path(root) / 'arms.json').get('frozen_code') or {}
    current = frozen_code()
    drifted = sorted(k for k in set(recorded) | set(current) if recorded.get(k) != current.get(k))
    if drifted:
        raise ValueError(f'OBSERVATION_CODE_CHANGED:{drifted}')


def arm_names() -> dict:
    """三臂的候选池 —— **唯一变量**。B/C 是同一份 32 只（B 多一道时点门）。"""
    from scripts.medium_term.p2_selection_check import TECH
    return {'A': list(TECH), 'B': verified_names(), 'C': verified_names()}


def arm_manifest(arm: str, start_session: str):
    """一臂的冻结 manifest。**`start` 与后续每日运行共用这一处构造**，保持一致。

    **不从账本读回**：`save_experiment` 把 `_asdict(manifest)` 原样落库（`initial_cash` 是
    **微美元**），而 `manifest_from_dict` 按**美元**解析 —— 两者不对称（生产代码从不从账本
    读回 manifest，总是重读原 JSON，所以这个坑一直没暴露）。确定性重建 + 下面的哈希守卫
    才是稳的：一旦重建结果与冻结记录不符，立即拒绝。
    """
    base = manifest_from_dict(read(BASELINE_MANIFEST))
    if base.execution_policy.get('entry_rule') != 'b3':
        raise ValueError('BASELINE_ENTRY_UNSUPPORTED')
    exp_id, scope = ARM_IDS[arm], f'SHADOW:{ARM_IDS[arm]}:R'
    m = replace(base, experiment_id=exp_id, account_scopes=(scope,), status='FROZEN',
                llm_policy={'overlay': 'fixed_pass', 'use_real_model': False},
                start_session=start_session, universe_id=f'forward-{arm}')
    if errors := m.validate():
        raise ValueError(';'.join(errors))
    return m


def registration() -> dict:
    """读并校验前向登记。**单独一个入口**，便于测试打桩（不必连 baseline manifest 一起换掉）。"""
    if not FORWARD_REGISTRATION.exists():
        raise ValueError('FORWARD_REGISTRATION_MISSING')
    reg = read(FORWARD_REGISTRATION)
    if reg.get('returns_examined'):
        # 登记自称已看过结果 ⇒ 拒绝：否则"先登记再观察"的前提不成立
        raise ValueError('REGISTRATION_NOT_BLIND')
    return reg


def start(root: Path, *, start_session: str, etf_raw: Path = None) -> dict:
    """三臂同日空仓起步。**任一臂账本已存在即拒绝**（不允许中途改起点）。"""
    root = Path(root).resolve()
    registration()
    root.mkdir(parents=True, exist_ok=True)
    created = {'start_session': start_session, 'arms': {}}
    for arm, names in arm_names().items():
        exp_id, scope = ARM_IDS[arm], f'SHADOW:{ARM_IDS[arm]}:R'
        ledger = root / arm / LEDGER_NAME
        if ledger.exists():
            raise ValueError(f'ARM_ALREADY_STARTED:{arm}')
        m = arm_manifest(arm, start_session)
        store = ShadowStore(ledger, exp_id)
        store.save_experiment(m)
        store.save_state(scope, new_account_state(scope, m.initial_cash), None, [])
        created['arms'][arm] = {'experiment_id': exp_id, 'universe_size': len(names),
                                'pit_gate': arm == 'B'}
    created['registration_sha256'] = file_hash(FORWARD_REGISTRATION)
    created['baseline_manifest_sha256'] = file_hash(BASELINE_MANIFEST)
    created['frozen_code'] = frozen_code()
    # **路径**（不是哈希）：live 快照是增长型数据，记哈希第二天就会被自己拒
    created['etf_raw'] = str(Path(etf_raw or ETF_RAW).resolve())
    write_json(root / 'arms.json', _plain(created))
    return created


def _load_inputs(names, etf_raw: Path = None):
    """三臂输入：**同一份** quality/actions/market/宇宙掩码，只有价格面板按臂取子集。

    `etf_raw` **必须指向 live 快照**（`refresh_data.refresh_live_etf` 的产物）：交易日历与
    市场门都从它来，指到冻结产物上日历就会永远停在那个日期 —— 观察期内的每一天都会
    被判成"没有新 session"。默认值只是为了让模块能单独跑起来。
    """
    from scripts.medium_term.p2_selection_check import (load_panels, market_frame,
                                                        trading_calendar)
    quality = pd.read_csv(QUALITY)
    actions = pd.read_csv(ACTIONS)
    actions['security_id'] = actions.security_id.astype(str)
    etf_raw = Path(etf_raw or ETF_RAW)
    calendar = trading_calendar(etf_raw)
    mask = members_mask(names['B'], str(pd.Timestamp(calendar[0]).date()),
                        str(pd.Timestamp(calendar[-1]).date()))
    prices_by_arm = {arm: load_panels(tech=tuple(n), panels=PANELS)[0]
                     for arm, n in names.items()}
    return prices_by_arm, market_frame(etf_raw), quality, actions, calendar, mask


class Arm:
    """一臂的运行期视图。**每次 `run_day` 从账本重建**，故可跨进程安全重入。"""

    def __init__(self, arm, root, prices, quality, actions, market, calendar, members):
        self.arm = arm
        self.root = Path(root)
        self.prices = prices
        self.calendar = calendar
        self.experiment_id = ARM_IDS[arm]
        self.scope = f'SHADOW:{self.experiment_id}:R'
        self.manifest = arm_manifest(arm, read(self.root / 'arms.json')['start_session'])
        self.store = ShadowStore(self.root / arm / LEDGER_NAME, self.experiment_id)
        if self.store.frozen_manifest_hash() != self.manifest.manifest_hash():
            # 重建出的 manifest 与冻结记录不符 ⇒ 停止，绝不按"改过的尺子"继续记
            raise ValueError(f'MANIFEST_CHANGED_SINCE_FREEZE:{arm}')
        self.gen = IncrementalCandidateGenerator(
            prices, market, quality, actions, {}, calendar,
            experiment_id=self.experiment_id, parent_version=self.manifest.parent_version,
            exit_policy_id=self.manifest.execution_policy['exit_policy_id'], top_n=TOP_N,
            max_wait_sessions=MAX_WAIT, require_matured=False, members=members)
        # **从空仓起步**（不是从账本 replay 到最新）：本进程要重放 [start, 目标日] 全程
        self.state = new_account_state(self.scope, self.manifest.initial_cash)
        with sqlite3.connect(self.root / arm / LEDGER_NAME) as con:
            self.saved = dict(con.execute(
                'SELECT sequence,state_hash FROM shadow_account_state '
                'WHERE experiment_id=? AND scope=?', (self.experiment_id, self.scope)).fetchall())
        self.acts = {}
        converted, _dropped = shadow_actions(
            actions, universe=set(prices.security_id),
            session_range=(str(pd.Timestamp(calendar[0]).date()),
                           str(pd.Timestamp(calendar[-1]).date())))
        for a in converted:
            self.acts.setdefault(a['ex_date'], []).append(a)

    def due(self, sess_str: str) -> list:
        """**从账本重建**到期队列：计划执行日 = 本 session、且尚未落终态的机会。"""
        terminal = self.store.opportunity_terminals()
        return [_opportunity_from_dict(o) for o in self.store.opportunities()
                if str(o.get('planned_execution_session')) == sess_str
                and o.get('opportunity_id') not in terminal]

    def run(self, session) -> dict:
        sess_str = str(pd.Timestamp(session).date())
        for o in self.gen.opportunities_for(session):
            self.store.put_opportunity(o)
        due = self.due(sess_str)
        result = step_account_session(
            self.state, session=pd.Timestamp(session), prices=self.prices, acts=self.acts,
            due=due, manifest=self.manifest, fee_bp=COST_BP,
            store=self.store, scope=self.scope, saved=self.saved)
        idempotent_noop = result.nav is None
        self.state = result.state
        filled = {e['opportunity_id'] for e in (result.events or [])
                  if e['type'] == 'fill' and e['side'] == 'BUY'}
        settled = {o.opportunity_id() for o in due}
        for oid in sorted(settled - filled):
            # 计划日已过而没成交 —— 终态是"错过执行"，**绝不按历史开盘价补成交**
            self.store.set_opportunity_terminal(oid, 'MISSED_EXECUTION', sess_str)
        return {'arm': self.arm, 'session': sess_str, 'noop': idempotent_noop,
                'universe_size': len(self.gen.prices.security_id.unique()),
                'due': len(due), 'filled': len(filled),
                'missed': len(settled - filled),
                'positions': len(self.state.positions),
                'equity': None if idempotent_noop else result.nav['equity']}


def latest_available_session(names, etf_raw=None) -> str:
    """三臂**都**有数据的最后一个交易日。

    取 B 臂（32 只里最大的那份）面板的最后一个 session 与交易日历的交集 —— A 的 13 只是
    B 的子集，所以它一并覆盖。**不做任何"猜"**：没有交集就报错，由作业脚本决定停一天。
    """
    prices_by_arm, _market, _q, _a, calendar, _m = _load_inputs(names, etf_raw)
    from scripts.medium_term.p2_selection_check import load_panels
    covered = min(pd.Timestamp(load_panels(tech=tuple(n), panels=PANELS)[0].session.max())
                  for n in names.values())
    options = [pd.Timestamp(s).normalize() for s in calendar if pd.Timestamp(s) <= covered]
    if not options:
        raise ValueError('NO_COMMON_SESSION:三臂面板与交易日历没有交集')
    return str(max(options).date())


def run_day(root: Path, session, etf_raw: Path = None) -> dict:
    """三臂一起推进到 `session`：**各自从 `start_session` 重放全程**，只有目标日是新的。

    重放是确定性的、账本是幂等落点，所以多次调用同一目标日、或崩溃后重入都安全。
    """
    root = Path(root).resolve()
    meta = read(root / 'arms.json')
    verify_frozen(root)
    start = pd.Timestamp(meta['start_session'])
    target = pd.Timestamp(session)
    if target < start:
        raise ValueError(f'TARGET_BEFORE_START:{target.date()}<{start.date()}')
    names = arm_names()
    prices_by_arm, market, quality, actions, calendar, mask = _load_inputs(
        names, etf_raw or meta.get('etf_raw'))
    range_ = [pd.Timestamp(s) for s in calendar
              if start <= pd.Timestamp(s) <= target]
    if not range_:
        raise ValueError('EMPTY_SESSION_RANGE')
    out = {}
    for arm in ARMS:
        view = Arm(arm, root, prices_by_arm[arm], quality, actions, market, calendar,
                   mask if arm == 'B' else None)
        for step_session in range_:
            out[arm] = view.run(step_session)
    out['_session'] = str(target.date())
    out['_start_session'] = str(start.date())
    out['_sessions_replayed'] = len(range_)
    write_json(root / 'last_session.json', _plain(out))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('start', help='三臂同日空仓起步（只跑一次）')
    s.add_argument('--root', required=True)
    s.add_argument('--start-session', required=True)
    s.add_argument('--etf-raw', help='live ETF 快照（交易日历与市场门的来源）')
    d = sub.add_parser('run-day', help='三臂各推进一个交易日')
    d.add_argument('--root', required=True)
    d.add_argument('--session', required=True,
                   help='交易日；传 auto 表示取三臂都覆盖的最后一个交易日')
    d.add_argument('--etf-raw', help='默认取 arms.json 里记的那份')
    args = parser.parse_args(argv)
    if args.command == 'start':
        out = start(Path(args.root), start_session=args.start_session, etf_raw=args.etf_raw)
    else:
        session = (latest_available_session(arm_names(), args.etf_raw)
                   if args.session == 'auto' else args.session)
        out = run_day(Path(args.root), session, etf_raw=args.etf_raw)
    print(json.dumps(_plain(out), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
