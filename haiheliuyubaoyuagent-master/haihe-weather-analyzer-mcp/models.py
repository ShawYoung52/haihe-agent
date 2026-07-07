"""降雨数据模型定义"""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class RainfallData(BaseModel):
    """降雨数据基础模型"""
    station_id: str = Field(..., description="站点ID")
    station_name: str = Field(..., description="站点名称")
    latitude: float = Field(..., description="纬度")
    longitude: float = Field(..., description="经度")
    rainfall_amount: float = Field(..., description="降雨量(mm)")
    measurement_time: datetime = Field(..., description="测量时间")
    data_quality: str = Field(default="good", description="数据质量")


class RainfallCityData(BaseModel):
    """城市降雨详细数据模型 - 包含栅格统计信息"""
    city_name: str = Field(..., description="城市名称")
    time_period: str = Field(..., description="时间范围描述")
    total_grid_points: int = Field(..., description="总栅格点数")
    average_rainfall_mm: float = Field(..., description="平均降雨量(mm)")
    max_rainfall_mm: float = Field(..., description="最大降雨量(mm)")
    min_rainfall_mm: float = Field(..., description="最小降雨量(mm)")
    processed_files: int = Field(..., description="处理的文件数量")
    data_source: str = Field(default="ec", description="数据来源")


class TimeRangeQuery(BaseModel):
    """时间范围查询参数"""
    start_time: datetime = Field(..., description="开始时间")
    end_time: datetime = Field(..., description="结束时间")
    station_ids: Optional[List[str]] = Field(None, description="站点ID列表")


class LocationQuery(BaseModel):
    """位置查询参数"""
    latitude: float = Field(..., description="纬度")
    longitude: float = Field(..., description="经度")
    radius_km: float = Field(default=10.0, description="搜索半径(公里)")


class StatisticalResult(BaseModel):
    """统计结果模型"""
    total_stations: int = Field(..., description="总站点数")
    average_rainfall: float = Field(..., description="平均降雨量(mm)")
    max_rainfall: float = Field(..., description="最大降雨量(mm)")
    min_rainfall: float = Field(..., description="最小降雨量(mm)")
    total_rainfall: float = Field(..., description="总降雨量(mm)")
    time_period: str = Field(..., description="时间周期")


class RegionalAnalysis(BaseModel):
    """区域分析结果"""
    region_name: str = Field(..., description="区域名称")
    stations: List[RainfallData] = Field(..., description="包含的站点数据")
    statistics: StatisticalResult = Field(..., description="统计信息")
    risk_level: str = Field(..., description="风险等级(high/medium/low)")


class ForecastData(BaseModel):
    """预报数据模型"""
    forecast_time: datetime = Field(..., description="预报时间")
    predicted_rainfall: float = Field(..., description="预测降雨量(mm)")
    confidence_level: float = Field(..., description="置信度(0-1)")
    weather_condition: str = Field(..., description="天气状况")


class AlertInfo(BaseModel):
    """预警信息模型"""
    alert_level: str = Field(..., description="预警级别(red/orange/yellow/blue)")
    affected_areas: List[str] = Field(..., description="影响区域")
    start_time: datetime = Field(..., description="预警开始时间")
    end_time: datetime = Field(..., description="预警结束时间")
    description: str = Field(..., description="预警描述")
