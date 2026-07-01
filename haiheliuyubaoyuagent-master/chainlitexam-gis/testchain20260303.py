import asyncio

from langchain_community.chat_models import ChatTongyi
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.prebuilt import create_react_agent
import chainlit as cl

# 配置 LLM（开启 streaming）
llm =  ChatTongyi(
        model="qwen-turbo",     # 或 qwen-plus / qwen-max
        streaming=True,         # 关键：开启流式
        temperature=0.7,
        api_key="sk-40c16d460ec44feb91006524c12ad8b2"
    )


@cl.on_chat_start
async def start():
    # 1. 连接你的 FastMCP Server (假设运行在 http://localhost:8000)
    # 你也可以使用 stdio 连接方式
    client = MultiServerMCPClient(
        {
            "weather": {
                "transport": "sse",
                "url": "http://10.241.167.159:3333/sse",
            }
        }
    )
    # tools = await load_mcp_tools(client)
    tools = await client.get_tools()

    # 2. 创建智能体
    # 此智能体能够自动循环：当 LLM 发现需要工具时，它会调用并把结果喂回给 LLM
    agent_executor = create_react_agent(llm, tools,prompt="""
    
    """)

    cl.user_session.set("agent", agent_executor)
    cl.author_names = {"Assistant": "AI Agent"}


@cl.on_message
async def main(message: cl.Message):
    agent = cl.user_session.get("agent")

    # 创建一个 Chainlit 消息容器用于流式展示
    final_answer = cl.Message(content="")

    # 使用 astream_events 捕获所有事件 (包括工具调用和 Token)
    async for event in agent.astream_events(
            {"messages": [("system",
     """你是一个专业的数据分析助手。

如果工具返回的是结构化数据：
1. 不要原样输出 JSON
2. 必须整理成自然语言报告

## 预报降水情况
- 回答白天调用工具自动把开始时间设置日期的凌晨5点开始
- 回答夜间调用工具自动把开始时间设置日期的凌晨20点开始
- 回答时要完整说明工具的返回结果，不能省略字段，例如数据来源等
"""),("user", message.content)]},
            version="v2"
    ):
        kind = event["event"]

        # 处理工具调用开始（让用户知道智能体在做事）
        if kind == "on_tool_start":
            # await cl.Message(
            #     content=f"🛠️ 正在调用工具: {event['name']}...",
            #     parent_id=message.id
            # ).send()
            pass

        # 处理工具返回结果（结构化数据处理）
        # LLM 会在下一次迭代中自动总结这些数据
        elif kind == "on_tool_end":
            # await cl.Message(
            #     content=f"✅ 工具 {event['name']} 返回了数据。",
            #     parent_id=message.id
            # ).send()
            pass
        # 处理流式文本输出
        elif kind == "on_chat_model_stream":
            content = event["data"]["chunk"].content
            if content:
                await final_answer.stream_token(content)

    await final_answer.send()