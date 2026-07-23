from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from utils.send_wx_message import send_message

wxrouter = APIRouter(
    prefix='/wx',
    tags=['wx']
)

class SendRequest(BaseModel):
    to: str = Field(..., description="联系人名称，微信搜索框能搜到的名字")
    message: str = Field(..., description="要发送的文本消息")
    strategy: str = Field("ocr", description='匹配策略："ocr" 或数字字符串')

class SendResponse(BaseModel):
    success: bool
    to: Optional[str] = None
    message: Optional[str] = None
    detail: Optional[str] = None

@wxrouter.post('/send_wx_message')
async def send_wx_message(req: SendRequest):
    """单条发送接口。"""
    if not req.to or not req.message:
        raise HTTPException(status_code=400, detail="to 和 message 不能为空")

    # UI 自动化是阻塞操作，放到线程池执行
    ok = await run_in_threadpool(send_message, req.to, req.message, req.strategy)

    if not ok:
        raise SendResponse(success=True,message="发送失败，请检查微信窗口和联系人名称")

    return SendResponse(success=True, to=req.to, message=req.message)