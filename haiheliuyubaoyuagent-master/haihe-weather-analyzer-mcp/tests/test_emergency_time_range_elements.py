"""时间段应急判定取数要素回归测试。

回归背景（内网 2026-08-12 实测）：
`evaluate_emergency_response_by_time_range` 调 `_observation_fetch_core` 时误传
`DEFAULT_OBS_ELEMENTS`（含 `PRE_1h` 等小时累计要素），而它用的 `data_code` 是分钟资料
`SURF_CHN_PRE_MIN`。分钟数据集没有 `PRE_1h`，天擎/MUSIC 直接回
`-3003 "Element:[PRE_1h] is not config."`，4 个时次（02/08/14/20）全部取数失败，
又被 `except ...: continue` 吞掉，最终 `events=[]` 返回「该时段内未触发应急响应」——
把「数据没取到」静默报成「未触发」。2023-07-30（23·7 海河大洪水）因此被误判为未触发。

修复口径：与兄弟函数 `evaluate_emergency_response_core`（同 pipeline）一致，
时间段判定也必须用分钟要素 `DEFAULT_MIN_PRE_ELEMENTS`（PRE/Q_PRE，不含 PRE_1h）。
`aggregate_minute_precipitation` 本就只读分钟 `PRE` 本地累加出 1/12/24h，
不需要响应里的 PRE_1h 等字段。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import haihe_mcp_tools as m


def _register_tools() -> dict:
    """用 passthrough 假 mcp 注册全部工具，按名字取出目标函数。"""
    captured = {}

    class FakeMCP:
        def tool(self, *args, **kwargs):
            def deco(fn):
                captured[fn.__name__] = fn
                return fn

            return deco

    m.register_haihe_tools(FakeMCP())
    return captured


_NOT_PASSED = object()


def test_emergency_time_range_uses_minute_elements(monkeypatch):
    """时间段应急判定取数不得把小时要素 PRE_1h 发给分钟资料 SURF_CHN_PRE_MIN。"""
    fn = _register_tools()["evaluate_emergency_response_by_time_range"]

    seen = []

    def fake_fetch(basin_codes, times, elements=_NOT_PASSED, data_code=_NOT_PASSED, window_hours=24):
        seen.append({"elements": elements, "data_code": data_code})
        return []

    monkeypatch.setattr(m, "_observation_fetch_core", fake_fetch)

    fn(start_time="2023-07-30 00:00:00", end_time="2023-07-30 23:59:59")

    assert seen, "时间段判定应至少发起一次 _observation_fetch_core 取数"
    for call in seen:
        elements = call["elements"]
        # 修复方式一：显式传分钟要素；方式二：省略 elements 走默认（默认即分钟要素）。
        if elements is not _NOT_PASSED:
            assert "PRE_1h" not in elements, "分钟资料不应请求 PRE_1h（小时要素）"
            assert elements == m.DEFAULT_MIN_PRE_ELEMENTS, (
                "应急时间段判定应与兄弟函数一致用分钟要素 DEFAULT_MIN_PRE_ELEMENTS"
            )
        if call["data_code"] is not _NOT_PASSED:
            assert call["data_code"] == m.DEFAULT_MIN_PRE_DATA_CODE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
