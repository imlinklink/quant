# 富途API K线数据限制分析与解决方案

## 问题分析

### 当前限制
富途OpenAPI的限制：
1. **`get_cur_kline()`**: 只能获取最近 **500根K线**
2. **`request_history_kline()`**: 可以获取历史数据，支持更长时间范围 ✅

### 原始代码问题
在 `mutifactor/data/futu_fetcher.py` 中，原来使用：
```python
# 获取最近500根K线
ret, data = self.quote_ctx.get_cur_kline(
    code=futu_code,
    num=500,
    ktype=futu_ktype,
    autype=AuType.QFQ
)
```

**问题**: 只能获取最近约2年的日线数据（500个交易日）

## ✅ 解决方案已实施

### 使用 `request_history_kline()` 获取历史数据

已修改 `futu_fetcher.py` 的 `fetch_stock_kline` 方法：

```python
# 优先使用 request_history_kline 获取历史数据
ret, data, page_req_key = self.quote_ctx.request_history_kline(
    code=futu_code,
    start=start_date,
    end=end_date,
    ktype=futu_ktype,
    autype=AuType.QFQ,
    max_count=None  # 返回所有数据
)

# 如果失败，降级使用 get_cur_kline
if ret != RET_OK:
    # ... 降级逻辑
```

## ✅ 测试结果

### 数据获取能力验证

| 时间范围 | 数据条数 | 实际跨度 | 状态 |
|---------|---------|---------|------|
| 最近1年 | 247条 | 365天 | ✅ 成功 |
| 最近2年 | 487条 | 730天 | ✅ 成功 |
| 最近3年 | 736条 | 1095天 | ✅ 成功 |
| 最近5年 | 1228条 | 1824天 | ✅ 成功 |

### 示例输出

```
测试: 最近3年
日期范围: 2023-03-07 至 2026-03-06
============================================================
✅ 成功获取数据:
   数据条数: 736
   实际日期范围: 2023-03-07 至 2026-03-06
   时间跨度: 1095 天
```

## 方法对比

### `get_cur_kline()` - 实时K线
- ✅ 优点：实时更新
- ❌ 限制：最多500根K线
- 📌 用途：实时交易、短期策略
- 🔔 注意：需要先订阅

### `request_history_kline()` - 历史K线 ✅ 推荐
- ✅ 优点：支持长时间历史数据
- ✅ 优点：不需要先订阅
- ✅ 优点：支持指定时间范围
- 📌 用途：回测、历史分析
- 🎯 **已采用此方法**

## 修改详情

### 文件修改
- `mutifactor/data/futu_fetcher.py`
  - 第268-331行：修改 `fetch_stock_kline` 方法
  - 优先使用 `request_history_kline`
  - 失败时降级到 `get_cur_kline`

### 向后兼容
- ✅ 如果新方法失败，自动降级到原方法
- ✅ 不会影响现有功能
- ✅ 接口保持不变

## 使用建议

### 1. 短期策略测试（< 2年）
- ✅ 两种方法均可
- 建议使用缓存机制

### 2. 中期策略测试（2-3年）
- ✅ 使用 `request_history_kline`
- 可以获取完整历史数据

### 3. 长期策略验证（3-5年）
- ✅ 使用 `request_history_kline`
- 数据充足，适合长期回测

### 4. 超长期策略（5年+）
- ⚠️ 建议分段回测
- 或考虑第三方数据源作为补充

## 性能优化建议

### 1. 数据缓存
```python
# 首次获取后保存到本地
cache_file = 'cache/hk_futu_data_full.parquet'
fetcher.save_data(price_data, cache_file)
```

### 2. 增量更新
- 定期更新缓存数据
- 只获取缺失的日期范围

### 3. 批量获取
- 使用多线程并行获取多只股票
- 注意API频率限制

## 注意事项

1. **API调用限制**: 批量获取时控制速度，避免触发限流
2. **数据完整性**: 检查停牌、退市等特殊情况
3. **复权处理**: 使用前复权(QFQ)确保数据连续性
4. **时区问题**: 注意港股交易时间
5. **分页处理**: 大量数据可能需要分页（使用 `page_req_key`）

## 总结

✅ **问题已解决**：现在可以获取超过500根K线的历史数据

✅ **实施效果**：成功获取5年历史数据（1228条记录）

✅ **向后兼容**：不影响现有功能

🎯 **下一步建议**：
1. 更新缓存数据以利用新功能
2. 运行长期回测验证策略稳定性
3. 定期更新数据缓存
