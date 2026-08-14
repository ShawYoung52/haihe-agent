"""当前天气实况 MUSIC 多时次合并（12 次调用 → 2 次）测试。

背景（2026-08-12 全问题类型性能优化）：
_query_same_successful_time 逐候选时次 × 每时次 2 接口 = 6×2=12 次调用。
MUSIC ByTime 接口 times 参数支持逗号连接多时次，一次请求返回全部时次 → 合并为
region 1 次 + basin 1 次。语义红线：从新到旧选第一个「region 覆盖完整 + basin 非空」
的时次，与原循环逐字等价。

安全回退：服务端不支持多时次（只返回单个时次 / 合并请求抛错）时，回退逐时次串行，
行为与原实现完全一致（无回归）。多时次检测 = region 与 basin 都返回 ≥2 个不同时次。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import current_weather_observation_service as svc

FIXED_NOW = datetime(2026, 8, 14, 12, 30, tzinfo=svc.BEIJING_TIMEZONE)  # UTC 04:30
# 候选：04,03,02,01,00 点（14 日）+ 23 点（13 日）


def _rec(station: str, province: str, city: str, cnty: str, dt: str, pre: float = 5.0) -> dict:
    return {
        "Station_Id_C": station,
        "Station_Name": station,
        "Province": province,
        "City": city,
        "Cnty": cnty,
        "PRE": pre,
        "PRE_1h": pre,
        "Datetime": dt,
    }


def _fmt(t: str) -> str:
    """YYYYMMDDHHMMSS → YYYY-MM-DD HH:MM:SS（记录 Datetime 用）。"""
    return f"{t[:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:00"


def _region_records(t: str) -> list[dict]:
    return [
        _rec(f"TJ{t}", "天津市", "天津市", "和平", _fmt(t), 8.0),
        _rec(f"BJ{t}", "北京市", "北京市", "朝阳", _fmt(t), 3.0),
        _rec(f"HB{t}", "河北省", "石家庄市", "长安", _fmt(t), 1.5),
    ]


def _basin_records(t: str) -> list[dict]:
    return [_rec(f"HL{t}", "海河流域", "海河流域", "流域", _fmt(t), 6.0)]


def _candidate_times() -> list[str]:
    return [
        "20260814040000", "20260814030000", "20260814020000",
        "20260814010000", "20260814000000", "20260813230000",
    ]


class _FakeClient:
    """多时次模式：comma 连接 times 返回全部时次记录；单时次返回该时次。"""

    def __init__(self, region_by_time: dict, basin_by_time: dict):
        self.region_by_time = region_by_time
        self.basin_by_time = basin_by_time
        self.region_calls = 0
        self.basin_calls = 0
        self.last_region_times = ""
        self.last_basin_times = ""

    def get_surf_ele_in_region_by_time(self, admin_codes, times, elements, data_code):
        self.region_calls += 1
        self.last_region_times = str(times)
        return self._lookup(times, self.region_by_time)

    def get_surf_ele_in_basin_by_time(self, basin_codes, times, elements, data_code):
        self.basin_calls += 1
        self.last_basin_times = str(times)
        return self._lookup(times, self.basin_by_time)

    def _lookup(self, times: str, by_time: dict) -> list[dict]:
        out: list[dict] = []
        for t in str(times).split(","):
            out.extend(by_time.get(t, []))
        return out


class TestMultiTimeCoalesce:
    def _run(self, client):
        svc._current_weather_cache.clear()
        return svc.query_current_weather_observation_core(
            lambda: client, now=FIXED_NOW, hours_back=6
        )

    def test_multi_time_uses_two_calls_and_selects_newest(self):
        """全部时次有覆盖时，合并为 2 次调用并选中最新时次。"""
        times = _candidate_times()
        client = _FakeClient(
            region_by_time={t: _region_records(t) for t in times},
            basin_by_time={t: _basin_records(t) for t in times},
        )
        result = self._run(client)

        assert client.region_calls == 1, f"region 应只调 1 次，实际 {client.region_calls}"
        assert client.basin_calls == 1, f"basin 应只调 1 次，实际 {client.basin_calls}"
        assert "," in client.last_region_times, "region 应传逗号连接的多时次"
        assert "," in client.last_basin_times, "basin 应传逗号连接的多时次"
        assert result["status"] == "ok"
        assert result["query_time_utc"] == "2026-08-14 04:00:00"

    def test_selects_latest_coverage_complete(self):
        """最新时次缺河北（覆盖不完整）时选次新的完整时次。"""
        times = _candidate_times()
        newest = times[0]
        region_by_time = {t: _region_records(t) for t in times}
        # 最新时次去掉河北记录 → 覆盖不完整
        region_by_time[newest] = [
            r for r in region_by_time[newest]
            if r["Province"] != "河北省"
        ]
        client = _FakeClient(
            region_by_time=region_by_time,
            basin_by_time={t: _basin_records(t) for t in times},
        )
        result = self._run(client)

        assert client.region_calls == 1
        assert result["status"] == "ok"
        assert result["query_time_utc"] == "2026-08-14 03:00:00"

    def test_fallback_when_server_returns_single_time(self):
        """服务端只返回单个时次（不支持多时次）→ 回退逐时次串行，行为不变。"""
        times = _candidate_times()
        newest = times[0]

        class _SingleTimeClient(_FakeClient):
            def _lookup(self, times_s, by_time):
                # 模拟服务端只响应列表第一个时次
                return by_time.get(str(times_s).split(",")[0], [])

        client = _SingleTimeClient(
            region_by_time={t: _region_records(t) for t in times},
            basin_by_time={t: _basin_records(t) for t in times},
        )
        result = self._run(client)

        # 1 次合并尝试（只回单时次）+ 回退串行第 1 个候选命中 = 2
        assert client.region_calls == 2, f"应 1 次合并尝试 + 1 次回退，实际 {client.region_calls}"
        assert result["status"] == "ok"
        assert result["query_time_utc"] == "2026-08-14 04:00:00"

    def test_fallback_when_coalesced_fetch_raises(self):
        """合并请求抛错 → 回退逐时次串行，单时次失败容错保持不变。"""
        times = _candidate_times()

        class _RaisingClient(_FakeClient):
            def _lookup(self, times_s, by_time):
                if "," in str(times_s):
                    raise RuntimeError("不支持多时次")
                return by_time.get(str(times_s), [])

        client = _RaisingClient(
            region_by_time={t: _region_records(t) for t in times},
            basin_by_time={t: _basin_records(t) for t in times},
        )
        result = self._run(client)

        # 1 次合并失败 + 回退串行第 1 个候选命中 = 2
        assert client.region_calls == 2, f"应 1 次合并失败 + 1 次回退，实际 {client.region_calls}"
        assert result["status"] == "ok"
        assert result["query_time_utc"] == "2026-08-14 04:00:00"
