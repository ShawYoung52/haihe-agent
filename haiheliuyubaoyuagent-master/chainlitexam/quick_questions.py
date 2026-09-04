"""按角色查询快捷问题（quick questions）。

供前端「查询指定角色对应的快捷问题」接口使用。

设计要点：
- 快捷问题**内容**以后端持有的 `chainlitexam/config/quickQA.json` 为事实源，随 Chainlit
  服务一起部署。**不读 `AgentWeb/config/quickQA.json`**——AgentWeb 在服务器上是独立部署到
  Tomcat webapps 的前端静态包，与 Chainlit 服务不在一起，后端读不到也不该依赖它的位置。
  前端面板若改走本接口下发，则以后端这份为唯一数据源；AgentWeb 那份仅留给旧的静态 fetch 路径。
- **角色 → 可见分区**的策略放在本模块代码里（`_ROLE_SECTION_IDS`），与 JSON 内容解耦，
  便于后端独立调整权限而不动前端静态文件。
- 文件小、内容静态，用 mtime 缓存避免每次请求重读磁盘；读到的分区在返回前深拷贝，
  防止调用方意外串改缓存对象。

角色口径（与 chain_gzt.ALLOWED_USER_ROLES 一致）：admin / forecaster / external。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

# 快捷问题配置文件路径（默认取后端自带 config/quickQA.json，随 Chainlit 服务部署；
# env 可覆盖，用于运维指向其它位置）。
_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "quickQA.json"
CONFIG_PATH_ENV = "QUICK_QA_CONFIG_PATH"

# 角色 → 可见分区 id 集合；值为 None 表示「全部可见」（不做过滤）。
# 口径（2026-09-04 用户拍板）：面板只给各角色展示与其工作相关的实况/预报类分区——
# admin 全量 8 区；
# forecaster（区局预报员）= 01天气资讯（实况/预报/预警）+ 02防汛服务（河系水库降雨预报与风险）；
# external（公众）= 仅 01天气资讯。
# 文旅/科普/行业/业务技术/统计/系统问答不上快捷面板。
_ROLE_SECTION_IDS: dict[str, set[int] | None] = {
    "admin": None,
    "forecaster": {1, 2},
    "external": {1},
}

# 与 chain_gzt.ALLOWED_USER_ROLES 保持一致（独立维护，避免反向 import chain_gzt）。
ALLOWED_ROLES = frozenset(_ROLE_SECTION_IDS.keys())

DEFAULT_ROLE = "external"

_cache: dict[str, Any] = {"mtime": None, "data": None}
# 端点是同步函数、跑在 FastAPI/anyio 线程池里，多请求并发；按 CLAUDE.md 缓存约定配锁，
# 避免一个线程写 mtime、另一个线程在 data 更新前读到「新 mtime + 旧 sections」。
_cache_lock = threading.Lock()


def _config_path() -> Path:
    override = os.getenv(CONFIG_PATH_ENV, "").strip()
    return Path(override) if override else _DEFAULT_CONFIG_PATH


def _load_sections() -> list[dict[str, Any]]:
    """读取并缓存 quickQA.json 的 sections；文件缺失/损坏返回空列表（不抛错）。"""
    path = _config_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    with _cache_lock:
        if _cache["mtime"] == mtime and _cache["data"] is not None:
            return _cache["data"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sections = raw.get("sections")
    if not isinstance(sections, list):
        return []
    with _cache_lock:
        _cache["mtime"] = mtime
        _cache["data"] = sections
    return sections


def get_quick_questions(role: str) -> dict[str, Any]:
    """按角色返回可见的快捷问题分区。

    返回 {"role": role, "sections": [...]}；sections 为深拷贝，结构与 quickQA.json 一致。
    未知/空角色按 external（最严格）处理——接口层对显式非法 role 会先 400，这里再兜底。
    """
    normalized = (role or "").strip().lower()
    if normalized not in ALLOWED_ROLES:
        normalized = DEFAULT_ROLE
    allowed = _ROLE_SECTION_IDS[normalized]
    sections = _load_sections()
    if allowed is not None:
        sections = [s for s in sections if s.get("id") in allowed]
    return {"role": normalized, "sections": copy.deepcopy(sections)}


def reset_cache() -> None:
    """测试用：清空 mtime 缓存。"""
    with _cache_lock:
        _cache["mtime"] = None
        _cache["data"] = None
