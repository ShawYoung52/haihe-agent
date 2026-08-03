"""问答智能体 HTTP 接口测试。

全部用假 chain，不依赖内网连通性。
运行：从 chainlitexam/ 目录跑 `<venv>/python.exe -m pytest tests/test_qa_http_api.py -v`
"""

import asyncio
import base64
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import qa_http_api as qa

# 1x1 透明 PNG，用于图片相关测试
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def _skip_if_chainlit_stubbed():
    """全量跑测试时 tests/stubs.py 可能已装上假 chainlit.Step。

    假 Step 不走真实 emitter，会让「思考过程被拦截」这类断言拿到假阴性。
    该情况下跳过，以单独运行本文件的结果为准（CLAUDE.md 已记录此现象）。
    """
    import chainlit as cl

    if getattr(cl.Step, "__module__", "").endswith("stubs") or hasattr(cl.Step, "_instances"):
        pytest.skip("tests/stubs.py 的假 chainlit.Step 已生效，跳过真实 emitter 断言")


# ---------------------------------------------------------------- 答案归并


def test_merge_answers_prefers_final_state_over_initial_empty():
    """先 send 空内容、再 update 填正文时，只取最终态，不得到空串。

    message_orchestrator.py:4239-4240 先 cl.Message(content="") + send()，
    之后才 stream_msg.content = text + update()。
    """
    steps = [
        {"id": "m1", "type": "assistant_message", "output": ""},
        {"id": "m1", "type": "assistant_message", "output": "海河流域明天多云。"},
    ]
    assert qa.merge_answers(steps) == "海河流域明天多云。"


def test_merge_answers_does_not_duplicate_same_message():
    """同一条消息多次 update 不产生重复内容。"""
    steps = [
        {"id": "m1", "type": "assistant_message", "output": ""},
        {"id": "m1", "type": "assistant_message", "output": "明天"},
        {"id": "m1", "type": "assistant_message", "output": "明天多云"},
        {"id": "m1", "type": "assistant_message", "output": "明天多云，局地小雨。"},
    ]
    assert qa.merge_answers(steps) == "明天多云，局地小雨。"


def test_merge_answers_joins_multiple_messages_in_order():
    """多条不同答案消息按首次出现顺序拼接。"""
    steps = [
        {"id": "m1", "type": "assistant_message", "output": ""},
        {"id": "m2", "type": "assistant_message", "output": ""},
        {"id": "m2", "type": "assistant_message", "output": "第二段"},
        {"id": "m1", "type": "assistant_message", "output": "第一段"},
    ]
    assert qa.merge_answers(steps) == "第一段\n\n第二段"


def test_merge_answers_skips_blank_and_returns_empty_when_nothing():
    steps = [
        {"id": "m1", "type": "assistant_message", "output": ""},
        {"id": "m2", "type": "assistant_message", "output": "   "},
    ]
    assert qa.merge_answers(steps) == ""


def test_merge_answers_drops_lead_in_only_with_fallback_text():
    """兜底路径的自相矛盾必须消除。

    `_prepend_thinking_summary` 会把 stream_msg 填成纯引导语，紧接着又
    send 一条"未能获得有效结果" —— 直接拼接会得到
    "已结合预报数据完成分析，为您整理结论如下：\\n\\n当前查询未能获得有效结果"。
    """
    steps = [
        {"id": "m1", "type": "assistant_message", "output": "已结合预报数据完成分析，为您整理结论如下："},
        {"id": "m2", "type": "assistant_message", "output": "当前查询未能获得有效结果，请换个问法或稍后重试。"},
    ]
    merged = qa.merge_answers(steps)
    assert "已结合预报数据完成分析" not in merged
    assert merged == "当前查询未能获得有效结果，请换个问法或稍后重试。"


@pytest.mark.parametrize(
    "sideband",
    [
        "❌ 大模型调用失败：TimeoutError: timed out",
        "⏱️ 大模型响应超时，请稍后重试。",
        "📊 图表已生成：",
        "（系统消息：历史极端天气图表已生成并展示）",
    ],
)
def test_merge_answers_drops_sideband_messages(sideband):
    """错误气泡、图表提示不该粘进正文。网页端它们是独立气泡。"""
    steps = [
        {"id": "m1", "type": "assistant_message", "output": "海河流域明天多云，局地小雨。"},
        {"id": "m2", "type": "assistant_message", "output": sideband},
    ]
    assert qa.merge_answers(steps) == "海河流域明天多云，局地小雨。"


def test_merge_answers_keeps_sideband_when_it_is_all_we_have():
    """全是旁路消息时保留原样，否则会把失败原因抹成空答案。"""
    steps = [
        {"id": "m1", "type": "assistant_message", "output": "❌ 大模型调用失败：TimeoutError"},
    ]
    assert "大模型调用失败" in qa.merge_answers(steps)


def test_merge_answers_can_disable_sideband_filter():
    steps = [
        {"id": "m1", "type": "assistant_message", "output": "正文"},
        {"id": "m2", "type": "assistant_message", "output": "📊 图表已生成："},
    ]
    assert "📊" in qa.merge_answers(steps, drop_sideband=False)


# ---------------------------------------------------------------- 历史裁剪


def _msgs():
    """构造 process_message 跑完两轮后 messages 的真实形态。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    return [
        HumanMessage(content="海河流域明天天气？"),
        AIMessage(content="", tool_calls=[{"name": "get_forecast", "args": {"d": 1}, "id": "call_1"}]),
        ToolMessage(content='{"huge":"' + "x" * 3000 + '"}', tool_call_id="call_1"),
        AIMessage(content="明天多云。"),
        HumanMessage(content="那后天呢？"),
        AIMessage(content="", tool_calls=[{"name": "get_forecast", "args": {"d": 2}, "id": "call_2"}]),
        ToolMessage(content='{"huge":"' + "y" * 3000 + '"}', tool_call_id="call_2"),
        AIMessage(content="后天晴。"),
    ]


def test_prune_history_drops_tool_messages_and_tool_call_shells():
    """ToolMessage 与带 tool_calls 的 AIMessage 空壳都必须丢掉。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    pruned = qa.prune_history(_msgs())

    assert not any(isinstance(m, ToolMessage) for m in pruned), "ToolMessage 未被丢弃"
    assert not any(
        isinstance(m, AIMessage) and getattr(m, "tool_calls", None) for m in pruned
    ), "带 tool_calls 的 AIMessage 空壳未被丢弃"
    assert [str(m.content) for m in pruned] == [
        "海河流域明天天气？",
        "明天多云。",
        "那后天呢？",
        "后天晴。",
    ]


def test_prune_history_drops_ai_message_with_both_content_and_tool_calls():
    """带 tool_calls 的 AIMessage 即使**同时有正文**也必须丢弃。

    这是最容易漏的情况：模型可以在同一条 AIMessage 里既给出文字又发起工具调用。
    若只靠「content 为空」来过滤，这类消息会被留下，其对应的 ToolMessage 却已被
    丢掉，产生孤儿 tool_calls → LLM API 报错。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    msgs = [
        HumanMessage(content="海河流域明天天气？"),
        AIMessage(
            content="我来查一下预报数据。",  # 既有正文，又有 tool_calls
            tool_calls=[{"name": "get_forecast", "args": {"d": 1}, "id": "call_1"}],
        ),
        ToolMessage(content='{"data":"..."}', tool_call_id="call_1"),
        AIMessage(content="明天多云。"),
    ]

    pruned = qa.prune_history(msgs)

    assert not any(
        isinstance(m, AIMessage) and getattr(m, "tool_calls", None) for m in pruned
    ), "带 tool_calls 的 AIMessage 即使有正文也必须丢弃，否则产生孤儿 tool_calls"
    assert [str(m.content) for m in pruned] == ["海河流域明天天气？", "明天多云。"]


def test_prune_history_leaves_no_orphan_tool_calls():
    """裁剪后不得存在孤儿 tool_calls，否则 LLM API 直接报错。

    LangChain 硬约束：带 tool_calls 的 AIMessage 后必须紧跟对应 ToolMessage。
    """
    from langchain_core.messages import AIMessage, ToolMessage

    # 混入「既有正文又有 tool_calls」的情况，确保覆盖到真实模型行为
    msgs = _msgs()
    msgs.insert(4, AIMessage(
        content="正在查询。",
        tool_calls=[{"name": "get_rain", "args": {}, "id": "call_x"}],
    ))
    msgs.insert(5, ToolMessage(content="{}", tool_call_id="call_x"))

    pruned = qa.prune_history(msgs)
    for i, m in enumerate(pruned):
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            nxt = pruned[i + 1] if i + 1 < len(pruned) else None
            assert isinstance(nxt, ToolMessage), f"索引 {i} 的 tool_calls 没有对应 ToolMessage"


def test_prune_history_compresses_significantly():
    """裁剪应大幅压缩上下文体积。"""
    original = _msgs()
    before = sum(len(str(m.content)) for m in original)
    after = sum(len(str(m.content)) for m in qa.prune_history(original))
    assert before > 6000
    assert after < 100


def test_prune_history_truncates_to_max_turns():
    """超过轮数上限时丢弃最旧的问答对。"""
    from langchain_core.messages import AIMessage, HumanMessage

    msgs = []
    for i in range(10):
        msgs.append(HumanMessage(content=f"问题{i}"))
        msgs.append(AIMessage(content=f"答案{i}"))

    pruned = qa.prune_history(msgs, max_turns=3)
    assert [str(m.content) for m in pruned] == [
        "问题7", "答案7", "问题8", "答案8", "问题9", "答案9",
    ]


def test_prune_history_handles_empty():
    assert qa.prune_history([]) == []


def test_prune_history_drops_system_messages():
    """SystemMessage 必须丢弃：prompt_template 已有 system 段，双 system 会干扰模型。"""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    pruned = qa.prune_history([
        SystemMessage(content="你是海河流域助手"),
        HumanMessage(content="Q1"),
        AIMessage(content="A1"),
    ])
    assert not any(isinstance(m, SystemMessage) for m in pruned)
    assert [str(m.content) for m in pruned] == ["Q1", "A1"]


def test_prune_history_never_starts_with_ai_message():
    """历史不能以「无提问的回答」开头——穷举所有 Human/AI 序列组合。

    截断会造成这种情况（实测原本有 1512 种触发序列），未截断的原始序列
    首条就是 AIMessage 时也会。两者都必须对齐到第一个 HumanMessage。
    """
    import itertools

    from langchain_core.messages import AIMessage, HumanMessage

    offenders = []
    for n in range(1, 9):
        for combo in itertools.product("HA", repeat=n):
            msgs = [
                HumanMessage(content=f"H{i}") if c == "H" else AIMessage(content=f"A{i}")
                for i, c in enumerate(combo)
            ]
            for turns in (1, 2, 3):
                pruned = qa.prune_history(msgs, max_turns=turns)
                if pruned and isinstance(pruned[0], AIMessage):
                    offenders.append(("".join(combo), turns))

    assert not offenders, f"这些序列产出了 AI 开头的历史：{offenders[:5]}"


def test_prune_history_returns_empty_when_no_human_message():
    """全是 AI 回答、没有任何提问时，整段历史无意义，应返回空。"""
    from langchain_core.messages import AIMessage

    assert qa.prune_history([AIMessage(content="A1"), AIMessage(content="A2")]) == []


# ---------------------------------------------------------------- 配置校验


def test_env_int_rejects_values_below_minimum(monkeypatch):
    """非法并发数必须回落默认值，绝不能让模块 import 时崩掉。

    asyncio.Semaphore(-1) 会在 import 期抛 ValueError 让整个服务起不来；
    Semaphore(0) 会让所有请求永久阻塞。
    """
    monkeypatch.setenv("QA_TEST_INT", "-1")
    assert qa._env_int("QA_TEST_INT", 4) == 4

    monkeypatch.setenv("QA_TEST_INT", "0")
    assert qa._env_int("QA_TEST_INT", 4) == 4

    monkeypatch.setenv("QA_TEST_INT", "abc")
    assert qa._env_int("QA_TEST_INT", 4) == 4

    monkeypatch.setenv("QA_TEST_INT", "3.5")
    assert qa._env_int("QA_TEST_INT", 4) == 4

    monkeypatch.setenv("QA_TEST_INT", "8")
    assert qa._env_int("QA_TEST_INT", 4) == 8


def test_env_int_allows_zero_when_minimum_is_zero(monkeypatch):
    """TTL 类配置允许 0（表示立即过期），但仍拒绝负数。"""
    monkeypatch.setenv("QA_TEST_TTL", "0")
    assert qa._env_int("QA_TEST_TTL", 1800, minimum=0) == 0

    monkeypatch.setenv("QA_TEST_TTL", "-5")
    assert qa._env_int("QA_TEST_TTL", 1800, minimum=0) == 1800


def test_semaphore_value_is_always_usable():
    """无论环境变量怎么配，MAX_CONCURRENCY 都必须能构造出可用的 Semaphore。"""
    assert qa.MAX_CONCURRENCY >= 1
    asyncio.Semaphore(qa.MAX_CONCURRENCY)  # 不抛异常即通过


@pytest.mark.parametrize(
    "file_id, expected",
    [
        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.png", True),
        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jpg", True),
        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.PNG", True),
        ("------------------------------------.png", False),   # 36 个连字符
        ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png", False),   # 无连字符
        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.exe", False),   # 可执行扩展名
        ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.py", False),    # 脚本扩展名
    ],
)
def test_file_id_regex_is_strict(file_id, expected):
    """文件名必须是严格 UUID + 图片扩展名，不能放过 .py/.exe 或畸形 UUID。"""
    assert bool(qa._FILE_ID_RE.match(file_id)) is expected


# ---------------------------------------------------------------- 会话存储


async def test_conversation_store_roundtrip():
    from langchain_core.messages import HumanMessage

    store = qa.InMemoryConversationStore(ttl_seconds=3600)
    cid = str(uuid.uuid4())
    assert await store.get(cid) == []

    await store.save(cid, [HumanMessage(content="你好")])
    got = await store.get(cid)
    assert [str(m.content) for m in got] == ["你好"]


async def test_conversation_store_expires_by_ttl():
    from langchain_core.messages import HumanMessage

    store = qa.InMemoryConversationStore(ttl_seconds=0)
    cid = str(uuid.uuid4())
    await store.save(cid, [HumanMessage(content="旧消息")])
    assert await store.get(cid) == [], "TTL 为 0 时应立即过期"


async def test_conversation_store_get_returns_copy():
    """返回副本，调用方 append 不污染 store。"""
    from langchain_core.messages import HumanMessage

    store = qa.InMemoryConversationStore(ttl_seconds=3600)
    cid = str(uuid.uuid4())
    await store.save(cid, [HumanMessage(content="原始")])

    got = await store.get(cid)
    got.append(HumanMessage(content="外部追加"))

    assert len(await store.get(cid)) == 1, "store 内部状态被外部修改污染"


async def test_conversation_store_cleanup_removes_expired():
    from langchain_core.messages import HumanMessage

    store = qa.InMemoryConversationStore(ttl_seconds=0)
    await store.save("a", [HumanMessage(content="x")])
    removed = await store.cleanup_expired()
    assert removed >= 1


async def test_conversation_lock_serializes_same_id_and_preserves_all_turns():
    """同一 conversation_id 并发时必须串行，否则丢历史。

    无锁时「读历史 → 问答 → 写历史」存在读改写竞态：后写的覆盖先写的，
    整轮对话消失（已实测复现）。lock_for() 保证串行。
    """
    from langchain_core.messages import AIMessage, HumanMessage

    store = qa.InMemoryConversationStore(ttl_seconds=3600)
    cid = str(uuid.uuid4())
    await store.save(cid, [HumanMessage(content="Q0"), AIMessage(content="A0")])

    async def turn(tag, delay):
        async with store.lock_for(cid):
            hist = await store.get(cid)
            await asyncio.sleep(delay)  # 模拟问答耗时，制造交错
            hist.append(HumanMessage(content=f"Q-{tag}"))
            hist.append(AIMessage(content=f"A-{tag}"))
            await store.save(cid, qa.prune_history(hist))

    await asyncio.gather(turn("X", 0.03), turn("Y", 0.01))

    contents = [str(m.content) for m in await store.get(cid)]
    assert any("Q-X" in c for c in contents), f"X 轮历史丢失：{contents}"
    assert any("Q-Y" in c for c in contents), f"Y 轮历史丢失：{contents}"
    assert len(contents) == 6, f"期望 3 轮共 6 条，实际 {contents}"


async def test_conversation_lock_is_stable_per_id():
    """同一 id 多次取到同一把锁；不同 id 互不阻塞。"""
    store = qa.InMemoryConversationStore(ttl_seconds=3600)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert store.lock_for(a) is store.lock_for(a)
    assert store.lock_for(a) is not store.lock_for(b)


async def test_cleanup_reclaims_orphan_locks():
    """只 lock_for 未 save 的会话锁必须能回收。

    这类锁不对应任何 _data 条目（如 ask 在写历史前就失败），
    只跟着 _data 过期一起删是清不掉的 —— 客户端反复传随机 UUID
    就能让锁字典无界增长。
    """
    store = qa.InMemoryConversationStore(ttl_seconds=3600)
    for _ in range(50):
        store.lock_for(str(uuid.uuid4()))

    assert len(store._conv_locks) == 50
    await store.cleanup_expired()
    assert len(store._conv_locks) == 0, "孤儿锁未被回收"


async def test_cleanup_keeps_locks_still_held():
    """正在被持有的锁不能回收，否则后来者拿到新锁、串行保证失效。"""
    store = qa.InMemoryConversationStore(ttl_seconds=3600)
    cid = str(uuid.uuid4())
    lock = store.lock_for(cid)

    async with lock:
        await store.cleanup_expired()
        assert store.lock_for(cid) is lock, "持有中的锁被换掉了"


async def test_cleanup_keeps_locks_with_live_history():
    from langchain_core.messages import HumanMessage

    store = qa.InMemoryConversationStore(ttl_seconds=3600)
    cid = str(uuid.uuid4())
    lock = store.lock_for(cid)
    await store.save(cid, [HumanMessage(content="Q")])

    await store.cleanup_expired()
    assert store.lock_for(cid) is lock, "仍有历史的会话锁被回收了"


# ---------------------------------------------------------------- 路径安全


@pytest.mark.parametrize(
    "session_id, file_id",
    [
        ("../../../etc", "passwd"),
        ("..", ".."),
        ("not-a-uuid", str(uuid.uuid4())),
        (str(uuid.uuid4()), "not-a-uuid"),
        (str(uuid.uuid4()), "../../secret"),
        ("", ""),
        (str(uuid.uuid4()) + "/..", str(uuid.uuid4())),
        ("C:\\Windows", "system32"),
        ("/etc", "passwd"),
    ],
)
def test_resolve_file_rejects_traversal_and_bad_ids(session_id, file_id):
    with pytest.raises(qa.InvalidFileReference):
        qa.resolve_file(session_id, file_id)


def test_resolve_file_accepts_valid_ids_but_missing_file():
    """合法 id 但文件不存在时抛 FileNotFound，而非安全异常。"""
    with pytest.raises(qa.FileExpiredOrMissing):
        qa.resolve_file(str(uuid.uuid4()), str(uuid.uuid4()) + ".png")


def test_resolve_file_returns_path_inside_files_dir(tmp_path, monkeypatch):
    sid, fid = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(qa, "_files_root", lambda: tmp_path)
    d = tmp_path / sid
    d.mkdir()
    (d / f"{fid}.png").write_bytes(_PNG)

    resolved = qa.resolve_file(sid, f"{fid}.png")
    assert resolved.read_bytes() == _PNG
    assert resolved.is_relative_to(tmp_path)


# ---------------------------------------------------------------- emitter


async def test_capturing_emitter_reasoning_skips_tool_and_user_message():
    """工具 step 的原始 JSON 和用户回显不应进入思考过程。

    工具 output 可能数万字节，且含内网地址。user_message 是用户问题回显。
    """
    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    _skip_if_chainlit_stubbed()

    ctx = init_http_context(thread_id=str(uuid.uuid4()))
    cap = qa.CapturingEmitter(ctx.session)
    ctx.emitter = cap
    context_var.set(ctx)

    async with cl.Step(name="query_tool", type="tool") as s:
        s.output = '{"站点数据": "海量JSON" * 5000}'

    # 工具 step 不应出现在 reasoning 里
    assert not any("海量JSON" in t for t in cap.reasoning_texts()), "工具原始数据不应进入 reasoning"
    assert len(cap.reasoning_steps) == 0, f"期望 0 条 reasoning，实际 {len(cap.reasoning_steps)}"


async def test_capturing_emitter_reasoning_uses_last_value():
    """reasoning_texts 应与 merge_answers 语义一致：总是取最后输出。"""
    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    _skip_if_chainlit_stubbed()

    ctx = init_http_context(thread_id=str(uuid.uuid4()))
    cap = qa.CapturingEmitter(ctx.session)
    ctx.emitter = cap
    context_var.set(ctx)

    async with cl.Step(name="think", type="llm") as s:
        s.output = "初始推理"
    # 模拟 ReasoningStep 被 close 时 output 被重置为 ""
    cap._record({"id": s.id, "type": "llm", "output": ""})

    texts = cap.reasoning_texts()
    # 统一语义后：总是取最后值，所以 "" 覆盖了 "初始推理"
    assert "初始推理" not in texts


async def test_capturing_emitter_skips_deleted_steps():
    """delete_step 标记的 id 在归并时应被跳过。"""
    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    _skip_if_chainlit_stubbed()

    ctx = init_http_context(thread_id=str(uuid.uuid4()))
    cap = qa.CapturingEmitter(ctx.session)
    ctx.emitter = cap
    context_var.set(ctx)

    m = cl.Message(content="先 send 一个消息")
    await m.send()
    await cap.delete_step({"id": m.id})
    m2 = cl.Message(content="这是最终消息")
    await m2.send()

    merged = qa.merge_answers(cap.answer_steps, deleted_ids=cap._deleted_ids)
    assert "先 send 一个消息" not in merged
    assert "这是最终消息" in merged


async def test_capturing_emitter_record_is_immune_to_downstream_mutation():
    """_record 必须做 dict.copy，否则下游 mutation 会污染记录。"""
    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    _skip_if_chainlit_stubbed()

    ctx = init_http_context(thread_id=str(uuid.uuid4()))
    cap = qa.CapturingEmitter(ctx.session)
    ctx.emitter = cap
    context_var.set(ctx)

    d = {"id": "m1", "type": "assistant_message", "output": "原始"}
    cap._record(d)
    d["output"] = "被篡改"

    assert cap.answer_steps[0]["output"] == "原始", "下游 mutation 污染了记录"


async def test_capturing_emitter_separates_answer_from_reasoning():
    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    _skip_if_chainlit_stubbed()
    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    _skip_if_chainlit_stubbed()

    ctx = init_http_context(thread_id=str(uuid.uuid4()))
    cap = qa.CapturingEmitter(ctx.session)
    ctx.emitter = cap
    context_var.set(ctx)

    await cl.Message(content="这是最终答案").send()
    async with cl.Step(name="🤔 思考过程") as s:
        s.output = "这是推理过程"

    assert qa.merge_answers(cap.answer_steps) == "这是最终答案"
    assert any("推理过程" in str(t) for t in cap.reasoning_texts())
    assert "这是最终答案" not in "".join(cap.reasoning_texts())


async def test_capturing_emitter_collects_images():
    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    ctx = init_http_context(thread_id=str(uuid.uuid4()))
    cap = qa.CapturingEmitter(ctx.session)
    ctx.emitter = cap
    context_var.set(ctx)

    await cl.Message(
        content="图表",
        elements=[cl.Image(content=_PNG, name="chart_0")],
    ).send()

    assert len(cap.elements) == 1
    assert cap.elements[0]["name"] == "chart_0"
    assert cap.elements[0]["mime"] == "image/png"


async def test_capturing_emitter_collects_gis_packets():
    import json

    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    ctx = init_http_context(thread_id=str(uuid.uuid4()))
    cap = qa.CapturingEmitter(ctx.session)
    ctx.emitter = cap
    context_var.set(ctx)

    packet = {"type": "gis_linkage", "schema_version": "v2", "scene": "river"}
    await cl.send_window_message(json.dumps(packet, ensure_ascii=False))

    assert cap.gis_packets == [packet]


async def test_capturing_emitter_ignores_non_json_window_message():
    """非 JSON 的 window message 不应让整个请求崩掉。"""
    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    ctx = init_http_context(thread_id=str(uuid.uuid4()))
    cap = qa.CapturingEmitter(ctx.session)
    ctx.emitter = cap
    context_var.set(ctx)

    await cl.send_window_message("这不是 JSON")
    assert cap.gis_packets == []


# ---------------------------------------------------------------- 并发隔离


async def test_concurrent_requests_are_isolated():
    """并发请求的 session / emitter / 答案必须完全隔离。"""
    import chainlit as cl
    from chainlit.context import context_var, init_http_context

    async def one(tag, delay):
        ctx = init_http_context(thread_id=str(uuid.uuid4()))
        cap = qa.CapturingEmitter(ctx.session)
        ctx.emitter = cap
        context_var.set(ctx)

        cl.user_session.set("tag", tag)
        await asyncio.sleep(delay)  # 交错执行

        await cl.Message(content=f"answer-{tag}").send()
        return tag, cl.user_session.get("tag"), ctx.session.id, qa.merge_answers(cap.answer_steps)

    results = await asyncio.gather(one("A", 0.03), one("B", 0.01), one("C", 0.02))

    for tag, seen_tag, _sid, answer in results:
        assert seen_tag == tag, f"{tag} 的 session 状态被串扰"
        assert answer == f"answer-{tag}", f"{tag} 的答案被串扰"
    assert len({sid for _, _, sid, _ in results}) == 3, "session 未隔离"


# ---------------------------------------------------------------- 资源回收


async def test_single_turn_response_cache_hits_same_question(monkeypatch):
    """单轮相同问题第二次命中缓存，不重复跑完整问答。"""
    rt = qa.QARuntime()

    async def factory():
        return {"planner_chain": None, "answer_chain": None, "thinking_chain": None, "tools": [], "callbacks": {}}

    rt.configure(factory)
    calls = {"n": 0}

    async def fake_run_once(*a, **k):
        calls["n"] += 1
        return {"answer": "固定答案", "conversation_id": str(uuid.uuid4()), "images": [], "gis": [], "reasoning": [], "elapsed_seconds": 0.1}

    monkeypatch.setattr(rt, "_run_once", fake_run_once)

    r1 = await rt.ask("今天天气")
    r2 = await rt.ask("今天天气")
    assert calls["n"] == 1, f"第二次应命中缓存，实际 {calls['n']} 次"
    assert r1["answer"] == r2["answer"] == "固定答案"


async def test_single_turn_response_cache_distinct_questions_do_not_share(monkeypatch):
    """不同问题不互相命中缓存。"""
    rt = qa.QARuntime()

    async def factory():
        return {"planner_chain": None, "answer_chain": None, "thinking_chain": None, "tools": [], "callbacks": {}}

    rt.configure(factory)
    calls = {"n": 0}

    async def fake_run_once(*a, **k):
        calls["n"] += 1
        return {"answer": f"答案-{calls['n']}", "conversation_id": str(uuid.uuid4()), "images": [], "gis": [], "reasoning": [], "elapsed_seconds": 0.1}

    monkeypatch.setattr(rt, "_run_once", fake_run_once)

    await rt.ask("今天天气")
    await rt.ask("明天天气")
    assert calls["n"] == 2, f"不同问题应各跑一次，实际 {calls['n']} 次"


async def test_multi_turn_requests_are_not_cached(monkeypatch):
    """多轮请求（带 conversation_id）不缓存，保证上下文正确。"""
    rt = qa.QARuntime()

    async def factory():
        return {"planner_chain": None, "answer_chain": None, "thinking_chain": None, "tools": [], "callbacks": {}}

    rt.configure(factory)
    calls = {"n": 0}

    async def fake_run_once(*a, **k):
        calls["n"] += 1
        return {"answer": "答案", "conversation_id": str(uuid.uuid4()), "images": [], "gis": [], "reasoning": [], "elapsed_seconds": 0.1}

    monkeypatch.setattr(rt, "_run_once", fake_run_once)

    cid = str(uuid.uuid4())
    await rt.ask("今天天气", conversation_id=cid)
    await rt.ask("今天天气", conversation_id=cid)
    assert calls["n"] == 2, f"多轮请求不应命中缓存，实际 {calls['n']} 次"


async def test_release_chainlit_session_reclaims_global_state():
    """HTTP 会话必须手动回收：Chainlit 只在 websocket 断开时清理。

    不回收会让 user_sessions / chat_contexts 无界增长（实测 ~5 KB/请求），
    生产上几万请求即 GB 级，最终 OOM。
    """
    import chainlit as cl
    from chainlit.chat_context import chat_contexts
    from chainlit.context import context_var, init_http_context
    from chainlit.user_session import user_sessions

    ctx = init_http_context(thread_id=str(uuid.uuid4()))
    ctx.emitter = qa.CapturingEmitter(ctx.session)
    context_var.set(ctx)
    sid = ctx.session.id

    cl.user_session.set("messages", ["很长的工具返回" * 100])
    assert sid in user_sessions

    qa._release_chainlit_session(sid)

    assert sid not in user_sessions, "user_sessions 未回收"
    assert sid not in chat_contexts, "chat_contexts 未回收"


def test_release_chainlit_session_is_safe_on_unknown_id():
    qa._release_chainlit_session(str(uuid.uuid4()))  # 不应抛异常


# ---------------------------------------------------------------- 脱敏


@pytest.mark.parametrize(
    "raw, forbidden",
    [
        ("连接 10.226.188.156:8000 失败", "10.226"),
        (r"文件 D:\PythonProject\haihe\secret.py 不存在", "D:\\"),
        ("postgresql+asyncpg://u:p@10.226.107.130:5432/db 超时", "postgresql"),
        ("/home/user/data/x.nc 读取失败", "/home/"),
        ("/var/log/app.log 权限不足", "/var/"),
    ],
)
def test_scrub_removes_internal_details(raw, forbidden):
    """内网地址与本地路径不得进入响应。

    HTTP 接口面向外部客户端（天河小程序），而 message_orchestrator 的
    reasoning.line 会写入**未脱敏的原始异常**，所以出口必须再过一道。
    """
    out = qa._scrub(raw)
    assert forbidden not in out, f"脱敏后仍含 {forbidden}：{out}"


def test_scrub_keeps_normal_text_intact():
    text = "海河流域明天多云，局地小雨，降雨量 12.5 毫米。"
    assert qa._scrub(text) == text


def test_scrub_handles_empty():
    assert qa._scrub("") == ""
    assert qa._scrub(None) is None
