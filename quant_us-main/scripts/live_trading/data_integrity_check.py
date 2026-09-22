"""行情数据完整性检查：**抓「静默停摆」**。

2026-09-22 一天里撞到两个同形态的缺陷 —— **作业照常跑、什么都不说、结果永久停住**：

1. **面板刷新漏掉「追加新 session」** ⇒ 面板永久冻结，影子作业每天判「没有新 session」，
   日志与「今天没事」一模一样；
2. **检查点与分区不同步**（分区被已知重写而检查点没更新）⇒ 下一次刷新**硬失败**
   （`FileExistsError: 已有分区哈希与检查点不一致`），而失败发生在作业里、要等人看日志。

两者都只能靠人手动核对输出才发现。本脚本把这两件事变成可自动检查的两条：

  A. **面板落后于原始库**（`raw_max > panel_max`）⇒ 刷新没有把新数据追加进去；
  B. **分区与检查点不同步**（现有分区的 sha256 ≠ 检查点记录）⇒ 下次刷新会直接失败。

**只查作业真正需要的那批分区**（TECH 13 或 forward-arms 32），不遍历全库 ——
看护每 5 分钟跑一次，把 1000+ 个文件全哈希一遍是浪费；而"会被作业撞到的那批"正是要防的。

退出码：0 = 正常，1 = 发现问题（细节在 stdout 的 JSON 里）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from scripts.medium_term.p2_selection_check import PANELS, TECH  # noqa: E402
from scripts.portfolio_shadow.refresh_data import (ACTIONS, CHECKPOINT, ETF_ROOT,  # noqa: E402
                                                   RAW_ROOT)

DEFAULT_CHECKPOINT = CHECKPOINT
DEFAULT_RAW_ROOT = RAW_ROOT
KIND = 'day'
AUTYPE = 'none'


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def universe_names(which: str) -> list:
    if which == 'tech':
        return list(TECH)
    from scripts.strategy_diagnostics.forward_arms import arm_names
    return list(arm_names()['B'])          # 三臂观察的 32 只（B 臂即全池）


def file_code(code: str) -> str:
    """证券 id（`SEC-US-AAPL`）→ 文件名里的代码（`US_AAPL`）。

    **抽成一处**：我第一版在检查点那条路径里做了转换、在原始库这条里忘了，
    于是 `raw_max_session` 拿 `SEC-US-AAPL` 去 glob `US_AAPL.csv.gz` **永远匹配不到**
    ⇒ 那条检查形同虚设（测试当场抓到）。两处各写一次正是它发生的原因。
    """
    return code.replace('SEC-US-', 'US_')


def panel_path(code: str, panels: Path) -> Path:
    return panels / f'{file_code(code)}.csv.gz'


def raw_max_session(code: str, raw_root: Path) -> str | None:
    parts = sorted((raw_root / KIND / AUTYPE).glob(f'year=*/{file_code(code)}.csv.gz'))
    best = None
    for p in parts:
        frame = pd.read_csv(p, usecols=lambda c: c in ('time_key', 'session'))
        col = 'time_key' if 'time_key' in frame.columns else 'session'
        if frame.empty:
            continue
        value = pd.to_datetime(frame[col]).dt.normalize().max()
        best = value if best is None or value > best else best
    return None if best is None else str(best.date())


def panel_max_session(code: str, panels: Path) -> str | None:
    p = panel_path(code, panels)
    if not p.exists():
        return None
    frame = pd.read_csv(p, usecols=['session'])
    if frame.empty:
        return None
    return str(pd.to_datetime(frame.session).dt.normalize().max().date())


def checkpoint_stale(codes: list, *, checkpoint: Path, raw_root: Path) -> list:
    if not checkpoint.exists():
        return [{'problem': 'CHECKPOINT_MISSING', 'path': str(checkpoint)}]
    state = json.loads(checkpoint.read_text())
    completed = state.get('completed') or {}
    wanted = {f'{c}|{KIND}|{AUTYPE}' for c in codes}
    out = []
    for key, record in completed.items():
        parts = key.split('|')
        # **只查下载器真正消费的那种键形状**：它用四段键
        # `{code}|{kind}|{autype}|{year}`（`download_market_history.py:98`），
        # 而检查点里另有 121 条**三段旧格式**遗留键（AXTI/LITE/MU 等），下载器从不查它们。
        # 我第一版把两种都查 ⇒ 对 LITE/MU 恒报「检查点不符」的**假警**，
        # 而假警与真警长得一模一样，看护就废了（本项目已有过一次教训）。
        if len(parts) != 4:
            continue
        code, kind, autype, year = parts
        code = code.replace('US.', 'SEC-US-')
        if f'{code}|{KIND}|{AUTYPE}' not in wanted:
            continue
        path = raw_root / kind / autype / f'year={year}' / f'{file_code(code)}.csv.gz'
        if not path.exists():
            continue
        actual = _sha256(path)
        if record.get('sha256') != actual:
            out.append({'problem': 'PARTITION_VS_CHECKPOINT', 'key': key,
                        'recorded': (record.get('sha256') or '')[:16],
                        'actual': actual[:16]})
    return out


def check(*, which: str = 'tech', panels: Path = PANELS, raw_root: Path = DEFAULT_RAW_ROOT,
          checkpoint: Path = DEFAULT_CHECKPOINT) -> dict:
    codes = universe_names(which)
    behind = []
    for code in codes:
        raw, panel = raw_max_session(code, raw_root), panel_max_session(code, panels)
        if raw is not None and (panel is None or raw > panel):
            behind.append({'security_id': code, 'raw': raw, 'panel': panel})
    stale = checkpoint_stale(codes, checkpoint=checkpoint, raw_root=raw_root)
    return {'universe': which, 'checked': len(codes),
            'panels_behind_raw': behind, 'checkpoint_mismatch': stale,
            'ok': not behind and not stale,
            'problems': ([f'PANELS_BEHIND_RAW:{len(behind)}']
                         if behind else []) +
                        ([f'CHECKPOINT_MISMATCH:{len(stale)}'] if stale else [])}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='data_integrity_check')
    ap.add_argument('--universe', default='tech', choices=('tech', 'forward-arms'))
    ap.add_argument('--panels', default=str(PANELS))
    ap.add_argument('--raw-root', default=str(DEFAULT_RAW_ROOT))
    ap.add_argument('--checkpoint', default=str(DEFAULT_CHECKPOINT))
    args = ap.parse_args(argv)
    result = check(which=args.universe, panels=Path(args.panels),
                   raw_root=Path(args.raw_root), checkpoint=Path(args.checkpoint))
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
