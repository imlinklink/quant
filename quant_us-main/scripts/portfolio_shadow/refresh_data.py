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


def tech_master_codes() -> set[str]:
    """13 只 TECH 的证券 id → 行情主表的 `code`。

    **两种格式的转换只在这里定义一次**：`p2_selection_check.TECH` 是 `SEC-US-AAPL`
    这样的证券 id，而主表 `code` 是 `US.AAPL`。
    """
    return {'US.' + sid.replace('SEC-US-', '') for sid in TECH}


def select_tech_master(master: pd.DataFrame) -> pd.DataFrame:
    """从主表选出 13 只 TECH；**选不满就报错，绝不返回空表**。

    空表会一路变成 `download(codes=[])`：下载工具对空 codes 不报错、只是什么都不做。实测
    2026-09-17 之后个股日线一直停在 09-16 —— 任务在跑、日志正常、数据一天都没前进，根因
    就是这里拿 `SEC-US-AAPL` 去匹配主表的 `US.AAPL`（0 行）。这种失败必须在这一步炸出来。
    """
    codes = tech_master_codes()
    selected = master[master.code.astype(str).isin(codes)]
    if selected.empty:
        raise ValueError('TECH_MASTER_EMPTY:主表里一只 TECH 都没匹配上（code 格式变了吗？）')
    missing = codes - set(selected.code.astype(str))
    if missing:
        raise ValueError(f'TECH_MASTER_INCOMPLETE:主表缺 {sorted(missing)}')
    return selected


def force_tail_refetch(checkpoint: Path, year: int) -> int:
    """把 `year` 年各分区的「已覆盖」上界回退，迫使下载工具**重新取尾部**。

    下载工具把「**请求过的**范围」当成「**已覆盖的**范围」：同一范围内第二次刷新会被判定
    covered 而**完全跳过下载**。但数据源可能当时还没发布最近那几天 —— 实测：北京 10:45
    请求 09-04..09-18 并记为已覆盖（当时 Futu 还没有 09-17），14:21 再请求同一范围就直接
    跳过，于是个股日线**永远停在数据源当时给到的那天**：任务在跑、日志正常、一天都没前进。

    回退上界不影响 sha256 校验（那比对的是文件内容哈希），只让 covered 判定失败从而重取。
    """
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


def history_unchanged(hist: pd.DataFrame, old: pd.DataFrame, tol: float = HISTORY_TOL):
    """历史段是否逐格不变（浮点噪声容差内）。返回 (是否不变, 最大绝对差)。

    这是刷新里**唯一会动的防线**：asof 面板是已冻结回测对照的输入，追加新 session 时若
    改动了历史任何一格，那些对照就失去可复现性。宁可停在这一步。
    """
    worst, ok = 0.0, True
    for col in old.columns:
        a = pd.to_numeric(hist[col], errors='coerce').astype(float)
        b = pd.to_numeric(old[col], errors='coerce').astype(float)
        both_nan = a.isna() & b.isna()
        if both_nan.all():
            continue
        # 单侧 NaN = 值出现或消失，是**真的改动**：`NaN > tol` 恒为 False，不显式判别
        # 就会被静默放行（行数不一致时 pandas 按索引对齐后正是这种形态）
        if (a.isna() ^ b.isna()).any():
            ok = False
        delta = (a - b).abs()
        if delta.notna().any():
            worst = max(worst, float(delta.max()))
        if (delta[~both_nan] > tol).any():
            ok = False
    return ok, worst


def refresh_panels(*, through: str | None = None) -> list[dict]:
    """把 TECH 面板续到最新。只追加新 session；历史段超容差即中止，不写。"""
    actions = pd.read_csv(ACTIONS)
    actions['security_id'] = actions.security_id.astype(str)
    out = []
    for sid in TECH:
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
        last = old.session.max()
        if through and pd.Timestamp(through) <= last:
            out.append({'security_id': sid, 'from': str(last.date()), 'appended': 0,
                        'note': 'already_current'})
            continue
        hist = rebuilt[rebuilt.session <= last].reset_index(drop=True)[list(old.columns)]
        ok, worst = history_unchanged(hist, old)
        if not ok:
            # 宁可停在这里，也不要让已冻结的对照结果失去可复现性
            raise ValueError(f'PANEL_HISTORY_CHANGED:{sid}:max|delta|={worst:.3e}')
        new = rebuilt[rebuilt.session > last]
        if len(new):
            pd.concat([old, new], ignore_index=True).to_csv(path, index=False, compression='gzip')
        out.append({'security_id': sid, 'from': str(last.date()),
                    'to': str(rebuilt.session.max().date()), 'appended': int(len(new)),
                    'history_max_delta': worst})
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


def refresh(*, live_dir: Path, through: str | None = None) -> dict:
    end = through or pd.Timestamp.today().strftime('%Y-%m-%d')
    start = (pd.Timestamp(end) - pd.Timedelta(days=14)).strftime('%Y-%m-%d')
    tech_master = ROOT / 'data/security_master_39.csv'
    select_tech_master(pd.read_csv(tech_master)).to_csv('/tmp/_tech_master.csv', index=False)
    year = pd.Timestamp(end).year
    return {
        # 先回退覆盖上界，否则同级范围的重跑会被判定 covered 而跳过下载（见函数说明）
        'tech_refetch': force_tail_refetch(CHECKPOINT, year),
        'etf_refetch': force_tail_refetch(ETF_ROOT / 'download_none.json', year),
        'tech_download': download(master=Path('/tmp/_tech_master.csv'), start=start, end=end,
                                  output_root=RAW_ROOT, checkpoint=CHECKPOINT),
        'etf_download': download(master=ETF_UNIVERSE, start=start, end=end,
                                 output_root=ETF_ROOT / 'market_history',
                                 checkpoint=ETF_ROOT / 'download_none.json'),
        'panels': refresh_panels(through=through),
        'live_etf': refresh_live_etf(live_dir),
    }


def main(argv=None):
    p = argparse.ArgumentParser(description='前向运行的行情刷新（只追加，不改历史）')
    p.add_argument('--live-dir', required=True, help='live ETF 快照目录')
    p.add_argument('--through', help='刷新到这个交易日（默认今天）')
    args = p.parse_args(argv)
    print(json.dumps(refresh(live_dir=Path(args.live_dir), through=args.through),
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
