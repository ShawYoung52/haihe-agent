"""逐小时风力风向（EDA）时段汇总测试（2026-09-04）。

甲方反馈：点位时段表把逐小时 EDA 去重后用"转"硬拼成
"北风4-5级转北风3-4级转西北风1-2级转西北风3-4级"机械长串。
`utils.wind_summary.summarize_wind_eda` 把连续同风向合并风力区间、
风向变化才用"转"连接，与 MCP rolling_forecast_service._summarize_tod_wind 同口径。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.wind_summary import summarize_wind_eda


def test_same_direction_merges_force_range():
    # 原始问题串：同向不同风力合并区间，风向变化才分段
    assert summarize_wind_eda(
        ["北风4-5级", "北风3-4级", "西北风1-2级", "西北风3-4级"]
    ) == "北风3~5级转西北风1~4级"


def test_identical_force_wind_collapses_to_dominant():
    # 弱风且风力恒定、非渐进转向 → 以主导风向概括
    assert summarize_wind_eda(["北风1-2级", "北风1-2级", "南风1-2级"]) == "以北风为主，风力1~2级"


def test_weak_wind_oscillation_uses_dominant_direction():
    # 0~2 级弱风来回摆动（非渐进转向）→ 主导风向 + 合并风力
    assert summarize_wind_eda(["北风0-1级", "南风0-1级", "北风0-1级"]) == "以北风为主，风力0~1级"


def test_gradual_weak_turn_keeps_phase_path():
    # 0~2 级且相邻方位单向渐进转向（北→东北→东，每步 45°）→ 保留完整阶段
    assert summarize_wind_eda(["北风0-1级", "东北风0-1级", "东风0-1级"]) == "北到东风0~1级"


def test_compound_gust_wind_kept_verbatim():
    # 带"阵风"的复合风况不进简单解析，原样保留
    assert summarize_wind_eda(["北风3-4级阵风6级", "南风1-2级"]) == "北风3-4级阵风6级转南风1~2级"


def test_empty_and_placeholder():
    assert summarize_wind_eda([]) is None
    assert summarize_wind_eda(["", "--", None]) is None


def test_single_wind():
    assert summarize_wind_eda(["北风3-4级"]) == "北风3~4级"
