# SIMULATE 连续运行一周验证清单

目标：在切 REAL 前，让港股 SIMULATE + 美股 DRY-RUN 真实连续运行至少 5 个交易日，
并完成每日对账，暴露只在长时间运行中出现的时序/状态问题。

## 前置
- [ ] Futu OpenD 已启动并解锁 SIMULATE
- [ ] 港股 config `trading.env: SIMULATE`；美股以 `--dry-run` 启动
- [ ] `make test-quant` 全绿（50 passed / 7 skipped）
- [ ] 已跑一次 `python3 scripts/live_trading/daily_reconcile.py` 验证脚本可用

## 每日（开市日，收盘后）
- [ ] 运行 `daily_reconcile.py`，差异必须为 0
- [ ] 打开确认页（港 8899 / 美 8899）：检查今天提案是否出现、能否正常点单/拒绝
- [ ] 检查日志：无 `RuntimeError`/`dictionary changed size`/`TimeoutError` 刷屏
- [ ] 对账本抽样：proposal_created / human_decision / execution_status /
      position_opened / position_closed 数量与操作一致
- [ ] 检查 `market_brief`：`run_market_brief.py` 今天生成成功（latest.json date=今天）
- [ ] 持仓页现价/盈亏/止盈止损线是否在更新（每 30 秒应变化）

## 专项（一周内至少各做一次）
- [ ] 周末演示：买入 → 出场触发 → 卖出确认（点「卖出」与「继续持有」各一次）→ 超时兜底
- [ ] 人工减仓/加仓后运行对账（验证 broker sync 余量逻辑）
- [ ] 手动改坏一份 data/*.yaml 再重启（验证 YAML 防覆盖：应报错而非清空）
- [ ] 把 OpenD 停 1 分钟再启动（验证自动重连与缓存不瘫痪）
- [ ] 点「加入观察池」热加入一只股票，确认下一轮选股包含它（验证 hot-add）

## 通过标准
- 连续 5 个交易日：对账 0 差异、无未处理异常、日志无资金/持仓异常
- 周末演示全流程可用
- 上述专项全部无异常

## 若发现差异
- 记录复现步骤与日志段
- 先在本线程修 + `make test-quant` 回归，再继续跑
- 不带着未解释的差异进入 REAL
