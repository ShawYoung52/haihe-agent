"""
NOTE: This MusicClient implementation is NOT used by the main MCP server.
The canonical implementation is in haihe_mcp_tools.py (imported by tools.py).
This file is kept for standalone CLI/scripts only; do not modify independently.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode

import requests


DEFAULT_OBS_ELEMENTS = (
    "Station_levl,Lat,Lon,Alti,Admin_Code_CHN,V_ACODE_4SEARCH,Town_code,"
    "City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Year,Mon,Day,Hour,"
    "PRE_1h,PRE_3h,PRE_6h,PRE_12h,PRE_24h,PRE,Station_Id_C"
)

# 可直接写死在代码里，服务器测试更方便；环境变量仍然可以覆盖这些默认值。
BUILTIN_MUSIC_CONFIG = {
    "service_ip": "10.226.90.120",
    "service_node_id": "NMIC_MUSIC_CMADAAS",
    "user_id": "BETJ_QXT_LYGXPT",
    "password": "Qxtly@2022ww",
    "timeout": 120,
}

# 这里给的是常见业务口径，正式上线前请按你们的正式规范核对。
DEFAULT_THRESHOLDS_MM = {
    # 暴雨：50.0～99.9毫米（此处用下限 50.0 作为触发阈值）
    "rainstorm_12h": 50.0,          # 12h 暴雨
    "rainstorm_24h": 50.0,          # 24h 暴雨
    "severe_rainstorm_24h": 100.0,  # 24h 大暴雨
    "extraordinary_24h": 250.0,     # 24h 特大暴雨
}


@dataclass
class MusicConfig:
    service_ip: str = os.getenv("MUSIC_SERVICE_IP", BUILTIN_MUSIC_CONFIG["service_ip"])
    service_node_id: str = os.getenv("MUSIC_SERVICE_NODE_ID", BUILTIN_MUSIC_CONFIG["service_node_id"])
    user_id: str = os.getenv("MUSIC_USER_ID", BUILTIN_MUSIC_CONFIG["user_id"])
    password: str = os.getenv("MUSIC_PASSWORD", BUILTIN_MUSIC_CONFIG["password"])
    timeout: int = int(os.getenv("MUSIC_TIMEOUT", str(BUILTIN_MUSIC_CONFIG["timeout"])))

    @property
    def base_url(self) -> str:
        return f"http://{self.service_ip}/music-ws/api"


class MusicApiError(Exception):
    pass


class MusicClient:
    def __init__(self, config: MusicConfig):
        if not config.user_id or not config.password:
            raise ValueError("请先在代码里的 BUILTIN_MUSIC_CONFIG 或环境变量中填写 MUSIC_USER_ID 和 MUSIC_PASSWORD")
        self.config = config
        self.session = requests.Session()

    @staticmethod
    def _build_sign(sign_params: Dict[str, str]) -> str:
        items = {k: str(v) for k, v in sign_params.items() if v is not None and v != ""}
        content = "&".join(f"{k}={items[k]}" for k in sorted(items.keys()))
        return hashlib.md5(content.encode("utf-8")).hexdigest().upper()

    def _request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = {k: str(v) for k, v in params.items() if v is not None and v != ""}
        query["timestamp"] = str(int(time.time() * 1000))
        query["nonce"] = str(uuid.uuid4())

        sign_params = dict(query)
        sign_params["pwd"] = self.config.password
        query["sign"] = self._build_sign(sign_params)

        # 保留 MUSIC 原接口常见符号，不做过度编码。
        url = f"{self.config.base_url}?{urlencode(query, safe=':,[]()') }"

        connect_timeout = float(os.getenv("MUSIC_CONNECT_TIMEOUT", "5"))
        read_timeout = float(os.getenv("MUSIC_READ_TIMEOUT", str(self.config.timeout)))
        timeout = (connect_timeout, read_timeout)

        max_retries = int(os.getenv("MUSIC_MAX_RETRIES", "2"))
        base_backoff = float(os.getenv("MUSIC_RETRY_BACKOFF_SEC", "1.0"))

        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                resp = self.session.get(url, timeout=timeout)
                resp.raise_for_status()
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_exc = exc
                if attempt >= max_retries:
                    raise
                time.sleep(base_backoff * (2 ** attempt) + random.uniform(0.0, 0.3))
        else:
            if last_exc:
                raise last_exc

        try:
            payload = resp.json()
        except Exception as exc:
            raise MusicApiError(f"接口返回不是 JSON: {resp.text[:500]}") from exc

        if isinstance(payload, dict):
            # 常见错误字段兜底
            for key in ("error", "errMsg", "message", "msg"):
                if key in payload and payload.get(key) and str(payload.get(key)).lower() not in {"ok", "success"}:
                    # 不直接抛错，先看是否也有 DS
                    if "DS" not in payload:
                        raise MusicApiError(str(payload.get(key)))
            return payload

        raise MusicApiError(f"未知返回结构: {type(payload)}")

    def call_api(self, interface_id: str, **kwargs: Any) -> List[Dict[str, Any]]:
        params = {
            "serviceNodeId": self.config.service_node_id,
            "userId": self.config.user_id,
            "dataFormat": "json",
            "interfaceId": interface_id,
            **kwargs,
        }
        payload = self._request(params)
        ds = payload.get("DS")
        if ds is None:
            raise MusicApiError(f"返回中没有 DS 字段: {json.dumps(payload, ensure_ascii=False)[:500]}")
        if isinstance(ds, list):
            return ds
        raise MusicApiError(f"DS 不是列表结构: {type(ds)}")

    # 1) 按时间、流域查询地面数据要素
    def get_surf_ele_in_basin_by_time(
        self,
        basin_codes: str,
        times: str,
        elements: str = DEFAULT_OBS_ELEMENTS,
        data_code: str = "SURF_CHN_MUL_HOR",
        ele_value_ranges: Optional[str] = None,
        order_by: Optional[str] = None,
        limit_cnt: Optional[int] = None,
        data_province_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.call_api(
            "getSurfEleInBasinByTime",
            dataCode=data_code,
            elements=elements,
            times=times,
            basinCodes=basin_codes,
            eleValueRanges=ele_value_ranges,
            orderBy=order_by,
            limitCnt=limit_cnt,
            dataProvinceId=data_province_id,
        )

    # 2) 按时间段、流域统计逐小时降水量
    def stat_surf_pre_in_basin(
        self,
        basin_codes: str,
        time_range: str,
        elements: str,
        stat_eles: str,
        data_code: str = "SURF_CHN_MUL_HOR",
        stat_ele_value_ranges: Optional[str] = None,
        order_by: Optional[str] = None,
        limit_cnt: Optional[int] = None,
        sta_levels: Optional[str] = None,
        data_province_id: Optional[str] = None,
        ele_value_ranges: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.call_api(
            "statSurfPreInBasin",
            dataCode=data_code,
            elements=elements,
            statEles=stat_eles,
            timeRange=time_range,
            basinCodes=basin_codes,
            statEleValueRanges=stat_ele_value_ranges,
            orderBy=order_by,
            limitCnt=limit_cnt,
            staLevels=sta_levels,
            dataProvinceId=data_province_id,
            eleValueRanges=ele_value_ranges,
        )


def safe_float(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "None"):
        return default
    try:
        return float(value)
    except Exception:
        return default


def normalize_station_level(level: Any) -> str:
    if level is None:
        return ""
    return str(level).strip()


def station_id_of(record: Dict[str, Any]) -> str:
    return str(record.get("Station_Id_C") or record.get("Station_Id_d") or record.get("Station_Id") or "").strip()


def filter_records_by_station_levels(
    records: Sequence[Dict[str, Any]],
    allowed_levels: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    if not allowed_levels:
        return [r for r in records if station_id_of(r)]
    allowed = {str(x).strip() for x in allowed_levels if str(x).strip()}
    return [
        r for r in records
        if station_id_of(r) and normalize_station_level(r.get("Station_levl")) in allowed
    ]


def deduplicate_latest_records(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一站同一时刻可能重复，保留最后一条。"""
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in records:
        sid = station_id_of(r)
        if not sid:
            continue
        dt = f"{r.get('Year','')}{r.get('Mon','')}{r.get('Day','')}{r.get('Hour','')}"
        result[(sid, dt)] = dict(r)
    return list(result.values())


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def find_adjacent_qualified_station_ids(
    records: Sequence[Dict[str, Any]],
    qualified_ids: Set[str],
    neighbor_km: float = 50.0,
) -> Set[str]:
    qualified_records = [r for r in records if station_id_of(r) in qualified_ids]
    adjacent_ids: Set[str] = set()
    for i in range(len(qualified_records)):
        r1 = qualified_records[i]
        sid1 = station_id_of(r1)
        lat1 = safe_float(r1.get("Lat"))
        lon1 = safe_float(r1.get("Lon"))
        if not sid1 or lat1 == 0.0 or lon1 == 0.0:
            continue
        for j in range(i + 1, len(qualified_records)):
            r2 = qualified_records[j]
            sid2 = station_id_of(r2)
            lat2 = safe_float(r2.get("Lat"))
            lon2 = safe_float(r2.get("Lon"))
            if not sid2 or lat2 == 0.0 or lon2 == 0.0:
                continue
            if haversine_km(lat1, lon1, lat2, lon2) <= neighbor_km:
                adjacent_ids.add(sid1)
                adjacent_ids.add(sid2)
    return adjacent_ids


def _pick_value_field(window_hours: int) -> str:
    if window_hours == 12:
        return "PRE_12h"
    if window_hours == 24:
        return "PRE_24h"
    raise ValueError("目前只支持 12 或 24 小时判定")


def evaluate_observation_response(
    records: Sequence[Dict[str, Any]],
    thresholds_mm: Optional[Dict[str, float]] = None,
    neighbor_km: float = 50.0,
    sustain_hourly_threshold_mm: float = 0.1,
    allowed_station_levels: Optional[Iterable[str]] = ("011", "012", "013"),
) -> Dict[str, Any]:
    """
    说明：
    1. 这里基于你新提供的 getSurfEleInBasinByTime 站点要素结果做“实况”判定。
    2. “强降水持续”先用一个可调演示口径：达标相邻站中，至少有 1 个站 PRE_1h >= sustain_hourly_threshold_mm。
       你们正式上线时，建议把这个规则改成业务方确认的持续性口径。
    """
    thresholds = dict(DEFAULT_THRESHOLDS_MM)
    if thresholds_mm:
        thresholds.update(thresholds_mm)

    records = filter_records_by_station_levels(records, allowed_station_levels)
    records = deduplicate_latest_records(records)

    total_station_ids = {station_id_of(r) for r in records if station_id_of(r)}
    total_count = len(total_station_ids)
    if total_count == 0:
        return {
            "triggered": False,
            "level": None,
            "message": "没有可用于判定的站点数据，请检查 basinCodes / times / staLevels / elements 是否正确。",
            "evidence": {},
        }

    def judge(window_hours: int, threshold_mm: float, ratio_threshold: float, level: str, rain_label: str) -> Optional[Dict[str, Any]]:
        value_field = _pick_value_field(window_hours)
        qualified_ids = {
            station_id_of(r)
            for r in records
            if station_id_of(r) and safe_float(r.get(value_field)) >= threshold_mm
        }
        # 按你的要求：不再考虑“相邻国家气象观测站”条件。
        # 直接将“相邻站集合”视为“满足降水阈值的站集合”。
        adjacent_ids = qualified_ids
        ratio = len(adjacent_ids) / total_count if total_count else 0.0

        sustained = any(
            station_id_of(r) in adjacent_ids and safe_float(r.get("PRE_1h")) >= sustain_hourly_threshold_mm
            for r in records
        )

        if ratio >= ratio_threshold and sustained:
            top_stations = []
            for r in records:
                sid = station_id_of(r)
                if sid in adjacent_ids:
                    top_stations.append(
                        {
                            "Station_Id_C": sid,
                            "Station_Name": r.get("Station_Name"),
                            "Province": r.get("Province"),
                            "City": r.get("City"),
                            "Cnty": r.get("Cnty"),
                            "Lat": r.get("Lat"),
                            "Lon": r.get("Lon"),
                            value_field: r.get(value_field),
                            "PRE_1h": r.get("PRE_1h"),
                        }
                    )
            top_stations.sort(key=lambda x: safe_float(x.get(value_field)), reverse=True)
            return {
                "triggered": True,
                "level": level,
                "message": f"满足{level}级应急响应条件（实况口径）",
                "evidence": {
                    "window_hours": window_hours,
                    "rain_label": rain_label,
                    "threshold_mm": threshold_mm,
                    "neighbor_km": neighbor_km,
                    "sustain_hourly_threshold_mm": sustain_hourly_threshold_mm,
                    "qualified_adjacent_station_count": len(adjacent_ids),
                    "total_station_count": total_count,
                    "ratio": round(ratio, 4),
                    "sustained": sustained,
                    "top_stations": top_stations[:20],
                },
            }
        return None

    # 按最高级别优先判定
    checks = [
        judge(24, thresholds["extraordinary_24h"], 0.15, "I", "特大暴雨"),
        judge(24, thresholds["severe_rainstorm_24h"], 0.15, "II", "大暴雨"),
        judge(12, thresholds["rainstorm_12h"], 0.20, "III", "暴雨"),
        judge(24, thresholds["rainstorm_24h"], 0.20, "IV", "暴雨"),
    ]
    for result in checks:
        if result:
            return result

    return {
        "triggered": False,
        "level": None,
        "message": "当前未满足 I/II/III/IV 级应急响应条件（仅基于本次实况站点数据判定）。",
        "evidence": {
            "total_station_count": total_count,
            "neighbor_km": neighbor_km,
            "sustain_hourly_threshold_mm": sustain_hourly_threshold_mm,
        },
    }


def json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="海河流域气象接口测试与实况应急响应判定")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_pull = sub.add_parser("pull", help="测试 getSurfEleInBasinByTime")
    p_pull.add_argument("--basin-codes", required=True, help="流域编码，多个逗号分隔")
    p_pull.add_argument("--times", required=True, help="时次，多个逗号分隔，例如 20250723080000")
    p_pull.add_argument("--elements", default=DEFAULT_OBS_ELEMENTS)
    p_pull.add_argument("--order-by", default=None)
    p_pull.add_argument("--limit-cnt", type=int, default=None)
    p_pull.add_argument("--data-province-id", default=None)
    p_pull.add_argument("--ele-value-ranges", default=None)
    p_pull.add_argument("--save", default=None, help="保存原始结果到 JSON 文件")

    p_stat = sub.add_parser("stat", help="测试 statSurfPreInBasin")
    p_stat.add_argument("--basin-codes", required=True)
    p_stat.add_argument("--time-range", required=True, help="如 [20250723000000,20250724000000]")
    p_stat.add_argument("--elements", required=True, help="分组字段，例如 Station_Id_C,Station_Name")
    p_stat.add_argument("--stat-eles", required=True, help="统计字段，例如 SUM_PRE_1h,MAX_PRE_1h,COUNT_PRE_1h")
    p_stat.add_argument("--stat-ele-value-ranges", default=None)
    p_stat.add_argument("--ele-value-ranges", default=None)
    p_stat.add_argument("--order-by", default=None)
    p_stat.add_argument("--limit-cnt", type=int, default=None)
    p_stat.add_argument("--sta-levels", default=None, help="如 011,012,013")
    p_stat.add_argument("--data-province-id", default=None)
    p_stat.add_argument("--save", default=None)

    p_judge = sub.add_parser("judge", help="直接拉取站点实况并做 I/II/III/IV 判定")
    p_judge.add_argument("--basin-codes", required=True)
    p_judge.add_argument("--times", required=True)
    p_judge.add_argument("--neighbor-km", type=float, default=50.0)
    p_judge.add_argument("--sustain-hourly-threshold-mm", type=float, default=0.1)
    p_judge.add_argument("--allowed-station-levels", default="011,012,013")
    p_judge.add_argument("--rainstorm-12h", type=float, default=DEFAULT_THRESHOLDS_MM["rainstorm_12h"])
    p_judge.add_argument("--rainstorm-24h", type=float, default=DEFAULT_THRESHOLDS_MM["rainstorm_24h"])
    p_judge.add_argument("--severe-rainstorm-24h", type=float, default=DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"])
    p_judge.add_argument("--extraordinary-24h", type=float, default=DEFAULT_THRESHOLDS_MM["extraordinary_24h"])
    p_judge.add_argument("--save-records", default=None)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    client = MusicClient(MusicConfig())

    if args.cmd == "pull":
        data = client.get_surf_ele_in_basin_by_time(
            basin_codes=args.basin_codes,
            times=args.times,
            elements=args.elements,
            order_by=args.order_by,
            limit_cnt=args.limit_cnt,
            data_province_id=args.data_province_id,
            ele_value_ranges=args.ele_value_ranges,
        )
        print(json_dump(data))
        if args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        return

    if args.cmd == "stat":
        data = client.stat_surf_pre_in_basin(
            basin_codes=args.basin_codes,
            time_range=args.time_range,
            elements=args.elements,
            stat_eles=args.stat_eles,
            stat_ele_value_ranges=args.stat_ele_value_ranges,
            ele_value_ranges=args.ele_value_ranges,
            order_by=args.order_by,
            limit_cnt=args.limit_cnt,
            sta_levels=args.sta_levels,
            data_province_id=args.data_province_id,
        )
        print(json_dump(data))
        if args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        return

    if args.cmd == "judge":
        records = client.get_surf_ele_in_basin_by_time(
            basin_codes=args.basin_codes,
            times=args.times,
            elements=DEFAULT_OBS_ELEMENTS,
        )
        if args.save_records:
            with open(args.save_records, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)

        result = evaluate_observation_response(
            records=records,
            thresholds_mm={
                "rainstorm_12h": args.rainstorm_12h,
                "rainstorm_24h": args.rainstorm_24h,
                "severe_rainstorm_24h": args.severe_rainstorm_24h,
                "extraordinary_24h": args.extraordinary_24h,
            },
            neighbor_km=args.neighbor_km,
            sustain_hourly_threshold_mm=args.sustain_hourly_threshold_mm,
            allowed_station_levels=[x.strip() for x in args.allowed_station_levels.split(",") if x.strip()],
        )
        print(json_dump(result))
        return


if __name__ == "__main__":
    main()
