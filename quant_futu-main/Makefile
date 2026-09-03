# Quant Futu - 港股量化交易系统 Makefile
# 使用方法: make <command>

.PHONY: help install momentum-analyze momentum-top momentum-search \
	hk-backtest hk-live-start hk-live-stop hk-live-status hk-live-log hk-live-view \
	us-backtest us-backtest-debug us-live-start us-live-stop us-live-status us-live-log us-live-view \
	us-sync-ak us-sync-incremental \
	a-backtest a-backtest-300 a-backtest-custom a-backtest-range \
	test test-v test-coverage test-report \
	clean clean-log clean-all clear-selection

# 默认目标
help:
	@echo "Quant Futu - 港股量化交易系统"
	@echo ""
	@echo "使用方法: make <command>"
	@echo ""
	@echo "=== 安装 ==="
	@echo "  install              安装依赖"
	@echo ""
	@echo "=== 🇭🇰 港股回测 ==="
	@echo "  hk-backtest          运行港股回测 (动量策略)"
	@echo "  hk-backtest-analyze  分析回测结果 (详细中文报告)"
	@echo ""
	@echo "=== 🇭🇰 港股实盘 ==="
	@echo "  hk-live-start        启动港股实盘（后台）"
	@echo "  hk-live-stop         停止港股实盘"
	@echo "  hk-live-status       查看港股实盘状态"
	@echo "  hk-live-log          查看港股实盘日志"
	@echo "  hk-live-view         查看港股选股结果"
	@echo ""
	@echo "=== 🇨🇳 A股回测 ==="
	@echo "  a-backtest         运行A股回测（中证1000）"
	@echo "  a-backtest-300     运行A股回测（沪深300）"
	@echo "  a-backtest-custom  运行A股回测（自定义股票）"
	@echo "  a-backtest-range   运行A股回测（交互指定日期）"
	@echo ""
	@echo "  python3 scripts/backtest/run_a_share_backtest.py --source csi1000 --start 2024-01-01 --end 2024-12-31"
	@echo ""
	@echo "=== 📊 动量分析 ==="
	@echo "  momentum-analyze    动量分析（默认老铺黄金）"
	@echo "  momentum-top        分析近期涨幅最大的20只股票"
	@echo "  make momentum-search KEY=黄金   按名称搜索并分析"
	@echo ""
	@echo "  python3 scripts/analysis/momentum_analysis.py --codes HK.06181"
	@echo "  python3 scripts/analysis/momentum_analysis.py --top-gainers 20"
	@echo "  python3 scripts/analysis/momentum_analysis.py --search 黄金 --plot"
	@echo ""
	@echo "=== 测试 ==="
	@echo "  test                 运行所有单元测试"
	@echo "  test-v               运行单元测试（详细输出）"
	@echo "  test-coverage        运行测试并生成覆盖率报告"
	@echo "  test-report          生成HTML测试报告"
	@echo ""
	@echo "=== 清理 ==="
	@echo "  clean                清理缓存和临时文件"
	@echo "  clean-log            清理日志文件"
	@echo "  clean-all            清理所有生成文件"
	@echo ""
	@echo "=== 选股结果管理 ==="
	@echo "  clear-selection      清空过时的选股结果"

# ==================== 安装 ====================
install:
	pip3 install -r requirements.txt

# ==================== 🇭🇰 港股回测 ====================
hk-backtest:
	@echo "🇭🇰 运行港股回测..."
	python3 scripts/backtest/run_hk_backtest.py backtest momentum

hk-backtest-analyze:
	@echo "🇭🇰 运行港股回测..."
	python3 scripts/backtest/analyze_backtest.py
# ==================== 📈 港股实盘交易 ====================
# ==================== 🇭🇰 港股实盘交易 ====================
hk-live-start:
	@mkdir -p logs; \
	echo "🇭🇰 启动港股实盘交易（后台运行）..."; \
	nohup python3 scripts/live_trading/run_hk_live.py start > logs/hk_live_output.log 2>&1 & \
	PID=$$!; \
	echo $$PID > hk_live.pid; \
	echo "进程ID: $$PID"; \
	sleep 2; \
	tail -10 logs/hk_live_trading.log 2>/dev/null || echo "等待日志生成..."

hk-live-stop:
	@echo "🇭🇰 停止港股实盘交易..."
	python3 scripts/live_trading/run_hk_live.py stop 2>/dev/null || true
	@if [ -f hk_live.pid ]; then \
		kill $$(cat hk_live.pid) 2>/dev/null && echo "已停止进程 $$(cat hk_live.pid)" || echo "进程已停止"; \
		rm -f hk_live.pid; \
	fi

hk-live-status:
	@echo "=== 🇭🇰 港股实盘状态 ==="
	@python3 scripts/live_trading/run_hk_live.py status 2>/dev/null || echo "无法获取状态"
	@echo ""
	@echo "=== 进程信息 ==="
	@ps aux | grep -E "run_hk_live|hk_live_manager" | grep -v grep || echo "没有运行中的进程"
	@echo ""
	@python3 scripts/live_trading/view_selection.py 2>/dev/null || echo "无法获取选股结果"

hk-live-log:
	@echo "🇭🇰 港股实时日志（Ctrl+C退出）..."
	tail -f logs/hk_live_output.log

hk-live-view:
	@echo "🇭🇰 查看港股选股结果..."
	@python3 scripts/live_trading/view_selection.py

hk-live-kill:
	@echo "🇭🇰 强制停止港股实盘进程..."
	@pkill -9 -f "run_hk_live.py" && echo "已强制停止" || echo "没有找到运行中的进程"
	@rm -f hk_live.pid .comate/hk_live_trading.pid .comate/hk_live_trading.info
# ==================== 选股结果管理 ====================
clear-selection:
	@echo "清空选股结果..."
	@python3 scripts/live_trading/clear_selection_results.py

# ==================== 📊 动量分析 ====================
momentum-analyze:
	@echo "📊 动量分析工具 - 用法示例："
	@echo ""
	@echo "  # 分析单只股票"
	@echo "  python3 scripts/analysis/momentum_analysis.py --codes HK.06181"
	@echo ""
	@echo "  # 分析多只股票"
	@echo "  python3 scripts/analysis/momentum_analysis.py --codes HK.06181 HK.02513 --days 180"
	@echo ""
	@echo "  # 搜索股票"
	@echo "  python3 scripts/analysis/momentum_analysis.py --search 黄金"
	@echo ""
	@echo "  # 查看近期涨幅最大的N只"
	@echo "  python3 scripts/analysis/momentum_analysis.py --top-gainers 20 --days 180"
	@echo ""
	@echo "  # 生成图表"
	@echo "  python3 scripts/analysis/momentum_analysis.py --codes HK.06181 --plot"
	@echo ""
	@python3 scripts/analysis/momentum_analysis.py --codes HK.06181 --days 90

momentum-top:
	@python3 scripts/analysis/momentum_analysis.py --top-gainers 20 --days 100

momentum-search:
	@python3 scripts/analysis/momentum_analysis.py --search $(or $(word 2,$(MAKECMDGOALS)),智能)

# ==================== 测试 ====================
test:
	@echo "🧪 运行所有单元测试..."
	python3 -m pytest -v --cov=. --cov-report=term-missing --cov-report=html:htmlcov

test-v:
	@echo "🧪 运行单元测试（详细输出）..."
	python3 -m pytest -vv --cov=. --cov-report=term-missing --cov-report=html:htmlcov

test-coverage:
	@echo "🧪 运行测试并生成覆盖率报告..."
	python3 -m pytest -v --cov=. --cov-report=term-missing --cov-report=html:htmlcov --cov-fail-under=80

test-report:
	@echo "🧪 生成HTML测试报告..."
	@mkdir -p reports
	python3 -m pytest -v --html=reports/report.html --self-contained-html --cov=. --cov-report=html:htmlcov
	@echo ""
	@echo "✅ 报告已生成:"
	@echo "   - HTML报告: reports/report.html"
	@echo "   - 覆盖率报告: htmlcov/index.html"
	@open reports/report.html 2>/dev/null || true

# 实盘交易逻辑测试（使用真实模拟仓）
test-live-select:
	@echo "测试选股逻辑..."
	@python3 scripts/live_trading/test_selection_direct.py


clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache 2>/dev/null || true
	@echo "已清理缓存文件"

clean-log:
	rm -f logs/*.log logs/*.log.* 2>/dev/null || true
	@echo "已清理日志文件"

clean-all: clean clean-log
	rm -rf output/* cache/* data/*.json 2>/dev/null || true
	rm -f live.pid .comate/live_trading.pid .comate/live_trading.info 2>/dev/null || true
	@echo "已清理所有生成文件"

# === 次新股动量扫描 ===
new-ipo-scan:
	@echo "执行次新股动量扫描..."
	PYTHONPATH=/Users/liwentao/PycharmProjects/quant_futu python3 scripts/daily/new_ipo_momentum_scan.py

# === 日内K线分析验证 ===
# 用法: make verify-intraday CODE=HK.01877 DATE=2026-04-20
verify-intraday:
	@echo "📊 运行日内K线分析验证..."
	STOCK_CODE=$(or $(CODE),SZ.300750) \
	TRADE_DATE=$(or $(DATE),2026-05-06) \
	python3 scripts/verify_intraday_analyzer.py
