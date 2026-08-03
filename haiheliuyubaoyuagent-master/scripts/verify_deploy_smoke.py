"""部署后冒烟脚本：验证 HTTP 问答接口可正常服务。

用法：
    cd chainlitexam
    <venv>/python.exe ../scripts/verify_deploy_smoke.py

或指定地址:
    <venv>/python.exe ../scripts/verify_deploy_smoke.py --base-url http://10.xx.xx.xx:8000

不传 --base-url 则用假 chain 本地验证（不调真实接口、不依赖内网连通）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "chainlitexam"))


def _test_local_modules():
    """第 0 步：新模块可正常 import。"""
    import qa_http_api as qa

    print("✓ qa_http_api 可正常 import")
    assert qa.MAX_CONCURRENCY >= 1
    assert qa.TIMEOUT_SECONDS >= 1
    print("✓ 配置常量校验通过")


async def _test_local_full_flow():
    """本地冒烟：假 chain 走完整 ask() 路径。

    不调真实 LLM/工具，只在本地验证「接口能启动 + 能返回合法 JSON」。
    """
    import asyncio
    import uuid

    import qa_http_api as qa
    from langchain_core.messages import AIMessage

    class FakeChain:
        async def ainvoke(self, i, config=None):
            await asyncio.sleep(0.005)
            return AIMessage(content="海河流域明天多云，局地小雨。")

    async def _stream(text, stream_msg=None, chunk_size=32, delay_ms=None):
        import chainlit as cl

        m = stream_msg or cl.Message(content="")
        m.content = text
        await (m.update() if stream_msg else m.send())

    def cbs():
        async def ai(c, i, config=None):
            return await c.ainvoke(i, config)

        async def pl(c, i, rs, config=None):
            return await c.ainvoke(i, config)

        async def th(c, i, rs, config=None):
            return ""

        async def an(c, i, sm, config=None):
            r = await c.ainvoke(i, config)
            sm.content = r.content
            await sm.update()
            return r.content

        return {
            "need_river_plot": lambda t: False,
            "extract_river_name": lambda t: "",
            "build_admin_overlay_for_plot": None,
            "render_and_send_plot": None,
            "build_river_network_brief": lambda a, b: "",
            "append_followup_if_needed": lambda t, u: t,
            "stream_text_to_message": _stream,
            "user_forbids_followup": lambda t: True,
            "make_followup_question": lambda t: "",
            "ainvoke_chain": ai,
            "astream_planner_think": pl,
            "astream_thinking_to_reasoning": th,
            "astream_answer_chain_to_message": an,
            "should_force_admin_units_reply": lambda t: False,
            "should_force_partition_table_reply": lambda t: False,
            "should_force_structured_impact_reply": lambda t: False,
            "build_admin_units_only_reply": lambda o: None,
            "build_partition_only_reply": lambda o: None,
            "build_structured_impact_reply": lambda o: None,
            "enrich_with_impact_time_tool": None,
            "tool_observation_to_text": lambda o: str(o),
            "send_gis_linkage": None,
        }

    rt = qa.QARuntime()

    async def factory():
        c = FakeChain()
        return {
            "planner_chain": c,
            "answer_chain": c,
            "thinking_chain": c,
            "tools": [],
            "callbacks": cbs(),
        }

    rt.configure(factory)

    # 单轮
    r = await rt.ask("海河流域明天天气怎么样？")
    assert r.get("answer") and "多云" in r["answer"], f"答案异常：{r}"
    assert r.get("conversation_id") and qa._UUID_RE.match(r["conversation_id"])
    assert isinstance(r.get("images"), list)
    assert isinstance(r.get("reasoning"), list)
    assert r.get("elapsed_seconds", 0) > 0
    print(f"✓ 单轮问答通过 ({r['elapsed_seconds']}s)")

    # 多轮
    cid = r["conversation_id"]
    r2 = await rt.ask("那后天呢？", conversation_id=cid)
    assert r2["conversation_id"] == cid
    print("✓ 多轮问答通过")

    # 图片路径安全
    import uuid as _uuid

    for sid, fid in [
        ("../etc", "passwd"),
        (str(_uuid.uuid4()), str(_uuid.uuid4()) + ".py"),
    ]:
        try:
            qa.resolve_file(sid, fid)
            assert False, f"应拒绝 {sid}/{fid}"
        except (qa.InvalidFileReference, qa.FileExpiredOrMissing):
            pass
    print("✓ 路径穿越防护通过")

    # 会话回收
    from chainlit.user_session import user_sessions
    from chainlit.chat_context import chat_contexts

    before = len(user_sessions), len(chat_contexts)
    for _ in range(5):
        await rt.ask("测试")
    after = len(user_sessions), len(chat_contexts)
    print(f"  user_sessions: {before[0]}→{after[0]}, chat_contexts: {before[1]}→{after[1]}")
    assert after[0] <= before[0] + 1, f"会话泄漏：{before[0]}→{after[0]}"
    print("✓ 会话回收通过")


async def _test_local():
    print("=== 本地冒烟测试 ===")
    _test_local_modules()
    await _test_local_full_flow()
    print("\n✓ 全部通过 —— 部署后可正常服务")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="部署后冒烟脚本")
    p.add_argument("--base-url", help="实际服务地址（如 http://10.xx.xx.xx:8000），不传则本地验证")
    args = p.parse_args()

    if args.base_url:
        print("远程模式尚未实现（待内网部署后补充）")
        print(f"请手工 curl 测试：curl -X POST {args.base_url}/api/v1/qa/ask -H 'Content-Type: application/json' -d '{{\"question\":\"测试\"}}'")
    else:
        import asyncio

        asyncio.run(_test_local())
