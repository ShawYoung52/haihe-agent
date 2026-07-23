# 海河流域降雨分析MCP服务

基于FastMCP构建的气象数据服务，提供多维度降雨数据查询和分析功能。

## 功能特性

### 🌧️ 数据查询工具
- **站点历史数据查询** - 获取指定气象站点的历史降雨记录
- **时间范围查询** - 按时间段查询降雨数据
- **位置查询** - 根据经纬度查询附近站点数据
- **区域分析** - 对特定区域进行综合降雨分析

### 📊 分析功能
- **统计计算** - 提供降雨数据的基础统计信息
- **趋势分析** - 分析降雨变化趋势
- **风险评估** - 自动评估降雨风险等级

### 🔮 预测预警
- **降雨预报** - 提供未来24小时降雨预测
- **预警检测** - 实时监测并发布降雨预警信息

## 安装部署

### 环境要求
- Python 3.8+
- pip包管理器

### 安装步骤

1. 克隆项目或下载源码
2. 安装依赖：
```bash
pip install -r requirements.txt
```

3. 或者以开发模式安装：
```bash
pip install -e .
```

## 快速开始

### 启动服务

```bash
# 默认启动 (localhost:8000)
python main.py

# 指定主机和端口
python main.py --host 0.0.0.0 --port 8080

# 异步模式启动
python main.py --async --port 8000
```

### 使用示例

#### 1. 获取服务信息
```python
result = get_service_info()
```

#### 2. 查询站点历史数据
```python
# 获取天津站最近24小时数据
data = get_station_history("ST001", 24)
```

#### 3. 时间范围查询
```python
# 查询2024年1月1日到1月2日的数据
data = query_time_range(
    "2024-01-01T00:00:00",
    "2024-01-02T23:59:59"
)
```

#### 4. 位置查询
```python
# 查询天津市区附近10公里范围内的站点
data = query_nearby_stations(39.12, 117.20, 10.0)
```

#### 5. 数据统计分析
```python
# 对查询到的数据进行统计
stats = calculate_rainfall_statistics(data)
```

#### 6. 区域分析
```python
# 分析天津市区域降雨情况
analysis = analyze_region_rainfall(
    "天津市", 
    ["ST001", "ST002", "ST003"]
)
```

#### 7. 获取预报
```python
# 获取未来12小时降雨预报
forecast = get_rainfall_forecast(12)
```

#### 8. 检查预警
```python
# 检查当前降雨预警
alerts = check_rainfall_alerts()
```

## API工具列表

| 工具名称 | 功能描述 | 主要参数 |
|---------|---------|---------|
| `get_service_info` | 获取服务基本信息 | 无 |
| `get_available_stations` | 获取可用站点列表 | 无 |
| `get_station_history` | 获取站点历史数据 | `station_id`, `hours_back` |
| `query_time_range` | 时间范围查询 | `start_time`, `end_time`, `station_ids` |
| `query_nearby_stations` | 位置查询 | `latitude`, `longitude`, `radius_km` |
| `calculate_rainfall_statistics` | 数据统计 | `data` |
| `analyze_region_rainfall` | 区域分析 | `region_name`, `station_ids` |
| `get_rainfall_forecast` | 降雨预报 | `hours_ahead` |
| `check_rainfall_alerts` | 预警检查 | 无 |

## 支持的站点

目前支持以下气象站点：
- ST001: 天津站
- ST002: 塘沽站  
- ST003: 武清站
- ST004: 静海站
- ST005: 宝坻站
- ST006: 蓟州站
- ST007: 宁河站

## 数据说明

- **数据来源**: 模拟气象观测数据
- **更新频率**: 实时生成
- **数据质量**: 高质量模拟数据
- **覆盖区域**: 天津市及周边地区

## 错误处理

服务包含完善的错误处理机制：
- 站点不存在时返回明确错误信息
- 参数验证确保输入有效性
- 网络异常时提供重试机制

## 开发指南

### 项目结构
```
haihe-weather-analyzer-mcp/
├── main.py              # 程序入口
├── server.py           # MCP服务器实现
├── tools.py            # 工具函数集合
├── models.py           # 数据模型定义
├── requirements.txt    # 依赖包列表
└── README.md          # 说明文档
```

### 扩展开发

1. **添加新工具**: 在 `tools.py` 中添加新的工具函数
2. **扩展数据模型**: 修改 `models.py` 添加新的数据结构
3. **自定义分析**: 在 `RainfallAnalyzer` 类中添加新的分析方法

## 许可证

MIT License

## 技术支持

如有问题请联系：haihe-weather-support@example.com