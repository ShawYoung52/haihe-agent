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
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from langchain_core.messages import HumanMessage

from tools.rolling_forecast_response import sanitize_forecast_core_summary

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
    "query_poi_hazard_reminders",
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
        "学校", "中学", "小学", "初中", "高中", "大学", "学院", "幼儿园",
        "医院", "场馆", "中心", "公园", "酒店", "大厦",
        "广场", "机场", "车站", "站", "码头", "景区", "名胜区", "园区", "小区", "村", "镇",
        "街道", "乡", "中",
    ]
    has_indicator = any(k in t for k in location_indicators)
    has_institution = any(s in t for s in institution_suffixes)
    time_blocklist = ["周末", "周六", "周日", "今天", "今日", "明天", "后天", "未来一周", "本周"]
    if any(k in t for k in time_blocklist) and not (has_indicator or has_institution):
        return False
    return has_indicator or has_institution


_DECISION_WEATHER_SUFFIXES = [
    "会展中心", "中心", "大学", "学院", "中学", "小学", "初中", "高中", "学校", "幼儿园",
    "医院", "公园", "酒店", "大厦", "广场", "机场", "车站", "码头", "景区", "园区", "小区",
]
_DECISION_RAIN_WORDS = ["下雨", "有雨", "降雨", "降水", "暴雨", "雷阵雨", "雨"]


def _extract_decision_slots_rule_based(user_text: str) -> dict | None:
    """纯规则抽取点位决策天气槽位；无法可靠抽取时返回 None（调用方回退 LLM）。"""
    t = (user_text or "").strip()
    if not t:
        return None

    # 1) 位置名：匹配机构后缀前的最长名词短语
    location = None
    for suffix in sorted(_DECISION_WEATHER_SUFFIXES, key=len, reverse=True):
        idx = t.find(suffix)
        if idx < 0:
            continue
        # 取后缀前的一段（跳过标点/介词/时间词）
        head = t[:idx]
        head = re.split(r"[，。？?！!、\s，]|今天|明天|后天|周末|未来|现在|上午|下午|晚上|夜里|[一二三四五六七八九十\d]+(小时|天|日|周)", head)[-1]
        # 去掉结尾的"在/去/到/位于/附近"等
        head = re.sub(r"(在|去|到|位于|附近|周边|旁边|距|距离)$", "", head)
        candidate = (head + suffix).strip()
        if candidate and len(candidate) >= 2 and head:
            location = candidate
            break

    if not location:
        return None

    # 2) 问题类型
    qtype = "general_weather"
    if any(w in t for w in ["适合", "活动", "户外", "露营", "出行"]):
        qtype = "activity"
    elif any(w in t for w in ["未来", "小时", "接下来"]):
        qtype = "rain_next_hours"
    elif any(w in t for w in _DECISION_RAIN_WORDS):
        qtype = "rain_now"
    elif any(w in t for w in ["能见度", "雾", "霾"]):
        qtype = "visibility"
    elif any(w in t for w in ["气温", "温度", "热", "冷"]):
        qtype = "temperature"
    elif any(w in t for w in ["风"]):
        qtype = "wind"

    return {
        "is_decision_weather": True,
        "location_name": location,
        "question_type": qtype,
        "need_clarification": False,
        "clarification_question": "",
    }


# POI 区域偏好：与 poi_nearest_observation_tool._pick_first_poi 同口径。
# 决策天气面向天津主场（天津市气象台），同名大众点（河西中心/实验中学等）必须优先天津，
# 否则检索结果可能把外省同名点当成目标点位。
# 显式区域先匹城市名再匹省名（“河北省石家庄市实验中学”须取“石家庄”而非“河北”，
# 避免命中天津河北区的“河北”子串）；海河流域地级市/省会全覆盖，防“石家庄实验中学”被主场天津抢占。
_POI_EXPLICIT_CITY_WORDS = (
    "石家庄", "唐山", "丰润", "保定", "廊坊", "沧州", "秦皇岛", "邯郸", "邢台",
    "承德", "张家口", "衡水", "太原", "大同", "朔州", "忻州", "阳泉",
    "呼和浩特", "包头", "赤峰", "集宁", "济南", "青岛", "德州", "聊城",
    "滨州", "东营", "郑州", "安阳", "新乡", "鹤壁", "焦作", "濮阳",
)
_POI_EXPLICIT_PROVINCE_WORDS = ("北京", "天津", "河北", "山西", "内蒙古", "山东", "河南", "辽宁")
# 显式区域词合并常量：城市在前（“河北省石家庄市实验中学”须命中“石家庄”而非“河北”），省份在后
_POI_EXPLICIT_REGION_WORDS = _POI_EXPLICIT_CITY_WORDS + _POI_EXPLICIT_PROVINCE_WORDS
# 海河流域省市词（默认主场外，其次优先流域内点位）
_POI_HAIHE_REGION_WORDS = ("北京", "天津", "河北", "山西", "内蒙古", "山东", "河南")
_TIANJIN_DISTRICTS = (
    "和平区", "河东区", "河西区", "南开区", "河北区", "红桥区", "东丽区", "西青区",
    "津南区", "北辰区", "武清区", "宝坻区", "滨海新区", "宁河区", "静海区", "蓟州区",
)
# 外省证据词（省/直辖市）：天津区县与外地城市同名（和平区-沈阳、河东区-临沂、滨海新区等），
# 出现这些词说明 POI 更可能在外省。河北区/北京路/山西路/山东路/河南路/河北路是天津区县/街道名，
# 用负向断言排除，避免误杀天津本地点位。
_POI_NON_TIANJIN_REGION_RE = re.compile(
    r"河北(?!区|路)|北京(?!路)|山西(?!路)|山东(?!路)|河南(?!路)|"
    r"辽宁|内蒙古|黑龙江|吉林|江苏|安徽|浙江|福建|江西|湖北|湖南|"
    r"广东|广西|海南|四川|贵州|云南|西藏|陕西|甘肃|青海|宁夏|新疆|"
    r"重庆|上海|香港|澳门|台湾"
)


def _decision_poi_text(poi: dict) -> str:
    """POI 的名称/地址/类别拼接文本，用于区域判定。"""
    return " ".join(
        str(poi.get(k) or "") for k in ("name", "address", "category_1", "category_2")
    )


def _decision_is_tianjin_poi(poi: dict) -> bool:
    """POI 是否天津证据充分：名称含“天津”、地址含“天津市”即认定。

    仅凭区县名（和平区/河东区/滨海新区等与外地城市同名）算弱证据，出现明确外省
    省/直辖市词时不再认定天津，避免“山东省临沂市河东区”等外省点冒充天津点。
    """
    text = _decision_poi_text(poi)
    if "天津" in str(poi.get("name") or ""):
        return True
    if "天津市" in text:
        return True
    if not any(district in text for district in _TIANJIN_DISTRICTS):
        return False
    return not bool(_POI_NON_TIANJIN_REGION_RE.search(text))


def _decision_poi_expected_region(keyword: str) -> str:
    """关键词显式指定区域则尊重之，否则默认天津（系统主场）。"""
    for word in _POI_EXPLICIT_REGION_WORDS:
        if word in keyword:
            return word
    return "天津"


def _decision_poi_region_rank(poi: dict, keyword: str) -> int:
    """按期望区域给候选 POI 排序分档（越大越优先）。

    默认（无显式区域）主场天津：天津证据充分 > 海河流域内 > 其它。
    显式指定区域时：命中期望区域 > 天津 > 其它。
    """
    text = _decision_poi_text(poi)
    expected = _decision_poi_expected_region(keyword)
    if expected != "天津":
        if expected in text:
            return 2
        return 1 if _decision_is_tianjin_poi(poi) else 0
    if _decision_is_tianjin_poi(poi):
        return 2
    if any(word in text for word in _POI_HAIHE_REGION_WORDS):
        return 1
    return 0


def _decision_pick_first_poi(poi_payload: dict, keyword: str = "") -> dict | None:
    """从 POI 检索结果中挑选第一个带有效经纬度的条目。

    keyword 提供区域上下文：显式含“天津”时严格过滤（只认天津证据充分的点位，
    宁可查不到也不拿外省同名点冒充）；否则按主场天津偏好排序。
    修复“河西中心/实验中学”等同名点被定位到其它城市的问题。
    """
    pois = poi_payload.get("pois") if isinstance(poi_payload, dict) else None
    if not isinstance(pois, list) or not pois:
        return None
    keyword = str(keyword or "")
    pois = [poi for poi in pois if isinstance(poi, dict)]
    if "天津" in keyword:
        pois = [poi for poi in pois if _decision_is_tianjin_poi(poi)]
    else:
        pois = sorted(
            pois,
            key=lambda poi: _decision_poi_region_rank(poi, keyword),
            reverse=True,
        )
    for poi in pois:
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


# POI 地理类型分类：用于点位天气回答时追加“注意事项”。
# 优先级 school → airport → station → scenic → mountain，先命中者返回。
# 关键词匹配保守优先：mountain 只认复合词，排除“石家庄/唐山/燕山”等单字“山”地名；
# station 只认列表内复合词，不认裸“站”。
POI_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "school": [
        "小学", "中学", "学校", "校区", "学院", "大学", "高中", "初中",
        "职校", "职业院校", "幼儿园", "学前", "培训学校", "子弟校",
    ],
    "airport": ["机场"],
    "station": [
        "火车站", "高铁站", "动车所", "长途汽车站", "客运站", "汽车站",
        "天津站", "天津西站", "天津南站", "天津北站", "滨海站", "塘沽站",
        "车站",
    ],
    "scenic": [
        "风景区", "风景名胜", "景区", "景点", "旅游区", "古镇", "公园",
        "湿地公园", "乐园", "游乐场", "游乐园", "度假区", "博物", "展览馆",
        "博物馆", "纪念馆", "会展", "风情区", "文化街",
    ],
    "mountain": ["山区", "山地", "山间", "山脚", "山腰", "山沟", "山坡", "峡谷", "山洪"],
}
_POI_CATEGORY_ORDER = ("school", "airport", "station", "scenic", "mountain")

# 知名天津景点（名称本身不含景区/公园等类别词，须点名关联景区）。
# 只对 name 匹配，且名称含街道/办事处/政务/社区/派出所/医院/村/县/镇/乡等非景区语义时跳过，
# 防“五大道街道政务中心”“五大道派出所”误判。裸“盘山”既是蓟州名山也是辽宁县名，
# 已从名单移除（保守优先；盘山风景名胜区仍走“风景名胜”关键词）。
_POI_KNOWN_SCENIC_NAMES: tuple[str, ...] = (
    "五大道", "古文化街", "意式风情区", "瓷房子", "天津之眼",
    "石家大院", "黄崖关长城", "独乐寺", "天后宫",
)
_POI_KNOWN_SCENIC_EXCLUDE = (
    "街道", "办事处", "政务", "社区", "派出所", "医院", "村", "县", "镇", "乡",
)


def _decision_is_known_scenic_name(name: str) -> bool:
    name = str(name or "").strip()
    if not name:
        return False
    if any(word in name for word in _POI_KNOWN_SCENIC_EXCLUDE):
        return False
    return any(spot in name for spot in _POI_KNOWN_SCENIC_NAMES)


def classify_poi_category(
    name: str,
    address: str = "",
    category_1: Any = None,
    category_2: Any = None,
) -> str | None:
    """按名称/地址/ES 类别识别 POI 地理类型。

    返回 school|scenic|mountain|airport|station 之一，无法可靠分类时返回 None
    （保守优先，避免把“石家庄/唐山”等普通地名误判成山区）。
    """
    if not name:
        return None
    parts = [str(name), str(address or ""), str(category_1 or ""), str(category_2 or "")]
    text = " ".join(part.strip() for part in parts if part and part.strip())
    if not text:
        return None
    for category in _POI_CATEGORY_ORDER:
        if category == "scenic" and _decision_is_known_scenic_name(name):
            return "scenic"
        if any(kw in text for kw in POI_CATEGORY_KEYWORDS[category]):
            return category
    return None


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


def _decision_temperature_text_value(value: Any) -> str | None:
    """点位天气表与点位结论使用整数温度。"""
    if value is None or str(value).strip() in {"", "—"}:
        return None
    try:
        rounded = Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return str(value).strip()
    return format(rounded, "f")


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


def _compact_decision_period(period: dict) -> dict:
    return {
        "region": period.get("region"),
        "start_time": period.get("start_time"),
        "end_time": period.get("end_time"),
        "period_label": period.get("period_label"),
        "weather": period.get("weather") if period.get("weather") is not None else period.get("WEA"),
        "tmax": _decision_temperature_text_value(
            period.get("tmax") if period.get("tmax") is not None else period.get("TMAX")
        ),
        "tmin": _decision_temperature_text_value(
            period.get("tmin") if period.get("tmin") is not None else period.get("TMIN")
        ),
        "EDA": period.get("EDA") if period.get("EDA") is not None else period.get("wind"),
        "wind": period.get("wind") if period.get("wind") is not None else period.get("EDA"),
        "visibility_min_km": (
            period.get("visibility_min_km")
            if period.get("visibility_min_km") is not None
            else (
                period.get("visibility_min_m")
                if period.get("visibility_min_m") is not None
                else period.get("VISMIN")
            )
        ),
        "visibility_unit": "千米",
        "rain_1h": (
            period.get("rainfall_mm")
            if period.get("rainfall_mm") is not None
            else period.get("TP1H")
        ),
    }


def _build_decision_hourly_facts(forecast_payload: dict, hourly: list[dict]) -> dict | None:
    """只消费滚动预报服务已经确定的小时窗口和 hourly_summary。"""
    if not hourly:
        return None
    rain_values = [value for item in hourly if (value := _decision_rain_value(item)) is not None]
    total = round(sum(rain_values), 2) if rain_values else None
    start_text = forecast_payload.get("forecast_start_time")
    end_text = forecast_payload.get("forecast_end_time")
    start_dt = _parse_decision_dt(start_text)
    end_dt = _parse_decision_dt(end_text)
    hours = int((end_dt - start_dt).total_seconds() // 3600) if start_dt and end_dt else len(hourly)
    query_mode = str(forecast_payload.get("query_mode") or "")
    if query_mode.startswith("current_hour"):
        return {
            "mode": "rain_current_hour",
            "target_start_time": start_text,
            "target_end_time": end_text,
            "total_rain_mm": total,
            "total_rain_text": _decision_rain_text(total),
            "rain_level": _decision_future_rain_level(total),
            "is_raining_now": total is not None and total > 0.1,
            "hourly_periods": [_compact_decision_period(item) for item in hourly],
        }
    return {
        "mode": "rain_next_hours",
        "hours": hours,
        "target_start_time": start_text,
        "target_end_time": end_text,
        "total_rain_mm": total,
        "total_rain_text": _decision_rain_text(total),
        "rain_level": _decision_future_rain_level(total),
        "hourly_periods": [_compact_decision_period(item) for item in hourly],
    }


def _compact_decision_forecast_facts(
    forecast_payload: dict,
) -> dict:
    """消费滚动预报服务结果，不在点位层重新计算地区、起报时次或查询窗口。"""
    periods = forecast_payload.get("periods") if isinstance(forecast_payload, dict) else []
    if not isinstance(periods, list):
        periods = []
    periods = [p for p in periods if isinstance(p, dict)]
    hourly = forecast_payload.get("hourly_summary") if isinstance(forecast_payload, dict) else []
    if not isinstance(hourly, list):
        hourly = []
    hourly = [item for item in hourly if isinstance(item, dict)]
    selected = hourly or periods
    rain_values = [value for item in selected if (value := _decision_rain_value(item)) is not None]
    compact_periods = [_compact_decision_period(item) for item in selected[:12]]
    start_time = forecast_payload.get("forecast_start_time")
    end_time = forecast_payload.get("forecast_end_time")
    if not start_time and selected:
        start_time = selected[0].get("start_time")
    if not end_time and selected:
        end_time = selected[-1].get("end_time")

    facts = {
        "data_source": forecast_payload.get("data_source"),
        "query_mode": forecast_payload.get("query_mode"),
        "fcst_time": forecast_payload.get("fcst_time"),
        "interval_hours": forecast_payload.get("interval_hours"),
        "target_start_time": start_time,
        "target_end_time": end_time,
        "has_rain_signal": any(value > 0.1 for value in rain_values),
        "total_rain_mm": round(sum(rain_values), 2) if rain_values else None,
        "periods": compact_periods,
    }
    hourly_facts = _build_decision_hourly_facts(forecast_payload, hourly)
    if hourly_facts:
        facts["hourly_rain"] = hourly_facts
    return facts


def _ainvoke_chain(callbacks: dict) -> Any:
    """从 callbacks 中取出 LLM 调用函数。"""
    fn = callbacks.get("ainvoke_chain")
    if not fn:
        raise RuntimeError("callbacks 中缺少 ainvoke_chain")
    return fn


async def _extract_decision_weather_slots(user_text: str, answer_chain: Any, callbacks: dict) -> dict:
    """抽取点位与问题类型；优先规则，规则不明确时回退 LLM。"""
    rule_slots = _extract_decision_slots_rule_based(user_text)
    if rule_slots:
        return rule_slots
    # 回退：现有 LLM 抽取
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


def _decision_table_cell(value: Any, default: str = "—") -> str:
    """清理代码生成的 Markdown 表格单元格，不改变接口字段的业务内容。"""
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip().replace("\r", " ").replace("\n", " ").replace("|", "｜")
    return re.sub(r"\s+", " ", text)


def _decision_period_label(period: dict) -> str:
    label = str(period.get("period_label") or "").strip()
    if label:
        return label
    start = _parse_decision_dt(period.get("start_time"))
    end = _parse_decision_dt(period.get("end_time"))
    if start and end:
        return (
            f"{start.month}月{start.day}日{start.hour}时"
            f"-{end.month}月{end.day}日{end.hour}时"
        )
    return (
        f"{_decision_table_cell(period.get('start_time'))}"
        f"-{_decision_table_cell(period.get('end_time'))}"
    )


def _decision_temperature_text(period: dict) -> str:
    tmin = period.get("tmin")
    tmax = period.get("tmax")
    if tmin is not None and tmax is not None:
        return f"{_decision_temperature_text_value(tmin)}~{_decision_temperature_text_value(tmax)}"
    return _decision_temperature_text_value(tmax if tmax is not None else tmin) or "—"


def _build_decision_weather_table(user_text: str, facts: dict) -> str:
    """根据滚动预报事实确定性生成点位天气表，风况原样使用接口 EDA。"""
    periods = [item for item in (facts.get("periods") or []) if isinstance(item, dict)]
    if not periods:
        return ""

    location = _decision_table_cell((facts.get("poi") or {}).get("name"), "该位置")
    hourly_rain = facts.get("hourly_rain") if isinstance(facts.get("hourly_rain"), dict) else {}
    mode = str(hourly_rain.get("mode") or "")
    if mode == "rain_current_hour":
        title = f"【{location}当前小时预报】"
    elif mode == "rain_next_hours":
        title = f"【{location}逐小时预报】"
    elif len(periods) == 1 and any(word in str(user_text or "") for word in ("明天", "明日")):
        title = f"【{location}明日预报】"
    else:
        title = f"【{location}逐日预报】"

    rows = [
        [
            _decision_period_label(period),
            period.get("weather"),
            _decision_temperature_text(period),
            period.get("EDA") if period.get("EDA") is not None else period.get("wind"),
        ]
        for period in periods
    ]
    headers = ["日期/时段", "天气现象", "气温(℃)", "风力风向"]
    lines = [
        title,
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_decision_table_cell(value) for value in row) + " |"
        for row in rows
    )
    return "\n".join(lines)


def _decision_core_only(answer: Any, user_text: str = "") -> str:
    """只保留模型生成的首句核心结论，丢弃其可能附带的表格或其它区块。"""
    text = str(answer or "").strip()
    match = re.search(r"【核心结论】\s*(.*?)(?=\n\s*【[^】]+】|\Z)", text, re.DOTALL)
    core = match.group(1) if match else text
    kept_lines = [
        line.strip()
        for line in core.splitlines()
        if line.strip()
        and not line.strip().startswith("|")
        and not line.strip().startswith(("数据来源：", "数据来源:"))
    ]
    normalized = re.sub(r"\s+", " ", " ".join(kept_lines)).strip()
    sentence = re.match(r"^.*?[。！？!?](?:[”’」』])?", normalized)
    core = sentence.group(0).strip() if sentence else normalized
    return sanitize_forecast_core_summary(core, user_text)


# 点位地理类型 → 注意事项模板（代码确定性生成，仅作为骨架，天气断言由 facts 数值派生）
_POI_CATEGORY_REMINDER_TEMPLATES: dict[str, str] = {
    "school": "学校区域师生与家长请注意出行安全，关注上下学时段路况与天气变化。",
    "scenic": "景区游客较多，雨时道路湿滑，请注意防滑、防雷与游览安全。",
    "mountain": "山区地形复杂，强降雨时易诱发地质灾害与山洪，请避免进入山谷、沟道等危险区域。",
    "airport": "机场区域请注意降雨、大风、雷暴及低能见度对航班起降的影响。",
    "station": "车站人流密集，请注意雨天路滑、乘车安全及列车运行调整信息。",
}


def _decision_max_wind_level(periods: list[dict]) -> int | None:
    """从预报时段的风况文本解析最大风力等级；无法解析返回 None。

    先抓所有 ``A～B级`` 区间取端点较大值，再对文本里全部 ``N级`` 单值取最大，
    保证 ``X～Y级转Z级``、``X～Y级阵风Z级`` 这类复合风况不低估。
    """
    max_level: int | None = None
    for period in periods or []:
        text = str(period.get("EDA") or period.get("wind") or "")
        for match in re.finditer(r"(\d+)\s*[～~-]\s*(\d+)\s*级", text):
            try:
                level = max(int(match.group(1)), int(match.group(2)))
            except ValueError:
                continue
            if max_level is None or level > max_level:
                max_level = level
        for match in re.finditer(r"(\d+)\s*级", text):
            try:
                level = int(match.group(1))
            except ValueError:
                continue
            if max_level is None or level > max_level:
                max_level = level
    return max_level


def _decision_min_visibility_km(periods: list[dict]) -> float | None:
    """从预报时段解析最小能见度（千米）；无法解析返回 None。"""
    minimum: float | None = None
    for period in periods or []:
        value = period.get("visibility_min_km")
        if value is None:
            value = period.get("visibility_min_m")
            if value is not None:
                try:
                    value = float(value) / 1000.0
                except (TypeError, ValueError):
                    value = None
        if value is None:
            continue
        try:
            num = float(value)
        except (TypeError, ValueError):
            continue
        if minimum is None or num < minimum:
            minimum = num
    return minimum


# 降雨强度级别：0=无明显降雨 1=小雨/有降雨 2=中雨(≥10mm) 3=大雨(≥25mm) 4=暴雨(≥50mm)
_RAIN_INTENSITY_LEVELS: dict[int, str] = {
    0: "无明显降雨",
    1: "小雨/有降雨",
    2: "中雨",
    3: "大雨",
    4: "暴雨",
}


def _decision_rain_intensity(facts: dict) -> tuple[int, float | None]:
    """从 facts 判定预报降雨强度级别。

    返回 (级别, 累计雨量mm或None)。按 total_rain_mm 分档，缺失时用
    has_rain_signal 兜底（>0mm 即小雨）。天气断言只来自 facts 实际数值，不编造。
    """
    mm = facts.get("total_rain_mm")
    try:
        mm_float = float(mm) if mm is not None and str(mm).strip() != "" else None
    except (TypeError, ValueError):
        mm_float = None
    if mm_float is not None:
        if mm_float >= 50:
            return 4, mm_float
        if mm_float >= 25:
            return 3, mm_float
        if mm_float >= 10:
            return 2, mm_float
        if mm_float > 0:
            return 1, mm_float
    if facts.get("has_rain_signal") is True:
        return 1, mm_float
    return 0, mm_float


# 隐患类型 × 降雨强度 → (风险研判, 专业建议)。强度级别同 _RAIN_INTENSITY_LEVELS。
# 代码确定性生成：风险等级与建议只由降雨强度分档 + 隐患类型决定，零编造。
_HAZARD_RAIN_RISK: dict[str, dict[int, tuple[str, str]]] = {
    "dzzh": {
        0: ("风险低", "无显著降雨，地质灾害风险低，可正常出行。"),
        1: ("风险较低", "有降雨，注意陡坡、边坡区域湿滑，避免在陡坡下方长时间停留。"),
        2: ("风险中等", "中雨使坡体含水量上升，注意滑坡、崩塌风险，避开陡坡、沟口下方。"),
        3: ("风险较高", "大雨易诱发滑坡、崩塌、泥石流，尽量避免进入山区及陡坡区域，留意地质险情。"),
        4: ("风险高", "暴雨极易诱发滑坡、崩塌、泥石流，切勿进入山谷、沟道、陡坡下方，按预警转移避险。"),
    },
    "sh": {
        0: ("风险低", "无显著降雨，山洪风险低。"),
        1: ("风险较低", "有降雨，避免在山洪沟道、河谷低洼处停留。"),
        2: ("风险中等", "中雨可能引发沟道洪水，远离山洪沟道、漫水路段，留意上游来水。"),
        3: ("风险较高", "大雨时段山洪风险上升，切勿进入河道、沟道及行洪区，关注预警信息。"),
        4: ("风险高", "暴雨易引发山洪，立即远离河道、沟道、行洪区，服从转移避险安排。"),
    },
    "zxhl": {
        0: ("风险低", "无显著降雨，中小河流水位平稳，风险低。"),
        1: ("风险较低", "有降雨，注意远离河岸、漫水桥，观察水位变化。"),
        2: ("风险中等", "中雨致中小河流水位上涨，远离河岸、桥梁及低洼河段。"),
        3: ("风险较高", "大雨致水位明显上涨，避免在河边、漫水桥、涉水路段停留。"),
        4: ("风险高", "暴雨致中小河流可能超警，远离河道及淹没区，关注水情预警。"),
    },
}


def _build_poi_reminder_section(facts: dict) -> str:
    """根据点位类别 + 周边隐患点确定性生成“⚠️ 注意事项”段落。

    无可展示内容（既无类别模板、也无降雨期隐患点）时返回空串。
    隐患点只在预报有降雨时提醒（有降雨才存在诱发风险），且只给类型+数量汇总表，
    不逐条列举隐患点名称/位置。天气断言只来自 facts 实际数值，不编造。
    """
    category = facts.get("poi_category")
    hp_raw = facts.get("hazard_points")
    hazard_points = hp_raw if isinstance(hp_raw, dict) else None
    has_hazard = (
        hazard_points is not None
        and hazard_points.get("status") == "ok"
        and int(hazard_points.get("total_found") or 0) > 0
    )
    # 有降雨才展示隐患点风险研判；无雨时不打扰。
    intensity, mm = _decision_rain_intensity(facts)
    show_hazard = has_hazard and intensity > 0
    if not category and not show_hazard:
        return ""

    lines: list[str] = ["⚠ 注意事项"]
    if category:
        template = _POI_CATEGORY_REMINDER_TEMPLATES.get(category)
        if template:
            clauses: list[str] = []
            periods = [p for p in (facts.get("periods") or []) if isinstance(p, dict)]
            if facts.get("has_rain_signal") is True:
                clauses.append("当前预报时段内有降雨信号，请携带雨具并注意防滑。")
            elif facts.get("total_rain_mm") is not None and float(facts["total_rain_mm"]) >= 10:
                clauses.append(f"未来累计降雨可达约 {float(facts['total_rain_mm']):.0f} 毫米，请注意防范。")
            max_wind = _decision_max_wind_level(periods)
            if not clauses and max_wind is not None and max_wind >= 6:
                clauses.append("风力较大，请注意大风防范。")
            min_vis = _decision_min_visibility_km(periods)
            if not clauses and min_vis is not None and min_vis < 1.0:
                clauses.append("能见度较低，出行请注意交通安全。")
            lines.append(template + (clauses[0] if clauses else ""))

    if show_hazard:
        categories = hazard_points.get("categories") if isinstance(hazard_points.get("categories"), list) else []
        # 统计各隐患类型数量，供风险研判表使用（隐患点不逐条列举，只给类型+数量汇总）
        hazard_counts: dict[str, tuple[str, int]] = {}
        for category_item in categories:
            if not isinstance(category_item, dict):
                continue
            key = str(category_item.get("key") or "")
            label = str(category_item.get("label") or "")
            count = int(category_item.get("count") or 0)
            if key and label and count > 0:
                hazard_counts[key] = (label, count)
        if hazard_counts:
            # 风险研判表：预报降雨强度 × 隐患类型 → 风险等级 + 专业建议（代码确定性生成）
            intensity_label = _RAIN_INTENSITY_LEVELS.get(intensity, "无明显降雨")
            if mm is not None and mm > 0:
                lines.append(f"预计未来降雨约 {mm:.0f} 毫米（{intensity_label}），周边灾害风险研判如下：")
            else:
                lines.append(f"预计未来为{intensity_label}，周边灾害风险研判如下：")
            lines.append("")
            lines.append("| 隐患类型 | 数量 | 风险研判 | 防范建议 |")
            lines.append("| --- | --- | --- | --- |")
            for key in ("dzzh", "sh", "zxhl"):
                if key not in hazard_counts:
                    continue
                label, count = hazard_counts[key]
                risk, advice = _HAZARD_RAIN_RISK[key][intensity]
                lines.append(f"| {label} | {count} 处 | {risk} | {advice} |")
            lines.append("")

    return "\n".join(line.rstrip() for line in lines).strip()


async def _generate_decision_weather_answer(user_text: str, facts: dict, answer_chain: Any, callbacks: dict) -> str:
    """由模型生成一句结论，再由代码生成点位天气表、注意事项和数据来源。"""
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
        "只输出【核心结论】及其正文，正文严格且只能有一句；只围绕用户明确询问的"
        "降雨、天气、气温、风力、能见度或活动适宜性直接作答，不主动扩展无关风险、背景或建议。\n"
        "不要机械补充“无降水/无降雨”或“风力为X级”等泛化描述；只有用户明确询问降水或风力时才回答对应要素。\n"
        "所有温度数值必须按四舍五入展示为整数，不得输出小数。\n"
        "未来N小时降雨问题必须使用代码给出的 rain_level 和 total_rain_text；当前是否下雨只能依据"
        "当前整点至下一整点预报判断，不得表述为降雨实况，也不得编造过去1/3/6小时累计雨量。\n"
        "表格、逐时或逐日数据行和数据来源均由代码生成；不得输出表格、其它标题、数据来源或技术说明。\n\n"
        f"用户问题：{user_text}\n\n"
        f"业务天气事实 JSON：{json.dumps(business_facts, ensure_ascii=False, default=str)}"
    )
    result = await _ainvoke_chain(callbacks)(answer_chain, {"messages": [HumanMessage(content=prompt)]})
    answer = getattr(result, "content", None) or str(result)
    core = _decision_core_only(answer, user_text)
    table = _build_decision_weather_table(user_text, facts)
    reminder = _build_poi_reminder_section(facts)
    source = _decision_table_cell(facts.get("data_source"), "天津市气象台滚动预报")
    sections = [f"【核心结论】\n{core}".rstrip()]
    if table:
        sections.append(table)
    if reminder:
        sections.append(reminder)
    sections.append(f"数据来源：{source}。")
    return "\n\n".join(section for section in sections if section).strip()
