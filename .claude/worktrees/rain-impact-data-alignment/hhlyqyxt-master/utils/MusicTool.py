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
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode

import pandas
import requests


DEFAULT_OBS_ELEMENTS = (
    "Station_levl,Lat,Lon,Alti,Admin_Code_CHN,V_ACODE_4SEARCH,Town_code,Station_Id_C,Datetime,"
    "City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Year,Mon,Day,Hour,"
    "PRE_1h,PRE_3h,PRE_6h,PRE_12h,PRE_24h,PRE"
)

# 可直接写死在代码里，服务器测试更方便；环境变量仍然可以覆盖这些默认值。
BUILTIN_MUSIC_CONFIG = {
    "service_ip": "10.226.90.120",
    "service_node_id": "NMIC_MUSIC_CMADAAS",
    "user_id": "BETJ_QXT_LYGXPT",
    "password": "Qxtly@2022ww",
    "timeout": 40000,
}

# 这里给的是常见业务口径，正式上线前请按你们的正式规范核对。
DEFAULT_THRESHOLDS_MM = {
    # 暴雨：50.0～99.9毫米（此处用下限 50.0 作为触发阈值）
    "rainstorm_12h": 50.0,          # 12h 暴雨
    "rainstorm_24h": 50.0,          # 24h 暴雨
    "severe_rainstorm_24h": 100.0,  # 24h 大暴雨
    "extraordinary_24h": 250.0,     # 24h 特大暴雨
}
DEFAULT_EC_OUTPUT_PATH = os.getenv("EC_OUTPUT_PATH", "/home/ev/data/ec/EC_AIFS/output/")
DEFAULT_EC_AIFS_ROOT = os.getenv("EC_AIFS_ROOT", "/home/ev/data/ec/EC_AIFS")
EC_GRIB_MM_MULTIPLIER = float(os.getenv("EC_GRIB_MM_MULTIPLIER", "1"))


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
                print("请求路径:", url)
                # response = self.session.get(url, timeout=timeout)
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

    # 3) 按时间段、流域统计中国地面逐小时降水量
    def stat_surf_pre_in_basin_new(
            self,
            basin_codes: str,
            timeRange: str,
            elements: str = ("Lat,Lon,Station_Id_C,City,Station_Name,Cnty,Province,Town"),
            data_code: str = "SURF_CHN_MUL_HOR",
            ele_value_ranges: Optional[str] = None,
            order_by: Optional[str] = None,
            limit_cnt: Optional[int] = None,
            data_province_id: Optional[str] = None,
            staLevels: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return self.call_api(
            "statSurfPreInBasin",
            # dataCode=data_code,
            elements=elements,
            timeRange=timeRange,
            basinCodes=basin_codes,
            eleValueRanges=ele_value_ranges,
            orderBy=order_by,
            limitCnt=limit_cnt,
            dataProvinceId=data_province_id,
            staLevels=staLevels,
        )

    def get_surf_ele_in_region_by_time(
            self,
            adminCodes: str,
            times: str,
            elements: str = ("Lat,Lon,Station_Id_C,City,Station_Name,Cnty,Province,Town"),
            data_code: str = "SURF_CHN_MUL_HOR",
            ele_value_ranges: Optional[str] = None,
            order_by: Optional[str] = None,
            limit_cnt: Optional[int] = None,
            data_province_id: Optional[str] = None,
            staLevels: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return self.call_api(
            "getSurfEleInRegionByTime",
            dataCode=data_code,
            elements=elements,
            times=times,
            adminCodes=adminCodes,
            eleValueRanges=ele_value_ranges,
            orderBy=order_by,
            limitCnt=limit_cnt,
            dataProvinceId=data_province_id,
            staLevels=staLevels,
        )
    def stat_surf_ele_in_region(self,
            adminCodes: str,
            timeRange: str,
            elements: str = ("Station_Id_C,Lat,Lon,Admin_Code_CHN,Town_code,City,Cnty,Province,Station_Name"),
            data_code: str = "SURF_CHN_MUL_HOR",
            statEles: str = "SUM_PRE_1h",
            ele_value_ranges: Optional[str] = None,
            order_by: Optional[str] = None,
            limit_cnt: Optional[int] = None,
            data_province_id: Optional[str] = None,
            staLevels: Optional[str] = None
        )-> List[Dict[str, Any]]:
        return self.call_api(
            "statSurfEleInRegion",
            dataCode=data_code,
            elements=elements,
            timeRange=timeRange,
            adminCodes=adminCodes,
            eleValueRanges=ele_value_ranges,
            orderBy=order_by,
            limitCnt=limit_cnt,
            dataProvinceId=data_province_id,
            staLevels=staLevels,
            statEles = statEles
        )

    # 3) 按时间段、流域获取中国地面逐分钟降水量
    def get_surf_pre_in_basin_timerange(
            self,
            basin_codes: str,
            timeRange: str,
            elements: str = ("Lat,Lon,Station_Id_C,City,Station_Name,Cnty,Province,Town"),
            data_code: str = "SURF_CHN_MUL_HOR",
            ele_value_ranges: Optional[str] = None,
            order_by: Optional[str] = None,
            limit_cnt: Optional[int] = None,
            data_province_id: Optional[str] = None,
            staLevels: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        return self.call_api(
            "getSurfEleInBasinByTimeRange",
            dataCode=data_code,
            elements=elements,
            timeRange=timeRange,
            basinCodes=basin_codes,
            eleValueRanges=ele_value_ranges,
            orderBy=order_by,
            limitCnt=limit_cnt,
            dataProvinceId=data_province_id,
            staLevels=staLevels,
        )

def safe_float(value: Any, default: float = 0.0) -> float:
    if value in (None, "", "None"):
        return default
    text = str(value).strip()
    if text in {"999999", "999999.0", "999990", "999990.0", "-9999", "-9999.0"}:
        return default
    try:
        return float(text)
    except Exception:
        return default


def normalize_station_level(level: Any) -> str:
    if level is None:
        return ""
    text = str(level).strip()
    if not text:
        return ""
    # 文档示例常写 011/012/013/016，但接口实返常为 11/12/13/16，统一兼容。
    return text.lstrip("0") or "0"


def station_id_of(record: Dict[str, Any]) -> str:
    return str(record.get("Station_Id_C") or record.get("Station_Id_d") or record.get("Station_Id") or "").strip()


def filter_records_by_station_levels(
    records: Sequence[Dict[str, Any]],
    allowed_levels: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    if not allowed_levels:
        return [r for r in records if station_id_of(r)]
    allowed = {normalize_station_level(x) for x in allowed_levels if normalize_station_level(x)}
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
    allowed_station_levels: Optional[Iterable[str]] = ("011", "012", "013", "016"),
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

    level_counts: Dict[str, int] = {}
    for r in records:
        lvl = normalize_station_level(r.get("Station_levl"))
        if lvl:
            level_counts[lvl] = level_counts.get(lvl, 0) + 1

    total_station_ids = {station_id_of(r) for r in records if station_id_of(r)}
    total_count = len(total_station_ids)
    if total_count == 0:
        return {
            "triggered": False,
            "level": None,
            "message": "没有可用于判定的站点数据，请检查 basinCodes / times / staLevels / elements 是否正确。",
            "evidence": {"level_counts": level_counts},
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

        top_stations = []
        sustained_station_ids = set()
        for r in records:
            sid = station_id_of(r)
            if sid in adjacent_ids and safe_float(r.get("PRE_1h")) >= sustain_hourly_threshold_mm:
                sustained_station_ids.add(sid)

        if ratio >= ratio_threshold and sustained:
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
                    "qualified_station_count": len(qualified_ids),
                    "qualified_adjacent_station_count": len(adjacent_ids),
                    "sustained_station_count": len(sustained_station_ids),
                    "total_station_count": total_count,
                    "ratio": round(ratio, 4),
                    "sustained": sustained,
                    "level_counts": level_counts,
                    "top_stations": top_stations[:20],
                },
            }
        return {
            "triggered": False,
            "candidate_level": level,
            "evidence": {
                "window_hours": window_hours,
                "rain_label": rain_label,
                "threshold_mm": threshold_mm,
                "qualified_station_count": len(qualified_ids),
                "qualified_adjacent_station_count": len(adjacent_ids),
                "sustained_station_count": len(sustained_station_ids),
                "total_station_count": total_count,
                "ratio": round(ratio, 4),
                "sustained": sustained,
            },
        }

    # 按最高级别优先判定
    checks = [
        judge(24, thresholds["extraordinary_24h"], 0.15, "I", "特大暴雨"),
        judge(24, thresholds["severe_rainstorm_24h"], 0.15, "II", "大暴雨"),
        judge(12, thresholds["rainstorm_12h"], 0.20, "III", "暴雨"),
        judge(24, thresholds["rainstorm_24h"], 0.20, "IV", "暴雨"),
    ]
    for result in checks:
        if result.get("triggered"):
            return result

    return {
        "triggered": False,
        "level": None,
        "message": "当前未满足 I/II/III/IV 级应急响应条件（仅基于本次实况站点数据判定）。",
        "evidence": {
            "total_station_count": total_count,
            "neighbor_km": neighbor_km,
            "sustain_hourly_threshold_mm": sustain_hourly_threshold_mm,
            "level_counts": level_counts,
            "checks": checks,
        },
    }


def _parse_forecast_start_time(start_time: str) -> datetime:
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H",
        "%Y%m%d%H",
        "%Y%m%d%H%M%S",
    ]
    parsed = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(start_time, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError(f"无法解析 start_time={start_time}，支持格式：YYYYMMDDHH 或 YYYY-MM-DD HH:MM[:SS]")
    if parsed.minute != 0 or parsed.second != 0:
        raise ValueError("start_time 必须是整点时次")
    if parsed.hour % 6 not in (0, 2):
        raise ValueError("EC 起报时次小时须为 0/6/12/18 或 2/8/14/20")
    return parsed


def _normalize_to_ec_cycle_time(start_time: datetime) -> datetime:
    """将时次对齐到 EC 常见 6 小时循环（00/06/12/18），向前取整。"""
    cycle_hour = (start_time.hour // 6) * 6
    return start_time.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


def _ec_file_prefix_candidates(start_time: datetime) -> List[str]:
    """实际起报与 00/06/12/18 归一化前缀，以及与 haihe_mcp_tools 一致的 YYYYMMDD000000 兼容项。"""
    seen: Set[str] = set()
    out: List[str] = []

    def add(p: str) -> None:
        if p not in seen:
            seen.add(p)
            out.append(p)

    for dt in (start_time, _normalize_to_ec_cycle_time(start_time)):
        add(dt.strftime("%Y%m%d%H") + "0000")
        add(dt.strftime("%Y%m%d%H") + "00")
    add(start_time.strftime("%Y%m%d") + "000000")
    return out


def _ec_daily_search_directories(ec_output_path: str, start_time: datetime) -> List[str]:
    ymd = start_time.strftime("%Y%m%d")
    year_s = str(start_time.year)
    dirs: List[str] = []
    seen: Set[str] = set()

    def add(p: str) -> None:
        p = os.path.normpath(p)
        if p not in seen:
            seen.add(p)
            dirs.append(p)

    root_aifs = os.getenv("EC_AIFS_ROOT", DEFAULT_EC_AIFS_ROOT).strip()
    if root_aifs:
        add(os.path.join(root_aifs, year_s, ymd))
    add(os.path.join(ec_output_path, year_s, ymd))
    op = ec_output_path.rstrip(os.sep)
    if op.endswith("output"):
        parent = os.path.dirname(op)
        add(os.path.join(parent, year_s, ymd))
    return dirs


def _find_ec_precip_file(ec_output_path: str, start_time: datetime, forecast_hours: int) -> Optional[str]:
    time_str_10 = start_time.strftime("%Y%m%d%H")
    legacy_tif = f"ec_{time_str_10}_rain_total_{forecast_hours}h.tif"
    grib_names = {f"{pfx}-{forecast_hours}h-oper-fc.grib2" for pfx in _ec_file_prefix_candidates(start_time)}
    legacy_fc = legacy_tif.casefold()
    grib_fc = {x.casefold() for x in grib_names}

    def match_name(file_name: str) -> bool:
        fn = file_name.casefold()
        return fn == legacy_fc or fn in grib_fc

    for day_dir in _ec_daily_search_directories(ec_output_path, start_time):
        if not os.path.isdir(day_dir):
            continue
        try:
            for file_name in os.listdir(day_dir):
                if not match_name(file_name):
                    continue
                full = os.path.join(day_dir, file_name)
                if os.path.isfile(full):
                    return full
        except OSError:
            continue

    if ec_output_path and os.path.isdir(ec_output_path):
        for root, _, files in os.walk(ec_output_path):
            for file_name in files:
                if match_name(file_name):
                    return os.path.join(root, file_name)
    return None


def _grib_read_help_text() -> str:
    return (
        "GRIB 读取需要下列之一：\n"
        "  1) GDAL 带 GRIB: conda install -c conda-forge libgdal-grib\n"
        "  2) Python 回退: conda install -c conda-forge cfgrib eccodes xarray"
    )


def _try_sample_grib_cfgrib(
    station_records: Sequence[Dict[str, Any]],
    path: str,
    method: str,
    value_mult: float,
) -> Optional[Dict[str, float]]:
    try:
        import numpy as np  # type: ignore[import-untyped]
        import xarray as xr  # type: ignore[import-untyped]
    except ImportError:
        return None

    method_norm = (method or "nearest").strip().lower()
    interp_kw = "linear" if method_norm == "bilinear" else "nearest"

    ds = None
    for kwargs in ({}, {"backend_kwargs": {"indexpath": ""}}):
        try:
            ds = xr.open_dataset(path, engine="cfgrib", **kwargs)
            break
        except Exception:
            ds = None
            continue
    if ds is None:
        return None

    sampled: Dict[str, float] = {}
    try:
        da = None
        for _name, v in ds.data_vars.items():
            arr = v.squeeze(drop=True)
            if arr.ndim == 2:
                da = arr
                break
        if da is None:
            for _name, v in ds.data_vars.items():
                arr = v.squeeze(drop=True)
                while arr.ndim > 2:
                    arr = arr.isel({arr.dims[0]: 0})
                if arr.ndim == 2:
                    da = arr
                    break
        if da is None:
            return None

        lat_name = lon_name = None
        for c in ("latitude", "lat"):
            if c in da.coords:
                lat_name = c
                break
        for c in ("longitude", "lon"):
            if c in da.coords:
                lon_name = c
                break
        if lat_name is None or lon_name is None:
            for c in da.coords:
                cl = str(c).lower()
                if "lat" in cl and lat_name is None:
                    lat_name = str(c)
                if "lon" in cl and lon_name is None:
                    lon_name = str(c)
        if lat_name is None or lon_name is None:
            return None

        lons = np.asarray(da[lon_name].values)
        lon_max = float(np.nanmax(lons)) if lons.size else 180.0

        for r in station_records:
            sid = station_id_of(r)
            if not sid:
                continue
            lat = safe_float(r.get("Lat"), default=math.nan)
            lon = safe_float(r.get("Lon"), default=math.nan)
            if math.isnan(lat) or math.isnan(lon):
                continue
            lon_u = lon
            if lon_max > 180.0 and lon_u < 0:
                lon_u += 360.0
            if lon_max <= 180.0 and lon_u > 180.0:
                lon_u -= 360.0
            val = math.nan
            try:
                pt = da.interp({lat_name: float(lat), lon_name: float(lon_u)}, method=interp_kw)
                val = float(np.asarray(pt.values).reshape(-1)[0])
            except Exception:
                try:
                    pt = da.sel({lat_name: float(lat), lon_name: float(lon_u)}, method=interp_kw)
                    val = float(np.asarray(pt.values).reshape(-1)[0])
                except Exception:
                    continue
            if math.isnan(val):
                continue
            sampled[sid] = val * value_mult
        return sampled if sampled else None
    finally:
        try:
            ds.close()
        except Exception:
            pass


def _open_forecast_raster_dataset(path: str) -> Any:
    from osgeo import gdal  # type: ignore[reportMissingImports]

    gdal.UseExceptions()
    lower = path.lower()
    is_grib = lower.endswith((".grib2", ".grb2", ".grib"))
    raw = gdal.Open(path)
    if raw is None:
        if is_grib:
            raise RuntimeError(f"GDAL 无法打开 GRIB（缺少 GRIB 插件）。{_grib_read_help_text()}")
        raise RuntimeError(f"无法打开栅格文件: {path}")
    if not is_grib:
        return raw
    subs = raw.GetSubDatasets()
    raw = None
    if not subs:
        raise RuntimeError(f"GRIB 无子数据集。{_grib_read_help_text()}")
    scored: List[Tuple[int, int, str]] = []
    for sname, desc in subs:
        dlow = desc.lower()
        pri = 5
        if any(k in dlow for k in ("precip", "rain", "total water", "water equiv", "tp")):
            pri = 0
        elif "unknown" in dlow or "surface" in dlow:
            pri = 2
        scored.append((pri, len(desc), sname))
    scored.sort(key=lambda x: (x[0], x[1]))
    for _, _, sname in scored:
        sds = gdal.Open(sname)
        if sds is not None and sds.RasterXSize > 2 and sds.RasterYSize > 2 and sds.RasterCount >= 1:
            return sds
    sds = gdal.Open(subs[0][0])
    if sds is None:
        raise RuntimeError(f"无法打开 GRIB 子数据集: {path}")
    return sds


def _sample_station_forecast_with_gdal(
    station_records: Sequence[Dict[str, Any]],
    raster_path: str,
    method: str,
    value_mult: float,
) -> Dict[str, float]:
    ds = _open_forecast_raster_dataset(raster_path)
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    gt = ds.GetGeoTransform()
    xsize = ds.RasterXSize
    ysize = ds.RasterYSize
    method_norm = (method or "nearest").strip().lower()
    if method_norm not in {"nearest", "bilinear"}:
        raise ValueError("sample_method 仅支持 nearest 或 bilinear")

    sampled: Dict[str, float] = {}
    try:
        def read_pixel(px: int, py: int) -> Optional[float]:
            if px < 0 or px >= xsize or py < 0 or py >= ysize:
                return None
            arr = band.ReadAsArray(px, py, 1, 1)
            if arr is None:
                return None
            val = float(arr[0, 0])
            if nodata is not None and abs(val - float(nodata)) < 1e-6:
                return None
            if math.isnan(val):
                return None
            return val * value_mult

        for r in station_records:
            sid = station_id_of(r)
            if not sid:
                continue
            lat = safe_float(r.get("Lat"), default=math.nan)
            lon = safe_float(r.get("Lon"), default=math.nan)
            if math.isnan(lat) or math.isnan(lon):
                continue
            fx = (lon - gt[0]) / gt[1]
            fy = (lat - gt[3]) / gt[5]
            if method_norm == "nearest":
                val = read_pixel(int(round(fx)), int(round(fy)))
                if val is None:
                    continue
            else:
                if abs(gt[2]) > 1e-12 or abs(gt[4]) > 1e-12:
                    val = read_pixel(int(round(fx)), int(round(fy)))
                    if val is None:
                        continue
                else:
                    x0 = math.floor(fx)
                    y0 = math.floor(fy)
                    x1 = x0 + 1
                    y1 = y0 + 1
                    q11 = read_pixel(x0, y0)
                    q21 = read_pixel(x1, y0)
                    q12 = read_pixel(x0, y1)
                    q22 = read_pixel(x1, y1)
                    vals = [v for v in (q11, q21, q12, q22) if v is not None]
                    if not vals:
                        continue
                    if len(vals) < 4:
                        val = sum(vals) / len(vals)
                    else:
                        dx = fx - x0
                        dy = fy - y0
                        val = (
                            q11 * (1 - dx) * (1 - dy)
                            + q21 * dx * (1 - dy)
                            + q12 * (1 - dx) * dy
                            + q22 * dx * dy
                        )
            sampled[sid] = val
    finally:
        try:
            ds = None
        except Exception:
            pass
    return sampled


def _sample_station_forecast_rain_mm(
    station_records: Sequence[Dict[str, Any]],
    raster_path: str,
    method: str = "nearest",
) -> Dict[str, float]:
    lower = raster_path.lower()
    is_grib = lower.endswith((".grib2", ".grb2", ".grib"))
    value_mult = EC_GRIB_MM_MULTIPLIER if is_grib else 1.0

    if is_grib:
        cfg = _try_sample_grib_cfgrib(station_records, raster_path, method, value_mult)
        if cfg:
            return cfg

    try:
        return _sample_station_forecast_with_gdal(station_records, raster_path, method, value_mult)
    except RuntimeError as exc:
        if is_grib:
            raise RuntimeError(
                f"{exc}\n（已尝试 cfgrib；若未安装: conda install -c conda-forge cfgrib eccodes xarray）"
            ) from exc
        raise


def evaluate_forecast_response(
    station_records: Sequence[Dict[str, Any]],
    rain12: Dict[str, float],
    rain24: Dict[str, float],
    rain6: Dict[str, float],
    thresholds_mm: Optional[Dict[str, float]] = None,
    sustain_threshold_6h_mm: float = 0.1,
    typhoon_landing_impact: bool = False,
    typhoon_impact_increasing: bool = False,
) -> Dict[str, Any]:
    thresholds = dict(DEFAULT_THRESHOLDS_MM)
    if thresholds_mm:
        thresholds.update(thresholds_mm)
    total_station_ids = {station_id_of(r) for r in station_records if station_id_of(r)}
    total_count = len(total_station_ids)
    if total_count == 0:
        return {"triggered": False, "level": None, "message": "没有可用于预报判定的国家站。", "evidence": {}}

    if rain6:
        sustained_station_ids = {sid for sid, v in rain6.items() if v >= sustain_threshold_6h_mm}
        sustain_source = "6h"
    else:
        merged = dict(rain24)
        merged.update(rain12)
        sustained_station_ids = {sid for sid, v in merged.items() if v >= sustain_threshold_6h_mm}
        sustain_source = "12h_or_24h_fallback"

    def judge(
        station_rain_mm: Dict[str, float],
        threshold_mm: float,
        ratio_threshold: float,
        level: str,
        rain_label: str,
        window_hours: int,
    ) -> Dict[str, Any]:
        qualified_ids = {sid for sid, val in station_rain_mm.items() if val >= threshold_mm}
        ratio = len(qualified_ids) / total_count if total_count else 0.0
        sustained_ids = qualified_ids & sustained_station_ids
        sustained = bool(sustained_ids)
        if ratio >= ratio_threshold and sustained:
            return {
                "triggered": True,
                "level": level,
                "message": f"满足{level}级应急响应条件（EC预报口径）",
                "evidence": {
                    "window_hours": window_hours,
                    "rain_label": rain_label,
                    "threshold_mm": threshold_mm,
                    "qualified_station_count": len(qualified_ids),
                    "sustained_station_count": len(sustained_ids),
                    "total_station_count": total_count,
                    "ratio": round(ratio, 4),
                    "sustained": sustained,
                },
            }
        return {
            "triggered": False,
            "candidate_level": level,
            "evidence": {
                "window_hours": window_hours,
                "rain_label": rain_label,
                "threshold_mm": threshold_mm,
                "qualified_station_count": len(qualified_ids),
                "sustained_station_count": len(sustained_ids),
                "total_station_count": total_count,
                "ratio": round(ratio, 4),
                "sustained": sustained,
            },
        }

    checks: List[Dict[str, Any]] = [
        judge(rain24, thresholds["extraordinary_24h"], 0.15, "I", "特大暴雨", 24),
        judge(rain24, thresholds["severe_rainstorm_24h"], 0.15, "II", "大暴雨", 24),
        (
            {
                "triggered": True,
                "level": "III",
                "message": "满足III级应急响应条件（预报口径：登陆台风影响继续加大）",
                "evidence": {"typhoon_impact_increasing": True},
            }
            if typhoon_impact_increasing
            else judge(rain12, thresholds["rainstorm_12h"], 0.20, "III", "暴雨", 12)
        ),
        (
            {
                "triggered": True,
                "level": "IV",
                "message": "满足IV级应急响应条件（预报口径：预报登陆台风将影响海河流域）",
                "evidence": {"typhoon_landing_impact": True},
            }
            if typhoon_landing_impact
            else judge(rain24, thresholds["rainstorm_24h"], 0.20, "IV", "暴雨", 24)
        ),
    ]
    for result in checks:
        if result.get("triggered"):
            result["evidence"] = {
                **result.get("evidence", {}),
                "sustain_source": sustain_source,
                "sustain_threshold_6h_mm": sustain_threshold_6h_mm,
            }
            return result
    return {
        "triggered": False,
        "level": None,
        "message": "当前未满足 I/II/III/IV 级应急响应条件（EC预报口径）。",
        "evidence": {
            "checks": checks,
            "total_station_count": total_count,
            "sustain_source": sustain_source,
            "sustain_threshold_6h_mm": sustain_threshold_6h_mm,
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
    p_judge.add_argument("--allowed-station-levels", default="11,12,13,16")
    p_judge.add_argument("--rainstorm-12h", type=float, default=DEFAULT_THRESHOLDS_MM["rainstorm_12h"])
    p_judge.add_argument("--rainstorm-24h", type=float, default=DEFAULT_THRESHOLDS_MM["rainstorm_24h"])
    p_judge.add_argument("--severe-rainstorm-24h", type=float, default=DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"])
    p_judge.add_argument("--extraordinary-24h", type=float, default=DEFAULT_THRESHOLDS_MM["extraordinary_24h"])
    p_judge.add_argument("--save-records", default=None)

    p_judge_forecast = sub.add_parser("judge_forecast", help="读取 EC 预报并做 I/II/III/IV 预报判定")
    p_judge_forecast.add_argument("--basin-codes", default="HHLY")
    p_judge_forecast.add_argument("--start-time", required=True, help="起报时次：如 2025072302 或 2025-07-23 02:00:00")
    p_judge_forecast.add_argument("--ec-output-path", default=DEFAULT_EC_OUTPUT_PATH)
    p_judge_forecast.add_argument("--allowed-station-levels", default="11,12,13,16")
    p_judge_forecast.add_argument("--rainstorm-12h", type=float, default=DEFAULT_THRESHOLDS_MM["rainstorm_12h"])
    p_judge_forecast.add_argument("--rainstorm-24h", type=float, default=DEFAULT_THRESHOLDS_MM["rainstorm_24h"])
    p_judge_forecast.add_argument("--severe-rainstorm-24h", type=float, default=DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"])
    p_judge_forecast.add_argument("--extraordinary-24h", type=float, default=DEFAULT_THRESHOLDS_MM["extraordinary_24h"])
    p_judge_forecast.add_argument("--sustain-threshold-6h-mm", type=float, default=0.1)
    p_judge_forecast.add_argument("--sample-method", default="nearest", choices=["nearest", "bilinear"])
    p_judge_forecast.add_argument("--typhoon-landing-impact", action="store_true")
    p_judge_forecast.add_argument("--typhoon-impact-increasing", action="store_true")
    p_judge_forecast.add_argument("--save-records", default=None)
    p_judge_forecast.add_argument("--save-result", default=None)

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

    if args.cmd == "judge_forecast":
        parsed_start_time = _parse_forecast_start_time(args.start_time)
        obs_query_time = parsed_start_time.strftime("%Y%m%d%H0000")
        station_records = client.get_surf_ele_in_basin_by_time(
            basin_codes=args.basin_codes,
            times=obs_query_time,
            elements=DEFAULT_OBS_ELEMENTS,
        )
        station_records = filter_records_by_station_levels(
            station_records, [x.strip() for x in args.allowed_station_levels.split(",") if x.strip()]
        )
        station_records = deduplicate_latest_records(station_records)

        tif_24h = _find_ec_precip_file(args.ec_output_path, parsed_start_time, 24)
        tif_12h = _find_ec_precip_file(args.ec_output_path, parsed_start_time, 12)
        tif_6h = _find_ec_precip_file(args.ec_output_path, parsed_start_time, 6)
        if not tif_24h and not tif_12h:
            daily = _ec_daily_search_directories(args.ec_output_path, parsed_start_time)
            raise ValueError(
                f"未找到 12h/24h 预报文件。已查按日目录: {daily[:2]}… 及递归: {args.ec_output_path}。"
                f" 可设置 EC_AIFS_ROOT（默认 /home/ev/data/ec/EC_AIFS）。"
            )

        rain24 = _sample_station_forecast_rain_mm(station_records, tif_24h, method=args.sample_method) if tif_24h else {}
        rain12 = _sample_station_forecast_rain_mm(station_records, tif_12h, method=args.sample_method) if tif_12h else {}
        rain6 = _sample_station_forecast_rain_mm(station_records, tif_6h, method=args.sample_method) if tif_6h else {}

        result = evaluate_forecast_response(
            station_records=station_records,
            rain12=rain12,
            rain24=rain24,
            rain6=rain6,
            thresholds_mm={
                "rainstorm_12h": args.rainstorm_12h,
                "rainstorm_24h": args.rainstorm_24h,
                "severe_rainstorm_24h": args.severe_rainstorm_24h,
                "extraordinary_24h": args.extraordinary_24h,
            },
            sustain_threshold_6h_mm=args.sustain_threshold_6h_mm,
            typhoon_landing_impact=args.typhoon_landing_impact,
            typhoon_impact_increasing=args.typhoon_impact_increasing,
        )
        result["query"] = {
            "basin_codes": args.basin_codes,
            "start_time": parsed_start_time.strftime("%Y-%m-%d %H:%M:%S"),
            "ec_output_path": args.ec_output_path,
            "sample_method": args.sample_method,
            "allowed_station_levels": [x.strip() for x in args.allowed_station_levels.split(",") if x.strip()],
            "ec_files": {"6h": tif_6h, "12h": tif_12h, "24h": tif_24h},
            "typhoon_landing_impact": args.typhoon_landing_impact,
            "typhoon_impact_increasing": args.typhoon_impact_increasing,
        }
        print(json_dump(result))
        if args.save_records:
            with open(args.save_records, "w", encoding="utf-8") as f:
                json.dump(station_records, f, ensure_ascii=False, indent=2)
        if args.save_result:
            with open(args.save_result, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        return


if __name__ == "__main__":
    # main()


    client = MusicClient(MusicConfig())
    # res = client.get_surf_ele_in_basin_by_time("HHLY_JUECE","20250730000000")
    # res = client.stat_surf_pre_in_basin_new("HHLY_JUECE","[20250729020000,20250730010000]")
    # res = client.get_surf_ele_in_region_by_time("120000","20260603000000")
    res = client.stat_surf_ele_in_region("120000","[20260603000000,20260603060000]")

    res = pandas.DataFrame(res)
    res.to_csv("rain.csv")
    print(res)
    # print(pandas.DataFrame(res))

    # res["SUM_PRE_1H"] = res["SUM_PRE_1H"].astype("float")
    # res["COUNT_PRE_1H"] = res["COUNT_PRE_1H"].astype("int")
    #
    # result = (
    #     res.groupby("Station_Id_C", as_index=False, sort=False)
    #     .agg({
    #         "Lat": "first",
    #         "Lon": "first",
    #         "City": "first",
    #         "Station_Name": "first",
    #         "Cnty": "first",
    #         "Province": "first",
    #         "Town": "first",
    #         "SUM_PRE_1H": "sum",
    #         "COUNT_PRE_1H": "sum"  # 如果这个字段表示累计次数，建议也求和
    #     })
    # )
    # print(result)
    #
    # result.to_csv("./new/20250730010000.csv")