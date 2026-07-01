import chainlit as cl
from langchain_core.messages import ToolMessage, HumanMessage, AIMessage


def _find_tool(tools, tool_name: str):
    return next((t for t in tools if t.name == tool_name), None)


def _friendly_llm_error_text(err: Exception) -> str:
    t = str(err)
    lower_t = t.lower()
    if "arrearage" in lower_t or "overdue-payment" in lower_t:
        return "❌ 当前大模型服务不可用：账户欠费或已停用（Arrearage）。请先在阿里云百炼控制台完成续费/结清后重试。"
    if "access denied" in lower_t or "api_key" in lower_t or "unauthorized" in lower_t:
        return "❌ 当前大模型服务鉴权失败。请检查 API Key 是否正确、是否过期以及对应模型权限。"
    return f"❌ 大模型调用失败：{t}"


def _nearest_valid_hour(hour_value: int) -> int:
    valid_hours = [2, 8, 14, 20]
    return min(valid_hours, key=lambda h: abs(h - hour_value))


def _build_hour_tolerant_args(tool_args):
    if not isinstance(tool_args, dict):
        return None, None, None

    candidate_keys = []
    for key, value in tool_args.items():
        if not isinstance(key, str):
            continue
        if key == "hour" or key.endswith("_hour"):
            if isinstance(value, int):
                candidate_keys.append((key, value))
            elif isinstance(value, str) and value.strip().isdigit():
                candidate_keys.append((key, int(value.strip())))

    if not candidate_keys:
        return None, None, None

    # 优先修正最常见的 hour 参数
    key, old_hour = sorted(candidate_keys, key=lambda kv: (0 if kv[0] == "hour" else 1, kv[0]))[0]
    new_hour = _nearest_valid_hour(old_hour)
    if new_hour == old_hour:
        return None, None, None

    new_args = dict(tool_args)
    new_args[key] = new_hour
    return new_args, old_hour, new_hour


async def _invoke_tool_with_tolerance(tool_name: str, tool, tool_args, step):
    try:
        return await tool.ainvoke(tool_args)
    except Exception as e:
        err_text = str(e)
        if tool_name != "get_city_rainfall_time_range" or "hour%6==2" not in err_text:
            raise

        retry_args, old_hour, new_hour = _build_hour_tolerant_args(tool_args)
        if not retry_args:
            raise

        step.input += (
            f"⚠️ 检测到 `{tool_name}` 小时参数不合法：{old_hour}，"
            f"已自动纠偏为 {new_hour} 并重试。\n"
        )
        print(f"[容错重试] {tool_name}: hour {old_hour} -> {new_hour}")
        return await tool.ainvoke(retry_args)


async def _render_river_plot_with_overlay(tools, river_observation, river_name: str, callbacks):
    admin_observation = await callbacks["build_admin_overlay_for_plot"](tools, river_observation)
    await callbacks["render_and_send_plot"](
        river_observation,
        title_suffix=river_name,
        admin_raw_result=admin_observation,
    )


async def _try_river_plot_fast_path(user_text: str, tools, messages, callbacks) -> bool:
    if not callbacks["need_river_plot"](user_text):
        return False

    try:
        river_tool = _find_tool(tools, "get_river_network_for_plot")
        if not river_tool:
            return False

        river_name = callbacks["extract_river_name"](user_text)
        river_observation = await river_tool.ainvoke({"start_river": river_name})
        await _render_river_plot_with_overlay(tools, river_observation, river_name, callbacks)

        brief = callbacks["build_river_network_brief"](river_observation, river_name)
        brief = callbacks["append_followup_if_needed"](brief, user_text)
        await callbacks["stream_text_to_message"](brief)

        messages.append(HumanMessage(content=user_text))
        messages.append(AIMessage(content=brief))
        cl.user_session.set("messages", messages)
        return True
    except Exception as e:
        print(f"河网快路径失败，回退到通用流程：{e}")
        return False


async def _try_manual_plot_fallback(user_text: str, tools, stream_msg: cl.Message, callbacks) -> bool:
    try:
        river_tool = _find_tool(tools, "get_river_network_for_plot")
        if not river_tool:
            return False

        river_name = callbacks["extract_river_name"](user_text)
        river_observation = await river_tool.ainvoke({"start_river": river_name})
        await _render_river_plot_with_overlay(tools, river_observation, river_name, callbacks)

        if stream_msg.content.strip():
            await stream_msg.remove()
            stream_msg = cl.Message(content="")
            await stream_msg.send()

        fallback_text = (
            f"已生成 `{river_name}` 河网图。请按下方图进行快速研判：\n\n"
            "| 关注点 | 现状 | 风险等级 |\n"
            "| :--- | :--- | :--- |\n"
            "| 主干与支流关系 | 已在图中标注 | 中 |\n"
            "| 下游传导路径 | 已在图中可见 | 中 |\n"
            "| 重点防守河段 | 需结合实时雨情复核 | 高 |\n\n"
            "建议：1) 盯主干交汇口 2) 盯低洼易涝段 3) 每30分钟滚动复核。\n\n"
            f"{'' if callbacks['user_forbids_followup'](user_text) else callbacks['make_followup_question'](user_text)}"
        )
        await callbacks["stream_text_to_message"](fallback_text, stream_msg=stream_msg)
        return True
    except Exception as e:
        print(f"河网图兜底绘制失败：{e}")
        return False


async def _run_tool_round(planner_msg, tools, messages, user_text: str, iteration: int, callbacks):
    ree = None
    forced_final_text = None
    gis_linkage_sent = False
    print(f"\n=== 第 {iteration} 轮工具调用 ===")

    async with cl.Step(name="step") as step:
        tool_call_summary = f"**正在调用气象分析工具... (第 {iteration} 轮)**\n\n"
        for i, tool_call in enumerate(planner_msg.tool_calls, 1):
            tool_call_summary += f"🔧 工具 {i}: `{tool_call['name']}`\n"
            tool_call_summary += f"📋 参数：{tool_call['args']}\n\n"

        step.input = tool_call_summary
        print(f"\n=== 准备执行工具 ===")
        print(f"工具列表：{[tc['name'] for tc in planner_msg.tool_calls]}")
        print(f"====================\n")

        for tool_call in planner_msg.tool_calls:
            tool_name = tool_call["name"]
            tool_args = tool_call["args"]
            tool = _find_tool(tools, tool_name)
            step.input += f"🔄 正在执行 `{tool_name}`...\n"

            if tool is None:
                observation_text = f"工具未找到：{tool_name}"
                messages.append(ToolMessage(content=observation_text, tool_call_id=tool_call["id"], role="tool"))
                step.input += f"❌ `{tool_name}` 不存在\n"
                continue

            try:
                observation = await _invoke_tool_with_tolerance(tool_name, tool, tool_args, step)
                if tool_name == "analyze_rainstorm_impact":
                    observation = await callbacks["enrich_with_impact_time_tool"](
                        observation=observation,
                        tool_args=tool_args,
                        tools=tools,
                        step=step,
                    )
                maybe_send_gis = callbacks.get("send_gis_linkage")
                if maybe_send_gis:
                    try:
                        await maybe_send_gis(
                            tool_name=tool_name,
                            tool_args=tool_args,
                            observation=observation,
                            user_text=user_text,
                            tools=tools,
                        )
                        gis_linkage_sent = True
                        

                    except Exception as gis_err:
                        # GIS 联动失败不应中断主问答流程
                        print(f"[GIS联动] 发送失败：{gis_err}")
                if tool_name == "analyze_rainstorm_impact" and callbacks["should_force_admin_units_reply"](user_text):
                    forced_final_text = callbacks["build_admin_units_only_reply"](observation)
                elif tool_name == "analyze_rainstorm_impact" and callbacks["should_force_partition_table_reply"](user_text):
                    forced_final_text = callbacks["build_partition_only_reply"](observation)
                elif tool_name == "analyze_rainstorm_impact" and callbacks["should_force_structured_impact_reply"](user_text):
                    forced_final_text = callbacks["build_structured_impact_reply"](observation)

                if tool_name == "get_river_network_for_plot":
                    river_name = tool_args.get("start_river", "全流域")
                    try:
                        await _render_river_plot_with_overlay(tools, observation, river_name, callbacks)
                    except Exception as e:
                        print(f"加载行政区划底图失败：{e}")
                        await callbacks["render_and_send_plot"](observation, title_suffix=river_name, admin_raw_result=None)

                    observation_text = (
                        f"（系统消息：已成功在前端为用户绘制了 {river_name} 的"
                        f"河网可视化图，并叠加行政区划底图。不要输出坐标数据，请继续用自然语言回答分析结果）"
                    )
                else:
                    observation_text = callbacks["tool_observation_to_text"](observation)

                step.input += "📊 **工具执行完毕**\n"
            except Exception as e:
                await cl.Message(
                    content="当前业务查询暂时失败，系统已记录本次请求。请稍后重试；如持续失败，请联系值班技术支持。"
                ).send()
                observation_text = f"工具执行失败：{str(e)}"

            messages.append(
                ToolMessage(
                    content=observation_text,
                    tool_call_id=tool_call["id"],
                    role="tool",
                )
            )

    if gis_linkage_sent:
        forced_final_text = "已为您将结果在地图中显示。"

    return forced_final_text,ree


async def process_message(message: cl.Message, planner_chain, answer_chain, tools, messages, callbacks):
    if await _try_river_plot_fast_path(message.content, tools, messages, callbacks):
        return

    messages.append(HumanMessage(content=message.content))

    stream_msg = cl.Message(content="")
    await stream_msg.send()

    try:
        planner_msg = await callbacks["ainvoke_chain"](planner_chain, {"messages": messages})
    except Exception as e:
        await cl.Message(content=_friendly_llm_error_text(e)).send()
        print(f"Planner 首轮调用失败：{e}")
        cl.user_session.set("messages", messages)
        return
    print(f"\n=== 第一次 Planner 调用结果 ===")
    print(f"Planner Message: {planner_msg}")
    print(f"Tool Calls: {planner_msg.tool_calls}")
    print(f"Content: {planner_msg.content}")
    print(f"========================\n")

    used_manual_plot_fallback = False

    if (not planner_msg.tool_calls) and callbacks["need_river_plot"](message.content):
        used_manual_plot_fallback = await _try_manual_plot_fallback(message.content, tools, stream_msg, callbacks)

    if not planner_msg.tool_calls:
        maybe_send_gis = callbacks.get("send_gis_linkage")
        guess_scene = callbacks.get("guess_gis_scene")
        if maybe_send_gis and guess_scene:
            try:
                scene = guess_scene(message.content)
                if scene in {"realtime_station", "emergency_rivers", "emergency_districts"}:
                    # 兜底：即使本轮未触发工具，也尝试按场景下发 GIS 联动包（会走本地 JSON 回退）。
                    await maybe_send_gis(
                        tool_name="local_scene_export_probe",
                        tool_args={},
                        observation={},
                        user_text=message.content,
                        tools=tools,
                    )
                    text = "已为您将结果在地图中显示。"
                    await callbacks["stream_text_to_message"](text, stream_msg=stream_msg)
                    messages.append(AIMessage(content=text))
                    cl.user_session.set("messages", messages)
                    return
            except Exception as gis_err:
                print(f"[GIS联动-无工具兜底] 发送失败：{gis_err}")

        if used_manual_plot_fallback:
            messages.append(AIMessage(content=stream_msg.content))
            cl.user_session.set("messages", messages)
            return

        try:
            answer_msg = await callbacks["ainvoke_chain"](answer_chain, {"messages": messages})
        except Exception as e:
            await cl.Message(content=_friendly_llm_error_text(e)).send()
            print(f"Answer 首轮调用失败：{e}")
            cl.user_session.set("messages", messages)
            return
        text = getattr(answer_msg, "content", None) or getattr(planner_msg, "content", None) or ""
        text = callbacks["append_followup_if_needed"](text, message.content)
        if text:
            await callbacks["stream_text_to_message"](text, stream_msg=stream_msg)
        messages.append(AIMessage(content=text))
        cl.user_session.set("messages", messages)
        return

    messages.append(planner_msg)

    max_iterations = 5
    iteration = 0

    while planner_msg.tool_calls and iteration < max_iterations:
        iteration += 1
        forced_final_text,ree = await _run_tool_round(planner_msg, tools, messages, message.content, iteration, callbacks)
        if ree:
            await cl.send_window_message(ree)

        if forced_final_text:
            await callbacks["stream_text_to_message"](forced_final_text, stream_msg=stream_msg)
            messages.append(AIMessage(content=forced_final_text))
            print("\n=== 使用定制化收口答案，退出循环 ===\n")
            break

        print(f"\n=== 第 {iteration} 轮 Planner 调用前 ===")
        print(f"Messages 数量：{len(messages)}")
        for i, msg in enumerate(messages):
            print(f"Message {i}: {type(msg).__name__} - {str(msg)[:100]}")
        print("======================\n")

        try:
            planner_msg = await callbacks["ainvoke_chain"](planner_chain, {"messages": messages})
            print(f"\n=== 第 {iteration} 轮 Planner 调用结果 ===")
            print(f"Planner Message: {planner_msg}")
            print(f"Tool Calls: {planner_msg.tool_calls if hasattr(planner_msg, 'tool_calls') else 'N/A'}")
            print(f"Content: {planner_msg.content}")
            print(f"========================\n")

            if not planner_msg.tool_calls:
                try:
                    answer_msg = await callbacks["ainvoke_chain"](answer_chain, {"messages": messages})
                except Exception as e:
                    await cl.Message(content=_friendly_llm_error_text(e)).send()
                    print(f"Answer 循环调用失败：{e}")
                    break
                text = getattr(answer_msg, "content", None) or getattr(planner_msg, "content", None) or ""
                text = callbacks["append_followup_if_needed"](text, message.content)
                if text:
                    await callbacks["stream_text_to_message"](text, stream_msg=stream_msg)
                messages.append(AIMessage(content=text))
                print("\n=== 回答器已生成最终回答，退出循环 ===\n")
                break

            messages.append(planner_msg)

            if planner_msg.tool_calls and stream_msg.content.strip():
                await stream_msg.remove()
                stream_msg = cl.Message(content="")
                await stream_msg.send()

        except Exception as e:
            error_msg = _friendly_llm_error_text(e)
            await cl.Message(content=error_msg).send()
            print(f"LLM 调用失败：{e}")
            print(f"Messages 内容：{messages}")
            break

    cl.user_session.set("messages", messages)
