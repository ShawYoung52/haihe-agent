#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信发送 HTTP 接口
"""

import sys
import ctypes

if sys.platform == "win32":
    try:
        # 设置每显示器 DPI 感知（v2），解决 125% 等缩放导致 pyautogui 点击坐标偏移
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from starlette.concurrency import run_in_threadpool

from send_wechat import send_message, send_batch

app = FastAPI(title="WeChat Send API", version="1.0.0")


class SendRequest(BaseModel):
    to: str = Field(..., description="联系人名称，微信搜索框能搜到的名字")
    message: str = Field(..., description="要发送的文本消息")
    strategy: str = Field("ocr", description='匹配策略："ocr" 或数字字符串')


class BatchSendRequest(BaseModel):
    to: List[str] = Field(..., description="联系人名称列表")
    message: str = Field(..., description="要发送的文本消息")
    strategy: str = Field("ocr", description='匹配策略："ocr" 或数字字符串')


class SendResponse(BaseModel):
    success: bool
    to: Optional[str] = None
    message: Optional[str] = None
    detail: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/send", response_model=SendResponse)
async def send_single(req: SendRequest):
    """单条发送接口。"""
    if not req.to or not req.message:
        raise HTTPException(status_code=400, detail="to 和 message 不能为空")

    # UI 自动化是阻塞操作，放到线程池执行
    ok = await run_in_threadpool(send_message, req.to, req.message, req.strategy)

    if not ok:
        raise HTTPException(status_code=500, detail="发送失败，请检查微信窗口和联系人名称")

    return SendResponse(success=True, to=req.to, message=req.message)


@app.post("/send/batch")
async def send_batch_endpoint(req: BatchSendRequest):
    """批量发送接口。"""
    if not req.to or not req.message:
        raise HTTPException(status_code=400, detail="to 和 message 不能为空")

    contacts = [(name, req.strategy) for name in req.to]
    result = await run_in_threadpool(send_batch, contacts, req.message)

    return {
        "success": result["success"] == result["total"],
        "result": result,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)