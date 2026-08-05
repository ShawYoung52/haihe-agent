from __future__ import annotations

import ctypes
import io
import time
from pathlib import Path

import win32clipboard
import win32con
from PIL import Image


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", ctypes.c_uint),
        ("x", ctypes.c_long),
        ("y", ctypes.c_long),
        ("fNC", ctypes.c_int),
        ("fWide", ctypes.c_bool),
    ]


def _open_clipboard(max_attempts: int = 10, delay: float = 0.2) -> None:
    for attempt in range(max_attempts):
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception:
            if attempt == max_attempts - 1:
                raise
            time.sleep(delay)


def copy_text(text: str) -> None:
    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


def copy_files(file_paths: list[Path]) -> None:
    if not file_paths:
        raise ValueError("file_paths不能为空")

    paths = [str(p.resolve()).replace("/", "\\") for p in file_paths]
    drop = DROPFILES()
    drop.pFiles = ctypes.sizeof(DROPFILES)
    drop.x = 0
    drop.y = 0
    drop.fNC = 0
    drop.fWide = True

    path_data = ("\0".join(paths) + "\0\0").encode("utf-16le")
    data = bytes(drop) + path_data

    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
    finally:
        win32clipboard.CloseClipboard()


def copy_image(image_path: Path) -> None:
    with Image.open(image_path) as source:
        image = source.convert("RGB")
        with io.BytesIO() as buffer:
            image.save(buffer, format="BMP")
            dib_data = buffer.getvalue()[14:]

    _open_clipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib_data)
    finally:
        win32clipboard.CloseClipboard()
