from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import threading
import uuid
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from wechat_tools import WeChatRPAClient


HOST = "0.0.0.0"
PORT = 18080

BASE_DIR = Path(r"C:\WechatRPA\gateway")
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
LOG_DIR = BASE_DIR / "logs"

MAX_UPLOAD_SIZE = 100 * 1024 * 1024
MAX_FILENAME_LENGTH = 180

ALLOWED_CLIENT_IPS = {
    "127.0.0.1",
    "::1",
    "10.226.245.128",
}

# 第一阶段只允许发到文件传输助手和测试群。
# 暂时不允许调用方指定正式群。
ALLOWED_TARGETS = {
    "文件传输助手",
    "【内部】天津气象台流域升级",
}

TARGET_KEY_MAP = {
    "file_transfer_assistant": "文件传输助手",
}

ALLOWED_FILE_EXTENSIONS = {
    ".doc",
    ".docx",
    ".pdf",
    ".txt",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".zip",
}

ALLOWED_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".bmp",
}

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


API_TOKEN = os.environ.get(
    "WECHAT_GATEWAY_TOKEN",
    "",
).strip()

if not API_TOKEN:
    raise RuntimeError(
        "环境变量WECHAT_GATEWAY_TOKEN不存在，"
        "拒绝启动网关。"
    )


UPLOAD_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

LOG_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


logger = logging.getLogger("wechat_gateway")
logger.setLevel(logging.INFO)

if not logger.handlers:
    file_handler = RotatingFileHandler(
        LOG_DIR / "gateway.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )

    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s"
        )
    )

    logger.addHandler(file_handler)


app = FastAPI(
    title="WeChat DMZ Gateway",
    version="1.1.0",
)

wechat_client = WeChatRPAClient(
    log_dir=r"C:\WechatRPA\logs",
    action_delay=1.5,
    maximize_wechat=True,
)

# 微信界面不能并发操作。
wechat_lock = threading.RLock()


class SendTextRequest(BaseModel):
    target: str = Field(
        min_length=1,
        max_length=100,
    )

    message: str = Field(
        min_length=1,
        max_length=5000,
    )

    send: bool = True


def verify_request(
    request: Request,
    authorization: str | None,
) -> None:
    client_ip = (
        request.client.host
        if request.client
        else ""
    )

    if client_ip not in ALLOWED_CLIENT_IPS:
        logger.warning(
            "拒绝来源IP：%s",
            client_ip,
        )

        raise HTTPException(
            status_code=403,
            detail="来源IP不允许访问",
        )

    expected = f"Bearer {API_TOKEN}"

    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="缺少Authorization",
        )

    if not secrets.compare_digest(
        authorization,
        expected,
    ):
        logger.warning(
            "Token验证失败，来源IP：%s",
            client_ip,
        )

        raise HTTPException(
            status_code=401,
            detail="Token无效",
        )


def validate_target(target: str) -> str:
    normalized = target.strip()

    if normalized not in ALLOWED_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"目标不在白名单中：{normalized}"
            ),
        )

    return normalized


def resolve_target_key(target_key: str) -> str:
    normalized_key = target_key.strip()

    target = TARGET_KEY_MAP.get(
        normalized_key
    )

    if not target:
        # 允许调用方直接传群名（白名单内），便于 send-file 用配置群名直接调用。
        target = normalized_key

    return validate_target(target)


def resolve_upload_filename(
    filename_b64: str | None,
    fallback_filename: str | None,
) -> str:
    """
    优先从纯ASCII的Base64字段恢复UTF-8文件名。

    multipart自带的filename在Windows curl/Python组合下可能被错误解码，
    因此中文文件名不能依赖UploadFile.filename。
    """

    filename: str | None = None

    if filename_b64:
        try:
            raw_name = base64.b64decode(
                filename_b64.strip(),
                validate=True,
            )
            filename = raw_name.decode(
                "utf-8",
                errors="strict",
            )
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid filename_b64: {exc}",
            ) from exc

    if not filename:
        filename = fallback_filename or "upload.bin"

    # 同时处理正斜杠、反斜杠，防止目录穿越或传入完整路径。
    filename = filename.replace("\\", "/")
    filename = filename.rsplit("/", 1)[-1].strip()

    # 清除Windows禁止字符和控制字符。
    filename = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]',
        "_",
        filename,
    )

    # Windows不允许文件名以空格或句点结尾。
    filename = filename.rstrip(" .")

    if not filename or filename in {".", ".."}:
        raise HTTPException(
            status_code=400,
            detail="Invalid upload filename",
        )

    suffix = Path(filename).suffix
    stem = Path(filename).stem

    if stem.upper() in WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
        filename = f"{stem}{suffix}"

    if len(filename) > MAX_FILENAME_LENGTH:
        keep_length = (
            MAX_FILENAME_LENGTH
            - len(suffix)
        )

        filename = (
            f"{stem[:max(1, keep_length)]}"
            f"{suffix}"
        )

    return filename


async def save_upload(
    upload: UploadFile,
    allowed_extensions: set[str],
    filename_b64: str | None = None,
) -> dict:
    original_name = resolve_upload_filename(
        filename_b64=filename_b64,
        fallback_filename=upload.filename,
    )

    suffix = Path(
        original_name
    ).suffix.lower()

    if suffix not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type: {suffix}"
            ),
        )

    day_folder = (
        UPLOAD_DIR
        / datetime.now().strftime("%Y%m%d")
    )

    upload_id = uuid.uuid4().hex

    task_folder = (
        day_folder
        / upload_id
    )

    task_folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 必须使用恢复后的UTF-8原始文件名，微信中才会显示正确。
    target_path = (
        task_folder
        / original_name
    )

    temporary_path = (
        task_folder
        / f"{original_name}.part"
    )

    total_size = 0
    digest = hashlib.sha256()

    try:
        with temporary_path.open("wb") as output:
            while True:
                chunk = await upload.read(
                    1024 * 1024
                )

                if not chunk:
                    break

                total_size += len(chunk)

                if total_size > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail="File exceeds 100 MB.",
                    )

                output.write(chunk)
                digest.update(chunk)

        if total_size <= 0:
            raise HTTPException(
                status_code=400,
                detail="Uploaded file is empty.",
            )

        temporary_path.replace(
            target_path
        )

    except Exception:
        temporary_path.unlink(
            missing_ok=True
        )

        target_path.unlink(
            missing_ok=True
        )

        try:
            task_folder.rmdir()
        except OSError:
            pass

        raise

    finally:
        await upload.close()

    return {
        "original_name": original_name,
        "stored_name": (
            f"{upload_id}/{original_name}"
        ),
        "path": target_path,
        "size": total_size,
        "sha256": digest.hexdigest(),
    }


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "status": "healthy",
        "service": "wechat_gateway",
        "version": "1.1.0",
        "time": datetime.now().isoformat(
            timespec="seconds"
        ),
    }


@app.post("/api/v1/send-text")
def send_text(
    body: SendTextRequest,
    request: Request,
    authorization: str | None = Header(
        default=None
    ),
) -> dict:
    verify_request(
        request,
        authorization,
    )

    target = validate_target(
        body.target
    )

    logger.info(
        "收到文本任务 | client=%s | target=%s | send=%s",
        request.client.host,
        target,
        body.send,
    )

    with wechat_lock:
        result = wechat_client.send_text(
            target=target,
            message=body.message,
            send=body.send,
        )

    return {
        "ok": result.ok,
        "result": result.to_dict(),
    }


@app.post("/api/v1/send-file")
async def send_file(
    request: Request,
    target_key: str = Form(...),
    send: bool = Form(True),
    filename_b64: str | None = Form(
        default=None
    ),
    file: UploadFile = File(...),
    authorization: str | None = Header(
        default=None
    ),
) -> dict:
    verify_request(
        request,
        authorization,
    )

    target = resolve_target_key(
        target_key
    )

    saved = await save_upload(
        upload=file,
        allowed_extensions=ALLOWED_FILE_EXTENSIONS,
        filename_b64=filename_b64,
    )

    logger.info(
        (
            "收到文件任务 | client=%s | target=%s "
            "| name=%s | size=%s | sha256=%s | send=%s"
        ),
        request.client.host,
        target,
        saved["original_name"],
        saved["size"],
        saved["sha256"],
        send,
    )

    with wechat_lock:
        result = wechat_client.send_file(
            target=target,
            file_path=saved["path"],
            send=send,
        )

    return {
        "ok": result.ok,
        "upload": {
            "original_name": saved["original_name"],
            "stored_name": saved["stored_name"],
            "size": saved["size"],
            "sha256": saved["sha256"],
        },
        "result": result.to_dict(),
    }


@app.post("/api/v1/send-image")
async def send_image(
    request: Request,
    target_key: str = Form(...),
    send: bool = Form(True),
    filename_b64: str | None = Form(
        default=None
    ),
    file: UploadFile = File(...),
    authorization: str | None = Header(
        default=None
    ),
) -> dict:
    verify_request(
        request,
        authorization,
    )

    target = resolve_target_key(
        target_key
    )

    saved = await save_upload(
        upload=file,
        allowed_extensions=ALLOWED_IMAGE_EXTENSIONS,
        filename_b64=filename_b64,
    )

    logger.info(
        (
            "收到图片任务 | client=%s | target=%s "
            "| name=%s | size=%s | sha256=%s | send=%s"
        ),
        request.client.host,
        target,
        saved["original_name"],
        saved["size"],
        saved["sha256"],
        send,
    )

    with wechat_lock:
        result = wechat_client.send_image(
            target=target,
            image_path=saved["path"],
            send=send,
        )

    return {
        "ok": result.ok,
        "upload": {
            "original_name": saved["original_name"],
            "stored_name": saved["stored_name"],
            "size": saved["size"],
            "sha256": saved["sha256"],
        },
        "result": result.to_dict(),
    }