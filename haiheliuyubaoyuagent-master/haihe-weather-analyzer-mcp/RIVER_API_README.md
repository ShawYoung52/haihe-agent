# 河系查询 REST API 使用说明

## 服务地址

```
http://10.226.107.130:8002
```

## 接口列表

### 1. 河流概况查询

```
POST /api/v1/river/profile
Body: {"name": "永定河"}
```

返回河流的基本信息、上游河流、直接下游、间接下游（含层级）及河段明细。

**返回示例：**

```json
{
  "code": 200,
  "data": {
    "river": "永定河",
    "segment_count": 5,
    "total_length_km": 296.1,
    "upstream_rivers": ["洋河", "桑干河", "天堂河", ...],
    "direct_downstream": [
      {"name": "北运河", "segment_count": 8, "total_length_km": 142.4}
    ],
    "indirect_downstream": [
      {"name": "海河", "level": 2, "segment_count": 4, "total_length_km": 65.8},
      {"name": "金钟河", "level": 3, "segment_count": 2, "total_length_km": 27.4},
      {"name": "永定新河", "level": 4, "segment_count": 4, "total_length_km": 55.2}
    ],
    "segments": [
      {"from": [115.44, 40.355], "to": [115.625, 40.299], "length_km": 22.308}
    ]
  }
}
```

**字段说明：**

| 字段 | 说明 |
|------|------|
| `segment_count` | 该河流的河段数量 |
| `total_length_km` | 河段总长度（公里） |
| `upstream_rivers` | 上游河流列表（汇入该河的所有干支流） |
| `direct_downstream` | **直接下游**：从该河末端节点直接分出的河流 |
| `indirect_downstream` | **间接下游**：经过直接下游才能到达的河流，`level` 表示层级（2=直接下游的下一级，以此类推） |
| `segments` | 河段明细，包含起点坐标、终点坐标、长度 |

---

### 2. 查询上游河流

```
POST /api/v1/river/upstream
Body: {"name": "永定河"}
```

返回所有上游河流（汇入该河的所有干支流）。

**返回示例：**

```json
{
  "code": 200,
  "data": {
    "river": "永定河",
    "upstream_count": 14,
    "upstream": [
      {"name": "洋河", "segment_count": 3, "total_length_km": 128.5},
      {"name": "桑干河", "segment_count": 3, "total_length_km": 95.2},
      ...
    ]
  }
}
```

---

### 3. 查询下游河流

```
POST /api/v1/river/downstream
Body: {"name": "永定河"}
```

返回河流的直接下游和间接下游（分层级）。

**返回示例：**

```json
{
  "code": 200,
  "data": {
    "river": "永定河",
    "direct_downstream": [
      {"name": "北运河", "segment_count": 8, "total_length_km": 142.4}
    ],
    "indirect_downstream": [
      {"name": "海河", "level": 2, "segment_count": 4, "total_length_km": 65.8},
      {"name": "金钟河", "level": 3, "segment_count": 2, "total_length_km": 27.4}
    ]
  }
}
```

---

## 上下游判定规则

### 直接下游（direct_downstream）

从当前河流的末端节点直接分出的所有河流。同一节点可能分出多条河（如永定河在屈家店枢纽同时分出永定新河和北运河）。

### 间接下游（indirect_downstream）

经过直接下游递归到达的河流。每条直接下游从汇入节点开始沿下游走，沿途收集分叉河流作为间接下游。`level` 表示层级距离（层级2 = 经过1条直接下游到达）。

### 上游河流（upstream_rivers）

沿有向图逆流方向递归，收集所有汇入当前河流的干支流。

---

## 数据说明

### 数据来源

河网数据来自 `river_directed_v5.pkl`，由原始 SHP 文件通过有向图构建生成。节点合并精度为 3 位小数（约 111m）。

### 逗号河名

部分河段使用逗号连接两个河名（如"京杭大运河,子牙河"），表示该河段同时属于两个水系。系统会自动识别这种命名规则，确保从任一河名都能匹配到该共享河段。

### 已知限制

- 部分非自然河道（如排水河、减河等人工渠道）在图上与自然河道共享坐标节点时，可能出现在间接下游中
- 河段完整性和拓扑精度取决于原始 SHP 数据质量
- Strahler 等级等河流属性需要额外拓扑计算，当前不提供

---

## 调用示例

```bash
# 河流概况
curl -X POST http://10.226.107.130:8002/api/v1/river/profile \
  -H "Content-Type: application/json" \
  -d '{"name":"永定河"}'

# 上游河流
curl -X POST http://10.226.107.130:8002/api/v1/river/upstream \
  -H "Content-Type: application/json" \
  -d '{"name":"子牙河"}'

# 下游河流
curl -X POST http://10.226.107.130:8002/api/v1/river/downstream \
  -H "Content-Type: application/json" \
  -d '{"name":"大清河"}'
```