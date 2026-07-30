"""分钟降水聚合工具（牵引侧独立实现，不跨仓库 import 问答智能体）。

参考 haihe-weather-analyzer-mcp/haihe_mcp_tools.py 的
aggregate_minute_precipitation 语义与口径，独立实现在牵引侧
以避免跨仓库依赖（内网服务器不并排放两个仓库）。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Optional, Sequence


TRUSTED_Q_PRE: frozenset = frozenset({"0", "3", "4"})


def _parse_datetime(r: dict) -> Optional[datetime]:
    """从记录中解析 Datetime，优先 Datetime 字段，其次 Year/Mon/Day/Hour/Min。"""
    dt = r.get("Datetime")
    if isinstance(dt, datetime):
        return dt
    if dt:
        text = str(dt).strip()
        for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    try:
        return datetime(
            int(r["Year"]), int(r["Mon"]), int(r["Day"]),
            int(r["Hour"]), int(r.get("Min", 0)), 0,
        )
    except (KeyError, ValueError, TypeError):
        return None


def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None or v == "" or v == "None":
        return default
    text = str(v).strip()
    if text in {"999999", "999999.0", "999990", "999990.0", "-9999", "-9999.0"}:
        return default
    try:
        return float(text)
    except (ValueError, TypeError):
        return default


def _q_pre_valid(q_pre: Any, trusted: frozenset) -> bool:
    if q_pre is None or str(q_pre).strip() == "":
        return True  # 未标注视为可信
    if not trusted:
        return True
    return str(q_pre).strip() in trusted


def aggregate_minute_precipitation(
    records: Sequence[dict],
    end_time: datetime,
    windows_hours: Sequence[int] = (1, 12, 24),
    trusted_q_pre: frozenset = TRUSTED_Q_PRE,
) -> list[dict]:
    """按站聚合分钟降水累计。

    Args:
        records: 分钟降水记录列表（含 Station_Id_C/Datetime/PRE/Q_PRE 等字段）
        end_time: 聚合窗口结束时刻（BJT）
        windows_hours: 累计窗口小时数，默认 (1, 12, 24)
        trusted_q_pre: 可信 Q_PRE 标志集合，默认 {"0","3","4"}；空集表示不过滤

    Returns:
        每站一条聚合结果，包含：
        - Station_Id_C 及元信息（Lat/Lon/City/Station_Name/Station_levl 等）
        - PRE_{h}h: 各窗口累计降水
        - pre_count_{h}h: 各窗口参与累加的分钟数
    """
    if not records:
        return []

    windows = sorted(set(int(h) for h in windows_hours))
    max_window = max(windows) if windows else 24
    max_cutoff = end_time - timedelta(hours=max_window)
    window_cutoffs = {h: end_time - timedelta(hours=h) for h in windows}

    by_station: dict[str, dict] = {}
    latest_dt: dict[str, datetime] = {}
    station_meta: dict[str, dict] = {}

    for r in records:
        sid = r.get("Station_Id_C")
        if not sid:
            continue
        sid = str(sid).strip()
        if not sid:
            continue

        dt = _parse_datetime(r)
        # 保留 [end_time - max_window, end_time]（闭区间），与 CSV 侧 `>=` 边界一致
        if dt is None or dt > end_time or dt < max_cutoff:
            continue

        if not _q_pre_valid(r.get("Q_PRE"), trusted_q_pre):
            continue

        pre = _safe_float(r.get("PRE"))
        if pre < 0:
            continue

        if sid not in by_station:
            by_station[sid] = {f"PRE_{h}h": 0.0 for h in windows}
            for h in windows:
                by_station[sid][f"pre_count_{h}h"] = 0
            latest_dt[sid] = dt
            station_meta[sid] = dict(r)
        elif dt > latest_dt[sid]:
            latest_dt[sid] = dt
            station_meta[sid] = dict(r)

        for h, cutoff in window_cutoffs.items():
            # 闭区间 [end_time - h, end_time]，与 24h 主窗口一致
            if dt >= cutoff:
                by_station[sid][f"PRE_{h}h"] += pre
                by_station[sid][f"pre_count_{h}h"] += 1

    out = []
    for sid, sums in by_station.items():
        row = dict(station_meta[sid])
        row.update(sums)
        row["Station_Id_C"] = sid
        out.append(row)
    return out
