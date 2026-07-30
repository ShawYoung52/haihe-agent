"""Shared core logic for decision weather point-of-interest queries.

This module holds POI recognition, distance calculation, forecast-result
consumption, and answer prompts shared by the planner-only
`query_decision_weather_for_poi` tool and `decision_weather_fast_path`.
Rolling-forecast region, timezone, issue time, and query-window rules belong
exclusively to ``rolling_forecast_service.py``.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from typing import Any

from langchain_core.messages import HumanMessage

DECISION_WEATHER_STATIONS = [
    {"region": "天津市区", "lon": 117.14, "lat": 39.24},
    {"region": "蓟州", "lon": 117.45, "lat": 40.05},
    {"region": "宝坻", "lon": 117.28, "lat": 39.73},
    {"region": "武清", "lon": 117.06, "lat": 39.43},
    {"region": "宁河", "lon": 117.85, "lat": 39.38},
    {"region": "静海", "lon": 116.92, "lat": 38.93},
    {"region": "北辰", "lon": 117.21, "lat": 39.07},
    {"region": "西青", "lon": 117.05, "lat": 39.08},
    {"region": "津南", "lon": 117.42, "lat": 38.95},
    {"region": "东丽", "lon": 117.34, "lat": 39.08},
    {"region": "滨海新区", "lon": 117.79, "lat": 39.16},
]

DECISION_WEATHER_COMPOSITE_TOOL = "query_decision_weather_for_poi"
DECISION_WEATHER_INTERNAL_TOOLS = {
    "search_poi",
    "search_poi_by_distance",
    "query_rolling_forecast",
    "get_server_time",
    "analyze_rainfall_by_time",
    "local_analyze_rainfall_by_time",
}


def filter_redundant_decision_weather_calls(tool_calls: list[Any]) -> list[Any]:
    """当 Planner 已选择点位决策天气组合工具时，移除其内部已覆盖的重复调用。"""
    calls = list(tool_calls or [])

    def tool_name(call: Any) -> str:
        if isinstance(call, dict):
            return str(call.get("name") or "")
        return str(getattr(call, "name", "") or "")

    if not any(tool_name(call) == DECISION_WEATHER_COMPOSITE_TOOL for call in calls):
        return calls

    # 点位天气组合工具已经完整封装 POI 定位、滚动预报调用和回答生成；
    # 时区、起报时次与查询窗口由滚动预报服务在内部统一计算。
    # 同轮出现它时只保留第一次组合工具调用，避免短临、时间或其它天气工具争抢结果。
    return [
        next(
            call for call in calls
            if tool_name(call) == DECISION_WEATHER_COMPOSITE_TOOL
        )
    ]

def _extract_first_json_object(text: str) -> dict:
    """从文本中提取第一个 JSON 对象，支持 Markdown 代码块包裹。"""
    if not isinstance(text, str):
        return {}
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start:end + 1])
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _parse_decision_dt(value: Any) -> datetime | None:
    """解析决策天气使用的日期时间字符串。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    return None


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """计算两点间大地线距离（千米）。"""
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def _nearest_decision_station(lon: float, lat: float) -> dict:
    """返回距离指定经纬度最近的滚动预报代表站。"""
    nearest = min(
        DECISION_WEATHER_STATIONS,
        key=lambda station: _haversine_km(lon, lat, station["lon"], station["lat"]),
    )
    distance_km = _haversine_km(lon, lat, nearest["lon"], nearest["lat"])
    return {**nearest, "distance_km": distance_km}


def _decision_weather_prefilter(user_text: str) -> bool:
    """前置过滤：判断问题是否可能属于具体点位决策天气查询。"""
    t = user_text or ""
    weather_keywords = [
        "天气", "下雨", "有雨", "降雨", "降水", "气温", "温度", "风", "能见度",
        "雾", "霾", "预报", "暴雨", "雷阵雨", "适合", "户外",
    ]
    if not any(k in t for k in weather_keywords):
        return False
    location_indicators = ["在", "去", "到", "位于", "附近", "周边", "旁边", "距", "距离"]
    institution_suffixes = [
        "学校", "大学", "学院", "医院", "场馆", "中心", "公园", "酒店", "大厦",
        "广场", "机场", "车站", "码头", "景区", "园区", "小区", "村", "镇",
        "街道", "乡",
    ]
    has_indicator = any(k in t for k in location_indicators)
    has_institution = any(s in t for s in institution_suffixes)
    time_blocklist = ["周末", "周六", "周日", "今天", "今日", "明天", "后天", "未来一周", "本周"]
    if any(k in t for k in time_blocklist) and not (has_indicator or has_institution):
        return False
    return has_indicator or has_institution


def _decision_pick_first_poi(poi_payload: dict) -> dict | None:
    """从 POI 检索结果中挑选第一个带有效经纬度的条目。"""
    pois = poi_payload.get("pois") if isinstance(poi_payload, dict) else None
    if not isinstance(pois, list):
        return None
    for poi in pois:
        if not isinstance(poi, dict):
            continue
        lon = poi.get("longitude")
        lat = poi.get("latitude")
        if lon is None or lat is None:
            location = poi.get("location")
            if isinstance(location, dict):
                lon = lon if lon is not None else location.get("lon")
                lat = lat if lat is not None else location.get("lat")
        try:
            return {**poi, "longitude": float(lon), "latitude": float(lat)}
        except Exception:
            continue
    return None


def _decision_period_overlaps(period: dict, start_dt: datetime, end_dt: datetime) -> bool:
    """判断单个预报时段是否与目标时段存在重叠。"""
    p_start = _parse_decision_dt(period.get("start_time"))
    p_end = _parse_decision_dt(period.get("end_time"))
    if not p_start or not p_end:
        return True
    return p_start < end_dt and p_end > start_dt


def _compact_decision_period(period: dict) -> dict:
    return {
        "region": period.get("region"),
        "start_time": period.get("start_time"),
        "end_time": period.get("end_time"),
        "weather": period.get("WEA"),
        "tmax": period.get("TMAX"),
        "tmin": period.get("TMIN"),
        "wind": period.get("EDA"),
        "visibility_min": period.get("VISMIN"),
        "rain_1h": period.get("TP1H"),
    }


def _sum_decision_rain(periods: list[dict], start_dt: datetime, end_dt: datetime) -> tuple[float | None, int]:
    values: list[float] = []
    for p in periods:
        p_start = _parse_decision_dt(p.get("start_time"))
        p_end = _parse_decision_dt(p.get("end_time"))
        if not p_start or not p_end:
            continue
        if p_start >= start_dt and p_end <= end_dt:
            rain = _decision_rain_value(p)
            if rain is not None:
                values.append(rain)
    if not values:
        return None, 0
    return round(sum(values), 2), len(values)


def _build_decision_hourly_facts(periods: list[dict], hourly_request: dict | None) -> dict | None:
    if not hourly_request:
        return None
    mode = hourly_request.get("mode")
    if mode == "rain_now":
        cutoff = hourly_request["cutoff_time"]
        hourly_rain: dict[str, Any] = {}
        for hours in (1, 3, 6):
            value, _count = _sum_decision_rain(periods, cutoff - timedelta(hours=hours), cutoff)
            hourly_rain[f"rain_{hours}h_mm"] = value
            hourly_rain[f"rain_{hours}h_text"] = _decision_rain_text(value)
        return {
            "mode": "rain_now",
            "cutoff_time": cutoff.strftime("%Y-%m-%d %H:%M:%S"),
            "cutoff_label": cutoff.strftime("%m月%d日%H时"),
            **hourly_rain,
            "is_raining_now": (
                hourly_rain["rain_1h_mm"] is not None
                and hourly_rain["rain_1h_mm"] > 0.1
            ),
        }

    if mode == "rain_next_hours":
        target_start = hourly_request["target_start"]
        target_end = hourly_request["target_end"]
        selected = []
        rain_values: list[float] = []
        for p in periods:
            p_start = _parse_decision_dt(p.get("start_time"))
            p_end = _parse_decision_dt(p.get("end_time"))
            if p_start and p_end and p_start >= target_start and p_end <= target_end:
                selected.append(p)
                if (rain := _decision_rain_value(p)) is not None:
                    rain_values.append(rain)
        total = round(sum(rain_values), 2) if rain_values else None
        return {
            "mode": "rain_next_hours",
            "hours": int(hourly_request.get("hours") or 3),
            "target_start_time": target_start.strftime("%Y-%m-%d %H:%M:%S"),
            "target_end_time": target_end.strftime("%Y-%m-%d %H:%M:%S"),
            "total_rain_mm": total,
            "total_rain_text": _decision_rain_text(total),
            "rain_level": _decision_future_rain_level(total),
            "hourly_periods": [_compact_decision_period(p) for p in selected],
        }
    return None


def _select_decision_periods(
    periods: list[dict],
    target_start: datetime,
    target_end: datetime,
    hourly_request: dict | None,
    hourly_facts: dict | None,
) -> list[dict]:
    if hourly_facts and hourly_facts.get("mode") == "rain_now":
        cutoff = hourly_request["cutoff_time"]
        return [p for p in periods if _parse_decision_dt(p.get("end_time")) == cutoff]

    if hourly_facts and hourly_facts.get("mode") == "rain_next_hours":
        start = hourly_request["target_start"]
        end = hourly_request["target_end"]
        return [
            p for p in periods
            if (p_start := _parse_decision_dt(p.get("start_time")))
            and (p_end := _parse_decision_dt(p.get("end_time")))
            and p_start >= start
            and p_end <= end
        ]

    selected = [p for p in periods if _decision_period_overlaps(p, target_start, target_end)]
    return selected or periods[:8]


def _compact_decision_forecast_facts(
    forecast_payload: dict,
    target_start: datetime | None = None,
    target_end: datetime | None = None,
    hourly_request: dict | None = None,
) -> dict:
    """将滚动预报 payload 压缩为确定性业务事实。

    target_start/target_end 为 None 时，从 forecast_payload 自身推导默认窗口。
    """
    periods = forecast_payload.get("periods") if isinstance(forecast_payload, dict) else []
    if not isinstance(periods, list):
        periods = []
    periods = [p for p in periods if isinstance(p, dict)]

    if target_start is None:
        fcst_time_str = forecast_payload.get("fcst_time")
        target_start = _parse_decision_dt(fcst_time_str) or datetime.now()
    if target_end is None:
        target_end = target_start + timedelta(hours=24)

    hourly_facts = _build_decision_hourly_facts(periods, hourly_request)
    selected = _select_decision_periods(periods, target_start, target_end, hourly_request, hourly_facts)

    compact_periods = []
    total_rain = 0.0
    has_rain = False
    for p in selected[:12]:
        if (rain_value := _decision_rain_value(p)) is not None:
            total_rain += rain_value
            if rain_value > 0.1:
                has_rain = True
        compact_periods.append(_compact_decision_period(p))

    facts = {
        "data_source": forecast_payload.get("data_source"),
        "query_mode": forecast_payload.get("query_mode"),
        "fcst_time": forecast_payload.get("fcst_time"),
        "interval_hours": forecast_payload.get("interval_hours"),
        "target_start_time": target_start.strftime("%Y-%m-%d %H:%M:%S"),
        "target_end_time": target_end.strftime("%Y-%m-%d %H:%M:%S"),
        "has_rain_signal": has_rain,
        "total_rain_mm": round(total_rain, 2),
        "periods": compact_periods,
    }
    if hourly_facts:
        facts["hourly_rain"] = hourly_facts
    return facts


def _decision_rain_value(period: dict) -> float | None:
    try:
        value = period.get("rainfall_mm")
        if value is None:
            value = period.get("rain_1h")
        if value is None:
            value = period.get("TP1H")
        return float(value)
    except Exception:
        return None


def _decision_rain_text(value: float | None) -> str:
    if value is None:
        return "暂无数据"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _decision_future_rain_level(total_rain: float | None) -> str:
    if total_rain is None:
        return "暂无足够逐小时降水数据"
    if total_rain <= 0.1:
        return "无降雨"
    if total_rain < 3:
        return "有小雨，雨量<3毫米"
    if total_rain < 10:
        return "有降雨"
    return "有明显降雨"


def _ainvoke_chain(callbacks: dict) -> Any:
    """从 callbacks 中取出 LLM 调用函数。"""
    fn = callbacks.get("ainvoke_chain")
    if not fn:
        raise RuntimeError("callbacks 中缺少 ainvoke_chain")
    return fn


async def _extract_decision_weather_slots(user_text: str, answer_chain: Any, callbacks: dict) -> dict:
    """使用 LLM 只识别点位和问题类型，不在本层计算任何预报时间参数。"""
    prompt = (
        "你是天津气象决策服务问答的结构化抽取器。请判断用户问题是否属于"
        "“具体地点/单位/场馆/学校/医院/设施附近的未来或当前天气决策服务”。\n"
        "普通区域预报（如天津、全市、西青、滨海新区、未来一周天气）不属于本类，返回 is_decision_weather=false。\n"
        "如果属于本类，只抽取位置名称和问题类型；不得计算、推断或输出当前时间、起报时间、"
        "目标开始时间、目标结束时间、时间步长或时效参数，这些全部由滚动预报服务依据用户原问处理。\n"
        "只有位置名称缺失或无法确定时才设置 need_clarification=true；不要因为相对时间或活动日期而在本层计算时间。\n"
        "只返回 JSON，不要输出解释。格式：\n"
        "{\n"
        '  "is_decision_weather": true,\n'
        '  "location_name": "梅江会展中心",\n'
        '  "question_type": "general_weather|rain_now|rain_next_hours|event_weather|visibility|temperature|wind|activity",\n'
        '  "need_clarification": false,\n'
        '  "clarification_question": ""\n'
        "}\n\n"
        f"用户问题：{user_text}"
    )
    result = await _ainvoke_chain(callbacks)(answer_chain, {"messages": [HumanMessage(content=prompt)]})
    content = getattr(result, "content", None) or str(result)
    return _extract_first_json_object(content)


def _normalize_decision_weather_slots(slots: dict) -> dict:
    """只校验点位识别结果；预报时间和窗口由滚动预报服务统一处理。"""
    location_name = str(slots.get("location_name") or "").strip()
    if not location_name:
        return {"error": "请补充要查询天气的位置名称，例如学校、场馆、医院或具体单位。"}
    return {
        "location_name": location_name,
        "question_type": str(slots.get("question_type") or "general_weather"),
    }


async def _generate_decision_weather_answer(user_text: str, facts: dict, answer_chain: Any, callbacks: dict) -> str:
    """基于业务天气事实生成面向用户的自然语言回答。"""
    business_facts = {
        "位置名称": (facts.get("poi") or {}).get("name") or "该位置",
        "位置地址": (facts.get("poi") or {}).get("address") or "",
        "查询开始时间": facts.get("target_start_time"),
        "查询结束时间": facts.get("target_end_time"),
        "问题类型": facts.get("question_type"),
        "是否有降雨信号": facts.get("has_rain_signal"),
        "累计降水量毫米": facts.get("total_rain_mm"),
        "预报时段": facts.get("periods") or [],
        "小时级降雨计算": facts.get("hourly_rain"),
        "数据来源": facts.get("data_source") or "天津市气象台滚动预报",
    }
    prompt = (
        "请仅依据下面 JSON 中的业务天气事实回答用户问题。不要编造未返回的天气、雨量、温度、风力或能见度。\n"
        "严禁输出点位定位过程、经纬度、代表点、工具名、接口名、URL、参数名、query_mode、fcst_time、startPeriod、endPeriod、interval 等技术信息。\n"
        "回答统一采用业务口径：\n"
        "1. 必须先输出【核心结论】，用一句话直接回答用户问题的要点，只围绕用户明确询问的降雨、天气、气温、风力、能见度或活动适宜性作答，不主动扩展无关风险、背景或建议。\n"
        "2. 综合天气/活动/考试/会展/节假日类：第二模块用【XX逐日预报】或【XX明日预报】，表格列为：日期｜天气现象｜气温(℃)｜风力（级）｜风向；日期必须写清楚完整月日和时间。\n"
        "3. 未来N小时是否下雨类：只输出【核心结论】和【逐小时预报】；核心结论用代码给出的 rain_level 和 total_rain_text 判断无降雨/有小雨/有明显降雨，逐小时预报只列出 JSON 中 hourly_periods 的时段；表格时段必须写成完整日期和时间，格式如 7月8日14时-7月8日15时，不要只写 14时-15时。\n"
        "4. 当前是否下雨类：只输出【核心结论】和【当前小时预报】；只能依据服务返回的当前整点至下一整点预报判断，不得表述为降雨实况，也不得编造过去1/3/6小时累计雨量。\n"
        "5. 风况字段中若同时包含风向和风力，请拆成“风力（级）”和“风向”；无法拆分时可在对应列写原始风况中的可识别部分。\n"
        "6. 末尾只写：数据来源：天津市气象台滚动预报。\n\n"
        f"用户问题：{user_text}\n\n"
        f"业务天气事实 JSON：{json.dumps(business_facts, ensure_ascii=False, default=str)}"
    )
    result = await _ainvoke_chain(callbacks)(answer_chain, {"messages": [HumanMessage(content=prompt)]})
    answer = getattr(result, "content", None) or str(result)
    pattern = re.compile(r"(【核心结论】)\s*(.*?)(?=\n\s*【[^】]+】|\Z)", re.DOTALL)

    def keep_first_sentence(match: re.Match) -> str:
        core = re.sub(r"\s+", " ", match.group(2)).strip()
        sentence = re.match(r"^.*?[。！？!?](?:[”’」』])?", core)
        return f"{match.group(1)}\n{sentence.group(0).strip() if sentence else core}".rstrip()

    return pattern.sub(keep_first_sentence, str(answer), count=1)

