#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信自动发送消息（可导入模块版）
保持与 test.py 完全一致的核心逻辑，仅增加线程锁和 COM 初始化。
"""

import uiautomation as auto
import pyperclip
import pyautogui
from PIL import ImageGrab
import numpy as np
import time
import random
import sys
import threading
import ctypes
from ctypes import wintypes

_user32 = ctypes.windll.user32

# 声明 Windows API 参数类型，避免 64 位句柄截断
_user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.INT, wintypes.UINT]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, wintypes.INT]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_user32.AttachThreadInput.restype = wintypes.BOOL
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL

# 强制 Windows 控制台使用 UTF-8，避免中文日志显示为 ?
if sys.platform == "win32":
    try:
        # 设置每显示器 DPI 感知（v2），解决 125% 等缩放导致坐标偏移
        _user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            _user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def _bring_to_foreground(hwnd):
    """把窗口恢复到前台，确保 pyautogui 操作落在微信上。"""
    if not hwnd:
        return
    try:
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, 9)
            time.sleep(0.3)

        _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)

        h_fore = _user32.GetForegroundWindow()
        if h_fore and h_fore != hwnd:
            fore_tid = _user32.GetWindowThreadProcessId(h_fore, None)
            app_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            _user32.AttachThreadInput(app_tid, fore_tid, True)
            _user32.SetForegroundWindow(hwnd)
            _user32.AttachThreadInput(app_tid, fore_tid, False)
        else:
            _user32.SetForegroundWindow(hwnd)

        for _ in range(30):
            if _user32.GetForegroundWindow() == hwnd:
                break
            time.sleep(0.05)
    except Exception as e:
        print(f"   [警告] 切到前台失败: {e}")

# ==================== 默认配置 ====================
DEFAULT_MIN_INTERVAL = 3
DEFAULT_MAX_INTERVAL = 6
# =================================================

_lock = threading.Lock()
_ocr_reader = None
_com_init = threading.local()


def _ensure_com():
    """在子线程中为 uiautomation 初始化 COM。"""
    if getattr(_com_init, "done", False):
        return
    try:
        ctypes.windll.ole32.CoInitialize(None)
        _com_init.done = True
    except Exception:
        pass


def get_ocr():
    """延迟加载并缓存 OCR 模型。"""
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        print("   [加载] OCR 模型（仅首次）...")
        _ocr_reader = easyocr.Reader(['ch_sim', 'en'], gpu=False)
    return _ocr_reader


def _normalize_for_match(s):
    """统一括号等符号，解决 OCR 把中文括号识别成英文括号的问题。"""
    return (s.replace(" ", "")
            .replace("【", "[")
            .replace("】", "]")
            .replace("（", "(")
            .replace("）", ")"))


def find_text_by_ocr(left, top, right, bottom, target):
    """OCR 精确匹配目标文字，返回坐标。"""
    img = ImageGrab.grab(bbox=(left, top, right, bottom))
    img_np = np.array(img)

    reader = get_ocr()
    results = reader.readtext(img_np)

    norm_target = _normalize_for_match(target)
    candidates = []
    for bbox, text, conf in results:
        text = text.strip().replace(" ", "")
        norm_text = _normalize_for_match(text)
        # 精确匹配或包含关系，支持群名、特殊符号、截断等情况
        if norm_text == norm_target or norm_target in norm_text or norm_text in norm_target:
            ys = [p[1] for p in bbox]
            center_y = top + sum(ys) / len(ys)
            center_x = left + sum([p[0] for p in bbox]) / len(bbox)
            candidates.append((center_x, center_y, conf))
            print(f"      [匹配] '{text}' (置信度: {conf:.3f}, y={center_y:.0f})")

    if not candidates:
        # 调试：打印所有识别到的文本，方便排查
        print(f"   [调试] OCR 未命中，识别到的文本: {[r[1].strip() for r in results]}")
        return None

    candidates.sort(key=lambda x: -x[2])  # 选置信度最高的
    return (candidates[0][0], candidates[0][1])


def send_to_contact(wechat_rect, name, strategy, msg):
    """给单个联系人发送消息。"""
    print(f"\n[发送] {name} (策略: {strategy})")

    # 1. 点击搜索框并输入
    pyautogui.click(wechat_rect.left + 140, wechat_rect.top + 60)
    time.sleep(0.3)
    pyautogui.hotkey('ctrl', 'a')
    pyautogui.press('delete')
    pyperclip.copy(name)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(1.5)  # 等待搜索结果

    # 2. 进入聊天
    if strategy == "ocr":
        pos = find_text_by_ocr(
            wechat_rect.left,
            wechat_rect.top + 80,
            wechat_rect.left + 400,
            wechat_rect.top + 500,
            name
        )
        if pos:
            pyautogui.click(pos[0], pos[1])
        else:
            print(f"   [警告] OCR 未识别，跳过 {name}")
            return False
    else:
        # 数字策略：按若干次↓
        skip = int(strategy)
        print(f"   [方向键] 按 {skip} 次向下...")
        for _ in range(skip):
            pyautogui.press('down')
            time.sleep(0.15)
        pyautogui.press('enter')

    time.sleep(1.0)  # 等待进入聊天

    # 3. 发送消息
    pyautogui.click(wechat_rect.right - 200, wechat_rect.bottom - 60)
    time.sleep(0.3)
    pyperclip.copy(msg)
    pyautogui.hotkey('ctrl', 'v')
    time.sleep(0.3)
    pyautogui.press('enter')

    print(f"   [成功] 已发送")
    return True


def send_message(to, message, strategy="ocr"):
    """
    给单个联系人发送消息。
    """
    if not to or not message:
        print("[错误] 接收人或消息不能为空")
        return False

    with _lock:
        try:
            _ensure_com()
            wechat = auto.WindowControl(searchDepth=1, ClassName="Qt51514QWindowIcon")
            if not wechat.Exists(3, 1):
                print("[错误] 未找到微信窗口，请确认微信已打开")
                return False

            print("[已连接] 微信窗口")
            hwnd = wechat.NativeWindowHandle
            _bring_to_foreground(hwnd)
            wechat.SwitchToThisWindow()
            time.sleep(0.5)
            rect = wechat.BoundingRectangle
            return send_to_contact(rect, to, strategy, message)
        except Exception as e:
            print(f"[错误] 发送异常: {e}")
            return False


def send_batch(contacts, message, min_interval=DEFAULT_MIN_INTERVAL, max_interval=DEFAULT_MAX_INTERVAL):
    """批量发送消息。"""
    if not contacts or not message:
        print("[错误] 联系人列表或消息不能为空")
        return {"success": 0, "fail": 0, "total": 0}

    success = 0
    fail = 0

    for i, (name, strategy) in enumerate(contacts, 1):
        try:
            print(f"\n[{i}/{len(contacts)}] ", end="")
            if send_message(name, message, strategy):
                success += 1
            else:
                fail += 1

            if i < len(contacts):
                interval = random.uniform(min_interval, max_interval)
                print(f"   [等待] {interval:.1f} 秒...")
                time.sleep(interval)

        except Exception as e:
            print(f"   [错误] 异常: {e}")
            fail += 1
            continue

    print("\n" + "=" * 50)
    print("发送完成")
    print(f"成功: {success} / {len(contacts)}")
    print(f"失败: {fail} / {len(contacts)}")
    print("[提醒] 请去微信确认是否发给正确的人！")
    print("=" * 50)

    return {"success": success, "fail": fail, "total": len(contacts)}


def main():
    """命令行入口，保持原有使用方式。"""
    CONTACTS = [
        ("文件传输助手", "ocr"),  # 测试用
        ("张淼", "ocr"),
        # ("李四", "ocr"),
        # 添加更多...
    ]
    MESSAGE = "这是一条批量发送的测试消息"

    print("=" * 50)
    print("微信批量发送（OCR 精确匹配版）")
    print("=" * 50)
    print(f"共 {len(CONTACTS)} 个联系人")
    print(f"消息: {MESSAGE}")
    print(f"间隔: {DEFAULT_MIN_INTERVAL}-{DEFAULT_MAX_INTERVAL} 秒")
    print("=" * 50)

    send_batch(CONTACTS, MESSAGE)


if __name__ == "__main__":
    main()