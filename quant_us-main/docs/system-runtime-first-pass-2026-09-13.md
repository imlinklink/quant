# 系统首次实跑与P1收尾（2026-09-13）

本轮依据 `medium-term-p1-review-handoff-2026-09-13.md` 收尾，转入在线运行诊断。
P1-007/008 的输入与中期代码 SHA-256 全部一致，旧产物没有覆盖。QQQ同口径基准与P2仍未放行；
历史回测参数未上线。当前工作区未提交，不属于前向15只实验冻结环境。

## 服务

2026-09-13 22:44（上海时间）启动 `python3 run_all.py --dry-run`，PID 67660，
后台进程持续运行，页面 `http://127.0.0.1:8890`。这次不是系统级守护服务，重启电脑不会自动恢复。
如需停止，先确认PID仍对应此命令，再用 `kill -TERM 67660`。
OpenD监听127.0.0.1:11111；Web、吊灯退出、9只突破监控、setup/outcome调度已启动。
周日突破扫描按规则跳过；当前无持仓。首页、确认台、建议页和两个健康API均HTTP 200。

实际配置为 Selection shadow、buy_strategy_v2 shadow、DRY-RUN账本，券商配置SIMULATE，
启动参数保证本次不提交券商订单。检查decision_events中order_intent_created/order_submitted/fill_received均为0。
当前观察池9只：SNDK、MU、SOXL、YINN、LITE、AXTI、RAM、MULL、RKLB；并非15只历史样本，未自动扩池。

## 已修复

setup遇到不可变快照冲突原来整批崩溃，现逐股票输出IMMUTABLE_SNAPSHOT_CONFLICT并继续扫描，
进程仍返回非零，不能把失败标绿。缺行情的股票保留在扫描分母，空批次不能退出码0。
同一状态只改变as_of时复用首次快照，不覆盖原文；特征、状态或输入改变仍拒绝。
全量测试662 passed、18 warnings；最后as_of幂等补丁再跑相关7项测试通过。git diff --check通过。

## 实跑暴露的薄弱点

1. **输入重现性**：9只重扫仍有拒绝，差异审计显示8只同一2026-09-11 session的bar_count/features改变，
   AXTI还改变previous_state；RAM没有有效session。不能删除旧快照再制造成功。下一步应固定
   交易日与回看起点、保存输入哈希，明确已冻结session重放和新版本重算的区别。
2. **每日研究调度未接通**：OutcomeSchedulerThread只调setup/outcome，Selection研究当前仍需手动运行。
   ReviewScheduler有selection_slot方法并不意味着服务调用了它。
3. **失败恢复弱**：daily job先claim、后执行，非零返回只有日志；没有成功/失败状态和受控重试。
   当前日历只排周末，节假日也须进一步核对。
4. **证据采集延迟**：本轮Selection串行采集9只股票事件与期权，约两分钟；期权9/9成功。
   不能把启动日志“LLM已启用”当作模型调用成功，须看该批对账结果。
5. **观测尚不足**：启动时健康API只有2个独立决策样本，状态insufficient_sample；
   休市、无持仓条件下未实测盘中报价变化、持仓退出和T+1链路。

## 审计产物与下一步

日志、HTTP快照、P1哈希复核、特征差异字段、测试输出均在
`data/runtime_audit/STARTUP-20260913-001/`。文件可能包含研究输出，不应公开发布整个目录。

优先修固定session重放与任务成功/失败状态，再把Selection→setup→outcome按时间顺序接到统一调度；
之后用真实交易日累计完整批次，核对数据覆盖、研究成功率、候选拒绝原因、T+1与到期结果。
当前运行验证是在线9只池工程联调，不计入冻结的15只原始价前向实验。
