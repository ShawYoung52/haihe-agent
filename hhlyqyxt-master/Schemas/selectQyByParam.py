from pydantic import BaseModel, Field


class SelectQyByParam(BaseModel):
    starttime: str = Field(
        default="2025-07-29 02:00:00", description="查询开始时间，格式为2026-05-02 00:00:00"
    )
    endtime: str = Field(
        default="2025-07-30 02:00:00", description="查询结束时间，格式为2026-05-02 00:00:00"
    )
