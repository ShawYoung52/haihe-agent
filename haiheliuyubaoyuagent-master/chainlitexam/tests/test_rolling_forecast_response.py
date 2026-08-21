"""Tests for rolling_forecast_response region activity mountain reminder."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

from chainlitexam.tools import rolling_forecast_response as rfr


def _daily(weather, rain):
    return [{"date_label": "8月20日", "weather": weather, "rainfall_max_24h_mm": rain, "EDA": "南风1-2级"}]


class TestMountainActivityReminder:
    def test_rain_mountain_query_gets_risk_reminder(self):
        bundle = rfr.build_rolling_forecast_bundle(
            "明天适合去蓟州游玩吗", {"daily_summary": _daily("阴转小雨", 5.0)}
        )
        section = bundle["code_section"]
        assert "【注意事项】" in section
        assert "不建议登山、溯溪、野外徒步" in section
        assert "山洪、落石隐患" in section
        assert "防滑鞋" in section

    def test_no_rain_mountain_query_gets_light_reminder(self):
        bundle = rfr.build_rolling_forecast_bundle(
            "明天适合去蓟州游玩吗", {"daily_summary": _daily("多云转阴有轻雾", 0.0)}
        )
        section = bundle["code_section"]
        assert "量力而行" in section
        assert "山洪" not in section  # 无雨不硬塞山洪警告（不死板）

    def test_non_mountain_activity_no_reminder(self):
        bundle = rfr.build_rolling_forecast_bundle(
            "明天适合去水上公园玩吗", {"daily_summary": _daily("阴转小雨", 5.0)}
        )
        assert "注意事项" not in bundle["code_section"]

    def test_panshan_query_also_mountain(self):
        bundle = rfr.build_rolling_forecast_bundle(
            "明天去盘山游玩合适吗", {"daily_summary": _daily("晴", 0.0)}
        )
        assert "【注意事项】" in bundle["code_section"]

    def test_activity_assembly_rejects_llm_generated_forecast_and_advice_sections(self):
        bundle = rfr.build_rolling_forecast_bundle(
            "明天适合去蓟州游玩吗？",
            {
                "daily_summary": _daily("多云", 0.0),
                "data_source": "天津市气象台滚动预报",
            },
        )
        llm_text = (
            "【核心结论】\n明天适合游玩。\n\n"
            "【明日蓟州天气预报】\n天气现象：阵雨\n\n"
            "【游玩建议】\n建议携带雨具。"
        )

        answer = rfr.assemble_rolling_forecast_answer(llm_text, [bundle])

        assert "【逐日活动预报】" in answer
        assert "【注意事项】" in answer
        assert "【明日蓟州天气预报】" not in answer
        assert "【游玩建议】" not in answer
        assert answer.count("数据来源：天津市气象台滚动预报") == 1


def test_general_weather_assembly_keeps_llm_travel_advice_section():
    bundle = rfr.build_rolling_forecast_bundle(
        "明天天气怎么样",
        {
            "daily_summary": _daily("多云", 0.0),
            "data_source": "天津市气象台滚动预报",
        },
    )
    llm_text = "【核心结论】\n明天天气平稳。\n\n【出行建议】\n可正常安排出行。"

    answer = rfr.assemble_rolling_forecast_answer(llm_text, [bundle])

    assert "【出行建议】" in answer
    assert "可正常安排出行。" in answer


def test_cycling_query_uses_activity_template_consistently_with_router():
    bundle = rfr.build_rolling_forecast_bundle(
        "蓟州明天骑行",
        {"daily_summary": _daily("多云", 0.0)},
    )

    assert bundle["category"] == "activity"
    assert "【逐日活动预报】" in bundle["code_section"]


@pytest.mark.parametrize(
    "decorated_header",
    ("### 【游玩建议】", "**【明日蓟州天气预报】**"),
)
def test_activity_assembly_rejects_markdown_decorated_code_owned_headers(decorated_header):
    bundle = rfr.build_rolling_forecast_bundle(
        "明天适合去蓟州游玩吗？",
        {"daily_summary": _daily("多云", 0.0)},
    )
    llm_text = (
        "【核心结论】\n明天适合游玩。\n\n"
        "【重点关注】\n请关注临近预报。\n\n"
        f"{decorated_header}\n模型虚构建议。"
    )

    answer = rfr.assemble_rolling_forecast_answer(llm_text, [bundle])

    assert "【重点关注】" in answer
    assert decorated_header not in answer
    assert "模型虚构建议。" not in answer


def _hazards_payload():
    return {
        "daily_summary": _daily("阴转小雨", 5.0),
        "data_source": "天津市气象台滚动预报",
        "region_hazards": [
            {
                "region": "蓟州",
                "region_display": "蓟州区",
                "total_found": 3,
                "radius_km": 25.0,
                "categories": [
                    {"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 2},
                    {"key": "sh", "label": "山洪", "kind": "山洪", "count": 1},
                ],
            }
        ],
    }


class TestRegionHazardTable:
    def test_region_hazard_table_renders(self):
        """区域查询的天气回答附带【区域】灾害风险表（类型×数量×研判×建议）。"""
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", _hazards_payload())
        section = bundle["code_section"]
        assert "【蓟州区灾害风险】" in section
        assert "灾害类型" in section
        assert "隐患点数量" in section
        assert "风险研判" in section
        assert "防范建议" in section
        assert "| 地质灾害 | 2 处 |" in section
        assert "| 山洪 | 1 处 |" in section
        assert "滑坡、崩塌、泥石流" in section
        assert "山洪灾害危险区" in section

    def test_region_hazard_table_plus_weather_table(self):
        """天气表在前、灾害风险表在后，两者并存。"""
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", _hazards_payload())
        section = bundle["code_section"]
        assert "8月20日" in section
        assert section.index("8月20日") < section.index("【蓟州区灾害风险】")

    def test_region_hazard_table_skips_zero_count(self):
        """count=0 的类型不渲染空行。"""
        payload = _hazards_payload()
        payload["region_hazards"][0]["categories"] = [
            {"key": "dzzh", "label": "地质灾害", "kind": "地灾", "count": 0},
            {"key": "zxhl", "label": "中小河流洪水", "kind": "河流", "count": 3},
        ]
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", payload)
        section = bundle["code_section"]
        assert "地质灾害" not in section
        assert "| 中小河流洪水 | 3 处 |" in section
        assert "中小河流洪水风险区" in section

    def test_region_hazard_table_no_data_skips_region(self):
        """区域无隐患数据（categories 空）时整表跳过。"""
        payload = _hazards_payload()
        payload["region_hazards"][0]["categories"] = []
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", payload)
        assert "灾害风险" not in bundle["code_section"]

    def test_region_hazard_table_absent_payload(self):
        """payload 无 region_hazards 字段（点位模式/降级失败）不出现风险表。"""
        bundle = rfr.build_rolling_forecast_bundle(
            "蓟州天气怎么样", {"daily_summary": _daily("多云", 0.0)}
        )
        assert "灾害风险" not in bundle["code_section"]

    def test_region_hazard_table_multi_region(self):
        """多区域查询渲染多张表，各自标题。"""
        payload = _hazards_payload()
        payload["region_hazards"].append(
            {
                "region": "宝坻",
                "region_display": "宝坻区",
                "total_found": 1,
                "radius_km": 25.0,
                "categories": [{"key": "zxhl", "label": "中小河流洪水", "kind": "河流", "count": 1}],
            }
        )
        bundle = rfr.build_rolling_forecast_bundle("蓟州宝坻天气", payload)
        section = bundle["code_section"]
        assert "【蓟州区灾害风险】" in section
        assert "【宝坻区灾害风险】" in section

    def test_region_hazard_table_fallback_label_and_risk(self):
        """未知 key 用兜底标签与研判，不抛异常。"""
        payload = _hazards_payload()
        payload["region_hazards"][0]["categories"] = [
            {"key": "unknown", "label": "其他灾害", "kind": "其他", "count": 2},
            {"key": "dzzh", "label": "", "kind": "", "count": 1},
        ]
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", payload)
        section = bundle["code_section"]
        assert "| 其他灾害 | 2 处 | 存在风险隐患 |" in section
        # label 为空时回退 key
        assert "| dzzh | 1 处 |" in section


class TestFormatRiskLevelCounts:
    def test_severity_ordered(self):
        """按严重度排序：一级>二级>三级>四级，与输入顺序无关。"""
        assert rfr._format_risk_level_counts({"四级": 2, "一级": 1, "三级": 5}) == "一级 1 处、三级 5 处、四级 2 处"

    def test_omits_zero_levels(self):
        assert rfr._format_risk_level_counts({"一级": 0, "三级": 2}) == "三级 2 处"

    def test_non_standard_keys_appended(self):
        """非一~四级键（接口降级/未知等级）如实列出、排在标准等级后。"""
        assert rfr._format_risk_level_counts({"三级": 2, "高风险": 1}) == "三级 2 处、高风险 1 处"

    def test_empty_returns_no_risk(self):
        assert rfr._format_risk_level_counts({}) == "本次无风险"
        assert rfr._format_risk_level_counts(None) == "本次无风险"


class TestRegionHazardTableRiskLevels:
    """区域天气#8：新 MCP 必带 risk_levels_available 键 → "本次风险等级"列始终出现。

    接口可达有数据按严重度列出、可达无风险"本次无风险"、接口不可达(None)"接口暂不可用"
    （不再静默隐藏列，回答自身可分辨"接口没调好"还是"旧代码未部署"）；旧 payload 无该键才不加列。
    """

    def _with_levels(self, risk_levels, available=True):
        payload = _hazards_payload()
        payload["region_hazards"][0]["risk_levels"] = risk_levels
        payload["region_hazards"][0]["risk_levels_available"] = available
        return payload

    def test_levels_column_renders(self):
        """available=True + 有数据 → 每类多一列"本次风险等级"，含按严重度排序的分布。"""
        payload = self._with_levels({
            "dzzh": {"label": "地质灾害风险", "kind": "geologic", "levels": {"四级": 1, "一级": 2}, "total": 3},
            "sh": {"label": "山洪", "kind": "mountain", "levels": {"三级": 2}, "total": 2},
        })
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", payload)
        section = bundle["code_section"]
        assert "本次风险等级" in section
        assert "| 地质灾害 | 2 处 | 一级 2 处、四级 1 处 |" in section
        assert "| 山洪 | 1 处 | 三级 2 处 |" in section

    def test_levels_column_shows_no_risk(self):
        """接口可达但本次无风险（levels 空）→ 列存在、显示"本次无风险"。"""
        payload = self._with_levels({
            "dzzh": {"label": "地质灾害风险", "kind": "geologic", "levels": {}, "total": 0},
        })
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", payload)
        section = bundle["code_section"]
        assert "本次风险等级" in section
        assert "| 地质灾害 | 2 处 | 本次无风险 |" in section

    def test_levels_column_shows_unavailable_when_interface_down(self):
        """接口不可达（available=False / risk_levels=None）→ 列显示"接口暂不可用"，表照常。

        2026-08-21 生产日志佐证：14所 findDataListByConfig 对三灾种全回 HTTP 500，
        query_region_risk_levels 返回 None；此处锁定降级不再静默隐藏列。
        """
        payload = self._with_levels(None, available=False)
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", payload)
        section = bundle["code_section"]
        assert "本次风险等级" in section
        assert "| 地质灾害 | 2 处 | 接口暂不可用 |" in section
        assert "| 山洪 | 1 处 | 接口暂不可用 |" in section

    def test_levels_column_omitted_when_missing(self):
        """旧 payload 无 risk_levels_available 键（升级前 MCP）→ 不加列（兼容）。"""
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", _hazards_payload())
        assert "本次风险等级" not in bundle["code_section"]

    def test_levels_kind_missing_uses_no_risk(self):
        """部分灾种无等级数据（如接口只回 dzzh）→ 该行显示"本次无风险"，不崩。"""
        payload = self._with_levels({
            "dzzh": {"label": "地质灾害风险", "kind": "geologic", "levels": {"一级": 1}, "total": 1},
        })
        bundle = rfr.build_rolling_forecast_bundle("蓟州天气怎么样", payload)
        section = bundle["code_section"]
        assert "| 地质灾害 | 2 处 | 一级 1 处 |" in section
        assert "| 山洪 | 1 处 | 本次无风险 |" in section
