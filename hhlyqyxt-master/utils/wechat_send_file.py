"""微信发送文件到群（可插拔契约）。

通过 HTTP 调用微信 DMZ 网关（跑在 Windows 服务器，微信已登录）发送话术与
报告文件。调用方 `ScheduledTask/call_respond.py:_send_to_group` 按
`send_file(group, file_path, caption) -> bool` 契约调用，无需改动。

配置（环境变量）：
    WECHAT_GATEWAY_URL    网关 base URL，默认 http://127.0.0.1:8000
    WECHAT_GATEWAY_TOKEN  网关 token（Authorization: Bearer <token>）
"""
import logging
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

DEFAULT_GATEWAY_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 30


def _gateway_url() -> str:
    return os.environ.get("WECHAT_GATEWAY_URL", DEFAULT_GATEWAY_URL).strip().rstrip("/")


def _gateway_token() -> str:
    return os.environ.get("WECHAT_GATEWAY_TOKEN", "").strip()


def _headers() -> dict:
    headers = {"Authorization": f"Bearer {_gateway_token()}"}
    return headers


def send_file(group: str, file_path: str, caption: str) -> bool:
    """发送文件到微信群（经微信 DMZ 网关）。

    group=群名，file_path=本地文件路径，caption=附带话术。

    先 POST send-text 发话术，再 POST send-file 上传报告文件。任一步失败都
    记日志并返回 False，异常不抛给调用方（调用方已按是否成功处理）。
    """
    base = _gateway_url()
    text_url = f"{base}/api/v1/send-text"
    file_url = f"{base}/api/v1/send-file"
    headers = _headers()
    filename = Path(file_path).name

    try:
        _post_text(text_url, headers, group, caption)
    except Exception as e:
        logger.warning("send_text 失败：group=%s url=%s err=%s", group, text_url, e)
        return False

    try:
        _post_file(file_url, headers, group, file_path, filename)
    except Exception as e:
        logger.warning("send_file 失败：group=%s url=%s file=%s err=%s", group, file_url, filename, e)
        return False

    return True


def _check_gateway_ok(resp, what: str) -> None:
    """HTTP 状态码 OK 后，再校验网关 JSON 的 ok 字段。

    微信 DMZ 网关发送失败时返回 HTTP 200 + {"ok": false, "result": {...}}
    （如微信未登录、目标未找到）。只查状态码会误判为成功 → 告警没送达却标
    sent。ok 为 False 时 raise，使 send_file 返回 False。
    """
    if resp.status_code >= 400:
        raise RuntimeError(f"{what} HTTP {resp.status_code}: {resp.text[:200]}")
    json_fn = getattr(resp, "json", None)
    if json_fn is None:
        return
    try:
        payload = json_fn()
    except ValueError:
        # 无 JSON 或非 JSON 响应，视为成功（网关可能只回状态码）。
        return
    if not isinstance(payload, dict):
        return
    if payload.get("ok") is False:
        detail = payload.get("result")
        raise RuntimeError(f"{what} 网关返回 ok=false: {detail!r}")


def _post_text(url: str, headers: dict, group: str, caption: str) -> None:
    resp = requests.post(
        url,
        json={"target": group, "message": caption, "send": True},
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    _check_gateway_ok(resp, "send-text")


def _post_file(url: str, headers: dict, group: str, file_path: str, filename: str) -> None:
    with open(file_path, "rb") as fh:
        resp = requests.post(
            url,
            files={"file": (filename, fh)},
            data={"target_key": group, "send": "true"},
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
        )
    _check_gateway_ok(resp, "send-file")