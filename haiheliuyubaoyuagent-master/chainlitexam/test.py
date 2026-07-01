#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信批量自动发送消息（OCR 精确匹配版）
- 每个联系人独立搜索 + OCR 识别点击
- 支持两种匹配策略：OCR 精确匹配 / 固定跳过次数（快速模式）
- 随机间隔，降低风控
"""

import uiautomation as auto
import pyperclip
import pyautogui
from PIL import ImageGrab
import numpy as np
import time
import random
import sys

# ==================== 配置区 ====================
# 联系人列表
# 格式: (名称, 策略)
# 策略 "ocr" = OCR 精确匹配（最稳，稍慢）
# 策略 数字 = 搜索后按几次↓（如 0=直接回车，1=按1次↓，快但位置要固定）
CONTACTS = [
    ("文件传输助手", "ocr"),  # 测试用
    ("张淼", "ocr"),
    # ("李四", "ocr"),
    # 添加更多...
]

# 发送内容
MESSAGE = "这是一条批量发送的测试消息"

# 发送间隔（秒），随机范围，防风控
MIN_INTERVAL = 3
MAX_INTERVAL = 6
# ==============================================

_OCR_READER = None


def get_ocr():
    global _OCR_READER
    if _OCR_READER is None:
        import easyocr
        print("   ⏳ 加载 OCR 模型（仅首次）...")
        _OCR_READER = easyocr.Reader(['ch_sim', 'en'], gpu=False)
    return _OCR_READER


def find_text_by_ocr(left, top, right, bottom, target):
    """OCR 精确匹配目标文字，返回坐标"""
    img = ImageGrab.grab(bbox=(left, top, right, bottom))
    img_np = np.array(img)

    reader = get_ocr()
    results = reader.readtext(img_np)

    candidates = []
    for bbox, text, conf in results:
        text = text.strip().replace(" ", "")
        if text == target:
            ys = [p[1] for p in bbox]
            center_y = top + sum(ys) / len(ys)
            # 排除聊天记录区域（通常 y > 400）
            if center_y > 400:
                continue
            center_x = left + sum([p[0] for p in bbox]) / len(bbox)
            candidates.append((center_x, center_y, conf))
            print(f"      ⭐ 匹配: '{text}' (置信度: {conf:.3f})")

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1])  # 选最靠上的（功能区优先）
    return (candidates[0][0], candidates[0][1])


def send_to_contact(wechat_rect, name, strategy, msg):
    """
    给单个联系人发送消息
    """
    print(f"\n📨 发送给: {name} (策略: {strategy})")

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
            print(f"   ⚠️ OCR 未识别，跳过 {name}")
            return False
    else:
        # 数字策略：按若干次↓
        skip = int(strategy)
        print(f"   ↓ 按 {skip} 次方向键...")
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

    print(f"   ✅ 已发送")
    return True


def main():
    print("=" * 50)
    print("微信批量发送（OCR 精确匹配版）")
    print("=" * 50)
    print(f"共 {len(CONTACTS)} 个联系人")
    print(f"消息: {MESSAGE}")
    print(f"间隔: {MIN_INTERVAL}-{MAX_INTERVAL} 秒")
    print("=" * 50)

    # 连接微信
    wechat = auto.WindowControl(searchDepth=1, ClassName="Qt51514QWindowIcon")
    if not wechat.Exists(3, 1):
        print("❌ 未找到微信窗口")
        sys.exit(1)
    print("\n✅ 已连接微信")

    wechat.SwitchToThisWindow()
    time.sleep(0.5)
    rect = wechat.BoundingRectangle

    # 批量发送
    success = 0
    fail = 0

    for i, (name, strategy) in enumerate(CONTACTS, 1):
        try:
            print(f"\n[{i}/{len(CONTACTS)}] ", end="")
            if send_to_contact(rect, name, strategy, MESSAGE):
                success += 1
            else:
                fail += 1

            # 间隔（最后一个不用等）
            if i < len(CONTACTS):
                interval = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
                print(f"   ⏳ 等待 {interval:.1f} 秒...")
                time.sleep(interval)

        except Exception as e:
            print(f"   ❌ 异常: {e}")
            fail += 1
            continue

    # 汇总
    print("\n" + "=" * 50)
    print("发送完成")
    print(f"成功: {success} / {len(CONTACTS)}")
    print(f"失败: {fail} / {len(CONTACTS)}")
    print("⚠️ 请去微信确认是否发给正确的人！")
    print("=" * 50)


if __name__ == "__main__":
    main()