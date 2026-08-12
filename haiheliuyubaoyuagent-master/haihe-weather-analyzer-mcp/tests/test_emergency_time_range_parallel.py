"""应急时间段判定并发取数测试。

回归背景（2026-08-12 性能排查）：
`evaluate_emergency_response_by_time_range` 对 4 个整点时次（02/08/14/20）**串行**
调用 `_observation_fetch_core`，每次拉全流域 24h 分钟数据 ~30s，4 次 ≈127s，
是「2023-07-30 是否启动应急响应」总耗时 142s 的主因（占比 89%）。

改造口径（已与用户确认）：
- 抽出单时次流水线 `_evaluate_one_synoptic_time`（fetch→filter→evaluate→report→event dict），
  用 ThreadPoolExecutor 并发执行（EMERGENCY_FETCH_WORKERS，默认 4）。
- 线程安全依据：`_observation_fetch_core` 每次 `new MusicClient()` → 独立 Session，无共享可变状态。
- 单时次失败容错（返回 None 跳过）、返回结构、按时间排序、max_level 聚合口径全部不变。
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import haihe_mcp_tools as m

# 2023-07-30 单日覆盖 02/08/14/20 共 4 个时次
_START = "2023-07-30 00:00:00"
_END = "2023-07-30 23:59:59"


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


def test_time_range_fetches_in_parallel(monkeypatch):
    """4 个时次应并发取数：峰值并发 > 1（串行则恒为 1）。"""
    fn = _register_tools()["evaluate_emergency_response_by_time_range"]

    lock = threading.Lock()
    state = {"cur": 0, "max": 0, "calls": 0}

    def fake_fetch(basin_codes, times, elements=None, data_code=None, window_hours=24):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
            state["calls"] += 1
        time.sleep(0.2)  # 模拟慢 IO，给并发重叠留窗口
        with lock:
            state["cur"] -= 1
        return []

    monkeypatch.setattr(m, "_observation_fetch_core", fake_fetch)

    start = time.monotonic()
    fn(start_time=_START, end_time=_END)
    elapsed = time.monotonic() - start

    assert state["calls"] == 4, "单日应触发 4 个时次取数"
    assert state["max"] > 1, f"应急时间段判定应并发取数（峰值并发={state['max']}，串行为 1）"
    # 串行下限 4×0.2=0.8s；并发应明显更小（留足机器抖动余量）
    assert elapsed < 0.7, f"并发总耗时应显著低于串行下限，实际 {elapsed:.2f}s"


def test_one_timestep_failure_does_not_abort_others(monkeypatch):
    """某时次取数抛错时，其余时次仍应被尝试（单点容错不拖垮整体）。"""
    fn = _register_tools()["evaluate_emergency_response_by_time_range"]

    attempted = []
    lock = threading.Lock()

    def fake_fetch(basin_codes, times, elements=None, data_code=None, window_hours=24):
        with lock:
            attempted.append(times)
        if times.endswith("080000"):  # 08 时次模拟取数失败
            raise RuntimeError("模拟 MUSIC 取数失败")
        return []

    monkeypatch.setattr(m, "_observation_fetch_core", fake_fetch)

    result = fn(start_time=_START, end_time=_END)

    assert len(attempted) == 4, f"4 个时次都应被尝试，实际 {len(attempted)}"
    assert isinstance(result, dict), "单点失败不应导致整个工具抛错"


def test_events_sorted_and_max_level_aggregated(monkeypatch):
    """并发收集后应按时间排序，且 max_level_in_period 取最高等级（I 最高）。"""
    fn = _register_tools()["evaluate_emergency_response_by_time_range"]

    # 并发完成顺序不确定：故意让后面的时次先返回更高等级，验证最终仍按时间排序、取最高级
    canned = {
        "20230730020000": {"time": "2023-07-30 02:00:00", "max_level": "IV",
                            "summary": "s2", "reached_station_count": 1, "total_station_count": 169},
        "20230730080000": {"time": "2023-07-30 08:00:00", "max_level": "II",
                            "summary": "s8", "reached_station_count": 5, "total_station_count": 169},
        "20230730140000": {"time": "2023-07-30 14:00:00", "max_level": "III",
                            "summary": "s14", "reached_station_count": 3, "total_station_count": 169},
        # 20 时次未触发 → 返回 None
        "20230730200000": None,
    }

    def fake_one(ts, basin_codes, allowed_station_levels):
        key = ts.strftime("%Y%m%d%H%M%S")
        # 让 02 时次慢一点，制造乱序完成
        if key == "20230730020000":
            time.sleep(0.05)
        return canned[key]

    monkeypatch.setattr(m, "_evaluate_one_synoptic_time", fake_one)

    result = fn(start_time=_START, end_time=_END)

    assert result["triggered_count"] == 3
    assert result["max_level_in_period"] == "II", "最高等级应取 I>II>III>IV 中的最高（II）"
    times = [e["time"] for e in result["events"]]
    assert times == sorted(times), "events 应按时间升序排序（不受并发完成顺序影响）"
    assert [e["max_level"] for e in result["events"]] == ["IV", "II", "III"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
