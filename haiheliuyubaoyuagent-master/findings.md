# 研究发现：58智能体接入 14所长图接口（降水实况文字）

## 目标接口

`POST /openapi/rainfall_describe/real`（降水实况文字）@ base `10.226.107.35:8001`

请求 body：
```json
{
  "areaIds": [6,7,8,9,10,11,12,13,14],   // 区域 id
  "beginTime": "2025-07-26 10:00:00",    // 开始时间（北京时）
  "endTime":   "2025-07-27 10:00:00",    // 结束时间（北京时）
  "interval":  24,                        // 间隔小时，>24 用累计
  "type":      "0",                       // 0 国家站 / 1 区域站
  "range":     "9"                        // 分区，9 或 11（接口文档必填）
  "isClimateImg": true                    // 出图文字颜色是否黑色（可选）
}
```

## 代码库同族模式

### 1. 14所出图模式（新，port 8080）`custom_tools/basin_drawing_tool.py`
- 模块级 `BASIN_DRAWING_API_BASE = os.getenv("BASIN_DRAWING_API_BASE", "http://10.226.107.35:8080")`
- `make_ttl_cache` 共享缓存（`custom_tools/_ttl_cache.py`，只缓存 status=="ok"）
- 核心函数 `generate_*_core(...) -> dict` + `register_*_tool(mcp: FastMCP)` 里 `@mcp.tool()` 包装
- 图片返回**代理 URL**，`server.py` 注册，需 `_IMAGE_URL_ALLOW_HOSTS` 放行

### 2. 同端口 base64 出图模式（old，port 8001）`haihe_mcp_tools.py:3613 get_station_rainfall_real_img`
- 直接 `requests.post(url, json=payload, timeout=60)`
- 兼容多种返回：`data.get("data") or .get("result") or .get("image") or data` 取 base64
- 返回 `{"base64": ..., "beginTime", "endTime", "interval", "range"}` 或 `{"error": ...}`
- 前端 `_run_tool_round` 特判 + `_try_rainfall_img_fast_path`（ENABLE_FAST_PATHS=true 时）解码 base64→cl.Image

### 3. 前端展示路径（chainlitexam/message_orchestrator.py）
- `TOOL_DISPLAY_NAMES` = tool 名 → 中文展示名（:1740）
- `_run_tool_round` 特判 `elif tool_name == "get_station_rainfall_real_img":`（:2150）
  - 取 `data["base64"]`，剥 `,` 前缀，`base64.b64decode` → `cl.Image(content=..., name=...)` 发送
  - 设置 `has_chart_generated=True`，`observation_text` 告知 LLM 已绘出并简要说明
- 错误分支区分「无记录/超时/鉴权」等，隐藏原始错误
- 降雨分布图 fast path（:2282）仅 ENABLE_FAST_PATHS=true 生效

### 4. HTTP 图片字段（chainlitexam/qa_http_api.py）
- `_build_image_payload`：把 emitter 的 cl.Image 元素映射成 `{name,url,mime}`（本地落盘文件 URL）
- 外部 markdown 图链（代理 URL）追加为 images 条目，但**仅放行 allowlist 主机**
- `_IMAGE_URL_ALLOW_HOSTS` 默认 `10.226.107.35:8080`；改基线须同步

### 5. prompt 路由（chainlitexam/prompts.py）
- 工具规则双轨：planner/`WEATHER_ASSISTANT_PROMPT` 两处都要加（如 get_station_rainfall_real_img 在 :399 与 :1055）
- 规则要点：图片工具只出图不给数值；明确要图才调；调后前端自动展示，回答简短

## 关键结论

- 新工具沿用 `get_station_rainfall_real_img` 的 base64 模式（同 port 8001），不碰 `_scrub` 白名单。
- 触发词：用户说"降水实况文字/生成降水实况/降水实况"等；区别于"降雨分布图/降水实况图"（旧图工具关键词，
  而"降水实况文字"不含"图"字，不会被旧 fast path 拦截）。
- 工具命名建议：`generate_rainfall_describe_longimg`（语义清晰，planner 可按 docstring 路由）。
- 参数用 camelCase（beginTime/endTime/areaIds/interval/range/type/isClimateImg），与接口及旧工具一致。