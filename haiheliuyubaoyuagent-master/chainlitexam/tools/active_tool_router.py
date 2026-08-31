"""安全的请求级主动工具路由。

只对高置信单域问题缩小首轮 Planner 工具集；复杂、混合或未知问题返回完整
Planner。第二轮是否回退完整 Planner 由 message_orchestrator 强制保证。
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable

from tools.decision_weather_core import (
    has_decision_weather_poi_marker,
    has_mixed_regional_and_poi_scope,
)
from tools.request_intent_policy import (
    CURRENT_TIME_MARKERS,
    FUTURE_TIME_MARKERS,
    is_areal_rainfall_query,
    is_rainfall_impact_intent,
    is_river_network_relation_intent,
    is_supported_current_observation_scope,
    is_unsafe_for_active_tool_filter,
)


@dataclass(frozen=True)
class ToolRouteDecision:
    mode: str
    query_type: str
    tool_names: tuple[str, ...]
    requires_tool: bool
    reason: str


_WEATHER_WORDS = ("天气", "气温", "温度", "降水", "下雨", "雨", "风力", "阵风", "能见度", "雾")
_KNOWN_RIVER_NAMES = (
    "大清河", "子牙河", "永定河", "北三河", "漳卫南运河", "漳卫河",
    "徒骇马颊河", "黑龙港", "滦河", "潮白河", "蓟运河", "海河干流", "海河",
)
_RIVER_QUERY_RE = re.compile(
    r"[\u4e00-\u9fff]{1,8}河(?=流域|河系|沿线|今天|今日|今晚|今夜|明天|明日|后天|未来|当前|现在|目前|天气|降雨|降水|有雨|会下雨|是否下雨|气温|温度|$|[，。！？?\s])"
)
_RIVER_FORECAST_TIME_RE = re.compile(
    r"今天晚上|今晚|今天|今日|明天|明日|(?<!大)后天|未来\s*"
    r"(?:[1-9]\d?|[一二两三四五六七八九]|[一二两三四五六七八九]?十[一二两三四五六七八九]?)\s*天"
)
_OTHER_RIVER_TIME_RE = re.compile(
    r"未来|今夜|周(?!边)|星期|礼拜|年|月|小时|钟头|分钟|"
    r"清晨|早上|上午|中午|下午|傍晚|晚上|夜间|夜里|夜晚|凌晨|白天|"
    r"昨天|昨日|前天|近期|最近|接下来|之后|以后|后续|"
    r"\d\s*(?:日|号|点|时|[:：/-])|[0-9一二两三四五六七八九十几]+\s*天"
)
_RIVER_FORECAST_EXCLUDE_MARKERS = (
    "水位", "水势", "库容", "闸上", "闸下", "蓄水量",
    "历史", "过去", "去年", "前年", "实况", "观测", "累计",
    "对比", "比较", "分别", "各自", "哪个", "哪条",
)
_RIVER_FORECAST_NON_RAIN_WEATHER_MARKERS = (
    "气温", "温度", "风力", "风向", "阵风", "风大", "大风", "风速", "能见度", "雾", "霾", "湿度", "云量",
)
_RIVER_FORECAST_POI_MARKERS = (
    "公园", "湿地", "附近", "景区", "机场", "大学", "医院", "广场", "车站", "火车站",
)
_RIVER_FORECAST_RETROSPECTIVE_RAIN_RE = re.compile(
    r"(?:已经|已)下雨|下雨了|雨下了吗|(?:今天|今日).*?下雨情况"
)
_REGION_RISK_SCOPE_MARKERS = (
    "天津市区", "天津市", "天津", "全市", "我市", "本市", "市区", "中心城区",
    "蓟州区", "蓟州", "宝坻区", "宝坻", "武清区", "武清", "宁河区", "宁河",
    "静海区", "静海", "北辰区", "北辰", "西青区", "西青", "津南区", "津南",
    "东丽区", "东丽", "滨海新区", "滨海",
)
_REGION_RISK_TIME_MARKERS = ("今天", "今日", "明天", "明日", "未来", "当前", "现在", "可能")
_REGION_RISK_NON_WEATHER_MARKERS = ("项目", "投资", "合同", "上线", "账号")
_REGION_RISK_SINGLE_HAZARD_MARKERS = (
    "山洪", "地质灾害", "中小河流洪水", "中小河流", "河流洪水", "滑坡", "崩塌", "泥石流",
)
_REGION_RISK_MIXED_DOMAIN_MARKERS = ("河流", "河道")

_DOMAIN_TOOLS: dict[str, tuple[str, ...]] = {
    "current": ("query_current_weather_observation",),
    "water_level": ("query_water_level",),
    "rain": ("query_basin_areal_rainfall",),
    "forecast": ("query_rolling_forecast",),
    "region_risk": ("query_region_weather_risks",),
    "decision_poi": ("query_decision_weather_for_poi",),
    "basin_forecast": ("get_river_system_rainfall_forecast",),
    "river_forecast": ("query_river_rainfall_forecast",),
    "warning_effective": ("get_effective_warning_info",),
    "warning_history": ("get_history_warning_info", "get_effective_warning_info"),
    "warning_national": ("get_national_warning_info",),
    "rag": ("rag_search",),
}


def _normalized_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 12
    return parsed if 5 <= parsed <= 20 else 12


def _classify_domains(text: str) -> list[str]:
    domains: list[str] = []
    if "预警" in text:
        domains.append("warning")
    if any(word in text for word in ("水位", "水势", "库容", "闸上", "闸下", "蓄水量")):
        domains.append("water_level")
    areal_rainfall = is_areal_rainfall_query(text)
    if areal_rainfall:
        domains.append("rain")

    has_weather = any(word in text for word in _WEATHER_WORDS)
    # “暴雨预警”里的“雨”不能再额外制造 current/forecast 域；只有用户同时明确
    # 询问天气、气温、风或能见度时才视为预警+天气混合问题。
    if "预警" in text and not any(
        word in text for word in ("天气", "气温", "温度", "风力", "阵风", "能见度")
    ):
        has_weather = False
    has_future = any(word in text for word in FUTURE_TIME_MARKERS)
    has_current = any(word in text for word in CURRENT_TIME_MARKERS)
    if has_weather and has_future:
        domains.append("forecast")
    if (
        has_weather
        and has_current
        and not areal_rainfall
        and "累计" not in text
        and "过去" not in text
    ):
        domains.append("current")

    if any(word in text for word in ("知识库", "业务规范", "规章制度")):
        domains.append("rag")
    return list(dict.fromkeys(domains))


def _looks_like_river_scope(text: str) -> bool:
    return any(name in text for name in _KNOWN_RIVER_NAMES) or bool(_RIVER_QUERY_RE.search(text))


def _has_river_forecast_exclusion(text: str) -> bool:
    """统一河流路由与通用河系回退共用实况、混合和非降雨排除规则。"""
    if any(marker in text for marker in _RIVER_FORECAST_EXCLUDE_MARKERS):
        return True
    if any(marker in text for marker in _RIVER_FORECAST_NON_RAIN_WEATHER_MARKERS):
        return True
    # 已发生降雨标记优先视为实况；即使同句还追问明天，也应交完整 Planner
    # 组合实况与预报工具，不能强制为单一河流未来预报。
    if "下了" in text or _RIVER_FORECAST_RETROSPECTIVE_RAIN_RE.search(text):
        return True
    return bool(re.search(r"(?:与|和)[\u4e00-\u9fff]{1,8}河", text))


def is_conservative_river_forecast_query(user_text: str, *, require_supported_time: bool = True) -> bool:
    """仅支持已实现的单一时间窗口；范围防护可单独检查纯河流意图。"""
    text = str(user_text or "").strip()
    if not text or not _looks_like_river_scope(text):
        return False
    if require_supported_time:
        if len(_RIVER_FORECAST_TIME_RE.findall(text)) != 1:
            return False
        if _OTHER_RIVER_TIME_RE.search(_RIVER_FORECAST_TIME_RE.sub("", text)):
            return False
    if not any(word in text for word in _WEATHER_WORDS):
        return False
    if is_unsafe_for_active_tool_filter(text):
        return False
    if has_decision_weather_poi_marker(text) or any(marker in text for marker in _RIVER_FORECAST_POI_MARKERS):
        return False
    if _has_river_forecast_exclusion(text):
        return False
    if is_areal_rainfall_query(text) or is_river_network_relation_intent(text) or is_rainfall_impact_intent(text):
        return False
    return True


def is_conservative_region_risk_query(user_text: str) -> bool:
    """仅识别天津已知区域的泛综合风险研判，专业或混合问题一律回退。"""
    text = str(user_text or "").strip()
    if not text or "风险" not in text:
        return False
    if not any(scope in text for scope in _REGION_RISK_SCOPE_MARKERS):
        return False
    if not any(marker in text for marker in _REGION_RISK_TIME_MARKERS):
        return False
    if any(marker in text for marker in _REGION_RISK_NON_WEATHER_MARKERS):
        return False
    if any(marker in text for marker in _REGION_RISK_SINGLE_HAZARD_MARKERS):
        return False
    if any(marker in text for marker in _REGION_RISK_MIXED_DOMAIN_MARKERS):
        return False
    # 预警、降雨/面雨量等已有专用域，以及河网、应急响应等混合域不能被泛风险抢占。
    if _classify_domains(text) or is_unsafe_for_active_tool_filter(text):
        return False
    return True


class ActiveToolRouter:
    """选择过滤工具集并缓存对应 Planner chain。"""

    def __init__(
        self,
        *,
        tools: list[Any],
        full_chain: Any,
        build_chain: Callable[[list[Any]], Any],
        candidate_index: Any,
        chain_cache_max_size: int = 64,
    ):
        self.full_chain = full_chain
        self._tools_by_name = {
            str(getattr(tool, "name", "")): tool
            for tool in tools
            if getattr(tool, "name", "")
        }
        self._build_chain = build_chain
        # 保留 candidate_index 参数，兼容既有运行时装配；主动路由的业务白名单
        # 只允许每个单域的必需工具，候选索引继续由 orchestrator 用于召回观测。
        try:
            capacity = int(chain_cache_max_size)
        except (TypeError, ValueError):
            capacity = 64
        self._chain_cache_max_size = capacity if capacity > 0 else 64
        self._chain_cache: OrderedDict[tuple[str, ...], Any] = OrderedDict()
        self._chain_cache_lock = threading.Lock()

    @property
    def chain_cache_size(self) -> int:
        with self._chain_cache_lock:
            return len(self._chain_cache)

    def _full(self, query_type: str, reason: str) -> ToolRouteDecision:
        return ToolRouteDecision("full", query_type, tuple(), False, reason)

    def select(self, user_text: str, limit: int = 12) -> ToolRouteDecision:
        text = str(user_text or "").strip()
        if not text:
            return self._full("unknown", "empty_query")
        if is_unsafe_for_active_tool_filter(text):
            return self._full("unsafe", "unsafe_domain")
        if has_mixed_regional_and_poi_scope(text):
            return self._full("mixed", "regional_and_poi_scope")
        if is_conservative_region_risk_query(text):
            required = _DOMAIN_TOOLS["region_risk"]
            if any(name not in self._tools_by_name for name in required):
                return self._full("region_risk", "required_tool_missing")
            return ToolRouteDecision(
                "filtered", "region_risk", required, True, "single_domain:region_risk"
            )
        if is_conservative_river_forecast_query(text):
            required = _DOMAIN_TOOLS["river_forecast"]
            if any(name not in self._tools_by_name for name in required):
                return self._full("river_forecast", "required_tool_missing")
            return ToolRouteDecision(
                "filtered", "river_forecast", required, True, "single_domain:river_forecast"
            )

        domains = _classify_domains(text)
        if len(domains) != 1:
            return self._full("mixed" if domains else "unknown", "mixed_or_unknown")

        domain = domains[0]
        policy_key = domain
        query_type = domain
        if domain == "warning":
            query_type = "warning"
            if any(word in text for word in FUTURE_TIME_MARKERS):
                return self._full("warning", "future_warning_requires_full")
            if any(word in text for word in ("中央气象台", "国家级", "全国预警")):
                policy_key = "warning_national"
            elif any(word in text for word in ("解除", "历史", "曾经", "发布过")):
                policy_key = "warning_history"
            else:
                policy_key = "warning_effective"
        elif domain == "current":
            if has_decision_weather_poi_marker(text) or any(
                marker in text for marker in _RIVER_FORECAST_POI_MARKERS
            ):
                policy_key = "decision_poi"
                query_type = "decision_poi"
            elif _looks_like_river_scope(text):
                return self._full("current", "river_current_requires_full")
            elif not is_supported_current_observation_scope(text):
                return self._full("current", "unsupported_current_scope")
        elif domain == "water_level" and any(word in text for word in FUTURE_TIME_MARKERS):
            return self._full("water_level", "future_water_level_requires_full")
        elif domain == "forecast":
            # 点位名可能自带河名（如“海河教育园区”），必须先按点位识别，
            # 避免误路由为整个河系预报。
            if has_decision_weather_poi_marker(text) or any(
                marker in text for marker in _RIVER_FORECAST_POI_MARKERS
            ):
                policy_key = "decision_poi"
                query_type = "decision_poi"
            elif "流域" in text or "河系" in text or _looks_like_river_scope(text):
                # 共享谓词已排除的实况/混合/非降雨问题，不能被通用预报域
                # 再次收窄为河系降雨工具；纯河系预报仍保留既有回退。
                if _has_river_forecast_exclusion(text):
                    return self._full("forecast", "river_forecast_requires_full")
                policy_key = "basin_forecast"
                query_type = "basin_forecast"

        required = _DOMAIN_TOOLS[policy_key]
        if any(name not in self._tools_by_name for name in required):
            return self._full(query_type, "required_tool_missing")

        max_tools = _normalized_limit(limit)
        names = list(required[:max_tools])
        return ToolRouteDecision(
            "filtered", query_type, tuple(names), True, f"single_domain:{policy_key}"
        )

    def chain_for(self, decision: ToolRouteDecision):
        if decision.mode != "filtered":
            return self.full_chain
        key = tuple(decision.tool_names)
        with self._chain_cache_lock:
            cached = self._chain_cache.get(key)
            if cached is not None:
                self._chain_cache.move_to_end(key)
                return cached
            selected = [self._tools_by_name[name] for name in key if name in self._tools_by_name]
            if len(selected) != len(key):
                raise RuntimeError("filtered tool unavailable")
            chain = self._build_chain(selected)
            self._chain_cache[key] = chain
            self._chain_cache.move_to_end(key)
            while len(self._chain_cache) > self._chain_cache_max_size:
                self._chain_cache.popitem(last=False)
            return chain
