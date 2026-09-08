"""逐小时风力风向（EDA）时段汇总——与 MCP rolling_forecast_service._summarize_tod_wind 同口径。

2026-09-04 甲方反馈：点位时段表把逐小时 EDA 去重后用"转"硬拼，出
"北风4-5级转北风3-4级转西北风1-2级转西北风3-4级"这种机械长串。这里把连续
同风向的多条合并风力区间（北风4-5级 + 北风3-4级 → 北风3~5级），风向变化才用
"转"连接。纯代码确定性、零编造：只重组工具返回的 EDA 文本，不引入新数值。

说明：MCP 侧 `rolling_forecast_service.summarize_wind_eda` 是同逻辑公共别名；
两侧若调整口径需同步（本模块为零依赖纯函数，避免 chainlitexam 反向依赖 MCP 包）。
"""
from __future__ import annotations

import re
from collections import Counter

_WIND_DIRECTION_ANGLE = {
    "北风": 0, "东北风": 45, "东风": 90, "东南风": 135,
    "南风": 180, "西南风": 225, "西风": 270, "西北风": 315,
}
_SIMPLE_WIND_RE = re.compile(
    r"^(?P<direction>[东南西北偏]+风)\s*"
    r"(?P<lo>\d+)\s*(?:[-~～]\s*(?P<hi>\d+)\s*)?级$"
)


def summarize_wind_eda(eda_values: list) -> str | None:
    """时段汇总风力风向：按连续风向阶段合并风力区间，风向变化用"转"连接。

    连续同风向的多条合并风力区间；带"阵风"等附加语义的复合风况及无风向词的
    原文（如"静风"）原样保留。0~2 级弱风只在相邻方位持续单向演变时表达具体阶段；
    大角度跳变或来回摆动时保留出现次数最多的实际风向；无唯一主导风向时保留首尾
    转向，首尾相同则保留连续去重后的完整阶段路径。较强风和复合原文不进弱风压缩分支。
    """
    entries: list[dict[str, object]] = []
    observed_forces: list[tuple[int, int]] = []
    observed_directions: list[str] = []
    for raw in eda_values:
        e = str(raw or "").strip()
        if not e or e == "--":
            continue
        match = _SIMPLE_WIND_RE.fullmatch(e)
        if not match:
            if not entries or entries[-1].get("raw") != e:
                entries.append({"raw": e})
            continue
        direction = match.group("direction")
        lo = int(match.group("lo"))
        hi = int(match.group("hi") or lo)
        observed_forces.append((lo, hi))
        observed_directions.append(direction)
        if entries and entries[-1].get("direction") == direction:
            phase = entries[-1]
            phase["lo"] = min(int(phase["lo"]), lo)
            phase["hi"] = max(int(phase["hi"]), hi)
            samples = phase["force_samples"]
            if (lo, hi) not in samples:
                samples.append((lo, hi))
        else:
            entries.append({
                "direction": direction,
                "lo": lo,
                "hi": hi,
                "force_samples": [(lo, hi)],
            })

    phases = [entry for entry in entries if "direction" in entry]

    if len(phases) > 1 and len(phases) == len(entries) and observed_forces:
        directions = [str(phase["direction"]) for phase in phases]
        angles = [_WIND_DIRECTION_ANGLE.get(direction) for direction in directions]
        if all(angle is not None for angle in angles) and max(force[1] for force in observed_forces) <= 2:
            deltas = [
                (int(current) - int(previous) + 180) % 360 - 180
                for previous, current in zip(angles, angles[1:])
            ]
            gradual = (
                len(set(directions)) == len(directions)
                and all(abs(delta) == 45 for delta in deltas)
                and (all(delta > 0 for delta in deltas) or all(delta < 0 for delta in deltas))
            )
            if not gradual:
                lo = min(force[0] for force in observed_forces)
                hi = max(force[1] for force in observed_forces)
                force = f"{lo}级" if lo == hi else f"{lo}~{hi}级"
                counts = Counter(observed_directions)
                highest = max(counts.values())
                leaders = [direction for direction, count in counts.items() if count == highest]
                if len(leaders) == 1:
                    direction_summary = f"以{leaders[0]}为主"
                elif observed_directions[0] == observed_directions[-1]:
                    phase_path: list[str] = []
                    for direction in observed_directions:
                        if not phase_path or phase_path[-1] != direction:
                            phase_path.append(direction)
                    direction_summary = "转".join(phase_path)
                else:
                    direction_summary = f"{observed_directions[0]}转{observed_directions[-1]}"
                return f"{direction_summary}，风力{force}"

    def format_phase(entry: dict[str, object]) -> str:
        direction = str(entry["direction"])
        lo, hi = int(entry["lo"]), int(entry["hi"])
        return f"{direction}{lo}级" if lo == hi else f"{direction}{lo}~{hi}级"

    def is_gradual_adjacent_turn(group: list[dict[str, object]]) -> bool:
        angles = [_WIND_DIRECTION_ANGLE.get(str(entry["direction"])) for entry in group]
        if any(angle is None for angle in angles):
            return False
        deltas = [
            (int(current) - int(previous) + 180) % 360 - 180
            for previous, current in zip(angles, angles[1:])
        ]
        return bool(deltas) and all(abs(delta) == 45 for delta in deltas) and (
            all(delta > 0 for delta in deltas) or all(delta < 0 for delta in deltas)
        )

    parts: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if "raw" in entry:
            parts.append(str(entry["raw"]))
            index += 1
            continue

        samples = entry["force_samples"]
        stable_force = samples[0] if len(samples) == 1 else None
        group = [entry]
        next_index = index + 1
        while stable_force is not None and next_index < len(entries):
            candidate = entries[next_index]
            if "raw" in candidate:
                break
            candidate_samples = candidate["force_samples"]
            if len(candidate_samples) != 1 or candidate_samples[0] != stable_force:
                break
            group.append(candidate)
            next_index += 1

        if stable_force is not None and stable_force[1] <= 2 and is_gradual_adjacent_turn(group):
            start = str(group[0]["direction"]).removesuffix("风")
            end = str(group[-1]["direction"])
            lo, hi = stable_force
            force = f"{lo}级" if lo == hi else f"{lo}~{hi}级"
            parts.append(f"{start}到{end}{force}")
        else:
            parts.extend(format_phase(phase) for phase in group)
        index = next_index
    return "转".join(parts) if parts else None
