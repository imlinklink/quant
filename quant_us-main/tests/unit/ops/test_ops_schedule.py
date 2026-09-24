"""`ops/` 的排程规格与看护的简报新鲜度判定。

这里钉死两件 2026-09-20 修掉的事：

1. **周末假警**。简报由 cron 在北京周一~周五 08:20 产出，而原判定拿 `date.today()`
   比 —— 于是每个周末每 5 分钟报一次"简报不是今天的"（还叠上周一 08:20 之前那几个
   小时）。假警把真警淹掉，与端口 8899/8890 写错是同一个病。
2. **两份定义**。`install_cron` 写死 `20 8 * * 1-5`、`watchdog` 另判"今天"，两者没有
   任何关系：排程挪半小时，看护就会在错的时间天天假报。现在只有一个 `jobs_spec.BRIEF`。

测试里**绝不允许真的弹告警**（`display alert` 会阻塞 60 秒并抢焦点），见 `_no_alert`。
"""
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]        # quant 根目录（ops/ 的上一级）
sys.path.insert(0, str(REPO / 'ops'))

import install_cron     # noqa: E402
import install_launchd  # noqa: E402
import jobs_spec        # noqa: E402
import watchdog         # noqa: E402

BRIEF = jobs_spec.BRIEF

# 2026-09-18 周五 / 09-19 周六 / 09-20 周日 / 09-21 周一（与生产实测同一周）
FRI, SAT, SUN, MON = date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20), date(2026, 9, 21)


class TestCronWeekdayField:
    def test_weekdays_compress_to_range(self):
        assert jobs_spec.cron_weekday_field((1, 2, 3, 4, 5)) == '1-5'

    def test_single_day(self):
        assert jobs_spec.cron_weekday_field((3,)) == '3'

    def test_two_days_use_comma_not_hyphen(self):
        # `1-2` 也合法，但两段之间的连字符容易被读成笔误
        assert jobs_spec.cron_weekday_field((1, 3)) == '1,3'

    def test_sunday_seven_normalises_to_zero(self):
        # cron 里 0 与 7 都是周日。存 7 渲染成 7 的话，看护按 `%w`（0=周日）判会**整天错位**
        assert jobs_spec.cron_weekday_field((7,)) == '0'

    def test_unsorted_and_duplicated_input(self):
        assert jobs_spec.cron_weekday_field((5, 1, 3, 2, 4, 1)) == '1-5'

    def test_split_ranges(self):
        assert jobs_spec.cron_weekday_field((1, 2, 4, 5)) == '1,2,4,5'

    def test_empty_rejected(self):
        with pytest.raises(ValueError):
            jobs_spec.cron_weekday_field(())


class TestLatestExpectedDate:
    """期望的是「≤ 现在的最近一次排程」，不是「今天是不是工作日」。"""

    def _at(self, y, m, d, hh, mm):
        return jobs_spec.latest_expected_date(datetime(y, m, d, hh, mm))

    def test_sunday_expects_friday(self):
        assert self._at(2026, 9, 20, 21, 13) == FRI

    def test_saturday_expects_friday(self):
        assert self._at(2026, 9, 19, 20, 0) == FRI

    def test_scheduled_day_before_the_hour_expects_the_previous_one(self):
        # 周一 07:00，而简报 08:20 才跑 → 今天那份**还不该存在**。
        # 只判"是不是工作日"会在这里假报，等于把周末的假警搬到周一早晨。
        assert self._at(2026, 9, 21, 7, 0) == FRI

    def test_exactly_at_the_scheduled_minute_expects_today(self):
        assert self._at(2026, 9, 21, 8, 20) == MON

    def test_after_the_hour_expects_today(self):
        assert self._at(2026, 9, 21, 9, 0) == MON


class TestBriefFreshness:
    def _brief(self, tmp_path, payload):
        p = tmp_path / 'latest.json'
        p.write_text(json.dumps(payload), encoding='utf-8')
        return p

    def test_saturday_brief_is_fresh_on_sunday(self, tmp_path):
        # 生产实测的原始场景：周日 21:13，简报日期 09-19，期望 09-18 ⇒ 最新。
        # 修复前这里报"简报不是今天的"，每 5 分钟一次、整个周末不停。
        p = self._brief(tmp_path, {'date': '2026-09-19', 'risk_level': 'cautious'})
        ok, data, expected, _ = watchdog.brief_fresh(p, now=datetime(2026, 9, 20, 21, 13))
        assert ok is True and expected == FRI and data['risk_level'] == 'cautious'

    def test_friday_brief_is_fresh_on_sunday(self, tmp_path):
        p = self._brief(tmp_path, {'date': '2026-09-18'})
        assert watchdog.brief_fresh(p, now=datetime(2026, 9, 20, 21, 13))[0] is True

    def test_thursday_brief_is_stale_on_sunday(self, tmp_path):
        # 过期两天 —— 这是真的没产出，必须报（不能把判定整体放松）
        p = self._brief(tmp_path, {'date': '2026-09-17'})
        assert watchdog.brief_fresh(p, now=datetime(2026, 9, 20, 21, 13))[0] is False

    def test_monday_morning_before_the_brief_run_is_not_a_false_alarm(self, tmp_path):
        p = self._brief(tmp_path, {'date': '2026-09-18'})
        assert watchdog.brief_fresh(p, now=datetime(2026, 9, 21, 7, 0))[0] is True

    def test_monday_after_the_brief_run_requires_today(self, tmp_path):
        p = self._brief(tmp_path, {'date': '2026-09-18'})
        assert watchdog.brief_fresh(p, now=datetime(2026, 9, 21, 9, 0))[0] is False

    def test_missing_file_is_stale_with_reason(self, tmp_path):
        ok, _, _, detail = watchdog.brief_fresh(tmp_path / 'nope.json',
                                                now=datetime(2026, 9, 20, 21, 13))
        assert ok is False and '读取失败' in detail

    def test_missing_date_field_is_stale(self, tmp_path):
        p = self._brief(tmp_path, {'risk_level': 'cautious'})
        assert watchdog.brief_fresh(p, now=datetime(2026, 9, 20, 21, 13))[0] is False


class TestScheduleIsSingleSource:
    """看护的期望时刻与**实际安装的排程**必须是同一份规格。

    这是本次修复的核心断言：修复前 crontab 里写死 `20 8 * * 1-5`、看护里写
    `date.today()`，两者毫无关系 —— 排程改了判定不会跟着改。

    2026-09-23 起简报由 **launchd** 驱动（cron 不补跑睡过的任务，08:20 落在睡眠窗口里，
    实测连着两天被吞掉），所以下面改成从 **launchd 的 calendar** 回读 ——
    断言的对象从 crontab 行换成 plist 里的 `StartCalendarInterval`，**意图不变**。
    """

    def _brief_launchd_calendar(self) -> list:
        cal = install_launchd.JOBS['market-brief'].get('calendar')
        if not cal:
            raise AssertionError('JOBS[market-brief] 里没有 calendar')
        return cal

    def test_launchd_calendar_comes_from_brief_spec(self):
        cal = self._brief_launchd_calendar()
        # 第一次尝试必须与 BRIEF 同源（看护判"该不该已有简报"用的就是 BRIEF 的时刻）
        self._assert_matches_brief(cal)
        # 允许**兜底尝试**，但它们的时刻也必须由 BRIEF 推出来（不得手写一个别的时刻）
        for c in cal:
            assert c['Minute'] == BRIEF['minute']
            assert c['Hour'] in (BRIEF['hour'], (BRIEF['hour'] + 2) % 24)

    @staticmethod
    def _assert_matches_brief(cal):
        from jobs_spec import launchd_calendar
        assert cal[:len(launchd_calendar(BRIEF))] == launchd_calendar(BRIEF), \
            '前几次尝试必须与 BRIEF 完全一致'

    def test_judgement_follows_the_rendered_calendar(self):
        """把渲染出的 launchd calendar **当成一份独立排程**回读，判定必须一致。

        比"两边都从 BRIEF 取值"更强：它读的是最终写进 plist 的东西，所以哪天有人在
        `JOBS` 里手写死一个时刻，这条会失败（而不是静静失配）。
        """
        cal = self._brief_launchd_calendar()
        # launchd Weekday 1=周一…7=周日 → cron 约定 0=周日…6=周六
        days = sorted({c['Weekday'] % 7 for c in cal})
        spec = {'hour': cal[0]['Hour'], 'minute': cal[0]['Minute'], 'weekdays': tuple(days)}
        assert jobs_spec.latest_expected_date(datetime(2026, 9, 20, 21, 13), spec) == FRI
        assert jobs_spec.latest_expected_date(datetime(2026, 9, 21, 8, 19), spec) == FRI
        assert jobs_spec.latest_expected_date(datetime(2026, 9, 21, 8, 20), spec) == MON

    def test_brief_is_no_longer_in_crontab(self):
        """搬走之后 crontab 里**不得**再有简报那一行 —— 否则一天跑两次。"""
        for line in install_cron.build_block().splitlines():
            assert '--mode morning' not in line or line.lstrip().startswith('#'), \
                '简报已经搬到 launchd，crontab 里不该还有它'

    def test_one_owner_for_the_brief(self):
        """简报只能有**一个**驱动者。这条防的是"搬了一半、两边都在跑"。"""
        in_cron = any('--mode morning' in ln and not ln.lstrip().startswith('#')
                      for ln in install_cron.build_block().splitlines())
        in_launchd = 'market-brief' in install_launchd.JOBS
        assert in_launchd and not in_cron, f'launchd={in_launchd} cron={in_cron}'


class TestProblemMessagesAreStable:
    """告警去重键是 `problems` 的**集合**（`alert_if_changed`）——

    文案里带上日期，"同一个持续问题"就会每天变一次、天天弹一次模态框。
    本仓库已经为此把 `shadow_job_check` 的文案刻意写成不含数字与日期的。
    """

    @staticmethod
    def _no_alert(*a, **k):
        # 测试里真弹一次 `display alert` 会阻塞 60 秒并抢走焦点 —— 宁可直接失败。
        raise AssertionError('测试不允许真的弹告警')

    def _problems_at(self, tmp_path, monkeypatch, brief_date, now):
        brief = tmp_path / 'brief.json'
        brief.write_text(json.dumps({'date': brief_date}), encoding='utf-8')
        monkeypatch.setattr(watchdog, 'port_open', lambda *a, **k: True)
        monkeypatch.setattr(watchdog, 'http_ok', lambda *a, **k: True)
        monkeypatch.setattr(watchdog, 'drift_checks', lambda: [])
        monkeypatch.setattr(watchdog, 'shadow_job_check', lambda days=3: (0, ''))
        monkeypatch.setattr(watchdog, 'LOG_DIR', tmp_path)
        monkeypatch.setattr(watchdog, 'alert_if_changed', lambda p: None)
        monkeypatch.setattr(watchdog, 'notify', self._no_alert)
        cfg = {'opend_port': 11111, 'services': [],
               'markets': [{'name': 'us', 'enabled': True, 'brief_path': str(brief)}]}
        watchdog.check_once(cfg, now=now)
        return json.loads((tmp_path / 'status.json').read_text(encoding='utf-8'))['problems']

    def test_stale_brief_message_identical_across_days(self, tmp_path, monkeypatch):
        # 同一条过期简报：周日看与两天后看，文案必须**一字不差**
        a = self._problems_at(tmp_path, monkeypatch, '2026-09-01', datetime(2026, 9, 20, 21, 13))
        b = self._problems_at(tmp_path, monkeypatch, '2026-09-01', datetime(2026, 9, 22, 21, 13))
        assert a == b
        assert a == ['[us] 简报不是最新的，需要运行 run_market_brief.py']

    def test_fresh_brief_reports_nothing(self, tmp_path, monkeypatch):
        probs = self._problems_at(tmp_path, monkeypatch, '2026-09-19',
                                  datetime(2026, 9, 20, 21, 13))
        assert probs == []


class TestAlertOnlyOnNewProblems:
    """告警的触发条件必须是「**新增**了问题」，不是「集合变了」。

    纯措辞修改（本次就把"简报不是今天的"改成了"简报不是最新的"）会让集合变化，
    若按"变了就弹"，就会弹一遍用户早就看到过的内容 —— 而重复的告警没人看，
    与没有告警等价。
    """

    def _run(self, tmp_path, monkeypatch, last, now):
        state = tmp_path / 'alert_state.json'
        state.write_text(json.dumps({'key': last}), encoding='utf-8')
        monkeypatch.setattr(watchdog, 'STATE_PATH', state)
        monkeypatch.setattr(watchdog, 'log', lambda m: None)
        popped = []
        monkeypatch.setattr(watchdog, 'notify',
                            lambda t, m: popped.append(m) or {'seen': True})
        result = watchdog.alert_if_changed(list(now))
        saved = json.loads(state.read_text(encoding='utf-8'))['key']
        return result, popped, saved

    def test_only_resolutions_does_not_pop(self, tmp_path, monkeypatch):
        result, popped, saved = self._run(tmp_path, monkeypatch, ['A', 'B'], ['A'])
        assert result is None and popped == [] and saved == ['A']

    def test_all_clear_does_not_pop(self, tmp_path, monkeypatch):
        result, popped, saved = self._run(tmp_path, monkeypatch, ['A'], [])
        assert result is None and popped == [] and saved == []

    def test_new_problem_pops(self, tmp_path, monkeypatch):
        result, popped, saved = self._run(tmp_path, monkeypatch, ['A'], ['A', 'B'])
        assert len(popped) == 1 and saved == ['A', 'B']

    def test_first_problem_pops(self, tmp_path, monkeypatch):
        result, popped, _ = self._run(tmp_path, monkeypatch, [], ['A'])
        assert len(popped) == 1

    def test_replaced_problem_pops(self, tmp_path, monkeypatch):
        # 一个消失、另一个出现 —— 是新增，要弹
        result, popped, _ = self._run(tmp_path, monkeypatch, ['A', 'B'], ['A', 'C'])
        assert len(popped) == 1

    def test_unchanged_does_not_pop(self, tmp_path, monkeypatch):
        result, popped, _ = self._run(tmp_path, monkeypatch, ['A'], ['A'])
        assert result is None and popped == []


class TestNoBareVarBeforeMultibyte:
    """macOS 自带 bash 3.2 会把 `$VAR` **紧邻的 UTF-8 字节吞进变量名**。

    实测：`set -u; HOLDER=1; echo "$HOLDER，锁"` →
    `HOLDER\\xef\\xbc\\x8c: unbound variable`，退出码 127/1。写成 `${HOLDER}` 就正常。

    本仓库的脚本里全是中文，所以下一行 `echo "... $VAR，..."` 随时会踩到 —— 而这类
    问题只在**那个分支真的被执行**时才暴露（`shadow_daily.sh` 的"锁被占用"与"日报目录
    不存在"两条分支就一直是坏的，直到 2026-09-20 实测才被发现）。
    全仓扫描，只扫非注释行。
    """
    PATTERN = re.compile(r'\$(\w+)([^\x00-\x7f])')

    def test_no_shell_script_has_the_trap(self):
        offenders = []
        for sh in sorted((REPO / 'ops').glob('*.sh')):
            for n, line in enumerate(sh.read_text(encoding='utf-8').splitlines(), 1):
                if line.lstrip().startswith('#'):        # 注释不执行
                    continue
                for m in self.PATTERN.finditer(line):
                    offenders.append(f'{sh.name}:{n}: ${m.group(1)}{m.group(2)}')
        assert offenders == [], (
            '这些行在 bash 3.2 下会 unbound variable，请改成 ${VAR}：\n  ' + '\n  '.join(offenders))

    def test_the_trap_really_is_a_trap(self):
        """反证：这条规则不是凭空加的 —— 无花括号确实失败，加花括号确实成功。

        没有这条，"扫描器"可能因为正则写错而永远通过（本仓库真出过"机制看起来在工作"）。
        """
        bare = 'set -u; HOLDER=1; echo "$HOLDER，锁"'
        assert subprocess.run(['/bin/bash', '-c', bare],
                              capture_output=True).returncode != 0
        braced = 'set -u; HOLDER=1; echo "${HOLDER}，锁"'
        proc = subprocess.run(['/bin/bash', '-c', braced], capture_output=True, text=True)
        assert proc.returncode == 0 and proc.stdout.strip() == '1，锁'


# ─── 星期的两套约定（cron 0=周日 / launchd 1=周一）────────────────────────────
# 本仓库已经因为星期约定错过一次（config.yaml 的 protocol_review.weekday 把美东周六
# 当成过周五），所以显式换算 + 显式测周日。
#
# 盘前简报从 cron 搬到 launchd（2026-09-23）的起因：实测连着两天（09-22、09-23）没跑。
# 机器 08:0x 入睡、08:3x 靠开盖才醒，而 **macOS 的 cron 不在唤醒后补跑** ⇒ 08:20 正好
# 落在睡眠窗口里被吞掉。时刻与星期仍来自 `jobs_spec.BRIEF`（唯一来源），搬迁不改变排程。


def test_launchd_weekday_converts_between_the_two_conventions():
    assert [jobs_spec.launchd_weekday(d) for d in (1, 2, 3, 4, 5, 6)] == [1, 2, 3, 4, 5, 6]
    assert jobs_spec.launchd_weekday(0) == 7          # cron 周日 → launchd 7
    assert jobs_spec.launchd_weekday(7) == 7          # cron 也接受 7 表示周日


def test_both_renderers_describe_the_same_days():
    """crontab 与 launchd 两种渲染必须落在同一组星期上（往返一致）。"""
    from_cron = {int(d) % 7 for d in BRIEF['weekdays']}
    from_launchd = {c['Weekday'] % 7 for c in jobs_spec.launchd_calendar(BRIEF)}
    assert from_cron == from_launchd, f'{from_cron} != {from_launchd}'


def test_market_brief_job_runs_from_the_dev_checkout():
    """简报不是实验，没有运行版本 —— 它跑的是开发 checkout（与搬到 launchd 之前一致）。

    2026-09-24 起改成跑**包装脚本**而不是直接跑 pipeline：这台机器早上 09:20 才苏醒并联网，
    而 launchd 的补跑发生在醒来那一瞬间 ⇒ LLM 调用 DNS 失败（实测 09:03，Errno 8）。
    脚本先等 DNS 与 OpenD 就绪再跑。
    """
    spec = install_launchd.JOBS['market-brief']
    assert spec['label'] == 'com.quant.market-brief'
    assert spec['workdir'] == str(REPO)
    argv = ' '.join(spec['argv'])
    assert 'market_brief_daily.sh' in argv, '应当跑包装脚本（等网络就绪）'
    script = REPO / 'ops' / 'market_brief_daily.sh'
    assert script.exists(), '包装脚本不存在'
    text = script.read_text(encoding='utf-8')
    # 就绪等待**抽到了共用模块**（`shop_daily` / `forward_arms` 用同一份）——脚本里只应看到调用
    assert 'wait_ready.py' in text, '应当调用共用的就绪等待 ops/wait_ready.py'
    waiter = (REPO / 'ops' / 'wait_ready.py').read_text(encoding='utf-8')
    assert 'socket.create_connection' in waiter and 'gethostbyname' in waiter, \
        '共用模块里没有就绪判据（DNS + OpenD）'
    # 三件关键性质：幂等、跑完核对产物日期
    assert 'market_brief/latest.json' in text and 'SKIP 今天已有简报' in text, \
        '缺少幂等守卫（多次尝试会重复调 LLM、覆盖当天简报）'
    assert 'BRIEF_DATE' in text, '跑完没有核对产出的简报是不是今天的'


def test_brief_script_is_executable_and_syntax_ok():
    """脚本得真的能跑 —— 语法错会让 launchd 每次都以 exit 2 失败，且没人看。"""
    import subprocess
    script = REPO / 'ops' / 'market_brief_daily.sh'
    assert script.stat().st_mode & 0o111, '脚本没有可执行位'
    r = subprocess.run(['/bin/bash', '-n', str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f'bash -n 失败：{r.stderr}'


# ─── 运行 checkout 的 config 必须与开发 checkout 一致 ─────────────────────────
# 2026-09-22 隔离搬迁时 `config.yaml` 没跟着走 ⇒ worktree 从 git 拿到 HEAD 的 93 行初始版，
# `trend_breakout.enabled` 与 `llm` 全缺 ⇒ **突破线监控器不启动、模型没有 key**，
# 静默约 24 小时（既有三项核对看的是部署定义与记录，不是运行 checkout 里的配置内容）。

def _check_runtime_config():
    sys.path.insert(0, str(REPO / 'ops'))
    import check_runtime_config
    return check_runtime_config


def test_runtime_config_matches_dev_checkout(tmp_path):
    """本机实际的三个 checkout 必须一致（不一致就是那次事故的形态）。"""
    crc = _check_runtime_config()
    assert crc.compare(crc.DEV_CONFIG, crc.RUNTIME_CHECKOUTS) == []


def test_missing_blocks_are_named(tmp_path):
    """缺块时必须**点名**缺了哪几个 —— 只说"不一致"找不到原因。"""
    crc = _check_runtime_config()
    dev = tmp_path / 'dev' / 'quant_us-main'
    rt = tmp_path / 'quant-runtime-x' / 'quant_us-main'
    dev.mkdir(parents=True); rt.mkdir(parents=True)
    (dev / 'config.yaml').write_text('llm:\n  enabled: true\ntrend_breakout:\n  enabled: true\n',
                                     encoding='utf-8')
    (rt / 'config.yaml').write_text('llm:\n  enabled: true\n', encoding='utf-8')
    problems = crc.compare(dev / 'config.yaml', (tmp_path / 'quant-runtime-x',))
    assert len(problems) == 1
    assert 'trend_breakout' in problems[0]
    assert 'quant-runtime-x' in problems[0]


def test_identical_copy_passes(tmp_path):
    crc = _check_runtime_config()
    dev = tmp_path / 'dev' / 'quant_us-main'
    rt = tmp_path / 'quant-runtime-y' / 'quant_us-main'
    dev.mkdir(parents=True); rt.mkdir(parents=True)
    body = 'llm:\n  enabled: true\n'
    (dev / 'config.yaml').write_text(body, encoding='utf-8')
    (rt / 'config.yaml').write_text(body, encoding='utf-8')
    assert crc.compare(dev / 'config.yaml', (tmp_path / 'quant-runtime-y',)) == []


def test_missing_runtime_config_is_reported(tmp_path):
    crc = _check_runtime_config()
    dev = tmp_path / 'dev' / 'quant_us-main'
    rt = tmp_path / 'quant-runtime-z' / 'quant_us-main'
    dev.mkdir(parents=True); rt.mkdir(parents=True)
    (dev / 'config.yaml').write_text('llm: {}\n', encoding='utf-8')
    problems = crc.compare(dev / 'config.yaml', (tmp_path / 'quant-runtime-z',))
    assert len(problems) == 1 and '没有 config.yaml' in problems[0]
