"""Tests for the decision weather POI tool."""

import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

# decision_weather.py imports langchain_core.tools.tool, which is not covered by the shared stubs.
_lc_tools = types.ModuleType("langchain_core.tools")


def _tool_stub(func):
    class _ToolWrapper:
        def __init__(self, fn):
            self._fn = fn
            self.name = fn.__name__
            self.description = fn.__doc__

        async def ainvoke(self, args):
            if isinstance(args, dict):
                return await self._fn(**args)
            return await self._fn(args)

    return _ToolWrapper(func)


_lc_tools.tool = _tool_stub
sys.modules["langchain_core.tools"] = _lc_tools

import chainlitexam.tools.decision_weather as dw
from chainlitexam.tools import decision_weather_core as dw_core


def test_build_decision_weather_tools_returns_one_tool():
    tools = dw.build_decision_weather_tools(None, [], {})
    assert len(tools) == 1
    assert tools[0].name == "query_decision_weather_for_poi"


def test_prefilter_allows_location_with_time_and_rejects_time_only():
    assert dw._decision_weather_prefilter("梅江会展中心明天天气怎么样") is True
    assert dw._decision_weather_prefilter("XX公园适合周末露营吗") is True
    assert dw._decision_weather_prefilter("今天天气怎么样") is False
    assert dw._decision_weather_prefilter("未来24小时会下雨吗") is False
    assert dw._decision_weather_prefilter("中心城区现在气温和风的实况") is False
    assert dw._decision_weather_prefilter("全市各站现在气温如何") is False
    assert dw._decision_weather_prefilter("天津在未来24小时会下雨吗") is False
    assert dw._decision_weather_prefilter("天津到明天会下雨吗") is False
    assert dw._decision_weather_prefilter("明天有潜在降雨风险吗") is False
    assert dw._decision_weather_prefilter("现在有潜在大风风险吗") is False
    assert dw._decision_weather_prefilter("我在五大道明天天气如何") is True


def test_prefilter_accepts_poi_guard_routed_keywords():
    # POI 守卫（rolling_forecast_service.POI_PLACE_KEYWORDS）把带点位词的问题路由到决策天气，
    # 前置过滤必须同口径放行——密云水库生产回归：守卫接了但 prefilter 拒了，空手而返
    assert dw._decision_weather_prefilter("未来三天密云水库有降水吗？") is True
    assert dw._decision_weather_prefilter("密云水库有降水吗") is True
    assert dw._decision_weather_prefilter("官厅水库周边会下雨吗") is True
    assert dw._decision_weather_prefilter("湿地公园附近会下雨吗") is True
    assert dw._decision_weather_prefilter("博物馆周边气温多少") is True
    assert dw._decision_weather_prefilter("开发区天气怎么样") is True


def test_rule_based_slots_extract_reservoir_location():
    slots = dw_core._extract_decision_slots_rule_based("未来三天密云水库有降水吗？")
    assert slots and slots["is_decision_weather"] is True
    assert slots["location_name"] == "密云水库"
    assert slots["question_type"] == "rain_next_hours"


def test_periods_rain_only_detects_precip_only_point():
    # 外埠点位（密云水库）滚动预报只回降水 TP1H，天气/气温/风为空
    rain_only = [
        {"weather": None, "tmax": None, "tmin": None, "EDA": "", "rain_1h": 0.0},
        {"weather": None, "tmax": None, "tmin": None, "EDA": "", "rain_1h": 0.5},
    ]
    assert dw_core._decision_periods_rain_only(rain_only) is True
    # 有天气/气温/风 → 非 rain-only
    with_text = [{"weather": "多云", "tmax": "30", "tmin": "22", "EDA": "东南风3级", "rain_1h": 0.0}]
    assert dw_core._decision_periods_rain_only(with_text) is False
    # 连降水也没有 → 不算 rain-only（属无数据）
    assert dw_core._decision_periods_rain_only([{"weather": None, "rain_1h": None}]) is False
    assert dw_core._decision_periods_rain_only([]) is False


def test_rain_only_point_renders_rain_table_not_empty_weather_table():
    # 生产回归：密云水库逐日表 天气/气温/风 全 "—" 被当成"没数据"
    facts = {
        "poi": {"name": "密云水库"},
        "periods": [
            {"period_label": "08月20日-08月21日", "weather": None, "tmax": None, "tmin": None, "EDA": "", "rain_1h": 0.0},
            {"period_label": "08月21日-08月22日", "weather": None, "tmax": None, "tmin": None, "EDA": "", "rain_1h": 0.0},
            {"period_label": "08月22日-08月23日", "weather": None, "tmax": None, "tmin": None, "EDA": "", "rain_1h": 0.5},
        ],
    }
    table = dw_core._build_decision_weather_table("未来三天密云水库有降水吗", facts)
    assert "逐日降水预报" in table
    assert "降水量(毫米)" in table
    assert "0.5" in table
    assert "密云水库" in table
    assert "天气现象" not in table


def test_full_text_point_still_renders_weather_table():
    facts = {
        "poi": {"name": "天津市区"},
        "periods": [
            {"period_label": "08月20日-08月21日", "weather": "多云", "tmax": "31", "tmin": "24", "EDA": "东南风3级", "rain_1h": 0.0},
        ],
    }
    table = dw_core._build_decision_weather_table("未来三天天气", facts)
    assert "天气现象" in table and "多云" in table and "东南风3级" in table


def test_point_display_name_prefers_keyword_for_fuzzy_prefix_match():
    # 模糊命中"基名+机构后缀"（水库本体不在库、命中水库旁医院）→ 展示用户所问基名
    assert dw_core._decision_point_display_name("密云水库医院", "密云水库", "fuzzy") == "密云水库"
    # 精确命中 → 用 POI 官方名（更规范）
    assert dw_core._decision_point_display_name("天津大学(卫津路校区)", "天津大学", "exact") == "天津大学(卫津路校区)"
    # 模糊命中但名与所问差异大（昵称命中）→ 仍用 POI 名
    assert dw_core._decision_point_display_name("天津大学", "天大", "fuzzy") == "天津大学"
    # 无 POI 名回退位置名
    assert dw_core._decision_point_display_name("", "密云水库", "fuzzy") == "密云水库"


def test_rain_only_point_suppresses_visibility_placeholder_reminder():
    # 外埠点位 VISMIN=0.0 是占位值（非真 0 能见度）→ 不应误报"能见度较低"（生产踩坑）
    rain_only_periods = [
        {"period_label": "08月20日-08月21日", "weather": None, "tmax": None, "tmin": None,
         "EDA": "", "rain_1h": 0.0, "visibility_min_km": 0.0},
        {"period_label": "08月21日-08月22日", "weather": None, "tmax": None, "tmin": None,
         "EDA": "", "rain_1h": 0.0, "visibility_min_km": 0.0},
    ]
    facts = {
        "poi": {"name": "密云水库"}, "poi_category": "reservoir",
        "has_rain_signal": False, "total_rain_mm": 0.0, "periods": rain_only_periods,
    }
    reminder = dw_core._build_poi_reminder_section(facts)
    assert "能见度较低" not in reminder


def test_full_text_point_keeps_low_visibility_reminder():
    # 全要素点位真实低能见度（0.5km）→ 保留"能见度较低"提醒
    periods = [
        {"period_label": "08月20日-08月21日", "weather": "雾", "tmax": "28", "tmin": "21",
         "EDA": "东南风2级", "rain_1h": 0.0, "visibility_min_km": 0.5},
    ]
    facts = {
        "poi": {"name": "天津市区"}, "poi_category": "school",
        "has_rain_signal": False, "total_rain_mm": 0.0, "periods": periods,
    }
    reminder = dw_core._build_poi_reminder_section(facts)
    assert "能见度较低" in reminder


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_rejects_non_poi_question():
    answer_chain = None
    callbacks = {"ainvoke_chain": lambda chain, inputs: None}
    tool = dw.build_decision_weather_tools(answer_chain, [], callbacks)[0]
    result = await tool.ainvoke({"user_text": "今天天气怎么样"})
    assert isinstance(result, str)
    assert "不属于" in result or "范围" in result


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_missing_tools():
    answer_chain = None
    callbacks = {"ainvoke_chain": lambda chain, inputs: None}
    tool = dw.build_decision_weather_tools(answer_chain, [], callbacks)[0]
    result = await tool.ainvoke({"user_text": "天津大学未来24小时天气怎么样"})
    assert isinstance(result, str)
    assert "暂时不可用" in result or "缺少" in result


class FakeChain:
    def __init__(self, overrides=None):
        self._overrides = overrides or {}

    async def ainvoke(self, *args, **kwargs):
        now = datetime.now()
        payload = {
            "is_decision_weather": True,
            "location_name": "天津大学",
            "target_start_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "target_end_time": (
                now.replace(hour=0, minute=0, second=0, microsecond=0)
                + timedelta(days=2)
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "interval_hours": 24,
            "question_type": "general_weather",
            "need_clarification": False,
            "clarification_question": "",
        }
        payload.update(self._overrides)

        class Result:
            content = json.dumps(payload, ensure_ascii=False)

        return Result()


class FakePoiTool:
    name = "search_poi"

    async def ainvoke(self, args):
        return [
            {
                "text": json.dumps(
                    {
                        "pois": [
                            {
                                "name": "天津大学",
                                "address": "天津市南开区卫津路92号",
                                "longitude": 117.16,
                                "latitude": 39.11,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            }
        ]


class FakeForecastTool:
    name = "query_rolling_forecast"

    async def ainvoke(self, args):
        now = datetime.now()
        return [
            {
                "text": json.dumps(
                    {
                        "periods": [
                            {
                                "start_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                                "end_time": (
                                    now + timedelta(hours=24)
                                ).strftime("%Y-%m-%d %H:%M:%S"),
                                "region": "南开区",
                                "WEA": "晴",
                                "TMAX": 32,
                                "TMIN": 24,
                                "EDA": "东南风3级",
                                "TP1H": 0,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            }
        ]


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_happy_path():
    answer_chain = FakeChain()
    tools = [FakePoiTool(), FakeForecastTool()]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    poi_tools = dw.build_decision_weather_tools(answer_chain, tools, callbacks)
    tool = poi_tools[0]
    result = await tool.ainvoke({"user_text": "天津大学未来24小时天气怎么样"})

    assert isinstance(result, str)
    assert "天津大学" in result or "核心结论" in result


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_need_clarification():
    answer_chain = FakeChain(overrides={"need_clarification": True, "clarification_question": "请补充具体地点。"})
    tools = [FakePoiTool(), FakeForecastTool()]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    tool = dw.build_decision_weather_tools(answer_chain, tools, callbacks)[0]
    result = await tool.ainvoke({"user_text": "学校明天天气怎么样"})

    assert isinstance(result, str)
    assert "请补充具体地点" in result


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_not_decision_weather():
    answer_chain = FakeChain(overrides={"is_decision_weather": False})
    tools = [FakePoiTool(), FakeForecastTool()]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    tool = dw.build_decision_weather_tools(answer_chain, tools, callbacks)[0]
    result = await tool.ainvoke({"user_text": "天津降雨量"})

    assert isinstance(result, str)
    assert "不是" in result or "不属于" in result


def test_rule_based_slot_extraction_locations():
    """规则抽取能识别常见点位名称。"""
    assert dw_core._extract_decision_slots_rule_based("梅江会展中心明天天气怎么样")["location_name"] == "梅江会展中心"
    assert dw_core._extract_decision_slots_rule_based("天津大学未来24小时会下雨吗")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("未来24小时天津大学会下雨吗")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("梅江会展中心适合户外活动吗")["question_type"] == "activity"
    # 天/日/周 时段词不得泄漏进位置名：无具体名词后缀前内容时回退 LLM（None）
    assert dw_core._extract_decision_slots_rule_based("未来三天学校天气") is None
    # 星期词（下周一/本周五/周三/下周）同样不得泄漏进位置名——2026-08-24 生产缺陷：
    # "下周一津泰达实验学校附近天气怎么样" 把"下周一"带进 POI 检索词与表标题。
    assert dw_core._extract_decision_slots_rule_based("下周一津泰达实验学校附近天气怎么样")["location_name"] == "津泰达实验学校"
    assert dw_core._extract_decision_slots_rule_based("下周一天津大学天气怎么样")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("本周五天津大学天气")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("周三天津大学天气")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("下星期五天津大学天气")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("下周一学校天气") is None  # 剥掉星期词后无具体名词


def test_rule_based_slot_extraction_falls_back_on_ambiguous():
    """无明确后缀的模糊问题应返回 None（回退 LLM）。"""
    assert dw_core._extract_decision_slots_rule_based("学校明天天气怎么样") is None  # 后缀"学校"前无具体名词
    assert dw_core._extract_decision_slots_rule_based("天气怎么样") is None


def test_classify_poi_category_five_categories():
    """POI 地理类型分类能识别五类点位。"""
    assert dw_core.classify_poi_category("天津大学", "天津市南开区卫津路92号") == "school"
    assert dw_core.classify_poi_category("天津滨海国际机场", "") == "airport"
    assert dw_core.classify_poi_category("天津站", "天津市河北区") == "station"
    assert dw_core.classify_poi_category("盘山风景名胜区", "") == "scenic"
    assert dw_core.classify_poi_category("梅江会展中心", "") == "scenic"
    assert dw_core.classify_poi_category("某山区乡镇", "") == "mountain"


def test_classify_poi_category_known_scenic_names():
    """知名天津景点（名称无景区/公园等类别词）能关联到景区。"""
    assert dw_core.classify_poi_category("五大道", "") == "scenic"
    assert dw_core.classify_poi_category("古文化街", "") == "scenic"
    assert dw_core.classify_poi_category("意式风情区", "") == "scenic"
    assert dw_core.classify_poi_category("天津之眼", "") == "scenic"
    # 行政/服务/聚落语义不得因知名景点名单误判
    assert dw_core.classify_poi_category("五大道街道政务服务中心", "") is None
    assert dw_core.classify_poi_category("五大道派出所", "") is None
    # 裸“盘山”是蓟州名山也是辽宁县名，保守优先不归类；盘山风景名胜区走“风景名胜”关键词
    assert dw_core.classify_poi_category("盘山", "") is None
    assert dw_core.classify_poi_category("盘山县", "") is None
    assert dw_core.classify_poi_category("盘山风景名胜区", "") == "scenic"
    # 地址落入知名景区旅游区同样命中（走“旅游区”关键词，非知名名单）
    assert dw_core.classify_poi_category("某商业中心", "天津市和平区五大道文化旅游区") == "scenic"


def test_classify_poi_category_false_positives():
    """普通地名/机构不得误判为五类点位（保守优先，返回 None）。"""
    assert dw_core.classify_poi_category("石家庄", "河北省石家庄市") is None
    assert dw_core.classify_poi_category("唐山", "河北省唐山市") is None
    assert dw_core.classify_poi_category("燕山", "") is None
    assert dw_core.classify_poi_category("天津市人民医院", "") is None
    assert dw_core.classify_poi_category("西站", "") is None  # 裸"站"不命中
    assert dw_core.classify_poi_category("", "天津市和平区") is None


def test_classify_poi_category_uses_es_categories():
    """ES category_1/category_2 字段能参与分类。"""
    assert dw_core.classify_poi_category("XX综合中心", "", "学校", None) == "school"
    assert dw_core.classify_poi_category("XX活动中心", "", "风景名胜", "景点") == "scenic"


def _poi_item(name, address, lon=117.0, lat=39.0):
    return {
        "name": name,
        "address": address,
        "longitude": lon,
        "latitude": lat,
    }


def test_decision_pick_first_poi_tianjin_preference():
    """同名大众点按区域偏好优先天津，不取检索结果第一个外省同名点。"""
    payload = {"pois": [
        _poi_item("实验中学", "河北省唐山市路北区", 118.18, 39.62),
        _poi_item("实验中学", "天津市和平区", 117.19, 39.12),
    ]}
    # 无显式区域 → 默认主场天津，取天津点
    picked = dw_core._decision_pick_first_poi(payload, "实验中学")
    assert picked is not None
    assert "天津" in picked["address"]
    # 显式含“天津” → 严格过滤，外省同名点被排除
    picked2 = dw_core._decision_pick_first_poi(payload, "天津实验中学")
    assert picked2 is not None and "天津" in picked2["address"]
    # 只有外省点 + 显式天津 → 宁可查不到也不冒充
    only_hebei = {"pois": [_poi_item("实验中学", "河北省唐山市路北区", 118.18, 39.62)]}
    assert dw_core._decision_pick_first_poi(only_hebei, "天津实验中学") is None
    # 兼容旧签名：不传 keyword 也能挑第一个有效条目
    assert dw_core._decision_pick_first_poi(payload) is not None


def test_decision_pick_first_poi_hexiqu_prefers_tianjin():
    """河西中心等同名问法默认主场天津，且天津河北区点位不被误杀。"""
    payload = {"pois": [
        _poi_item("河西中心", "安徽省合肥市庐阳区", 117.28, 31.86),
        _poi_item("河西中心", "天津市河西区越秀路", 117.22, 39.11),
    ]}
    picked = dw_core._decision_pick_first_poi(payload, "河西中心")
    assert picked is not None
    assert "天津市" in picked["address"]


def test_decision_pick_first_poi_shijiazhuang_explicit_region():
    """关键词显式指定外省城市（石家庄）时，尊重该城市而非默认主场天津。"""
    payload = {"pois": [
        _poi_item("实验中学", "天津市和平区", 117.19, 39.12),
        _poi_item("实验中学", "河北省石家庄市长安区", 114.51, 38.05),
    ]}
    picked = dw_core._decision_pick_first_poi(payload, "石家庄实验中学")
    assert picked is not None
    assert "石家庄" in picked["address"]
    # 省+市连写同样解析到具体城市（先城市后省份，不命中天津河北区的“河北”）
    picked2 = dw_core._decision_pick_first_poi(payload, "河北省石家庄市实验中学")
    assert picked2 is not None
    assert "石家庄" in picked2["address"]


def test_decision_pick_first_poi_strict_excludes_outside_province():
    """显式“天津”严格过滤不得被外省同名区县（河东区-临沂等）误认成天津点。"""
    payload = {"pois": [
        _poi_item("中心医院", "山东省临沂市河东区", 118.35, 35.05),
        _poi_item("中心医院", "天津市河东区", 117.25, 39.13),
    ]}
    picked = dw_core._decision_pick_first_poi(payload, "天津中心医院")
    assert picked is not None
    assert "天津市" in picked["address"]
    # 只有外省同名区县点 + 显式天津 → 宁可查不到也不冒充
    only_linyi = {"pois": [_poi_item("实验中学", "山东省临沂市河东区", 118.35, 35.05)]}
    assert dw_core._decision_pick_first_poi(only_linyi, "天津实验中学") is None
    # 沈阳和平区同样被排除；天津“河北区”不被误杀
    only_shenyang = {"pois": [_poi_item("实验中学", "辽宁省沈阳市和平区", 123.43, 41.80)]}
    assert dw_core._decision_pick_first_poi(only_shenyang, "天津实验中学") is None
    tianjin_hebei = {"pois": [_poi_item("某中学", "河北区昆纬路", 117.20, 39.15)]}
    kept = dw_core._decision_pick_first_poi(tianjin_hebei, "天津某中学")
    assert kept is not None and "河北区" in kept["address"]


def test_poi_reminder_rain_signal_zero_mm_header():
    """has_rain_signal 有雨但累计 0mm 时，风险表标题不出现自相矛盾的“0 毫米”。"""
    facts = {
        "poi_category": "mountain",
        "has_rain_signal": True,
        "total_rain_mm": 0.0,
        "periods": [],
        "hazard_points": {
            "status": "ok", "total_found": 1, "radius_km": 5.0,
            "categories": [{"key": "dzzh", "label": "地质灾害", "count": 1, "records": []}],
        },
    }
    text = dw_core._build_poi_reminder_section(facts)
    assert "风险研判" in text
    assert "0 毫米" not in text
    assert "预计未来为小雨/有降雨" in text


def test_build_poi_reminder_section_hazard_points():
    """含周边隐患点且有降雨时输出风险研判表，不逐条列举隐患点。"""
    facts = {
        "poi_category": "scenic",
        "has_rain_signal": True,
        "total_rain_mm": 5.0,
        "periods": [{"EDA": "东南风3级", "wind": None, "visibility_min_km": None}],
        "hazard_points": {
            "status": "ok",
            "total_found": 2,
            "radius_km": 5.0,
            "categories": [
                {"key": "dzzh", "label": "地质灾害", "count": 1, "records": [
                    {"name": "滑坡隐患点A", "county": "蓟州区", "city": "天津市", "distance_km": 1.2},
                ]},
                {"key": "sh", "label": "山洪", "count": 1, "records": [
                    {"name": "山洪危险区B", "county": "宝坻区", "city": "天津市", "distance_km": 4.1},
                ]},
            ],
        },
    }
    text = dw_core._build_poi_reminder_section(facts)
    assert "【注意事项】" in text
    # 风险研判表：降雨强度 × 隐患类型 → 风险等级 + 专业建议
    assert "风险研判" in text
    assert "| 地质灾害 | 1 处 |" in text
    assert "| 山洪 | 1 处 |" in text
    assert "有降雨，注意陡坡、边坡区域湿滑" in text
    assert "有降雨，避免在山洪沟道、河谷低洼处停留" in text
    # 只给类型+数量汇总，不逐条列举隐患点（名称/位置/距离不再出现）
    assert "隐患点：" not in text
    assert "**地质灾害" not in text
    assert "滑坡隐患点A" not in text
    assert "山洪危险区B" not in text
    # 天气条件从句来自实际 facts 数值（“有降雨”不带“信号”二字）
    assert "当前预报时段内有降雨，请携带雨具" in text
    assert "降雨信号" not in text


def test_build_poi_reminder_section_empty():
    """无类别且无隐患点时返回空串（不追加注意事项）。"""
    assert dw_core._build_poi_reminder_section({"poi_category": None, "hazard_points": None}) == ""
    assert dw_core._build_poi_reminder_section({"poi_category": None, "hazard_points": {"status": "no_data", "total_found": 0}}) == ""


def test_build_poi_reminder_section_rain_risk_matrix():
    """风险研判表随降雨强度分档变化：暴雨→风险高，无雨→不出风险表。"""
    base_hazard = {
        "status": "ok",
        "total_found": 1,
        "radius_km": 5.0,
        "categories": [
            {"key": "dzzh", "label": "地质灾害", "count": 1, "records": [
                {"name": "滑坡点", "county": "蓟州区", "city": "天津市", "distance_km": 1.0},
            ]},
        ],
    }
    # 暴雨（60mm）→ 风险高
    heavy = dw_core._build_poi_reminder_section({
        "poi_category": "mountain",
        "has_rain_signal": True,
        "total_rain_mm": 60.0,
        "periods": [],
        "hazard_points": base_hazard,
    })
    assert "（暴雨）" in heavy
    assert "风险高" in heavy
    assert "暴雨极易诱发滑坡、崩塌、泥石流" in heavy
    # 无雨（0mm）→ 不出风险研判表，但带确定性风险状态（2026-08-24 用户口径）：
    # “周边 X 处隐患点 + 本次预报无明显降雨，诱发风险低”，不再是纯类别模板。
    dry = dw_core._build_poi_reminder_section({
        "poi_category": "mountain",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [],
        "hazard_points": base_hazard,
    })
    assert "【注意事项】" in dry
    assert "山区" in dry
    assert "地质灾害 1 处" in dry
    assert "短期诱发风险低" in dry
    assert "降雨时请提高警惕" in dry
    assert "可正常出行" not in dry
    assert "| 隐患类型" not in dry
    # 缺 total_rain_mm，仅 has_rain_signal=True → 兜底为小雨
    fallback = dw_core._build_poi_reminder_section({
        "poi_category": "mountain",
        "has_rain_signal": True,
        "total_rain_mm": None,
        "periods": [],
        "hazard_points": base_hazard,
    })
    assert "预计未来为小雨/有降雨" in fallback
    assert "风险较低" in fallback


def test_build_poi_reminder_section_wind_and_visibility():
    """大风/低能见度从句由预报数值派生，不编造。"""
    # 6级风 → 大风提示
    windy = dw_core._build_poi_reminder_section({
        "poi_category": "airport",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"EDA": "西北风6-7级", "visibility_min_km": None}],
        "hazard_points": None,
    })
    assert "风力较大" in windy
    # 低能见度 → 能见度提示
    foggy = dw_core._build_poi_reminder_section({
        "poi_category": "airport",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"EDA": "东南风2级", "visibility_min_km": 0.5}],
        "hazard_points": None,
    })
    assert "能见度较低" in foggy
    # 无风无雾无雨 → 只有通用提示，不含天气灾害条目（2026-08-24 天气自适应：晴天不提示降雨/大风/低能见度）
    plain = dw_core._build_poi_reminder_section({
        "poi_category": "airport",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"EDA": "东南风2级", "visibility_min_km": 10.0}],
        "hazard_points": None,
    })
    assert "风力较大" not in plain
    assert "能见度较低" not in plain
    assert "降雨" not in plain
    assert "低能见度" not in plain


def test_decision_max_wind_level_compound_wind():
    """复合风况（X～Y级转Z级 / X～Y级阵风Z级）取真实最大风力，不被区间下限低估。"""
    # 阵风 7 级 → 必须识别为 >=6 触发大风提示（修复前取区间下限 6，恰好压线；转 5 级则更明显地低估）
    gusty = dw_core._build_poi_reminder_section({
        "poi_category": "airport",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"EDA": "东南风3～4级阵风6级", "visibility_min_km": None}],
        "hazard_points": None,
    })
    assert "风力较大" in gusty

    turning = dw_core._build_poi_reminder_section({
        "poi_category": "airport",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"EDA": "南风3～4级转6级", "visibility_min_km": None}],
        "hazard_points": None,
    })
    assert "风力较大" in turning

    # 区间下限压线场景：5～6级阵风7级 真实最大 7 级
    edge = dw_core._build_poi_reminder_section({
        "poi_category": "airport",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"EDA": "东南风5～6级阵风7级", "visibility_min_km": None}],
        "hazard_points": None,
    })
    assert "风力较大" in edge


def test_build_poi_reminder_section_school_multi_item():
    """学校类别注意事项为多行条目（2026-08-24 用户反馈"注意事项太少"，从单行扩多行）。

    有降雨时：通用上下学条目 + 降雨/大风对户外活动影响条目都给出（多行）。
    """
    text = dw_core._build_poi_reminder_section({
        "poi_category": "school",
        "has_rain_signal": True,
        "total_rain_mm": 5.0,
        "periods": [{"weather": "小雨", "EDA": "北风1-2级", "visibility_min_km": 10.0}],
        "hazard_points": None,
    })
    assert "【注意事项】" in text
    assert "1. " in text and "2. " in text
    assert "学校区域" in text
    assert "上下学时段" in text
    assert "户外活动" in text
    assert "诱发风险低" not in text  # 无隐患点数据不编造风险结论


def test_build_poi_reminder_section_weather_adaptive():
    """天气自适应（2026-08-24 甲方反馈）：晴天/无雨不提示降雨、道路湿滑、低能见度；
    有雨/大风/低能见度时才给对应条目。通用安全提示（上下学/航班动态/班次动态）恒给。"""
    # 晴天周末景区（用户原话场景：没降雨全是晴天）→ 不出现降雨/道路湿滑/雷雨/能见度
    sunny_scenic = dw_core._build_poi_reminder_section({
        "poi_category": "scenic",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"weather": "晴", "EDA": "南风2级", "visibility_min_km": 10.0}],
        "hazard_points": None,
    })
    assert "降雨" not in sunny_scenic
    assert "道路湿滑" not in sunny_scenic
    assert "雷雨" not in sunny_scenic
    assert "能见度" not in sunny_scenic
    assert "游览安全" in sunny_scenic  # 通用防滑/安全提示恒给

    # 有雨景区 → 降雨/道路湿滑条目出现
    rainy_scenic = dw_core._build_poi_reminder_section({
        "poi_category": "scenic",
        "has_rain_signal": True,
        "total_rain_mm": 8.0,
        "periods": [{"weather": "小雨", "EDA": "南风2级", "visibility_min_km": 10.0}],
        "hazard_points": None,
    })
    assert "道路湿滑" in rainy_scenic

    # 雷雨景区 → 雷雨避让条目出现
    storm_scenic = dw_core._build_poi_reminder_section({
        "poi_category": "scenic",
        "has_rain_signal": True,
        "total_rain_mm": 12.0,
        "periods": [{"weather": "雷阵雨", "EDA": "南风3级", "visibility_min_km": 8.0}],
        "hazard_points": None,
    })
    assert "空旷高地" in storm_scenic

    # 无雨学校 → 不出现降雨/大风对户外活动的影响条目，但通用上下学条目恒给
    calm_school = dw_core._build_poi_reminder_section({
        "poi_category": "school",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"weather": "晴", "EDA": "北风1-2级", "visibility_min_km": 10.0}],
        "hazard_points": None,
    })
    assert "上下学时段" in calm_school
    assert "户外活动" not in calm_school
    assert "降雨" not in calm_school

    # 大风机场 → 大风相关条目出现
    windy_airport = dw_core._build_poi_reminder_section({
        "poi_category": "airport",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"weather": "晴", "EDA": "西北风6-7级", "visibility_min_km": 10.0}],
        "hazard_points": None,
    })
    assert "大风" in windy_airport

    # 无雨车站 → 不出现"雨天路滑"，但班次动态通用提示恒给
    calm_station = dw_core._build_poi_reminder_section({
        "poi_category": "station",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"weather": "晴", "EDA": "东风2级", "visibility_min_km": 10.0}],
        "hazard_points": None,
    })
    assert "雨天路滑" not in calm_station
    assert "班次动态" in calm_station


def test_build_poi_reminder_section_no_rain_with_hazard_state():
    """无雨 + 周边有隐患点 → 确定性风险状态（隐患点数量 + 诱发风险低），不出风险研判表。"""
    text = dw_core._build_poi_reminder_section({
        "poi_category": "school",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [],
        "hazard_points": {
            "status": "ok", "total_found": 2, "radius_km": 5.0,
            "categories": [
                {"key": "dzzh", "label": "地质灾害", "count": 1, "records": []},
                {"key": "sh", "label": "山洪", "count": 1, "records": []},
            ],
        },
    })
    assert "周边 5 公里内有 地质灾害 1 处、山洪 1 处" in text
    assert "本次预报无明显降雨，短期诱发风险低" in text
    assert "降雨时请提高警惕" in text
    assert "| 隐患类型" not in text  # 无雨不出表


def test_build_poi_reminder_section_no_rain_no_hazard_points():
    """无雨 + 查询成功但周边无隐患点 → “暂无已知隐患点，风险总体较低”（确定性结论）。"""
    text = dw_core._build_poi_reminder_section({
        "poi_category": "school",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [],
        "hazard_points": {"status": "ok", "total_found": 0, "radius_km": 5.0, "categories": []},
    })
    assert "周边 5 公里内暂无已知地质灾害/山洪/中小河流隐患点" in text
    assert "本次预报无明显降雨，风险总体较低" in text


def test_build_poi_reminder_section_no_rain_hazard_query_failed():
    """隐患点查询失败（None/非 ok）→ 不编造“无隐患点”，也不出风险状态。"""
    for hp in (None, {"status": "no_data", "total_found": 0}, {"status": "error"}):
        text = dw_core._build_poi_reminder_section({
            "poi_category": "school",
            "has_rain_signal": False,
            "total_rain_mm": 0.0,
            "periods": [],
            "hazard_points": hp,
        })
        assert "隐患点" not in text
        assert "风险总体较低" not in text
        assert "诱发风险低" not in text


def test_build_poi_reminder_section_historical_no_rain_risk_state():
    """历史实况无雨 → 风险状态用“当日实际”措辞（与预报措辞区分）。"""
    text = dw_core._build_poi_reminder_section({
        "poi_category": "school",
        "query_mode": "historical_obs_request",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [],
        "hazard_points": {
            "status": "ok", "total_found": 1, "radius_km": 5.0,
            "categories": [{"key": "dzzh", "label": "地质灾害", "count": 1, "records": []}],
        },
    })
    assert "当日实际无明显降雨" in text
    assert "后续降雨时请提高警惕" in text


@pytest.mark.asyncio
async def test_decision_weather_answer_reminder_position():
    """提醒插在表格与数据来源之间，数据来源恒为最后一行。"""
    now = datetime.now()
    facts = {
        "poi": {"name": "天津大学", "address": "天津市南开区卫津路92号", "lon": 117.16, "lat": 39.11},
        "question_type": "general_weather",
        "data_source": "天津市气象台滚动预报",
        "has_rain_signal": True,
        "total_rain_mm": 12.0,
        "periods": [{
            "start_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": (now + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S"),
            "region": "南开区",
            "WEA": "晴",
            "TMAX": 32,
            "TMIN": 24,
            "EDA": "东南风3级",
            "TP1H": 0,
        }],
        "poi_category": "school",
        "hazard_points": {
            "status": "ok",
            "total_found": 1,
            "radius_km": 5.0,
            "categories": [
                {"key": "zxhl", "label": "中小河流", "count": 1, "records": [
                    {"name": "沿河隐患点", "county": "南开区", "city": "天津市", "distance_km": 2.5},
                ]},
            ],
        },
    }

    class _FakeResult:
        content = "【核心结论】明天有雨，请注意带伞。"

    async def _fake_ainvoke_chain(chain, inputs):
        return _FakeResult()

    callbacks = {"ainvoke_chain": _fake_ainvoke_chain}
    result = await dw_core._generate_decision_weather_answer("天津大学未来24小时天气怎么样", facts, None, callbacks)
    assert "【注意事项】" in result
    # 提醒位于数据来源之前
    assert result.find("【注意事项】") < result.find("数据来源：")
    assert result.rstrip().endswith("数据来源：天津市气象台滚动预报。")


# ---------- 历史日期 → 历史实况查询 ----------


def test_rule_based_slot_extraction_hao_date():
    """“8月10号天津大学”规则抽取不得把日期带进位置名。"""
    assert dw_core._extract_decision_slots_rule_based("8月10号天津大学天气怎么样")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("2025年8月10日天津大学天气怎么样")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("昨天天津大学天气怎么样")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("前天梅江会展中心天气怎么样")["location_name"] == "梅江会展中心"


def test_rule_based_slot_extraction_particle_cleanup():
    """前导虚词/量词残留不得污染位置名（“前天的天津大学”的“的”、“10月份去”的“份/去”）。"""
    assert dw_core._extract_decision_slots_rule_based("前天的天津大学天气怎么样")["location_name"] == "天津大学"
    assert dw_core._extract_decision_slots_rule_based("10月份去天津大学天气怎么样")["location_name"] == "天津大学"


def test_rule_based_slot_extraction_hao_date_rain():
    """带“号”的历史日期问法仍能识别降雨意图。"""
    slots = dw_core._extract_decision_slots_rule_based("8月10号天津大学下雨了吗")
    assert slots["location_name"] == "天津大学"
    assert slots["question_type"] == "rain_now"


def test_is_past_date_forecast_payload():
    """past_date 标记识别：含 historical_window 的 dict 才算。"""
    assert dw_core._is_past_date_forecast_payload({"status": "past_date", "historical_window": {}}) is True
    assert dw_core._is_past_date_forecast_payload({"status": "past_date"}) is False
    assert dw_core._is_past_date_forecast_payload({"status": "ok", "periods": []}) is False
    assert dw_core._is_past_date_forecast_payload(None) is False
    assert dw_core._is_past_date_forecast_payload("字符串") is False


def test_decision_is_historical_facts():
    """query_mode 以 historical 开头判定为历史实况。"""
    assert dw_core._decision_is_historical_facts({"query_mode": "historical_obs"}) is True
    assert dw_core._decision_is_historical_facts({"query_mode": "calendar_daily"}) is False
    assert dw_core._decision_is_historical_facts({}) is False


# 历史实况工具 ok 返回（与 query_poi_historical_weather 结构一致）
def _historical_payload():
    return {
        "status": "ok",
        "query_type": "historical_observation",
        "query_mode": "historical_obs",
        "data_source": "自动站历史实况",
        "forecast_start_time": "2026-08-10 00:00",
        "forecast_end_time": "2026-08-11 00:00",
        "lon": 117.16,
        "lat": 39.11,
        "nearest_station": {"station_name": "天津大学站", "distance_km": 0.8},
        "periods": [
            {
                "region": "天津大学",
                "start_time": "2026-08-10 00:00",
                "end_time": "2026-08-11 00:00",
                "period_label": "8月10日",
                "weather": "多云",
                "tmax": 31.0,
                "tmin": 22.0,
                "EDA": "东南风2级",
                "wind": "东南风2级",
                "rain_1h": 2.5,
                "rainfall_mm": 2.5,
                "TP1H": 2.5,
                "visibility_min_km": 10.0,
                "sampled_hours": 4,
            }
        ],
    }


class _FakeResult:
    def __init__(self, text):
        self.content = text


async def _fake_answer(text):
    async def _fn(chain, inputs):
        return _FakeResult(text)
    return _fn


@pytest.mark.asyncio
async def test_generate_decision_historical_answer_no_data_message():
    """历史实况 no_data → 明确提示该日不可查，不编造天气；真实工具只有 start_time 也能带出日期。"""
    # 真实 _no_data_payload 结构：只有 start_time/end_time，无 forecast_start_time
    payload = {
        "status": "no_data",
        "query_mode": "historical_obs",
        "start_time": "2026-08-10 00:00",
        "end_time": "2026-08-11 00:00",
        "message": "未查询到历史实况数据。",
    }
    text = await dw_core._generate_decision_historical_answer(
        "8月10号天津大学天气怎么样", payload, {}, "天津大学", "general_weather", None, {}
    )
    assert "2026-08-10" in text
    assert "暂无可用历史实况数据" in text
    assert "换用未来日期" in text


@pytest.mark.asyncio
async def test_generate_decision_historical_answer_error_message():
    """历史实况 error → 通用不可用提示。"""
    text = await dw_core._generate_decision_historical_answer(
        "8月10号天津大学天气怎么样", {"status": "error", "query_mode": "historical_obs"}, {}, "天津大学", "general_weather", None, {}
    )
    assert "历史实况查询暂不可用" in text


@pytest.mark.asyncio
async def test_generate_decision_historical_answer_ok_title_and_no_reminder():
    """历史实况 ok 未传隐患上下文 → 表格标题【X历史实况】、无注意事项、数据来源为历史实况。"""
    callbacks = {"ainvoke_chain": await _fake_answer("【核心结论】8月10日天津大学以多云为主，最高气温31℃，最低气温22℃，实际有2.5毫米降雨。")}
    text = await dw_core._generate_decision_historical_answer(
        "8月10号天津大学天气怎么样",
        _historical_payload(),
        {"address": "天津市南开区卫津路92号", "longitude": 117.16, "latitude": 39.11},
        "天津大学",
        "general_weather",
        None,
        callbacks,
    )
    assert "【核心结论】" in text
    assert "【天津大学历史实况】" in text
    assert "【注意事项】" not in text  # 未传 poi_category/hazard_points 时不追加；有隐患上下文时见 *_with_hazard_reminder 用例
    assert "数据来源：自动站历史实况。" in text
    assert text.rstrip().endswith("数据来源：自动站历史实况。")


@pytest.mark.asyncio
async def test_decision_historical_answer_prompt_wording():
    """历史实况回答 prompt 强制“实况/当日实际”措辞，禁止“预计/将”。"""
    captured = {}

    async def _capture(chain, inputs):
        captured["prompt"] = inputs["messages"][0].content
        return _FakeResult("【核心结论】8月10日天津大学以多云为主，实际有2.5毫米降雨。")

    text = await dw_core._generate_decision_weather_answer(
        "8月10号天津大学天气怎么样",
        {
            "poi": {"name": "天津大学", "address": "天津市南开区卫津路92号", "lon": 117.16, "lat": 39.11},
            "question_type": "general_weather",
            "query_mode": "historical_obs",
            "data_source": "自动站历史实况",
            "target_start_time": "2026-08-10 00:00",
            "target_end_time": "2026-08-11 00:00",
            "has_rain_signal": True,
            "total_rain_mm": 2.5,
            "periods": [
                {
                    "start_time": "2026-08-10 00:00",
                    "end_time": "2026-08-11 00:00",
                    "period_label": "8月10日",
                    "weather": "多云",
                    "tmax": "31",
                    "tmin": "22",
                    "EDA": "东南风2级",
                }
            ],
        },
        None,
        {"ainvoke_chain": _capture},
    )
    assert "8月10日实际" in captured["prompt"]
    assert "实况/实际/当日" in captured["prompt"]
    assert "不得使用“预计/将/未来”" in captured["prompt"]
    assert "【天津大学历史实况】" in text
    assert "【注意事项】" not in text


class FakePastForecastTool:
    """返回 past_date 标记的滚动预报工具，模拟“8月10号”历史日期。"""
    name = "query_rolling_forecast"

    async def ainvoke(self, args):
        return [{
            "text": json.dumps({
                "status": "past_date",
                "query_mode": "historical_obs_request",
                "data_source": "历史日期",
                "message": "该日期已属过去，请调用 query_poi_historical_weather 查询历史实况。",
                "historical_window": {
                    "target_start": "2026-08-10 00:00",
                    "target_end": "2026-08-11 00:00",
                    "forecast_start_date": "2026-08-10",
                    "forecast_days": 1,
                },
                "periods": [],
            }, ensure_ascii=False)
        }]


class FakeHistoricalTool:
    name = "query_poi_historical_weather"

    def __init__(self, payload=None):
        self._payload = payload if payload is not None else _historical_payload()
        self.last_args = None

    async def ainvoke(self, args):
        self.last_args = args
        return [{"text": json.dumps(self._payload, ensure_ascii=False)}]


class FakeHazardTool:
    """返回周边隐患点 ok 载荷的假工具，用于验证历史路由同样查询隐患点。"""
    name = "query_poi_hazard_reminders"

    def __init__(self, payload=None):
        self._payload = payload if payload is not None else {
            "status": "ok",
            "query_type": "poi_hazard_reminders",
            "total_found": 2,
            "categories": [
                {"key": "dzzh", "label": "地质灾害", "count": 1},
                {"key": "sh", "label": "山洪", "count": 1},
            ],
        }
        self.last_args = None

    async def ainvoke(self, args):
        self.last_args = args
        return [{"text": json.dumps(self._payload, ensure_ascii=False)}]


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_routes_to_historical():
    """决策天气工具遇 past_date 标记 → 自动调历史实况工具并生成历史实况回答。"""
    answer_chain = FakeChain()
    tools = [FakePoiTool(), FakePastForecastTool(), FakeHistoricalTool()]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    tool = dw.build_decision_weather_tools(answer_chain, tools, callbacks)[0]
    result = await tool.ainvoke({"user_text": "8月10号天津大学天气怎么样"})

    assert isinstance(result, str)
    assert "【天津大学历史实况】" in result
    assert "数据来源：自动站历史实况。" in result


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_historical_tool_missing():
    """历史实况工具缺失时给出明确提示，不静默回退未来预报。"""
    answer_chain = FakeChain()
    tools = [FakePoiTool(), FakePastForecastTool()]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    tool = dw.build_decision_weather_tools(answer_chain, tools, callbacks)[0]
    result = await tool.ainvoke({"user_text": "8月10号天津大学天气怎么样"})

    assert isinstance(result, str)
    assert "历史" in result
    assert "暂不可用" in result


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_historical_no_data_keeps_message():
    """历史实况 no_data 时明确提示该日不可查，不得回退未来预报。"""
    answer_chain = FakeChain()
    tools = [FakePoiTool(), FakePastForecastTool(), FakeHistoricalTool({
        "status": "no_data",
        "query_mode": "historical_obs",
        "forecast_start_time": "2026-08-10 00:00",
        "message": "未查询到历史实况数据。",
    })]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    tool = dw.build_decision_weather_tools(answer_chain, tools, callbacks)[0]
    result = await tool.ainvoke({"user_text": "8月10号天津大学天气怎么样"})

    assert isinstance(result, str)
    assert "暂无可用历史实况数据" in result
    assert "未来日期" in result


# ---------- fast path（DecisionWeatherQAService）历史路由 parity ----------

import chainlitexam.tools.decision_weather_fast_path as dw_fp  # noqa: E402


@pytest.mark.asyncio
async def test_fast_path_routes_to_historical(monkeypatch):
    """fast path 遇 past_date 标记同样自动转历史实况工具，与 planner 工具行为一致。"""
    # _emit 尾部写会话消息需要 chainlit 上下文，测试里打成 no-op
    monkeypatch.setattr(dw_fp.cl.user_session, "set", lambda *a, **k: None)
    answer_chain = FakeChain()
    historical_tool = FakeHistoricalTool()
    fake_tools = {
        "search_poi": FakePoiTool(),
        "query_rolling_forecast": FakePastForecastTool(),
        "query_poi_historical_weather": historical_tool,
    }
    emitted = {}

    class _Runtime:
        def find_tool(self, tools, name):
            return fake_tools.get(name)

        async def invoke_fast_tool(self, name, tool, args, user_text):
            return await tool.ainvoke(args)

        def clean_table_cell(self, value):
            return dw_core._decision_table_cell(value)

        def sanitize_display_text(self, text):
            return text

        def prepend_thinking_summary(self, text, user_text, has_chart=False):
            return text

    async def _emit_fn(text):
        emitted["text"] = text

    callbacks = {
        "ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke(),
        "append_followup_if_needed": lambda text, user_text: text,
        "stream_text_to_message": _emit_fn,
    }
    runtime = _Runtime()
    service = dw_fp.DecisionWeatherQAService(answer_chain, list(fake_tools.values()), callbacks, runtime)
    handled = await service.try_handle("8月10号天津大学天气怎么样", [])
    assert handled is True
    assert "【天津大学历史实况】" in emitted["text"]
    assert "数据来源：自动站历史实况。" in emitted["text"]
    # 历史工具收到 historical_window 的起止时间
    assert historical_tool.last_args is not None
    assert historical_tool.last_args["start_time"] == "2026-08-10 00:00"
    assert historical_tool.last_args["end_time"] == "2026-08-11 00:00"


# ---------- 历史实况提醒（注意事项）：历史式措辞 + 隐患点接线（双入口 parity） ----------


def test_build_poi_reminder_section_historical_wording():
    """历史实况的注意事项用“当日实际/累计”等过去措辞，不使用“预计/未来”。"""
    facts = {
        "poi_category": "school",
        "query_mode": "historical_obs",
        "has_rain_signal": True,
        "total_rain_mm": 2.5,
        "target_start_time": "2026-08-10 00:00",
        "target_end_time": "2026-08-11 00:00",
        "periods": [{"rainfall_mm": 2.5}],
        "hazard_points": None,
    }
    text = dw_core._build_poi_reminder_section(facts)
    assert "【注意事项】" in text
    assert "当日实际有降雨" in text
    assert "当前预报时段内" not in text
    assert "预计" not in text
    assert "未来" not in text


def test_build_poi_reminder_section_historical_rain_amount_clause():
    """历史实况累计降雨 ≥10mm → “当日累计降雨约 X 毫米”措辞，不用“未来累计降雨可达”。"""
    facts = {
        "poi_category": "scenic",
        "query_mode": "historical_obs",
        "has_rain_signal": False,
        "total_rain_mm": 30.0,
        "target_start_time": "2026-08-10 00:00",
        "target_end_time": "2026-08-11 00:00",
        "periods": [{"rainfall_mm": 30.0}],
        "hazard_points": None,
    }
    text = dw_core._build_poi_reminder_section(facts)
    assert "当日累计降雨约 30 毫米" in text
    assert "未来累计降雨可达" not in text


def test_build_poi_reminder_section_historical_hazard_table():
    """历史实况含隐患点 → 风险研判表头用“当日实际降雨”，只含历史式措辞。"""
    facts = {
        "poi_category": "mountain",
        "query_mode": "historical_obs",
        "has_rain_signal": True,
        "total_rain_mm": 30.0,
        "target_start_time": "2026-08-10 00:00",
        "target_end_time": "2026-08-11 00:00",
        "periods": [{"rainfall_mm": 30.0}],
        "hazard_points": {
            "status": "ok",
            "total_found": 3,
            "categories": [
                {"key": "dzzh", "label": "地质灾害", "count": 2},
                {"key": "sh", "label": "山洪", "count": 1},
            ],
        },
    }
    text = dw_core._build_poi_reminder_section(facts)
    assert "【注意事项】" in text
    assert "当日实际降雨约 30 毫米（大雨），周边灾害风险研判如下：" in text
    assert "| 地质灾害 | 2 处 |" in text
    assert "| 山洪 | 1 处 |" in text
    assert "预计未来" not in text


def test_build_poi_reminder_section_historical_multi_day_label():
    """多日历史时段用“该时段”指代日期，避免“当日”误导。"""
    facts = {
        "poi_category": "school",
        "query_mode": "historical_obs",
        "has_rain_signal": True,
        "total_rain_mm": 15.0,
        "target_start_time": "2026-08-10 00:00",
        "target_end_time": "2026-08-13 00:00",
        "periods": [{"rainfall_mm": 5.0}, {"rainfall_mm": 10.0}],
        "hazard_points": None,
    }
    text = dw_core._build_poi_reminder_section(facts)
    assert "该时段实际有降雨" in text
    assert "当日" not in text


@pytest.mark.asyncio
async def test_generate_decision_historical_answer_with_hazard_reminder():
    """历史实况回答传入 poi_category+hazard_points → 追加注意事项（当日实际措辞）。"""
    callbacks = {"ainvoke_chain": await _fake_answer("【核心结论】8月10日天津大学实际有2.5毫米降雨。")}
    text = await dw_core._generate_decision_historical_answer(
        "8月10号天津大学天气怎么样",
        _historical_payload(),
        {"address": "天津市南开区卫津路92号", "longitude": 117.16, "latitude": 39.11},
        "天津大学",
        "general_weather",
        None,
        callbacks,
        poi_category="school",
        hazard_points={
            "status": "ok",
            "total_found": 1,
            "categories": [{"key": "dzzh", "label": "地质灾害", "count": 1}],
        },
    )
    assert "【核心结论】" in text
    assert "【天津大学历史实况】" in text
    assert "【注意事项】" in text
    assert "风险研判" in text
    assert "当日实际" in text
    assert "预计" not in text
    assert "未来" not in text
    assert text.rstrip().endswith("数据来源：自动站历史实况。")


@pytest.mark.asyncio
async def test_query_decision_weather_for_poi_historical_routes_with_hazard():
    """历史日期路由同样分类并查询隐患点 → 回答追加注意事项（风险研判）。"""
    answer_chain = FakeChain()
    hazard_tool = FakeHazardTool()
    tools = [FakePoiTool(), FakePastForecastTool(), FakeHistoricalTool(), hazard_tool]
    callbacks = {"ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke()}

    tool = dw.build_decision_weather_tools(answer_chain, tools, callbacks)[0]
    result = await tool.ainvoke({"user_text": "8月10号天津大学天气怎么样"})

    assert isinstance(result, str)
    assert "【天津大学历史实况】" in result
    assert "【注意事项】" in result
    assert "风险研判" in result
    assert "当日实际" in result
    # 隐患工具收到点位坐标
    assert hazard_tool.last_args is not None
    assert hazard_tool.last_args["lon"] == 117.16
    assert hazard_tool.last_args["lat"] == 39.11


@pytest.mark.asyncio
async def test_fast_path_routes_to_historical_with_hazard(monkeypatch):
    """fast path 历史路由同样分类并查询隐患点 → 注意事项（与 planner 工具一致）。"""
    monkeypatch.setattr(dw_fp.cl.user_session, "set", lambda *a, **k: None)
    answer_chain = FakeChain()
    hazard_tool = FakeHazardTool()
    fake_tools = {
        "search_poi": FakePoiTool(),
        "query_rolling_forecast": FakePastForecastTool(),
        "query_poi_historical_weather": FakeHistoricalTool(),
        "query_poi_hazard_reminders": hazard_tool,
    }
    emitted = {}

    class _Runtime:
        def find_tool(self, tools, name):
            return fake_tools.get(name)

        async def invoke_fast_tool(self, name, tool, args, user_text):
            return await tool.ainvoke(args)

        def clean_table_cell(self, value):
            return dw_core._decision_table_cell(value)

        def sanitize_display_text(self, text):
            return text

        def prepend_thinking_summary(self, text, user_text, has_chart=False):
            return text

    async def _emit_fn(text):
        emitted["text"] = text

    callbacks = {
        "ainvoke_chain": lambda chain, inputs: answer_chain.ainvoke(),
        "append_followup_if_needed": lambda text, user_text: text,
        "stream_text_to_message": _emit_fn,
    }
    runtime = _Runtime()
    service = dw_fp.DecisionWeatherQAService(answer_chain, list(fake_tools.values()), callbacks, runtime)
    handled = await service.try_handle("8月10号天津大学天气怎么样", [])
    assert handled is True
    assert "【注意事项】" in emitted["text"]
    assert "风险研判" in emitted["text"]
    assert "当日实际" in emitted["text"]
    assert hazard_tool.last_args is not None
    assert hazard_tool.last_args["lon"] == 117.16
    assert hazard_tool.last_args["lat"] == 39.11


class TestPoiCategoryExtended:
    """港口/水库/山洪区 POI 分类（用户要求的天津港、密云水库、蓟州场景）。"""

    def test_tianjin_port(self):
        assert dw_core.classify_poi_category("天津港", "天津市滨海新区") == "port"
        assert dw_core.classify_poi_category("天津新港", "") == "port"
        assert dw_core.classify_poi_category("塘沽港", "") == "port"
        assert dw_core.classify_poi_category("天津港码头", "") == "port"
        assert dw_core.classify_poi_category("天津港保税区", "") == "port"

    def test_reservoir(self):
        assert dw_core.classify_poi_category("密云水库", "北京市密云区") == "reservoir"
        assert dw_core.classify_poi_category("于桥水库", "天津市蓟州区") == "reservoir"

    def test_jizhou_mountain_risk(self):
        assert dw_core.classify_poi_category("蓟州", "天津市蓟州区") == "mountain"
        assert dw_core.classify_poi_category("蓟县", "") == "mountain"

    def test_existing_categories_unchanged(self):
        assert dw_core.classify_poi_category("盘山风景名胜区", "") == "scenic"
        assert dw_core.classify_poi_category("泰达航母主题公园", "") == "scenic"
        assert dw_core.classify_poi_category("天津大学", "") == "school"
        assert dw_core.classify_poi_category("盘山", "") is None
        assert dw_core.classify_poi_category("石家庄", "河北省石家庄市") is None


class TestPoiReminderExtended:
    def test_reservoir_water_level_reminder(self):
        # 水库：实际水位 + 洪水/山洪风险研判（按水位距汛限余量分档），不走通用单行模板
        facts = {
            "poi_category": "reservoir",
            "has_rain_signal": True,
            "periods": [],
            "total_rain_mm": 1.0,
            "water_level_info": {
                "reservoir_name": "密云水库",
                "water_level_m": "133.5",
                "flood_limit_m": "152.0",
                "storage": "12.3",
                "outflow_m3s": "45.0",
            },
        }
        out = dw_core._build_poi_reminder_section(facts)
        assert "1. 目前密云水库库上水位约 133.5 米（汛限水位 152.0 米）" in out
        assert "蓄水量约 12.3 百万立方米" in out
        assert "出库流量约 45.0 立方米/秒" in out
        # 余量 = 152.0 - 133.5 = 18.5 米 → 余量充足 + 有降雨 → 山洪风险研判
        assert "低于汛限水位约 18.5 米" in out
        assert "山洪风险" in out

    def test_port_reminder(self):
        facts = {"poi_category": "port", "has_rain_signal": False, "periods": [], "total_rain_mm": None}
        out = dw_core._build_poi_reminder_section(facts)
        # 港口注意事项为确定性多句：风力系泊 + 跟踪预警（无雾/雷阵雨天数时分现象句省略）
        assert "关注风力变化，适时调整缆绳，做好系泊加固" in out
        assert "保障港口生产航行安全" in out

    def test_reservoir_without_water_info_no_crash(self):
        # 无水位数据：不编造水位，只按降雨给一般洪水/山洪研判
        facts = {"poi_category": "reservoir", "has_rain_signal": False, "periods": [], "total_rain_mm": None}
        out = dw_core._build_poi_reminder_section(facts)
        assert "【注意事项】" in out
        assert "库上水位" not in out, "无水位数据时不应编造水位"
        assert "未来无明显降雨，短期库区水位预计平稳" in out
        assert "山洪风险" in out

    def test_reservoir_no_rain_with_water_info_gives_risk_judgment(self):
        # 无降雨但有水位数据：水位照常 + 余量分档研判（无雨时不给"水位上涨"警告，防与结论矛盾）
        facts = {
            "poi_category": "reservoir",
            "has_rain_signal": False,
            "periods": [],
            "total_rain_mm": None,
            "water_level_info": {
                "reservoir_name": "密云水库",
                "water_level_m": "133.5",
                "flood_limit_m": "152.0",
                "storage": "12.3",
                "outflow_m3s": "45.0",
            },
        }
        out = dw_core._build_poi_reminder_section(facts)
        assert "【注意事项】" in out
        assert "1. 目前密云水库库上水位约 133.5 米（汛限水位 152.0 米）" in out
        assert "2. 蓄水量约 12.3 百万立方米" in out
        assert "3. 出库流量约 45.0 立方米/秒" in out
        # 余量 18.5 米 + 无雨 → 平稳研判，不警告水位上涨
        assert "低于汛限水位约 18.5 米" in out
        assert "短期库区水位预计平稳" in out
        assert "水位上涨" not in out, "无降雨时不应警告水位上涨"


class TestFetchWaterLevel:
    @pytest.mark.asyncio
    async def test_fetch_water_level_ok(self):
        class _Tool:
            async def ainvoke(self, args):
                return {
                    "data_type": "reservoir",
                    "count": 1,
                    "records": [{
                        "station_name": "密云水库",
                        "time": "2026-08-17 15:00:00",
                        "water_level_m": "133.5",
                        "汛限水位(m)": "152.0",
                        "蓄水量(百万m³)": "12.3",
                        "出库流量(m³/s)": "45.0",
                    }],
                    "source": "十四所水位接口",
                }
        info = await dw_core._decision_fetch_water_level("密云水库", _Tool(), lambda t, a: t.ainvoke(a))
        assert info is not None
        assert info["reservoir_name"] == "密云水库"
        assert info["water_level_m"] == "133.5"
        assert info["flood_limit_m"] == "152.0"

    @pytest.mark.asyncio
    async def test_fetch_water_level_no_tool(self):
        assert await dw_core._decision_fetch_water_level("密云水库", None, lambda t, a: t.ainvoke(a)) is None

    @pytest.mark.asyncio
    async def test_fetch_water_level_error_payload(self):
        class _Tool:
            async def ainvoke(self, args):
                return {"error": "水位服务请求超时", "data_type": "reservoir"}
        assert await dw_core._decision_fetch_water_level("密云水库", _Tool(), lambda t, a: t.ainvoke(a)) is None

    @pytest.mark.asyncio
    async def test_fetch_water_level_exception(self):
        class _Tool:
            async def ainvoke(self, args):
                raise RuntimeError("boom")
        assert await dw_core._decision_fetch_water_level("密云水库", _Tool(), lambda t, a: t.ainvoke(a)) is None

    @pytest.mark.asyncio
    async def test_fetch_water_level_empty_records(self):
        class _Tool:
            async def ainvoke(self, args):
                return {"data_type": "reservoir", "count": 0, "records": [], "source": "x"}
        assert await dw_core._decision_fetch_water_level("密云水库", _Tool(), lambda t, a: t.ainvoke(a)) is None



class TestEcRainFallbackAnswer:
    """超 240h 点位日期的 EC 降水回退回答（Task 6）：只讲降雨、零编造其他要素。"""

    EC_PAYLOAD = {
        "status": "ec_rain_fallback",
        "target_date": "2026-09-01",
        "rain_mm": 5.0,
        "window_hours": 24,
        "data_source": "ECMWF AIFS",
    }

    def test_is_ec_rain_fallback_payload(self):
        assert dw_core._is_ec_rain_fallback_payload(self.EC_PAYLOAD) is True
        assert dw_core._is_ec_rain_fallback_payload({"status": "ok"}) is False
        assert dw_core._is_ec_rain_fallback_payload({"status": "past_date"}) is False
        assert dw_core._is_ec_rain_fallback_payload(None) is False

    def test_ec_answer_rain_positive_no_fabrication(self):
        text = dw_core._build_ec_rain_answer_text(self.EC_PAYLOAD, "密云水库")
        assert "9月1日" in text and "5.0" in text
        assert "ECMWF AIFS" in text
        assert "气温" in text and "暂无法提供" in text
        assert "预计有降雨" in text
        # 绝不出现气温/风力区间（零编造）
        assert "~" not in text
        assert "级" not in text

    def test_ec_answer_zero_rain(self):
        p = dict(self.EC_PAYLOAD, rain_mm=0.0)
        text = dw_core._build_ec_rain_answer_text(p, "密云水库")
        assert "无明显降雨" in text
        assert "~" not in text

    def test_ec_answer_bad_target_date_falls_back(self):
        p = dict(self.EC_PAYLOAD, target_date="not-a-date")
        text = dw_core._build_ec_rain_answer_text(p, "密云水库")
        assert "not-a-date" in text


class TestDecisionAnswerQualityBatch:
    """2026-08-19 问答质量批：日期当天/逐日结论/无明显降雨/剔能见度·降水量/山地·港口注意事项/POI 类别不符显示名。"""

    # #2 表格日期只显示当天
    def test_period_label_daily_shows_single_day(self):
        assert dw_core._decision_period_label({"period_label": "08月22日-08月23日"}) == "08月22日"

    def test_period_label_hourly_keeps_time_range(self):
        label = dw_core._decision_period_label({"period_label": "08月22日14时-08月22日15时"})
        assert "时" in label and "-" in label

    def test_period_label_daily_from_start_end(self):
        period = {"start_time": "2026-08-22 08:00", "end_time": "2026-08-23 08:00"}
        assert dw_core._decision_period_label(period) == "8月22日"

    # #5 无明显降雨
    def test_polish_core_replaces_no_rain_phrasing(self):
        assert dw_core._polish_decision_core("预计未来三天不会下雨。") == "预计未来三天无明显降雨。"
        assert "无明显降雨" in dw_core._polish_decision_core("明天无降雨。")

    # #8 剔除能见度/累计降水量具体数值
    def test_polish_core_strips_visibility_and_precip(self):
        core = dw_core._polish_decision_core(
            "气温在24~31°C之间，能见度最低降至2.6千米。"
        )
        assert "能见度" not in core
        core2 = dw_core._polish_decision_core(
            "未来三天多雷阵雨，累计降水量约2.6毫米。"
        )
        assert "累计降水量" not in core2
        assert "2.6" not in core2

    # #3 逐日结论保留多句
    def test_core_only_keeps_multi_sentence_when_multiday(self):
        answer = "【核心结论】20日多云转阴。21日雷阵雨。22日雷阵雨转阴。气温24~31°C。"
        core = dw_core._decision_core_only(answer, "未来三天天气", max_sentences=4)
        assert "20日多云转阴" in core and "22日雷阵雨转阴" in core

    def test_core_only_single_day_truncates_to_first(self):
        answer = "【核心结论】明天阴转小雨。户外适宜性一般。"
        core = dw_core._decision_core_only(answer, "明天天气", max_sentences=1)
        assert core == "明天阴转小雨。"

    # #1 POI 类别不符的模糊命中 → 显示用户所问名
    def test_display_name_category_mismatch_uses_location(self):
        out = dw_core._decision_point_display_name(
            "泰达控股天津泰达电力公司", "天津泰达实验学校", "fuzzy"
        )
        assert out == "天津泰达实验学校"

    def test_display_name_same_category_keeps_poi(self):
        # 同类别（学校）模糊命中仍用官方 POI 名
        out = dw_core._decision_point_display_name(
            "天津泰达实验学校（东校区）", "天津泰达实验学校", "fuzzy"
        )
        assert out == "天津泰达实验学校"  # 前缀命中，展示所问基名

    def test_display_name_abbreviation_not_regressed(self):
        # “天大”无类别词 → 不触发类别改写，保留官方名“天津大学”
        out = dw_core._decision_point_display_name("天津大学", "天大", "fuzzy")
        assert out == "天津大学"

    # #6 山区注意事项
    def test_mountain_reminder_rain(self):
        facts = {"poi_category": "mountain", "has_rain_signal": True,
                 "periods": [{"weather": "阴转小雨", "rain_1h": 2.0}], "total_rain_mm": 2.0}
        out = dw_core._build_poi_reminder_section(facts)
        assert "不建议登山、溯溪、野外徒步" in out
        assert "山洪、落石隐患" in out
        assert "防滑鞋" in out

    def test_mountain_reminder_no_rain(self):
        facts = {"poi_category": "mountain", "has_rain_signal": False,
                 "periods": [{"weather": "晴", "rain_1h": 0.0}], "total_rain_mm": None}
        out = dw_core._build_poi_reminder_section(facts)
        assert "量力而行" in out
        assert "山洪" not in out

    # #7 港口注意事项（分现象分天）
    def test_port_reminder_per_phenomenon(self):
        periods = [
            {"weather": "多云转阴有轻雾", "rain_1h": 0.0, "start_time": "2026-08-20 08:00", "end_time": "2026-08-21 08:00"},
            {"weather": "阴有轻雾", "rain_1h": 0.0, "start_time": "2026-08-21 08:00", "end_time": "2026-08-22 08:00"},
            {"weather": "雷阵雨", "rain_1h": 2.6, "start_time": "2026-08-22 08:00", "end_time": "2026-08-23 08:00"},
        ]
        facts = {"poi_category": "port", "has_rain_signal": True, "periods": periods, "total_rain_mm": 2.6}
        out = dw_core._build_poi_reminder_section(facts)
        assert "20–21日" in out and "轻雾" in out and "加强瞭望" in out
        assert "22日" in out and "雷阵雨" in out and "防雨防雷" in out
        assert "适时调整缆绳" in out
        assert "码头路面湿滑" in out
        assert "保障港口生产航行安全" in out

    def test_day_ranges_grouping(self):
        assert dw_core._decision_day_ranges([20, 21, 23]) == "20–21日、23日"
        assert dw_core._decision_day_ranges([22]) == "22日"
        assert dw_core._decision_day_ranges([]) == ""


class TestDecisionTargetDayScoping:
    """单日问法（下周一/明天/具体日期）把结论/表格/注意事项聚焦到所问那天。"""

    NOW = dw_core.datetime(2026, 8, 19)  # 周三

    @pytest.fixture(autouse=True)
    def _pin_now(self, monkeypatch):
        # scope 函数内部走 _decision_now_bjt()（真实时间）——钉死为 2026-08-19（周三），
        # 否则日历一走动"下周一"的解析目标就漂移（2026-08-24 起该测试曾因此失效）。
        monkeypatch.setattr(
            dw_core,
            "_decision_now_bjt",
            lambda: dw_core.datetime(2026, 8, 19, 12, 0, tzinfo=dw_core.timezone(dw_core.timedelta(hours=8))),
        )

    def test_target_dates_next_monday(self):
        out = dw_core._decision_target_dates("下周一天津泰达实验学校附近天气怎么样？", self.NOW)
        assert out == {dw_core.date(2026, 8, 24)}

    def test_target_dates_tomorrow(self):
        assert dw_core._decision_target_dates("明天XX学校天气", self.NOW) == {dw_core.date(2026, 8, 20)}

    def test_target_dates_multi_day_returns_none(self):
        assert dw_core._decision_target_dates("未来三天XX学校天气", self.NOW) is None
        assert dw_core._decision_target_dates("本周末XX公园适合玩吗", self.NOW) is None

    def test_target_dates_explicit_date(self):
        assert dw_core._decision_target_dates("8月22日XX学校天气", self.NOW) == {dw_core.date(2026, 8, 22)}

    def _period(self, day, hour_start, hour_end, weather, rain):
        return {
            "start_time": f"2026-08-{day:02d} {hour_start:02d}:00",
            "end_time": f"2026-08-{day if hour_end>hour_start else day+1:02d} {hour_end:02d}:00",
            "weather": weather, "rain_1h": rain, "tmax": 30, "tmin": 24, "EDA": "南风1-2级",
        }

    def test_scope_filters_to_monday_and_recomputes_rain(self):
        periods = [
            self._period(21, 8, 20, "雷阵雨", 5.0),   # 周五有雨
            self._period(24, 8, 20, "晴间多云", 0.0),  # 周一白天 晴
            self._period(24, 20, 8, "晴间多云", 0.0),  # 周一夜间 晴
        ]
        facts = {"periods": periods, "has_rain_signal": True, "total_rain_mm": 5.0}
        scoped = dw_core._decision_scope_facts_to_target_dates(facts, "下周一天气怎么样")
        assert len(scoped["periods"]) == 2
        # 周一无雨 → 降雨信号重算为 False、不再误报"有降雨"
        assert scoped["has_rain_signal"] is False
        assert scoped["total_rain_mm"] == 0.0  # 与 _compact_decision_forecast_facts 全 0 口径一致

    def test_scope_no_match_keeps_all(self):
        periods = [self._period(19, 8, 20, "晴", 0.0)]
        facts = {"periods": periods, "has_rain_signal": False, "total_rain_mm": None}
        scoped = dw_core._decision_scope_facts_to_target_dates(facts, "下周一天气")
        assert len(scoped["periods"]) == 1  # 周一不在窗口 → 不过滤

    def test_period_label_day_night(self):
        day = {"start_time": "2026-08-24 08:00", "end_time": "2026-08-24 20:00"}
        night = {"start_time": "2026-08-24 20:00", "end_time": "2026-08-25 08:00"}
        assert dw_core._decision_period_label(day) == "8月24日白天"
        assert dw_core._decision_period_label(night) == "8月24日夜间"


class TestUniformPerdayCollapse:
    """多日天气相同/均无雨时，核心结论不逐日重复（防死板）。"""

    def test_uniform_no_rain_collapsed(self):
        answer = "【核心结论】未来三天密云水库无明显降雨。8月20日密云水库无明显降雨，21日无明显降雨，22日无明显降雨。"
        core = dw_core._decision_core_only(answer, "未来三天密云水库有降水吗", max_sentences=4)
        assert core == "未来三天密云水库无明显降雨。"
        assert core.count("无明显降雨") == 1

    def test_varied_days_kept(self):
        answer = "【核心结论】未来三天多阵性降水。20日多云转阴，21日雷阵雨，22日雷阵雨转阴。"
        core = dw_core._decision_core_only(answer, "未来三天天气", max_sentences=4)
        assert "21日雷阵雨" in core and "22日雷阵雨转阴" in core

    def test_uniform_detector(self):
        assert dw_core._uniform_perday_descriptor("20日晴，21日晴，22日晴") == "晴"
        assert dw_core._uniform_perday_descriptor("20日多云转阴，21日雷阵雨") is None


class TestReminderNumbering:
    """注意事项用【注意事项】标题 + 1. 2. 3. 编号。"""

    def test_reservoir_items_numbered(self):
        facts = {
            "poi_category": "reservoir", "has_rain_signal": True,
            "periods": [{"weather": "小雨", "rain_1h": 1.0, "EDA": "南风1-2级"}],
            "total_rain_mm": 1.0,
            "water_level_info": {
                "reservoir_name": "密云水库白河坝上", "water_level_m": 150.23,
                "flood_limit_m": 154.0, "storage": 2762.3, "outflow_m3s": 44.7,
            },
        }
        out = dw_core._build_poi_reminder_section(facts)
        assert out.startswith("【注意事项】")
        assert "1. 目前密云水库白河坝上库上水位约 150.23 米（汛限水位 154.0 米）" in out
        assert "2. 蓄水量约 2762.3 百万立方米" in out
        assert "3. 出库流量约 44.7 立方米/秒" in out
        # 4. 风险研判：余量 3.8 米（充足档）+ 有降雨 → 山洪风险
        assert "低于汛限水位约 3.8 米" in out
        assert "山洪风险" in out
        assert "水库区域" not in out, "水库不走通用单行模板"

    def test_port_items_numbered(self):
        periods = [
            {"weather": "多云转阴有轻雾", "rain_1h": 0.0, "start_time": "2026-08-20 08:00", "end_time": "2026-08-21 08:00"},
            {"weather": "雷阵雨", "rain_1h": 2.6, "start_time": "2026-08-21 08:00", "end_time": "2026-08-22 08:00"},
        ]
        facts = {"poi_category": "port", "has_rain_signal": True, "periods": periods, "total_rain_mm": 2.6}
        out = dw_core._build_poi_reminder_section(facts)
        assert out.startswith("【注意事项】")
        # 轻雾 / 雷阵雨 / 系泊 / 码头湿滑 / 跟踪预警 各占一条编号
        numbered = [line for line in out.splitlines() if line[:2] in ("1.", "2.", "3.", "4.", "5.")]
        assert numbered[0].startswith("1. ") and "加强瞭望" in numbered[0]
        assert any(line.startswith("2. ") for line in numbered)
        assert any("适时调整缆绳" in line for line in numbered)

    def test_mountain_rain_numbered(self):
        facts = {"poi_category": "mountain", "has_rain_signal": True,
                 "periods": [{"weather": "阴转小雨", "rain_1h": 2.0}], "total_rain_mm": 2.0}
        out = dw_core._build_poi_reminder_section(facts)
        assert out.startswith("【注意事项】")
        assert "1. 受降雨影响" in out and "2. 山区降雨易造成步道湿滑" in out


class TestUnwrapToolResultHeuristic:
    """普通 Markdown 字符串不再误报 JSON 解析失败；真正的 JSON 字符串仍正常解析。"""

    def test_markdown_string_returned_as_is(self, capsys):
        from utils.tool_result import _unwrap_tool_result
        text = "【核心结论】未来三天密云水库无明显降雨。"
        out = _unwrap_tool_result(text)
        assert out == text
        captured = capsys.readouterr()
        assert "JSON 解析失败" not in captured.out

    def test_json_string_still_parsed(self):
        from utils.tool_result import _unwrap_tool_result
        out = _unwrap_tool_result('{"status": "ok"}')
        assert out == {"status": "ok"}

    def test_malformed_json_still_warns(self, capsys):
        from utils.tool_result import _unwrap_tool_result
        out = _unwrap_tool_result('{"status": "ok"')
        assert out == '{"status": "ok"'
        assert "JSON 解析失败" in capsys.readouterr().out


class TestReservoirNoRainReminder:
    """水库无降雨时不警告"降雨引起的水位上涨"（避免与结论矛盾），水位数值仍展示。"""

    def test_no_rain_reservoir_skips_rain_warning(self):
        facts = {
            "poi_category": "reservoir", "has_rain_signal": False,
            "periods": [{"weather": "晴", "rain_1h": 0.0, "EDA": "南风1-2级"}],
            "total_rain_mm": 0.0,
            "water_level_info": {
                "reservoir_name": "密云水库", "water_level_m": 150.23,
                "flood_limit_m": 154.0,
            },
        }
        out = dw_core._build_poi_reminder_section(facts)
        assert "降雨引起的水位上涨" not in out  # 无雨不再警告雨致上涨
        assert "1. 目前密云水库库上水位约 150.23 米（汛限水位 154.0 米）" in out  # 水位照常
        # 余量 = 154.0 - 150.23 = 3.77 米（充足档）+ 无雨 → 平稳研判
        assert "低于汛限水位约 3.8 米" in out
        assert "短期库区水位预计平稳" in out


class TestReservoirRiskTiers:
    """水库洪水/山洪风险研判按「水位距汛限余量」分档 + 降雨叠加，确定性、零编造。"""

    def _reminder(self, water_level, flood_limit, has_rain):
        facts = {
            "poi_category": "reservoir",
            "has_rain_signal": has_rain,
            "periods": [],
            "total_rain_mm": 1.0 if has_rain else None,
            "water_level_info": {
                "reservoir_name": "某水库", "water_level_m": str(water_level),
                "flood_limit_m": str(flood_limit),
            },
        }
        return dw_core._build_poi_reminder_section(facts)

    def test_at_or_above_flood_limit_with_rain_high_risk(self):
        out = self._reminder(154.5, 154.0, has_rain=True)
        assert "已达/超过汛限水位" in out
        assert "洪水与下游山洪的风险较高" in out

    def test_at_or_above_flood_limit_no_rain_still_warns_discharge(self):
        out = self._reminder(154.5, 154.0, has_rain=False)
        assert "已达/超过汛限水位" in out
        assert "泄洪调度" in out
        assert "水位上涨" not in out

    def test_near_limit_rain_warns_breakthrough(self):
        out = self._reminder(153.0, 154.0, has_rain=True)  # 距汛限 1.0 米
        assert "距汛限水位仅约 1.0 米" in out
        assert "易突破汛限" in out
        assert "库区洪水及下游山洪风险" in out

    def test_near_limit_no_rain_calm(self):
        out = self._reminder(153.0, 154.0, has_rain=False)
        assert "距汛限水位约 1.0 米" in out
        assert "短期预计平稳" in out

    def test_ample_margin_rain_still_mentions_shanhong(self):
        out = self._reminder(133.5, 152.0, has_rain=True)  # 余量 18.5 米
        assert "低于汛限水位约 18.5 米" in out
        assert "蓄水余量较充足" in out
        assert "山洪风险" in out

    def test_ample_margin_no_rain_peaceful(self):
        out = self._reminder(133.5, 152.0, has_rain=False)
        assert "蓄水余量较充足" in out
        assert "短期库区水位预计平稳" in out
        assert "水位上涨" not in out

    def test_no_water_data_rain_general_warning(self):
        facts = {"poi_category": "reservoir", "has_rain_signal": True, "periods": [], "total_rain_mm": 1.0}
        out = dw_core._build_poi_reminder_section(facts)
        assert "预报未来有降雨" in out
        assert "山洪风险" in out
        assert "库上水位" not in out

    def test_no_water_data_no_rain_general_caution(self):
        facts = {"poi_category": "reservoir", "has_rain_signal": False, "periods": [], "total_rain_mm": None}
        out = dw_core._build_poi_reminder_section(facts)
        assert "未来无明显降雨，短期库区水位预计平稳" in out
        assert "突发涨水与山洪风险" in out

    def test_historical_rain_wording(self):
        # 历史实况：风险研判用"当日实际"措辞，不用"预报/未来"
        facts = {
            "poi_category": "reservoir", "query_mode": "historical_obs_request",
            "has_rain_signal": True, "periods": [], "total_rain_mm": 1.0,
            "water_level_info": {
                "reservoir_name": "某水库", "water_level_m": "150.0", "flood_limit_m": "152.0",
            },
        }
        out = dw_core._build_poi_reminder_section(facts)
        assert "当日实际有降雨" in out
        assert "预报" not in out and "未来" not in out
        assert "低于汛限水位约 2.0 米" in out
