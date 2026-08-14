"""问答智能体 HTTP 接口适配层（供天河小程序等外部客户端调用）。

核心思路：`message_orchestrator.process_message()` 与 Chainlit 深度耦合
（92 处 `cl.*` 调用），且永远返回 None——答案是流进 `cl.Message` 的。
本模块用 `init_http_context()` 伪造一个 Chainlit HTTP 会话，再替换 emitter
拦截所有输出，从而在**不改动核心问答逻辑**的前提下把它暴露成 HTTP 接口。

依赖方向约束：本模块**禁止 import chain_gzt**。
`chain_gzt.py` 有大量模块级副作用（创建 FastAPI app、双 mount、matplotlib
字体扫描、数据库表检查）和重依赖，import 它会让本模块无法独立测试。
chain / callbacks 由 `chain_gzt` 在自身初始化完成后通过 `configure()` 注入。

本模块自身依赖 chainlit（emitter 基类、context、config），这是不可避免的——
拦截 Chainlit 输出就得继承它的 emitter。import chainlit 会创建 `.files/`
与 `.chainlit/` 目录（`chainlit/config.py` 模块级 mkdir），属已知代价。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Protocol

from chainlit.emitter import BaseChainlitEmitter
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 配置


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """读整型环境变量，非法或越界时回落默认并告警。

    `minimum` 兜底很重要：配错 `QA_API_MAX_CONCURRENCY=-1` 会让
    `asyncio.Semaphore(-1)` 在 **import 期**抛 ValueError，整个服务起不来；
    配成 0 则所有请求永久阻塞。宁可回落默认值，也不能让服务挂掉。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("环境变量 %s 值非法（%r），使用默认值 %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning(
            "环境变量 %s=%d 小于允许最小值 %d，使用默认值 %d", name, value, minimum, default
        )
        return default
    return value


MAX_CONCURRENCY = _env_int("QA_API_MAX_CONCURRENCY", 4)
TIMEOUT_SECONDS = _env_int("QA_API_TIMEOUT_SECONDS", 180)
FILE_TTL_SECONDS = _env_int("QA_API_FILE_TTL_SECONDS", 1800, minimum=0)
CONVERSATION_TTL_SECONDS = _env_int("QA_API_CONVERSATION_TTL_SECONDS", 3600, minimum=0)
RESPONSE_CACHE_TTL_SECONDS = _env_int("QA_API_RESPONSE_CACHE_TTL", 300, minimum=0)
# 单轮响应缓存最大条目数：超限时修剪过期条目，防无界增长（内存泄漏）。
RESPONSE_CACHE_MAX_SIZE = _env_int("QA_API_RESPONSE_CACHE_MAX_SIZE", 200)
MAX_HISTORY_TURNS = _env_int("QA_API_MAX_HISTORY_TURNS", 10)
MAX_QUESTION_LENGTH = 2000

EMPTY_ANSWER_FALLBACK = "当前查询未能获得有效结果，请换个问法或稍后重试。"

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# 文件名必须是「严格 UUID + 可选安全扩展名」。
# - 不能用宽松的 [0-9a-fA-F-]{36}：那样 36 个连字符也能过。
# - 扩展名必须白名单：否则 .py/.exe 之类也能拼出来。
# - 扩展名必须**可选**：Chainlit 用 `mimetypes.guess_extension(mime)` 命名，
#   而 `image/jpg`（非标准但工具常返）和空 mime 都返回 None → 文件落盘时没有
#   后缀。若强制要求扩展名，这些图片存在却永远取不回来。
_FILE_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"(?:\.(?:png|jpg|jpeg|gif|webp|svg|bmp))?$",
    re.IGNORECASE,
)


class InvalidFileReference(ValueError):
    """图片引用非法（格式错误或疑似路径穿越）。"""


class FileExpiredOrMissing(FileNotFoundError):
    """图片不存在或已被 TTL 清理。"""


class QANotConfigured(RuntimeError):
    """运行时未注入 chain / callbacks。"""


# ---------------------------------------------------------------- 脱敏

# 内网地址与本地路径不得出现在响应里。HTTP 接口面向外部客户端（天河小程序），
# 暴露面比只给内部业务人员看的网页端大得多。
# `message_orchestrator` 的 `reasoning.line(f"❌ ...{str(e)[:200]}")` 会把**未脱敏的
# 原始异常**写进思考过程，实测能带出内网 IP 和绝对路径，所以出口必须再过一道。
_SCRUB_PATTERNS = [
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "[内网地址]"),
    (re.compile(r"[A-Za-z]:\\[^\s\"'<>|]+"), "[路径]"),          # Windows 绝对路径
    (re.compile(r"(?<![\w.])/(?:home|usr|var|etc|opt|root)/[^\s\"'<>|]+"), "[路径]"),
    (re.compile(r"postgresql(?:\+\w+)?://[^\s\"']+"), "[数据库连接]"),
]


def _scrub(text: str) -> str:
    """抹掉内网 IP、绝对路径、数据库连接串。"""
    if not text:
        return text
    out = text if isinstance(text, str) else str(text)
    for pattern, repl in _SCRUB_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _release_chainlit_session(session_id: str) -> None:
    """回收 Chainlit 为该 HTTP 会话建立的全局状态。

    Chainlit 只在 websocket 断开时清理（`chainlit/socket.py` 里的
    `user_sessions.pop`），`init_http_context()` 造出来的会话永远走不到那条路径。
    不清理则 `user_sessions` / `chat_contexts` 无界增长直到 OOM。
    """
    try:
        from chainlit.chat_context import chat_contexts
        from chainlit.user_session import user_sessions

        user_sessions.pop(session_id, None)
        chat_contexts.pop(session_id, None)
    except Exception:
        logger.warning("回收 Chainlit 会话状态失败，可能造成内存滞留")


# ---------------------------------------------------------------- 答案归并

# 旁路消息：网页端是独立气泡（语义清晰），拼进 HTTP 单字段就会糊成一段。
# 典型翻车：兜底路径下 stream_msg 被 `_prepend_thinking_summary` 填成纯引导语
# （"已结合预报数据完成分析，为您整理结论如下："），紧接着又 send 一条
# "当前查询未能获得有效结果" —— 拼起来自相矛盾。
_SIDEBAND_PREFIXES = ("❌", "⏱️", "📊", "（系统消息：")

# 只有引导语、没有实质结论的开场白。单独出现时不算答案。
_LEAD_IN_ONLY = (
    "已结合预报数据完成分析，为您整理结论如下：",
    "已理解您的问题，为您解答如下：",
)
_LEAD_IN_ONLY_NORMALIZED = tuple(s.rstrip("：:") for s in _LEAD_IN_ONLY)


def _is_sideband(text: str) -> bool:
    """判断是否为旁路提示（错误气泡 / 图表提示 / 纯引导语）。"""
    stripped = text.strip()
    if stripped.startswith(_SIDEBAND_PREFIXES):
        return True
    return stripped.rstrip("：:") in _LEAD_IN_ONLY_NORMALIZED


def merge_answers(answer_steps: Iterable[dict], *, drop_sideband: bool = True, deleted_ids: set[str] | None = None) -> str:
    """把 emitter 收到的答案 step 事件归并成最终答案文本。

    `message_orchestrator.py:4239-4240` 先 `cl.Message(content="")` + `send()`，
    之后才 `stream_msg.content = text` + `update()`。同一条消息因此会产生多个
    step 事件，且**首个事件内容为空**。

    所以必须按 step id 归并、每个 id 取最终态、再按首次出现顺序拼接非空内容。
    直接取最后一条会拿到空串；全部拼接会重复。

    `drop_sideband=True` 时过滤掉错误气泡、图表提示和纯引导语，只留主答案
    （业务确认口径：小程序拿到的应是干净正文）。若过滤后什么都不剩，
    退回未过滤结果，避免把"仅有错误提示"的情况变成空答案。
    """
    deleted = deleted_ids or set()
    final_by_id: dict[str, str] = {}
    order: list[str] = []

    for step in answer_steps:
        if not isinstance(step, dict):
            continue
        step_id = str(step.get("id") or "")
        if not step_id:
            continue
        if step_id in deleted:
            continue
        if step_id not in final_by_id:
            order.append(step_id)
        final_by_id[step_id] = str(step.get("output") or "")

    parts = [final_by_id[i].strip() for i in order if final_by_id[i].strip()]
    if not drop_sideband:
        return "\n\n".join(parts)

    main = [p for p in parts if not _is_sideband(p)]
    # 全是旁路消息（如纯错误）时保留原样，否则会返回空答案丢失失败原因
    return "\n\n".join(main or parts)


# ---------------------------------------------------------------- 历史裁剪


def prune_history(messages: list, *, max_turns: int | None = None) -> list:
    """裁剪对话历史，只保留干净的问答对。

    `process_message` 原地 append，一轮跑完后 list 里混着：
      - HumanMessage          用户问题            → 保留
      - AIMessage(tool_calls) 工具调用空壳         → 丢弃
      - ToolMessage           工具原始返回(数十KB) → 丢弃
      - AIMessage(content)    最终答案            → 保留
      - SystemMessage         系统提示            → 丢弃（见下）

    三条硬约束：
      1. 不能原样存整个 list —— 几轮后上下文爆炸，工具原始 JSON 对后续对话无价值。
      2. **不能只丢 ToolMessage** —— LangChain 要求带 `tool_calls` 的 AIMessage
         后必须紧跟对应 ToolMessage，只丢一半会产生孤儿 tool_calls，LLM API 报错。
         因此二者必须成对丢弃。
      3. **必须丢 SystemMessage** —— `prompt_template` 自己已有 system 段，
         历史里再带一条会形成双 system，干扰模型。
      4. **截断必须从 HumanMessage 边界开始** —— 直接 `kept[-turns*2:]` 在历史
         非严格 Human/AI 交替时会产出以 AIMessage 开头的历史（实测 1512 种序列
         会触发），等于让模型看到一个没有对应提问的回答。
    """

    kept = []
    for m in messages or []:
        if isinstance(m, (ToolMessage, SystemMessage)):
            continue
        if isinstance(m, AIMessage):
            if getattr(m, "tool_calls", None):
                continue  # 工具调用空壳，与上面的 ToolMessage 成对丢弃
            if not str(m.content).strip():
                continue
        kept.append(m)

    turns = MAX_HISTORY_TURNS if max_turns is None else max_turns
    if turns > 0 and len(kept) > turns * 2:
        kept = kept[-turns * 2:]

    # 对齐到第一个 HumanMessage：历史不能以「无提问的回答」开头。
    # 截断会造成这种情况，未截断的原始序列也可能（首条就是 AIMessage）。
    for i, m in enumerate(kept):
        if isinstance(m, HumanMessage):
            return kept[i:]
    return []  # 一条提问都没有，整段无意义


# ---------------------------------------------------------------- 会话存储


class ConversationStore(Protocol):
    """对话历史存储接口。

    本期为进程内存实现；将来换 PostgreSQL 或改成客户端传历史，只需换实现类。
    """

    async def get(self, conversation_id: str) -> list: ...
    async def save(self, conversation_id: str, messages: list) -> None: ...
    async def cleanup_expired(self) -> int: ...
    def lock_for(self, conversation_id: str) -> asyncio.Lock: ...


class InMemoryConversationStore:
    """进程内存实现 + TTL 过期。

    已知取舍：服务重启丢上下文，用户需重新开始会话（用户已确认可接受）。
    """

    def __init__(self, ttl_seconds: int = CONVERSATION_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[float, list]] = {}
        self._lock = asyncio.Lock()
        self._conv_locks: dict[str, asyncio.Lock] = {}

    def _expired(self, saved_at: float) -> bool:
        return (time.time() - saved_at) > self._ttl

    def lock_for(self, conversation_id: str) -> asyncio.Lock:
        """取该会话的串行锁。

        同一 conversation_id 的并发请求必须串行「读历史 → 问答 → 写历史」，
        否则后写的会覆盖先写的、丢掉一整轮对话（已实测复现）。
        """
        lock = self._conv_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conv_locks[conversation_id] = lock
        return lock

    async def get(self, conversation_id: str) -> list:
        async with self._lock:
            entry = self._data.get(conversation_id)
            if entry is None:
                return []
            saved_at, messages = entry
            if self._expired(saved_at):
                self._data.pop(conversation_id, None)
                return []
            return list(messages)  # 返回副本，避免调用方 append 污染内部状态

    async def save(self, conversation_id: str, messages: list) -> None:
        async with self._lock:
            self._data[conversation_id] = (time.time(), list(messages))

    async def cleanup_expired(self) -> int:
        """清理过期历史，并回收孤儿会话锁。

        锁必须独立回收：`lock_for()` 可能在从未 `save()` 的 cid 上创建锁
        （如 `ask` 在写历史前就失败），这类锁不对应任何 `_data` 条目，
        只跟着 `_data` 过期一起删是清不掉的 —— 客户端反复传随机 UUID
        就能让锁字典无界增长。
        """
        async with self._lock:
            stale = [k for k, (ts, _) in self._data.items() if self._expired(ts)]
            for k in stale:
                self._data.pop(k, None)

            # 保留仍有历史的、以及当前正被持有的锁（持有中的不能丢，否则
            # 后来者会拿到一把新锁，串行保证失效）
            self._conv_locks = {
                k: lock
                for k, lock in self._conv_locks.items()
                if k in self._data or lock.locked()
            }
            return len(stale)


# ---------------------------------------------------------------- emitter


class CapturingEmitter(BaseChainlitEmitter):
    """拦截 Chainlit 输出，把答案 / 思考过程 / 图片 / GIS 收集起来。

    `process_message` 内部所有输出都经过 emitter，所以这里是唯一的拦截点，
    无需改动核心逻辑。
    """

    # 工具 step 的 output 可能是数万字节的原始 JSON（MUSIC 站点数据等），
    # 进入 reasoning[] 会同时造成数据量爆炸和敏感信息泄漏（内网地址等）。
    # user_message 是用户问题回显，不是思考过程。
    _REASONING_SKIP_TYPES = frozenset({"tool", "user_message"})

    def __init__(self, session):
        super().__init__(session)
        self.answer_steps: list[dict] = []
        self.reasoning_steps: list[dict] = []
        self.elements: list[dict] = []
        self.gis_packets: list[Any] = []
        self._deleted_ids: set[str] = set()

    def _record(self, step_dict: dict) -> None:
        if not isinstance(step_dict, dict):
            return
        # 浅拷贝防止下游 mutation 污染记录。Chainlit 在 send 后可能修改 dict
        # 的 output 字段（如 ReasoningStep.close 时清空），不拷贝则记录跟着变。
        copy = dict(step_dict)
        if copy.get("type") == "assistant_message":
            self.answer_steps.append(copy)
        elif copy.get("type") not in self._REASONING_SKIP_TYPES:
            self.reasoning_steps.append(copy)

    async def send_step(self, step_dict):
        self._record(step_dict)

    async def update_step(self, step_dict):
        self._record(step_dict)

    async def delete_step(self, step_dict):
        """标记被删除的 step，归并时跳过。

        message_orchestrator.py 的快速路径（如河网绘制）会 stream_msg.remove()
        触发 delete_step。不覆盖的话，被删消息的旧事件仍留在 answer_steps 里，
        merge_answers 会输出本应消失的内容。
        """
        if isinstance(step_dict, dict):
            sid = step_dict.get("id")
            if sid:
                self._deleted_ids.add(str(sid))

    async def send_element(self, element_dict):
        if isinstance(element_dict, dict):
            self.elements.append(element_dict)

    async def send_window_message(self, data):
        # GIS 联动包是 JSON 字符串；非 JSON 内容忽略而不是让请求崩掉
        try:
            self.gis_packets.append(json.loads(data) if isinstance(data, str) else data)
        except (TypeError, ValueError):
            logger.debug("window message 非 JSON，已忽略")

    def reasoning_texts(self) -> list[str]:
        """按 step id 归并思考过程，取每个的最终态。

        与 `merge_answers` 语义一致：总是取最后一次 output，即使为空。
        然后过滤掉被 delete_step 标记的 id 和空内容。
        """
        final_by_id: dict[str, str] = {}
        order: list[str] = []
        for step in self.reasoning_steps:
            sid = str(step.get("id") or "")
            if not sid:
                continue
            if sid not in order:
                order.append(sid)
            final_by_id[sid] = str(step.get("output") or "")
        return [
            final_by_id[i].strip()
            for i in order
            if i in final_by_id and final_by_id[i].strip() and i not in self._deleted_ids
        ]



# ---------------------------------------------------------------- 图片


_FILES_ROOT: Path | None = None


def _files_root() -> Path:
    """Chainlit 的文件落盘根目录。

    必须读 `chainlit.config.FILES_DIRECTORY`，不能自己拼路径——该目录由
    Chainlit 按进程 cwd 在 import 时定下来，自己拼会跟实际落盘位置不一致。
    """
    global _FILES_ROOT
    if _FILES_ROOT is None:
        from chainlit.config import FILES_DIRECTORY

        _FILES_ROOT = Path(FILES_DIRECTORY)
    return _FILES_ROOT


def resolve_file(session_id: str, file_id: str) -> Path:
    """校验并解析图片真实路径。

    三重防护，任一失败即拒绝：
      1. session_id 必须是严格 UUID，file_id 必须是 `<uuid>.<ext>`
      2. 拼接后 resolve() 消解 `..`
      3. 解析结果必须仍在 FILES_DIRECTORY 之内
    """
    sid = (session_id or "").strip()
    fid = (file_id or "").strip()

    if not _UUID_RE.match(sid):
        raise InvalidFileReference("session_id 格式非法")
    if not _FILE_ID_RE.match(fid):
        raise InvalidFileReference("file_id 格式非法")

    root = _files_root().resolve()
    target = (root / sid / fid).resolve()

    if not target.is_relative_to(root):
        raise InvalidFileReference("路径越界")
    if not target.is_file():
        raise FileExpiredOrMissing("图片不存在或已过期")
    return target


def cleanup_expired_files(ttl_seconds: int = FILE_TTL_SECONDS) -> int:
    """删除超过 TTL 的会话文件子目录。每个 session 独立子目录，清理边界干净。

    注意：这是**同步阻塞 IO**（实测清理含 300 文件的目录会卡 event loop ~32ms）。
    在 async 上下文中请用 `cleanup_expired_files_async()`，不要直接调本函数。

    mtime 语义：目录内新增文件会刷新父目录 mtime，因此活跃会话不会被误删；
    持续产图的会话不会过期，会话结束后才开始倒计时——这是期望行为。
    """
    root = _files_root()
    if not root.is_dir():
        return 0

    removed = 0
    cutoff = time.time() - ttl_seconds
    for child in root.iterdir():
        if not child.is_dir() or not _UUID_RE.match(child.name):
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError as e:
            logger.warning("清理会话目录失败：%s", type(e).__name__)
    return removed


async def cleanup_expired_files_async(ttl_seconds: int = FILE_TTL_SECONDS) -> int:
    """`cleanup_expired_files` 的 async 包装：放线程池执行，不阻塞 event loop。"""
    return await asyncio.to_thread(cleanup_expired_files, ttl_seconds)


def _build_image_payload(emitter: CapturingEmitter, session) -> list[dict]:
    """把 emitter 收到的图片元素映射成可访问 URL。

    URL 里的目录段用图片**实际落盘的父目录名**，而不是直接写 `session.id`。
    二者当前等价（Chainlit `BaseSession.files_dir` 返回 `FILES_DIRECTORY / self.id`），
    但取实际路径可在 Chainlit 改变该规则时自动跟随，不至于让所有图片 URL 失效。
    """
    files = getattr(session, "files", None) or {}
    images = []
    for el in emitter.elements:
        if el.get("type") != "image":
            continue
        key = el.get("chainlitKey")
        entry = files.get(key) if key else None
        if not entry:
            continue
        path = Path(entry.get("path") or "")
        if not path.is_file():
            continue
        images.append({
            "name": el.get("name") or path.stem,
            "url": f"/api/v1/qa/files/{path.parent.name}/{path.name}",
            "mime": el.get("mime") or entry.get("type") or "image/png",
        })
    return images


# ---------------------------------------------------------------- 运行时


@contextlib.contextmanager
def _suppress_chainlit_data_layer():
    """HTTP 请求期间临时令 chainlit 数据层返回 None，跳过 step 持久化写库。

    HTTP 客户端不读 DB（答案走 CapturingEmitter 内存捕获、多轮上下文走
    InMemoryConversationStore），每请求 ~20-50 次 fire-and-forget 写库 +
    孤儿 thread/step 行只增不减，是纯浪费。chainlit 的 step.send/update 在
    data_layer 为 None 时不产生持久化任务（`if data_layer:` 分支不执行），
    恢复前无后台任务引用旧层，退出后可安全还原。

    必须同时置 `_data_layer_initialized=True`：chain_gzt 手工装 SQLAlchemyDataLayer
    时未置该标志，若只置 None 会让 `get_data_layer()` 走懒加载分支重建一个真层。
    """
    import chainlit.data as _cl_data

    prev_layer = getattr(_cl_data, "_data_layer", None)
    prev_initialized = getattr(_cl_data, "_data_layer_initialized", False)
    _cl_data._data_layer = None
    _cl_data._data_layer_initialized = True
    try:
        yield
    finally:
        _cl_data._data_layer = prev_layer
        _cl_data._data_layer_initialized = prev_initialized


class QARuntime:
    """持有注入的 chain 与 callbacks，供 HTTP 请求复用。

    chain 与 tools 进程级缓存一次（`load_sse_tools()` 无状态，可安全缓存）。
    """

    def __init__(self):
        self._factory = None
        self._runtime: dict[str, Any] | None = None
        self._init_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
        self.store: ConversationStore = InMemoryConversationStore()
        # 单轮问答响应缓存：相同问题 + 相同开关在 TTL 内直接返回上次结果，
        # 避免每次都走完整 planner + 工具 + LLM。多轮请求（带 conversation_id）
        # 不缓存，保证上下文正确。默认 TTL 5 分钟。
        self._response_cache: dict[str, tuple[float, dict]] = {}

    def configure(self, runtime_factory) -> None:
        """由 chain_gzt 注入：一个 async 工厂，返回
        {"planner_chain", "answer_chain", "thinking_chain", "tools", "callbacks"}。
        """
        self._factory = runtime_factory
        self._runtime = None

    @property
    def configured(self) -> bool:
        return self._factory is not None

    async def _get_runtime(self) -> dict[str, Any]:
        """取（并首次构造）运行时。

        工厂里含 `load_sse_tools()`，要连内网 MCP —— 内网抖动时可能长时间挂住，
        所以必须自带超时；否则「180s 超时」形同虚设，所有请求无限期等待。
        失败后清空缓存，让下一个请求可以重试。
        """
        if self._runtime is not None:
            return self._runtime
        if self._factory is None:
            raise QANotConfigured("问答运行时未初始化")
        async with self._init_lock:
            if self._runtime is None:
                try:
                    self._runtime = await asyncio.wait_for(
                        self._factory(), timeout=TIMEOUT_SECONDS
                    )
                except BaseException:
                    self._runtime = None  # 允许后续请求重试
                    raise
        return self._runtime

    def _maybe_prune_response_cache(self) -> None:
        """单轮响应缓存超限时修剪过期条目（防无界增长）。

        只在超过 RESPONSE_CACHE_MAX_SIZE 时才扫描，避免每请求白扫。
        """
        if len(self._response_cache) <= RESPONSE_CACHE_MAX_SIZE:
            return
        cutoff = time.time() - RESPONSE_CACHE_TTL_SECONDS
        for key in [
            k for k, (ts, _) in self._response_cache.items() if ts < cutoff
        ]:
            del self._response_cache[key]

    async def ask(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        include_reasoning: bool = True,
        include_gis: bool = True,
    ) -> dict[str, Any]:
        """执行一次问答。

        返回 {answer, conversation_id, images, gis, reasoning, elapsed_seconds}。
        超时抛 asyncio.TimeoutError，未配置抛 QANotConfigured。
        """
        text = (question or "").strip()
        if not text:
            raise ValueError("question 不能为空")
        if len(text) > MAX_QUESTION_LENGTH:
            raise ValueError(f"question 超长（上限 {MAX_QUESTION_LENGTH} 字）")

        cid = (conversation_id or "").strip()
        if cid and not _UUID_RE.match(cid):
            raise ValueError("conversation_id 格式非法")
        # 不存在或已过期的 cid 当新会话处理，不报错
        cid = cid or str(uuid.uuid4())

        # 单轮请求（无 conversation_id）命中缓存则直接返回，省掉完整问答流程。
        # 多轮请求不缓存（上下文不同）。缓存 key 含开关，排除开关差异。
        response_cache_key = None
        if not conversation_id:
            response_cache_key = json.dumps(
                {"q": text, "r": include_reasoning, "g": include_gis},
                sort_keys=True, ensure_ascii=False,
            )
            hit = self._response_cache.get(response_cache_key)
            if hit and (time.time() - hit[0]) < RESPONSE_CACHE_TTL_SECONDS:
                # dict() 浅拷贝只拷贝顶层 dict，images/gis/reasoning 列表仍是
                # 共享引用。下游（如 JSON 序列化）只读不修改，但防御性地复制列表
                # 引用，避免将来有人加 mutation 逻辑时污染缓存。
                raw = hit[1]
                cached = {
                    "answer": raw["answer"],
                    "images": list(raw["images"]),
                    "gis": list(raw["gis"]),
                    "reasoning": list(raw["reasoning"]),
                    "elapsed_seconds": 0,
                }
                cached["conversation_id"] = cid  # 单轮每次应返回新会话 id
                return cached

        runtime = await self._get_runtime()
        # 同一 conversation_id 串行，避免「读历史→问答→写历史」的读改写竞态
        # （并发两个请求会让后写的覆盖先写的，丢掉一整轮对话；已实测复现）
        async with self.store.lock_for(cid):
            # 观测埋点：记录 HTTP 信号量排队耗时，供 TimingContext.http_queue_wait_ms 使用，
            # 让 P95/P99 分析能分离「排队等待」与「实际处理」。
            sem_wait_start = time.time()
            async with self._semaphore:
                sem_wait_ms = (time.time() - sem_wait_start) * 1000
                result = await asyncio.wait_for(
                    self._run_once(
                        text, cid, runtime, include_reasoning, include_gis,
                        http_queue_wait_ms=sem_wait_ms,
                    ),
                    timeout=TIMEOUT_SECONDS,
                )

        if response_cache_key is not None and RESPONSE_CACHE_TTL_SECONDS > 0:
            self._response_cache[response_cache_key] = (time.time(), result)
            self._maybe_prune_response_cache()
        return result
    async def _run_once(
        self,
        question: str,
        conversation_id: str,
        runtime: dict[str, Any],
        include_reasoning: bool,
        include_gis: bool,
        http_queue_wait_ms: float = 0.0,
    ) -> dict[str, Any]:
        import chainlit as cl
        from chainlit.context import context_var, init_http_context

        from message_orchestrator import process_message

        started = time.time()

        # 每请求独立 Chainlit 会话。ContextVar 在 asyncio Task 间天然隔离，
        # cl.user_session 以 session.id 为 key，因此并发请求不会串扰。
        ctx = init_http_context(thread_id=str(uuid.uuid4()))
        emitter = CapturingEmitter(ctx.session)
        ctx.emitter = emitter
        context_var.set(ctx)

        # 排队发生在 _run_once 之前（ask 的 _semaphore），此处把等待时间写入
        # 会话供 process_message 读取到 TimingContext.http_queue_wait_ms。
        if http_queue_wait_ms:
            try:
                cl.user_session.set("http_queue_wait_ms", float(http_queue_wait_ms))
            except Exception:
                pass

        # 历史副本传进去（process_message 会原地 append 本轮问答）
        history = await self.store.get(conversation_id)

        pq_started = time.time()
        try:
            # HTTP 模式跳过 chainlit data-layer 落库（见 _suppress_chainlit_data_layer）。
            with _suppress_chainlit_data_layer():
                await process_message(
                    message=cl.Message(content=question),
                    planner_chain=runtime["planner_chain"],
                    answer_chain=runtime["answer_chain"],
                    thinking_chain=runtime["thinking_chain"],
                    tools=runtime["tools"],
                    messages=history,
                    callbacks=runtime["callbacks"],
                )
        finally:
            # 即使超时被取消，也要落盘本轮历史——否则用户下一轮上下文断裂。
            # shield 防止 save 自身被同一个 CancelledError 打断。
            try:
                await asyncio.shield(
                    self.store.save(conversation_id, prune_history(history))
                )
            except Exception:
                logger.warning("保存对话历史失败，本轮上下文将丢失")

            # 关键：Chainlit 只在 websocket 断开时回收 session
            # （`chainlit/socket.py` 的 user_sessions.pop），HTTP 会话永远走不到那里。
            # 不手动清理会让 user_sessions / chat_contexts 无界增长（实测 ~5 KB/请求，
            # 生产上几万请求即 GB 级），最终 OOM。
            _release_chainlit_session(ctx.session.id)

        pq_elapsed = time.time() - pq_started
        total_elapsed = round(time.time() - started, 2)
        if pq_elapsed > 5:
            print(f"[QA-TIMING] {question[:30]!r} process_message={pq_elapsed:.1f}s total={total_elapsed}s", flush=True)

        answer = merge_answers(emitter.answer_steps, deleted_ids=emitter._deleted_ids) or EMPTY_ANSWER_FALLBACK
        return {
            "answer": _scrub(answer),
            "conversation_id": conversation_id,
            "images": _build_image_payload(emitter, ctx.session),
            "gis": list(emitter.gis_packets) if include_gis else [],
            "reasoning": [_scrub(t) for t in emitter.reasoning_texts()] if include_reasoning else [],
            "elapsed_seconds": total_elapsed,
        }


# 进程级单例，由 chain_gzt 在启动时 configure()
runtime = QARuntime()

# 注意：模块级 asyncio 同步原语（QARuntime 的 _semaphore/_init_lock）依赖
# 「整个进程只有一个 event loop」这个前提。本项目成立（chainlit CLI 只
# asyncio.run 一次）。若将来引入同步 TestClient 或多 loop，需改成 lazy 创建，
# 否则会抛 "bound to a different event loop"。


CLEANUP_INTERVAL_SECONDS = _env_int("QA_API_CLEANUP_INTERVAL_SECONDS", 300)


async def run_cleanup_loop(interval_seconds: int = CLEANUP_INTERVAL_SECONDS) -> None:
    """后台清理循环：定期回收过期图片与对话历史。

    必须真的被启动 —— 否则 `QA_API_FILE_TTL_SECONDS` /
    `QA_API_CONVERSATION_TTL_SECONDS` 只是纸面配置，图片和历史永不回收。
    由 `chain_gzt` 在服务启动时 create_task。
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            files_removed = await cleanup_expired_files_async()
            convs_removed = await runtime.store.cleanup_expired()
            if files_removed or convs_removed:
                logger.info(
                    "问答接口清理：图片目录 %d 个、会话历史 %d 条", files_removed, convs_removed
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            # 清理失败不能让循环退出，否则后续永不清理
            logger.warning("问答接口清理任务本轮失败，将在下个周期重试", exc_info=False)
