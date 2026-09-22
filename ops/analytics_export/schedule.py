"""定时任务的事实采集（需求：用户要一页看全"有哪些定时任务"）。

**只采"已安装"的状态，不采定义文件**。理由是本仓库最贵的教训就在这条线上：
「代码是对的、装上去的是旧的」—— `ops/install_launchd.py` 里写着作业，但真正会跑的是
`~/Library/LaunchAgents/*.plist` 与 `crontab -l`。两者不一致时，页面必须显示**实际会跑的**。

**这里跑外部命令（launchctl / crontab / git / ps）** —— 这一层是**批处理导出器**，
不是 web；web 只读导出的 JSON。采不到的字段一律标未采集，**不猜、不填 0**。

三条刻意的如实标注（都是本项目踩过的）：
1. **退出码 0 ≠ 做了有用的工作** —— `launchctl` 只记进程退出码，作业跑成功但什么都没做
   （「没有新 session」）在退出码上看不出来。
2. **cron 不记录退出码，且睡过不补跑** —— macOS 的 cron 在机器睡眠期间错过就真的错过。
3. **`KeepAlive` 的作业没有"上次退出码"** 的意义 —— 它是重启策略，退出会被立刻拉起。
"""
from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import contract as C

LAUNCH_AGENTS = Path.home() / 'Library' / 'LaunchAgents'
LABEL_PREFIX = 'com.quant.'
WEEKDAY_CN = {0: '周日', 1: '周一', 2: '周二', 3: '周三', 4: '周四', 5: '周五', 6: '周六',
              7: '周日'}


def _run(cmd: list, timeout=15, env_c: bool = False):
    env = None
    if env_c:
        import os
        env = {**os.environ, 'LC_ALL': 'C', 'LANG': 'C'}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _schedule_text(plist: dict) -> tuple:
    cal = plist.get('StartCalendarInterval')
    if cal:
        parts = []
        for c in (cal if isinstance(cal, list) else [cal]):
            wd = WEEKDAY_CN.get(c.get('Weekday'))
            hh, mm = c.get('Hour'), c.get('Minute')
            parts.append(f"{wd or '每天'} {hh:02d}:{mm:02d}" if hh is not None else '每天')
        return '、'.join(parts) + '（本地时区）', cal
    if plist.get('StartInterval'):
        n = plist['StartInterval']
        return f"每 {n} 秒（睡醒后补一次）", None
    return '未声明时间（可能是常驻）', None


def _launchctl_states() -> dict:
    """`launchctl list` → `{label: {pid, last_exit}}`。取不到就空 —— 不猜。"""
    out = _run(['launchctl', 'list'])
    if out is None:
        return {}
    states = {}
    for line in out.splitlines()[1:]:
        parts = line.split('\t')
        if len(parts) < 3 or not parts[2].startswith(LABEL_PREFIX):
            continue
        pid, status, label = parts[0], parts[1], parts[2]
        states[label] = {
            'pid': None if pid == '-' else int(pid),
            'last_exit_status': None if status == '-' else int(status),
        }
    return states


def _proc_start(pid) -> str | None:
    if not pid:
        return None
    # **必须 LC_ALL=C**：本机 locale 是中文时 `ps -o lstart=` 输出「二  9/22 17:20:42 2026」，
    # 定长格式串匹配不了（实测踩到）。C locale 下才是 `Tue Sep 22 17:20:42 2026`。
    out = _run(['/bin/ps', '-o', 'lstart=', '-p', str(pid)], env_c=True)
    return out.strip() if out and out.strip() else None


def _git_head_time(path: str) -> dict:
    head = _run(['git', '-C', path, 'log', '-1', '--format=%H%n%cI'])
    if not head:
        return {'sha': None, 'committed_at': None}
    sha, _, when = head.strip().partition('\n')
    return {'sha': sha.strip()[:8], 'committed_at': when.strip()}


def collect_launchd() -> list:
    jobs = []
    if not LAUNCH_AGENTS.exists():
        return jobs
    for plist_path in sorted(LAUNCH_AGENTS.glob(f'{LABEL_PREFIX}*.plist')):
        try:
            plist = plistlib.load(open(plist_path, 'rb'))
        except Exception as exc:
            jobs.append({'label': plist_path.stem, 'installed': True,
                         'status': C.READ_FAILED, 'why': f'plist 读不动：{exc!r}'})
            continue
        label = plist.get('Label') or plist_path.stem
        text, cal = _schedule_text(plist)
        jobs.append({
            'label': label, 'installed': True, 'plist': str(plist_path),
            'schedule_text': text, 'calendar': cal,
            'argv': plist.get('ProgramArguments', []),
            'workdir': plist.get('WorkingDirectory'),
            'env': {k: v for k, v in (plist.get('EnvironmentVariables') or {}).items()
                    if k != 'PATH'},
            'keep_alive': bool(plist.get('KeepAlive')),
            'process_type': plist.get('ProcessType'),
        })
    return jobs


def collect_cron() -> tuple:
    raw = _run(['crontab', '-l'])
    if raw is None:
        return [], False
    entries = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        parts = s.split(None, 5)
        if len(parts) < 6:
            continue
        minute, hour, dom, month, dow, cmd = parts
        entries.append({'spec': ' '.join(parts[:5]), 'minute': minute, 'hour': hour,
                        'day_of_week': dow, 'command': cmd})
    return entries, True


def _cron_text(entry: dict) -> str:
    dow = entry.get('day_of_week', '*')
    hh, mm = entry.get('hour'), entry.get('minute')
    days = {'1-5': '周一~周五', '6': '周六', '0': '周日', '*': '每天'}.get(dow, f'周{dow}')
    if hh.isdigit() and mm.isdigit():
        return f'{days} {int(hh):02d}:{int(mm):02d}（本地时区）'
    return f'{days}（分={mm} 时={hh}）'


def _within(path: str, root: str) -> bool:
    """`path` 是否就在 `root` 里 —— **按路径分量比，不做字符串前缀**。

    字符串前缀会把 `/Users/wh1817w/quant-runtime-main/...` 判成在 `/Users/wh1817w/quant`
    里面（后者是前者的字符串前缀）⇒ 实测导致开发 checkout 被误报 STALE。
    本仓库在删除守卫上踩过同一个坑（子串匹配），这是第二次。
    """
    try:
        p, r = Path(path).resolve(), Path(root).resolve()
    except OSError:
        return False
    return p == r or r in p.parents


def _parse_lstart(s: str):
    """`ps -o lstart=` 的形态：`Tue Sep 22 17:20:42 2026`（本地时区、无年份歧义）。"""
    try:
        return datetime.strptime(s.strip(), '%a %b %d %H:%M:%S %Y')
    except (ValueError, AttributeError):
        return None


def collect(base: Path) -> dict:
    """全部定时任务的**已安装事实** + 运行版本核对。"""
    now = datetime.now(timezone.utc)
    jobs = collect_launchd()
    states = _launchctl_states()
    runtime_roots = set()
    for j in jobs:
        st = states.get(j['label'])
        j['pid'] = (st or {}).get('pid')
        j['last_exit_status'] = (st or {}).get('last_exit_status')
        j['running'] = bool(j['pid'])
        if j['keep_alive']:
            # KeepAlive 的作业退出会被立刻拉起，「上次退出码」不是状态指示
            j['exit_meaning'] = '此作业是 KeepAlive：退出即被拉起，退出码只反映上一次退出的原因'
        elif j['last_exit_status'] is None:
            j['exit_meaning'] = C.NOT_COLLECTED
            j['exit_why'] = 'launchctl 里没有该作业的记录（装了吗？）'
        elif j['last_exit_status'] == 0:
            j['exit_meaning'] = '上次正常退出'
        else:
            j['exit_meaning'] = f'上次异常退出（码 {j["last_exit_status"]}）'
        j['process_started_at'] = _proc_start(j['pid'])

    # 运行版本核对只针对**作业的工作目录**（绝对路径）—— 不从 argv 里瞎猜：
    # 第一版把所有 argv 里的父目录都当候选，于是相对路径 `.` 也被算成一个「根目录」
    # （`Path('run_all.py').parents == ['.']`，而 cwd 恰好有 quant_us-main 与 ops）。
    runtime_roots = {str(j['workdir']) for j in jobs
                     if j.get('workdir') and str(j['workdir']).startswith('/')}

    cron, cron_ok = collect_cron()
    for e in cron:
        e['schedule_text'] = _cron_text(e)

    # ---- 运行版本核对：直接盯住「装上去的东西是旧的」那条缺口 ----
    version_checks = []
    seen_services = {}
    for root in sorted(runtime_roots):
        head = _git_head_time(root)
        owner = next((j for j in jobs
                      if j.get('workdir') and _within(j['workdir'], root)
                      and j['keep_alive']), None)
        if owner:
            # 嵌套的根（如 runtime-main 与 runtime-main/quant_us-main）会指向同一个服务 ⇒
            # 只保留最外层那一条，免得看起来像「查了两遍」
            prev = seen_services.get(owner['label'])
            if prev and len(root) >= len(prev):
                continue
            seen_services[owner['label']] = root
        started = owner.get('process_started_at') if owner else None
        verdict, why = C.NOT_COLLECTED, ''
        committed = None
        if head.get('committed_at'):
            try:
                committed = datetime.fromisoformat(head['committed_at']) \
                    .astimezone().replace(tzinfo=None)
            except ValueError:
                committed = None
        started_dt = _parse_lstart(started) if started else None
        if committed and started_dt:
            if started_dt < committed:
                verdict = C.STALE
                why = (f'服务进程启动于 {started_dt:%Y-%m-%d %H:%M:%S}，'
                       f'而该目录最近一次提交在 {committed:%Y-%m-%d %H:%M:%S} '
                       f'**之后** ⇒ **服务很可能仍在跑旧代码**，要重启才会加载')
            else:
                verdict = C.OK
                why = (f'服务启动于 {started_dt:%Y-%m-%d %H:%M:%S}，晚于最近一次提交 '
                       f'（{committed:%Y-%m-%d %H:%M:%S}）⇒ 已加载当前代码')
        elif not owner:
            why = '该目录没有常驻进程在跑（只有批处理作业）'
        else:
            why = f'取不到可比较的时刻（提交时间 {head.get("committed_at")} / 启动 {started}）'
        version_checks.append({'root': root, 'head': head, 'service_started_at': started,
                              'service': owner['label'] if owner else None,
                              'verdict': verdict, 'why': why})

    gaps = [
        {'field': '作业代码版本（批处理作业）', 'status': C.NOT_IMPLEMENTED,
         'why': '**未实现**：上面只核对了**常驻服务**。批处理作业每次运行都会重新加载代码，'
                '但它们跑的是各自 checkout 的 HEAD —— 「某个作业上次跑的时候用的是哪版代码」'
                '没有记录。这正是 2026-09-22 踩过的那次（代码已更新、服务仍跑旧的）'},
        {'field': 'cron 的执行结果', 'status': C.NOT_COLLECTED,
         'why': 'macOS 的 cron **不记录退出码**，且**睡过不补跑**；只能看日志文件的写入时间判断'},
        {'field': '定义与已安装的漂移', 'status': C.NOT_COLLECTED,
         'why': '本页只显示**已安装**的（实际会跑的）。「定义里有但没装」的漂移由看护的 '
                'install_launchd --check / install_cron --check 负责，那两项从开发 checkout 跑'},
    ]
    return {
        'generated_at': now.isoformat(),
        'source': {'plists_dir': str(LAUNCH_AGENTS), 'launchctl_read': bool(states),
                   'crontab_read': cron_ok},
        'launchd': jobs,
        'cron': cron,
        'version_checks': version_checks,
        'gaps': gaps,
        'notes': [
            '**退出码 0 不等于做了有用的工作** —— 作业跑成功但什么都没做（例如「没有新 session」）'
            '在退出码上看不出来；要判断有没有实际推进，看对应实验页或作业流水。',
            '本页只显示**已安装**的作业（真正会跑的），不显示定义文件里写了什么。',
        ],
    }
