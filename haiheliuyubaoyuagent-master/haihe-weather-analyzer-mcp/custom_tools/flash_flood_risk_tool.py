"""山洪风险 MCP 工具。

数据产品：TJ_MDRWTFLD_REGI_DSR 海河区域山洪动态阈值计算产品。
默认查询最近 6 小时产品文件；优先使用 getSevpFileByTimeRange。
"""
from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from haihe_mcp_tools import MusicClient, MusicConfig


FLASH_FLOOD_DATA_CODE = "TJ_MDRWTFLD_REGI_DSR"
FLASH_FLOOD_PRODUCT_NAME = "海河区域山洪动态阈值计算产品"

_RISK_LEVEL_ORDER = {
    "红色": 4,
    "橙色": 3,
    "黄色": 2,
    "蓝色": 1,
    "高风险": 4,
    "较高风险": 3,
    "中风险": 2,
    "低风险": 1,
    "无风险": 0,
}
_RISK_WORDS = ("山洪", "风险", "预警", "危险", "告警", "红色", "橙色", "黄色", "蓝色", "高风险", "中风险", "低风险")
_SAFE_MAX_READ_BYTES = int(os.getenv("FLASH_FLOOD_MAX_READ_BYTES", "10485760"))


def _time_range(hours: int = 6) -> tuple[str, str, str]:
    now = datetime.now()
    start = now - timedelta(hours=max(int(hours or 6), 1))
    start_s = start.strftime("%Y%m%d%H%M%S")
    end_s = now.strftime("%Y%m%d%H%M%S")
    readable = f"{start:%Y-%m-%d %H:%M:%S} ~ {now:%Y-%m-%d %H:%M:%S}"
    return f"[{start_s},{end_s}]", readable, now.strftime("%Y-%m-%d %H:%M:%S")


def _no_data_payload(time_range: str, readable: str, message: str, debug_reason: str = "") -> dict:
    return {
        "status": "no_data",
        "query_type": "flash_flood_risk",
        "product_code": FLASH_FLOOD_DATA_CODE,
        "product_name": FLASH_FLOOD_PRODUCT_NAME,
        "time_range": time_range,
        "time_range_readable": readable,
        "files": [],
        "latest_file": None,
        "risk_status": "unknown",
        "risk_level": "无法判定",
        "risk_count": 0,
        "message": message,
        "debug_reason": debug_reason[:300] if debug_reason else "",
    }


def _pick(record: dict, *keys: str) -> Any:
    lower_map = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        if key in record and record[key] not in (None, "", "None"):
            return record[key]
        lk = key.lower()
        if lk in lower_map and lower_map[lk] not in (None, "", "None"):
            return lower_map[lk]
    return None


def _normalize_file_record(record: dict) -> dict:
    file_name = _pick(record, "fileName", "file_name", "FILE_NAME", "name", "Name", "filename")
    file_path = _pick(record, "filePath", "file_path", "FILE_PATH", "path", "Path", "url", "URL", "fileUrl", "fileURL")
    data_time = _pick(record, "Datetime", "datetime", "dataTime", "data_time", "time", "Time", "validTime", "valid_time")
    size = _pick(record, "fileSize", "file_size", "size", "Size")
    return {
        "file_name": str(file_name or "").strip(),
        "file_path": str(file_path or "").strip(),
        "data_time": str(data_time or "").strip(),
        "file_size": size,
        "raw": record,
    }


def _sort_files(files: list[dict]) -> list[dict]:
    def key(item: dict) -> str:
        return str(item.get("data_time") or item.get("file_name") or item.get("file_path") or "")
    return sorted(files, key=key, reverse=True)


def _risk_level_from_text(text: str) -> tuple[str, int]:
    if not text:
        return "无法判定", -1
    best = ("无法判定", -1)
    for name, score in _RISK_LEVEL_ORDER.items():
        if name in text and score > best[1]:
            best = (name, score)
    # 兼容英文或文件名编码
    upper = text.upper()
    english = [("RED", "红色", 4), ("ORANGE", "橙色", 3), ("YELLOW", "黄色", 2), ("BLUE", "蓝色", 1), ("HIGH", "高风险", 4), ("MEDIUM", "中风险", 2), ("LOW", "低风险", 1)]
    for token, label, score in english:
        if token in upper and score > best[1]:
            best = (label, score)
    return best


def _read_text_file(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        return ""
    if p.stat().st_size > _SAFE_MAX_READ_BYTES:
        return ""
    suffix = p.suffix.lower()
    if suffix not in {".json", ".geojson", ".csv", ".txt", ".xml", ".dat"}:
        return ""
    raw = p.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return raw.decode(enc, errors="ignore")
        except Exception:
            continue
    return ""


def _count_risk_items_from_json(obj: Any) -> tuple[int, list[str]]:
    count = 0
    areas: list[str] = []

    def walk(x: Any):
        nonlocal count
        if isinstance(x, dict):
            text = " ".join(str(v) for v in x.values() if not isinstance(v, (dict, list)))
            level, score = _risk_level_from_text(text)
            if score > 0:
                count += 1
                name = _pick(x, "name", "area", "region", "county", "town", "站名", "区域", "区县", "乡镇")
                if name:
                    areas.append(str(name))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return count, sorted(set(areas))[:20]


def _analyze_file_content(file_path: str) -> dict:
    text = _read_text_file(file_path)
    if not text:
        return {"parsed": False, "risk_count": 0, "risk_level": "无法判定", "risk_areas": []}

    level, score = _risk_level_from_text(text)
    risk_count = 0
    risk_areas: list[str] = []

    try:
        obj = json.loads(text)
        risk_count, risk_areas = _count_risk_items_from_json(obj)
    except Exception:
        # CSV/文本：按行粗略统计包含风险词的行。
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        risk_lines = [line for line in lines if any(w in line for w in _RISK_WORDS)]
        risk_count = len(risk_lines)
        try:
            sample = text[:5000]
            dialect = csv.Sniffer().sniff(sample)
            reader = csv.DictReader(sample.splitlines(), dialect=dialect)
            for row in reader:
                row_text = " ".join(str(v) for v in row.values())
                _, row_score = _risk_level_from_text(row_text)
                if row_score > 0:
                    name = _pick(row, "name", "area", "region", "county", "town", "站名", "区域", "区县", "乡镇")
                    if name:
                        risk_areas.append(str(name))
        except Exception:
            pass

    if score <= 0 and risk_count > 0:
        level = "有风险"
    return {
        "parsed": True,
        "risk_count": risk_count,
        "risk_level": level,
        "risk_areas": sorted(set(risk_areas))[:20],
    }


def _infer_from_file_metadata(files: list[dict]) -> dict:
    if not files:
        return {"risk_status": "unknown", "risk_level": "无法判定", "risk_count": 0, "risk_areas": [], "parsed_file": False}

    latest = files[0]
    text = " ".join(str(latest.get(k) or "") for k in ("file_name", "file_path", "data_time"))
    level, score = _risk_level_from_text(text)
    if score > 0:
        return {"risk_status": "risk_found", "risk_level": level, "risk_count": 1, "risk_areas": [], "parsed_file": False}

    file_path = str(latest.get("file_path") or "").strip()
    parsed = _analyze_file_content(file_path) if file_path.startswith("/") else {"parsed": False, "risk_count": 0, "risk_level": "无法判定", "risk_areas": []}
    if parsed.get("parsed"):
        risk_count = int(parsed.get("risk_count") or 0)
        level = str(parsed.get("risk_level") or "无法判定")
        if risk_count > 0 or level not in {"无法判定", "无风险"}:
            return {"risk_status": "risk_found", "risk_level": level, "risk_count": risk_count, "risk_areas": parsed.get("risk_areas") or [], "parsed_file": True}
        return {"risk_status": "no_risk_found", "risk_level": "无明显风险", "risk_count": 0, "risk_areas": [], "parsed_file": True}

    return {"risk_status": "product_found_unparsed", "risk_level": "无法从文件元数据直接判定", "risk_count": 0, "risk_areas": [], "parsed_file": False}


def register_flash_flood_risk_tool(mcp: FastMCP) -> None:
    @mcp.tool()
    def query_flash_flood_risk(hours: int = 6) -> dict:
        """查询海河区域山洪动态阈值产品，判断是否存在山洪风险。"""
        time_range, readable, query_time = _time_range(hours)
        try:
            client = MusicClient(MusicConfig())
            rows = client.call_api(
                "getSevpFileByTimeRange",
                dataCode=FLASH_FLOOD_DATA_CODE,
                timeRange=time_range,
            )
        except Exception as exc:
            return _no_data_payload(time_range, readable, "山洪风险产品查询失败，请稍后重试。", str(exc))

        files = _sort_files([_normalize_file_record(r) for r in rows if isinstance(r, dict)])
        if not files:
            return _no_data_payload(time_range, readable, "最近时段未查询到山洪动态阈值产品文件。", "empty_files")

        risk = _infer_from_file_metadata(files)
        status = risk.get("risk_status") or "unknown"
        if status == "risk_found":
            message = "已查询到山洪风险相关信号。"
        elif status == "no_risk_found":
            message = "当前产品内容未识别到明显山洪风险信号。"
        else:
            message = "已查询到山洪动态阈值产品，但当前仅能获取文件元数据，无法直接判定是否有山洪风险。"

        return {
            "status": "ok",
            "query_type": "flash_flood_risk",
            "product_code": FLASH_FLOOD_DATA_CODE,
            "product_name": FLASH_FLOOD_PRODUCT_NAME,
            "interface_id": "getSevpFileByTimeRange",
            "time_range": time_range,
            "time_range_readable": readable,
            "query_time": query_time,
            "file_count": len(files),
            "latest_file": files[0],
            "files": files[:10],
            "risk_status": status,
            "risk_level": risk.get("risk_level") or "无法判定",
            "risk_count": risk.get("risk_count") or 0,
            "risk_areas": risk.get("risk_areas") or [],
            "parsed_file": bool(risk.get("parsed_file")),
            "message": message,
        }
