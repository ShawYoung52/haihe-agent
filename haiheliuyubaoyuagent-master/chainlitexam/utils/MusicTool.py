"""
MUSIC (天擎) 接口客户端，用于查询气象站点降雨数据。
改造自 hhlyqyxt 项目，移除了本场景不需要的 EC 预报、应急响应判定等逻辑。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests


BUILTIN_MUSIC_CONFIG = {
    "service_ip": "10.226.90.120",
    "service_node_id": "NMIC_MUSIC_CMADAAS",
    "user_id": "BETJ_QXT_LYGXPT",
    "password": "Qxtly@2022ww",
    "timeout": 120,
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
            raise ValueError("请在环境变量或 BUILTIN_MUSIC_CONFIG 中填写天擎 MUSIC_USER_ID 和 MUSIC_PASSWORD")
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

        url = f"{self.config.base_url}?{urlencode(query, safe=':,[]()')}"

        connect_timeout = float(os.getenv("MUSIC_CONNECT_TIMEOUT", "5"))
        read_timeout = float(os.getenv("MUSIC_READ_TIMEOUT", str(self.config.timeout)))
        timeout = (connect_timeout, read_timeout)

        max_retries = int(os.getenv("MUSIC_MAX_RETRIES", "2"))
        base_backoff = float(os.getenv("MUSIC_RETRY_BACKOFF_SEC", "1.0"))

        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                print("请求路径:", url)
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
            raise MusicApiError(f"返回中没有 DS 字段: {json.dumps(payload, ensure_ascii=False)[:500]}")
        if isinstance(ds, list):
            return ds
        raise MusicApiError(f"DS 不是列表结构: {type(ds)}")

    def stat_surf_pre_in_basin_new(
        self,
        basin_codes: str,
        timeRange: str,
        elements: str = "Lat,Lon,Station_Id_C,City,Station_Name,Cnty,Province,Town",
        ele_value_ranges: Optional[str] = None,
        order_by: Optional[str] = None,
        limit_cnt: Optional[int] = None,
        data_province_id: Optional[str] = None,
        staLevels: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.call_api(
            "statSurfPreInBasin",
            elements=elements,
            timeRange=timeRange,
            basinCodes=basin_codes,
            eleValueRanges=ele_value_ranges,
            orderBy=order_by,
            limitCnt=limit_cnt,
            dataProvinceId=data_province_id,
            staLevels=staLevels,
        )