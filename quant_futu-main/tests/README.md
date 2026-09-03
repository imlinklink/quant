# 单元测试指南

## 测试结构

```
tests/
├── conftest.py              # pytest配置和共享fixtures
├── pytest.ini              # pytest配置文件（在项目根目录）
├── unit/                   # 单元测试
│   ├── base/              # 基础类测试
│   │   └── test_base_strategy.py
│   ├── data/              # 数据模块测试
│   │   ├── test_hk_fetcher.py
│   │   ├── test_us_fetcher.py
│   │   └── test_db_kline_cache.py
│   ├── infra/             # 基础设施测试
│   │   └── test_yaml_storage.py
│   ├── live/              # 实盘交易测试
│   │   ├── test_position_manager.py
│   │   ├── test_state_persistence.py
│   │   ├── test_buy_timing.py
│   │   └── test_price_fetcher.py
│   ├── strategies/        # 策略测试
│   │   ├── test_momentum.py
│   │   └── test_exit_strategy.py
│   ├── trading/           # 交易接口测试
│   │   └── test_futu_trader.py
│   ├── backtest/          # 回测测试
│   │   └── test_backtest_engine.py
│   └── utils/             # 工具类测试
│       └── test_config_loader.py
└── integration/           # 集成测试
    └── (待补充)
```

## 运行测试

### 1. 运行所有测试

```bash
# 使用pytest
pytest

# 或使用Makefile
make test
```

### 2. 运行特定测试文件

```bash
# 运行单个文件
pytest tests/unit/strategies/test_momentum.py

# 运行特定目录
pytest tests/unit/data/
```

### 3. 运行特定测试类或方法

```bash
# 运行特定类
pytest tests/unit/strategies/test_momentum.py::TestMomentumScore

# 运行特定方法
pytest tests/unit/strategies/test_momentum.py::TestMomentumScore::test_uptrend_positive_momentum
```

### 4. 使用标记过滤测试

```bash
# 只运行单元测试
pytest -m unit

# 排除慢速测试
pytest -m "not slow"

# 只运行集成测试
pytest -m integration

# 只运行需要数据库的测试
pytest -m database
```

### 5. 详细输出

```bash
# 详细输出
pytest -v

# 更详细的输出
pytest -vv

# 显示print输出
pytest -s
```

### 6. 并行执行

```bash
# 安装pytest-xdist
pip install pytest-xdist

# 并行执行（自动检测CPU数量）
pytest -n auto

# 指定进程数
pytest -n 4
```

### 7. 失败时停止

```bash
# 第一个失败时停止
pytest -x

# 2个失败时停止
pytest --maxfail=2
```

## 测试覆盖率

### 生成覆盖率报告

```bash
# 运行测试并生成覆盖率报告
pytest --cov=. --cov-report=term-missing

# 生成HTML报告
pytest --cov=. --cov-report=html

# 指定覆盖率阈值
pytest --cov=. --cov-fail-under=80
```

### 查看HTML覆盖率报告

```bash
# 生成报告后，打开htmlcov/index.html
open htmlcov/index.html
```

## 测试分类

### 单元测试 (@pytest.mark.unit)

- 测试单个函数或类的功能
- 不依赖外部资源（数据库、API等）
- 使用mock隔离外部依赖
- 执行速度快

### 集成测试 (@pytest.mark.integration)

- 测试多个模块的集成
- 可能需要外部资源
- 执行速度较慢

### 慢速测试 (@pytest.mark.slow)

- 执行时间超过1秒的测试
- 大数据集测试
- 性能基准测试

## 编写测试规范

### 1. 测试文件命名

- 文件名以 `test_` 开头
- 例如：`test_momentum.py`

### 2. 测试类命名

- 类名以 `Test` 开头
- 例如：`TestMomentumStrategy`

### 3. 测试方法命名

- 方法名以 `test_` 开头
- 使用描述性名称，说明测试什么
- 例如：`test_uptrend_positive_momentum`

### 4. 测试结构

遵循 AAA 模式：

```python
def test_example():
    # Arrange (准备)
    data = create_test_data()
    strategy = Strategy()
    
    # Act (执行)
    result = strategy.calculate(data)
    
    # Assert (断言)
    assert result > 0
```

### 5. 使用Fixtures

```python
import pytest

@pytest.fixture
def sample_data():
    return create_sample_data()

def test_with_fixture(sample_data):
    # 使用fixture
    assert len(sample_data) > 0
```

### 6. 参数化测试

```python
@pytest.mark.parametrize("input,expected", [
    (100, 100),
    (200, 200),
    (300, 300),
])
def test_parameterized(input, expected):
    assert calculate(input) == expected
```

### 7. 异常测试

```python
import pytest

def test_exception():
    with pytest.raises(ValueError):
        raise ValueError("Test exception")
```

## Mock使用

### 1. 基本Mock

```python
from unittest.mock import Mock

def test_with_mock():
    mock_obj = Mock()
    mock_obj.method.return_value = 100
    
    result = mock_obj.method()
    assert result == 100
```

### 2. Patch

```python
from unittest.mock import patch

@patch('module.function')
def test_with_patch(mock_function):
    mock_function.return_value = 100
    
    result = function()
    assert result == 100
```

### 3. Patch上下文管理器

```python
from unittest.mock import patch

def test_with_patch_context():
    with patch('module.function') as mock_func:
        mock_func.return_value = 100
        
        result = function()
        assert result == 100
```

## 测试最佳实践

### 1. 独立性

每个测试应该独立，不依赖其他测试的执行顺序或结果。

### 2. 可重复性

测试结果应该稳定，多次运行结果相同。

### 3. 清晰性

测试代码应该清晰易懂，测试意图明确。

### 4. 快速性

单元测试应该快速执行，避免耗时操作。

### 5. 完整性

测试应该覆盖正常情况、边界情况和异常情况。

## 持续集成

### GitHub Actions配置示例

```yaml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v2
    
    - name: Set up Python
      uses: actions/setup-python@v2
      with:
        python-version: 3.9
    
    - name: Install dependencies
      run: |
        pip install -r requirements.txt
    
    - name: Run tests
      run: |
        pytest --cov=. --cov-report=xml
    
    - name: Upload coverage
      uses: codecov/codecov-action@v2
```

## 测试报告

### 生成Junit XML报告

```bash
pytest --junit-xml=reports/junit.xml
```

### 生成HTML报告

```bash
pytest --html=reports/report.html --self-contained-html
```

## 故障排查

### 1. 测试失败

```bash
# 查看详细错误信息
pytest -vv --tb=long

# 进入调试模式
pytest --pdb
```

### 2. 导入错误

```bash
# 检查Python路径
python -c "import sys; print(sys.path)"

# 确保项目根目录在路径中
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
```

### 3. Fixture未找到

```bash
# 确保conftest.py在正确位置
# 确保fixture名称正确
```

## 性能测试

### 使用pytest-benchmark

```python
def test_performance(benchmark):
    result = benchmark(calculate, data)
    assert result > 0
```

## 调试技巧

### 1. 使用print调试

```bash
pytest -s test_file.py
```

### 2. 使用pdb调试

```python
def test_example():
    import pdb; pdb.set_trace()
    # 测试代码
```

### 3. 使用pytest pdb

```bash
pytest --pdb test_file.py
```

## 常见问题

### Q: 测试发现不了？

A: 检查文件名、类名、方法名是否符合pytest规范。

### Q: Fixture未找到？

A: 检查conftest.py位置和fixture名称。

### Q: 测试覆盖率低？

A: 使用覆盖率报告找出未覆盖的代码，补充测试。

### Q: 测试执行慢？

A: 使用mock隔离外部依赖，使用标记跳过慢速测试。

## 参考资料

- [Pytest官方文档](https://docs.pytest.org/)
- [Python Mock文档](https://docs.python.org/3/library/unittest.mock.html)
- [pytest-cov文档](https://pytest-cov.readthedocs.io/)
