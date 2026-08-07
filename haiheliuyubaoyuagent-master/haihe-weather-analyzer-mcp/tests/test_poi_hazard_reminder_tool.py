"""POI 周边灾害隐患点查询工具单元测试。

全部使用 mock 的 psycopg2.connect + FakeConn/FakeCursor，不依赖内网数据库。
参考 tests/test_river_system_rainfall_forecast.py 与 tests/test_poi_search_cache.py 的测试模式。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from custom_tools.poi_hazard_reminder_tool import (  # noqa: E402
    _haversine_km,
    _hazard_rows_cache,
    _query_poi_hazard_reminders_core,
    register_poi_hazard_reminder_tool,
)
from custom_tools.poi_hazard_reminder_tool import config as _config  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    _hazard_rows_cache.clear()
    yield
    _hazard_rows_cache.clear()


def _fake_postgres_conf():
    return {
        "host": "127.0.0.1",
        "port": "5432",
        "dbname": "hhly",
        "user": "postgres",
        "password": "postgres",
        "schema": "public",
        "connect_timeout": "5",
        "sslmode": "disable",
    }


class _FakeCursor:
    """按表名返回预设行；未预设的表触发 UndefinedTable 语义（抛异常）。"""

    def __init__(self, rows_by_table):
        self._rows_by_table = rows_by_table
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        for table, rows in self._rows_by_table.items():
            if table in sql:
                self._rows = rows
                return
        raise RuntimeError('relation "unknown_table" does not exist')

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows_by_table):
        self._rows_by_table = rows_by_table
        self.closed = False
        self.rollback_called = False

    def cursor(self, *args, **kwargs):
        return _FakeCursor(self._rows_by_table)

    def rollback(self):
        self.rollback_called = True

    def close(self):
        self.closed = True


def _dzzh_row(name, lon, lat, county="蓟州区", city="天津市"):
    return {"id": 1, "name": name, "lon": lon, "lat": lat,
            "county_name": county, "city_name": city, "status": 0}


def _install_fake_db(monkeypatch, rows_by_table, connect_error=None):
    module = sys.modules["custom_tools.poi_hazard_reminder_tool"]
    calls = []

    def _fake_connect(**kwargs):
        calls.append(kwargs)
        if connect_error is not None:
            raise connect_error
        return _FakeConn(rows_by_table)

    monkeypatch.setattr(module.psycopg2, "connect", _fake_connect)
    # config 是 configparser.ConfigParser，需按 section 注入 postgres 配置
    _config.remove_section("postgres")
    _config.add_section("postgres")
    for key, value in _fake_postgres_conf().items():
        _config.set("postgres", key, str(value))
    return calls


def test_haversine_filter_keeps_only_nearby(monkeypatch):
    """haversine 过滤只保留半径内隐患点，且按距离升序。"""
    # 蓟州区某点 (117.45, 40.05)；近点 (117.46, 40.05) ≈1km，远点 (118.00, 40.00) ≈46km
    rows = {
        "t_msis_be_fxyj_dzzh_info": [
            _dzzh_row("近处隐患点", 117.46, 40.05),
            _dzzh_row("远处隐患点", 118.00, 40.00),
        ],
        "t_msis_be_fxyj_sh_info": [],
        "t_msis_be_fxyj_zxhl_info": [],
    }
    _install_fake_db(monkeypatch, rows)
    result = _query_poi_hazard_reminders_core(117.45, 40.05, 5.0)
    assert result["status"] == "ok"
    assert result["total_found"] == 1
    assert len(result["categories"]) == 1
    dzzh = result["categories"][0]
    assert dzzh["key"] == "dzzh"
    assert dzzh["label"] == "地质灾害"
    assert len(dzzh["records"]) == 1
    assert dzzh["records"][0]["name"] == "近处隐患点"
    assert dzzh["records"][0]["distance_km"] < 2.0


def test_per_category_grouping_and_output_shape(monkeypatch):
    """三类隐患点分组输出，records 含 name/county/distance_km。"""
    rows = {
        "t_msis_be_fxyj_dzzh_info": [
            _dzzh_row("地灾点A", 117.46, 40.05),
            _dzzh_row("地灾点B", 117.47, 40.06),
        ],
        "t_msis_be_fxyj_sh_info": [
            _dzzh_row("山洪点A", 117.45, 40.06, county="宝坻区"),
        ],
        "t_msis_be_fxyj_zxhl_info": [
            _dzzh_row("河流点A", 117.44, 40.04, county="蓟州区"),
        ],
    }
    _install_fake_db(monkeypatch, rows)
    result = _query_poi_hazard_reminders_core(117.45, 40.05, 10.0)
    assert result["status"] == "ok"
    assert result["total_found"] == 4
    keys = [c["key"] for c in result["categories"]]
    assert keys == ["dzzh", "sh", "zxhl"]
    dzzh = next(c for c in result["categories"] if c["key"] == "dzzh")
    assert dzzh["count"] == 2
    assert all("name" in r and "county" in r and "distance_km" in r for r in dzzh["records"])
    # 距离升序
    distances = [r["distance_km"] for r in dzzh["records"]]
    assert distances == sorted(distances)


def test_no_data_when_nothing_in_radius(monkeypatch):
    """半径内无隐患点 → status=no_data、total_found=0。"""
    rows = {
        "t_msis_be_fxyj_dzzh_info": [_dzzh_row("远处点", 118.00, 40.00)],
        "t_msis_be_fxyj_sh_info": [],
        "t_msis_be_fxyj_zxhl_info": [],
    }
    _install_fake_db(monkeypatch, rows)
    result = _query_poi_hazard_reminders_core(117.45, 40.05, 5.0)
    assert result["status"] == "no_data"
    assert result["total_found"] == 0
    assert result["categories"] == []


def test_missing_table_degrades(monkeypatch):
    """单表缺失（UndefinedTable）不致命，其余两表仍返回，status 仍为 ok。"""
    # 注意：不预设 sh 表键，_FakeCursor.execute 对其抛 RuntimeError 模拟缺表
    rows = {
        "t_msis_be_fxyj_dzzh_info": [_dzzh_row("地灾点", 117.46, 40.05)],
        "t_msis_be_fxyj_zxhl_info": [],
    }
    _install_fake_db(monkeypatch, rows)
    result = _query_poi_hazard_reminders_core(117.45, 40.05, 5.0)
    assert result["status"] == "ok"
    assert result["total_found"] == 1
    assert "山洪" in (result.get("debug_reason") or "")


def test_db_down_returns_error(monkeypatch):
    """数据库不可用 → status=error 且不抛异常。"""
    _install_fake_db(monkeypatch, {}, connect_error=ConnectionError("db down"))
    result = _query_poi_hazard_reminders_core(117.45, 40.05, 5.0)
    assert result["status"] == "error"
    assert result["total_found"] == 0


def test_lazy_cache_reuses_rows_within_ttl(monkeypatch):
    """TTL 内第二次查询不重连（复用缓存）；TTL=0 强制重连。"""
    import custom_tools.poi_hazard_reminder_tool as mod
    rows = {
        "t_msis_be_fxyj_dzzh_info": [_dzzh_row("地灾点", 117.46, 40.05)],
        "t_msis_be_fxyj_sh_info": [],
        "t_msis_be_fxyj_zxhl_info": [],
    }
    calls = _install_fake_db(monkeypatch, rows)

    mod.HAZARD_CACHE_TTL = 3600
    r1 = _query_poi_hazard_reminders_core(117.45, 40.05, 5.0)
    r2 = _query_poi_hazard_reminders_core(117.45, 40.05, 5.0)
    assert r1["status"] == "ok" and r2["status"] == "ok"
    assert len(calls) == 1  # 第二次未重连

    # TTL=0 强制重连
    mod.HAZARD_CACHE_TTL = 0
    r3 = _query_poi_hazard_reminders_core(117.45, 40.05, 5.0)
    assert r3["status"] == "ok"
    assert len(calls) == 2
    mod.HAZARD_CACHE_TTL = 3600


def test_invalid_params(monkeypatch):
    """非法经纬度/半径 → status=error，不访问数据库。"""
    result = _query_poi_hazard_reminders_core(200.0, 40.05, 5.0)
    assert result["status"] == "error"
    result = _query_poi_hazard_reminders_core(117.45, 40.05, 0.0)
    assert result["status"] == "error"
    result = _query_poi_hazard_reminders_core(117.45, 40.05, 51.0)
    assert result["status"] == "error"
    result = _query_poi_hazard_reminders_core("abc", 40.05, 5.0)
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_register_exposes_tool():
    """注册函数应生成可调用工具。"""
    from fastmcp import FastMCP
    mcp = FastMCP("test")
    register_poi_hazard_reminder_tool(mcp)
    tools = await mcp.list_tools()
    tool_names = {tool.name for tool in tools}
    assert "query_poi_hazard_reminders" in tool_names


def test_haversine_distance():
    """haversine 距离计算：0.01° 纬度 ≈ 1.11km。"""
    assert _haversine_km(117.45, 40.05, 117.45, 40.06) == pytest.approx(1.11, abs=0.01)
    assert _haversine_km(117.45, 40.05, 117.45, 40.05) == 0.0
