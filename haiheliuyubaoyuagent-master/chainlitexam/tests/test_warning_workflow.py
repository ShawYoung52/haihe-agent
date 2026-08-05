"""预警表格按问法作用域裁剪测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

from chainlitexam.tools import warning_workflow as wf
from chainlitexam.tools.warning_workflow import (
    _build_warning_table_markdown,
    _is_broad_scoped_warning_query,
    _trim_warning_regions_for_scope,
)

def _record(area="全市各区县", dept="天津市气象台", event="暴雨", sev="黄色", time="2026-08-03 09:00", msg="发布"):
    return {
        "department": dept, "eventType": event, "severity": sev,
        "locationName": area, "time": time, "msgType": msg,
    }


def test_city_scope_does_not_expand_district_details():
    """问市台/全市预警时，影响区域列不展开各区县明细。"""
    records = [
        _record(area="全市各区县", dept="天津市气象台"),
        _record(area="蓟州区、宝坻区", dept="天津市气象台"),
    ]
    trimmed = _trim_warning_regions_for_scope(records, "天津市气象台发布了哪些预警")
    # 市级范围问法：应折叠/标记为市级，不逐区县展开
    assert all("全市" in str(r.get("locationName") or "") or "各区县" in str(r.get("locationName") or "") for r in trimmed)


def test_district_scope_keeps_matching_district():
    """问具体区县时，仅保留该区县相关记录。"""
    records = [
        _record(area="蓟州区、宝坻区", dept="天津市气象台"),
        _record(area="滨海新区", dept="天津市气象台"),
    ]
    trimmed = _trim_warning_regions_for_scope(records, "蓟州区有暴雨预警吗")
    assert all("蓟州" in str(r.get("locationName") or "") for r in trimmed)


def test_table_region_column_can_be_hidden():
    """市级问法下表格可隐藏影响区域列（不展开区县明细）。"""
    records = [_record(area="全市各区县")]
    hidden = _build_warning_table_markdown(records, "【生效预警清单】", show_region_column=False)
    assert "影响区域" not in hidden
    shown = _build_warning_table_markdown(records, "【生效预警清单】", show_region_column=True)
    assert "影响区域" in shown


def test_generic_no_location_query_keeps_all_district_records():
    """问法未指定区县（如"现在有哪些暴雨预警"）时，各区县记录不得被丢弃。"""
    records = [
        _record(area="蓟州区、宝坻区", dept="天津市气象台"),
        _record(area="滨海新区", dept="天津市气象台"),
    ]
    trimmed = _trim_warning_regions_for_scope(records, "现在有哪些暴雨预警")
    assert len(trimmed) == 2
    assert all("蓟州" in str(r.get("locationName") or "") for r in trimmed[:1])
    assert all("滨海" in str(r.get("locationName") or "") for r in trimmed[1:])


def test_district_qualified_with_city_prefix_keeps_matching_district():
    """"天津市X区"前缀问法（含"天津"子串）应走区县分支，不折叠为全市。"""
    records = [
        _record(area="蓟州区、宝坻区", dept="天津市气象台"),
        _record(area="滨海新区", dept="天津市气象台"),
    ]
    trimmed = _trim_warning_regions_for_scope(records, "天津市蓟州区有暴雨预警吗")
    assert len(trimmed) == 1
    assert all("蓟州" in str(r.get("locationName") or "") for r in trimmed)
    assert all("全市" not in str(r.get("locationName") or "") for r in trimmed)


def test_district_qualified_keeps_region_column():
    """"天津市X区"前缀问法不应视作市级问法，保留影响区域列。"""
    records = [_record(area="蓟州区、宝坻区")]
    table = _build_warning_table_markdown(records, "【生效预警清单】", show_region_column=not _is_broad_scoped_warning_query("天津市蓟州区有暴雨预警吗"))
    assert "影响区域" in table


def test_rule_based_warning_route_effective():
    """"现在有什么预警"应路由到生效预警接口。"""
    route = wf._route_warning_tools_rule_based("现在有什么预警？")
    assert route is not None
    assert "get_effective_warning_info" in route["tool_names"]


def test_rule_based_warning_route_history():
    """"暴雨预警解除了吗"应包含历史预警接口。"""
    route = wf._route_warning_tools_rule_based("暴雨预警解除了吗？")
    assert route is not None
    assert "get_history_warning_info" in route["tool_names"]


def test_rule_based_warning_route_today():
    """"今天发布了哪些预警"应包含今日动态接口。"""
    route = wf._route_warning_tools_rule_based("今天发布了哪些预警？")
    assert route is not None
    assert "get_today_warning_summary" in route["tool_names"]


def test_rule_based_warning_route_national():
    """"中央气象台和天津预警"应同时包含国家与本地接口。"""
    route = wf._route_warning_tools_rule_based("中央气象台和天津市发布的预警信息")
    assert route is not None
    assert "get_national_warning_info" in route["tool_names"]
    assert "get_effective_warning_info" in route["tool_names"]


def test_rule_based_warning_route_falls_back_without_keyword():
    """无预警关键词时返回 None（回退 LLM）。"""
    assert wf._route_warning_tools_rule_based("明天天气怎么样") is None
    assert wf._route_warning_tools_rule_based("") is None