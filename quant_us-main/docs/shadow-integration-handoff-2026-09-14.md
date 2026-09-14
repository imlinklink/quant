# Shadow／DRY-RUN 联调启动记录

2026-09-14 12:09 UTC核对：服务PID 26056，命令 `python3 run_all.py --dry-run`，
Web `http://127.0.0.1:8890` 健康接口返回200。保持现有9只在线观察池，未改成15只历史实验池。
本轮为工程联调，不属于冻结的15只raw-asof前向批次，P2没有上线。

## 已完成

- config.yaml启用shadow_integration；统一入口拒绝在此开关启用时使用券商执行模式。
- setup回看窗口锚定最近已完成交易日，固定500个自然日；输入协议登记为
  completed-session-500d-v1，保留原有浮动窗口快照。
- 实际首次扫描和重复扫描返回完全一致：8/9质量通过，RAM仅56根日线，要求250根，
  明确拒绝；候选0条。命令退出码1表示存在质量失败，不表示批次崩溃。
- 已有Selection批次对账通过：9只覆盖、一次完成的模型调用、快照完整、回放一致、订单副作用0。
- 收盘后Selection及对账接入统一调度；Selection成功后才允许setup执行；outcome独立结算。
- 日任务开始/完成/失败写入决策事件。Selection失败不自动重调模型；setup/outcome明确失败
  最多3次、间隔至少5分钟。进程中断后遗留running不自动抢跑，需人工核查。
- 常规节假日和周末不启动联调日任务；临时休市仍需补充官方日历核验。
- 最终全量回归：667 passed、18 warnings；git diff --check通过。

## 自动运行时间

美东交易日16:20开始Selection→对账；16:30之后且Selection对账成功才执行setup；
outcome按现有配置时间执行。当前夏令时对应上海次日04:20、04:30，冬令时顺延一小时。
Selection执行超过16:30时，setup等待其结束。当前实现只接入收盘批次，没有盘前Selection。
服务需保持运行、电脑不休眠、OpenD在线；未安装开机自启或系统级守护。

## 尚未验证

截至核对时尚未到9月14日美东收盘，shadow_job事件0条，不能声称自动收盘批次已经通过。
需观察首个真实收盘批次的任务状态、Selection对账、setup分母及outcome退出码。
T+1实际跨日处理和有持仓退出仍需后续交易日验证；没有候选时不人为制造信号。
RAM质量失败会使setup批次有限重试后保持失败，不降低250日门槛。
服务使用子进程执行setup，因此后续输入版本修复会在调用时加载；调度主体已重启加载。

## 证据与操作

产物目录 `data/runtime_audit/SHADOW-INTEGRATION-20260914-001/`：
final-check.json（最终代码/配置哈希、健康与订单事件检查）、pytest-final.log、
setups-versioned.log、setups-repeat.log、reconcile.json、service.log。
manifest.json保存启动时哈希，最终修复后哈希以final-check.json为准，两者均保留。
配置可能含密钥，不将完整配置复制进交接文档；当前代码仍未提交。

停止时先核对PID 26056仍对应本服务，再执行 `kill -TERM 26056`；优雅退出约需20秒。
下一动作：首个收盘后检查service.log及shadow_job_started/shadow_job_finished事件，
核对任务成功/失败，失败时按证据修复，不重复调用结果不明的模型任务。
