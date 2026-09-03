#!/bin/bash

# 单元测试运行脚本

echo "======================================"
echo "运行单元测试"
echo "======================================"
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 项目根目录
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 切换到项目根目录
cd "$PROJECT_ROOT"

# 检查Python环境
echo "检查Python环境..."
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}错误: 未找到python3${NC}"
    exit 1
fi

echo "Python版本:"
python3 --version
echo ""

# 检查pytest是否安装
echo "检查pytest..."
if ! python3 -c "import pytest" 2>/dev/null; then
    echo -e "${YELLOW}pytest未安装，正在安装...${NC}"
    pip3 install pytest pytest-cov
fi

echo "pytest版本:"
python3 -m pytest --version
echo ""

# 创建必要的目录
echo "创建必要的目录..."
mkdir -p logs
mkdir -p htmlcov
mkdir -p .pytest_cache
echo ""

# 运行测试
echo "======================================"
echo "开始运行测试"
echo "======================================"
echo ""

# 根据参数选择测试类型
if [ "$1" = "fast" ]; then
    echo -e "${YELLOW}运行快速测试（排除慢速测试）...${NC}"
    python3 -m pytest -v -m "not slow" --cov=. --cov-report=term-missing --cov-report=html:htmlcov
elif [ "$1" = "unit" ]; then
    echo -e "${YELLOW}运行单元测试...${NC}"
    python3 -m pytest -v -m unit --cov=. --cov-report=term-missing --cov-report=html:htmlcov tests/unit/
elif [ "$1" = "integration" ]; then
    echo -e "${YELLOW}运行集成测试...${NC}"
    python3 -m pytest -v -m integration --cov=. --cov-report=term-missing --cov-report=html:htmlcov tests/integration/
elif [ "$1" = "coverage" ]; then
    echo -e "${YELLOW}运行测试并生成详细覆盖率报告...${NC}"
    python3 -m pytest -v --cov=. --cov-report=term-missing --cov-report=html:htmlcov --cov-fail-under=80
elif [ "$1" = "report" ]; then
    echo -e "${YELLOW}生成测试报告...${NC}"
    mkdir -p reports
    python3 -m pytest -v --html=reports/report.html --self-contained-html --cov=. --cov-report=html:htmlcov
    echo ""
    echo -e "${GREEN}报告已生成:${NC}"
    echo "  - HTML报告: reports/report.html"
    echo "  - 覆盖率报告: htmlcov/index.html"
    open reports/report.html 2>/dev/null || true
else
    echo -e "${YELLOW}运行所有测试...${NC}"
    python3 -m pytest -v --cov=. --cov-report=term-missing --cov-report=html:htmlcov
fi

# 检查测试结果
TEST_RESULT=$?

echo ""
echo "======================================"
if [ $TEST_RESULT -eq 0 ]; then
    echo -e "${GREEN}✓ 所有测试通过！${NC}"
    echo ""
    echo "覆盖率报告: htmlcov/index.html"
    echo "查看报告: open htmlcov/index.html"
else
    echo -e "${RED}✗ 测试失败${NC}"
    echo ""
    echo "请检查上面的错误信息"
fi
echo "======================================"

# 显示测试统计
if [ -f .pytest_cache/v/cache/lastfailed ]; then
    echo ""
    echo -e "${YELLOW}最近失败的测试:${NC}"
    python3 -m pytest --cache-show lastfailed
fi

# 退出
exit $TEST_RESULT
