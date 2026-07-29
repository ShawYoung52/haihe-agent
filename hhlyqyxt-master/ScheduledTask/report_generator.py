"""天河报告接口调用。
应急响应 I-IV 级触发时，调用天河报告接口生成海河流域气象公报。
"""
import logging

import requests

logger = logging.getLogger(__name__)

REPORT_API_URL = "http://10.226.188.156:8000/api/report/generate"
REPORT_TEMPLATE = "haihe_weather_bulletin"
DEFAULT_TIMEOUT = 30


def trigger_weather_bulletin_report(
    response_level: int,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> bool:
    """I-IV 级应急响应时，调用天河报告接口生成海河流域气象公报。

    Args:
        response_level: 1=I级, 2=II级, 3=III级, 4=IV级, 0=无预警。
        timeout: HTTP 请求超时秒数（默认 30s）。

    Returns:
        True 表示调用成功（HTTP < 400），False 表示跳过或失败。
        任何异常均被捕获记 WARNING，不抛给调用方。
    """
    if response_level < 1:
        return False

    try:
        resp = requests.post(
            REPORT_API_URL,
            json={"template": REPORT_TEMPLATE},
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        logger.warning("天河报告接口超时（%ds），应急%d级", timeout, response_level)
        return False
    except requests.exceptions.RequestException as e:
        logger.warning("天河报告接口调用失败：%s，应急%d级", e, response_level)
        return False

    if resp.status_code >= 400:
        logger.warning("天河报告接口返回非成功状态码：%s，body=%s", resp.status_code, resp.text[:200])
        return False
    logger.info("天河报告触发成功（应急%d级），status=%s", response_level, resp.status_code)
    return True
