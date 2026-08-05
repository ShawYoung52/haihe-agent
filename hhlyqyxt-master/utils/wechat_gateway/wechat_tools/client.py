from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import pyautogui
from pywechat.WechatTools import Tools

from .clipboard_ops import copy_files, copy_image, copy_text
from .config import PRODUCT_ROUTES


@dataclass
class SendResult:
    ok: bool
    target: str
    kind: str
    detail: str
    path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class WeChatRPAClient:
    """DMZ服务器本地微信RPA工具。所有微信操作严格串行。"""

    _send_lock = threading.RLock()

    def __init__(
        self,
        *,
        log_dir: str | Path = r"C:\WechatRPA\logs",
        action_delay: float = 1.5,
        maximize_wechat: bool = True,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.action_delay = max(float(action_delay), 0.5)
        self.maximize_wechat = maximize_wechat
        pyautogui.FAILSAFE = False

        self.logger = logging.getLogger("wechat_rpa")
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            handler = logging.FileHandler(
                self.log_dir / "wechat_rpa.log",
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
            )
            self.logger.addHandler(handler)

    def _open_chat(self, target: str):
        if not target or not target.strip():
            raise ValueError("target不能为空")
        return Tools.open_dialog_window(
            friend=target.strip(),
            search_pages=0,
            is_maximize=self.maximize_wechat,
        )

    @staticmethod
    def _clear_input(edit_area) -> None:
        edit_area.click_input()
        pyautogui.hotkey("ctrl", "a", _pause=False)
        pyautogui.press("backspace", _pause=False)
        time.sleep(0.4)

    @staticmethod
    def _paste(edit_area, *, send: bool, wait_after_paste: float) -> None:
        edit_area.click_input()
        pyautogui.hotkey("ctrl", "v", _pause=False)
        time.sleep(wait_after_paste)
        if send:
            pyautogui.hotkey("alt", "s", _pause=False)
            time.sleep(1.2)

    @staticmethod
    def _validate_file(
        file_path: str | Path,
        *,
        allowed_suffixes: set[str] | None = None,
    ) -> Path:
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")
        if path.stat().st_size <= 0:
            raise ValueError(f"文件为空：{path}")
        if allowed_suffixes and path.suffix.lower() not in allowed_suffixes:
            raise ValueError(
                f"不支持的文件类型：{path.suffix}，允许：{sorted(allowed_suffixes)}"
            )
        return path

    def _record(self, result: SendResult) -> SendResult:
        self.logger.info(json.dumps(result.to_dict(), ensure_ascii=False))
        return result

    def send_text(self, target: str, message: str, *, send: bool = True) -> SendResult:
        if not message:
            raise ValueError("message不能为空")
        with self._send_lock:
            try:
                edit_area, _ = self._open_chat(target)
                self._clear_input(edit_area)
                copy_text(message)
                self._paste(edit_area, send=send, wait_after_paste=0.8)
                return self._record(SendResult(
                    True, target, "text",
                    "文本发送成功" if send else "文本已粘贴，未发送",
                ))
            except Exception as exc:
                return self._record(SendResult(
                    False, target, "text", f"{type(exc).__name__}: {exc}"
                ))

    def send_file(
        self,
        target: str,
        file_path: str | Path,
        *,
        send: bool = True,
    ) -> SendResult:
        path = self._validate_file(file_path)
        with self._send_lock:
            try:
                edit_area, _ = self._open_chat(target)
                self._clear_input(edit_area)
                copy_files([path])
                size_mb = path.stat().st_size / 1024 / 1024
                wait_seconds = min(max(3.0 + size_mb * 0.3, 3.5), 20.0)
                self._paste(edit_area, send=send, wait_after_paste=wait_seconds)
                return self._record(SendResult(
                    True, target, "file",
                    "文件发送成功" if send else "文件已粘贴，未发送",
                    str(path),
                ))
            except Exception as exc:
                return self._record(SendResult(
                    False, target, "file", f"{type(exc).__name__}: {exc}", str(path)
                ))

    def send_image(
        self,
        target: str,
        image_path: str | Path,
        *,
        send: bool = True,
    ) -> SendResult:
        path = self._validate_file(
            image_path,
            allowed_suffixes={".png", ".jpg", ".jpeg", ".bmp"},
        )
        with self._send_lock:
            try:
                edit_area, _ = self._open_chat(target)
                self._clear_input(edit_area)
                copy_image(path)
                size_mb = path.stat().st_size / 1024 / 1024
                wait_seconds = min(max(2.0 + size_mb * 0.1, 2.5), 10.0)
                self._paste(edit_area, send=send, wait_after_paste=wait_seconds)
                return self._record(SendResult(
                    True, target, "image",
                    "原生图片发送成功" if send else "原生图片已粘贴，未发送",
                    str(path),
                ))
            except Exception as exc:
                return self._record(SendResult(
                    False, target, "image", f"{type(exc).__name__}: {exc}", str(path)
                ))

    def send_package(
        self,
        target: str,
        *,
        text: str | None = None,
        files: Iterable[str | Path] | None = None,
        images: Iterable[str | Path] | None = None,
    ) -> list[SendResult]:
        """向一个目标按文字、文件、图片顺序发送。"""
        results: list[SendResult] = []
        if text:
            results.append(self.send_text(target, text, send=True))
            time.sleep(self.action_delay)
        for path in files or []:
            results.append(self.send_file(target, path, send=True))
            time.sleep(self.action_delay)
        for path in images or []:
            results.append(self.send_image(target, path, send=True))
            time.sleep(self.action_delay)
        return results

    def send_to_targets(
        self,
        targets: Iterable[str],
        *,
        text: str | None = None,
        files: Iterable[str | Path] | None = None,
        images: Iterable[str | Path] | None = None,
        continue_on_error: bool = True,
    ) -> list[SendResult]:
        """依次向多个目标发送，禁止并发。"""
        results: list[SendResult] = []
        file_list = list(files or [])
        image_list = list(images or [])

        for target in targets:
            target_results = self.send_package(
                target,
                text=text,
                files=file_list,
                images=image_list,
            )
            results.extend(target_results)
            if not continue_on_error and any(not r.ok for r in target_results):
                break
            time.sleep(self.action_delay)
        return results

    def publish_product(
        self,
        product_type: str,
        *,
        text: str | None = None,
        files: Iterable[str | Path] | None = None,
        images: Iterable[str | Path] | None = None,
        dry_run: bool = True,
        continue_on_error: bool = True,
    ) -> dict:
        """按产品类型自动选择群。dry_run=True时仅生成计划。"""
        if product_type not in PRODUCT_ROUTES:
            raise ValueError(
                f"未知产品类型：{product_type}，可选：{list(PRODUCT_ROUTES)}"
            )

        targets = list(PRODUCT_ROUTES[product_type])
        file_list = [str(self._validate_file(p)) for p in (files or [])]
        image_list = [
            str(self._validate_file(
                p,
                allowed_suffixes={".png", ".jpg", ".jpeg", ".bmp"},
            ))
            for p in (images or [])
        ]

        plan = {
            "product_type": product_type,
            "targets": targets,
            "text": text,
            "files": file_list,
            "images": image_list,
            "dry_run": dry_run,
        }

        if dry_run:
            self.logger.info("DRY_RUN | %s", json.dumps(plan, ensure_ascii=False))
            return {"ok": True, "plan": plan, "results": []}

        results = self.send_to_targets(
            targets,
            text=text,
            files=file_list,
            images=image_list,
            continue_on_error=continue_on_error,
        )
        return {
            "ok": all(r.ok for r in results),
            "plan": plan,
            "results": [r.to_dict() for r in results],
        }
