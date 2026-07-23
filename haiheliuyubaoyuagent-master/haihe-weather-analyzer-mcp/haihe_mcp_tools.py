from __future__ import annotations

import hashlib
import json
import math
import os
import random
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from fastmcp import FastMCP

from constants import DEFAULT_BASIN_CODES, DEFAULT_DATA_CODE, DEFAULT_OBS_ELEMENTS, DEFAULT_THRESHOLDS_MM, DEFAULT_MIN_PRE_DATA_CODE, DEFAULT_MIN_PRE_ELEMENTS
from rolling_forecast_service import is_basin_weather_query, query_rolling_forecast_core

try:
    from exception.CustomException import BusinessException
except Exception:
    class BusinessException(Exception):
        pass

DEFAULT_EC_OUTPUT_PATH = os.getenv("EC_OUTPUT_PATH", "/home/ev/data/ec/EC_AIFS/output/")
# 按日分目录时的根目录，例如 /home/ev/data/ec/EC_AIFS/2026/20260308/*.grib2
DEFAULT_EC_AIFS_ROOT = os.getenv("EC_AIFS_ROOT", "/home/ev/data/ec/EC_AIFS")
# GRIB 累计降水：ECMWF 等产品常为「米」水深，需 ×1000 得 mm；若为 kg/m² 则数值上等同 mm，设 1 即可
EC_GRIB_MM_MULTIPLIER = float(os.getenv("EC_GRIB_MM_MULTIPLIER", "1"))

BUILTIN_MUSIC_CONFIG = {
    "service_ip": os.getenv("MUSIC_SERVICE_IP", "10.226.90.120"),
    "service_node_id": os.getenv("MUSIC_SERVICE_NODE_ID", "NMIC_MUSIC_CMADAAS"),
    "user_id": os.getenv("MUSIC_USER_ID", "BETJ_QXT_LYGXPT"),
    "password": os.getenv("MUSIC_PASSWORD", "Qxtly@2022ww"),
    "timeout": int(os.getenv("MUSIC_TIMEOUT", "120")),
}

# 风力预警判定配置
WIND_WARNING_THRESHOLDS = (
    {"level": "蓝色", "avg_level": 6, "gust_level": 7},
    {"level": "黄色", "avg_level": 8, "gust_level": 9},
    {"level": "橙色", "avg_level": 10, "gust_level": 11},
    {"level": "红色", "avg_level": 12, "gust_level": 13},
)
WIND_TABLE_STATION_LEVEL = "16"
WIND_OBSERVATION_ELEMENTS = (
    "Station_levl,City,Station_Name,Cnty,Town,UPDATE_TIME,"
    "WIN_S_Gust_Max,WIN_S_Avg_2mi,WIN_D_Gust_Max"
)
# 天擎接口使用 UTC+0 时，按北京时间生成接口入参。
TIANJIN_TIMEZONE = ZoneInfo("Asia/Shanghai")

# RAG 知识库检索配置
RAG_API_BASE_URL = os.getenv("RAG_API_BASE_URL", "http://10.226.188.156:8033")
RAG_RETRIEVE_PATH = os.getenv("RAG_RETRIEVE_PATH", "/api/v1/chat/knowledge_base_share/search_docs/")
RAG_API_URL = os.getenv("RAG_API_URL", f"{RAG_API_BASE_URL}{RAG_RETRIEVE_PATH}")
RAG_API_TIMEOUT = int(os.getenv("RAG_API_TIMEOUT", "30"))

# 预警生效和历史接口配置
WARNING_API_BASE_URL = os.getenv("WARNING_API_BASE_URL", "https://10.226.123.200:9089")
WARNING_EFFECTIVE_PATH = os.getenv(
    "WARNING_EFFECTIVE_PATH",
    "/weather-public-server/yjxx/selectEffectiveWaring",
)
WARNING_HISTORY_PATH = os.getenv(
    "WARNING_HISTORY_PATH",
    "/weather-public-server/yjxx/selectHistoryWaring",
)
WARNING_API_TIMEOUT = int(os.getenv("WARNING_API_TIMEOUT", "15"))
WARNING_API_VERIFY_SSL = os.getenv("WARNING_API_VERIFY_SSL", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
WARNING_SEVERITY_ORDER = ["蓝色", "黄色", "橙色", "红色"]
NATIONAL_WARNING_API_URL = os.getenv("NATIONAL_WARNING_API_URL", "http://10.1.64.146/awsdsi/main/alerts")
NATIONAL_WARNING_API_TIMEOUT = int(os.getenv("NATIONAL_WARNING_API_TIMEOUT", "15"))
NATIONAL_WARNING_DEFAULT_KEYWORDS = os.getenv("NATIONAL_WARNING_DEFAULT_KEYWORDS", "天津")
NATIONAL_WARNING_MAX_ITEMS = int(os.getenv("NATIONAL_WARNING_MAX_ITEMS", "30"))

# 天津滚动预报接口配置
ROLLING_FORECAST_API_URL = os.getenv(
    "ROLLING_FORECAST_API_URL",
    "http://10.226.120.112:8088/tjgrid/gdyb/getGdybDataByParam",
)
ROLLING_FORECAST_TIMEOUT = int(os.getenv("ROLLING_FORECAST_TIMEOUT", "120"))
ROLLING_FORECAST_ELEMENTS = (
    "WEA",
    "TMAX",
    "TMIN",
    "EDA",
    "RHMAX",
    "RHMIN",
    "TCCMAX",
    "TCCMIN",
    "VISMIN",
    "TP1H",
)
ROLLING_FORECAST_ELEMENT_NAMES = {
    "WEA": "天气现象",
    "TMAX": "最高气温",
    "TMIN": "最低气温",
    "EDA": "风况",
    "RHMAX": "最大相对湿度",
    "RHMIN": "最小相对湿度",
    "TCCMAX": "最大总云量",
    "TCCMIN": "最小总云量",
    "VISMIN": "最小能见度",
    "TP1H": "1小时降水量",
}
ROLLING_FORECAST_COORDS = {
    "天津市区": "117.14_39.24",
    "蓟州": "117.45_40.05",
    "宝坻": "117.28_39.73",
    "武清": "117.06_39.43",
    "宁河": "117.85_39.38",
    "静海": "116.92_38.93",
    "北辰": "117.21_39.07",
    "西青": "117.05_39.08",
    "津南": "117.42_38.95",
    "东丽": "117.34_39.08",
    "滨海新区": "117.79_39.16",
}
ROLLING_FORECAST_REGION_ALIASES = {
    "市区": "天津市区",
    "中心城区": "天津市区",
    "蓟州区": "蓟州",
    "宝坻区": "宝坻",
    "武清区": "武清",
    "宁河区": "宁河",
    "静海区": "静海",
    "北辰区": "北辰",
    "西青区": "西青",
    "津南区": "津南",
    "东丽区": "东丽",
    "滨海": "滨海新区",
}

# POI Elasticsearch 查询配置
POI_ES_HOST = os.getenv("POI_ES_HOST", "http://10.226.107.130:9200")
POI_ES_INDEX = os.getenv("POI_ES_INDEX", "poi_points")
POI_ES_TIMEOUT = int(os.getenv("POI_ES_TIMEOUT", "60"))
POI_ES_MAX_RETRIES = int(os.getenv("POI_ES_MAX_RETRIES", "3"))
POI_SEARCH_MAX_SIZE = int(os.getenv("POI_SEARCH_MAX_SIZE", "50"))
POI_SEARCH_MAX_DISTANCE_KM = int(os.getenv("POI_SEARCH_MAX_DISTANCE_KM", "200"))
_POI_ES_CLIENT = None
_POI_ES_LOCK = threading.Lock()

# RAG 请求体默认模板：name/key/query 在运行时按命中的知识库与用户问题填充
RAG_REQUEST_DEFAULTS = {
    "top_k": 5,
    "score_threshold": 0.2,
    "use_reranker": True,
    "reranker_model": {"本地": "bce-reranker-base_v1"},
    "file_tag_filter": False,
    "file_tag": [{"file_tag": "作者", "content": ["小明"]}],
    "search_method": "hybrid_search",
    "retrieval_evaluate": False,
    "sensitive_check": True,
}

# 知识库元数据列表：name 与 key 一一对应；description 供大模型做意图路由匹配。
RAG_KNOWLEDGE_BASES = [
    {
        "name": "标准规范库",
        "key": "e92fbfc2-1c49-448e-968f-d9a719385f58",
        "description": "包含气象行业法规、气象国家标准、气象行业标准",
    },
    {
        "name": "书籍文献库",
        "key": "5bc4a1b4-ac02-4550-8741-20122ed85fd1",
        "description": "包含气象专业书籍、暴雨文献、强对流文献、暴雪文献、高温文献",
    },
    {
        "name": "专家经验库",
        "key": "1edacafe-00d3-4f0b-a482-78f5f3460d51",
        "description": "包含天气复盘分析、天气概念模型、洪水个例概述、监测预报字典、首席专家经验",
    },
    {
        "name": "基础培训库",
        "key": "237529ca-cf0b-48ba-aee3-b9e1bb87797f",
        "description": "包含强对流分析语料、理论基础知识、预报分析与问答",
    },
    {
        "name": "科普语料库",
        "key": "1a79fd90-ffd8-475b-b09a-bad6dc8ad95b",
        "description": "包含暴雨和高温天气的新闻报道",
    },
]


def _rag_find_kb_by_key(kb_key: str) -> dict | None:
    """按 key 精确匹配知识库元数据。"""
    for kb in RAG_KNOWLEDGE_BASES:
        if kb["key"] == kb_key:
            return kb
    return None


def _rag_build_request(kb: dict, query: str) -> dict:
    """取命中知识库的 name/key，连同用户问题填入 RAG 请求体模板。"""
    body = dict(RAG_REQUEST_DEFAULTS)
    body["key"] = kb["key"]
    body["name"] = kb["name"]
    body["query"] = query
    return body


def _rag_extract_contexts(rag_result: dict, max_contexts: int = 5) -> list[dict]:
    """按天河 RAG 接口固定结构提取 result.contexts。"""
    if not isinstance(rag_result, dict):
        return []

    result = rag_result.get("result")
    if not isinstance(result, dict):
        return []

    raw_contexts = result.get("contexts")
    if not isinstance(raw_contexts, list):
        return []

    contexts = []
    for item in raw_contexts[:max_contexts]:
        if not isinstance(item, dict):
            continue

        content = str(item.get("content") or "").strip()
        if not content:
            continue

        contexts.append(
            {
                "content": content,
                "source": str(item.get("source") or "").strip(),
                "score": item.get("score"),
                "chunking_type": str(item.get("chunking_type") or "").strip(),
                "kb_name": str(item.get("kb_name") or "").strip(),
            }
        )

    return contexts


def _rag_contexts_to_chunks(contexts: list[dict]) -> list[str]:
    """从标准化 contexts 中提取文本片段。"""
    return [
        str(item.get("content") or "").strip()
        for item in contexts
        if isinstance(item, dict) and str(item.get("content") or "").strip()
    ]

def _warning_api_url(path: str) -> str:
    return f"{WARNING_API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"


def _normalize_warning_item(item: dict) -> dict:
    """只保留问答所需的预警字段，避免 raw/raw_response 重复进入模型上下文。"""
    if not isinstance(item, dict):
        return {
            "content": str(item),
            "eventType": None,
            "department": None,
            "time": None,
            "severity": None,
            "msgType": None,
            "locationName": None,
        }
    return {
        "content": item.get("content"),
        "eventType": item.get("eventType"),
        "department": item.get("department"),
        "time": item.get("time"),
        "severity": item.get("severity"),
        "msgType": item.get("msgType"),
        "locationName": item.get("locationName"),
    }


def _is_today_warning_item(item: dict, today: str) -> bool:
    warning_time = str(item.get("time") or "").strip()
    return warning_time.startswith(today)


def _build_today_warning_summary(history_raw_data: list[dict], effective_raw_data: list[dict], now: datetime) -> dict:
    today = now.strftime("%Y-%m-%d")
    today_history = [item for item in history_raw_data if _is_today_warning_item(_normalize_warning_item(item), today)]
    today_published = [_normalize_warning_item(item) for item in today_history]
    effective_warnings = [_normalize_warning_item(item) for item in effective_raw_data]
    event_types = sorted({str(item.get("eventType") or "").strip() for item in today_published if str(item.get("eventType") or "").strip()})
    return {
        "warning_status": "today_summary",
        "query_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "query_hour_text": now.strftime("%H时"),
        "today": today,
        "published_count": len(today_published),
        "new_or_update_count": len(today_published),
        "effective_count": len(effective_warnings),
        "event_types": event_types,
        "today_published_warnings": today_published,
        "today_new_or_update_warnings": today_published,
        "effective_warnings": effective_warnings,
        "field_mapping": {
            "预警正文": "content",
            "预警类型": "eventType",
            "发布单位": "department",
            "发布时间": "time",
            "等级": "severity",
            "发布状态": "msgType",
        },
    }


def _fetch_warning_info(path: str, warning_status: str, include_raw: bool = False) -> dict:
    """请求预警接口"""
    now = datetime.now()
    query_time = now.strftime("%Y-%m-%d %H:%M:%S")
    query_hour_text = now.strftime("%H时")
    url = _warning_api_url(path)
    try:
        resp = requests.get(url, timeout=WARNING_API_TIMEOUT, verify=WARNING_API_VERIFY_SSL)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return {
            "error": "warning_api_failed",
            "message": str(e),
            "url": url,
            "warning_status": warning_status,
            "query_time": query_time,
            "query_hour_text": query_hour_text,
            "warnings": [],
            "count": 0,
            "severity_order": WARNING_SEVERITY_ORDER,
        }

    raw_data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw_data, list):
        raw_data = []
    warnings = [_normalize_warning_item(item) for item in raw_data]
    result = {
        "warning_status": warning_status,
        "query_time": query_time,
        "query_hour_text": query_hour_text,
        "code": payload.get("code") if isinstance(payload, dict) else None,
        "msg": payload.get("msg") if isinstance(payload, dict) else None,
        "count": len(raw_data),
        "warnings": warnings,
        "severity_order": WARNING_SEVERITY_ORDER,
        "field_mapping": {
            "预警正文": "warnings[].content",
            "预警类型": "warnings[].eventType",
            "发布单位": "warnings[].department",
            "发布时间": "warnings[].time",
            "等级": "warnings[].severity",
            "发布状态": "warnings[].msgType",
        },
    }
    if include_raw:
        result["raw_data"] = raw_data
        result["raw_response"] = payload
    return result


def _fetch_today_warning_summary() -> dict:
    now = datetime.now()
    history = _fetch_warning_info(WARNING_HISTORY_PATH, "history", include_raw=True)
    effective = _fetch_warning_info(WARNING_EFFECTIVE_PATH, "effective", include_raw=True)
    if history.get("error"):
        return history
    if effective.get("error"):
        return effective
    return _build_today_warning_summary(
        history_raw_data=history.get("raw_data") or [],
        effective_raw_data=effective.get("raw_data") or [],
        now=now,
    )


def _normalize_national_warning_row(row: list) -> dict:
    """把中央气象台预警 data 行转成回答所需的轻量命名字段。"""
    values = list(row) if isinstance(row, list) else []

    def get(index: int):
        return values[index] if index < len(values) else None

    return {
        "province": get(0),
        "city": get(1),
        "county": get(2),
        "event_type": get(3),
        "severity": get(4),
        "publish_time": get(5),
        "content": get(9),
        "msgType": get(10),
        "url": get(11),
    }


def _split_keywords(keywords: str | None) -> list[str]:
    return [part.strip() for part in str(keywords or "").replace("，", ",").split(",") if part.strip()]


def _national_warning_matches_keywords(item: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    area_text = "".join(
        str(item.get(key) or "")
        for key in ("province", "city", "county")
    )
    full_text = area_text + "".join(
        str(item.get(key) or "")
        for key in ("event_type", "content")
    )
    strict_area_keywords = {"天津", "天津市"}
    for keyword in keywords:
        if keyword in strict_area_keywords:
            # 天津是行政区筛选条件，优先匹配省/市/县，避免其他地区正文
            # 只因偶然提到“天津/京津冀”而被纳入天津清单。
            if keyword in area_text:
                return True
            if not area_text.strip() and keyword in full_text:
                return True
            continue
        if keyword in full_text:
            return True
    return False


def _fetch_national_warning_info(keywords: str | None = None, max_items: int | None = None) -> dict:
    """请求中央气象台预警接口，返回筛选后的轻量化预警记录。"""
    now = datetime.now()
    query_time = now.strftime("%Y-%m-%d %H:%M:%S")
    query_hour_text = now.strftime("%H时")
    try:
        resp = requests.get(NATIONAL_WARNING_API_URL, timeout=NATIONAL_WARNING_API_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        return {
            "error": "national_warning_api_failed",
            "message": str(e),
            "url": NATIONAL_WARNING_API_URL,
            "query_time": query_time,
            "query_hour_text": query_hour_text,
            "warnings": [],
            "count": 0,
        }

    groups = payload if isinstance(payload, list) else []
    all_count = 0
    matched_warnings = []
    keyword_list = _split_keywords(keywords if keywords is not None else NATIONAL_WARNING_DEFAULT_KEYWORDS)
    limit = int(max_items or NATIONAL_WARNING_MAX_ITEMS)
    for group in groups:
        if not isinstance(group, dict):
            continue
        data_rows = group.get("data")
        if not isinstance(data_rows, list):
            continue
        for row in data_rows:
            item = _normalize_national_warning_row(row)
            all_count += 1
            if _national_warning_matches_keywords(item, keyword_list):
                matched_warnings.append(item)

    warnings = matched_warnings[:limit] if limit > 0 else matched_warnings

    return {
        "warning_status": "national",
        "source": "中央气象台预警接口",
        "url": NATIONAL_WARNING_API_URL,
        "query_time": query_time,
        "query_hour_text": query_hour_text,
        "group_count": len(groups),
        "source_count": all_count,
        "matched_count": len(matched_warnings),
        "count": len(warnings),
        "keywords": keyword_list,
        "max_items": limit,
        "truncated": limit > 0 and len(matched_warnings) > limit,
        "warnings": warnings,
        "field_mapping": {
            "省份": "warnings[].province",
            "地市": "warnings[].city",
            "区县": "warnings[].county",
            "预警类型": "warnings[].event_type",
            "等级": "warnings[].severity",
            "发布时间": "warnings[].publish_time",
            "内容": "warnings[].content",
            "详情链接": "warnings[].url",
        },
    }


def _rag_search_doc() -> str:
    """动态生成 rag_search 工具说明：内嵌知识库目录，供大模型按 description 做意图路由。"""
    catalog = "\n".join(
        f"- kb_key=`{kb['key']}`（{kb['name']}）：{kb['description']}"
        for kb in RAG_KNOWLEDGE_BASES
    )
    return (
        "检索业务知识库，返回与问题相关的参考内容片段（chunks）。\n"
        "适用场景：用户问题属于制度、预案、规范、办法、标准等文档型知识范畴，"
        "而非实时气象/河网数据查询。\n\n"
        "意图路由：对比下列知识库的功能描述，选出最匹配的一个，将其 kb_key 作为参数传入；"
        "若没有任何知识库与问题相关，则不要调用本工具。\n\n"
        f"可用知识库：\n{catalog}\n\n"
        "参数：\n"
        "- query：用户问题原文（中文）。\n"
        "- kb_key：上面选中的知识库 key。\n\n"
        "返回：JSON，含 knowledge_base、kb_key、contexts、chunks、sources、count。"
        "contexts 为天河 RAG 接口固定返回结构 result.contexts 的提取结果，"
        "包含 content、source、score、chunking_type、kb_name；chunks 仅为 contexts[].content 的文本列表；"
        "sources 为 contexts[].source 去重后的来源文件列表。"
        "回答时须严格依据 contexts[].content 作答，不得编造；"
        "若 contexts 非空，回答开头必须使用固定句式：根据知识库中的《xxx》……，"
        "其中 xxx 必须来自 contexts[].source；若有多个 source，可写为：根据知识库中的《A》《B》……。"
        "contexts 为空时如实说明未检索到相关内容。"
    )



@dataclass
class MusicConfig:
    service_ip: str = BUILTIN_MUSIC_CONFIG["service_ip"]
    service_node_id: str = BUILTIN_MUSIC_CONFIG["service_node_id"]
    user_id: str = BUILTIN_MUSIC_CONFIG["user_id"]
    password: str = BUILTIN_MUSIC_CONFIG["password"]
    timeout: int = BUILTIN_MUSIC_CONFIG["timeout"]

    @property
    def base_url(self) -> str:
        return f"http://{self.service_ip}/music-ws/api"


class MusicApiError(Exception):
    pass


class MusicClient:
    def __init__(self, config: Optional[MusicConfig] = None):
        self.config = config or MusicConfig()
        if not self.config.user_id or not self.config.password or "请改成" in self.config.user_id:
            raise BusinessException("请先在环境变量或 haihe_mcp_tools.py 的 BUILTIN_MUSIC_CONFIG 中填写 MUSIC 账号密码")
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

        url = f"{self.config.base_url}?{urlencode(query, safe=':,[]()')}"

        # requests timeout 支持 (connect, read) 元组：连接很快失败，读取允许更长
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
                    raise BusinessException(
                        "MUSIC 服务连接失败：远端关闭连接或网络不可达。"
                        f" host={self.config.service_ip}, retries={max_retries}, "
                        f"connect_timeout={connect_timeout}s, read_timeout={read_timeout}s。"
                        " 请检查内网连通性、白名单和 MUSIC 服务状态。"
                    ) from exc
                time.sleep(base_backoff * (2 ** attempt) + random.uniform(0.0, 0.3))
            except requests.exceptions.RequestException as exc:
                raise BusinessException(
                    "MUSIC 服务请求失败：请检查服务地址、账号权限和网络策略。"
                    f" host={self.config.service_ip}, detail={exc}"
                ) from exc
        else:
            if last_exc:
                raise last_exc

        try:
            payload = resp.json()
        except Exception as exc:
            raise MusicApiError(f"接口返回不是 JSON: {resp.text[:500]}") from exc

        if isinstance(payload, dict):
            for key in ("error", "errMsg", "message", "msg"):
                if key in payload and payload.get(key) and str(payload.get(key)).lower() not in {"ok", "success"}:
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
            return_code = str(payload.get("returnCode", ""))
            message = str(payload.get("returnMessage", "")).lower()
            # 查询成功但无记录，按空数据处理
            if return_code == "-1" and "no record" in message:
                return []
            raise MusicApiError(f"返回中没有 DS 字段: {json.dumps(payload, ensure_ascii=False)[:500]}")
        if isinstance(ds, list):
            return ds
        raise MusicApiError(f"DS 不是列表结构: {type(ds)}")

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

    def get_surf_ele_in_basin_by_time_range(
        self,
        basin_codes: str,
        time_range: str,
        elements: str = DEFAULT_OBS_ELEMENTS,
        data_code: str = "SURF_CHN_MUL_HOR",
        ele_value_ranges: Optional[str] = None,
        order_by: Optional[str] = None,
        limit_cnt: Optional[int] = None,
        data_province_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """按时间段、流域检索地面数据要素（支持分钟级时间窗口）。"""
        return self.call_api(
            "getSurfEleInBasinByTimeRange",
            dataCode=data_code,
            elements=elements,
            timeRange=time_range,
            basinCodes=basin_codes,
            eleValueRanges=ele_value_ranges,
            orderBy=order_by,
            limitCnt=limit_cnt,
            dataProvinceId=data_province_id,
        )

    def get_surf_ele_in_region_by_time(
        self,
        admin_codes: str,
        times: str,
        elements: str = WIND_OBSERVATION_ELEMENTS,
        data_code: str = "SURF_CHN_MUL_MIN",
    ) -> List[Dict[str, Any]]:
        """按行政区划获取指定时次的地面站实况要素。"""
        return self.call_api(
            "getSurfEleInRegionByTime",
            dataCode=data_code,
            elements=elements,
            times=times,
            adminCodes=admin_codes,
        )

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

    def stat_surf_pre_in_basin_new(
        self,
        basin_codes: str,
        timeRange: str,
        elements: str = "Lat,Lon,Station_Id_C,City,Station_Name,Cnty,Province,Town",
        statEles: str | None = None,
        ele_value_ranges: Optional[str] = None,
        order_by: Optional[str] = None,
        limit_cnt: Optional[int] = None,
        data_province_id: Optional[str] = None,
        staLevels: Optional[str] = None,
        data_code: str = "SURF_CHN_MUL_HOR",
    ) -> List[Dict[str, Any]]:
        """简化版 statSurfPreInBasin 调用"""
        params = dict(
            dataCode=data_code,
            elements=elements,
            timeRange=timeRange,
            basinCodes=basin_codes,
        )
        if statEles:
            params["statEles"] = statEles
        if ele_value_ranges is not None:
            params["eleValueRanges"] = ele_value_ranges
        if order_by is not None:
            params["orderBy"] = order_by
        if limit_cnt is not None:
            params["limitCnt"] = limit_cnt
        if data_province_id is not None:
            params["dataProvinceId"] = data_province_id
        if staLevels is not None:
            params["staLevels"] = staLevels
        return self.call_api("statSurfPreInBasin", **params)


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


def _parse_valid_wind_speed(value: Any) -> Optional[float]:
    """解析风速；天擎缺测码、0 和非正值均不参与大风阈值计算。"""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in {"", "none", "null", "nan", "99999", "99999.0", "999999", "999999.0", "999990", "999990.0", "-9999", "-9999.0"}:
        return None
    try:
        speed = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(speed) or speed <= 0 or speed >= 99999:
        return None
    return speed


def _wind_speed_to_level(speed: Optional[float]) -> Optional[int]:
    """按风力等级下限将 m/s 转为 0-18 级；无效风速返回 None。"""
    if speed is None:
        return None
    # 各级下限（m/s）
    level_min_speeds = (
        (61.3, 18), (56.1, 17), (51.0, 16), (46.2, 15), (41.5, 14),
        (37.0, 13), (32.7, 12), (28.5, 11), (24.5, 10), (20.8, 9),
        (17.2, 8), (13.9, 7), (10.8, 6), (8.0, 5), (5.5, 4),
        (3.4, 3), (1.6, 2), (0.3, 1), (0.0, 0),
    )
    for min_speed, level in level_min_speeds:
        if speed >= min_speed:
            return level
    return 0


def _wind_direction_to_text(value: Any) -> str:
    """将接口返回的角度风向转为中文十六方位；已是文字时原样保留。"""
    if value is None or str(value).strip() == "":
        return "暂未提供"
    text = str(value).strip()
    try:
        degrees = float(text)
    except (TypeError, ValueError):
        return text
    if not math.isfinite(degrees) or degrees < 0 or degrees > 360:
        return "暂未提供"
    directions = (
        "北", "北东北", "东北", "东东北", "东", "东东南", "东南", "南东南",
        "南", "南西南", "西南", "西西南", "西", "西西北", "西北", "北西北",
    )
    return directions[int((degrees % 360 + 11.25) // 22.5) % 16]


def _format_wind_metric(speed: Optional[float], level: Optional[int]) -> str:
    if speed is None or level is None:
        return "暂无有效数据"
    return f"{level}级（{speed:.1f}米/秒）"


def _station_meets_wind_threshold(metric: Dict[str, Any], threshold: Dict[str, Any]) -> bool:
    """平均风或阵风任一项达标，即认为该站达到对应预警阈值。"""
    avg_level = metric.get("avg_level")
    gust_level = metric.get("gust_level")
    return (
        (avg_level is not None and avg_level >= threshold["avg_level"])
        or (gust_level is not None and gust_level >= threshold["gust_level"])
    )


def _group_wind_metrics_by_county(metrics: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按区县聚合站点，供影响区域分布及模型表述直接使用。"""
    grouped: Dict[str, set[str]] = {}
    for metric in metrics:
        county = str(metric.get("county") or "未注明区县").strip() or "未注明区县"
        station_key = str(metric.get("station_key") or metric.get("station") or "未命名站点")
        grouped.setdefault(county, set()).add(station_key)
    return [
        {"county": county, "station_count": len(stations), "stations": sorted(stations)}
        for county, stations in sorted(grouped.items())
    ]


def _evaluate_tianjin_wind_warning(records: Sequence[Dict[str, Any]], query_time: str) -> Dict[str, Any]:
    """基于全部有效站点判定大风预警阈值，016 国家站仅用于明细表。"""
    metrics: List[Dict[str, Any]] = []
    invalid_record_count = 0

    for record in records:
        if not isinstance(record, dict):
            invalid_record_count += 1
            continue

        avg_speed = _parse_valid_wind_speed(record.get("WIN_S_Avg_2mi"))
        gust_speed = _parse_valid_wind_speed(record.get("WIN_S_Gust_Max"))
        # 两项均无效时不能用于任一阈值判定，也不能计入区域统计。
        if avg_speed is None and gust_speed is None:
            invalid_record_count += 1
            continue

        station = str(record.get("Station_Name") or "未命名站点").strip() or "未命名站点"
        county = str(record.get("Cnty") or "未注明区县").strip() or "未注明区县"
        town = str(record.get("Town") or "").strip()
        metrics.append(
            {
                "station": station,
                "station_key": f"{county}/{station}",
                "county": county,
                "town": town,
                "station_level": normalize_station_level(record.get("Station_levl")),
                "update_time": str(record.get("UPDATE_TIME") or "").strip(),
                "avg_speed_mps": avg_speed,
                "avg_level": _wind_speed_to_level(avg_speed),
                "gust_speed_mps": gust_speed,
                "gust_level": _wind_speed_to_level(gust_speed),
                "gust_direction": _wind_direction_to_text(record.get("WIN_D_Gust_Max")),
            }
        )

    base_result = {
        "query_time": query_time,
        "source_record_count": len(records),
        "valid_station_count": len(metrics),
        "invalid_record_count": invalid_record_count,
        "thresholds": list(WIND_WARNING_THRESHOLDS),
        "threshold_rule": "平均风或最大阵风任一项达到对应阈值，即达到该预警等级。",
    }
    if not metrics:
        return {
            **base_result,
            "status": "no_valid_wind_data",
            "message": "接口未返回可用于大风预警判定的有效风力数据。",
            "station_table": [],
            "threshold_comparison": [],
            "area_distribution": [],
            "attention_areas": [],
            "recommendations": [],
        }

    max_gust = max(
        (metric for metric in metrics if metric["gust_speed_mps"] is not None),
        key=lambda item: item["gust_speed_mps"],
        default=None,
    )
    max_avg = max(
        (metric for metric in metrics if metric["avg_speed_mps"] is not None),
        key=lambda item: item["avg_speed_mps"],
        default=None,
    )

    threshold_comparison = []
    highest_threshold = None
    highest_qualified_metrics: List[Dict[str, Any]] = []
    for threshold in WIND_WARNING_THRESHOLDS:
        qualified_metrics = [
            metric for metric in metrics
            if _station_meets_wind_threshold(metric, threshold)
        ]
        reached = bool(qualified_metrics)
        if reached:
            highest_threshold = threshold
            highest_qualified_metrics = qualified_metrics
        threshold_comparison.append(
            {
                "level": threshold["level"],
                "avg_standard": f"≥{threshold['avg_level']}级",
                "gust_standard": f"≥{threshold['gust_level']}级",
                "current_status": "已达到" if reached else "未达到",
                "qualified_station_count": len(qualified_metrics),
                "qualified_areas": _group_wind_metrics_by_county(qualified_metrics),
            }
        )

    blue_threshold = WIND_WARNING_THRESHOLDS[0]
    blue_qualified = [
        metric for metric in metrics
        if _station_meets_wind_threshold(metric, blue_threshold)
    ]
    # 未达到蓝色阈值、但任一风力指标距蓝色阈值仅差 1 级的站点列为关注对象。
    attention_metrics = [
        metric for metric in metrics
        if metric not in blue_qualified
        and (
            (metric["avg_level"] is not None and metric["avg_level"] >= blue_threshold["avg_level"] - 1)
            or (metric["gust_level"] is not None and metric["gust_level"] >= blue_threshold["gust_level"] - 1)
        )
    ]

    table_rows = []
    for metric in metrics:
        # 国家站（016/16）只影响明细表，不得缩小全市判定或区域统计范围。
        if metric["station_level"] != WIND_TABLE_STATION_LEVEL:
            continue
        table_rows.append(
            {
                "station": metric["station"],
                "county": metric["county"],
                "average_wind": _format_wind_metric(metric["avg_speed_mps"], metric["avg_level"]),
                "maximum_gust": _format_wind_metric(metric["gust_speed_mps"], metric["gust_level"]),
                "gust_direction": metric["gust_direction"],
                "blue_standard": "平均风≥6级或阵风≥7级（蓝色）",
                "meets_blue": "是" if _station_meets_wind_threshold(metric, blue_threshold) else "否",
                "update_time": metric["update_time"],
            }
        )

    highest_level = highest_threshold["level"] if highest_threshold else None
    highest_area_distribution = _group_wind_metrics_by_county(highest_qualified_metrics)
    recommendations = []
    if highest_threshold:
        area_text = "、".join(item["county"] for item in highest_area_distribution)
        recommendations.append(
            f"达标区域（{area_text or '相关区县'}）：建议发布或维持{highest_level}大风预警，并持续跟踪风力变化。"
        )
    if attention_metrics:
        attention_text = "、".join(item["county"] for item in _group_wind_metrics_by_county(attention_metrics))
        recommendations.append(f"接近蓝色阈值区域（{attention_text}）：建议加密监测，提前做好预警准备。")
    if not recommendations:
        recommendations.append("当前未达到大风预警阈值，建议维持常规监测。")

    return {
        **base_result,
        "status": "ok",
        "business_conclusion": {
            "reached": bool(highest_threshold),
            "highest_level": highest_level,
            "max_gust": max_gust,
            "max_average_wind": max_avg,
        },
        "station_table": sorted(table_rows, key=lambda item: (item["county"], item["station"])),
        "threshold_comparison": threshold_comparison,
        "area_distribution": [
            {
                "level": item["level"],
                "areas": item["qualified_areas"],
                "station_count": item["qualified_station_count"],
            }
            for item in threshold_comparison
        ],
        "attention_areas": _group_wind_metrics_by_county(attention_metrics),
        "recommendations": recommendations,
    }


def normalize_station_level(level: Any) -> str:
    if level is None:
        return ""
    text = str(level).strip()
    if not text:
        return ""
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
    result: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for r in records:
        sid = station_id_of(r)
        if not sid:
            continue
        dt = f"{r.get('Year','')}{r.get('Mon','')}{r.get('Day','')}{r.get('Hour','')}"
        result[(sid, dt)] = dict(r)
    return list(result.values())


def _deduplicate_latest_per_station(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按站点去重，每个站点仅保留时间最新的一条记录。"""
    latest: Dict[str, Tuple[datetime, Dict[str, Any]]] = {}
    for r in records:
        sid = station_id_of(r)
        if not sid:
            continue
        dt = _parse_record_datetime(r)
        if dt is None:
            continue
        if sid not in latest or dt > latest[sid][0]:
            latest[sid] = (dt, dict(r))
    return [record for _, record in latest.values()]


# ===================== 分钟降水聚合（SURF_CHN_PRE_MIN） =====================

# 分钟降水质量标志可信取值；业务方可通过环境变量覆盖，如 "0,3,4,5"
TRUSTED_MIN_PRE_Q_PRE: Set[str] = {
    x.strip()
    for x in os.getenv("TRUSTED_MIN_PRE_Q_PRE", "0,3,4").split(",")
    if x.strip()
}


def _parse_record_datetime(r: Dict[str, Any]) -> Optional[datetime]:
    """从分钟降水记录中解析观测时间，优先使用 Datetime 字段，其次使用 Year/Mon/Day/Hour/Min。"""
    dt = r.get("Datetime")
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
    except Exception:
        return None


def _is_min_pre_quality_valid(q_pre: Any, trusted: Optional[Set[str]] = None) -> bool:
    """判断分钟降水质量标志是否可信；未配置或缺失时默认可信。"""
    if q_pre is None or str(q_pre).strip() == "":
        return True
    trusted = trusted if trusted is not None else TRUSTED_MIN_PRE_Q_PRE
    if not trusted:
        return True
    return str(q_pre).strip() in trusted


def aggregate_minute_precipitation(
    records: Sequence[Dict[str, Any]],
    end_time: Optional[datetime] = None,
    windows_hours: Sequence[int] = (1, 12, 24),
    trusted_q_pre: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    把 SURF_CHN_PRE_MIN 的分钟降水记录聚合为各站点的累计降水。

    返回每条记录包含：
      - 站点元信息（Lat/Lon/Station_Name 等）
      - 最新观测时间字段（Year/Mon/Day/Hour/Min/Datetime）
      - PRE: 最新一分钟降水量
      - PRE_1h / PRE_12h / PRE_24h: 对应窗口累计降水（窗口按 end_time 向前推）
      - pre_count_1h / pre_count_12h / pre_count_24h: 参与累加的分钟数
    """
    if end_time is None:
        # 默认以数据中最新的时间作为 end_time
        end_time = max(
            (dt for dt in (_parse_record_datetime(r) for r in records) if dt),
            default=None,
        )
    if end_time is None:
        return []

    windows = sorted(set(windows_hours))
    max_window = max(windows) if windows else 24
    window_cutoffs = {h: end_time - timedelta(hours=h) for h in windows}

    by_station: Dict[str, Dict[str, Any]] = {}
    station_meta: Dict[str, Dict[str, Any]] = {}
    latest_dt: Dict[str, datetime] = {}

    for r in records:
        sid = station_id_of(r)
        if not sid:
            continue
        dt = _parse_record_datetime(r)
        if dt is None or dt > end_time or dt <= end_time - timedelta(hours=max_window):
            continue
        pre = safe_float(r.get("PRE"))
        if pre < 0:
            continue
        if not _is_min_pre_quality_valid(r.get("Q_PRE"), trusted_q_pre):
            continue

        if sid not in by_station:
            by_station[sid] = {f"PRE_{h}h": 0.0 for h in windows}
            for h in windows:
                by_station[sid][f"pre_count_{h}h"] = 0
            latest_dt[sid] = dt
            station_meta[sid] = dict(r)
        else:
            if dt > latest_dt[sid]:
                latest_dt[sid] = dt
                station_meta[sid] = dict(r)

        for h, cutoff in window_cutoffs.items():
            if dt > cutoff:
                by_station[sid][f"PRE_{h}h"] += pre
                by_station[sid][f"pre_count_{h}h"] += 1

    out: List[Dict[str, Any]] = []
    for sid, meta in station_meta.items():
        agg = by_station[sid]
        merged = dict(meta)
        merged.update({k: round(v, 2) if isinstance(v, float) else v for k, v in agg.items()})
        latest = latest_dt[sid]
        merged.update({
            "Year": latest.year,
            "Mon": latest.month,
            "Day": latest.day,
            "Hour": latest.hour,
            "Min": latest.minute,
            "Datetime": latest.strftime("%Y-%m-%d %H:%M:%S"),
            "PRE": safe_float(meta.get("PRE")),
        })
        out.append(merged)
    return out


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
    allowed_station_levels: Optional[Iterable[str]] = ("11", "12", "13", "16"),
) -> Dict[str, Any]:
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

    def judge(window_hours: int, threshold_mm: float, ratio_threshold: float, level: str, rain_label: str) -> Dict[str, Any]:
        value_field = _pick_value_field(window_hours)
        qualified_ids = {
            station_id_of(r)
            for r in records
            if station_id_of(r) and safe_float(r.get(value_field)) >= threshold_mm
        }
        # 按你的要求：不再考虑"相邻国家气象观测站"条件。
        # 将"相邻站集合"直接等同于"满足降水阈值的站集合"。
        adjacent_ids = qualified_ids
        ratio = len(qualified_ids) / total_count if total_count else 0.0

        sustained_station_ids = {
            station_id_of(r)
            for r in records
            if station_id_of(r) in qualified_ids and safe_float(r.get("PRE_1h")) >= sustain_hourly_threshold_mm
        }
        sustained = bool(sustained_station_ids)

        if ratio >= ratio_threshold and sustained:
            top_stations = []
            for r in records:
                sid = station_id_of(r)
                if sid in adjacent_ids:
                    top_stations.append({
                        "Station_Id_C": sid,
                        "Station_Name": r.get("Station_Name"),
                        "Province": r.get("Province"),
                        "City": r.get("City"),
                        "Cnty": r.get("Cnty"),
                        "Lat": r.get("Lat"),
                        "Lon": r.get("Lon"),
                        value_field: r.get(value_field),
                        "PRE_1h": r.get("PRE_1h"),
                    })
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
        "message": "当前未满足 I/II/III/IV 级应急响应条件（仅基于本次实况站点数据判定，不考虑相邻站条件）。",
        "evidence": {
            "total_station_count": total_count,
            "neighbor_km": neighbor_km,
            "sustain_hourly_threshold_mm": sustain_hourly_threshold_mm,
            "level_counts": level_counts,
            "checks": checks,
        },
    }


def _observation_fetch_core(
    basin_codes: str,
    times: str,
    elements: str = DEFAULT_MIN_PRE_ELEMENTS,
    data_code: str = DEFAULT_MIN_PRE_DATA_CODE,
    window_hours: int = 24,
) -> List[Dict[str, Any]]:
    """
    拉取流域分钟降水实况并聚合为累计降水。

    times：结束时刻，如 20250723083000；会向前推 window_hours 小时作为查询窗口。
    """
    if not times:
        raise BusinessException("times 不能为空，例如 20250723083000")
    end_time_str = _normalize_time_param(times)
    try:
        end_time = datetime.strptime(end_time_str, "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise BusinessException(f"无法解析 times={times}，需要 14 位 YYYYMMDDHHMMSS") from exc
    start_time = end_time - timedelta(hours=window_hours)
    time_range = f"[{start_time.strftime('%Y%m%d%H%M%S')},{end_time.strftime('%Y%m%d%H%M%S')}]"

    client = MusicClient()
    records = client.get_surf_ele_in_basin_by_time_range(
        basin_codes=basin_codes,
        time_range=time_range,
        elements=elements,
        data_code=data_code,
    )
    aggregated = aggregate_minute_precipitation(
        records,
        end_time=end_time,
        windows_hours=(1, 12, 24),
    )
    return aggregated


def _observation_filter_core(
    records: Sequence[Dict[str, Any]],
    allowed_station_levels: str = "11,12,13,16",
) -> Dict[str, Any]:
    levels = [x.strip() for x in str(allowed_station_levels).split(",") if x.strip()]
    filtered = filter_records_by_station_levels(records, levels)
    deduped = deduplicate_latest_records(filtered)
    total_station_ids = {station_id_of(r) for r in deduped if station_id_of(r)}
    return {
        "allowed_station_levels": levels,
        "records": deduped,
        "record_count": len(deduped),
        "total_station_count": len(total_station_ids),
    }


def _observation_evaluate_core(
    records: Sequence[Dict[str, Any]],
    allowed_station_levels: Sequence[str],
    neighbor_km: float,
    sustain_hourly_threshold_mm: float,
    rainstorm_12h: float,
    rainstorm_24h: float,
    severe_rainstorm_24h: float,
    extraordinary_24h: float,
) -> Dict[str, Any]:
    return evaluate_observation_response(
        records=records,
        thresholds_mm={
            "rainstorm_12h": rainstorm_12h,
            "rainstorm_24h": rainstorm_24h,
            "severe_rainstorm_24h": severe_rainstorm_24h,
            "extraordinary_24h": extraordinary_24h,
        },
        neighbor_km=neighbor_km,
        sustain_hourly_threshold_mm=sustain_hourly_threshold_mm,
        allowed_station_levels=list(allowed_station_levels),
    )


def _observation_report_core(
    evaluation: Dict[str, Any],
    basin_codes: str,
    times: str,
    allowed_station_levels: Sequence[str],
    include_records: bool = False,
    records: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    result = dict(evaluation)
    result["query"] = {
        "basin_codes": basin_codes,
        "times": times,
        "allowed_station_levels": list(allowed_station_levels),
    }
    if include_records:
        result["records"] = list(records or [])
    return result


def _parse_forecast_start_time(start_time: str) -> datetime:
    if not start_time:
        raise BusinessException("start_time 不能为空，例如 2025072302 或 2025-07-23 02:00:00")
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H",
        # 须先于 %Y%m%d%H%M%S：否则 "2026031000"(YYYYMMDDHH) 会被解析成 3 月 1 日
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
        raise BusinessException(f"无法解析 start_time={start_time}，支持格式：YYYYMMDDHH 或 YYYY-MM-DD HH:MM[:SS]")
    if parsed.minute != 0 or parsed.second != 0:
        # 输入若包含分秒，按"向下取整到整点"处理，避免前端传入非整点时直接报错。
        parsed = parsed.replace(minute=0, second=0, microsecond=0)
    # 允许 0/6/12/18（常见 GRIB 起报）与 2/8/14/20（原 MUSIC/EC 约定）
    if parsed.hour % 6 not in (0, 2):
        raise BusinessException("EC 起报时次小时须为整点且为 0/6/12/18 或 2/8/14/20")
    return parsed


def _normalize_to_ec_cycle_time(start_time: datetime) -> datetime:
    """
    将请求时次对齐到 EC 常见 6 小时循环（00/06/12/18），向前取整。
    例如：02->00, 08->06, 14->12, 20->18。
    """
    cycle_hour = (start_time.hour // 6) * 6
    return start_time.replace(hour=cycle_hour, minute=0, second=0, microsecond=0)


def _ec_file_prefix_candidates(start_time: datetime) -> List[str]:
    """
    EC GRIB 前缀（常见 14 位：YYYYMMDDHH0000，少数数据源为 12 位 YYYYMMDDHH00）。
    同时尝试「实际起报」与「对齐到 00/06/12/18」：磁盘上的 -Nh-oper-fc.grib2 往往带 02/08/14/20，
    若只按归一化前缀会整目录匹配失败。
    """
    seen: Set[str] = set()
    out: List[str] = []
    for dt in (start_time, _normalize_to_ec_cycle_time(start_time)):
        for p in (dt.strftime("%Y%m%d%H") + "0000", dt.strftime("%Y%m%d%H") + "00"):
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def _ec_daily_search_directories(ec_output_path: str, start_time: datetime) -> List[str]:
    """
    按日存储时的候选目录：{根}/{年}/{YYYYMMDD}/。
    默认根为 EC_AIFS_ROOT；若 ec_output_path 以 output 结尾，则在其上级目录下再找同年同日文件夹。
    """
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
    """
    查找预报累计降水文件。支持：
    1) 按日目录：EC_AIFS/{年}/{YYYYMMDD}/ 下精确文件名（你方现状）
    2) 旧版扁平/ output 下递归：GeoTIFF / GRIB2
    3) GeoTIFF：ec_YYYYMMDDHH_rain_total_{N}h.tif
    4) GRIB2：{YYYYMMDDHHMMSS}-{N}h-oper-fc.grib2
    """
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


DEFAULT_EC_FORECAST_HOURS = (12, 24, 36, 48, 60, 72)


def ec_forecast_precip_files_by_horizon(
    parsed_start: datetime,
    ec_output_path: str,
    hours: Sequence[int] = DEFAULT_EC_FORECAST_HOURS,
) -> Dict[str, Optional[str]]:
    """起报时刻下各预报时效累计降水文件路径；某时效无文件则为 None。"""
    return {f"{h}h": _find_ec_precip_file(ec_output_path, parsed_start, h) for h in hours}


def collect_ec_forecast_precip_files(
    start_time: str,
    ec_output_path: str = DEFAULT_EC_OUTPUT_PATH,
    hours: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """
    解析起报时次，返回 12h/24h/36h/48h/60h/72h（默认）等各时效 EC 累计降水栅格路径。
    不要求任一时效必须存在；缺失项为 null，供前端单独展示或下载。
    """
    hrs = tuple(hours) if hours else DEFAULT_EC_FORECAST_HOURS
    if not hrs:
        hrs = DEFAULT_EC_FORECAST_HOURS
    parsed = _parse_forecast_start_time(start_time)
    ec_files = ec_forecast_precip_files_by_horizon(parsed, ec_output_path, hrs)
    return {
        "start_time": parsed.strftime("%Y-%m-%d %H:%M:%S"),
        "start_time_compact": parsed.strftime("%Y%m%d%H"),
        "ec_output_path": ec_output_path,
        "ec_files": ec_files,
    }


def _grib_read_help_text() -> str:
    return (
        "GRIB 读取需要下列之一：\n"
        "  1) GDAL 带 GRIB 驱动: conda install -c conda-forge libgdal-grib\n"
        "  2) Python 回退（推荐，与 GDAL 插件无关）: conda install -c conda-forge cfgrib eccodes xarray"
    )


def _try_sample_grib_cfgrib(
    station_records: Sequence[Dict[str, Any]],
    path: str,
    method: str,
    value_mult: float,
) -> Optional[Dict[str, float]]:
    """用 xarray+cfgrib 在站点经纬度上取值（不依赖 gdal_GRIB 插件）。"""
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
        def pick_precip_da() -> Optional[Any]:
            """
            仅选择"降水"变量，避免把温度（约 288K）等字段误判为降水。
            若未识别到标准降水变量（例如 mock/模板化 GRIB），回退到首个二维网格变量，
            以保证端到端链路可验证。
            """
            candidates: List[Tuple[int, Any]] = []
            fallback_2d: List[Any] = []

            for var_name, v in ds.data_vars.items():
                arr = v.squeeze(drop=True)
                while arr.ndim > 2:
                    arr = arr.isel({arr.dims[0]: 0})
                if arr.ndim != 2:
                    continue
                fallback_2d.append(arr)

                name_l = str(var_name).lower()
                attrs_text = " ".join(
                    str(arr.attrs.get(k, "")).lower()
                    for k in ("long_name", "standard_name", "GRIB_name", "GRIB_shortName", "units")
                )
                text = f"{name_l} {attrs_text}"

                # 明确排除常见非降水变量
                bad_tokens = (
                    "temperature", "temp", "t2m", "2t", "kelvin", "wind", "u component",
                    "v component", "pressure", "msl", "geopotential", "gh", "humidity",
                )
                if any(tok in text for tok in bad_tokens):
                    continue

                score = 0
                # 降水关键词（tp 是 ECMWF total precipitation 常见短名）
                good_tokens = (
                    "total precipitation", "precipitation", "precip", "rain", "tp", "apcp",
                    "grib_shortname=tp", "water equivalent",
                )
                for tok in good_tokens:
                    if tok in text:
                        score += 5
                # 单位线索（mm 或 kg/m^2 常与累计降水对应；m 也常见于 tp）
                units_l = str(arr.attrs.get("units", "")).lower()
                if any(u in units_l for u in ("mm", "kg m-2", "kg/m2", "kg m**-2")):
                    score += 3
                if units_l.strip() == "m":
                    score += 2

                if score > 0:
                    candidates.append((score, arr))

            if not candidates:
                if fallback_2d:
                    return fallback_2d[0]
                return None
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]

        da = pick_precip_da()
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
    """用 GDAL 打开 TIF 或 GRIB2（GRIB 自动选最可能的降水子数据集；需安装 gdal GRIB 驱动）。"""
    try:
        from osgeo import gdal  # type: ignore[reportMissingImports]
    except Exception as exc:
        raise BusinessException("缺少 osgeo.gdal 依赖，无法读取 EC 预报栅格") from exc

    gdal.UseExceptions()
    lower = path.lower()
    is_grib = lower.endswith((".grib2", ".grb2", ".grib"))
    raw = gdal.Open(path)
    if raw is None:
        if is_grib:
            raise BusinessException(
                f"GDAL 无法打开 GRIB（缺少 GRIB 驱动插件）。{_grib_read_help_text()}"
            )
        raise BusinessException(f"无法打开栅格文件: {path}")

    if not is_grib:
        return raw

    subs = raw.GetSubDatasets()
    raw = None
    if not subs:
        raise BusinessException(
            f"GRIB 文件无子数据集（GDAL 未正确识别 GRIB）。{_grib_read_help_text()}"
        )

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
        raise BusinessException(f"无法打开 GRIB 子数据集: {path}")
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
        raise BusinessException("sample_method 仅支持 nearest 或 bilinear")

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
    except BusinessException as exc:
        if is_grib:
            raise BusinessException(
                f"{exc}\n（已优先尝试 cfgrib，若仍失败请安装: conda install -c conda-forge cfgrib eccodes xarray）"
            ) from exc
        raise


def _evaluate_forecast_check(
    station_records: Sequence[Dict[str, Any]],
    station_rain_mm: Dict[str, float],
    total_station_count: int,
    threshold_mm: float,
    ratio_threshold: float,
    level: str,
    rain_label: str,
    window_hours: int,
    sustained_station_ids: Set[str],
) -> Dict[str, Any]:
    qualified_ids = {sid for sid, val in station_rain_mm.items() if val >= threshold_mm}
    ratio = len(qualified_ids) / total_station_count if total_station_count else 0.0
    sustained = bool(qualified_ids & sustained_station_ids)
    if ratio >= ratio_threshold and sustained:
        top_stations = []
        for r in station_records:
            sid = station_id_of(r)
            if sid in qualified_ids:
                top_stations.append({
                    "Station_Id_C": sid,
                    "Station_Name": r.get("Station_Name"),
                    "Province": r.get("Province"),
                    "City": r.get("City"),
                    "Cnty": r.get("Cnty"),
                    "Lat": r.get("Lat"),
                    "Lon": r.get("Lon"),
                    "forecast_rain_mm": round(station_rain_mm.get(sid, 0.0), 2),
                })
        top_stations.sort(key=lambda x: safe_float(x.get("forecast_rain_mm")), reverse=True)
        return {
            "triggered": True,
            "level": level,
            "message": f"满足{level}级应急响应条件（EC预报口径）",
            "evidence": {
                "window_hours": window_hours,
                "rain_label": rain_label,
                "threshold_mm": threshold_mm,
                "qualified_station_count": len(qualified_ids),
                "sustained_station_count": len(qualified_ids & sustained_station_ids),
                "total_station_count": total_station_count,
                "ratio": round(ratio, 4),
                "sustained": sustained,
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
            "sustained_station_count": len(qualified_ids & sustained_station_ids),
            "total_station_count": total_station_count,
            "ratio": round(ratio, 4),
            "sustained": sustained,
        },
    }


def _forecast_fetch_core(
    start_time: str,
    basin_codes: str,
    ec_output_path: str,
    allowed_station_levels: str,
) -> Dict[str, Any]:
    parsed_start_time = _parse_forecast_start_time(start_time)
    ec_files_paths = ec_forecast_precip_files_by_horizon(parsed_start_time, ec_output_path)
    path_24h = ec_files_paths.get("24h")
    path_12h = ec_files_paths.get("12h")
    if not path_24h and not path_12h:
        ts = parsed_start_time.strftime("%Y%m%d%H")
        pfx = _ec_file_prefix_candidates(parsed_start_time)
        g12 = [f"{p}-12h-oper-fc.grib2" for p in pfx]
        g24 = [f"{p}-24h-oper-fc.grib2" for p in pfx]
        daily_dirs = _ec_daily_search_directories(ec_output_path, parsed_start_time)
        daily_hint = daily_dirs[0] if daily_dirs else "(未配置 EC_AIFS_ROOT)"
        raise BusinessException(
            f"未找到起报 {ts} 的 12h/24h 预报文件。\n"
            f"  已查按日目录: {', '.join(daily_dirs[:3])}{'…' if len(daily_dirs) > 3 else ''}\n"
            f"  已递归目录: {ec_output_path}\n"
            f"  期望文件名: GeoTIFF ec_{ts}_rain_total_12h.tif 或 GRIB2 如 {g12[0]} / {g24[0]}\n"
            f"排查: ls -la {daily_hint} 2>/dev/null | head -20\n"
            f"  或: find {daily_hint} -maxdepth 1 -name '*12h-oper-fc.grib2' 2>/dev/null"
        )

    levels = [x.strip() for x in str(allowed_station_levels).split(",") if x.strip()]
    client = MusicClient()
    station_records = _fetch_observation_records_with_fallback(
        client, basin_codes, parsed_start_time, levels
    )

    total_station_ids = {station_id_of(r) for r in station_records if station_id_of(r)}
    total_count = len(total_station_ids)
    if total_count == 0:
        raise BusinessException("没有可用于预报判定的国家站，请检查 basin_codes/allowed_station_levels")

    return {
        "parsed_start_time": parsed_start_time,
        "ec_files": ec_files_paths,
        "station_records": station_records,
        "total_station_count": total_count,
        "allowed_station_levels": levels,
    }


def _fetch_observation_records_with_fallback(
    client: MusicClient,
    basin_codes: str,
    parsed_start_time: datetime,
    levels: List[str],
) -> List[Dict[str, Any]]:
    """按起报时次查询实况站点；无记录时兜底查询近 6 小时并保留每个站点最新记录。"""
    obs_query_time = parsed_start_time.strftime("%Y%m%d%H0000")
    station_records = client.get_surf_ele_in_basin_by_time(
        basin_codes=basin_codes,
        times=obs_query_time,
        elements=DEFAULT_OBS_ELEMENTS,
    )
    station_records = filter_records_by_station_levels(station_records, levels)
    station_records = deduplicate_latest_records(station_records)

    if station_records:
        return station_records

    fallback_start = parsed_start_time - timedelta(hours=6)
    time_range = f"[{fallback_start.strftime('%Y%m%d%H%M%S')},{obs_query_time}]"
    station_records = client.get_surf_ele_in_basin_by_time_range(
        basin_codes=basin_codes,
        time_range=time_range,
        elements=DEFAULT_OBS_ELEMENTS,
    )
    station_records = filter_records_by_station_levels(station_records, levels)
    return _deduplicate_latest_per_station(station_records)


def _forecast_filter_core(
    station_records: Sequence[Dict[str, Any]],
    ec_files_paths: Dict[str, Optional[str]],
    sample_method: str,
    sustain_threshold_6h_mm: float,
) -> Dict[str, Any]:
    path_24h = ec_files_paths.get("24h")
    path_12h = ec_files_paths.get("12h")
    path_6h = ec_files_paths.get("6h")

    rain24 = _sample_station_forecast_rain_mm(station_records, path_24h, method=sample_method) if path_24h else {}
    rain12 = _sample_station_forecast_rain_mm(station_records, path_12h, method=sample_method) if path_12h else {}
    rain6 = _sample_station_forecast_rain_mm(station_records, path_6h, method=sample_method) if path_6h else {}

    if rain6:
        sustained_station_ids = {sid for sid, v in rain6.items() if v >= sustain_threshold_6h_mm}
        sustain_source = "6h"
    else:
        merged = dict(rain24)
        merged.update(rain12)
        sustained_station_ids = {sid for sid, v in merged.items() if v >= sustain_threshold_6h_mm}
        sustain_source = "12h_or_24h_fallback"

    return {
        "rain24": rain24,
        "rain12": rain12,
        "rain6": rain6,
        "sustained_station_ids": sustained_station_ids,
        "sustain_source": sustain_source,
        "sustain_threshold_6h_mm": sustain_threshold_6h_mm,
    }


def _forecast_evaluate_core(
    station_records: Sequence[Dict[str, Any]],
    total_count: int,
    rain24: Dict[str, float],
    rain12: Dict[str, float],
    sustained_station_ids: Set[str],
    rainstorm_12h: float,
    rainstorm_24h: float,
    severe_rainstorm_24h: float,
    extraordinary_24h: float,
    typhoon_landing_impact: bool,
    typhoon_impact_increasing: bool,
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    checks.append(
        _evaluate_forecast_check(
            station_records=station_records,
            station_rain_mm=rain24,
            total_station_count=total_count,
            threshold_mm=extraordinary_24h,
            ratio_threshold=0.15,
            level="I",
            rain_label="特大暴雨",
            window_hours=24,
            sustained_station_ids=sustained_station_ids,
        )
    )
    checks.append(
        _evaluate_forecast_check(
            station_records=station_records,
            station_rain_mm=rain24,
            total_station_count=total_count,
            threshold_mm=severe_rainstorm_24h,
            ratio_threshold=0.15,
            level="II",
            rain_label="大暴雨",
            window_hours=24,
            sustained_station_ids=sustained_station_ids,
        )
    )

    if typhoon_impact_increasing:
        checks.append({
            "triggered": True,
            "level": "III",
            "message": "满足III级应急响应条件（预报口径：登陆台风影响继续加大）",
            "evidence": {"typhoon_impact_increasing": True},
        })
    else:
        checks.append(
            _evaluate_forecast_check(
                station_records=station_records,
                station_rain_mm=rain12,
                total_station_count=total_count,
                threshold_mm=rainstorm_12h,
                ratio_threshold=0.20,
                level="III",
                rain_label="暴雨",
                window_hours=12,
                sustained_station_ids=sustained_station_ids,
            )
        )

    if typhoon_landing_impact:
        checks.append({
            "triggered": True,
            "level": "IV",
            "message": "满足IV级应急响应条件（预报口径：预报登陆台风将影响海河流域）",
            "evidence": {"typhoon_landing_impact": True},
        })
    else:
        checks.append(
            _evaluate_forecast_check(
                station_records=station_records,
                station_rain_mm=rain24,
                total_station_count=total_count,
                threshold_mm=rainstorm_24h,
                ratio_threshold=0.20,
                level="IV",
                rain_label="暴雨",
                window_hours=24,
                sustained_station_ids=sustained_station_ids,
            )
        )
    return checks


def _forecast_report_core(
    checks: Sequence[Dict[str, Any]],
    parsed_start_time: datetime,
    basin_codes: str,
    ec_output_path: str,
    allowed_levels: Sequence[str],
    sample_method: str,
    typhoon_landing_impact: bool,
    typhoon_impact_increasing: bool,
    ec_files_paths: Dict[str, Optional[str]],
    sustain_source: str,
    sustain_threshold_6h_mm: float,
    total_count: int,
    include_records: bool = False,
    station_records: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    query = {
        "basin_codes": basin_codes,
        "start_time": parsed_start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "allowed_station_levels": list(allowed_levels),
        "ec_output_path": ec_output_path,
        "sample_method": sample_method,
        "typhoon_landing_impact": typhoon_landing_impact,
        "typhoon_impact_increasing": typhoon_impact_increasing,
    }

    result: Dict[str, Any] = {
        "triggered": False,
        "level": None,
        "message": "当前未满足 I/II/III/IV 级应急响应条件（EC预报口径）。",
        "evidence": {
            "checks": list(checks),
            "total_station_count": total_count,
            "ec_files": dict(ec_files_paths),
            "sustain_source": sustain_source,
            "sustain_threshold_6h_mm": sustain_threshold_6h_mm,
        },
        "query": query,
    }

    for item in checks:
        if item.get("triggered"):
            result = dict(item)
            result["query"] = query
            result["evidence"] = {
                **result.get("evidence", {}),
                "ec_files": dict(ec_files_paths),
                "sustain_source": sustain_source,
                "sustain_threshold_6h_mm": sustain_threshold_6h_mm,
            }
            break

    if include_records:
        result["records"] = list(station_records or [])
    return result


def evaluate_haihe_forecast_emergency_response_core(
    start_time: str,
    basin_codes: str = DEFAULT_BASIN_CODES,
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
) -> dict:
    """
    预报应急响应判定（与 MCP tool 逻辑一致），供队列 worker / 脚本直接 import 调用。
    若 EC 12h/24h 文件尚未落盘，会抛出 BusinessException，便于上层重试入队。
    返回 evidence.ec_files 含 6h/12h/24h/48h/72h 路径（后两者无文件时为 null）。
    """
    fetched = _forecast_fetch_core(
        start_time=start_time,
        basin_codes=basin_codes,
        ec_output_path=ec_output_path,
        allowed_station_levels=allowed_station_levels,
    )
    filtered = _forecast_filter_core(
        station_records=fetched["station_records"],
        ec_files_paths=fetched["ec_files"],
        sample_method=sample_method,
        sustain_threshold_6h_mm=sustain_threshold_6h_mm,
    )
    checks = _forecast_evaluate_core(
        station_records=fetched["station_records"],
        total_count=fetched["total_station_count"],
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
    return _forecast_report_core(
        checks=checks,
        parsed_start_time=fetched["parsed_start_time"],
        basin_codes=basin_codes,
        ec_output_path=ec_output_path,
        allowed_levels=fetched["allowed_station_levels"],
        sample_method=sample_method,
        typhoon_landing_impact=typhoon_landing_impact,
        typhoon_impact_increasing=typhoon_impact_increasing,
        ec_files_paths=fetched["ec_files"],
        sustain_source=filtered["sustain_source"],
        sustain_threshold_6h_mm=filtered["sustain_threshold_6h_mm"],
        total_count=fetched["total_station_count"],
        include_records=include_records,
        station_records=fetched["station_records"],
    )


def _normalize_time_param(t: str) -> str:
    """将 LLM 可能传的各种时间格式归一化为天擎接口需要的 YYYYMMDDHHmmss（14位）"""
    if not t:
        return t
    t = t.strip()
    # 已经是正确格式
    if len(t) == 14 and t.isdigit():
        return t
    # 格式如 "2026060408"（10位）→ 补 "0000"
    if len(t) == 10 and t.isdigit():
        return t + "0000"
    # 格式如 "2026-06-04 08:00:00" → 转紧凑
    import re as _re
    m = _re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", t)
    if m:
        return f"{m[1]}{m[2]}{m[3]}{m[4]}{m[5]}{m[6]}"
    m = _re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", t)
    if m:
        return f"{m[1]}{m[2]}{m[3]}{m[4]}{m[5]}00"
    m = _re.match(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m[1]}{m[2]}{m[3]}080000"
    # 格式如 "2026/06/04 08:00:00"
    m = _re.match(r"(\d{4})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", t)
    if m:
        return f"{m[1]}{m[2]}{m[3]}{m[4]}{m[5]}{m[6]}"
    return t


def evaluate_emergency_response_core(
    basin_codes: str = DEFAULT_BASIN_CODES,
    times: str = "",
    neighbor_km: float = 50.0,
    sustain_hourly_threshold_mm: float = 0.1,
    allowed_station_levels: str = "11,12,13,16",
    rainstorm_12h: float = DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
    rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
    severe_rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
    extraordinary_24h: float = DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
    include_records: bool = False,
) -> dict:
    """
    应急响应判定核心函数（模块级，可被 MCP 工具和 REST API 共同调用）。
    """
    records = _observation_fetch_core(
        basin_codes=basin_codes,
        times=times,
        elements=DEFAULT_MIN_PRE_ELEMENTS,
    )
    filtered = _observation_filter_core(records=records, allowed_station_levels=allowed_station_levels)
    evaluation = _observation_evaluate_core(
        records=filtered["records"],
        allowed_station_levels=filtered["allowed_station_levels"],
        neighbor_km=neighbor_km,
        sustain_hourly_threshold_mm=sustain_hourly_threshold_mm,
        rainstorm_12h=rainstorm_12h,
        rainstorm_24h=rainstorm_24h,
        severe_rainstorm_24h=severe_rainstorm_24h,
        extraordinary_24h=extraordinary_24h,
    )
    result = _observation_report_core(
        evaluation=evaluation,
        basin_codes=basin_codes,
        times=times,
        allowed_station_levels=filtered["allowed_station_levels"],
        include_records=include_records,
        records=records,
    )
    return result


def _get_poi_es_client():
    """加载 POI Elasticsearch 客户端，避免未调用 POI 工具时影响 MCP 启动。"""
    global _POI_ES_CLIENT
    if _POI_ES_CLIENT is not None:
        return _POI_ES_CLIENT
    with _POI_ES_LOCK:
        if _POI_ES_CLIENT is not None:
            return _POI_ES_CLIENT
        try:
            from elasticsearch import Elasticsearch
        except Exception as exc:
            raise BusinessException(
                "缺少 elasticsearch 依赖，请先安装 elasticsearch 包后再使用 POI 查询工具"
            ) from exc

        try:
            _POI_ES_CLIENT = Elasticsearch(
                [POI_ES_HOST],
                request_timeout=POI_ES_TIMEOUT,
                max_retries=POI_ES_MAX_RETRIES,
                retry_on_timeout=True,
            )
        except TypeError:
            # 兼容 elasticsearch 7.x 客户端参数名。
            _POI_ES_CLIENT = Elasticsearch(
                [POI_ES_HOST],
                timeout=POI_ES_TIMEOUT,
                max_retries=POI_ES_MAX_RETRIES,
                retry_on_timeout=True,
            )
        return _POI_ES_CLIENT


def _validate_poi_keyword(keyword: str) -> str:
    value = str(keyword or "").strip()
    if not value:
        raise BusinessException("POI 查询关键词不能为空")
    return value


def _validate_poi_size(size: int) -> int:
    try:
        value = int(size)
    except Exception as exc:
        raise BusinessException("size 必须是整数") from exc
    if value < 1:
        return 1
    return min(value, POI_SEARCH_MAX_SIZE)


def _validate_poi_distance(distance_km: int | float) -> float:
    try:
        value = float(distance_km)
    except Exception as exc:
        raise BusinessException("distance_km 必须是数字") from exc
    if value <= 0:
        raise BusinessException("distance_km 必须大于 0")
    return min(value, float(POI_SEARCH_MAX_DISTANCE_KM))


def _extract_poi_total(search_resp: dict) -> int:
    total = ((search_resp or {}).get("hits") or {}).get("total", 0)
    if isinstance(total, dict):
        return int(total.get("value") or 0)
    try:
        return int(total)
    except Exception:
        return 0


def _normalize_poi_hit(hit: dict) -> dict:
    source = hit.get("_source") or {}
    location = source.get("location") or {}
    lon = source.get("longitude")
    lat = source.get("latitude")
    if isinstance(location, dict):
        lon = lon if lon is not None else location.get("lon")
        lat = lat if lat is not None else location.get("lat")

    distance_m = None
    sort_values = hit.get("sort")
    if isinstance(sort_values, list) and len(sort_values) >= 2:
        try:
            distance_m = float(sort_values[1])
        except Exception:
            distance_m = None

    return {
        "id": hit.get("_id"),
        "score": hit.get("_score"),
        "name": source.get("name"),
        "category_1": source.get("category_1"),
        "category_2": source.get("category_2"),
        "address": source.get("address"),
        "location": source.get("location"),
        "longitude": lon,
        "latitude": lat,
        "distance_m": distance_m,
    }


def _build_poi_result(match_type: str, search_resp: dict, rows: list[dict] | None = None) -> dict:
    hits = ((search_resp or {}).get("hits") or {}).get("hits") or []
    result_rows = rows if rows is not None else hits
    return {
        "status": "success",
        "match_type": match_type,
        "total": _extract_poi_total(search_resp),
        "rows": result_rows if result_rows else None,
        "hits": hits,
        "pois": [_normalize_poi_hit(hit) for hit in hits],
    }


def _search_poi_core(keyword: str, size: int = 10) -> dict:
    keyword = _validate_poi_keyword(keyword)
    size = _validate_poi_size(size)
    es = _get_poi_es_client()

    exact_body = {
        "size": size,
        "query": {
            "term": {
                "name.keyword": keyword
            }
        }
    }
    exact_resp = es.search(index=POI_ES_INDEX, body=exact_body)
    exact_hits = exact_resp["hits"]["hits"]
    if exact_hits:
        return _build_poi_result("exact", exact_resp, rows=[exact_hits[0]])

    fuzzy_body = {
        "size": size,
        "_source": [
            "name",
            "category_1",
            "category_2",
            "address",
            "location",
            "longitude",
            "latitude",
        ],
        "query": {
            "bool": {
                "should": [
                    {
                        "match_phrase": {
                            "name": {
                                "query": keyword,
                                "boost": 30,
                            }
                        }
                    },
                    {
                        "match": {
                            "name": {
                                "query": keyword,
                                "operator": "and",
                                "boost": 10,
                            }
                        }
                    },
                    {
                        "match": {
                            "name": {
                                "query": keyword,
                                "boost": 1,
                            }
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [
            {
                "_score": {
                    "order": "desc"
                }
            }
        ],
    }
    fuzzy_resp = es.search(index=POI_ES_INDEX, body=fuzzy_body)
    return _build_poi_result("fuzzy", fuzzy_resp)


def _search_poi_by_distance_core(
    keyword: str,
    lon: float,
    lat: float,
    size: int = 10,
    distance_km: int | float = 10,
) -> dict:
    keyword = _validate_poi_keyword(keyword)
    size = _validate_poi_size(size)
    distance_km = _validate_poi_distance(distance_km)
    try:
        lon = float(lon)
        lat = float(lat)
    except Exception as exc:
        raise BusinessException("lon 和 lat 必须是数字") from exc

    body = {
        "size": size,
        "_source": [
            "name",
            "category_1",
            "category_2",
            "address",
            "location",
            "longitude",
            "latitude",
        ],
        "query": {
            "bool": {
                "filter": [
                    {
                        "geo_distance": {
                            "distance": f"{distance_km}km",
                            "location": {
                                "lat": lat,
                                "lon": lon,
                            },
                        }
                    }
                ],
                "should": [
                    {
                        "term": {
                            "name.keyword": {
                                "value": keyword,
                                "boost": 100,
                            }
                        }
                    },
                    {
                        "match_phrase": {
                            "name": {
                                "query": keyword,
                                "boost": 30,
                            }
                        }
                    },
                    {
                        "match": {
                            "name": {
                                "query": keyword,
                                "operator": "and",
                                "boost": 10,
                            }
                        }
                    },
                ],
                "minimum_should_match": 1,
            }
        },
        "sort": [
            {
                "_score": {
                    "order": "desc"
                }
            },
            {
                "_geo_distance": {
                    "location": {
                        "lat": lat,
                        "lon": lon,
                    },
                    "order": "asc",
                    "unit": "m",
                    "distance_type": "arc",
                }
            },
        ],
    }
    fuzzy_resp = _get_poi_es_client().search(index=POI_ES_INDEX, body=body)
    return _build_poi_result("fuzzy", fuzzy_resp)


def register_haihe_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def search_poi(keyword: str, size: int = 10) -> dict:
        """
        根据名称查询地点、设施、单位等兴趣点,返回经纬度信息。

        查询策略：
        - 先用 name.keyword 做精确匹配；
        - 精确匹配无结果时，再按名称短语、分词 AND、普通分词做加权模糊匹配；
        - 返回原始 ES hits，并额外提供精简 pois 列表，便于问答直接引用名称、地址、经纬度。

        Args:
            keyword: POI 名称关键词，例如“天津站”“海河医院”“独流减河”。
            size: 返回数量，默认 10，最大值可通过 POI_SEARCH_MAX_SIZE 配置。
        """
        try:
            return _search_poi_core(keyword=keyword, size=size)
        except Exception as exc:
            return {
                "status": "error",
                "message": f"POI 查询失败：{exc}",
                "match_type": None,
                "rows": None,
                "hits": [],
                "pois": [],
            }

    @mcp.tool()
    def search_poi_by_distance(
        keyword: str,
        lon: float,
        lat: float,
        size: int = 10,
        distance_km: int = 10,
    ) -> dict:
        """
        按名称和经纬度范围查询附近 POI。

        适合回答“某坐标附近的医院/学校/水库/村庄/站点”等地点检索问题。结果先按名称相关度排序，
        再按到输入坐标的地理距离升序排序，pois 中的 distance_m 表示距离（米）。

        Args:
            keyword: POI 名称关键词。
            lon: 查询中心点经度。
            lat: 查询中心点纬度。
            size: 返回数量，默认 10，最大值可通过 POI_SEARCH_MAX_SIZE 配置。
            distance_km: 查询半径，单位公里，默认 10。
        """
        try:
            return _search_poi_by_distance_core(
                keyword=keyword,
                lon=lon,
                lat=lat,
                size=size,
                distance_km=distance_km,
            )
        except Exception as exc:
            return {
                "status": "error",
                "message": f"附近 POI 查询失败：{exc}",
                "match_type": None,
                "rows": None,
                "hits": [],
                "pois": [],
            }

    @mcp.tool()
    def query_rolling_forecast(
        user_query: str,
        regions: str = "",
        lon: float | None = None,
        lat: float | None = None,
        point_name: str = "",
        matched_region: str = "",
        fcst_time: str | None = None,
        start_period: int = 0,
        end_period: int = 240,
        interval: int = 12,
        forecast_start_date: str = "",
        forecast_days: int = 0,
    ) -> dict:
        """查询天津滚动预报。适合未来天气、未来一周、明后天、周末、升降温、强降雨、大暴雨、雾霾/能见度、户外活动适宜性等预报类问题。

        **适用范围仅限天津及天津区级区域。禁止**用于"海河流域""流域"或具体河系/子流域
        （大清河、子牙河、永定河、北三河、漳卫南运河、徒骇马颊河、黑龙港、滦河、潮白河、蓟运河等）
        对象的天气/降雨问题——无论今天、明天还是未来，这些问题必须改用
        `get_river_system_rainfall_forecast`（九大分区河系级降雨预报）。
        本工具是天津 11 站点接口，不覆盖海河流域；误用时工具会直接报错。

        当问题询问“高温/暴雨/大风预警期间”的实际最高气温、雨量、风力、影响时段或影响区域时，
        仍属于动态预报查询，应调用本工具；例如“高温预警期间最高会到多少度？”。
        只有询问预警发布标准、颜色等级、阈值、定义或各级区别时，才不属于本工具的预报查询范围。

        未来自然日问题由大模型选择本工具，并传入 forecast_start_date 和 forecast_days。
        “本周末/这个周末/周末天气”查询当周尚未过去的周六、周日；“下周末”查询下一个自然周的周六、周日。
        其他“最近会……/近期会……/未来一周/未来七天/未来几天”必须从明天 00:00 开始查 7 个自然日；
        “未来N天”明确了数量时从明天开始查 N 天。预警过程的动态预报值未说明日期时也按明天起 7 天处理。
        不要由模型传底层时效参数代替这两个业务参数。

        Args:
            user_query: 用户原始问题，用于解析区域；未说明地点时默认查询天津全部区域。
            regions: 可选的区域文本，例如“西青”“西青和津南”；为空时从 user_query 解析。
            lon: 可选点位经度；传入 lon/lat 时进入点位模式，按指定坐标查询。
            lat: 可选点位纬度；传入 lon/lat 时进入点位模式，按指定坐标查询。
            point_name: 点位名称，用于返回结果标注，例如“梅江会展中心附近代表点”。
            matched_region: 点位匹配到的滚动预报代表区域，例如“西青”。
            fcst_time: 可选起报时间，格式 YYYYMMDDHHMMSS；默认按当前时间自动选择最新可用起报时次。
            start_period: 起始预报时效，单位小时。
            end_period: 结束预报时效，单位小时，默认 240。
            interval: 时间步长，单位小时，默认 12。
            forecast_start_date: 自然日查询的开始日期，格式 YYYY-MM-DD。除明确的周末查询外，未来类问题从明天开始。
            forecast_days: 从 forecast_start_date 起连续查询的自然日数。与 forecast_start_date 同时传入时，
                工具自动使用每日 00:00-24:00、24 小时步长，并覆盖 fcst_time/start_period/end_period/interval。

        未来日历日参数规则：
        - “本周末”“这个周末”“周末天气”：周一至周五从当周周六开始查2天；周六从当天开始查2天；周日从当天开始查1天。
        - “下周末”：forecast_start_date=下一个自然周的周六，forecast_days=2。
        - “最近会……”“近期会……”“未来一周”“未来七天/7天”“未来几天”：从明天开始，forecast_days=7。
        - “未来N天”明确了数量时：从明天开始，forecast_days=N。
        - “明天”：forecast_start_date=明天，forecast_days=1；“后天”则从后天开始查 1 天。
        - 日历日模式会返回 daily_summary、temperature_analysis、visibility_analysis、
          rainstorm_analysis 和 weather_focus，回答时应优先使用这些代码统计结果，不要由模型重新比较数组。

        当前滚动气象信息专用规则：
        - “请输出当前时刻的滚动气象信息实况”由调用方对本工具调用两次。
        - 第一次查当前整点至下一整点，interval=1，end_period=start_period+1。
        - 第二次从下一整点起查未来12小时，interval=12，end_period=start_period+12。
        - 两次必须使用同一 fcst_time，regions 留空以查询全市11个代表区域，不传 forecast_start_date/forecast_days。
        - 调用方由代码计算 TP1H 平均值、最大值和对应地区；模型使用接口返回与代码统计撰写总结，表格仅由代码生成。
        """
        # 点位模式（调用方已解析出明确经纬度，如决策天气 POI）不做流域拦截，
        # 避免"潮白河湿地公园"类点位问题被误伤。
        if (lon is None or lat is None) and (
            is_basin_weather_query(user_query) or is_basin_weather_query(regions)
        ):
            raise BusinessException(
                "本工具仅覆盖天津及区级区域，不适用于海河流域/河系天气问题；"
                "请改用 get_river_system_rainfall_forecast 查询九大分区河系级降雨预报。"
            )
        return query_rolling_forecast_core(
            user_query=user_query,
            regions=regions,
            lon=lon,
            lat=lat,
            point_name=point_name,
            matched_region=matched_region,
            fcst_time=fcst_time,
            start_period=start_period,
            end_period=end_period,
            interval=interval,
            forecast_start_date=forecast_start_date,
            forecast_days=forecast_days,
        )

    @mcp.tool()
    def get_haihe_station_observations(
        basin_codes: str,
        times: str,
        elements: str = DEFAULT_OBS_ELEMENTS,
        ele_value_ranges: str | None = None,
        order_by: str | None = None,
        limit_cnt: int | None = None,
        data_province_id: str | None = None,
    ) -> list[dict]:
        """按流域和时次拉取站点实况要素。适合问：过去某个整点时刻（02/08/14/20时）各站实况数据。
注意：仅气象观测整点时刻（02时/08时/14时/20时）有数据，非整点查询会返回空。
不适合问"今天天气怎么样""当前雨情"——请用 get_city_rainfall_time_range 或 analyze_rainfall_by_time。"""
        # 时间格式归一化：兼容 LLM 可能传的各种紧凑格式
        times = _normalize_time_param(times)

        # 解析 order_by 中的字段名和方向；上游 API 的 orderBy 只接收字段名，不接受 "PRE_1h DESC"
        sort_field, sort_desc = None, False
        api_order_by = order_by
        if order_by:
            parts = order_by.strip().split()
            if len(parts) >= 2 and parts[-1].upper() in ("DESC", "ASC"):
                sort_field = parts[0]
                sort_desc = parts[-1].upper() == "DESC"
                api_order_by = sort_field

        client = MusicClient()
        result = client.get_surf_ele_in_basin_by_time(
            basin_codes=basin_codes,
            times=times,
            elements=elements,
            ele_value_ranges=ele_value_ranges,
            order_by=api_order_by,
            limit_cnt=None if sort_desc else limit_cnt,
            data_province_id=data_province_id,
        )

        # 如果调用方指定了排序方向，在本地按字段排序并截断
        if sort_field and isinstance(result, list):
            actual_field = sort_field
            if result and sort_field not in result[0]:
                for key in result[0].keys():
                    if key.upper() == sort_field.upper():
                        actual_field = key
                        break
            try:
                result = sorted(
                    result,
                    key=lambda x: float(x.get(actual_field, 0) or 0),
                    reverse=sort_desc,
                )
            except Exception:
                pass
            if limit_cnt:
                result = result[:limit_cnt]

        return result

    @mcp.tool()
    def stat_haihe_basin_precipitation(
        basin_codes: str,
        time_range: str,
        elements: str,
        stat_eles: str,
        sta_levels: str | None = None,
        stat_ele_value_ranges: str | None = None,
        ele_value_ranges: str | None = None,
        order_by: str | None = None,
        limit_cnt: int | None = None,
        data_province_id: str | None = None,
    ) -> list[dict]:
        """按流域和时间段统计降水。适合问：海河流域过去24小时哪些站降水最大、某类站累计情况怎样。"""
        client = MusicClient()
        return client.stat_surf_pre_in_basin_new(
            basin_codes=basin_codes,
            timeRange=time_range,
            elements=elements,
            statEles=stat_eles,
            staLevels=sta_levels,
            ele_value_ranges=ele_value_ranges,
            order_by=order_by,
            limit_cnt=limit_cnt,
            data_province_id=data_province_id,
        )

    @mcp.tool()
    def fetch_haihe_observation_response_inputs(
        basin_codes: str = DEFAULT_BASIN_CODES,
        times: str = "",
    ) -> dict:
        """
        观测判定拆分-1（fetch）：拉取流域站点实况记录。
        """
        records = _observation_fetch_core(
            basin_codes=basin_codes,
            times=times,
        )
        return {"records": records, "record_count": len(records), "query": {"basin_codes": basin_codes, "times": times}}

    @mcp.tool()
    def filter_haihe_observation_response_records(
        records: list[dict],
        allowed_station_levels: str = "11,12,13,16",
    ) -> dict:
        """
        观测判定拆分-2（filter）：按国家站等级过滤并去重。
        """
        filtered = _observation_filter_core(records=records, allowed_station_levels=allowed_station_levels)
        return filtered

    @mcp.tool()
    def evaluate_haihe_observation_response_records(
        records: list[dict],
        allowed_station_levels: str = "11,12,13,16",
        neighbor_km: float = 50.0,
        sustain_hourly_threshold_mm: float = 0.1,
        rainstorm_12h: float = DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
        rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
        severe_rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
        extraordinary_24h: float = DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
    ) -> dict:
        """
        观测判定拆分-3（evaluate）：基于过滤后的站点记录做 I/II/III/IV 判定。
        """
        filtered = _observation_filter_core(records=records, allowed_station_levels=allowed_station_levels)
        return _observation_evaluate_core(
            records=filtered["records"],
            allowed_station_levels=filtered["allowed_station_levels"],
            neighbor_km=neighbor_km,
            sustain_hourly_threshold_mm=sustain_hourly_threshold_mm,
            rainstorm_12h=rainstorm_12h,
            rainstorm_24h=rainstorm_24h,
            severe_rainstorm_24h=severe_rainstorm_24h,
            extraordinary_24h=extraordinary_24h,
        )

    @mcp.tool()
    def report_haihe_observation_response(
        evaluation: dict,
        basin_codes: str = DEFAULT_BASIN_CODES,
        times: str = "",
        allowed_station_levels: str = "11,12,13,16",
        include_records: bool = False,
        records: list[dict] | None = None,
    ) -> dict:
        """
        观测判定拆分-4（report）：拼装统一输出结构（query + 可选 records）。
        """
        levels = [x.strip() for x in str(allowed_station_levels).split(",") if x.strip()]
        return _observation_report_core(
            evaluation=evaluation,
            basin_codes=basin_codes,
            times=times,
            allowed_station_levels=levels,
            include_records=include_records,
            records=records,
        )

    @mcp.tool()
    def evaluate_haihe_emergency_response(
        basin_codes: str = DEFAULT_BASIN_CODES,
        times: str = "",
        scope: str = "haihe",
        neighbor_km: float = 50.0,
        sustain_hourly_threshold_mm: float = 0.1,
        allowed_station_levels: str = "11,12,13,16",
        rainstorm_12h: float = DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
        rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
        severe_rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
        extraordinary_24h: float = DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
        include_records: bool = False,
    ) -> dict:
        """
        基于流域站点实况，自动判定海河流域是否满足 I/II/III/IV 级应急响应条件。

        说明：
        - 当前是实况口径，不含台风和未来预报口径。
        - 不再考虑"相邻国家气象观测站"条件；仅以满足降水阈值的站点集合判定。
        - 国家站等级默认使用 11,12,13,16。
        - scope：判定范围，"haihe"为全流域，"nine_zone"为九分区。九分区时需 basin_codes 传九分区编码（如 h9_001,h9_002...）。
        """
        scope_norm = (scope or "haihe").strip().lower()
        if scope_norm in ("nine_zone", "9zone", "9"):
            from emergency_response_interface import query_haihe_emergency_observation
            return query_haihe_emergency_observation(
                times=times,
                basin_codes=basin_codes,
                scope="nine_zone",
                neighbor_km=neighbor_km,
                sustain_hourly_threshold_mm=sustain_hourly_threshold_mm,
                allowed_station_levels=allowed_station_levels,
                rainstorm_12h=rainstorm_12h,
                rainstorm_24h=rainstorm_24h,
                severe_rainstorm_24h=severe_rainstorm_24h,
                extraordinary_24h=extraordinary_24h,
                include_records=include_records,
            )
        result = evaluate_emergency_response_core(
            basin_codes=basin_codes,
            times=times,
            neighbor_km=neighbor_km,
            sustain_hourly_threshold_mm=sustain_hourly_threshold_mm,
            allowed_station_levels=allowed_station_levels,
            rainstorm_12h=rainstorm_12h,
            rainstorm_24h=rainstorm_24h,
            severe_rainstorm_24h=severe_rainstorm_24h,
            extraordinary_24h=extraordinary_24h,
            include_records=include_records,
        )
        result["deprecated_note"] = (
            "建议迁移到四段工具链：fetch_haihe_observation_response_inputs -> "
            "filter_haihe_observation_response_records -> "
            "evaluate_haihe_observation_response_records -> "
            "report_haihe_observation_response。"
        )
        return result

    @mcp.tool()
    def fetch_haihe_forecast_response_inputs(
        start_time: str,
        basin_codes: str = DEFAULT_BASIN_CODES,
        ec_output_path: str = DEFAULT_EC_OUTPUT_PATH,
        allowed_station_levels: str = "11,12,13,16",
    ) -> dict:
        """
        预报判定拆分-1（fetch）：解析时次、定位 EC 文件、获取并预处理站点记录。
        """
        fetched = _forecast_fetch_core(
            start_time=start_time,
            basin_codes=basin_codes,
            ec_output_path=ec_output_path,
            allowed_station_levels=allowed_station_levels,
        )
        return {
            "parsed_start_time": fetched["parsed_start_time"].strftime("%Y-%m-%d %H:%M:%S"),
            "ec_files": fetched["ec_files"],
            "total_station_count": fetched["total_station_count"],
            "allowed_station_levels": fetched["allowed_station_levels"],
            "station_records": fetched["station_records"],
        }

    @mcp.tool()
    def filter_haihe_forecast_response_inputs(
        station_records: list[dict],
        ec_files: dict,
        sample_method: str = "nearest",
        sustain_threshold_6h_mm: float = 0.1,
    ) -> dict:
        """
        预报判定拆分-2（filter）：按站点采样 6h/12h/24h 预报雨量并产出持续性站点集合。
        """
        filtered = _forecast_filter_core(
            station_records=station_records,
            ec_files_paths=ec_files,
            sample_method=sample_method,
            sustain_threshold_6h_mm=sustain_threshold_6h_mm,
        )
        return {
            "rain24": filtered["rain24"],
            "rain12": filtered["rain12"],
            "rain6": filtered["rain6"],
            "sustained_station_ids": sorted(filtered["sustained_station_ids"]),
            "sustain_source": filtered["sustain_source"],
            "sustain_threshold_6h_mm": filtered["sustain_threshold_6h_mm"],
        }

    @mcp.tool()
    def evaluate_haihe_forecast_response_inputs(
        station_records: list[dict],
        total_station_count: int,
        rain24: dict,
        rain12: dict,
        sustained_station_ids: list[str],
        rainstorm_12h: float = DEFAULT_THRESHOLDS_MM["rainstorm_12h"],
        rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["rainstorm_24h"],
        severe_rainstorm_24h: float = DEFAULT_THRESHOLDS_MM["severe_rainstorm_24h"],
        extraordinary_24h: float = DEFAULT_THRESHOLDS_MM["extraordinary_24h"],
        typhoon_landing_impact: bool = False,
        typhoon_impact_increasing: bool = False,
    ) -> dict:
        """
        预报判定拆分-3（evaluate）：基于采样结果做 I/II/III/IV 判定检查。
        """
        checks = _forecast_evaluate_core(
            station_records=station_records,
            total_count=int(total_station_count),
            rain24={str(k): float(v) for k, v in (rain24 or {}).items()},
            rain12={str(k): float(v) for k, v in (rain12 or {}).items()},
            sustained_station_ids=set(sustained_station_ids or []),
            rainstorm_12h=rainstorm_12h,
            rainstorm_24h=rainstorm_24h,
            severe_rainstorm_24h=severe_rainstorm_24h,
            extraordinary_24h=extraordinary_24h,
            typhoon_landing_impact=typhoon_landing_impact,
            typhoon_impact_increasing=typhoon_impact_increasing,
        )
        return {"checks": checks}

    @mcp.tool()
    def report_haihe_forecast_response(
        checks: list[dict],
        start_time: str,
        basin_codes: str = DEFAULT_BASIN_CODES,
        ec_output_path: str = DEFAULT_EC_OUTPUT_PATH,
        allowed_station_levels: str = "11,12,13,16",
        sample_method: str = "nearest",
        typhoon_landing_impact: bool = False,
        typhoon_impact_increasing: bool = False,
        ec_files: dict | None = None,
        sustain_source: str = "6h",
        sustain_threshold_6h_mm: float = 0.1,
        total_station_count: int = 0,
        include_records: bool = False,
        station_records: list[dict] | None = None,
    ) -> dict:
        """
        预报判定拆分-4（report）：拼装统一输出结构（query/evidence/可选records）。
        """
        parsed_start = _parse_forecast_start_time(start_time)
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
            ec_files_paths=ec_files or {},
            sustain_source=sustain_source,
            sustain_threshold_6h_mm=sustain_threshold_6h_mm,
            total_count=int(total_station_count),
            include_records=include_records,
            station_records=station_records,
        )

    @mcp.tool()
    def evaluate_haihe_forecast_emergency_response(
        start_time: str,
        basin_codes: str = DEFAULT_BASIN_CODES,
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
    ) -> dict:
        """
        基于 EC 预报栅格与流域国家站位置，自动判定海河流域是否满足 I/II/III/IV 级应急响应条件（预报口径）。

        条件映射：
        - I 级：未来24小时特大暴雨（>= extraordinary_24h）站数占比 >= 15%，且强降水持续；
        - II 级：未来24小时大暴雨（>= severe_rainstorm_24h）站数占比 >= 15%，且强降水持续；
        - III 级：登陆台风影响继续加大，或未来12小时暴雨（>= rainstorm_12h）站数占比 >= 20%，且强降水持续；
        - IV 级：预报登陆台风将影响，或未来24小时暴雨（>= rainstorm_24h）站数占比 >= 20%，且强降水持续。
        """
        result = evaluate_haihe_forecast_emergency_response_core(
            start_time=start_time,
            basin_codes=basin_codes,
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
        result["deprecated_note"] = (
            "建议迁移到四段工具链：fetch_haihe_forecast_response_inputs -> "
            "filter_haihe_forecast_response_inputs -> "
            "evaluate_haihe_forecast_response_inputs -> "
            "report_haihe_forecast_response。"
        )
        return result

    @mcp.tool()
    def list_haihe_ec_forecast_precip_files(
        start_time: str,
        ec_output_path: str = DEFAULT_EC_OUTPUT_PATH,
        hours: str = "12,24,36,48,60,72",
    ) -> dict:
        """
        列出指定起报时次下，各预报时效（默认 12/24/36/48/60/72 小时）EC 累计降水栅格文件路径。
        某时效未落盘则为 null；不触发应急判定，仅查路径。
        """
        hrs: List[int] = []
        for x in str(hours).split(","):
            x = x.strip()
            if x.isdigit():
                hrs.append(int(x))
        if not hrs:
            hrs = list(DEFAULT_EC_FORECAST_HOURS)
        return collect_ec_forecast_precip_files(start_time, ec_output_path, tuple(hrs))

    @mcp.tool()
    def create_haihe_forecast_impact_precip_map_job(
        start_time: str,
        hours: str = "12,24,36,48,60,72",
        ec_output_path: str | None = None,
        config_path: str | None = None,
        basin_vector: str | None = None,
        draw_options: dict | None = None,
    ) -> dict:
        """
        创建海河流域"预报影响"降水专题图任务（默认启用 CMA 色标）。

        - 默认分级色标（mm）：
          无降水=#ffffffff，0-10=#a6f28eff，10-25=#3bb941ff，25-50=#61b8ffff，
          50-100=#0001fcff，100-250=#fc00f9ff，>=250=#7f0141ff。
        - 返回队列任务信息，可再用 HTTP 队列接口查询进度和产物。
        """
        from forecast_product_queue import enqueue_forecast_product_job, job_to_dict

        hrs: List[int] = []
        for x in str(hours).split(","):
            x = x.strip()
            if x.isdigit():
                hrs.append(int(x))
        hours_tuple = tuple(hrs) if hrs else None

        opts: Dict[str, Any] = {}
        if isinstance(draw_options, dict):
            opts.update(draw_options)
        opts.setdefault("color_scheme", "cma")

        job = enqueue_forecast_product_job(
            start_time=start_time,
            hours=hours_tuple,
            ec_output_path=ec_output_path,
            config_path=config_path,
            basin_vector=basin_vector,
            draw_options=opts,
        )
        return {
            "message": "已创建海河流域预报影响降水专题图任务",
            "color_scheme": opts.get("color_scheme"),
            "job": job_to_dict(job),
        }

    @mcp.tool()
    def create_haihe_observation_impact_precip_map_job(
        times: str,
        accum_hours: str = "1,6,12,24",
        basin_codes: str = DEFAULT_BASIN_CODES,
        allowed_station_levels: str = "11,12,13,16",
        config_path: str | None = None,
        basin_vector: str | None = None,
        draw_options: dict | None = None,
    ) -> dict:
        """
        创建海河流域"实况影响"降水专题图任务（默认启用 CMA 色标）。

        - 默认分级色标（mm）：
          无降水=#ffffffff，0-10=#a6f28eff，10-25=#3bb941ff，25-50=#61b8ffff，
          50-100=#0001fcff，100-250=#fc00f9ff，>=250=#7f0141ff。
        """
        from observation_product_queue import enqueue_observation_product_job, obs_job_to_dict

        hrs: List[int] = []
        for x in str(accum_hours).split(","):
            x = x.strip()
            if x.isdigit():
                hrs.append(int(x))
        accum_tuple = tuple(hrs) if hrs else None

        opts: Dict[str, Any] = {}
        if isinstance(draw_options, dict):
            opts.update(draw_options)
        opts.setdefault("color_scheme", "cma")

        job = enqueue_observation_product_job(
            times=times,
            accum_hours=accum_tuple,
            basin_codes=basin_codes,
            allowed_station_levels=allowed_station_levels,
            config_path=config_path,
            basin_vector=basin_vector,
            draw_options=opts,
        )
        return {
            "message": "已创建海河流域实况影响降水专题图任务",
            "color_scheme": opts.get("color_scheme"),
            "job": obs_job_to_dict(job),
        }

    @mcp.tool()
    def get_station_rainfall_real_img(
        beginTime: str = "",
        endTime: str = "",
        areaIds: list = None,
        interval: int = 24,
        range: str = "9",
        type: str = "0",
        isClimateImg: bool = False,
    ) -> dict:
        """
        获取各子流域分区面雨量分布图（降水实况图）。只用于展示图片，不返回雨量数值。
        仅当用户明确要求"降雨分布图""降水实况图""面雨量分布图"时调用。
        不要将此工具用于回答"下雨吗""雨量多少""天气怎么样"等常规降雨预报/实况查询。

        Args:
            beginTime: 开始时间，格式如 "2025-07-27 08:00:00"。不传则自动取当前时间前推interval小时
            endTime: 结束时间，格式如 "2025-07-28 08:00:00"。不传则自动取当前时间
            areaIds: 区域id列表，默认[6,7,8,9,10,11,12,13,14]（海河9大分区）
            interval: 间隔(单位:小时)，默认24。超过24h使用累计，如10天间隔为240
            range: 分区，默认"9"，可传"9"或"11"分区
            type: 站点类型，"0"是国家站，"1"是区域站。默认"0"
            isClimateImg: 出图的文字颜色是否黑色，默认False
        """
        import datetime as dt

        now = dt.datetime.now()

        if not endTime:
            endTime = now.strftime("%Y-%m-%d %H:00:00")
        if not beginTime:
            beginTime = (now - dt.timedelta(hours=interval)).strftime("%Y-%m-%d %H:00:00")
        if areaIds is None:
            areaIds = [6, 7, 8, 9, 10, 11, 12, 13, 14]

        url = "http://10.226.107.35:8001/openapi/meteor_img/stationRainRealImg?forceCreate=1"
        payload = {
            "beginTime": beginTime,
            "endTime": endTime,
            "areaIds": areaIds,
            "interval": interval,
            "range": range,
            "type": type,
            "isClimateImg": isClimateImg,
        }

        try:
            resp = requests.post(url, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            # 兼容多种返回格式
            if isinstance(data, dict):
                img_base64 = data.get("data") or data.get("result") or data.get("image") or data
            else:
                img_base64 = data
            return {
                "base64": img_base64 if isinstance(img_base64, str) else str(img_base64),
                "beginTime": beginTime,
                "endTime": endTime,
                "interval": interval,
                "range": range,
            }
        except Exception as e:
            return {"error": f"获取降水实况图失败: {str(e)}"}

    @mcp.tool()
    def get_effective_warning_info(include_raw: bool = False) -> dict:
        """
        查询当前仍在生效的预警信息
        当用户询问"现在有什么预警"、"当前预警"、"正在生效的预警"、"目前有哪些预警"等实时预警问题时调用。
        默认仅返回 content/eventType/department/time/severity/msgType 等问答所需字段，count 为预警条数，
        query_time/query_hour_text 为查询时刻，severity_order 给出蓝色、黄色、橙色、红色从低到高的等级顺序。
        include_raw=True 仅用于接口排查，会额外返回 raw_response/raw_data，正常问答不要开启。
        """
        return _fetch_warning_info(WARNING_EFFECTIVE_PATH, "effective", include_raw=include_raw)

    @mcp.tool()
    def get_history_warning_info(include_raw: bool = False) -> dict:
        """
        查询历史预警信息。
        当用户询问"历史预警"、"已解除预警"、"过去发布过哪些预警"等历史/解除预警问题时调用。
        默认仅返回 content/eventType/department/time/severity/msgType 等问答所需字段，count 为预警条数，
        query_time/query_hour_text 为查询时刻，severity_order 给出蓝色、黄色、橙色、红色从低到高的等级顺序。
        include_raw=True 仅用于接口排查，会额外返回 raw_response/raw_data，正常问答不要开启。
        """
        return _fetch_warning_info(WARNING_HISTORY_PATH, "history", include_raw=include_raw)

    @mcp.tool()
    def get_today_warning_summary() -> dict:
        """
        查询今日预警动态汇总。
        当用户询问"今天新发了哪些预警"、"今日发布了哪些预警"、"今天预警动态"等问题时优先调用。
        工具会查询历史预警并按 time 是否为今天筛选，不区分 msgType 状态；
        today_published_warnings 为今日全部预警记录。每条记录仅保留 content/eventType/department/time/severity/msgType。
        """
        return _fetch_today_warning_summary()

    @mcp.tool()
    def get_national_warning_info(keywords: str = "", max_items: int = 30) -> dict:
        """
        查询中央气象台/国家局预警信息。
        当用户询问国家局、中央气象台、全国、周边区域、华北、京津冀等预警信息时调用。
        keywords 为逗号分隔的区域或关键词，默认仅使用“天津”。
        用户询问“国家局和天津市发布的预警信息”或“中央气象台和天津市预警”时，
        必须传 keywords="天津"，不得自动扩大到北京、河北、华北、京津冀或全国。
        只有用户明确询问这些地区时，才传入相应关键词。
        max_items 控制最多返回记录数。返回轻量化 warnings 记录；content 为预警正文，url 为国家气象中心详情链接。
        """
        return _fetch_national_warning_info(keywords=keywords or None, max_items=max_items)

    @mcp.tool()
    def get_tianjin_wind_warning_assessment(times: str = "") -> dict:
        """
        查询天津市当前风力，并按大风预警阈值完成确定性判定。
        当用户询问“当前风力是否达到大风预警标准”“现在是否需要发布大风预警”
        “哪些区风力达标”“当前最大阵风是否达标”等实时风力阈值问题时调用。

        平均风或最大阵风任一项达到对应阈值即达标。全量有效站点用于全市结论、
        阈值对比和区域分布；仅 Station_levl 为 016（兼容接口返回的 16）的国家站
        会出现在 station_table 明细表中。times 可传 YYYYMMDDHHmmss 用于指定时次，
        不传时按服务器当前整点查询。
        """
        # 服务器系统时钟为 UTC+0；接口 times 和前端展示时间均使用天津本地时间（UTC+8）。
        tianjin_now = datetime.now(TIANJIN_TIMEZONE)
        request_time = _normalize_time_param(times) if times else tianjin_now.strftime("%Y%m%d%H0000")
        query_time = tianjin_now.strftime("%Y-%m-%d %H:%M:%S")
        try:
            records = MusicClient().get_surf_ele_in_region_by_time(
                admin_codes="120000",
                times=request_time,
            )
        except Exception as exc:
            return {
                "status": "wind_observation_api_failed",
                "query_time": query_time,
                "request_time": request_time,
                "message": f"天津市风力实况查询失败：{exc}",
                "station_table": [],
                "threshold_comparison": [],
                "area_distribution": [],
                "attention_areas": [],
                "recommendations": [],
            }

        result = _evaluate_tianjin_wind_warning(records, query_time=query_time)
        result["request_time"] = request_time
        return result

    @mcp.tool()
    def evaluate_emergency_response_by_time_range(
        start_time: str = "",
        end_time: str = "",
        source: str = "observation",
        basin_codes: str = DEFAULT_BASIN_CODES,
        allowed_station_levels: str = "11,12,13,16",
    ) -> dict:
        """
        按时间段判定海河流域应急响应事件（I/II/III/IV级）。
        基于实况站点的降雨数据，判断指定时间段内哪些时次触发了应急响应。

        功能：
        - 扫描时间段内的每个整点观测时次（02/08/14/20）
        - 对各时次自动执行 fetch → filter → evaluate → report 流水线
        - 合并返回所有触发了I/II/III/IV级响应的事件清单

        Args:
            start_time: 开始时间 "YYYY-MM-DD HH:MM:SS"，默认当前往前推24h
            end_time: 结束时间 "YYYY-MM-DD HH:MM:SS"，默认当前时间
            source: 数据来源，"observation"实况/"forecast"预报（目前支持observation）
            basin_codes: 流域代码，默认"HHLY"（海河流域），可传子流域
            allowed_station_levels: 站点等级，默认""
        """
        from datetime import datetime as _dt, timedelta as _td

        now = _dt.now()
        if not end_time:
            end_time = now.strftime("%Y-%m-%d %H:%M:%S")
        if not start_time:
            start_time = (now - _td(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

        try:
            end_dt = _dt.strptime(end_time, "%Y-%m-%d %H:%M:%S")
            start_dt = _dt.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return {"error": "时间格式错误，请使用 YYYY-MM-DD HH:MM:SS"}

        events = []
        synoptic_hours = [2, 8, 14, 20]

        # 遍历时间段内的每个完整日，取整点观测时次
        cur = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        while cur <= end_dt:
            for h in synoptic_hours:
                ts = cur.replace(hour=h)
                if ts < start_dt or ts > end_dt:
                    continue
                try:
                    ts_str = ts.strftime("%Y%m%d%H%M%S")
                    records = _observation_fetch_core(
                        basin_codes=basin_codes,
                        times=ts_str,
                        elements=DEFAULT_OBS_ELEMENTS,
                    )
                    filtered = _observation_filter_core(
                        records=records,
                        allowed_station_levels=allowed_station_levels,
                    )
                    evaluation = _observation_evaluate_core(
                        records=filtered["records"],
                        allowed_station_levels=filtered["allowed_station_levels"],
                        neighbor_km=50.0,
                        sustain_hourly_threshold_mm=0.1,
                        rainstorm_12h=30.0,
                        rainstorm_24h=50.0,
                        severe_rainstorm_24h=100.0,
                        extraordinary_24h=250.0,
                    )
                    report = _observation_report_core(
                        evaluation=evaluation,
                        basin_codes=basin_codes,
                        times=ts_str,
                        allowed_station_levels=filtered["allowed_station_levels"],
                        include_records=False,
                    )
                    lev = report.get("max_level") or evaluation.get("level")
                    if lev:
                        events.append({
                            "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                            "max_level": lev,
                            "summary": report.get("summary", ""),
                            "reached_station_count": report.get("reached_station_count", 0),
                            "total_station_count": report.get("total_station_count", 0),
                        })
                except Exception as e:
                    print(f"[应急响应] {ts} 判定失败: {e}")
                    continue
            cur += _td(days=1)

        if not events:
            return {
                "start_time": start_time,
                "end_time": end_time,
                "events": [],
                "message": f"该时段内未触发应急响应（最高未达IV级）",
            }

        max_levels = sorted(set(e["max_level"] for e in events),
                           key=lambda x: {"I": 0, "II": 1, "III": 2, "IV": 3}.get(x, 99))
        return {
            "start_time": start_time,
            "end_time": end_time,
            "max_level_in_period": max_levels[0] if max_levels else "无",
            "triggered_count": len(events),
            "events": events,
        }

    def rag_search(query: str, kb_key: str) -> dict:
        kb = _rag_find_kb_by_key(kb_key)
        if kb is None:
            return {
                "error": "unknown_kb_key",
                "kb_key": kb_key,
                "available_keys": [k["key"] for k in RAG_KNOWLEDGE_BASES],
                "contexts": [],
                "chunks": [],
                "sources": [],
                "count": 0,
            }

        request_body = _rag_build_request(kb, query)
        try:
            resp = requests.post(RAG_API_URL, json=request_body, timeout=RAG_API_TIMEOUT)
            resp.raise_for_status()
            rag_result = resp.json()
        except Exception as e:
            print(f"[RAG] 检索接口调用失败：{e}")
            return {
                "knowledge_base": kb["name"],
                "kb_key": kb["key"],
                "query": query,
                "error": "rag_api_failed",
                "contexts": [],
                "chunks": [],
                "sources": [],
                "count": 0,
            }

        contexts = _rag_extract_contexts(rag_result, max_contexts=int(request_body.get("top_k") or 5))
        chunks = _rag_contexts_to_chunks(contexts)
        sources = sorted(
            {
                str(item.get("source") or "").strip()
                for item in contexts
                if str(item.get("source") or "").strip()
            }
        )
        return {
            "knowledge_base": kb["name"],
            "kb_key": kb["key"],
            "query": query,
            "contexts": contexts,
            "chunks": chunks,
            "sources": sources,
            "count": len(contexts),
        }

    # 采用函数式注册，把知识库目录注入工具说明。
    rag_search.__doc__ = _rag_search_doc()
    mcp.tool()(rag_search)

