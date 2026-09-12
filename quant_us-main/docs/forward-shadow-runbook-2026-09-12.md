# 前向 shadow 运行手册（2026-09-12）

目的：验证 **LLM 是否带来真实选股增量**（技术设计 §3.5/§4.3）。这是**唯一不依赖退市数据**的路径——从样本冻结日之后逐日向前跑，因此**需要真实交易日累积**，不是一次能跑完的任务。

## 0. 为什么必须在"本机"跑

决策账本 `data/execution.sqlite3` 是 **SQLite 单写者**文件，且由你本机的实时系统共用。在沙箱/第二个进程里并发打开会出现 `unable to open database file` / `disk I/O error`（已实测）。**因此本手册的所有命令都在拥有该账本的机器上执行**，不要从别处并发跑。

安全边界：只允许 shadow，**不下单**。严禁 `run_all.py --real`。

## 1. 前置

- 本机 Futu OpenD 已启动并登录（`127.0.0.1:11111`，仅需行情权限）。
- `config.yaml`：`buy_strategy_v2.mode: shadow`（必须）；`shadow_only: true`。
- 样本候选名单已冻结（2026-09-12，39 只待资产类型核验的存续证券）：`data/security_master_39.csv` 与 `docs/sample-frame-registration-2026-09-12.md`。
- 若要让 shadow 覆盖这批样本，先核对并登记实际观察列表：选股使用 `dip_buy.watch_list ∪ trend_breakout.watch_list`，setup 优先使用 `buy_strategy_v2.watch_list`。只改后一项不会扩大 LLM 选股池。沙箱内生成的模板见 `data/shadow/config_shadow.yaml`；其中 `futu.host` 是当时联调地址，本机请按实际 OpenD 地址配置。

## 2. 每个美股交易日收盘后运行

```bash
# 1) LLM 研究批次：只读，不产生 proposal/approval/order，保存版本化研究批次
python3 scripts/live_trading/run_daily_selection.py

# 2) 中期买入 setup shadow：不创建 proposal、不调用 LLM、不下单
python3 scripts/live_trading/run_daily_setups.py --json

# 3) Selection shadow 对账：只读账本与研究批次，不调用模型/执行器
python3 scripts/live_trading/reconcile_selection_decision.py --json
```

（如使用完整服务：`python3 run_all.py --dry-run` 启动 Web 与调度；**不要**加 `--real`。）

## 3. 每批核对（对应测试手册 §14）

- [ ] selection 模式仍为 `shadow`；
- [ ] 每个 selection decision 有输入快照、模型、prompt 版本和输出；
- [ ] setup 引用正确的 selection decision id；
- [ ] LLM 接受 / 观察 / 拒绝 / 资料不足都能进入账本；
- [ ] 同一股票同一交易日不重复领取 setup（账本幂等保护会拒绝重复快照）；
- [ ] `falling` 周线状态不能进入可执行集合；
- [ ] T 日 setup 最早 T+1 执行；
- [ ] 过度高开、跌破初始止损会被拒绝；
- [ ] shadow **不生成券商订单**；
- [ ] outcome 按 1/3/5/10/20/40 日补算；
- [ ] 页面数量能与决策账本 funnel 对账；确认台为空时先查 funnel 各层数量，**不要降低阈值**。

对账 funnel：

```text
原始股票池 → 数据质量通过 → LLM selection 接受 → weekly gate → daily setup
→ daily confirmed → gap/stop 可成交 → confirmation desk
```

## 4. 时长与判定

- **管道联调最低值**：至少 **2 个独立美股交易日**、累计 **3 个有效研究批次**。这只是联调底线，**不足以判断收益优势**。
- 收益评估应按**实际独立决策数与持仓周期**等待足够样本；报告需并列展示历史探索与前向效果，**不可合成单一显著结论**。

## 5. 需预先登记的研究级门槛（§3.6，禁止事后按数据设阈值）

严格层时间字段完整率、可追溯原文比例、ticker 映射成功率、packet 构建成功率、LLM 结构化输出成功率、有效引用率、缺失率、股票/年份覆盖率。达不到就保持 **D=`inconclusive`**，继续 ABC。

## 6. 当前已知局限（写进报告）

- 样本为**当前存续**（幸存者偏差），前向 shadow 不消除它，只消除"回测期已看过收益"的问题；
- 富途财报**无 `observed_at`**（诊断层实测：严格层 0 条可用），历史 D 只能作"受限证据的回放探索"；
- 2026 年模型参数可能记得未来结果——**历史 D 的模型后见知识无法排除**；前向 shadow 是唯一能真正归因 LLM 增量的路径。
