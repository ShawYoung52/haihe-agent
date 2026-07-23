"""暴雨影响河流传播时间（river_propagation）离线验证脚本。

用途：内网离线服务器无 pytest 时，用本脚本验证传播时间功能的关键行为。
依赖：仅标准库 + 项目自身代码（message_orchestrator 用例需要 chainlit 环境，缺失时自动跳过）。

用法（在 haiheliuyubaoyuagent-master 目录下，用服务器的项目 python）：
    python scripts/verify_river_propagation_offline.py                     # 单元用例（无需 DB）
    python scripts/verify_river_propagation_offline.py --csv rain.csv      # CSV→GeoJSON 真实链路（需 DB+pkl）
    python scripts/verify_river_propagation_offline.py --csv rain.csv --aggregate-only  # 只验证 CSV 聚合

CSV 格式：必须含 Station_Id_C, Datetime, PRE, Lat, Lon 列（可选 City/Station_Name/Cnty/Province/Town）。

退出码：0 = 全部通过；1 = 有用例失败。
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT.parent / "hhlyqyxt-master" / "utils"))
sys.path.insert(0, str(REPO_ROOT / "haihe-weather-analyzer-mcp"))

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    """用例装饰器：记录通过/失败，不中断后续用例。"""

    def wrapper(fn):
        try:
            fn()
            _RESULTS.append((name, True, ""))
        except Exception:
            _RESULTS.append((name, False, traceback.format_exc(limit=3)))
        return fn

    return wrapper


def _raises(fn, exc_type=ValueError) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# 1. 牵引核心：rainfall_impact_geojson
# ---------------------------------------------------------------------------

import rainfall_impact_geojson as rig  # noqa: E402


def _direct(name, length_km, **extra):
    edge = {"edge_key": f"k-{name}-{length_km}", "river_name": name, "length_km": length_km}
    edge.update(extra)
    return edge


def _downstream(name, end_km):
    return {"edge_key": f"d-{name}-{end_km}", "river_name": name, "end_distance_km": end_km}


@check("核心: 下游边取最大 end_distance_km，时间=距离/7.2")
def _():
    r = rig._build_river_propagation({"a": _direct("滦河", 3.0)},
                                     [_downstream("滦河", 36.0), _downstream("滦河", 12.0)], 2.0)
    river = r["rivers"][0]
    assert r["flow_velocity_mps"] == 2.0 and len(r["rivers"]) == 1
    assert river["propagation_distance_km"] == 36.0
    assert river["propagation_time_hours"] == 5.0
    assert river["arrival_estimate_readable"] == "约5.0小时"
    assert river["has_downstream"] is True


@check("核心: 仅直接边河流取最长直接河段，分钟级可读文本")
def _():
    r = rig._build_river_propagation({"a": _direct("东河", 1.8), "b": _direct("东河", 3.6)}, [], 2.0)
    river = r["rivers"][0]
    assert river["propagation_distance_km"] == 3.6
    assert river["propagation_time_hours"] == 0.5
    assert river["arrival_estimate_readable"] == "约30分钟"
    assert river["has_downstream"] is False


@check("核心: 下游口径优先于直接边（直接边更长也取下游）")
def _():
    river = rig._build_river_propagation({"a": _direct("滦河", 10.0)}, [_downstream("滦河", 5.0)], 2.0)["rivers"][0]
    assert river["propagation_distance_km"] == 5.0 and river["has_downstream"] is True


@check("核心: NaN 长度跳过 + 按传播时间降序")
def _():
    r = rig._build_river_propagation(
        {"a": _direct("甲河", float("nan")), "b": _direct("乙河", 7.2)},
        [_downstream("丙河", 72.0)], 2.0)
    assert [x["river_name"] for x in r["rivers"]] == ["丙河", "乙河"]


@check("核心: 空输入返回同构空块")
def _():
    assert rig._build_river_propagation({}, [], 2.0) == {"flow_velocity_mps": 2.0, "rivers": []}


@check("核心: 滦河单字名经映射回填，与 GeoJSON 口径一致")
def _():
    r = rig._build_river_propagation(
        {"a": _direct("滦", 3.6, is_luan=True, objectid="1")}, [], 2.0, luan_mapping={"1": "滦河"})
    assert r["rivers"][0]["river_name"] == "滦河"


@check("核心: 非法流速（0/负/NaN）抛 ValueError")
def _():
    assert _raises(lambda: rig._validate_params(50.0, 30.0, 50.0, 0.0))
    assert _raises(lambda: rig._validate_params(50.0, 30.0, 50.0, -1.0))
    assert _raises(lambda: rig._validate_params(50.0, 30.0, 50.0, float("nan")))


@check("核心: _empty_result 带同构 river_propagation 空块")
def _():
    r = rig._empty_result(stations=[], threshold=50.0, buffer_km=30.0, downstream_km=50.0,
                          direct_match_km=10.0, schema="public", table="t", graph_path=None,
                          extra=None, flow_velocity_mps=3.0)
    assert r["river_propagation"] == {"flow_velocity_mps": 3.0, "rivers": []}


# ---------------------------------------------------------------------------
# 2. MCP 适配层：fixed_rainfall_impact_tool
# ---------------------------------------------------------------------------

import fixed_rainfall_impact_tool as frit  # noqa: E402


@check("MCP: 流速解析 0/None→默认2.0，负/NaN 报错")
def _():
    assert frit._resolve_flow_velocity(0) == 2.0
    assert frit._resolve_flow_velocity(None) == 2.0
    assert frit._resolve_flow_velocity(3.0) == 3.0
    assert _raises(lambda: frit._resolve_flow_velocity(-1))
    assert _raises(lambda: frit._resolve_flow_velocity(float("nan")))


@check("MCP: 空结果与有结果响应都含 river_propagation 键")
def _():
    empty = frit._empty_response({"time_range_readable": "t"}, 50.0, set(), set(), 10.0)
    assert empty["river_propagation"] == {"flow_velocity_mps": 2.0, "rivers": []}
    result = {"segments": [], "river_geojson": None, "downstream_start_stats": {},
              "affected_rivers": ["滦河"], "impact_stations": [],
              "river_propagation": {"flow_velocity_mps": 2.0, "rivers": [
                  {"river_name": "滦河", "propagation_distance_km": 48.2,
                   "propagation_time_hours": 6.7, "arrival_estimate_readable": "约6.7小时",
                   "has_downstream": True}]}}
    resp = frit._format_mcp_response(result, {"time_range_readable": "t"}, 50.0, set(), set())
    assert resp["river_propagation"]["rivers"][0]["propagation_time_hours"] == 6.7
    # 旧版核心无该字段时自动补空块（向后兼容）
    del result["river_propagation"]
    resp2 = frit._format_mcp_response(result, {"time_range_readable": "t"}, 50.0, set(), set())
    assert resp2["river_propagation"] == {"flow_velocity_mps": 2.0, "rivers": []}


@check("MCP: flow_velocity_mps 透传到核心 builder（含 0→默认）")
def _():
    captured = {}

    def fake_builder(stations, **kwargs):
        captured.update(kwargs)
        return {"segments": [], "river_geojson": None, "downstream_start_stats": {},
                "affected_rivers": [], "impact_stations": [],
                "river_propagation": {"flow_velocity_mps": kwargs["flow_velocity_mps"], "rivers": []}}

    original = frit._load_impact_builder
    frit._load_impact_builder = lambda: fake_builder
    try:
        rainfall = {"time_range_readable": "t", "level_analysis": [
            {"level": "暴雨", "stations": [{"name": "s1", "lon": 117.0, "lat": 39.0, "rainfall": 80.0}]}]}
        base = dict(time_str="20260723080000", start_time="", end_time="", rainfall_threshold_mm=50.0,
                    max_edges=100, include_background=True, downstream_km=50.0, direct_graph_match_km=10.0,
                    pg_conf={}, analyze_rainfall_core=lambda *a, **k: rainfall,
                    rain_levels=[("暴雨", 50.0, 99.9)], graph_path=None)
        frit.build_affected_river_network_result(**base, flow_velocity_mps=3.0)
        assert captured["flow_velocity_mps"] == 3.0
        frit.build_affected_river_network_result(**base)
        assert captured["flow_velocity_mps"] == 2.0
    finally:
        frit._load_impact_builder = original


# ---------------------------------------------------------------------------
# 3. 问答层简报（需要 chainlit 环境，缺失则跳过）
# ---------------------------------------------------------------------------

try:
    sys.path.insert(0, str(REPO_ROOT))
    from chainlitexam import message_orchestrator as mo  # noqa: E402

    @check("问答: brief 含传播时间行（下游措辞）")
    def _():
        result = {
            "time_range_readable": "t", "rainfall_threshold_mm": 50.0,
            "affected_rivers": ["滦河"], "affected_zone_77_regions": [],
            "affected_admin_divisions": [], "total_segments": 3, "affected_segments": 3,
            "river_propagation": {"flow_velocity_mps": 2.0, "rivers": [
                {"river_name": "滦河", "propagation_distance_km": 48.2,
                 "propagation_time_hours": 6.7, "arrival_estimate_readable": "约6.7小时",
                 "has_downstream": True}]},
        }
        brief = mo._build_affected_river_network_brief(result, "暴雨影响哪些河系")
        assert "按经验流速 2.0 m/s 估算" in brief and "约6.7小时" in brief and "传播至下游" in brief

    @check("问答: brief 缺 river_propagation 时向后兼容")
    def _():
        result = {"time_range_readable": "t", "rainfall_threshold_mm": 50.0,
                  "affected_rivers": ["滦河"], "affected_zone_77_regions": [],
                  "affected_admin_divisions": [], "total_segments": 1, "affected_segments": 1}
        brief = mo._build_affected_river_network_brief(result, "暴雨影响哪些河系")
        assert "经验流速" not in brief and "滦河" in brief

except Exception as exc:  # noqa: BLE001
    _RESULTS.append((f"问答层简报用例（跳过：{type(exc).__name__}）", True, ""))


# ---------------------------------------------------------------------------
# CSV → GeoJSON 真实链路验证（--csv 模式）
# ---------------------------------------------------------------------------


def _load_mcp_pg_and_graph(config_path: Path) -> tuple[dict, str]:
    """从 MCP config.ini 读取 postgres 连接与河网图路径（优先 v6 有向图）。"""
    import configparser

    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8-sig")
    pg_conf = dict(config["postgres"])
    graph_path = config.get("paths", "graph", fallback="")
    if graph_path:
        p = Path(graph_path)
        v6 = (p / rig.DIRECTED_GRAPH_FILENAME) if (p.is_dir() or not p.name) else p.with_name(rig.DIRECTED_GRAPH_FILENAME)
        if v6.is_file():
            graph_path = str(v6)
    return pg_conf, graph_path


def run_csv_pipeline(args) -> int:
    """读 5 分钟降水 CSV → 聚合 24h → 生成影响河流 GeoJSON 并打印传播时间。"""
    import json

    print(f"[1/3] 聚合 CSV：{args.csv}")
    station_df = rig.aggregate_5min_station_pre_to_24h(args.csv)
    total = len(station_df)
    heavy = station_df[station_df["rain_24h"] >= args.threshold]
    print(f"      站点总数={total}，≥{args.threshold}mm 站点数={len(heavy)}，"
          f"最大 24h 雨量={float(station_df['rain_24h'].max()) if total else 0.0}mm")
    if total and args.top:
        print(f"      雨量前 {min(args.top, total)} 站：")
        for _, row in station_df.head(args.top).iterrows():
            print(f"        {row['station_id']} {row.get('station_name') or ''} "
                  f"({row['lon']}, {row['lat']}) {row['rain_24h']}mm")
    if args.aggregate_only:
        print("[aggregate-only] 仅聚合 CSV，未访问数据库。")
        return 0

    print("[2/3] 连接数据库与河网图，生成专题图数据...")
    pg_conf, graph_path = _load_mcp_pg_and_graph(Path(args.config))
    result = rig.build_rain24h_impact_river_geojson(
        args.csv,
        rain_threshold_mm=args.threshold,
        pg_conf=pg_conf,
        graph_path=graph_path,
        flow_velocity_mps=args.velocity,
    )

    print("[3/3] 结果汇总")
    affected = result.get("affected_rivers", [])
    print(f"      受影响河流 {len(affected)} 条：{'、'.join(affected) if affected else '无'}")
    propagation = result.get("river_propagation") or {}
    rivers = propagation.get("rivers") or []
    if rivers:
        print(f"      传播时间估算（经验流速 {propagation.get('flow_velocity_mps')} m/s）：")
        for r in rivers:
            scope = "下游" if r.get("has_downstream") else "就地河段"
            print(f"        {r['river_name']}: {r['propagation_distance_km']}km（{scope}）"
                  f" → {r['arrival_estimate_readable']}")
    else:
        print("      无传播时间数据（未达阈值或无受影响河流）。")

    out_path = Path(args.out)
    payload = {
        "river_geojson": result.get("river_geojson"),
        "station_geojson": result.get("station_geojson"),
        "affected_rivers": affected,
        "river_propagation": propagation,
        "params": result.get("params"),
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"      GeoJSON 已写出：{out_path.resolve()}（可用 QGIS 或前端打开查看）")
    return 0


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------


def _parse_args(argv):
    import argparse

    parser = argparse.ArgumentParser(description="暴雨影响河流传播时间离线验证")
    parser.add_argument("--csv", help="5 分钟站点降水 CSV 路径（提供后走真实链路，不再跑单元用例）")
    parser.add_argument("--out", default="rain_impact_result.geojson", help="GeoJSON 输出路径")
    parser.add_argument("--threshold", type=float, default=50.0, help="暴雨阈值 mm（默认 50）")
    parser.add_argument("--velocity", type=float, default=2.0, help="经验流速 m/s（默认 2.0）")
    parser.add_argument("--config", default=str(REPO_ROOT / "haihe-weather-analyzer-mcp" / "config.ini"),
                        help="MCP config.ini 路径（读取数据库连接与河网图路径）")
    parser.add_argument("--aggregate-only", action="store_true", help="只聚合 CSV，不访问数据库")
    parser.add_argument("--top", type=int, default=10, help="打印雨量前 N 站（默认 10，0 不打印）")
    return parser.parse_args(argv)


if __name__ == "__main__":
    cli_args = _parse_args(sys.argv[1:])
    if cli_args.csv:
        sys.exit(run_csv_pipeline(cli_args))

    failed = 0
    for name, ok, detail in _RESULTS:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failed += 1
            print(detail)
    print(f"\n合计 {len(_RESULTS)} 项，通过 {len(_RESULTS) - failed}，失败 {failed}")
    sys.exit(1 if failed else 0)
