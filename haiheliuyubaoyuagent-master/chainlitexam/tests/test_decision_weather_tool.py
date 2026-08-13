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
    assert "⚠ 注意事项" in text
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
    # 天气条件从句来自实际 facts 数值
    assert "当前预报时段内有降雨信号" in text


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
    # 无雨（0mm）→ 不提醒隐患点（有降雨才提示），只保留类别模板
    dry = dw_core._build_poi_reminder_section({
        "poi_category": "mountain",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [],
        "hazard_points": base_hazard,
    })
    assert "⚠ 注意事项" in dry
    assert "山区" in dry
    assert "风险低" not in dry
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
    # 无风无雾无雨 → 只有类型模板
    plain = dw_core._build_poi_reminder_section({
        "poi_category": "airport",
        "has_rain_signal": False,
        "total_rain_mm": 0.0,
        "periods": [{"EDA": "东南风2级", "visibility_min_km": 10.0}],
        "hazard_points": None,
    })
    assert plain.count("机场") == 1
    assert "风力较大" not in plain
    assert "能见度较低" not in plain


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
    assert "⚠ 注意事项" in result
    # 提醒位于数据来源之前
    assert result.find("⚠ 注意事项") < result.find("数据来源：")
    assert result.rstrip().endswith("数据来源：天津市气象台滚动预报。")
