# 预报检验评估集成到问答智能体 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 forecast_evaluate 预报检验能力集成到问答智能体（MCP 工具 + fast path + prompt 规则）

**Architecture:** 新增 MCP 工具 `evaluate_forecast` 封装检验API，消息编排器新增 fast path 关键词直连，Planner LLM 通过新增 prompt 规则感知工具路由。不修改 forecast_evaluate 原代码，MCP 层做薄封装导入。

**Tech Stack:** Python 3.10+, FastMCP, requests, matplotlib, Chainlit Message/Step, LangChain messages

## Global Constraints

- **不修改** `forecast_evaluate/scripts/` 下原有代码（除 config.py 扩展区划配置）
- **工具注册**走 `@mcp.tool()` 装饰器模式，与其他 custom_tools 一致
- **Fast path** 必须遵守契约：`_show_business_reasoning` + 所有返回路径关闭 reasoning step + 引用 `thinking_chain` 或 `generate_fast_path_thinking`
- **测试**从 `chainlitexam/` 目录运行，使用隔离 venv `D:\PythonProject\.venv-haihe-tests\Scripts\python.exe`
- **日期时间**默认北京时间（UTC+8），工具返回统一标注"北京时"
- **内部服务地址**不在用户可见输出中暴露，全部用 `_scrub_internal_data` 处理
- **检验API产品代码**：`NAFP_SCMOC_NC`(国家指导)、`NAFP_BETJ_DS_NC`(天津预报)、`NAFP_ECTHIN_NC`(ECMWF)

---

### Task 1: 创建 MCP 工具 `forecast_evaluate_tool.py`

**Files:**
- Create: `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/forecast_evaluate_tool.py`

**Interfaces:**
- Produces: `register_forecast_evaluate_tool(mcp: FastMCP) -> None`

- [ ] **Step 1: 创建工具文件框架**

```python
# haihe-weather-analyzer-mcp/forecast_evaluate_tool.py
"""预报检验评估 MCP 工具。

封装 forecast_evaluate/scripts/ 核心函数，提供统一的 'evaluate_forecast' 工具。
通过检验 API (10.226.107.74:31002) 获取 TS/PC/BIAS/MAE/ME 等指标，
支持降水（晴雨/分级/累计）与温度检验，含进程内缓存（TTL=1h）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# 导入 forecast_evaluate 核心函数
_EVALUATE_SCRIPTS = Path(__file__).resolve().parents[1] / "forecast_evaluate 2" / "forecast_evaluate" / "scripts"
if str(_EVALUATE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_EVALUATE_SCRIPTS))

from config import Config as EvalConfig
from forecast_evaluate import request_scores, run_rain_eva, run_temp_eva
from analyzer import ForecastAnalyzer

# 进程内缓存
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 3600  # 1 小时


def _cache_key(*args: Any) -> str:
    raw = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str) -> dict[str, Any] | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    stored_at, data = entry
    if time.time() - stored_at > _CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    logger.info("[forecast_evaluate] cache hit key=%s", key[:12])
    return data


def _cache_set(key: str, data: dict[str, Any]) -> None:
    _CACHE[key] = (time.time(), data)
    logger.info("[forecast_evaluate] cache set key=%s (size=%d)", key[:12], len(_CACHE))
```

- [ ] **Step 2: 编写结果格式化函数**

```python
def _format_evaluate_result(api_result: dict, element: str, test_type: str,
                            rain_type: str | None) -> dict[str, Any]:
    """将检验API返回的原始数据转化为 LLM 可消费的结构化 JSON。"""
    analyzer = ForecastAnalyzer(api_result)
    report = analyzer.generate_detailed_report()

    metrics: dict[str, dict[str, Any]] = {}
    for category, sub_details in report.get("details", {}).items():
        if isinstance(sub_details, dict):
            for metric_name, data in sub_details.items():
                ranking: list[tuple[str, float]] = data.get("ranking", [])
                best = ranking[0] if ranking else ("", 0.0)
                metrics[metric_name] = {
                    "ranking": [[name, round(val, 2)] for name, val in ranking],
                    "best": best[0],
                    "best_value": round(best[1], 2),
                    "unit": _metric_unit(metric_name),
                }

    summary = report.get("summary", "")
    time_range = api_result.get("time_range", {})

    return {
        "element": EvalConfig.ALL_ELEMENTS.get(element, element),
        "element_code": element,
        "test_type": EvalConfig.TEST_TYPE_NAMES.get(test_type, test_type),
        "test_type_code": test_type,
        "time_range": time_range,
        "rain_type": rain_type,
        "data_source": "检验API",
        "metrics": metrics,
        "summary": summary,
    }


def _metric_unit(metric_name: str) -> str:
    if "准确率" in metric_name or "PC" in metric_name:
        return "%"
    if "MAE" in metric_name or "ME" in metric_name:
        return "°C"
    if "TS" in metric_name:
        return ""
    if "偏差" in metric_name or "BIAS" in metric_name:
        return ""
    return ""
```

- [ ] **Step 3: 编写 MCP 工具注册函数**

```python
def register_forecast_evaluate_tool(mcp: FastMCP) -> None:

    @mcp.tool()
    def evaluate_forecast(
        element: str,
        test_type: str,
        rain_type: str = "",
        begin_time: str = "",
        end_time: str = "",
        time_session: int = 24,
    ) -> dict[str, Any]:
        """查询预报检验评分数据。

        支持 TS评分、准确率(PC)、偏差(BIAS)、平均绝对误差(MAE)、平均误差(ME)
        等指标的查询。对比产品为国家指导、天津预报、ECMWF。

        :param element: 检验要素，rain24=24h降水，tmax24=最高温，tmin24=最低温，t2m=2m温度
        :param test_type: 检验维度，daily=逐日，time_session=逐时效，area=分地区
        :param rain_type: 降水子类（仅降水需要），ng=晴雨，g=分级暴雨，acc=累计
        :param begin_time: 开始时间 YYYY-MM-DD HH:MM:SS，默认本月1日
        :param end_time: 结束时间 YYYY-MM-DD HH:MM:SS，默认昨天
        :param time_session: 预报时效(小时)，24/48/72，默认24
        """
        # 参数校验
        valid_elements = set(EvalConfig.ALL_ELEMENTS.keys())
        if element not in valid_elements:
            return {"error": f"无效要素 {element}，可选: {sorted(valid_elements)}"}

        valid_test_types = set(EvalConfig.TEST_TYPE_NAMES.keys())
        if test_type not in valid_test_types:
            return {"error": f"无效检验维度 {test_type}，可选: {sorted(valid_test_types)}"}

        is_rain = element in EvalConfig.RAIN_ELEMENTS
        if is_rain and rain_type not in ("ng", "g", "acc", ""):
            return {"error": f"降水需要指定 rain_type: ng/g/acc，当前: {rain_type!r}"}
        if not is_rain:
            rain_type = None  # type: ignore[assignment]

        # 默认时间：本月 1 日 ~ 昨天
        now = datetime.now()
        month_begin = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        yesterday_end = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)

        b_time = begin_time if begin_time else month_begin.strftime("%Y-%m-%d %H:%M:%S")
        e_time = end_time if end_time else yesterday_end.strftime("%Y-%m-%d %H:%M:%S")

        # 缓存查找
        ck = _cache_key(element, test_type, rain_type, b_time, e_time, time_session)
        cached = _cache_get(ck)
        if cached is not None:
            return cached

        # 实时调用检验API
        try:
            if is_rain:
                api_result = run_rain_eva(
                    test_type=test_type,
                    rain_type=rain_type,
                    begin_time=b_time,
                    end_time=e_time,
                    time_session=time_session,
                    save_json=False,
                )
            else:
                api_result = run_temp_eva(
                    test_type=test_type,
                    begin_time=b_time,
                    end_time=e_time,
                    time_session=time_session,
                    save_json=False,
                )

            if "error" in api_result:
                return {"error": api_result["error"]}

            if not api_result.get("request_success"):
                raw = api_result.get("raw_response", {})
                return {"error": f"检验API返回失败: {raw.get('code', 'unknown')}"}

            formatted = _format_evaluate_result(api_result, element, test_type, rain_type)
            _cache_set(ck, formatted)
            return formatted

        except Exception as exc:
            logger.exception("[forecast_evaluate] 工具执行异常")
            return {"error": f"预报检验查询失败: {type(exc).__name__}: {exc}"}
```

- [ ] **Step 4: 写入文件并验证导入**

Run: `py -3 -c "import sys; sys.path.insert(0, r'D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\haihe-weather-analyzer-mcp'); from forecast_evaluate_tool import register_forecast_evaluate_tool; print('import OK')"`

- [ ] **Step 5: Commit**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git add haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/forecast_evaluate_tool.py
git commit -m "feat(mcp): add evaluate_forecast tool for forecast verification"
```

---

### Task 2: 在 server.py 注册工具

**Files:**
- Modify: `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/server.py`

**Interfaces:**
- Consumes: `register_forecast_evaluate_tool(mcp: FastMCP)` from `forecast_evaluate_tool`

- [ ] **Step 1: 修改 server.py**

在文件顶部新增导入（紧接 `from custom_tools import` 块之后）：

```python
# 在现有 custom_tools 导入块之后新增
from forecast_evaluate_tool import register_forecast_evaluate_tool
```

在 `_register_tools` 方法末尾（`register_safe_emergency_response_tool` 之后）新增：

```python
        register_forecast_evaluate_tool(self.mcp)
```

完整修改位置：
- **import** at `server.py:15` (after `register_safe_emergency_response_tool,`)
- **register call** at `server.py:41` (after `register_safe_emergency_response_tool(self.mcp)`)

- [ ] **Step 2: Commit**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git add haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/server.py
git commit -m "feat(mcp): register evaluate_forecast tool in server"
```

---

### Task 3: 在 prompts.py 新增预报检验规则

**Files:**
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/prompts.py`

**Interfaces:**
- Consumes: None (standalone text addition)

- [ ] **Step 1: 找到插入位置**

搜索 `WEATHER_ASSISTANT_PROMPT` 中最后一条编号规则的位置。在现有规则末尾追加新规则。

- [ ] **Step 2: 新增第 13 条规则**

```python
# 在 WEATHER_ASSISTANT_PROMPT 字符串中，最后一条编号规则之后追加：

13. **预报检验与模式评估**：当用户询问 TS评分、晴雨准确率、预报评分、
   模式评估、预报检验、偏差分析(BIAS)、误差分析(MAE/ME)、落区预报对比、
   各家模式对比、暴雨预报效果 等预报检验类问题时，必须调用
   `evaluate_forecast` 工具。参数提取规则：
   - 问"暴雨TS"→ element=rain24, rain_type=g（分级暴雨）
   - 问"晴雨预报"→ element=rain24, rain_type=ng（晴雨）
   - 问"累计降水/面雨量误差"→ element=rain24, rain_type=acc（累计）
   - 问"温度误差/最高温/最低温"→ element=tmax24 或 tmin24
   - 问"逐日/最近一周/逐天"→ test_type=daily
   - 问"分时效/24h/48h/72h"→ test_type=time_session
   - 问"分地区/各区/落区"→ test_type=area
   - 未明确时间范围时，默认查询本月1日至昨天。
   回答时以表格对比展示各家产品（**国家指导**、**天津预报**、**ECMWF**）
   的排名和数值，产品名称加粗，数值保留1-2位小数。
   不要暴露后端工具名、API地址、检验公式等技术细节。
```

- [ ] **Step 3: Commit**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git add haiheliuyubaoyuagent-master/chainlitexam/prompts.py
git commit -m "feat(prompts): add rule 13 for forecast evaluate tool routing"
```

---

### Task 4: 在 message_orchestrator.py 新增 forecast evaluate fast path

**Files:**
- Modify: `haiheliuyubaoyuagent-master/chainlitexam/message_orchestrator.py`

**Interfaces:**
- Consumes: `_show_business_reasoning`, `_invoke_tool_for_fast_path`, `_emit_fast_path_result`, `_prepend_thinking_summary`
- Produces: `_try_forecast_evaluate_fast_path(user_text, thinking_chain, tools, messages, callbacks) -> bool`

- [ ] **Step 1: 定义触发关键词检测函数**

在 `_try_general_weather_fast_path` 函数之前（约 line 2892）新增：

```python
def _need_forecast_evaluate(user_text: str) -> bool:
    """检测用户问题是否需要调用预报检验工具。"""
    if not user_text:
        return False
    keywords = [
        "TS评分", "ts评分", "晴雨预报", "晴雨准确率",
        "模式评估", "模式对比", "模式比较", "各家模式",
        "预报检验", "预报评分", "预报评估",
        "准确率对比", "偏差分析", "偏差对比",
        "落区预报", "暴雨落区", "误差分析",
        "BIAS", "bias", "MAE", "mae",
        "预报效果", "预报准确性",
        "暴雨TS", "暴雨ts",
    ]
    return any(k in user_text for k in keywords)
```

- [ ] **Step 2: 新增 fast path 函数**

```python
async def _try_forecast_evaluate_fast_path(
    user_text: str, thinking_chain, tools, messages, callbacks
) -> bool:
    """预报检验快速路径：直接调用 evaluate_forecast MCP 工具"""
    if not _need_forecast_evaluate(user_text):
        return False

    tool = _find_tool(tools, "evaluate_forecast")
    if not tool:
        return False

    reasoning = None
    try:
        # 从用户问题中提取参数
        element = "rain24"
        test_type = "daily"
        rain_type = ""
        time_session = 24

        # 逐时效 / 分地区
        if any(k in user_text for k in ("逐时效", "分时效", "时间序列")):
            test_type = "time_session"
        elif any(k in user_text for k in ("分地区", "各区", "落区", "各个区")):
            test_type = "area"
        elif any(k in user_text for k in ("逐日", "每天", "逐天")):
            test_type = "daily"

        # 降水子类
        if any(k in user_text for k in ("晴雨", "晴雨预报")):
            rain_type = "ng"
        elif any(k in user_text for k in ("暴雨", "TS评分", "ts评分", "TS")):
            rain_type = "g"
        elif any(k in user_text for k in ("累计", "面雨量误差")):
            rain_type = "acc"

        # 温度
        if any(k in user_text for k in ("最高温", "最高温度", "高温")):
            element = "tmax24"
            rain_type = ""
        elif any(k in user_text for k in ("最低温", "最低温度", "低温")):
            element = "tmin24"
            rain_type = ""
        elif any(k in user_text for k in ("2米温度", "2m温度", "气温")):
            element = "t2m"
            rain_type = ""

        # 预报时效
        import re
        session_match = re.search(r"(\d{2,3})\s*(?:小时|h)", user_text)
        if session_match:
            time_session = int(session_match.group(1))

        reasoning = await _show_business_reasoning(
            "预报检验与模式评估",
            ["检验API数据"],
            "将对比各家模式预报评分并给出结论",
        )
        await generate_fast_path_thinking(
            thinking_chain, user_text, "预报检验与模式评估", ["检验API数据"], reasoning
        )
        await reasoning.stage("📡 查询数据", "正在查询预报检验数据...")

        result = await _invoke_tool_for_fast_path(
            "evaluate_forecast",
            tool,
            {
                "element": element,
                "test_type": test_type,
                "rain_type": rain_type,
                "time_session": time_session,
            },
            user_text,
        )
        data = _unwrap_tool_result(result)
        if not isinstance(data, dict):
            await _emit_fast_path_result(
                "抱歉，预报检验数据查询结果格式异常，请稍后重试。",
                messages, user_text, reasoning=reasoning,
            )
            return True

        if "error" in data:
            await _emit_fast_path_result(
                f"抱歉，预报检验查询失败：{data['error']}",
                messages, user_text, reasoning=reasoning,
            )
            return True

        # 构建业务化回答
        text = _build_forecast_evaluate_answer(data, user_text)
        await _emit_fast_path_result(text, messages, user_text, reasoning=reasoning)
        return True

    except Exception as e:
        print(f"[预报检验快速路径] 失败：{e}")
        traceback.print_exc()
        return False
    finally:
        if reasoning is not None:
            await reasoning.close()
```

- [ ] **Step 3: 新增回答构建函数**

```python
def _build_forecast_evaluate_answer(data: dict, user_text: str) -> str:
    """基于 evaluate_forecast 工具返回构建 Markdown 回答。"""
    element = data.get("element", "")
    test_type = data.get("test_type", "")
    time_range = data.get("time_range", {})
    begin = time_range.get("begin", "")[:10] if time_range.get("begin") else ""
    end = time_range.get("end", "")[:10] if time_range.get("end") else ""

    lines = [
        f"## {element}预报检验结果",
        "",
        f"**检验维度**: {test_type}　**时段**: {begin} ~ {end}　**数据来源**: 检验API",
        "",
    ]

    metrics = data.get("metrics", {})
    if not metrics:
        lines.append("暂无有效检验数据。")
        return "\n".join(lines)

    # 排名表格
    for metric_name, metric_data in metrics.items():
        ranking = metric_data.get("ranking", [])
        if not ranking:
            continue
        unit = metric_data.get("unit", "")
        lines.append(f"### {metric_name}")
        lines.append("")
        # 表头
        cols = ["| 排名 | 产品 | 数值 |", "| :--- | :--- | :--- |"]
        lines.extend(cols)
        for i, (name, value) in enumerate(ranking, 1):
            val_str = f"{value:.2f}{unit}" if unit else f"{value:.2f}"
            prefix = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}"
            lines.append(f"| {prefix} | **{name}** | {val_str} |")
        lines.append("")

    # 总结
    summary = data.get("summary", "")
    if summary:
        lines.append("### 总结")
        lines.append("")
        lines.append(summary)

    return "\n".join(lines)
```

- [ ] **Step 4: 在 process_message 的 fast path 链中注册**

在 `process_message` 中，`ENABLE_FAST_PATHS` 分支下，在 `_try_general_weather_fast_path` 之后（line 3864）新增：

```python
        # 预报检验评估快速路径（TS评分/晴雨/模式对比/误差分析）
        if await _try_forecast_evaluate_fast_path(message.content, thinking_chain, tools, messages, callbacks):
            _log_query_exit(query_start_time, session_id, query_summary, "ok")
            return
```

同时更新 `process_message` 的 docstring 中 fast path 列表。

- [ ] **Step 5: Commit**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git add haiheliuyubaoyuagent-master/chainlitexam/message_orchestrator.py
git commit -m "feat(fast-path): add forecast evaluate fast path"
```

---

### Task 5: 扩展 config.py 区划配置（海河流域）

**Files:**
- Modify: `forecast_evaluate 2/forecast_evaluate/scripts/config.py`

- [ ] **Step 1: 新增海河流域区划代码**

在海河流域未确认 API 支持的具体区划代码之前，先预留配置占位，默认仍用天津：

```python
# 在 AreaConfig 类中，TJ_AREA_NAMES 之后新增：

# 海河流域行政区划代码（暂为天津，后续按API能力扩展）
HAIHE_AREA_CODES = {
    '120000': '天津市',     # 海河干流
    # 以下为海河流域涉及省市，待API支持后启用：
    # '110000': '北京市',     # 永定河
    # '130000': '河北省',     # 大清河/子牙河/漳卫南运河等
    # '140000': '山西省',     # 上游
    # '150000': '内蒙古自治区', # 滦河上游
    # '370000': '山东省',     # 漳卫南运河下游
    # '410000': '河南省',     # 漳卫南运河上游
}
```

同时在 `DefaultConfig` 中新增默认区划码：

```python
# 在 DefaultConfig 类中新增：
HAIHE_DEFAULT_AREA_CODES = '120000'  # 默认天津，待API支持海河流域后扩展
```

- [ ] **Step 2: Commit**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git add "forecast_evaluate 2/forecast_evaluate/scripts/config.py"
git commit -m "feat(config): reserve Haihe basin area codes for future expansion"
```

---

### Task 6: 编写测试

**Files:**
- Create: `haiheliuyubaoyuagent-master/chainlitexam/tests/test_forecast_evaluate_fast_path.py`

- [ ] **Step 1: 编写 fast path 触发关键字测试**

```python
# chainlitexam/tests/test_forecast_evaluate_fast_path.py
"""Test forecast evaluate fast path keyword detection and static contract compliance."""

import sys
from pathlib import Path

# 确保能从 chainlitexam 目录运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from message_orchestrator import _need_forecast_evaluate


class TestForecastEvaluateKeywordDetection:
    """测试 _need_forecast_evaluate 关键词检测"""

    TRIGGER_QUERIES = [
        "最近一次暴雨的TS评分是多少",
        "哪个模式的晴雨预报最准",
        "各家模式的暴雨TS评分对比",
        "大模型预报效果如何",
        "有没有模式误差分析",
        "哪个模式对暴雨落区预报最好",
        "最近的预报准确率对比",
        "BIAS偏差怎么样",
        "MAE误差分析",
        "天津预报和ECMWF的TS评分",
    ]

    NON_TRIGGER_QUERIES = [
        "明天会下雨吗",
        "天津今天天气怎么样",
        "大清河流域未来三天天气",
        "当前降雨情况如何",
        "暴雨影响哪些河流",
    ]

    @pytest.mark.parametrize("query", TRIGGER_QUERIES)
    def test_trigger_keywords(self, query):
        assert _need_forecast_evaluate(query), f"Should trigger: {query}"

    @pytest.mark.parametrize("query", NON_TRIGGER_QUERIES)
    def test_no_false_trigger(self, query):
        assert not _need_forecast_evaluate(query), f"Should NOT trigger: {query}"
```

- [ ] **Step 2: 运行新测试**

Run: `D:\PythonProject\.venv-haihe-tests\Scripts\python.exe -m pytest chainlitexam/tests/test_forecast_evaluate_fast_path.py -v`

Expected: All tests PASS

- [ ] **Step 3: 运行 fast path 静态检查**

Run: `D:\PythonProject\.venv-haihe-tests\Scripts\python.exe chainlitexam/tests/test_fast_paths.py`

Expected: `_try_forecast_evaluate_fast_path` 出现在结果中且状态为 PASS

- [ ] **Step 4: Commit**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git add haiheliuyubaoyuagent-master/chainlitexam/tests/test_forecast_evaluate_fast_path.py
git commit -m "test: add forecast evaluate keyword detection & fast path compliance tests"
```

---

### Task 7: 集成验证

**Files:** None (手动验证)

- [ ] **Step 1: 启动 MCP 服务端**

在单独终端中启动：
```bash
cd D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\haihe-weather-analyzer-mcp
python server.py
```

验证点：日志中出现 `evaluate_forecast` 工具注册，服务无报错启动。

- [ ] **Step 2: 启动 Chainlit 前端**

在单独终端中启动：
```bash
cd D:\PythonProject\haiheliuyubaoyuagent-master\haiheliuyubaoyuagent-master\chainlitexam
ENABLE_FAST_PATHS=true chainlit run chain_gzt.py
```

- [ ] **Step 3: 六个典型问题手工测试**

| # | 问题 | 预期 |
|---|------|------|
| 1 | "最近一次暴雨的TS评分是多少？" | 返回暴雨TS评分排名表格 |
| 2 | "哪个模式的晴雨预报最准？" | 返回晴雨准确率排名 |
| 3 | "各家模式的暴雨TS评分对比" | 返回分地区TS评分表格 |
| 4 | "大模型预报效果如何？" | 返回各产品预报评分对比 |
| 5 | "有没有模式误差分析？" | 返回 MAE/ME 排名 |
| 6 | "哪个模式对暴雨落区预报最好？" | 返回分地区 TS/准确率 |

- [ ] **Step 4: 运行全量测试**

Run: `D:\PythonProject\.venv-haihe-tests\Scripts\python.exe -m pytest chainlitexam/tests/ -v`

Expected: 所有已有测试保持 PASS，无回归。

- [ ] **Step 5: 最终 Commit**

```bash
cd "D:/PythonProject/haiheliuyubaoyuagent-master"
git add -A
git commit -m "feat: integrate forecast evaluate into QA agent (MCP tool + fast path + prompts)"
```
