# 影子账户每日自动运行：卡点与待查

写给需要去查资料的人。**先看 §3 和 §8**，前面是背景。

日期：2026-09-18 ｜ 环境：macOS 15.7.3 (24G419)，x86_64 ｜ 仓库在 `~/Documents/quant`

---

## 1. 我要做的事

让 R/L 双影子账户每个交易日**无人值守自动跑一次**，在「T 收盘后 → T+1 开盘前」的窗口里
完成：结算 T 的成交、为 T+1 冻结候选与证据、用真实模型为 T+1 冻结动作。

窗口约 17 小时。**错过窗口等于错过那一天的决策**（不能事后补——补了就是用今天的结果
填当时的记录，那是明确禁止的）。

排程时间选在**北京时间 08:50、周二~周六**（= 美东周一~周五收盘后）：此刻 T 的行情已落定，
而 T+1 的决策截止（美东 09:20）还有约 12 小时。

## 2. 代码侧已经做完并逐项验证过的

昨天（09-17）全部完成，`main` 在 `94aedbf`，全量 965 个测试通过：

| 部件 | 状态 |
|---|---|
| 每日运行脚本 `ops/shadow_daily.sh` | ✅ 手动跑通过：刷新行情 → 抓当日市场日报 → 入库 → `run-daily`，exit 0 |
| 行情刷新（只追加、历史段有断言守着） | ✅ |
| 市场日报（HTML）→ 市场级证据 | ✅ 抽正文 → 入库 → 并入每个候选的证据包 |
| 生产实验 | ✅ 已冻结 `M1-FORWARD-S-20260917`（strict 证据等级） |

**这些都不是纸面结论**：脚本我手动从头到尾跑过，产物我逐项核对过。

## 3. 卡在哪：macOS 的 TCC 不让 `cron` 和 `launchd` 读 `~/Documents`

**`~/Documents` 是 macOS 的受保护目录（TCC）**。`cron` 和 `launchd` 启动的进程**默认没有
访问权**，于是连"打开脚本文件"这一步都做不到：

```
/bin/bash: /Users/wh1817w/Documents/quant/ops/shadow_daily.sh: Operation not permitted
getcwd: cannot access parent directories: Operation not permitted
```

注意是 **`Operation not permitted`（EPERM，errno 1）**，不是 `Permission denied`（EACCES，
errno 13）。文件权限位正常、没有 ACL —— 是 TCC 拦的。

### 这不是我新加的任务独有的问题

**整个 `ops` cron 块已经坏了一段时间**，同样原因：

| 日志 | EPERM 行数 | 最后更新 |
|---|---|---|
| `ops/logs/cron_watchdog.log` | **1942** | 09-18 10:25 |
| `ops/logs/cron_morning.log` | 3 | 09-17 08:20 |
| `ops/logs/cron_evening.log` | 6 | 09-14 17:10 |
| `ops/logs/cron_weekly.log` | 1 | 09-12 10:00 |

```
cron_morning.log:  can't open file 'ops/pipeline.py': EPERM
cron_watchdog.log: can't open file 'ops/watchdog.py': EPERM
cron_scan_backfill.log: can't open file 'scripts/.../backfill_scan_outcomes.py': EPERM
```

也就是说：**盘前简报、盘后回填、看护、周报全都一直在失败**，只是没人看日志。

## 4. 还叠了第二个问题：macOS 的 `cron` 不补跑错过的任务

09-18 早上（机器当时在睡觉）的日志时间戳：

```
cron_morning.log       08:20  → 停在 09-17   ✗ 没触发
cron_scan_backfill.log 08:35  → 停在 09-17   ✗ 没触发
cron_shadow_daily.log  08:50  → 文件不存在   ✗ 没触发
cron_watchdog.log      */5    → 09-18 10:25  ✓ 触发了
```

**睡过的那一次，`cron` 直接跳过，不补跑。** 别的任务无所谓，但我这个窗口只有 17 小时，
跳过就等于丢一天。`launchd` 的 `StartCalendarInterval` 在唤醒后**会补跑**，所以我把
调度器换成了 LaunchAgent —— 这部分已实现并验证排程正确。

## 5. 已试过的两条路，结果一样

| 调度器 | 会不会补跑 | 能不能读 `~/Documents` |
|---|---|---|
| `cron`（`ops/install_cron.py`） | ❌ 不会 | ❌ EPERM |
| `launchd` LaunchAgent（`ops/install_launchd.py`，已装） | ✅ 会 | ❌ **同样 EPERM** |

LaunchAgent 我实测过（`launchctl kickstart` 立刻触发），stderr 就是上面那段。
日志在 `~/Library/Logs/quant/shadow_daily.err.log`。

**所以换调度器解决不了 TCC 那堵墙**，这是我原本没料到的。

## 6. 一处我觉得反常、想请你也看看的地方

`cron` 的 shell 重定向 `>> /Users/.../Documents/quant/ops/logs/cron_watchdog.log` **是成功的**
（日志文件确实在增长、内容就是那些 EPERM），但同一个进程里 `python3 ops/watchdog.py`
**打不开**那个目录下的 `.py` 文件。

**同一受保护目录，写入能成、读取被拒**——这和 TCC "整个目录一起拦"的常见描述不太一致。
不确定是我理解错了，还是有别的机制（沙箱？SIP？某个 entitlement？）。这会直接影响该选哪条修法。

## 7. 我在考虑的三个方案

| 方案 | 要做什么 | 代价 |
|---|---|---|
| **A. 给 `/bin/bash` 授「完全磁盘访问权限」** | 系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 加 `/bin/bash` | **授权很宽**：此后任何 bash 脚本都能读你的全盘，不只这一个任务 |
| **B. 把仓库移出 `~/Documents`**（如 `~/quant`） | `mv`，然后我重装 cron 与 launchd | 全库只有 **1 处**硬编码绝对路径要改（还是个测试），其余都从 `__file__` 推导。2.4G 同卷移动是瞬时的。**顺带修好整个 ops 块** |
| **C. 不授权，改手动跑** | 每天开机后手动执行一行命令 | 失去无人值守，且靠人记得 |

我倾向 **B**：它不需要宽授权，而且顺手把已经坏了一周的其它 ops 任务一起救活。

## 8. 想请你查的具体问题

1. **macOS 15 (Sequoia) 上，`launchd` 的 LaunchAgent 访问 `~/Documents` 到底需要什么？**
   是必须给它的可执行文件（`/bin/bash`、`/usr/bin/python3` …）授"完全磁盘访问权限"，
   还是"文件和文件夹 → 文稿文件夹"那一项就够？有没有**更窄**的授权方式（比如给某个
   专门的 wrapper 二进制授权，而不是给整个 `bash`）？

2. **§6 那个"能写不能读"是不是正常的？** 如果是，说明什么？

3. **方案 B（仓库移出 `~/Documents`）有没有坑？** 家目录根 `~/quant` 是否确实不在 TCC
   保护范围内？会不会有别的地方（比如 Git、iCloud 同步、备份）受影响？

4. **有没有第四条路我没想到？** 例如让 LaunchAgent 通过某个已被授权的进程间接执行、
   或者把任务注册成 `LaunchDaemon`（系统级）而不是 `LaunchAgent`。

## 9. 现状（在问题解决前）

- **LaunchAgent 已安装并加载**（`com.quant.shadow-daily`，北京周二~周六 08:50）。它会按时
  触发，但每次都因 TCC 失败，错误写在 `~/Library/Logs/quant/shadow_daily.err.log`。
  **它不会"静默什么都不做"——会留下错误**。
- cron 里重复的那条**已撤掉**，避免两个调度器打架。
- 在 TCC 解决之前，**每天的手动跑法**（一行）：

  ```bash
  bash ~/Documents/quant/ops/shadow_daily.sh
  ```

  有效实验是 `M1-FORWARD-S-20260917`；没候选时它会如实打印 `no_opportunities: true`
  然后正常退出。
