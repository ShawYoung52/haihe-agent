# Final Review Fix Report

## Commit

- SHA: `30ab42a`
- Branch: `feat/qa-agent-rain-impact-sync`
- Message: `fix(qa-agent): C1 timezone bug + C2 server.py 30->20 + I1/I2 tests + I4 arrival note`

## Fixes Applied

### C1 (Critical, security) — DONE

`haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py:_derive_rain_end_time`
no longer stamps a `Z` UTC suffix onto naive BJT strings via
`dateparser.parse().strftime("%Y-%m-%dT%H:%M:%SZ")`. The function now returns
the raw string (naive BJT or already-tz-aware). The traction-side
`hhlyqyxt-master/utils/rainfall_impact_geojson.py:_normalize_end_time`
performs the correct `naive → Asia/Shanghai → UTC` normalization. Removes the
8-hour drift that was propagating into `estimated_arrival_time`.

Both dateutil `parser` imports inside `_derive_rain_end_time` were deleted.

### C2 (Critical, contract) — DONE

`haihe-weather-analyzer-mcp/server.py:92` `rainfall_impact_rule.direct` changed
from `"30km缓冲区..."` to `"20km缓冲区..."` to match the actual
`DEFAULT_STATION_BUFFER_KM = 20.0` and the wording already in
`fixed_rainfall_impact_tool.IMPACT_RULES.direct`. The `propagation` entry was
extended with the `estimated_arrival_time = t0_source_time + propagation_time_hours`
disclosure (reviewer suggestion, since no separate `arrival` field exists here).

### I1 + I2 (Important, test robustness) — DONE

`haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`:
- `test_derive_rain_end_time_from_time_range_readable` rewritten to use the
  production separator ` ~ ` (matches `tools.py:1399`) with a strict
  equality assertion (`end == "2026-07-23 08:00"`) instead of the weak
  substring probe that masked C1.
- Added `test_derive_rain_end_time_supports_zhi_separator` for backward
  compatibility with the legacy ` 至 ` format.
- No `test_derive_rain_end_time_from_time_range_iso_pair` exists in the test
  file, so no additional edit was required.

### I4 (Important, docs) — DONE

`haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py:IMPACT_RULES["arrival"]`
now discloses that all `rainstorm_stations` share the query-window end time
(not per-station real rain-stop time), so every direct-edge T0 collapses to
the same value — with a note that this can be relaxed once upstream provides
per-station real `rain_end_time`.

## Verification

`D:/PythonProject/haiheliuyubaoyuagent-master/.venv/Scripts/python.exe -m pytest test_fixed_rainfall_impact_propagation.py -v`

Result: **18 passed** in 0.03s (17 pre-existing + 1 new backward-compat test).

## Files Touched

- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py`
- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/server.py`
- `haiheliuyubaoyuagent-master/haihe-weather-analyzer-mcp/test_fixed_rainfall_impact_propagation.py`

## Skipped

Minor items M1-M4 intentionally not addressed per instructions (dead code,
lazy import, CLAUDE.md line length, unreachable regex corner case).
