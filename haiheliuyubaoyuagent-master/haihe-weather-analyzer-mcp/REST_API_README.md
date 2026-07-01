# 应急响应判定 REST API 使用说明

## 服务地址

```
http://10.226.107.130:8002
```

## 启动方式

已部署在服务器，自动启动。如遇问题联系管理员。

## 接口列表

### 1. 应急响应判定（按时间段自动评估）

```
POST /api/v1/emergency/evaluate
```

自动扫描时间段内的每个整点观测时次（02/08/14/20），判断是否触发应急响应。

**请求体（JSON）：**

```json
{
  "start_time": "2023-07-29 20:00:00",
  "end_time": "2023-07-30 20:00:00"
}
```

参数说明：
- `start_time`：开始时间，格式 `YYYY-MM-DD HH:MM:SS`，不传默认24小时前
- `end_time`：结束时间，格式同上，不传默认当前时间
- `basin_codes`：流域代码，默认 `HHLY_JUECE`（海河流域）
- `allowed_station_levels`：站点等级，默认 `11,12,13,16`

**调用示例：**

```bash
curl -X POST http://10.226.107.130:8002/api/v1/emergency/evaluate \
  -H "Content-Type: application/json" \
  -d '{"start_time": "2023-07-29 20:00:00", "end_time": "2023-07-30 20:00:00"}'
```

**返回示例：**

```json
{
  "code": 200,
  "data": {
    "start_time": "2023-07-29 20:00:00",
    "end_time": "2023-07-30 20:00:00",
    "max_level": "II",
    "triggered_count": 5,
    "events": [
      {"time": "2023-07-29 20:00", "max_level": "IV", "reached_station_count": 47},
      {"time": "2023-07-30 02:00", "max_level": "II", "reached_station_count": 28},
      ...
    ]
  },
  "message": "success"
}
```

---

### 2. 应急响应按日汇总

```
POST /api/v1/emergency/summary
```

与 evaluate 不同，以"天"为单位合并展示。

**参数（Query）：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| start_time | string | 否 | 开始时间，默认7天前 |
| end_time | string | 否 | 结束时间，默认当前 |
| basin_codes | string | 否 | 流域代码，默认 HHLY_JUECE |
| allowed_station_levels | string | 否 | 站点等级，默认 11,12,13,16 |

**调用示例：**

```bash
curl -X POST "http://10.226.107.130:8002/api/v1/emergency/summary?start_time=2023-07-29%2020:00:00&end_time=2023-07-30%2020:00:00"
```

**返回示例：**

```json
{
  "code": 200,
  "data": {
    "daily_summary": [
      {"date": "2023-07-29", "max_level": "IV", "triggered_count": 1, "events": [...]},
      {"date": "2023-07-30", "max_level": "II", "triggered_count": 4, "events": [...]}
    ]
  }
}
```

---

### 3. 查询应急事件列表

```
GET /api/v1/emergency/events
```

查数据库中的应急事件记录（已保存的历史事件）。

**参数（Query）：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| status | string | 否 | 按状态筛选：active / received / resolved / cancelled |
| page | int | 否 | 页码，默认1 |
| page_size | int | 否 | 每页条数，默认20，最大100 |

**调用示例：**

```bash
curl "http://10.226.107.130:8002/api/v1/emergency/events"
curl "http://10.226.107.130:8002/api/v1/emergency/events?status=active"
```

---

### 4. 查看事件详情

```
GET /api/v1/emergency/events/{event_code}
```

**调用示例：**

```bash
curl http://10.226.107.130:8002/api/v1/emergency/events/EMERG-20260603-088d81a5
```

返回事件基本信息 + 关联产品图列表 + 站点快照列表。

---

### 5. 确认签收事件

```
POST /api/v1/emergency/events/{event_code}/confirm
```

点击确认后，事件状态从 active 变为 received（已接收），记录确认人和确认时间。

**参数（Query）：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| confirm_user | string | 否 | 确认人姓名 |

**调用示例：**

```bash
curl -X POST "http://10.226.107.130:8002/api/v1/emergency/events/EMERG-20260603-088d81a5/confirm?confirm_user=%E5%BC%A0%E4%B8%89"
```

---

### 6. 列出所有接口

```
GET /api/v1/endpoints
```

### 7. 健康检查

```
GET /health
```

## 常用查询示例

### 查今天有没有触发应急响应

```bash
curl -X POST http://10.226.107.130:8002/api/v1/emergency/evaluate \
  -H "Content-Type: application/json" \
  -d '{}'
```

### 查"23·7"暴雨期间（2023年7月底~8月初）

```bash
curl -X POST http://10.226.107.130:8002/api/v1/emergency/evaluate \
  -H "Content-Type: application/json" \
  -d '{"start_time": "2023-07-29 20:00:00", "end_time": "2023-08-02 20:00:00"}'
```

### 查某天的应急响应

```bash
curl -X POST "http://10.226.107.130:8002/api/v1/emergency/summary?start_time=2023-07-29%2020:00:00&end_time=2023-07-30%2020:00:00"
```

## 注意事项

1. **时间范围不宜过大**，一次查询建议不超过7天，评估范围为实况时效内的逐02/08/14/20时次
2. 判定标准基于**国家站**（默认11/12/13/16级站点），如需区域站可调整 allowed_station_levels 参数
3. 判定口径为**实况**（天擎自动站数据），不含预报
4. 所有时间均为**北京时间**