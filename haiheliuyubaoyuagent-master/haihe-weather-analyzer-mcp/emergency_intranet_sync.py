from __future__ import annotations

import json
import os
import configparser
import time
from datetime import datetime
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from emergency_scenario_client import merge_scenario_query_params


DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "emergency_cache")


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def fetch_json_from_intranet(url: str, timeout_sec: int = 10) -> Dict[str, Any]:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout_sec) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def save_event_cache(event_id: str, payload: Dict[str, Any], cache_dir: str = DEFAULT_CACHE_DIR) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{event_id}.json")
    payload_to_save = {
        "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event_id": event_id,
        "payload": payload,
    }
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload_to_save, f, ensure_ascii=False, indent=2)
    return path


def load_event_cache(event_id: str, cache_dir: str = DEFAULT_CACHE_DIR) -> Optional[Dict[str, Any]]:
    path = os.path.join(cache_dir, f"{event_id}.json")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_and_cache_event(event_id: str, intranet_url: str, cache_dir: str = DEFAULT_CACHE_DIR) -> Dict[str, Any]:
    data = fetch_json_from_intranet(intranet_url)
    cache_path = save_event_cache(event_id, data, cache_dir=cache_dir)
    return {
        "event_id": event_id,
        "cache_path": cache_path,
        "data": data,
    }


def get_event_with_cache(
    event_id: str,
    intranet_url: str,
    *,
    prefer_cache: bool = True,
    cache_dir: str = DEFAULT_CACHE_DIR,
) -> Dict[str, Any]:
    if prefer_cache:
        cached = load_event_cache(event_id, cache_dir=cache_dir)
        if cached is not None:
            return {
                "source": "local_cache",
                "event_id": event_id,
                "data": cached.get("payload"),
                "saved_at": cached.get("saved_at"),
            }

    fetched = fetch_and_cache_event(event_id, intranet_url, cache_dir=cache_dir)
    return {
        "source": "intranet",
        "event_id": event_id,
        "data": fetched["data"],
        "cache_path": fetched["cache_path"],
    }


def build_url_from_template(
    url_template: str,
    *,
    event_id: str = "",
    times: str = "",
) -> str:
    """
    支持占位写法：
    - {event_id} / ${event_id}
    - {times} / ${times}
    """
    return (
        str(url_template)
        .replace("{event_id}", event_id)
        .replace("${event_id}", event_id)
        .replace("{times}", times)
        .replace("${times}", times)
    )


def _parse_times_compact(times: str) -> datetime:
    t = str(times).strip()
    if len(t) == 10 and t.isdigit():
        return datetime.strptime(t, "%Y%m%d%H")
    if len(t) == 14 and t.isdigit():
        return datetime.strptime(t, "%Y%m%d%H%M%S")
    raise ValueError("times 仅支持 10 位(YYYYMMDDHH) 或 14 位(YYYYMMDDHHMMSS)")


def _format_times_compact(dt: datetime, digits: int) -> str:
    if digits == 10:
        return dt.strftime("%Y%m%d%H")
    return dt.strftime("%Y%m%d%H%M%S")


def build_times_range(start_times: str, end_times: str, step_hours: int = 1) -> List[str]:
    if step_hours <= 0:
        raise ValueError("step_hours 必须大于 0")
    start_txt = str(start_times).strip()
    end_txt = str(end_times).strip()
    start_digits = len(start_txt)
    end_digits = len(end_txt)
    if start_digits not in (10, 14) or end_digits not in (10, 14):
        raise ValueError("start-times / end-times 必须是 10 位或 14 位")

    out_digits = max(start_digits, end_digits)
    start_dt = _parse_times_compact(start_txt)
    end_dt = _parse_times_compact(end_txt)
    if end_dt < start_dt:
        raise ValueError("end_times 不能早于 start_times")

    current = start_dt
    out: List[str] = []
    while current <= end_dt:
        out.append(_format_times_compact(current, out_digits))
        current += timedelta(hours=step_hours)
    return out


def _fetch_json_by_route(base_url: str, route: str, params: Dict[str, Any], timeout_sec: int) -> Dict[str, Any]:
    merged = merge_scenario_query_params(params)
    query = urlencode({k: v for k, v in merged.items() if v is not None})
    url = f"{base_url.rstrip('/')}{route}?{query}"
    return fetch_json_from_intranet(url, timeout_sec=timeout_sec)


def _fetch_json_by_route_with_retry(
    base_url: str,
    route: str,
    params: Dict[str, Any],
    timeout_sec: int,
    *,
    max_attempts: int = 3,
    retry_sleep_sec: float = 1.0,
) -> Dict[str, Any]:
    last_err: Optional[Exception] = None
    attempts = max(1, int(max_attempts))
    for i in range(attempts):
        try:
            return _fetch_json_by_route(base_url, route, params, timeout_sec=timeout_sec)
        except Exception as exc:
            last_err = exc
            text = str(exc).lower()
            is_timeout = ("timed out" in text) or ("timeout" in text)
            if (not is_timeout) or (i >= attempts - 1):
                raise
            time.sleep(max(0.0, float(retry_sleep_sec)))
    if last_err is not None:
        raise last_err
    raise RuntimeError("unknown fetch error")


def _collect_times_from_merged(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    slots = doc.get("slots")
    if not isinstance(slots, list):
        return []
    out: List[str] = []
    seen = set()
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        t = str(slot.get("times") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return sorted(out)


def _filter_times_by_date_list(times_list: List[str], date_list: List[str]) -> List[str]:
    """
    date_list 形态：['2024-07-24', '2025-08-26'] 或 ['20240724', '20250826']
    匹配 times 的前 8 位 YYYYMMDD。
    """
    normalized_days = set()
    for d in date_list:
        txt = str(d).strip()
        if not txt:
            continue
        day = txt.replace("-", "").replace("/", "")
        if len(day) == 8 and day.isdigit():
            normalized_days.add(day)
    if not normalized_days:
        return times_list
    return [t for t in times_list if str(t).strip()[:8] in normalized_days]


def _filter_times_by_explicit_list(times_list: List[str], explicit: List[str]) -> List[str]:
    wanted = {str(x).strip() for x in explicit if str(x).strip()}
    if not wanted:
        return times_list
    return [t for t in times_list if str(t).strip() in wanted]


def _load_all_river_names_from_db(config_path: str) -> List[str]:
    try:
        import psycopg2
    except Exception as exc:
        raise ValueError(f"未安装 psycopg2，无法自动读取全部河流: {exc}") from exc
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    if not cp.has_section("postgres"):
        raise ValueError(f"配置文件缺少 [postgres]: {config_path}")
    pg = cp["postgres"]
    schema = pg.get("schema", "public").strip() or "public"
    river_table = pg.get("river_table", "haihe_river_directed_simple_v2").strip() or "haihe_river_directed_simple_v2"
    sql = f"""
        SELECT DISTINCT COALESCE(NULLIF(TRIM(river_name), ''), NULLIF(TRIM(src_name), '')) AS rn
        FROM {schema}.{river_table}
        WHERE COALESCE(NULLIF(TRIM(river_name), ''), NULLIF(TRIM(src_name), '')) IS NOT NULL
        ORDER BY rn
    """
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
                cur.execute(sql)
                rows = cur.fetchall() or []
        return [str(r[0]).strip() for r in rows if r and r[0] is not None and str(r[0]).strip()]
    finally:
        conn.close()


def _parse_scene_list(scene_csv: str) -> List[str]:
    raw = [x.strip() for x in str(scene_csv or "").split(",") if x.strip()]
    if not raw:
        return ["monitor", "emergency_admin_regions", "emergency_partitions", "emergency_rivers"]
    allowed = {"monitor", "emergency_regions", "emergency_admin_regions", "emergency_partitions", "emergency_rivers"}
    bad = [x for x in raw if x not in allowed]
    if bad:
        raise ValueError(
            f"--scenes 包含不支持的场景: {bad}；可选值: "
            "monitor,emergency_regions,emergency_admin_regions,emergency_partitions,emergency_rivers"
        )
    # 保持去重后顺序
    out: List[str] = []
    seen = set()
    for s in raw:
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="内网应急数据拉取并落盘工具")
    parser.add_argument(
        "--export-scenario-geojson",
        action="store_true",
        help="从 merged 文件二次导出带坐标场景包（调用 /scenario/* 接口）",
    )
    parser.add_argument("--merged-json", default="", help="merged_6h_all_ranges.json 路径（导出模式必填）")
    parser.add_argument("--output-file", default="", help="导出模式输出文件路径")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="导出模式服务地址")
    parser.add_argument("--river-name", default="", help="导出模式应急河流主河名；不传时自动读取全部河流")
    parser.add_argument("--config-path", default=os.path.join(os.path.dirname(__file__), "config.ini"), help="用于读取全部河流的配置文件路径")
    parser.add_argument("--max-rivers", type=int, default=0, help="导出模式河流数量上限，0 表示不限制")
    parser.add_argument(
        "--scenes",
        default="monitor,emergency_admin_regions,emergency_partitions,emergency_rivers",
        help=(
            "导出模式场景列表，逗号分隔："
            "monitor,emergency_admin_regions,emergency_partitions,emergency_rivers"
            "（兼容旧值 emergency_regions）"
        ),
    )
    parser.add_argument(
        "--monitor-source",
        default="merged",
        help="monitor 场景数据源：merged 或 live（live 走实时接口，字段更全）",
    )
    parser.add_argument("--monitor-limit", type=int, default=2000, help="monitor=live 时站点条数上限")
    parser.add_argument("--monitor-basin-codes", default="", help="monitor=live 时可选 basin_codes")
    parser.add_argument("--monitor-station-levels", default="11,12,13,16", help="monitor=live 时站点级别过滤")
    parser.add_argument(
        "--split-output-dir",
        default="",
        help="导出模式按场景拆分输出目录；传入后不再写单一 output-file",
    )
    parser.add_argument(
        "--times-list",
        default="",
        help="导出模式只提取指定时次，逗号分隔，如 20240724000000,20250826000000",
    )
    parser.add_argument(
        "--date-list",
        default="",
        help="导出模式只提取指定日期（当天所有时次），逗号分隔，如 2024-07-24,2025-08-26,2023-07-29",
    )
    parser.add_argument("--event-id", default="", help="应急事件ID，例如 EVT-20260424-001（与 --times 二选一）")
    parser.add_argument("--times", default="", help="事件发生时间，例如 20260424080000（与 --event-id 二选一）")
    parser.add_argument("--start-times", default="", help="批量开始时间，例如 2024072200 或 20240722000000")
    parser.add_argument("--end-times", default="", help="批量结束时间，例如 2024072600 或 20240726000000")
    parser.add_argument("--step-hours", type=int, default=1, help="批量时间步长（小时），默认 1")
    parser.add_argument(
        "--url-template",
        default="",
        help="内网URL模板，支持 {event_id}/{times} 或 ${event_id}/${times} 占位",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_CACHE_DIR,
        help="落盘目录（建议填内网服务器目录，如 /data/emergency_cache）",
    )
    parser.add_argument("--timeout-sec", type=int, default=10, help="HTTP 超时秒数，默认 10")
    parser.add_argument(
        "--map-render",
        default="",
        help="仅导出模式：/scenario/* 的 map_render；空则读 SCENARIO_MAP_RENDER（未设时默认 wms_sql）。full/geojson/off 表示整包 GeoJSON。",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="强制刷新：忽略本地已有缓存，重新从内网拉取",
    )
    args = parser.parse_args()

    if args.export_scenario_geojson:
        cli_mr = str(getattr(args, "map_render", "") or "").strip()
        if cli_mr:
            if cli_mr.lower() in {"full", "geojson", "off", "none", "0"}:
                os.environ["SCENARIO_MAP_RENDER"] = ""
            else:
                os.environ["SCENARIO_MAP_RENDER"] = cli_mr
        merged_json = str(args.merged_json).strip()
        output_file = str(args.output_file).strip()
        if not merged_json or not os.path.isfile(merged_json):
            raise ValueError("导出模式需要有效 --merged-json 文件路径")
        split_output_dir = str(args.split_output_dir).strip()
        if not output_file and not split_output_dir:
            raise ValueError("导出模式需要 --output-file")
        scenes = _parse_scene_list(args.scenes)
        monitor_source = str(args.monitor_source).strip().lower() or "merged"
        if monitor_source not in {"merged", "live"}:
            raise ValueError("--monitor-source 仅支持 merged 或 live")
        times_list = _collect_times_from_merged(merged_json)
        explicit_times = [x.strip() for x in str(args.times_list).split(",") if x.strip()]
        date_list = [x.strip() for x in str(args.date_list).split(",") if x.strip()]
        if explicit_times:
            times_list = _filter_times_by_explicit_list(times_list, explicit_times)
        if date_list:
            times_list = _filter_times_by_date_list(times_list, date_list)
        if not times_list:
            raise ValueError("筛选后未找到任何可导出时次，请检查 --times-list/--date-list")
        river_names: List[str] = []
        if "emergency_rivers" in scenes:
            river_name = str(args.river_name).strip()
            if river_name:
                river_names = [river_name]
            else:
                river_names = _load_all_river_names_from_db(str(args.config_path))
                max_rivers = int(args.max_rivers or 0)
                if max_rivers > 0:
                    river_names = river_names[:max_rivers]
            if not river_names:
                raise ValueError("导出模式未解析到任何河流名")
        slots: List[Dict[str, Any]] = []
        ok_count = 0
        fail_count = 0
        for t in times_list:
            one: Dict[str, Any] = {"times": t, "ok": True}
            common_local = {
                "times": t,
                "force_local": "true",
                "local_json_path": merged_json,
            }
            common_live = {
                "times": t,
                "limit": max(1, int(args.monitor_limit)),
                "allowed_station_levels": str(args.monitor_station_levels or "11,12,13,16"),
            }
            basin_codes = str(args.monitor_basin_codes or "").strip()
            if basin_codes:
                common_live["basin_codes"] = basin_codes
            else:
                common_live["scope"] = "haihe"
            try:
                if "monitor" in scenes:
                    monitor_params = common_live if monitor_source == "live" else common_local
                    one["monitor_stations"] = _fetch_json_by_route_with_retry(
                        args.base_url, "/scenario/monitor/stations", monitor_params, timeout_sec=max(1, int(args.timeout_sec))
                    )
                if "emergency_regions" in scenes:
                    one["emergency_regions"] = _fetch_json_by_route_with_retry(
                        args.base_url, "/scenario/emergency/regions", common_local, timeout_sec=max(1, int(args.timeout_sec))
                    )
                if "emergency_admin_regions" in scenes:
                    one["emergency_admin_regions"] = _fetch_json_by_route_with_retry(
                        args.base_url, "/scenario/emergency/admin_regions", common_local, timeout_sec=max(1, int(args.timeout_sec))
                    )
                if "emergency_partitions" in scenes:
                    one["emergency_partitions"] = _fetch_json_by_route_with_retry(
                        args.base_url, "/scenario/emergency/partitions", common_local, timeout_sec=max(1, int(args.timeout_sec))
                    )
                if "emergency_rivers" in scenes:
                    river_items: List[Dict[str, Any]] = []
                    river_ok = 0
                    river_fail = 0
                    for rn in river_names:
                        try:
                            payload = _fetch_json_by_route_with_retry(
                                args.base_url,
                                "/scenario/emergency/rivers",
                                {**common_local, "river_name": rn},
                                timeout_sec=max(1, int(args.timeout_sec)),
                            )
                            river_items.append({"river_name": rn, "ok": True, "payload": payload})
                            river_ok += 1
                        except Exception as rex:
                            river_items.append({"river_name": rn, "ok": False, "error": str(rex)})
                            river_fail += 1
                    one["emergency_rivers"] = {
                        "count_total": len(river_names),
                        "count_ok": river_ok,
                        "count_fail": river_fail,
                        "items": river_items,
                    }
                ok_count += 1
            except Exception as e:
                fail_count += 1
                one["ok"] = False
                one["error"] = str(e)
            slots.append(one)
        out = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "meta": {
                "base_url": args.base_url,
                "source_merged_json": merged_json,
                "scenes": scenes,
                "monitor_source": monitor_source,
                "river_count": len(river_names),
                "count_total": len(times_list),
                "count_ok": ok_count,
                "count_fail": fail_count,
            },
            "slots": slots,
        }
        split_files: Dict[str, str] = {}
        if split_output_dir:
            os.makedirs(split_output_dir, exist_ok=True)
            scene_key_map = {
                "monitor": "monitor_stations",
                "emergency_regions": "emergency_regions",
                "emergency_admin_regions": "emergency_admin_regions",
                "emergency_partitions": "emergency_partitions",
                "emergency_rivers": "emergency_rivers",
            }
            for scene in scenes:
                key = scene_key_map[scene]
                scene_slots: List[Dict[str, Any]] = []
                for s in slots:
                    scene_slots.append(
                        {
                            "times": s.get("times"),
                            "ok": s.get("ok"),
                            key: s.get(key),
                            "error": s.get("error"),
                        }
                    )
                scene_doc = {
                    "generated_at": out["generated_at"],
                    "meta": {**out["meta"], "scene": scene},
                    "slots": scene_slots,
                }
                scene_file = os.path.join(split_output_dir, f"{scene}.json")
                with open(scene_file, "w", encoding="utf-8") as f:
                    json.dump(scene_doc, f, ensure_ascii=False, indent=2)
                split_files[scene] = scene_file
        else:
            _ensure_parent(output_file)
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
        print(
            json.dumps(
                {
                    "ok": True,
                    "output_file": output_file if not split_output_dir else None,
                    "split_output_dir": split_output_dir or None,
                    "split_files": split_files or None,
                    "count_total": len(times_list),
                    "count_ok": ok_count,
                    "count_fail": fail_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not str(args.url_template).strip():
        raise ValueError("非导出模式必须传 --url-template")

    event_id = str(args.event_id).strip()
    times = str(args.times).strip()
    start_times = str(args.start_times).strip()
    end_times = str(args.end_times).strip()
    is_batch = bool(start_times or end_times)

    if is_batch:
        if event_id:
            raise ValueError("批量模式不支持 event_id，请使用 start-times/end-times")
        if not start_times or not end_times:
            raise ValueError("批量模式需要同时提供 start-times 与 end-times")
        times_list = build_times_range(start_times, end_times, step_hours=int(args.step_hours))
        items: List[Dict[str, Any]] = []
        ok_count = 0
        fail_count = 0
        for t in times_list:
            cache_key = t
            url = build_url_from_template(args.url_template, event_id="", times=t)
            try:
                if args.refresh:
                    data = fetch_json_from_intranet(url, timeout_sec=max(1, int(args.timeout_sec)))
                    cache_path = save_event_cache(cache_key, data, cache_dir=args.output_dir)
                    one = {
                        "ok": True,
                        "source": "intranet",
                        "times": t,
                        "url": url,
                        "cache_path": cache_path,
                    }
                else:
                    res = get_event_with_cache(
                        event_id=cache_key,
                        intranet_url=url,
                        prefer_cache=True,
                        cache_dir=args.output_dir,
                    )
                    one = {
                        "ok": True,
                        "source": res.get("source"),
                        "times": t,
                        "url": url,
                        "cache_path": res.get("cache_path"),
                    }
                ok_count += 1
            except Exception as e:
                fail_count += 1
                one = {
                    "ok": False,
                    "times": t,
                    "url": url,
                    "error": str(e),
                }
            items.append(one)
        result = {
            "mode": "batch_by_times",
            "count_total": len(times_list),
            "count_ok": ok_count,
            "count_fail": fail_count,
            "items": items,
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if (not event_id) and (not times):
        raise ValueError("event_id/times/start-times,end-times 至少传一种模式")
    cache_key = event_id or times
    url = build_url_from_template(args.url_template, event_id=event_id, times=times)
    if args.refresh:
        data = fetch_json_from_intranet(url, timeout_sec=max(1, int(args.timeout_sec)))
        cache_path = save_event_cache(cache_key, data, cache_dir=args.output_dir)
        result = {
            "source": "intranet",
            "cache_key": cache_key,
            "event_id": event_id or None,
            "times": times or None,
            "url": url,
            "cache_path": cache_path,
        }
    else:
        result = get_event_with_cache(
            event_id=cache_key,
            intranet_url=url,
            prefer_cache=True,
            cache_dir=args.output_dir,
        )
        result["cache_key"] = cache_key
        result["event_id"] = event_id or None
        result["times"] = times or None
        result["url"] = url
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()

