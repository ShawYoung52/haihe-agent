from __future__ import annotations

import configparser
import json
import os
from typing import Any, Dict, List, Optional

# 直接复用仓库中的“核心判定逻辑”
from constants import DEFAULT_BASIN_CODES, DEFAULT_OBS_ELEMENTS, DEFAULT_THRESHOLDS_MM, _looks_like_nine_zone_codes
from haihe_mcp_tools import (
    _forecast_evaluate_core,
    _forecast_filter_core,
    _forecast_report_core,
    _normalize_time_param,
    _observation_fetch_core,
    _parse_forecast_start_time,
    BusinessException,
    DEFAULT_EC_OUTPUT_PATH,
    deduplicate_latest_records,
    ec_forecast_precip_files_by_horizon,
    evaluate_haihe_forecast_emergency_response_core,
    evaluate_observation_response,
    filter_records_by_station_levels,
    safe_float,
    station_id_of,
)

DEFAULT_HAIHE_BASIN_CODES = os.getenv("HAIHE_BASIN_CODES", DEFAULT_BASIN_CODES).strip() or DEFAULT_BASIN_CODES
DEFAULT_NINE_ZONE_BASIN_CODES = os.getenv("NINE_ZONE_BASIN_CODES", "").strip()
_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(_ROOT_DIR, "config.ini")
_DEFAULT_NINE_ZONE_TABLE = os.getenv("NINE_ZONE_TABLE", "haihe_zone_9").strip() or "haihe_zone_9"
_DEFAULT_NINE_ZONE_CODE_FIELD = os.getenv("NINE_ZONE_CODE_FIELD", "zone_code").strip() or "zone_code"


def _is_safe_identifier(name: str) -> bool:
    txt = (name or "").strip()
    if not txt:
        return False
    return txt.replace("_", "").isalnum()


def _parse_code_csv(basin_codes: str) -> List[str]:
    return [x.strip() for x in str(basin_codes or "").split(",") if x.strip()]


def _is_nine_zone_scope(scope: str) -> bool:
    normalized = (scope or "").strip().lower()
    return normalized in ("nine_zone", "nine-zone", "ninezone", "local", "9zone", "9")


def _read_pg_schema(config_path: str) -> str:
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    if cp.has_section("postgres"):
        return cp.get("postgres", "schema", fallback="public").strip() or "public"
    return "public"


def _load_nine_zone_codes_from_db(config_path: str) -> str:
    try:
        import psycopg2
    except Exception:
        return ""
    if not os.path.isfile(config_path):
        return ""
    if not _is_safe_identifier(_DEFAULT_NINE_ZONE_TABLE):
        return ""
    if not _is_safe_identifier(_DEFAULT_NINE_ZONE_CODE_FIELD):
        return ""
    schema = _read_pg_schema(config_path)
    if not _is_safe_identifier(schema):
        schema = "public"
    sql = (
        f"SELECT DISTINCT {_DEFAULT_NINE_ZONE_CODE_FIELD} AS zone_code "
        f"FROM {schema}.{_DEFAULT_NINE_ZONE_TABLE} "
        f"WHERE {_DEFAULT_NINE_ZONE_CODE_FIELD} IS NOT NULL "
        f"AND BTRIM({_DEFAULT_NINE_ZONE_CODE_FIELD}) <> '' "
        f"ORDER BY zone_code"
    )
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    if not cp.has_section("postgres"):
        return ""
    pg = cp["postgres"]
    try:
        conn = psycopg2.connect(
            host=pg.get("host", "127.0.0.1"),
            port=pg.getint("port", 5432),
            dbname=pg.get("dbname", "postgres"),
            user=pg.get("user", "postgres"),
            password=pg.get("password", ""),
            sslmode=pg.get("sslmode", "prefer"),
            connect_timeout=pg.getint("connect_timeout", 5),
        )
    except Exception:
        return ""
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall() or []
        values = [str(r[0]).strip() for r in rows if r and r[0] is not None and str(r[0]).strip()]
        return ",".join(values)
    except Exception:
        return ""
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _load_nine_zone_union_wkt(config_path: str, zone_codes: List[str]) -> str:
    try:
        import psycopg2
    except Exception as exc:
        raise ValueError(f"九分区筛站依赖 psycopg2，当前不可用: {exc}") from exc
    if not os.path.isfile(config_path):
        raise ValueError(f"配置文件不存在: {config_path}")
    if not _is_safe_identifier(_DEFAULT_NINE_ZONE_TABLE):
        raise ValueError(f"非法九分区表名: {_DEFAULT_NINE_ZONE_TABLE}")
    if not _is_safe_identifier(_DEFAULT_NINE_ZONE_CODE_FIELD):
        raise ValueError(f"非法九分区编码字段: {_DEFAULT_NINE_ZONE_CODE_FIELD}")
    schema = _read_pg_schema(config_path)
    if not _is_safe_identifier(schema):
        schema = "public"
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    if not cp.has_section("postgres"):
        raise ValueError("config.ini 缺少 [postgres]，无法读取九分区边界")
    pg = cp["postgres"]
    sql = (
        f"SELECT ST_AsText(ST_UnaryUnion(ST_Collect(geom))) AS wkt "
        f"FROM {schema}.{_DEFAULT_NINE_ZONE_TABLE} "
        f"WHERE {_DEFAULT_NINE_ZONE_CODE_FIELD} = ANY(%s)"
    )
    conn = psycopg2.connect(
        host=pg.get("host", "127.0.0.1"),
        port=pg.getint("port", 5432),
        dbname=pg.get("dbname", "postgres"),
        user=pg.get("user", "postgres"),
        password=pg.get("password", ""),
        sslmode=pg.get("sslmode", "prefer"),
        connect_timeout=pg.getint("connect_timeout", 5),
    )
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (zone_codes,))
                row = cur.fetchone()
        wkt = (str(row[0]).strip() if row and row[0] is not None else "")
        if not wkt:
            raise ValueError(f"九分区表未查到对应编码: {','.join(zone_codes)}")
        return wkt
    finally:
        try:
            conn.close()
        except Exception:
            pass


def filter_records_by_nine_zone(
    records: List[Dict[str, Any]],
    basin_codes: str,
    *,
    config_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    zone_codes = _parse_code_csv(basin_codes)
    if not zone_codes:
        return []
    cfg = os.path.abspath(config_path or _DEFAULT_CONFIG)
    union_wkt = _load_nine_zone_union_wkt(cfg, zone_codes)
    try:
        ogr = __import__("osgeo.ogr", fromlist=["ogr"]).ogr
    except Exception:
        from osgeo import ogr  # type: ignore
    geom = ogr.CreateGeometryFromWkt(union_wkt)
    if geom is None:
        raise ValueError("九分区几何解析失败")
    out: List[Dict[str, Any]] = []
    for r in records:
        lon = safe_float(r.get("Lon"))
        lat = safe_float(r.get("Lat"))
        if abs(lon) < 0.01 or abs(lat) < 0.01:
            continue
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint(float(lon), float(lat))
        if geom.Intersects(pt):
            out.append(r)
    return out


def resolve_emergency_basin_codes(
    basin_codes: Optional[str] = None,
    scope: str = "haihe",
    config_path: Optional[str] = None,
) -> str:
    """
    统一解析应急判定范围编码：
    - scope=haihe：默认 HHLY（可由 HAIHE_BASIN_CODES 覆盖）
    - scope=nine_zone：默认读取 NINE_ZONE_BASIN_CODES；若未配置则报错提示显式传 basin_codes
    """
    if basin_codes is not None and str(basin_codes).strip():
        return str(basin_codes).strip()

    normalized = (scope or "haihe").strip().lower()
    if normalized in ("haihe", "basin", "hhly"):
        return DEFAULT_HAIHE_BASIN_CODES
    if normalized in ("nine_zone", "nine-zone", "ninezone", "local", "9zone", "9"):
        if DEFAULT_NINE_ZONE_BASIN_CODES:
            return DEFAULT_NINE_ZONE_BASIN_CODES
        cfg = os.path.abspath(config_path or _DEFAULT_CONFIG)
        db_codes = _load_nine_zone_codes_from_db(cfg)
        if db_codes:
            return db_codes
        raise ValueError(
            "scope=nine_zone 但未解析到分区编码：请配置 NINE_ZONE_BASIN_CODES，"
            "或在库表 haihe_zone_9.zone_code 中提供数据，或显式传 basin_codes"
        )
    raise ValueError(f"不支持的 scope={scope!r}，可选 haihe / nine_zone")


def _simplify_result(core_result: Dict[str, Any], *, include_evidence: bool = False) -> Dict[str, Any]:
    """
    将核心判定结果归一成外部可消费的结构：
      - reached: 是否触发（达到应急条件）
      - level: 触发等级（I/II/III/IV），未触发时为 None
    """
    reached = bool(core_result.get("triggered"))
    level = core_result.get("level") if reached else None
    out: Dict[str, Any] = {
        "reached": reached,
        "level": level,
        "message": core_result.get("message"),
    }
    # 默认不返回 evidence，避免“adjacent/neighbor_km”等字段命名带来的误解；
    # 如你确实需要证据细节，再显式开启。
    if include_evidence and "evidence" in core_result:
        out["evidence"] = core_result.get("evidence")
    return out


def _normalize_station_record_from_export_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sid = str(row.get("station_id") or "").strip()
    if not sid:
        return None
    lon = safe_float(row.get("lon"))
    lat = safe_float(row.get("lat"))
    if lon is None or lat is None:
        return None
    return {
        "Station_Id_C": sid,
        "Station_Name": row.get("station") or sid,
        "Station_levl": row.get("station_level"),
        "Lat": lat,
        "Lon": lon,
        "City": row.get("city"),
        "Cnty": row.get("cnty"),
    }


def _load_local_station_records(
    *,
    start_time: str,
    local_station_json_path: str,
    allowed_station_levels: str,
) -> List[Dict[str, Any]]:
    path = os.path.abspath(local_station_json_path)
    if not os.path.isfile(path):
        raise BusinessException(f"本地站点JSON不存在: {path}")
    try:
        payload = json.loads(open(path, "r", encoding="utf-8").read())
    except Exception as exc:
        raise BusinessException(f"读取本地站点JSON失败: {exc}") from exc
    slots = payload.get("slots")
    if not isinstance(slots, list):
        raise BusinessException("本地站点JSON格式错误：缺少 slots 列表")
    target_times = _parse_forecast_start_time(start_time).strftime("%Y%m%d%H0000")
    station_rows: List[Dict[str, Any]] = []
    for slot in slots:
        if not isinstance(slot, dict) or not bool(slot.get("ok")):
            continue
        if str(slot.get("times") or "").strip() != target_times:
            continue
        p = slot.get("payload")
        items = p.get("list") if isinstance(p, dict) else None
        if not isinstance(items, list):
            continue
        for row in items:
            if not isinstance(row, dict):
                continue
            normalized = _normalize_station_record_from_export_row(row)
            if normalized:
                station_rows.append(normalized)
    if not station_rows:
        raise BusinessException(
            f"本地站点JSON未找到 times={target_times} 的有效站点数据，请确认导出时次覆盖该 start_time"
        )
    levels = [x.strip() for x in str(allowed_station_levels).split(",") if x.strip()]
    station_rows = filter_records_by_station_levels(station_rows, levels)
    station_rows = deduplicate_latest_records(station_rows)
    if not station_rows:
        raise BusinessException("本地站点JSON筛选后无有效站点，请检查 allowed_station_levels")
    return station_rows


def _evaluate_forecast_with_local_station_records(
    *,
    start_time: str,
    basin_codes: str,
    ec_output_path: str,
    allowed_station_levels: str,
    rainstorm_12h: float,
    rainstorm_24h: float,
    severe_rainstorm_24h: float,
    extraordinary_24h: float,
    sustain_threshold_6h_mm: float,
    sample_method: str,
    typhoon_landing_impact: bool,
    typhoon_impact_increasing: bool,
    include_records: bool,
    local_station_json_path: str,
) -> Dict[str, Any]:
    parsed_start = _parse_forecast_start_time(start_time)
    ec_files = ec_forecast_precip_files_by_horizon(parsed_start, ec_output_path)
    if not ec_files.get("24h") and not ec_files.get("12h"):
        raise BusinessException("本地模式下未找到 12h/24h 预报文件，请检查 ec_output_path 与 start_time")
    station_records = _load_local_station_records(
        start_time=start_time,
        local_station_json_path=local_station_json_path,
        allowed_station_levels=allowed_station_levels,
    )
    total_station_ids = {station_id_of(r) for r in station_records if station_id_of(r)}
    total_count = len(total_station_ids)
    filtered = _forecast_filter_core(
        station_records=station_records,
        ec_files_paths=ec_files,
        sample_method=sample_method,
        sustain_threshold_6h_mm=sustain_threshold_6h_mm,
    )
    checks = _forecast_evaluate_core(
        station_records=station_records,
        total_count=total_count,
        rain24=filtered["rain24"],
        rain12=filtered["rain12"],
        sustained_station_ids=filtered["sustained_station_ids"],
        rainstorm_12h=rainstorm_12h,
        rainstorm_24h=rainstorm_24h,
        severe_rainstorm_24h=severe_rainstorm_24h,
        extraordinary_24h=extraordinary_24h,
        typhoon_landing_impact=typhoon_landing_impact,
        typhoon_impact_increasing=typhoon_impact_increasing,
    )
    levels = [x.strip() for x in str(allowed_station_levels).split(",") if x.strip()]
    return _forecast_report_core(
        checks=checks,
        parsed_start_time=parsed_start,
        basin_codes=basin_codes,
        ec_output_path=ec_output_path,
        allowed_levels=levels,
        sample_method=sample_method,
        typhoon_landing_impact=typhoon_landing_impact,
        typhoon_impact_increasing=typhoon_impact_increasing,
        ec_files_paths=ec_files,
        sustain_source=filtered["sustain_source"],
        sustain_threshold_6h_mm=filtered["sustain_threshold_6h_mm"],
        total_count=total_count,
        include_records=include_records,
        station_records=station_records,
    )


def query_haihe_emergency_observation(
    times: str,
    basin_codes: Optional[str] = None,
    scope: str = "haihe",
    config_path: Optional[str] = None,
    neighbor_km: float = 50.0,
    sustain_hourly_threshold_mm: float = 0.1,
    allowed_station_levels: str = "11,12,13,16",
    rainstorm_12h: float = DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
    rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
    severe_rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
    extraordinary_24h: float = DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
    include_records: bool = False,
    include_evidence: bool = False,
) -> Dict[str, Any]:
    """
    实况口径：判断是否达到 I/II/III/IV 级应急响应条件。

    参数 times：例如 20250723080000
    """
    if not times:
        raise ValueError("times 不能为空，例如 20250723080000")
    times = _normalize_time_param(times)
    basin = resolve_emergency_basin_codes(
        basin_codes=basin_codes,
        scope=scope,
        config_path=config_path,
    )

    source_basin = DEFAULT_HAIHE_BASIN_CODES if (_is_nine_zone_scope(scope) or _looks_like_nine_zone_codes(basin)) else basin
    records = _observation_fetch_core(
        basin_codes=source_basin,
        times=times,
    )
    if _is_nine_zone_scope(scope) or _looks_like_nine_zone_codes(basin):
        records = filter_records_by_nine_zone(records, basin, config_path=config_path)

    allowed_levels_list = [x.strip() for x in allowed_station_levels.split(",") if x.strip()]
    core_result = evaluate_observation_response(
        records=records,
        thresholds_mm={
            "rainstorm_12h": rainstorm_12h,
            "rainstorm_24h": rainstorm_24h,
            "severe_rainstorm_24h": severe_rainstorm_24h,
            "extraordinary_24h": extraordinary_24h,
        },
        neighbor_km=neighbor_km,
        sustain_hourly_threshold_mm=sustain_hourly_threshold_mm,
        allowed_station_levels=allowed_levels_list,
    )

    if include_records:
        core_result["records"] = records
    return _simplify_result(core_result, include_evidence=include_evidence)


def query_haihe_emergency_forecast(
    start_time: str,
    basin_codes: Optional[str] = None,
    scope: str = "haihe",
    ec_output_path: str = DEFAULT_EC_OUTPUT_PATH,
    allowed_station_levels: str = "11,12,13,16",
    rainstorm_12h: float = DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
    rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
    severe_rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
    extraordinary_24h: float = DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
    sustain_threshold_6h_mm: float = 0.1,
    sample_method: str = "nearest",
    typhoon_landing_impact: bool = False,
    typhoon_impact_increasing: bool = False,
    include_records: bool = False,
    include_evidence: bool = False,
    local_station_json_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    预报口径：判断是否达到 I/II/III/IV 级应急响应条件。

    start_time：起报时次，例如 2026032502 或 2025-06-30 02:00:00（必须为整点）
    """
    basin = resolve_emergency_basin_codes(basin_codes=basin_codes, scope=scope)
    try:
        if local_station_json_path and str(local_station_json_path).strip():
            core_result = _evaluate_forecast_with_local_station_records(
                start_time=start_time,
                basin_codes=basin,
                ec_output_path=ec_output_path,
                allowed_station_levels=allowed_station_levels,
                rainstorm_12h=rainstorm_12h,
                rainstorm_24h=rainstorm_24h,
                severe_rainstorm_24h=severe_rainstorm_24h,
                extraordinary_24h=extraordinary_24h,
                sustain_threshold_6h_mm=sustain_threshold_6h_mm,
                sample_method=sample_method,
                typhoon_landing_impact=typhoon_landing_impact,
                typhoon_impact_increasing=typhoon_impact_increasing,
                include_records=include_records,
                local_station_json_path=str(local_station_json_path).strip(),
            )
            # 便于前端/联调确认数据口径来源
            core_result.setdefault("query", {})
            if isinstance(core_result["query"], dict):
                core_result["query"]["station_source"] = "local_json"
                core_result["query"]["local_station_json_path"] = os.path.abspath(str(local_station_json_path).strip())
        else:
            core_result = evaluate_haihe_forecast_emergency_response_core(
                start_time=start_time,
                basin_codes=basin,
                ec_output_path=ec_output_path,
                allowed_station_levels=allowed_station_levels,
                rainstorm_12h=rainstorm_12h,
                rainstorm_24h=rainstorm_24h,
                severe_rainstorm_24h=severe_rainstorm_24h,
                extraordinary_24h=extraordinary_24h,
                sustain_threshold_6h_mm=sustain_threshold_6h_mm,
                sample_method=sample_method,
                typhoon_landing_impact=typhoon_landing_impact,
                typhoon_impact_increasing=typhoon_impact_increasing,
                include_records=include_records,
            )
        return _simplify_result(core_result, include_evidence=include_evidence)
    except BusinessException as e:
        # EC 文件缺失等属于“暂时无法判定/不触发”的情况：返回 reached=False，并保留错误信息便于上层重试/告警
        return {
            "reached": False,
            "level": None,
            "message": str(e),
        }


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="海河应急响应判定外部接口（实况/预报）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_obs = sub.add_parser("observation", help="实况口径判定")
    p_obs.add_argument("--times", required=True, help="实况时次，例如 20250723080000")
    p_obs.add_argument("--basin-codes", default=None)
    p_obs.add_argument("--scope", default="haihe", choices=["haihe", "nine_zone"])
    p_obs.add_argument("--allowed-station-levels", default="11,12,13,16")
    p_obs.add_argument("--neighbor-km", type=float, default=50.0)
    p_obs.add_argument("--sustain-hourly-threshold-mm", type=float, default=0.1)
    p_obs.add_argument("--include-evidence", action="store_true")

    p_fc = sub.add_parser("forecast", help="预报口径判定")
    p_fc.add_argument("--start-time", required=True, help="起报时次，例如 2026032502 或 2025-06-30 02:00:00")
    p_fc.add_argument("--basin-codes", default=None)
    p_fc.add_argument("--scope", default="haihe", choices=["haihe", "nine_zone"])
    p_fc.add_argument("--ec-output-path", default=DEFAULT_EC_OUTPUT_PATH)
    p_fc.add_argument("--allowed-station-levels", default="11,12,13,16")
    p_fc.add_argument("--sample-method", default="nearest", choices=["nearest", "bilinear"])
    p_fc.add_argument("--sustain-threshold-6h-mm", type=float, default=0.1)
    p_fc.add_argument("--typhoon-landing-impact", action="store_true")
    p_fc.add_argument("--typhoon-impact-increasing", action="store_true")
    p_fc.add_argument("--include-evidence", action="store_true")

    args = parser.parse_args()

    if args.cmd == "observation":
        res = query_haihe_emergency_observation(
            times=args.times,
            basin_codes=args.basin_codes,
            scope=args.scope,
            allowed_station_levels=args.allowed_station_levels,
            neighbor_km=args.neighbor_km,
            sustain_hourly_threshold_mm=args.sustain_hourly_threshold_mm,
            include_evidence=args.include_evidence,
        )
    else:
        res = query_haihe_emergency_forecast(
            start_time=args.start_time,
            basin_codes=args.basin_codes,
            scope=args.scope,
            ec_output_path=args.ec_output_path,
            allowed_station_levels=args.allowed_station_levels,
            sample_method=args.sample_method,
            sustain_threshold_6h_mm=args.sustain_threshold_6h_mm,
            typhoon_landing_impact=args.typhoon_landing_impact,
            typhoon_impact_increasing=args.typhoon_impact_increasing,
            include_evidence=args.include_evidence,
        )

    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()

