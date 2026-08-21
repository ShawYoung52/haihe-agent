"""主动工具过滤使用的保守请求意图策略。

这里集中维护会改变业务工具选择的高风险谓词；不确定时返回不安全，让完整
Planner 处理。该模块只做字符串判断，不依赖 Chainlit 或模型运行时。
"""
from __future__ import annotations

import re


CURRENT_TIME_MARKERS = ("现在", "当前", "目前", "实时", "实况")
FUTURE_TIME_MARKERS = (
    "明天", "明日", "后天", "未来", "周末", "一周", "下周", "几点开始",
    "什么时候停", "何时停", "预报", "预计",
)

EMERGENCY_RESPONSE_INTENT_MARKERS = (
    "防汛应急响应", "应急响应", "防汛响应", "启动响应", "是否启动",
    "应急等级", "应急级别", "响应等级", "响应级别", "几级响应",
)
_RIVER_RELATION_MARKERS = (
    "河网", "水系", "拓扑", "上游", "下游", "上下游", "汇入", "流向",
    "连接河流", "连通", "干流",
)
_STATIC_FILTER_UNSAFE_MARKERS = (
    "行政区", "地图", "图层", "GIS", "gis", "分布图", "实况图", "长图", "画图",
)
_RAIN_WORDS = ("降雨", "降水", "雨量", "下雨", "暴雨", "这场雨")
_RAINFALL_IMPACT_WORDS = ("影响", "波及", "涉及", "冲击")
_RAINFALL_IMPACT_TARGETS = ("河流", "河道", "河网", "范围", "区域", "上游", "下游")
_AREAL_DIRECT_MARKERS = ("面雨量", "分区雨量", "流域雨量", "流域降雨", "河系雨量")
_AREAL_SCOPE_MARKERS = (
    "九分区", "9分区", "11分区", "77分区", "246分区", "各分区",
    "各流域", "子流域", "各河系",
)
_CURRENT_SCOPE_RE = re.compile(
    r"[\u4e00-\u9fff]{1,8}?(?:特别行政区|自治区|自治州|新区|省|市|区|县|旗)"
)
_SUPPORTED_CURRENT_ADMIN_SCOPES = {
    "天津市", "北京市", "河北省", "蓟州区", "中心城区", "全市", "市区",
}
_SUPPORTED_CURRENT_SCOPE_MARKERS = (
    "天津市", "天津", "北京市", "北京", "河北省", "河北", "中心城区",
    "蓟州", "全市", "市区", "海河流域",
)
_GENERIC_CURRENT_QUERY_PREFIXES = (
    "现在", "当前", "目前", "实时", "实况", "今天", "今日", "刚才",
    "天气", "气温", "温度", "降水", "降雨", "下雨", "雨", "风力", "阵风",
    "能见度", "雾",
)


def is_emergency_response_intent(user_text: str) -> bool:
    text = str(user_text or "")
    return any(marker in text for marker in EMERGENCY_RESPONSE_INTENT_MARKERS) or bool(
        re.search(r"(?:达到|启动|是否|判定|判断).{0,6}(?:[一二三四五六七八九十\d]+级)?响应", text)
    )


def has_mixed_current_future_scope(user_text: str) -> bool:
    text = str(user_text or "")
    return any(word in text for word in CURRENT_TIME_MARKERS) and any(
        word in text for word in FUTURE_TIME_MARKERS
    )


def is_river_network_relation_intent(user_text: str) -> bool:
    text = str(user_text or "")
    if any(marker in text for marker in _RIVER_RELATION_MARKERS):
        return True
    return bool(
        re.search(r"(?:连接|相连|连到|流入).{0,6}(?:河流|河道|哪条河)", text)
        or re.search(r"(?:河流|河道|哪条河).{0,6}(?:连接|相连|连通|流入)", text)
    )


def is_rainfall_impact_intent(user_text: str) -> bool:
    text = str(user_text or "")
    return (
        any(word in text for word in _RAIN_WORDS)
        and any(word in text for word in _RAINFALL_IMPACT_WORDS)
        and any(word in text for word in _RAINFALL_IMPACT_TARGETS)
    )


def is_unsafe_for_active_tool_filter(user_text: str) -> bool:
    text = str(user_text or "")
    return (
        any(marker in text for marker in _STATIC_FILTER_UNSAFE_MARKERS)
        or is_emergency_response_intent(text)
        or is_river_network_relation_intent(text)
        or is_rainfall_impact_intent(text)
    )


def is_areal_rainfall_query(user_text: str) -> bool:
    text = str(user_text or "")
    if any(marker in text for marker in _AREAL_DIRECT_MARKERS):
        return True
    return (
        any(scope in text for scope in _AREAL_SCOPE_MARKERS)
        and any(rain_word in text for rain_word in _RAIN_WORDS)
    )


def is_supported_current_observation_scope(user_text: str) -> bool:
    """实况聚合工具只覆盖固定区域；未知或更细地域交回完整 Planner。"""
    text = str(user_text or "").strip()
    admin_scopes = _CURRENT_SCOPE_RE.findall(text)
    if any(scope not in _SUPPORTED_CURRENT_ADMIN_SCOPES for scope in admin_scopes):
        return False
    if any(marker in text for marker in _SUPPORTED_CURRENT_SCOPE_MARKERS):
        return True
    normalized = re.sub(r"^(?:请问|请|帮我|麻烦|查一下|查询一下)+", "", text).strip()
    return normalized.startswith(_GENERIC_CURRENT_QUERY_PREFIXES)
