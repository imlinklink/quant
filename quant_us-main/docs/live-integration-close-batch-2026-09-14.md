# 收盘批次联调复盘（首个批次 · 2026-09-14 收盘 → 9-15 凌晨）

> 基线：服务 `run_all.py --dry-run`（PID 26056），代码 `b548b09`，session=纽约日期 2026-09-14。
> 结论：**首个批次未跑通**。selection 因 LLM 超时失败 → setup 被闸门跳过 → outcome 两次重试失败（第三次待触发）。

## 一、三连 job 时间线（账本 shadow_job 事件，UTC）

| 时间(UTC) | 事件 | 结果 |
|---|---|---|
| 20:20:21 | selection_and_reconcile started (attempt 1) | — |
| 20:22:32 | decision_requested + model_attempt_started | — |
| 20:23:36 | model_attempt_failed | **LLM 失败** |
| 20:23:36 | selection_and_reconcile finished | **failed, exit=1** |
| — | daily_setup_shadow | **跳过**（selection 未 succeeded）|
| 21:30:07 | selection_outcomes started (attempt 1) | — |
| 21:33:03 | selection_outcomes finished (attempt 1) | **failed, exit=1** |
| 21:38:06 | selection_outcomes started (attempt 2) | — |
| 21:41:01 | selection_outcomes finished (attempt 2) | **failed, exit=1** |
| 21:46:04 | selection_outcomes started (attempt 3) | — |
| 21:48:59 | selection_outcomes finished (attempt 3) | **failed, exit=1**（max_attempts 耗尽）|

## 二、selection 失败（根因已定位）

- 链路：scan → decision_requested → model_attempt_started → **model_attempt_failed**。
- 模型 `deepseek-chat`（`api.deepseek.com`），耗时 **63554ms≈63.5s**，`raw_response`/`validation_errors` 均为空 → **HTTP/超时层失败**（非回复校验失败）。
- 配置 `timeout:30s` + `max_retries:2` → 63.5s ≈ 两次 30s 超时后放弃。
- `run_daily_selection.py` 退出码 1 → `_selection` 立即返回 1 → **reconcile_selection_decision.py 未执行**。
- 背景：`llm_decision_runs` 里 9-10 的 `decision_7318705b` 也 failed、9-13 的 validated → **DeepSeek 端点近期间歇性超时**。

## 三、outcome 失败（三次，根因待定位）

- `run_outcomes.py` 里唯一的显式 `return 1` 是 **`fetcher.connect()` 失败 → 「无法连接富途 OpenD」**（line 136-138）；亦可能为 `fetch_multiple_stocks`/`run()` 未捕获异常（traceback → exit 1）。
- 但 **OpenD 在线**（PID 29377，端口 11111），服务本身已有 2 条到 11111 的 ESTABLISHED 连接 → 排除「OpenD 宕机」。
- 三次 attempt 均未产出任何 `outcome_observed` 事件（`decision_outcomes_v2` 仍 925 条）→ 失败发生在取数/连接阶段，未进入结算。
- **子进程 stdout/stderr 未捕获**（`subprocess.run` 无 capture，落终端，已不可见）→ 精确异常需手动重跑才能看到。

## 四、结论与影响

1. **今晚未产生任何选股结果、对账或订单副作用**（DRY-RUN 本就不该有订单，此点符合预期）。
2. 失败是**外部依赖脆弱性**，不是业务逻辑错误：
   - selection：DeepSeek LLM 超时；
   - outcome：OpenD 子进程级连接或取数异常（具体未捕获）。
3. 执行尺子首测**未通过**，但暴露了两处真实的联调缺口（LLM 端点稳定性、OpenD 并发/子进程连接）。

## 五、下一步（按红线，不自动重跑）

1. **outcome**：attempt 3 也 failed（21:46:04→21:48:59，exit=1），`max_attempts=3` 已耗尽，outcome 最终失败，今日结算产出 0。
2. **selection**：查 DeepSeek（API key/额度/网络到 api.deepseek.com 连通性）；证据明确后**手动补跑一次 `reconcile_selection_decision.py`（非模型）**。
3. **outcome**：手动跑 `run_outcomes.py --config config.yaml` 一次，看 stdout/traceback 定位真实异常（这是诊断，非重调模型）。
4. ~~给 `_selection`/`_run_setups`/`_run` 的 `subprocess.run` 加 `capture_output`~~ **已修**：新增 `_run_subprocess`（捕获 stdout/stderr 落日志、超时返回 -1），三处 runner 已复用；测试 `test_outcome_scheduler.py` 3 条，全量 680 passed。**需重启服务后才生效**（PID 26056 仍跑旧代码，明早下一个批次前重启即可）。

## 六、证据产物

- 账本：`data/execution.sqlite3`（`decision_events` / `llm_decision_runs` / `llm_model_attempts`）。
- 日志：`logs/quant_us_2026-09-14.log`（主循环正常，子进程输出不在其中）。
