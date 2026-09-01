"""天津当前天气实况按区县明细（regions.tianjin_districts）测试。

背景（2026-09-01 用户口径）：问"天津当前天气实况"时回答里出现"海河流域"行、
却不列天津各区县明细——"问的不是天津的吗，为什么会出现海河流域，而不把天津各区的列出来呢"。
修复：`query_current_weather_observation_core` 在 `regions` 里新增 `tianjin_districts`，
按记录 `Cnty` 分组、逐区县复用 `_calculate_area_stats`，供 answer 层列天津各区县表；
海河流域/北京/河北仅在用户明确问到时才展示（prompt 层控制）。

零编造：展示名用原始 `Cnty` 不改写；缺 `Cnty` 的记录归入"未分区"，不丢数据。
确定性"滚动实况"路径（current_weather_observation_response.build_*）只读 REGION_LABELS
固定键，新增 `tianjin_districts` 不影响该路径。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import current_weather_observation_service as svc

API_UTC = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def _rec(station: str, province: str, city: str, cnty: str, pre: float = 5.0) -> dict:
    return {
        "Station_Id_C": station,
        "Station_Name": station,
        "Province": province,
        "City": city,
        "Cnty": cnty,
        "PRE": pre,
        "PRE_1h": pre,
        "Datetime": "2026-09-01 00:00:00",
    }


# 天津 4 个区县（含两站同区、含缺 Cnty）+ 北京/河北各 1 站（不应进 tianjin_districts）
REGION_RECORDS = [
    _rec("TJ_HX1", "天津市", "天津市", "河西区", 8.0),
    _rec("TJ_HX2", "天津市", "天津市", "河西区", 2.0),   # 同区两站 → 聚合
    _rec("TJ_JZ", "天津市", "天津市", "蓟州区", 25.0),
    _rec("TJ_BH", "天津市", "天津市", "滨海新区", 0.0),
    _rec("TJ_NONE", "天津市", "天津市", "", 4.0),         # 缺 Cnty → 未分区
    _rec("BJ_CY", "北京市", "北京市", "朝阳区", 30.0),     # 不应进 tianjin_districts
    _rec("HB_SJZ", "河北省", "石家庄市", "长安区", 12.0),  # 不应进 tianjin_districts
]
BASIN_RECORDS = [_rec("HL001", "海河流域", "海河流域", "流域", 6.0)]


def _run(monkeypatch):
    def fake_query(client, *, now, hours_back):
        return API_UTC, list(REGION_RECORDS), list(BASIN_RECORDS), []

    monkeypatch.setattr(svc, "_query_same_successful_time", fake_query)
    monkeypatch.setattr(svc, "CURRENT_WEATHER_CACHE_TTL", 3600)
    svc._current_weather_cache.clear()
    fixed_now = datetime(2026, 9, 1, 8, 30, tzinfo=svc.BEIJING_TIMEZONE)
    return svc.query_current_weather_observation_core(
        lambda: None, now=fixed_now, hours_back=6
    )


class TestTianjinDistricts:
    def test_districts_grouped_by_cnty(self, monkeypatch):
        """tianjin_districts 按 Cnty 分组，只含天津各区县，不含北京/河北/海河流域。"""
        result = _run(monkeypatch)
        assert result["status"] == "ok"
        districts = result["regions"]["tianjin_districts"]
        names = [d["name"] for d in districts]
        assert set(names) == {"河西区", "蓟州区", "滨海新区", "未分区"}
        assert "朝阳区" not in names, "北京区县不应进 tianjin_districts"
        assert "长安区" not in names, "河北区县不应进 tianjin_districts"

    def test_district_stats_reuse_area_stats(self, monkeypatch):
        """每个区县复用 _calculate_area_stats：同区两站聚合、字段齐全。"""
        result = _run(monkeypatch)
        districts = {d["name"]: d for d in result["regions"]["tianjin_districts"]}
        hexi = districts["河西区"]
        assert hexi["record_count"] == 2, "同区两站应聚合成一条区县记录"
        assert hexi["average_pre_mm"] == 5.0, f"河西平均应为 (8+2)/2=5.0，实际 {hexi['average_pre_mm']}"
        assert hexi["max_pre_mm"] == 8.0
        # 复用 _calculate_area_stats 的结构字段
        for field in (
            "valid_pre_station_count",
            "max_pre_station",
            "max_pre_1h_mm",
            "max_pre_1h_station",
            "rainfall_judgement",
        ):
            assert field in hexi, f"区县记录缺字段 {field}"

    def test_districts_sorted_by_max_pre_desc(self, monkeypatch):
        """按最大雨量降序：蓟州(25) > 河西(8) > 未分区(4) > 滨海(0)。"""
        result = _run(monkeypatch)
        names = [d["name"] for d in result["regions"]["tianjin_districts"]]
        assert names == ["蓟州区", "河西区", "未分区", "滨海新区"], f"排序错误：{names}"

    def test_missing_cnty_grouped_as_unassigned(self, monkeypatch):
        """缺 Cnty 的记录归入"未分区"，不丢数据。"""
        result = _run(monkeypatch)
        districts = {d["name"]: d for d in result["regions"]["tianjin_districts"]}
        assert "未分区" in districts
        assert districts["未分区"]["max_pre_mm"] == 4.0
        assert districts["未分区"]["record_count"] == 1

    def test_existing_six_buckets_unchanged(self, monkeypatch):
        """向后兼容：原 6 桶结构不变，新增 tianjin_districts 不影响既有键。"""
        result = _run(monkeypatch)
        regions = result["regions"]
        for key in ("tianjin", "tianjin_central", "jizhou", "beijing", "hebei", "haihe_basin"):
            assert key in regions, f"既有桶 {key} 不应被移除"
        # 全市桶仍是全部天津站聚合（含未分区那站）
        assert regions["tianjin"]["record_count"] == 5


class TestTianjinDistrictsSortEdge:
    """code-review 2026-09-01：排序口径必须与 rainfall_judgement 的 rain_basis 一致——
    累计 PRE 缺测但小时 PRE_1h 有值的区县，不应被当"无数据"排到最后。"""

    def test_district_with_only_hourly_pre_not_sorted_last(self, monkeypatch):
        # 武清：累计 PRE 缺测（None），小时 PRE_1h=30（大雨）——应按 30 降序排最前，
        # 不能因 max_pre_mm=None 落到最后被当成"无数据"。
        region_records = [
            _rec("TJ_WQ", "天津市", "天津市", "武清区", 0.0),  # 先占位，下面覆盖 PRE
            _rec("TJ_JZ", "天津市", "天津市", "蓟州区", 5.0),
        ]
        region_records[0]["PRE"] = None        # 累计缺测
        region_records[0]["PRE_1h"] = 30.0     # 小时有值（大雨）

        def fake_query(client, *, now, hours_back):
            return API_UTC, list(region_records), list(BASIN_RECORDS), []

        monkeypatch.setattr(svc, "_query_same_successful_time", fake_query)
        monkeypatch.setattr(svc, "CURRENT_WEATHER_CACHE_TTL", 3600)
        svc._current_weather_cache.clear()
        fixed_now = datetime(2026, 9, 1, 8, 30, tzinfo=svc.BEIJING_TIMEZONE)
        result = svc.query_current_weather_observation_core(
            lambda: None, now=fixed_now, hours_back=6
        )
        districts = result["regions"]["tianjin_districts"]
        names = [d["name"] for d in districts]
        assert names[0] == "武清区", (
            f"累计缺测但小时有值的区县应按小时雨量排最前，实际顺序：{names}"
        )

