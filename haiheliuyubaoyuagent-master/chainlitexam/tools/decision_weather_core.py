"""Shared core logic for decision weather point-of-interest queries.

This module holds the helpers and LLM prompts used by both the planner-only
`query_decision_weather_for_poi` tool and the legacy `DecisionWeatherQAService`
fast path. Keeping them in one place avoids duplicated prompts and slot
normalization rules.
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

DECISION_WEATHER_ALLOWED_INTERVALS = [1, 3, 6, 12, 24]


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


def _select_decision_fcst_time(now: datetime | None = None) -> str:
    """选择距离当前时间最近的滚动预报起报时间（08 或 20 时）。"""
    now = now or datetime.now()
    if now.hour >= 8:
        return now.strftime("%Y%m%d080000")
    return (now - timedelta(days=1)).strftime("%Y%m%d200000")


def _normalize_decision_interval(value: Any) -> int:
    """将时间步长归一化为允许的滚动预报步长。"""
    try:
        interval = int(value)
    except Exception:
        interval = 12
    if interval in DECISION_WEATHER_ALLOWED_INTERVALS:
        return interval
    return min(DECISION_WEATHER_ALLOWED_INTERVALS, key=lambda x: abs(x - interval))


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


def _decision_period_args(fcst_time: str, target_start: datetime, target_end: datetime) -> tuple[int, int]:
    """将目标时段转换为滚动预报的 start_period / end_period 参数。"""
    fcst_dt = datetime.strptime(fcst_time, "%Y%m%d%H%M%S")
    start_hours = (target_start - fcst_dt).total_seconds() / 3600
    end_hours = (target_end - fcst_dt).total_seconds() / 3600
    start_period = max(0, int(math.floor(start_hours)))
    end_period = max(start_period + 1, int(math.ceil(end_hours)))
    return start_period, end_period


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
        "街道", "乡", "区", "县", "市", "省",
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


def _compact_decision_forecast_facts(forecast_payload: dict, target_start: datetime, target_end: datetime) -> dict:
    """压缩滚动预报结果为业务事实字典。"""
    periods = forecast_payload.get("periods") if isinstance(forecast_payload, dict) else []
    if not isinstance(periods, list):
        periods = []
    selected = [p for p in periods if isinstance(p, dict) and _decision_period_overlaps(p, target_start, target_end)]
    if not selected:
        selected = [p for p in periods if isinstance(p, dict)][:8]

    compact_periods = []
    total_rain = 0.0
    has_rain = False
    for p in selected[:12]:
        rain = p.get("TP1H")
        try:
            rain_value = float(rain)
        except Exception:
            rain_value = None
        if rain_value is not None:
            total_rain += rain_value
            if rain_value > 0.1:
                has_rain = True
        compact_periods.append({
            "region": p.get("region"),
            "start_time": p.get("start_time"),
            "end_time": p.get("end_time"),
            "weather": p.get("WEA"),
            "tmax": p.get("TMAX"),
            "tmin": p.get("TMIN"),
            "wind": p.get("EDA"),
            "visibility_min": p.get("VISMIN"),
            "rain_1h": rain,
        })

    return {
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


def _ainvoke_chain(callbacks: dict) -> Any:
    """从 callbacks 中取出 LLM 调用函数。"""
    fn = callbacks.get("ainvoke_chain")
    if not fn:
        raise RuntimeError("callbacks 中缺少 ainvoke_chain")
    return fn


async def _extract_decision_weather_slots(user_text: str, answer_chain: Any, callbacks: dict) -> dict:
    """使用 LLM 从用户问题中抽取点位决策天气所需槽位。"""
    now = datetime.now()
    prompt = (
        "你是天津气象决策服务问答的结构化抽取器。请判断用户问题是否属于"
        "“具体地点/单位/场馆/学校/医院/设施附近的未来或当前天气决策服务”。\n"
        "普通区域预报（如天津、全市、西青、滨海新区、未来一周天气）不属于本类，返回 is_decision_weather=false。\n"
        "如果属于本类，请抽取位置名称、目标开始时间、目标结束时间、时间步长和问题类型。\n"
        "当前时间为："
        f"{now.strftime('%Y-%m-%d %H:%M:%S')}。\n"
        "时间规则：没有明确时间时，target_start_time 默认为当前时间，target_end_time 默认为当前时间后24小时，interval_hours=12；"
        "明日/明天为明天00:00到后天00:00，interval_hours=24；"
        "未来N小时为当前时间到N小时后，interval_hours优先取N，若N不在1/3/6/12/24中则选最接近值；"
        "高考期间、国庆期间等公共事件可给出具体时间，但必须标明 time_basis=公共常识、time_confidence=medium；"
        "展会期间、会议期间、考试期间等没有明确日期的事件必须 need_clarification=true。\n"
        "滚动预报只适合未来时段；若目标时段已经过去或无法确定，need_clarification=true。\n"
        "只返回 JSON，不要输出解释。格式：\n"
        "{\n"
        '  "is_decision_weather": true,\n'
        '  "location_name": "梅江会展中心",\n'
        '  "target_start_time": "YYYY-MM-DD HH:MM:SS",\n'
        '  "target_end_time": "YYYY-MM-DD HH:MM:SS",\n'
        '  "interval_hours": 12,\n'
        '  "question_type": "general_weather|rain_now|rain_next_hours|event_weather|visibility|temperature|wind|activity",\n'
        '  "time_basis": "用户明示|相对时间|公共常识|无法确定",\n'
        '  "time_confidence": "high|medium|low",\n'
        '  "need_clarification": false,\n'
        '  "clarification_question": ""\n'
        "}\n\n"
        f"用户问题：{user_text}"
    )
    result = await _ainvoke_chain(callbacks)(answer_chain, {"messages": [HumanMessage(content=prompt)]})
    content = getattr(result, "content", None) or str(result)
    return _extract_first_json_object(content)


def _normalize_decision_weather_slots(slots: dict) -> dict:
    """校验并规范化 LLM 抽取的槽位。"""
    location_name = str(slots.get("location_name") or "").strip()
    if not location_name:
        return {"error": "请补充要查询天气的位置名称，例如学校、场馆、医院或具体单位。"}

    now = datetime.now()
    target_start = _parse_decision_dt(slots.get("target_start_time")) or now
    target_end = _parse_decision_dt(slots.get("target_end_time")) or (target_start + timedelta(hours=24))
    if target_end <= target_start:
        return {"error": "请确认查询的结束时间需要晚于开始时间。"}

    if target_end <= now:
        return {"error": "该时段已经过去，滚动预报仅支持未来天气查询；如需历史天气，需要改用历史实况或历史预报数据。"}

    max_end = now + timedelta(hours=240)
    if target_start > max_end:
        return {"error": "当前滚动预报最多支持未来约10天，请缩短或调整查询时段。"}
    if target_end > max_end:
        target_end = max_end

    return {
        "location_name": location_name,
        "target_start": target_start,
        "target_end": target_end,
        "interval": _normalize_decision_interval(slots.get("interval_hours")),
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
        "数据来源": facts.get("data_source") or "天津市气象台滚动预报",
    }
    prompt = (
        "请仅依据下面 JSON 中的业务天气事实回答用户问题。不要编造未返回的天气、雨量、温度、风力或能见度。\n"
        "严禁输出点位定位过程、经纬度、代表点、工具名、接口名、URL、参数名、query_mode、fcst_time、startPeriod、endPeriod、interval 等技术信息。\n"
        "回答统一采用业务口径：\n"
        "1. 必须先输出【核心结论】，用一句话直接回答天气是否良好、是否有降雨、是否有灾害性天气或是否适合活动。\n"
        "2. 综合天气/活动/考试/会展/节假日类：第二模块用【XX逐日预报】或【XX明日预报】，表格列为：日期｜天气现象｜气温(℃)｜风力（级）｜风向。\n"
        "3. 未来N小时是否下雨类：第二模块用【XX逐小时预报】，表格列为：时段｜天气现象｜气温(℃)｜风力（级）｜风向。\n"
        "4. 当前是否下雨类：核心结论写“当前无降雨/当前正在降雨”；第二模块用【降雨情况】，列出已返回的累计雨量或时段降水，缺失的1小时/3小时/6小时雨量不要编造。\n"
        "5. 风况字段中若同时包含风向和风力，请拆成“风力（级）”和“风向”；无法拆分时可在对应列写原始风况中的可识别部分。\n"
        "6. 末尾只写：数据来源：天津市气象台滚动预报。\n\n"
        f"用户问题：{user_text}\n\n"
        f"业务天气事实 JSON：{json.dumps(business_facts, ensure_ascii=False, default=str)}"
    )
    result = await _ainvoke_chain(callbacks)(answer_chain, {"messages": [HumanMessage(content=prompt)]})
    return getattr(result, "content", None) or str(result)
