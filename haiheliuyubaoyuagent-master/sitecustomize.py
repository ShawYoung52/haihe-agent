"""项目级 Python 启动补丁入口。

用于 systemd 工作目录为 haiheliuyubaoyuagent-master 时，确保 Chainlit 前端能安装
“上个月面雨量”快速路径补丁。
"""
from __future__ import annotations

import os
import sys

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CHAINLIT_DIR = os.path.join(_BASE_DIR, "chainlitexam")
if os.path.isdir(_CHAINLIT_DIR) and _CHAINLIT_DIR not in sys.path:
    sys.path.insert(0, _CHAINLIT_DIR)

try:
    from last_month_areal_patch import install_last_month_areal_patch

    install_last_month_areal_patch()
except Exception as exc:  # pragma: no cover - 启动期兜底
    print(f"[sitecustomize] 上个月面雨量补丁安装失败：{exc}")
