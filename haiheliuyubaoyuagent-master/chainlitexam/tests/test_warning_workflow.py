"""预警表格按问法作用域裁剪测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from chainlitexam.tests.stubs import ensure_stubs
ensure_stubs()

from chainlitexam.tools.warning_workflow import (
    _build_warning_table_markdown,
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