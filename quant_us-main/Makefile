# ============================================================
# Makefile - quant_us_intraday 美股双吊灯策略
# ============================================================
# 快速开始: make help

SHELL := /bin/bash
PROJECT_DIR := $(shell pwd)
VENV_PYTHON := $(PROJECT_DIR)/.venv/bin/python3
SYSTEM_PYTHON := /usr/bin/python3
PYTHON := $(shell [ -f "$(VENV_PYTHON)" ] && echo "$(VENV_PYTHON)" || echo "$(SYSTEM_PYTHON)")

.DEFAULT_GOAL := help

# ------------------------------------------------------------------
# 帮助信息
# ------------------------------------------------------------------
.PHONY: help
help:
	@echo ""
	@echo "  📊 quant_us_intraday - 美股双吊灯策略"
	@echo "  ================================================"
	@echo ""
	@echo "  📦 安装"
	@echo "    make install          安装依赖（自动创建 .venv）"
	@echo ""
	@echo "  🔬 回测 & 校验"
	@echo "    make backtest        双吊灯回测（需指定股票和日期）"
	@echo "    make backtest-sndk   快速回测 SNDK 1小时线"
	@echo "    make backtest-5min   快速回测 SNDK 5分钟线"
	@echo "    make verify-intraday 日内K线分析校验（抄底逻辑）"
	@echo "    make check           语法检查"
	@echo ""
	@echo "  🚀 实盘"
	@echo "    make web           启动Web服务（调参用）"
	@echo "    make start         启动完整系统（Web+监控，模拟）"
	@echo "    make start-real    同上，实盘"
	@echo "    make stop          停止后台服务"
	@echo "    make live          [已废弃，用 make start]"
	@echo "    make live-dry      [已废弃，用 make start]"
	@echo ""
	@echo "  🧹 清理"
	@echo "    make clean           清理临时文件"
	@echo ""
	@echo "  📖 示例"
	@echo "    make backtest STOCK=US.AAPL DATE=2026-03-01"
	@echo "    make backtest STOCK=US.TSLA DATE=2026-04-01 direction=short"
	@echo "    python scripts/run_chandelier_backtest.py US.SNDK 2026-04-07 --price 714"
	@echo ""
	@echo "  ⚙️  配置"
	@echo "    K线粒度: config.yaml → backtest_chandelier.kline_type"
	@echo "    交易环境: config.yaml → live_manager.trd_env (SIMULATE/REAL)"
	@echo ""

# ------------------------------------------------------------------
# 安装依赖
# ------------------------------------------------------------------
.PHONY: install
install:
	@echo "📦 安装 Python 依赖..."
	@if [ ! -d "$(PROJECT_DIR)/.venv" ]; then \
		echo "  创建虚拟环境..."; \
		$(SYSTEM_PYTHON) -m venv .venv; \
	fi
	@echo "  安装 futu-api, pyyaml, matplotlib..."
	@$(VENV_PYTHON) -m pip install --quiet futu-api pyyaml matplotlib pillow
	@echo "✅ 依赖安装完成"

# ------------------------------------------------------------------
# 语法检查
# ------------------------------------------------------------------
.PHONY: check
check:
	@echo "🔍 语法检查..."
	@$(PYTHON) -m py_compile mutifactor/strategies/dual_chandelier.py && echo "  ✅ dual_chandelier.py"
	@$(PYTHON) -m py_compile mutifactor/strategies/chandelier_backtest.py && echo "  ✅ chandelier_backtest.py"
	@$(PYTHON) -m py_compile mutifactor/trading/futu_connector.py && echo "  ✅ futu_connector.py"
	@$(PYTHON) -m py_compile scripts/live_trading/chandelier_exit_manager.py && echo "  ✅ chandelier_exit_manager.py"
	@$(PYTHON) -m py_compile scripts/live_trading/run_manager.py && echo "  ✅ run_manager.py"
	@$(PYTHON) -m py_compile scripts/verify_intraday.py && echo "  ✅ verify_intraday.py"
	@echo "✅ 语法检查完成"

# ------------------------------------------------------------------
# 双吊灯回测（通用）
# 用法: make backtest STOCK=US.AAPL DATE=2026-03-01 [OPTIONS...]
# 示例:
#   make backtest STOCK=US.SNDK DATE=2026-04-07
#   make backtest STOCK=US.TSLA DATE=2026-04-01 direction=short
# ------------------------------------------------------------------
.PHONY: backtest
backtest: _check_stock _check_python
	@echo "🔬 双吊灯回测: $(STOCK) $(DATE)"
	@echo "  ⚠️  确保 config.yaml 中 backtest_chandelier.kline_type 已设置正确"
	@$(PYTHON) scripts/run_chandelier_backtest.py "$(STOCK)" "$(DATE)" \
		$(foreach opt,$(BACKTEST_OPTS),$(opt))

# 默认参数
STOCK ?= US.SNDK
DATE  ?= 2026-04-07

# 额外的 backtest 选项（用 BACKTEST_OPTS=--no-plot 传递）
BACKTEST_OPTS ?=

# ------------------------------------------------------------------
# SNDK 快速回测
# ------------------------------------------------------------------
.PHONY: backtest-sndk
backtest-sndk:
	@echo "🔬 回测 SNDK 1小时线..."
	@$(PYTHON) scripts/run_chandelier_backtest.py US.SNDK 2026-04-07 --price 714

.PHONY: backtest-sndk-5min
backtest-sndk-5min:
	@echo "🔬 回测 SNDK 5分钟线..."
	@$(PYTHON) scripts/run_chandelier_backtest.py US.SNDK 2026-04-07 --price 714

# ------------------------------------------------------------------
# 实盘管理器
# ------------------------------------------------------------------
# ------------------------------------------------------------------
# 统一入口（Web + 监控，一个进程）
# ------------------------------------------------------------------
.PHONY: web
web: _check_python
	@echo "🌐 启动Web服务（调参用）..."
	@$(PYTHON) run_all.py --web-only

.PHONY: start
start: _check_python
	@echo "🚀 启动完整系统（Web + 抄底监控 + 止盈止损，模拟）..."
	@$(PYTHON) run_all.py --dry-run

.PHONY: start-real
start-real: _check_python
	@echo "🚀 启动完整系统（Web + 抄底监控 + 止盈止损，实盘）..."
	@$(PYTHON) run_all.py --real

.PHONY: stop
stop:
	@echo "🛑 停止后台服务..."
	@kill $(shell pgrep -f "run_all.py" 2>/dev/null) 2>/dev/null; true
	@kill $(shell pgrep -f "web/app.py" 2>/dev/null) 2>/dev/null; true
	@echo "✅ 已停止"

# ------------------------------------------------------------------
# 以下为旧命令（保留兼容）
# ------------------------------------------------------------------
.PHONY: live
live: start

.PHONY: live-dry
live-dry: start

.PHONY: live-dry-old
live-dry-old: _check_python
	@echo "🚀 启动实盘管理器（dry-run）..."
	@$(PYTHON) scripts/live_trading/run_manager.py --dry-run

# ------------------------------------------------------------------
# 日内K线分析校验（抄底逻辑）
# ------------------------------------------------------------------
.PHONY: verify-intraday
verify-intraday: _check_python
	@echo "📊 运行日内K线分析校验..."
	@$(PYTHON) scripts/verify_intraday.py $(STOCK)

# ------------------------------------------------------------------
# 切换交易环境
# ------------------------------------------------------------------
.PHONY: env-simulate
env-simulate:
	@echo "切换为 SIMULATE 模式..."
	@$(PYTHON) -c "import yaml; c=yaml.safe_load(open('config.yaml')); c['live_manager']['trd_env']='SIMULATE'; yaml.safe_dump(c, open('config.yaml','w'), allow_unicode=True)"
	@echo "✅ 已切换为 SIMULATE（模拟盘）"

.PHONY: env-real
env-real:
	@echo "切换为 REAL 模式..."
	@$(PYTHON) -c "import yaml; c=yaml.safe_load(open('config.yaml')); c['live_manager']['trd_env']='REAL'; yaml.safe_dump(c, open('config.yaml','w'), allow_unicode=True)"
	@echo "⚠️  已切换为 REAL（实盘）请确认！"

# ------------------------------------------------------------------
# 清理
# ------------------------------------------------------------------
.PHONY: clean
clean:
	@echo "🧹 清理临时文件..."
	@find . -name "*.pyc" -delete
	@find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	@find . -name ".DS_Store" -delete
	@rm -f /tmp/_tw_*.txt
	@echo "✅ 清理完成"

# ------------------------------------------------------------------
# 内部
# ------------------------------------------------------------------
.PHONY: _check_python _check_stock
_check_python:
	@if [ ! -f "$(VENV_PYTHON)" ] && ! command -v python3 &>/dev/null; then \
		echo "❌ 未找到 Python，请运行 make install"; exit 1; \
	fi

_check_stock:
	@if [ "$(STOCK)" = "" ] || [ "$(DATE)" = "" ]; then \
		echo "❌ 请指定 STOCK 和 DATE 参数"; exit 1; \
	fi
