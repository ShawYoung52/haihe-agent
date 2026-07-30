"""天河报告接口调用。
应急响应 I-IV 级触发时，调用天河报告接口生成海河流域气象公报。
"""
import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

REPORT_API_URL = "http://10.226.188.156:8001/api/report/generate"
REPORT_TEMPLATE = "haihe_weather_bulletin"
DEFAULT_TIMEOUT = 30


def trigger_weather_bulletin_report(
    response_level: int,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[dict]:
    """I-IV 级应急响应时，调用天河报告接口生成海河流域气象公报。

    Args:
        response_level: 1=I级, 2=II级, 3=III级, 4=IV级, 0=无预警。
        timeout: HTTP 请求超时秒数（默认 30s）。

    Returns:
        成功时返回 {"docx_url": ..., "pdf_url": ...}；跳过或失败返回 None。
        任何异常均被捕获记 WARNING，不抛给调用方。

    天河接口成功响应示例：
        {
            "template_id": "haihe_weather_bulletin",
            "docx_url": "http://192.168.0.147:8000/files/docs/...docx",
            "pdf_url": "http://192.168.0.147:8000/files/docs/...pdf"
        }
    """
    if response_level < 1:
        return None

    try:
        resp = requests.post(
            REPORT_API_URL,
            json={"template": REPORT_TEMPLATE},
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        logger.warning("天河报告接口超时（%ds），应急%d级", timeout, response_level)
        return None
    except requests.exceptions.RequestException as e:
        logger.warning("天河报告接口调用失败：%s，应急%d级", e, response_level)
        return None

    if resp.status_code >= 400:
        logger.warning(
            "天河报告接口返回非成功状态码：%s，body=%s",
            resp.status_code, resp.text[:200],
        )
        return None

    try:
        payload = resp.json()
    except ValueError as e:
        logger.warning("天河报告接口响应非 JSON：%s，body=%s", e, resp.text[:200])
        return None

    docx_url = payload.get("docx_url")
    pdf_url = payload.get("pdf_url")
    logger.info(
        "天河报告触发成功（应急%d级），docx=%s, pdf=%s",
        response_level, docx_url, pdf_url,
    )
    return {"docx_url": docx_url, "pdf_url": pdf_url}
