#!/usr/bin/env python3
"""安装/预览/卸载 quant 运营定时任务（crontab）。

用法：
    python3 ops/install_cron.py --print    # 预览将写入的 crontab
    python3 ops/install_cron.py --install  # 安装（幂等，重复执行只保留一份）
    python3 ops/install_cron.py --remove   # 移除 quant-ops 块
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = '/usr/bin/python3'  # cron 环境 PATH 较短，用绝对路径

# 排程来自 `jobs_spec.py`（**单一事实来源**）：`watchdog.py` 用同一份规格判断
# "简报该不该已经更新了"。两处各写一份的话，改了一边就会**每天假报**或**真出事不报**。
# 盘前简报 2026-09-23 起改由 launchd 驱动，这里不再渲染它的 crontab 行；
# 那份规格仍被看护使用，所以本模块不再需要 import 它。

MARK_START = '# >>> quant-ops (auto-managed) >>>'
MARK_END = '# <<< quant-ops <<<'


def build_block() -> str:
    log_dir = ROOT / 'ops' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        MARK_START,
        # 2026-09-19 重装。此前整块指向迁移前的 ~/Documents/quant，六条全在报
        # `Operation not permitted`（TCC）。**实测探针确认**：仓库搬到 ~/quant 之后
        # cron 已能正常执行这里的命令（探针每分钟写一行，两次都成功）。
        # 但「macOS cron 不补跑睡过的任务」这条没变 —— 时间敏感的任务仍应走 launchd。
        # 盘前简报**已搬到 launchd**（2026-09-23，见 install_launchd.py 的 market-brief）：
        # 实测连着两天没跑 —— 机器 08:0x 入睡、08:3x 靠开盖才醒，而 macOS 的 cron
        # **不在唤醒后补跑**，08:20 正好落在睡眠窗口里被吞掉。launchd 睡醒会补跑一次。
        # 时刻仍由 `jobs_spec.BRIEF` 定义（看护判断"该不该有产出"用的是同一份），
        # 所以这里只是不再渲染 crontab 行。
        '# 盘前简报（含选股建议与结果回填）**已移到 launchd**：见 install_launchd.py 的',
        '#   market-brief。原因：macOS cron 不补跑睡过的任务，而 08:20 落在睡眠窗口里。',
        '# 盘后：结果回填（只美股；港股线 2026-09-19 起未恢复）',
        f'10 17 * * 1-5 cd {ROOT} && '
        f'{PY} ops/pipeline.py --mode evening --markets us '
        f'>> {log_dir}/cron_evening.log 2>&1',
        '# 周六：周报（只美股）',
        f'0 10 * * 6 cd {ROOT} && '
        f'{PY} ops/pipeline.py --mode weekly --markets us '
        f'>> {log_dir}/cron_weekly.log 2>&1',
        '# 抄底扫描回填 + 归因（08:35）**已移除**（2026-09-19）：那条线服务的是 `dip_buy`，',
        '#   而 `run_all.py` 已明写「买入主链不再启动 15m dip_buy 监控器」、config 里',
        '#   `standalone_dip_buy: false` ⇒ **写入方不存在了**。`scan_ledger.record_scan` 只有',
        '#   `dip_buy_monitor` 一个调用方，所以扫描流水自 2026-09-13 起就没再增长 ——',
        '#   而那对命令只回看 3 天，于是**永远报「0 条」**，与"今天没事"长得一模一样。',
        '#   留着它等于每个交易日生产两条恒为 0 的日志。新买入主链有自己的评估闭环（sqlite）。',
        '# 港股收盘后(17:35)的扫描回填 + 归因**已移除**（2026-09-19：港股线未恢复）。',
        '#   要恢复就加回来，并同时把 ops_config.yaml 的 markets.hk.enabled 打开，',
        '#   否则看护会每 5 分钟报一次"港股简报不是今天的"。',
        '# R/L 双影子账户每日运行与常驻服务**都不在 cron**：',
        '#   见 install_launchd.py —— shadow-daily（每日三次）与 trading-service（KeepAlive）。',
        '# 看护（watchdog）**已移到 launchd**（StartInterval 300s）：它原在本块里每 5 分钟跑，',
        '#   但块整体是死的，等于没有看护 —— 2026-09-19 服务静默停了 3.5 小时无人知道。',
        MARK_END,
    ]
    return '\n'.join(lines) + '\n'


def current_crontab() -> str:
    try:
        r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=10)
        return r.stdout
    except Exception:
        return ''


def strip_old(content: str) -> str:
    lines = content.splitlines()
    out = []
    skip = False
    for ln in lines:
        if ln.strip() == MARK_START:
            skip = True
            continue
        if ln.strip() == MARK_END:
            skip = False
            continue
        if not skip:
            out.append(ln)
    text = '\n'.join(out).strip()
    return (text + '\n') if text else ''


def install():
    content = current_crontab()
    content = strip_old(content)
    content += build_block()
    p = subprocess.run(['crontab', '-'], input=content, text=True, capture_output=True)
    if p.returncode != 0:
        print('安装失败:', p.stderr)
        return 1
    print('✅ 已安装定时任务（幂等）')
    print()
    print(build_block())
    return 0


def remove():
    content = strip_old(current_crontab())
    p = subprocess.run(['crontab', '-'], input=content, text=True, capture_output=True)
    print('✅ 已移除 quant-ops 定时任务' if p.returncode == 0 else f'移除失败: {p.stderr}')
    return p.returncode


def installed_block() -> str:
    """当前 crontab 里 quant-ops 块的内容（不含起止标记行）。取不到返回 ''。"""
    lines = current_crontab().splitlines()
    out, inside = [], False
    for ln in lines:
        if ln.strip() == MARK_START:
            inside = True
            continue
        if ln.strip() == MARK_END:
            inside = False
            continue
        if inside:
            out.append(ln)
    return '\n'.join(out).strip()


def check() -> int:
    """核对**已安装的** crontab 块与 `build_block()` 是否一致。不一致返回 1。

    为什么需要它：本仓库的失效形态是「代码是对的、**装上去的是旧的**」——
    迁移到 `~/quant` 之后没人重跑 `--install`，于是整块仍指向 `~/Documents`、
    六条任务全在报 `Operation not permitted`，而**代码本身一直是对的**
    （`ROOT = parents[1]` 生成的路径从来没错）。单元测试抓不住这类漂移：
    它测的是"生成器生成什么"，而坏的是"盘上装的是什么"。
    """
    expected = '\n'.join(build_block().splitlines()[1:-1]).strip()   # 去掉起止标记行
    actual = installed_block()
    if actual == expected:
        return 0
    print('❌ 已安装的 crontab 与生成结果不一致（漂移）：')
    if not actual:
        print('   盘上完全没有 quant-ops 块 —— 跑 `--install`')
    else:
        import difflib
        for line in difflib.unified_diff(actual.splitlines(), expected.splitlines(),
                                         fromfile='已安装', tofile='应为', lineterm=''):
            print('   ' + line)
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--print', action='store_true')
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--remove', action='store_true')
    parser.add_argument('--check', action='store_true',
                        help='核对已安装的是否与生成结果一致（漂移检测）')
    args = parser.parse_args()

    if args.print:
        print('当前 crontab 里 quant-ops 块将替换为：')
        print('-' * 60)
        print(build_block())
        return 0
    if args.install:
        return install()
    if args.remove:
        return remove()
    if args.check:
        rc = check()
        print('✅ 已安装的 crontab 与生成结果一致' if rc == 0 else '')
        return rc
    parser.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
