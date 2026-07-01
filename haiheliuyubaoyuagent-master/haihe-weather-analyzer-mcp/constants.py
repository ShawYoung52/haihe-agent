"""海河流域 MCP 服务共享常量。"""
from __future__ import annotations

DEFAULT_BASIN_CODES = "HHLY"
DEFAULT_DATA_CODE = "SURF_CHN_MUL_HOR"

# 分钟降水资料（用于应急响应分钟级判定）
DEFAULT_MIN_PRE_DATA_CODE = "SURF_CHN_PRE_MIN"
DEFAULT_MIN_PRE_ELEMENTS = (
    "Station_Id_C,Station_levl,Lat,Lon,Alti,Admin_Code_CHN,V_ACODE_4SEARCH,Town_code,"
    "City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Country,COUNTRYCODE,"
    "Year,Mon,Day,Hour,Min,Datetime,PRE,Q_PRE,PRE_Sensor_Heigh,Station_Type,"
    "REP_CORR_ID,UPDATE_TIME,D_RETAIN_ID,DATA_ID,D_SOURCE_ID,V08010,RYMDHM,IYMDHM"
)

DEFAULT_OBS_ELEMENTS = (
    "Station_levl,Lat,Lon,Alti,Admin_Code_CHN,V_ACODE_4SEARCH,Town_code,"
    "City,Station_Name,Cnty,NetCode,Province,REGIONCODE,Town,Year,Mon,Day,Hour,"
    "PRE_1h,PRE_3h,PRE_6h,PRE_12h,PRE_24h,PRE,Station_Id_C"
)

DEFAULT_THRESHOLDS_MM = {
    # 暴雨：50.0～99.9毫米（此处用下限 50.0 作为触发阈值）
    "rainstorm_12h": 50.0,
    "rainstorm_24h": 50.0,
    "severe_rainstorm_24h": 100.0,
    "extraordinary_24h": 250.0,
}


def _looks_like_nine_zone_codes(basin_codes: str) -> bool:
    """判断 basin_codes 是否为海河九分区编码格式（如 h9_01）。"""
    parts = [x.strip().lower() for x in str(basin_codes or "").split(",") if x.strip()]
    return bool(parts) and all(p.startswith("h9_") for p in parts)
