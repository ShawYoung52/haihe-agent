import streamlit as st

st.title("海河流域智能体")
prompt = st.chat_input("请输入您的要求")

from openai import OpenAI

client = OpenAI(
    # 若没有配置环境变量，请用百炼API Key将下行替换为：api_key="sk-xxx"
    api_key="sk-e23c088e4639437b912a624c82801400",
    base_url="https://dashscope.aliyuncs.com/api/v2/apps/protocols/compatible-mode/v1",
)

response = client.responses.create(

)

if prompt:
    user_messagee = st.chat_message("human")
    user_messagee.write(prompt)

    ai_message = st.chat_message("ai")

    ai_reply = client.responses.create(
        model="qwen3-max-2026-01-23",
        input=prompt
    )

    ai_message.write(ai_reply.output_text)
