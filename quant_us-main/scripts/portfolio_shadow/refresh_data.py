"""前向运行的行情刷新：只追加，不改历史。

三步（前两步各自有独立的 root 与检查点，用错会把追加变成截断）：

1. 下载原始日线 —— TECH 走主 root，ETF 走**它自己的** root 与检查点。两者分开是因为
   ETF 的分区从来就没写进主检查点，用主 root 会触发「已有分区哈希与检查点不一致」的守卫；
   而 `--overwrite` 在这里是**危险**的：它跳过 concat 分支，会把年度分区截断成只剩请求区间。
2. 把 13 只 TECH 的 asof 面板续到最新 —— **只追加新 session**，写入前断言历史段逐格不变
   （浮点噪声容差内）；断言不过就不写，宁可停在这一步也不污染已冻结的对照结果。
3. 更新 live ETF 快照 —— `trading_calendar` / `market_frame` 的来源。只产出它们真正需要的
   `etf_raw_daily.csv.gz`；qfq 与公司行动诊断仍是 `prepare_etf_inputs` 的审计产物，不在这里动。

用法：
    python3 -m scripts.portfolio_shadow.refresh_data --live-dir <dir> [--through YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

from scripts.data.normalize_market_history import normalize
from scripts.data.asof_feature_panel import build_asof_panel
from scripts.medium_term.p2_selection_check import ACTIONS, PANELS, TECH
from scripts.medium_term.prepare_etf_inputs import _load_partitions

ROOT = Path(__file__).resolve().parents[2]
RAW_ROOT = ROOT / 'data/market_history/raw'
CHECKPOINT = ROOT / 'data/market_history/checkpoints/download_state.json'
# ETF 有自己的 root 与检查点（主检查点里没有它们的条目）
ETF_ROOT = ROOT / 'data/medium_term/US-MT-MOM-BASELINE-001'
ETF_UNIVERSE = ETF_ROOT / 'universe_etf.csv'
HISTORY_TOL = 1e-9
# **共享刷新锁**：影子日作业与三臂前向作业都会调这里，而 `to_csv` 不是原子的
# ⇒ 并发追加同一批面板有写坏的风险。锁放在**数据侧**（不是各自的运行锁），两边共用。
REFRESH_LOCK = ROOT / 'data/market_history/.refresh.lock'


def master_codes(names) -> set[str]:
    """证券 id（`SEC-US-AAPL`）→ 行情主表的 `code`（`US.AAPL`）。

    **两种格式的转换只在这里定义一次**。默认仍是 13 只 TECH（向后兼容）；三臂前向实验会
    传它自己的 32 只，所以这里收一个 `names` 而不是写死 `TECH`。
    """
    return {'US.' + str(sid).replace('SEC-US-', '') for sid in names}


def tech_master_codes() -> set[str]:
    return master_codes(TECH)


def select_master(master: pd.DataFrame, names=None) -> pd.DataFrame:
    """从主表选出给定证券（默认 13 只 TECH）；**选不满就报错，绝不返回空表**。

    空表会一路变成 `download(codes=[])`：下载工具对空 codes 不报错、只是什么都不做。实测
    2026-09-17 之后个股日线一直停在 09-16 —— 任务在跑、日志正常、数据一天都没前进，根因
    就是这里拿 `SEC-US-AAPL` 去匹配主表的 `US.AAPL`（0 行）。这种失败必须在这一步炸出来。
    """
    codes = master_codes(TECH if names is None else names)
    selected = master[master.code.astype(str).isin(codes)]
    if selected.empty:
        raise ValueError('MASTER_EMPTY:主表里一只都没匹配上（code 格式变了吗？）')
    missing = codes - set(selected.code.astype(str))
    if missing:
        raise ValueError(f'MASTER_INCOMPLETE:主表缺 {sorted(missing)}')
    return selected


def select_tech_master(master: pd.DataFrame) -> pd.DataFrame:
    return select_master(master, TECH)


def force_tail_refetch(checkpoint: Path, year: int) -> int:
    """把 `year` 年各分区的「已覆盖」上界回退，迫使下载工具**重新取尾部**。

    下载工具把「**请求过的**范围」当成「**已覆盖的**范围」：同一范围内第二次刷新会被判定
    covered 而**完全跳过下载**。但数据源可能当时还没发布最近那几天 —— 实测：北京 10:45
    请求 09-04..09-18 并记为已覆盖（当时 Futu 还没有 09-17），14:21 再请求同一范围就直接
    跳过，于是个股日线**永远停在数据源当时给到的那天**：任务在跑、日志正常、一天都没前进。

    回退上界不影响 sha256 校验（那比对的是文件内容哈希），只让 covered 判定失败从而重取。

    检查点不存在时返回 0（与下载工具 `_load_checkpoint` 的语义一致）——此时没有「已覆盖」
    可言，不需要回退。
    """
    if not checkpoint.exists():
        return 0
    state = json.loads(checkpoint.read_text())
    stale = f'{year - 1}-12-31'
    changed = 0
    for key, rec in (state.get('completed') or {}).items():
        parts = key.split('|')
        if len(parts) != 4 or parts[3] != str(year) or rec.get('requested_end') == stale:
            continue
        rec['requested_end'] = stale
        changed += 1
    if changed:
        checkpoint.write_text(json.dumps(state))
    return changed


def download(*, master: Path, start: str, end: str, output_root: Path, checkpoint: Path) -> dict:
    """调用既有下载工具（追加 + 去重）。**绝不传 `--overwrite`**。"""
    cmd = [sys.executable, str(ROOT / 'scripts/data/download_market_history.py'),
           '--master', str(master), '--start', start, '--end', end, '--autype', 'none',
           '--output-root', str(output_root), '--checkpoint', str(checkpoint)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f'DOWNLOAD_FAILED:{proc.stderr.strip()[-400:]}')
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {}


#: 面板里的**原始**列。它们变了 = 数据被改写（无论怎么解释都不该发生）⇒ 必须停。
RAW_PANEL_COLUMNS = ('raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume')


def history_unchanged(hist: pd.DataFrame, old: pd.DataFrame, tol: float = HISTORY_TOL):
    """历史段比对。返回 `(原始列是否逐格不变, 原始列最大差, 复权列最大差)`。

    **两类列必须分开判**：

    · **原始列**（`RAW_PANEL_COLUMNS`）变了 = 数据被改写 ⇒ 硬失败、宁可停在这一步。
      这是「只追加」的全部意义。
    · **复权列**（`asof_*` / `scale_to_next`）变了 = **构造性的**：面板是「锚在最新一天」的
      复权视图，行动表新增一条记录（例如新宣告的股息）就会把整段历史按同一因子重述。
      这不是损坏 —— 而且 P0-4 已证明这些列派生的信号全是**比较型**，对整段同因子缩放不变。
      实测：META 2026-09-21 新宣告股息 0.525 使整段 `asof_close` 平移 7.9e-4，
      旧实现在这里把**日常刷新**判成了数据损坏，日作业从此停摆。

    复权列的差异必须**报出来**（`refresh_panels` 的输出里有 `adjusted_max_delta`），
    不许静默通过 —— 这条防线的价值在于「看得见」，不在于「一律拒绝」。
    """
    # **结构性检查放在最前**：行数不一致就是改动，不该依赖 dtype 的意外行为。
    # 实测教训：`(a.isna() ^ b.isna()).any()` 在等长以外的情形会因 pandas 的 `skipna=True`
    # 把 NaN 跳过，于是「重建少了/多了几行」可能整条溜过去 —— 旧实现只是恰好被 `session`
    # 列的 object dtype 兜住（object 上的 NaN 不会被 skipna 跳过）。显式判，不靠巧合。
    if len(hist) != len(old):
        return False, float('inf'), float('inf')
    raw_worst, adj_worst, ok = 0.0, 0.0, True
    for col in old.columns:
        a = pd.to_numeric(hist[col], errors='coerce').astype(float)
        b = pd.to_numeric(old[col], errors='coerce').astype(float)
        is_raw = col in RAW_PANEL_COLUMNS
        both_nan = a.isna() & b.isna()
        if both_nan.all():
            continue
        # 单侧 NaN = 值出现或消失，是**真的改动**：`NaN > tol` 恒为 False，不显式判别
        # 就会被静默放行（行数不一致时 pandas 按索引对齐后正是这种形态）
        # `.fillna(True)`：对齐产生的 NaN 一律当「有改动」（fail-closed），
        # 不让 skipna 把它吞掉
        changed = bool((a.isna() ^ b.isna()).fillna(True).any())
        delta = (a - b).abs()
        worst = 0.0 if not delta.notna().any() else float(delta.max())
        changed = changed or bool((delta[~both_nan] > tol).any())
        if is_raw:
            raw_worst = max(raw_worst, worst)
            ok = ok and not changed
        else:
            adj_worst = max(adj_worst, worst)
    return ok, raw_worst, adj_worst


def refresh_panels(*, through: str | None = None, names=None) -> list[dict]:
    """把给定证券（默认 13 只 TECH）的面板续到最新。

    只追加新 session；历史段超容差即中止，不写。三臂前向实验传它自己的 32 只。
    """
    actions = pd.read_csv(ACTIONS)
    actions['security_id'] = actions.security_id.astype(str)
    if through is None:
        # **默认只刷到「收盘已过」的 session**（复用数据就绪门的同一处判据）。
        # 盘中跑刷新会把当天那根**还没走完的 bar** 追加进面板，它下一分钟就变了 ——
        # 于是「只追加」的守卫在下一次刷新时必然失败，日作业就此停摆。
        # 实测：2026-09-22 00:09（美东 09-21 盘中）手动跑刷新踩到过，AAPL 最后一行
        # 的 raw_close/volume 两分钟内就变了。
        from .data_readiness import expected_session
        closed = expected_session()
        if closed:
            through = closed
    out = []
    for sid in (TECH if names is None else names):
        code = 'US_' + sid.replace('SEC-US-', '')
        path = PANELS / f'{code}.csv.gz'
        raw = pd.concat([pd.read_csv(f) for f in
                         sorted(glob.glob(str(RAW_ROOT / f'day/none/year=*/{code}.csv.gz')))],
                        ignore_index=True)
        norm = normalize(raw, 'day').rename(columns={'date': 'session'})
        norm['security_id'] = sid
        rebuilt = build_asof_panel(norm, actions[actions.security_id.eq(sid)])
        old = pd.read_csv(path)
        old['session'] = pd.to_datetime(old.session).dt.normalize()
        if through:
            rebuilt = rebuilt[rebuilt.session <= pd.Timestamp(through)]
        # 已存的行里，**未收盘的 session** 是历史遗留（早先盘中跑刷新写进去的）：
        # 丢掉它，让下一次正常刷新自己补上正确的收盘值。留着它会永久卡住守卫。
        keep = old[old.session <= rebuilt.session.max()].reset_index(drop=True)
        dropped = len(old) - len(keep)
        last = keep.session.max()
        cols = list(old.columns)
        # 历史段比对**排除最后一行**：那一行可能正来自一个当时还没收盘的 session，
        # 重述它是数据源在给出完整值，不是历史被改写。
        head_new, head_old = (rebuilt[rebuilt.session < last].reset_index(drop=True)[cols],
                              keep[keep.session < last].reset_index(drop=True)[cols])
        ok, raw_worst, adj_worst = history_unchanged(head_new, head_old)
        if not ok:
            # 原始列被改写：宁可停在这里，也不要让已冻结的对照失去可复现性
            raise ValueError(f'PANEL_RAW_HISTORY_CHANGED:{sid}:max|delta|={raw_worst:.3e}')
        last_old = keep[keep.session == last].reset_index(drop=True)[cols]
        last_new = rebuilt[rebuilt.session == last].reset_index(drop=True)[cols]
        last_delta = 0.0
        if len(last_old) and len(last_new):
            for col in cols:
                d = (pd.to_numeric(last_new[col], errors='coerce').astype(float)
                     - pd.to_numeric(last_old[col], errors='coerce').astype(float)).abs()
                if d.notna().any():
                    last_delta = max(last_delta, float(d.max()))
        # **必须把 rebuilt 里新出现的 session 追加进来**：第一版改写漏掉了这一步，
        # 结果面板永远停在原处、影子作业静默停摆（靠"面板为什么不前进"才追出来）。
        appended_rows = rebuilt[rebuilt.session > last]
        new_file = pd.concat([keep[keep.session < last], last_new, appended_rows],
                             ignore_index=True)
        changed = dropped or last_delta > HISTORY_TOL or len(new_file) != len(keep)
        if changed:
            new_file.to_csv(path, index=False, compression='gzip')
        out.append({'security_id': sid, 'from': str(last.date()),
                    'to': str(new_file.session.max().date()),
                    'appended': int(len(new_file) - len(keep)),
                    'dropped_unclosed_rows': int(dropped),
                    'last_row_restated': bool(last_delta > HISTORY_TOL),
                    'last_row_delta': last_delta,
                    'history_max_delta': raw_worst, 'adjusted_max_delta': adj_worst})
    return out


def refresh_live_etf(live_dir: Path) -> dict:
    """更新 live ETF 快照（交易日历与市场门的来源）。

    只产出 `etf_raw_daily.csv.gz`：那是 `trading_calendar` / `market_frame` 唯一读取的文件。
    qfq 与公司行动诊断属于审计产物，仍由 `prepare_etf_inputs` 单独产出，不在这里动。
    """
    codes = sorted(pd.read_csv(ETF_UNIVERSE).code.astype(str).unique())
    raw = _load_partitions(ETF_ROOT / 'market_history', 'none', codes)
    live_dir.mkdir(parents=True, exist_ok=True)
    path = live_dir / 'etf_raw_daily.csv.gz'
    raw.to_csv(path, index=False, compression='gzip')
    return {'path': str(path), 'codes': len(codes), 'rows': int(len(raw)),
            'through': str(raw.session.max().date())}


def refresh(*, live_dir: Path, through: str | None = None, names=None,
            master_out: Path | None = None) -> dict:
    """刷新行情。**拿不到共享锁就跳过**（另一个作业正在刷同一批面板）。"""
    REFRESH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    if not _acquire(REFRESH_LOCK):
        return {'skipped': 'REFRESH_IN_PROGRESS', 'lock': str(REFRESH_LOCK)}
    try:
        return _refresh(live_dir=live_dir, through=through, names=names,
                        master_out=master_out)
    finally:
        _release(REFRESH_LOCK)


def _acquire(lock: Path) -> bool:
    import os
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            holder = int((lock / 'pid').read_text().strip())
            os.kill(holder, 0)
            return False                      # 持有者还活着
        except (OSError, ValueError):
            pass                              # 过期锁：持有者已不在
        import shutil
        shutil.rmtree(lock, ignore_errors=True)
        try:
            lock.mkdir()
        except FileExistsError:
            return False
    (lock / 'pid').write_text(str(os.getpid()))
    return True


def _release(lock: Path) -> None:
    import shutil
    shutil.rmtree(lock, ignore_errors=True)


def _refresh(*, live_dir: Path, through: str | None = None, names=None,
             master_out: Path | None = None) -> dict:
    if through is None:
        # 与 `refresh_panels` 同一口径：不要把未收盘的当天当成已完成的日子去取
        from .data_readiness import expected_session
        through = expected_session()
    end = through or pd.Timestamp.today().strftime('%Y-%m-%d')
    start = (pd.Timestamp(end) - pd.Timedelta(days=14)).strftime('%Y-%m-%d')
    tech_master = ROOT / 'data/security_master_39.csv'
    master_out = Path(master_out or '/tmp/_tech_master.csv')
    select_master(pd.read_csv(tech_master), names).to_csv(master_out, index=False)
    year = pd.Timestamp(end).year
    return {
        # 先回退覆盖上界，否则同级范围的重跑会被判定 covered 而跳过下载（见函数说明）
        'tech_refetch': force_tail_refetch(CHECKPOINT, year),
        'etf_refetch': force_tail_refetch(ETF_ROOT / 'download_none.json', year),
        'tech_download': download(master=master_out, start=start, end=end,
                                  output_root=RAW_ROOT, checkpoint=CHECKPOINT),
        'etf_download': download(master=ETF_UNIVERSE, start=start, end=end,
                                 output_root=ETF_ROOT / 'market_history',
                                 checkpoint=ETF_ROOT / 'download_none.json'),
        'panels': refresh_panels(through=through, names=names),
        'live_etf': refresh_live_etf(live_dir),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description='前向运行的行情刷新（只追加，不改历史）')
    p.add_argument('--live-dir', required=True, help='live ETF 快照目录')
    p.add_argument('--through', help='刷新到这个交易日（默认今天）')
    p.add_argument('--universe', choices=('tech', 'forward-arms'), default='tech',
                   help='刷新哪一批证券：tech = 13 只 TECH（默认）；'
                        'forward-arms = 三臂前向实验的 32 只')
    args = p.parse_args(argv)
    names = None
    if args.universe == 'forward-arms':
        # 惰性导入：三臂的证券清单只有一处定义（`forward_arms.arm_names`），不在这里再写一份
        from scripts.strategy_diagnostics.forward_arms import arm_names
        names = arm_names()['B']
    master_out = Path('/tmp/_master_%s.csv' % args.universe)
    print(json.dumps(refresh(live_dir=Path(args.live_dir), through=args.through,
                             names=names, master_out=master_out),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
