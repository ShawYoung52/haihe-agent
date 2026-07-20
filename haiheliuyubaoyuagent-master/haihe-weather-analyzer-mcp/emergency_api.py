"""
应急响应查询/确认接口

POST /api/emergency/observation  实况判定（传 times）
POST /api/emergency/forecast     预报判定（传 start_time，自动读 EC tif）
POST /api/emergency/confirm      确认应急响应事件（写入 hh_emergency_event）
GET  /api/emergency/events       查询已确认事件
GET  /api/emergency/levels       查询应急等级定义
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import time
import uuid
from datetime import datetime, timedelta

from constants import DEFAULT_BASIN_CODES
from rolling_forecast_grid import (
    resolve_forecast_grid_source,
    sample_rolling_forecast_at_stations,
)
from typing import Any, Optional
from urllib.parse import urlencode

from haihe_mcp_tools import aggregate_minute_precipitation

import psycopg2
import requests
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("emergency")

app = FastAPI(title="应急响应查询服务", version="1.0.0")

# ===================== 配置 =====================
MUSIC_CONFIG = {
    "service_ip": os.getenv("MUSIC_SERVICE_IP", "10.226.90.120"),
    "service_node_id": os.getenv("MUSIC_SERVICE_NODE_ID", "NMIC_MUSIC_CMADAAS"),
    "user_id": os.getenv("MUSIC_USER_ID", "BETJ_QXT_LYGXPT"),
    "password": os.getenv("MUSIC_PASSWORD", "Qxtly@2022ww"),
    "timeout": 120,
}

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "10.226.107.130"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "postgres"),
}

# EC 预报文件路径
EC_OUTPUT_PATH = os.getenv("EC_OUTPUT_PATH", "/home/ev/data/ec/EC_AIFS/output")
EC_AIFS_ROOT = os.getenv("EC_AIFS_ROOT", "/home/ev/data/ec/EC_AIFS")

OBS_ELEMENTS = (
    "Station_levl,Lat,Lon,Alti,Admin_Code_CHN,V_ACODE_4SEARCH,Town_code,"
    "Station_Id_C,Datetime,City,Station_Name,Cnty,NetCode,Province,REGIONCODE,"
    "Town,Year,Mon,Day,Hour,PRE_1h,PRE_3h,PRE_6h,PRE_12h,PRE_24h,PRE"
)

# 分钟降水资料（实况应急响应分钟级）
MIN_PRE_DATA_CODE = "SURF_CHN_PRE_MIN"
MIN_PRE_ELEMENTS = (
    "Station_Id_C,Station_levl,Lat,Lon,Alti,Admin_Code_CHN,V_ACODE_4SEARCH,Town_code,"
    "City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Country,COUNTRYCODE,"
    "Year,Mon,Day,Hour,Min,Datetime,PRE,Q_PRE,PRE_Sensor_Heigh,Station_Type,"
    "REP_CORR_ID,UPDATE_TIME,D_RETAIN_ID,DATA_ID,D_SOURCE_ID,V08010,RYMDHM,IYMDHM"
)

RAIN_THRESHOLDS = {
    "extraordinary_24h": 250.0,
    "severe_rainstorm_24h": 100.0,
    "rainstorm_12h": 50.0,
    "rainstorm_24h": 50.0,
}

# ===================== 天擎客户端 =====================
class MusicClient:
    def __init__(self):
        self.session = requests.Session()

    def _build_sign(self, params: dict) -> str:
        items = {k: str(v) for k, v in params.items() if v is not None and v != ""}
        content = "&".join(f"{k}={items[k]}" for k in sorted(items.keys()))
        return hashlib.md5(content.encode()).hexdigest().upper()

    def call_api(self, interface_id: str, **kwargs) -> list[dict]:
        cfg = MUSIC_CONFIG
        params = {
            "serviceNodeId": cfg["service_node_id"],
            "userId": cfg["user_id"],
            "dataFormat": "json",
            "interfaceId": interface_id,
            **kwargs,
            "timestamp": str(int(time.time() * 1000)),
            "nonce": str(uuid.uuid4()),
        }
        sign_params = dict(params)
        sign_params["pwd"] = cfg["password"]
        params["sign"] = self._build_sign(sign_params)
        url = f"http://{cfg['service_ip']}/music-ws/api?{urlencode(params, safe=':,[]()')}"

        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=(5, cfg["timeout"]))
                resp.raise_for_status()
                payload = resp.json()
                if isinstance(payload, dict):
                    ds = payload.get("DS")
                    if ds is None:
                        raise Exception(f"天擎返回无DS: {json.dumps(payload, ensure_ascii=False)[:200]}")
                    return ds if isinstance(ds, list) else list(ds)
                return payload
            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt >= 2:
                    raise
                time.sleep(1 * (2 ** attempt) + random.uniform(0, 0.5))
        raise Exception("天擎请求失败")

    def get_surf_ele_in_basin_by_time(self, basin_codes: str, times: str) -> list[dict]:
        return self.call_api(
            "getSurfEleInBasinByTime",
            dataCode="SURF_CHN_MUL_HOR",
            elements=OBS_ELEMENTS,
            times=times,
            basinCodes=basin_codes,
        )

    def get_surf_ele_in_basin_by_time_range(
        self,
        basin_codes: str,
        time_range: str,
        elements: str = OBS_ELEMENTS,
        data_code: str = "SURF_CHN_MUL_HOR",
    ) -> list[dict]:
        return self.call_api(
            "getSurfEleInBasinByTimeRange",
            dataCode=data_code,
            elements=elements,
            timeRange=time_range,
            basinCodes=basin_codes,
        )


# ===================== 实况判定逻辑 =====================
def safe_float(v, default=0.0):
    if v in (None, "", "None"):
        return default
    t = str(v).strip()
    if t in {"999999", "999999.0", "999990", "999990.0", "-9999", "-9999.0"}:
        return default
    try:
        return float(t)
    except Exception:
        return default


def _normalize_time_param(t: str) -> str:
    """将各种时间格式归一化为 14 位 YYYYMMDDHHMMSS。"""
    t = (t or "").strip()
    if len(t) == 14 and t.isdigit():
        return t
    if len(t) == 10 and t.isdigit():
        return t + "0000"
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(t, fmt).strftime("%Y%m%d%H%M%S")
        except ValueError:
            continue
    return t


def _build_min_pre_time_range(times: str, window_hours: int = 24) -> tuple[str, datetime]:
    """把单时刻参数转成分钟降水查询窗口，返回 (time_range, end_time)。"""
    end_time_str = _normalize_time_param(times)
    end_time = datetime.strptime(end_time_str, "%Y%m%d%H%M%S")
    start_time = end_time - timedelta(hours=window_hours)
    time_range = f"[{start_time.strftime('%Y%m%d%H%M%S')},{end_time.strftime('%Y%m%d%H%M%S')}]"
    return time_range, end_time


def evaluate_observation(records: list[dict], thresholds: dict = None) -> dict:
    th = dict(RAIN_THRESHOLDS)
    if thresholds:
        th.update(thresholds)
    records = [r for r in records if r.get("Station_Id_C")]
    seen = {}
    for r in records:
        sid = r.get("Station_Id_C", "")
        dt = f"{r.get('Year','')}{r.get('Mon','')}{r.get('Day','')}{r.get('Hour','')}"
        seen[(sid, dt)] = r
    records = list(seen.values())
    total = len(records)
    if total == 0:
        return {"triggered": False, "level": None, "message": "无站点数据"}

    def judge(win, thresh, ratio_thresh):
        field = f"PRE_{win}h"
        qids = set()
        for r in records:
            sid = r.get("Station_Id_C", "")
            if sid and safe_float(r.get(field)) >= thresh:
                qids.add(sid)
        ratio = len(qids) / total if total else 0
        sustained = any(safe_float(r.get("PRE_1h")) >= 0.1 for r in records if r.get("Station_Id_C", "") in qids)
        return ratio >= ratio_thresh and sustained

    if judge(24, th["extraordinary_24h"], 0.15):
        return {"triggered": True, "level": "I", "message": "满足I级应急响应条件（特大暴雨）"}
    if judge(24, th["severe_rainstorm_24h"], 0.15):
        return {"triggered": True, "level": "II", "message": "满足II级应急响应条件（大暴雨）"}
    if judge(12, th["rainstorm_12h"], 0.20):
        return {"triggered": True, "level": "III", "message": "满足III级应急响应条件（暴雨）"}
    if judge(24, th["rainstorm_24h"], 0.20):
        return {"triggered": True, "level": "IV", "message": "满足IV级应急响应条件（暴雨）"}
    return {"triggered": False, "level": None, "message": "未满足 I/II/III/IV 级条件"}


# ===================== 预报判定逻辑 =====================
def _find_ec_precip_file(start_time: str, forecast_hours: int) -> Optional[str]:
    """查找 EC 预报 tif 文件"""
    # 文件名格式: ec_{start_time}_rain_total_{forecast_hours}h.tif
    # start_time 格式: YYYYMMDDHH (如 2026030902)
    fname = f"ec_{start_time}_rain_total_{forecast_hours}h.tif"

    # 搜索路径
    search_dirs = []
    year = start_time[:4]
    ymd = start_time[:8]
    search_dirs.append(os.path.join(EC_OUTPUT_PATH, year, ymd))
    search_dirs.append(os.path.join(EC_OUTPUT_PATH, ymd))
    search_dirs.append(EC_OUTPUT_PATH)

    root = EC_AIFS_ROOT
    if root:
        search_dirs.append(os.path.join(root, year, ymd))
        search_dirs.append(os.path.join(root, "output", year, ymd))

    for d in search_dirs:
        p = os.path.join(d, fname)
        if os.path.isfile(p):
            return p

    # 递归兜底
    for d in [EC_OUTPUT_PATH]:
        if os.path.isdir(d):
            for root, dirs, files in os.walk(d):
                if fname in files:
                    return os.path.join(root, fname)
    return None


def _sample_forecast_at_stations(
    records: list[dict],
    raster_path: str,
    value_mult: float = 1.0,
) -> dict[str, float]:
    """从 tif 文件中采样站点位置的值"""
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(raster_path)
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    gt = ds.GetGeoTransform()
    xsize, ysize = ds.RasterXSize, ds.RasterYSize

    sampled = {}
    for r in records:
        sid = r.get("Station_Id_C", "")
        if not sid:
            continue
        lat = safe_float(r.get("Lat"), default=math.nan)
        lon = safe_float(r.get("Lon"), default=math.nan)
        if math.isnan(lat) or math.isnan(lon):
            continue
        px = int((lon - gt[0]) / gt[1])
        py = int((lat - gt[3]) / gt[5])
        if px < 0 or px >= xsize or py < 0 or py >= ysize:
            continue
        arr = band.ReadAsArray(px, py, 1, 1)
        if arr is None:
            continue
        val = float(arr[0, 0])
        if nodata is not None and abs(val - float(nodata)) < 1e-6:
            continue
        if math.isnan(val):
            continue
        sampled[sid] = val * value_mult
    ds = None
    return sampled


def evaluate_forecast(records: list[dict], start_time: str, thresholds: dict = None) -> dict:
    """基于 EC tif 的预报判定"""
    th = dict(RAIN_THRESHOLDS)
    if thresholds:
        th.update(thresholds)

    records = [r for r in records if r.get("Station_Id_C")]
    seen = {}
    for r in records:
        sid = r.get("Station_Id_C", "")
        dt = f"{r.get('Year','')}{r.get('Mon','')}{r.get('Day','')}{r.get('Hour','')}"
        seen[(sid, dt)] = r
    records = list(seen.values())
    total = len(records)
    if total == 0:
        return {"triggered": False, "level": None, "message": "无站点数据", "ec_files": {}}

    # 按数据可用性切换：有滚动预报 .nc 用滚动预报，否则用 EC tif
    source_info = resolve_forecast_grid_source(ec_output_path=EC_OUTPUT_PATH)
    ec_files: dict[str, str] = {}
    rain_data: dict[str, dict[str, float]] = {}

    if source_info["source"] == "rolling_forecast":
        nc_path = source_info["file"]
        for h in [6, 12, 24]:
            rain_data[f"{h}h"] = sample_rolling_forecast_at_stations(nc_path, records, h)
        data_source = f"滚动预报网格（cycle={source_info['cycle']}）"
    else:
        for h in [6, 12, 24]:
            p = _find_ec_precip_file(start_time, h)
            if p:
                ec_files[f"{h}h"] = p
        if not ec_files:
            return {
                "triggered": False, "level": None,
                "message": f"未找到 EC 预报文件: {EC_OUTPUT_PATH}/.../ec_{start_time}_rain_total_*.tif",
                "ec_files": {},
            }
        for key, path in ec_files.items():
            rain_data[key] = _sample_forecast_at_stations(records, path)
        data_source = "ECMWF AIFS"

    rain6 = rain_data.get("6h", {})
    rain12 = rain_data.get("12h", {})
    rain24 = rain_data.get("24h", {})
    sustained_ids = set()
    if rain6:
        sustained_ids = {sid for sid, v in rain6.items() if v >= 0.1}
    else:
        merged = dict(rain24)
        merged.update(rain12)
        sustained_ids = {sid for sid, v in merged.items() if v >= 0.1}

    def judge(rain_mm, threshold, ratio_thresh, level):
        qids = {sid for sid, val in rain_mm.items() if val >= threshold}
        ratio = len(qids) / total if total else 0
        sustained = bool(qids & sustained_ids)
        return ratio >= ratio_thresh and sustained

    if rain24 and judge(rain24, th["extraordinary_24h"], 0.15, "I"):
        return {"triggered": True, "level": "I", "message": f"满足I级应急响应条件（{data_source}-特大暴雨）", "ec_files": ec_files}
    if rain24 and judge(rain24, th["severe_rainstorm_24h"], 0.15, "II"):
        return {"triggered": True, "level": "II", "message": f"满足II级应急响应条件（{data_source}-大暴雨）", "ec_files": ec_files}
    if rain12 and judge(rain12, th["rainstorm_12h"], 0.20, "III"):
        return {"triggered": True, "level": "III", "message": f"满足III级应急响应条件（{data_source}-暴雨）", "ec_files": ec_files}
    if rain24 and judge(rain24, th["rainstorm_24h"], 0.20, "IV"):
        return {"triggered": True, "level": "IV", "message": f"满足IV级应急响应条件（{data_source}-暴雨）", "ec_files": ec_files}
    return {"triggered": False, "level": None, "message": f"未满足 I/II/III/IV 级条件（{data_source}）", "ec_files": ec_files}


# ===================== 事件管理 =====================
def _get_db():
    return psycopg2.connect(**DB_CONFIG, connect_timeout=5)


def _create_event(event_code: str, event_type: str, event_level: str, title: str,
                   status: str, start_time: str, ext: dict = None) -> dict:
    """写入 hh_emergency_event 表"""
    now = datetime.now()
    event_id = str(uuid.uuid4())[:8]
    # 转时间格式："20260517000000" -> "2026-05-17 00:00:00"
    if start_time and len(start_time) >= 14 and start_time.isdigit():
        st = f"{start_time[:4]}-{start_time[4:6]}-{start_time[6:8]} {start_time[8:10]}:{start_time[10:12]}:{start_time[12:14]}"
    elif start_time and len(start_time) >= 8 and start_time.isdigit():
        st = f"{start_time[:4]}-{start_time[4:6]}-{start_time[6:8]} 00:00:00"
    else:
        st = start_time
    if not event_code:
        event_code = f"EMERG-{now.strftime('%Y%m%d')}-{event_id}"

    ext_data = ext or {}
    if isinstance(ext_data, dict):
        ext_data["event_id"] = event_id

    with _get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO hh_emergency_event
                    (event_code, event_type, event_level, title, status,
                     start_time, ext, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s::timestamp, %s, %s, %s)
                RETURNING id, event_code, event_type, event_level, title, status,
                          start_time, end_time, ext, created_at, updated_at
            """, (
                event_code, event_type, event_level, title, status,
                st, json.dumps(ext_data, ensure_ascii=False),
                now, now,
            ))
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else {"error": "插入失败"}


def _list_events(event_code: str = "", times: str = ""):
    with _get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if event_code:
                cur.execute("SELECT * FROM hh_emergency_event WHERE event_code = %s ORDER BY created_at DESC", (event_code,))
            elif times:
                cur.execute("SELECT * FROM hh_emergency_event WHERE start_time::text LIKE %s ORDER BY created_at DESC", (f"{times}%",))
            else:
                cur.execute("SELECT * FROM hh_emergency_event ORDER BY created_at DESC LIMIT 100")
            rows = cur.fetchall()
            return [dict(r) for r in rows]


# ===================== 接口定义 =====================

class ObservationRequest(BaseModel):
    times: str = Field(..., description="查询时刻，如 20260517000000")
    basin_codes: str = Field(DEFAULT_BASIN_CODES, description="流域编码")
    include_evidence: bool = Field(False, description="是否返回详细证据")


class ForecastRequest(BaseModel):
    start_time: str = Field(..., description="起报时次，如 2026030902")
    basin_codes: str = Field(DEFAULT_BASIN_CODES, description="流域编码")
    include_evidence: bool = Field(False, description="是否返回详细证据")


class ConfirmRequest(BaseModel):
    level: str = Field(..., description="确认等级：I/II/III/IV")
    times: str = Field(..., description="触发时刻，如 20260517000000")
    event_type: str = Field("rainstorm", description="事件类型：rainstorm/flood/typhoon")
    event_code: str = Field("", description="事件编码（自动生成可留空）")
    title: str = Field("", description="事件标题")
    confirmed_by: str = Field("", description="确认人")
    remark: str = Field("", description="备注")


class EventQuery(BaseModel):
    event_code: str = Field("", description="事件编码")
    times: str = Field("", description="按时刻筛选")


@app.post("/api/emergency/observation")
def emergency_observation(req: ObservationRequest):
    """实况判定：调天擎按分钟级窗口查站点数据，聚合后判断是否触发应急响应"""
    try:
        client = MusicClient()
        time_range, end_time = _build_min_pre_time_range(req.times, window_hours=24)
        raw_records = client.get_surf_ele_in_basin_by_time_range(
            basin_codes=req.basin_codes,
            time_range=time_range,
            elements=MIN_PRE_ELEMENTS,
            data_code=MIN_PRE_DATA_CODE,
        )
        records = aggregate_minute_precipitation(raw_records, end_time=end_time, windows_hours=(1, 12, 24))
        result = evaluate_observation(records)
        if req.include_evidence:
            result["station_count"] = len(records)
            result["records"] = records[:20]
        result["query"] = {"times": req.times, "time_range": time_range, "basin_codes": req.basin_codes, "source": "天擎分钟降水实况"}
        return result
    except Exception as e:
        logger.exception(f"实况判定失败")
        raise HTTPException(500, f"判定失败: {e}")


@app.post("/api/emergency/forecast")
def emergency_forecast(req: ForecastRequest):
    """预报判定：读 EC tif + 分钟级站点数据，判断是否触发应急响应"""
    try:
        client = MusicClient()
        time_range, end_time = _build_min_pre_time_range(req.start_time + "0000", window_hours=24)
        raw_records = client.get_surf_ele_in_basin_by_time_range(
            basin_codes=req.basin_codes,
            time_range=time_range,
            elements=MIN_PRE_ELEMENTS,
            data_code=MIN_PRE_DATA_CODE,
        )
        records = aggregate_minute_precipitation(raw_records, end_time=end_time, windows_hours=(1, 12, 24))
        result = evaluate_forecast(records, req.start_time)
        if req.include_evidence:
            result["station_count"] = len(records)
            result["records"] = records[:20]
        result["query"] = {"start_time": req.start_time, "time_range": time_range, "basin_codes": req.basin_codes, "source": "EC预报+分钟降水实况"}
        return result
    except Exception as e:
        logger.exception(f"预报判定失败")
        raise HTTPException(500, f"判定失败: {e}")


@app.post("/api/emergency/confirm")
def confirm_emergency(req: ConfirmRequest):
    """确认应急响应事件（写入 hh_emergency_event）"""
    try:
        dt_obj = datetime.strptime(req.times[:8], "%Y%m%d")
        level_names = {"I": "特大暴雨", "II": "大暴雨", "III": "暴雨", "IV": "暴雨"}
        level_name = level_names.get(req.level, "")
        title = req.title or f"{dt_obj.strftime('%Y年%m月%d日')} 应急响应{req.level}级（{level_name}）"

        event = _create_event(
            event_code=req.event_code,
            event_type=req.event_type,
            event_level=req.level,
            title=title,
            status="confirmed",
            start_time=req.times,
            ext={"confirmed_by": req.confirmed_by, "remark": req.remark},
        )
        return {"success": True, "message": "已确认", "event": event}
    except Exception as e:
        logger.exception(f"确认失败")
        raise HTTPException(500, f"确认失败: {e}")


@app.get("/api/emergency/events")
def list_events(event_code: str = "", times: str = ""):
    """查询已确认事件"""
    try:
        events = _list_events(event_code=event_code, times=times)
        return {"count": len(events), "events": events}
    except Exception as e:
        logger.exception(f"查询事件失败")
        raise HTTPException(500, f"查询失败: {e}")


@app.get("/api/emergency/levels")
def get_levels():
    return {
        "levels": [
            {"level": "I", "name": "特大暴雨", "threshold_24h": RAIN_THRESHOLDS["extraordinary_24h"]},
            {"level": "II", "name": "大暴雨", "threshold_24h": RAIN_THRESHOLDS["severe_rainstorm_24h"]},
            {"level": "III", "name": "暴雨", "threshold_12h": RAIN_THRESHOLDS["rainstorm_12h"]},
            {"level": "IV", "name": "暴雨", "threshold_24h": RAIN_THRESHOLDS["rainstorm_24h"]},
        ]
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "应急响应查询服务"}


def main():
    port = int(os.getenv("EMERGENCY_API_PORT", "8092"))
    host = os.getenv("EMERGENCY_API_HOST", "0.0.0.0")
    print(f"🔄 应急响应查询服务启动: http://{host}:{port}")
    print(f"📡 POST /api/emergency/observation  实况判定")
    print(f"📡 POST /api/emergency/forecast     预报判定（EC tif）")
    print(f"📡 POST /api/emergency/confirm      确认并写入事件")
    print(f"📡 GET  /api/emergency/events       查询事件")
    print(f"📡 GET  /api/emergency/levels       等级定义")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()