import configparser
import json
import logging
import random
from datetime import datetime, timedelta
from typing import List

from models import (
    RainfallData, TimeRangeQuery, LocationQuery,
    StatisticalResult, RegionalAnalysis, ForecastData, AlertInfo, RainfallCityData
)

logger = logging.getLogger(__name__)


def _rainfall_stats_from_values(values, min_exclude_zero: bool = False) -> dict:
    """从有效降雨像素值计算统计量。"""
    import numpy as np

    values = np.asarray(values)
    avg = round(float(values.mean()), 2)
    max_val = round(float(values.max()), 2)
    if min_exclude_zero:
        positive = values[values > 0]
        min_val = round(float(positive.min()), 2) if positive.size > 0 else 0.0
    else:
        min_val = round(float(values.min()), 2)
    return {
        "average_rainfall_mm": avg,
        "max_rainfall_mm": max_val,
        "min_rainfall_mm": min_val,
        "valid_count": int(values.size),
    }


def compute_rainfall_stats_for_geometry(
    geometry,
    raster_path: str,
    source_srid: int = 4326,
    min_exclude_zero: bool = False,
) -> dict:
    """计算指定矢量几何在栅格内的降雨统计量。

    Args:
        geometry: ogr.Geometry 多边形；为空时返回零值。
        raster_path: 降雨栅格文件路径。
        source_srid: 边界几何的 SRID，默认 4326。
        min_exclude_zero: 为 True 时最小值忽略 0（保持城市统计旧行为）。

    Returns:
        dict: 含 average_rainfall_mm、max_rainfall_mm、min_rainfall_mm、valid_count。
    """
    from osgeo import gdal, ogr, osr
    import numpy as np

    empty = {
        "average_rainfall_mm": 0.0,
        "max_rainfall_mm": 0.0,
        "min_rainfall_mm": 0.0,
        "valid_count": 0,
    }
    if geometry is None or geometry.IsEmpty():
        return empty

    dataset = gdal.Open(raster_path)
    if dataset is None:
        raise ValueError(f"无法打开栅格文件: {raster_path}")

    try:
        geotransform = dataset.GetGeoTransform()
        band = dataset.GetRasterBand(1)
        nodata_value = band.GetNoDataValue()
        raster_xsize = dataset.RasterXSize
        raster_ysize = dataset.RasterYSize

        mem_driver = gdal.GetDriverByName("MEM")
        mask_ds = mem_driver.Create("", raster_xsize, raster_ysize, 1, gdal.GDT_Byte)
        mask_ds.SetGeoTransform(geotransform)
        mask_ds.SetProjection(dataset.GetProjection())

        raster_srs = osr.SpatialReference()
        raster_srs.ImportFromWkt(dataset.GetProjection())
        try:
            raster_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except Exception:
            pass

        geom_to_rasterize = geometry.Clone()
        if source_srid and source_srid != 4326:
            source_srs = osr.SpatialReference()
            source_srs.ImportFromEPSG(source_srid)
            try:
                source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            except Exception:
                pass
            transform = osr.CoordinateTransformation(source_srs, raster_srs)
            geom_to_rasterize.Transform(transform)

        temp_driver = ogr.GetDriverByName("MEM")
        temp_datasource = temp_driver.CreateDataSource("temp")
        temp_layer = temp_datasource.CreateLayer(
            "temp_layer", srs=raster_srs, geom_type=ogr.wkbPolygon
        )
        layer_defn = temp_layer.GetLayerDefn()
        temp_feature = ogr.Feature(layer_defn)
        temp_feature.SetGeometry(geom_to_rasterize)
        temp_layer.CreateFeature(temp_feature)

        err = gdal.RasterizeLayer(
            dataset=mask_ds, bands=[1], layer=temp_layer, burn_values=[1]
        )
        if err != 0:
            raise ValueError(f"栅格化边界失败，错误码: {err}")

        mask_array = mask_ds.GetRasterBand(1).ReadAsArray()
        rainfall_array = band.ReadAsArray()

        nodata_mask = (
            rainfall_array != nodata_value
            if nodata_value is not None
            else True
        )
        valid_mask = (mask_array == 1) & nodata_mask & (~np.isnan(rainfall_array))
        valid_values = rainfall_array[valid_mask]
    finally:
        dataset = None

    if valid_values.size == 0:
        return empty
    return _rainfall_stats_from_values(valid_values, min_exclude_zero=min_exclude_zero)


def compute_rainfall_stats_for_raster(
    raster_path: str, min_exclude_zero: bool = False
) -> dict:
    """计算整张栅格的有效降雨统计量。"""
    from osgeo import gdal
    import numpy as np

    dataset = gdal.Open(raster_path)
    if dataset is None:
        raise ValueError(f"无法打开栅格文件: {raster_path}")

    try:
        band = dataset.GetRasterBand(1)
        nodata_value = band.GetNoDataValue()
        arr = band.ReadAsArray()
        valid = arr[~np.isnan(arr)]
        if nodata_value is not None:
            valid = valid[valid != nodata_value]
    finally:
        dataset = None

    if valid.size == 0:
        return {
            "average_rainfall_mm": 0.0,
            "max_rainfall_mm": 0.0,
            "min_rainfall_mm": 0.0,
            "valid_count": 0,
        }
    return _rainfall_stats_from_values(valid, min_exclude_zero=min_exclude_zero)


def find_ec_forecast_tif(
    ec_output_path: str, start_time: datetime, forecast_hours: int
) -> str | None:
    """在 ec_output_path 中查找匹配时间与时效的 EC AIFS tif 文件。"""
    import os

    time_str = start_time.strftime("%Y%m%d%H")
    pattern = f"ec_{time_str}_rain_total_{forecast_hours}h.tif"

    search_roots = [ec_output_path]
    linux_path = "/home/ev/data/ec/EC_AIFS/output"
    if os.path.isdir(linux_path) and linux_path != ec_output_path:
        search_roots.append(linux_path)
    env_root = os.environ.get("EC_AIFS_ROOT", "")
    if env_root and os.path.isdir(env_root) and env_root not in search_roots:
        search_roots.append(env_root)
    env_output = os.path.join(env_root, "output") if env_root else ""
    if env_output and os.path.isdir(env_output) and env_output not in search_roots:
        search_roots.append(env_output)

    for root_dir in search_roots:
        if not os.path.isdir(root_dir):
            continue
        for root, dirs, files in os.walk(root_dir):
            if pattern in files:
                return os.path.join(root, pattern)
    return None


def resolve_forecast_raster_path(
    forecast_hours: int, start_time: datetime, ec_output_path: str
) -> tuple[str | None, str]:
    """根据数据可用性选择滚动预报或 EC AIFS 栅格文件。

    Returns:
        (file_path, data_source_label)
    """
    import rolling_forecast_grid

    source_info = rolling_forecast_grid.resolve_forecast_grid_source(
        ec_output_path=ec_output_path
    )
    if source_info.get("source") == "rolling_forecast" and source_info.get("file"):
        nc_path = source_info["file"]
        try:
            materialized = rolling_forecast_grid.materialize_rolling_forecast_to_files(
                nc_path, [forecast_hours]
            )
            tiff_path = materialized.get(f"{forecast_hours}h")
            if tiff_path:
                cycle = source_info.get("cycle", "")
                return tiff_path, f"滚动预报网格（cycle={cycle}）"
        except Exception as exc:
            logger.warning("滚动预报切片失败，尝试 EC 兜底: %s", exc)

    ec_path = find_ec_forecast_tif(ec_output_path, start_time, forecast_hours)
    if ec_path:
        return ec_path, "ECMWF AIFS"

    return None, "ECMWF AIFS（无可用预报文件）"


class RainfallAnalyzer:
    """降雨数据分析器"""

    def __init__(self, config: configparser.ConfigParser):
        # 模拟站点数据
        self.config = config

    def _generate_mock_data(self, station_id: str, station_name: str,
                            lat: float, lon: float, hours_back: int = 24) -> List[RainfallData]:
        """生成模拟降雨数据"""
        data = []
        base_time = datetime.now() - timedelta(hours=hours_back)

        for i in range(hours_back):
            measurement_time = base_time + timedelta(hours=i)
            # 模拟降雨量数据（正态分布）
            rainfall = max(0, random.normalvariate(5, 3))  # 平均5mm，标准差3mm

            data.append(RainfallData(
                station_id=station_id,
                station_name=station_name,
                latitude=lat,
                longitude=lon,
                rainfall_amount=round(rainfall, 2),
                measurement_time=measurement_time,
                data_quality="good"
            ))

        return data

    def get_station_data(self, station_id: str, hours_back: int = 24) -> List[RainfallData]:
        """获取指定站点的历史降雨数据"""
        station = next((s for s in self.stations if s["id"] == station_id), None)
        if not station:
            raise ValueError(f"未找到站点: {station_id}")

        return self._generate_mock_data(
            station["id"], station["name"],
            station["lat"], station["lon"], hours_back
        )

    def get_all_stations_data(self, hours_back: int = 24) -> List[RainfallData]:
        """获取所有站点的历史降雨数据"""
        all_data = []
        for station in self.stations:
            data = self._generate_mock_data(
                station["id"], station["name"],
                station["lat"], station["lon"], hours_back
            )
            all_data.extend(data)
        return all_data

    def query_by_time_range(self, query: TimeRangeQuery) -> List[RainfallData]:
        """按时间范围查询降雨数据"""
        # 计算需要生成多少小时的数据
        hours_diff = int((query.end_time - query.start_time).total_seconds() / 3600)
        hours_diff = max(1, min(hours_diff, 168))  # 限制在1-168小时

        if query.station_ids:
            # 查询特定站点
            results = []
            for station_id in query.station_ids:
                try:
                    data = self.get_station_data(station_id, hours_diff)
                    # 过滤时间范围
                    filtered_data = [
                        d for d in data
                        if query.start_time <= d.measurement_time <= query.end_time
                    ]
                    results.extend(filtered_data)
                except ValueError:
                    continue
            return results
        else:
            # 查询所有站点
            all_data = self.get_all_stations_data(hours_diff)
            return [
                d for d in all_data
                if query.start_time <= d.measurement_time <= query.end_time
            ]

    def query_by_location(self, query: LocationQuery) -> List[RainfallData]:
        """按地理位置查询附近站点的降雨数据"""
        nearby_stations = []

        # 找到附近的站点
        for station in self.stations:
            distance = self._calculate_distance(
                query.latitude, query.longitude,
                station["lat"], station["lon"]
            )
            if distance <= query.radius_km:
                nearby_stations.append(station)

        # 获取这些站点的数据
        results = []
        for station in nearby_stations:
            data = self._generate_mock_data(
                station["id"], station["name"],
                station["lat"], station["lon"], 24
            )
            results.extend(data[-1:])  # 只取最新数据

        return results

    def _calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """计算两点间距离（简化版）"""
        # 使用简单的欧几里得距离估算
        lat_diff = abs(lat1 - lat2) * 111  # 纬度差转换为公里
        lon_diff = abs(lon1 - lon2) * 111 * 0.8  # 经度差转换为公里（考虑纬度影响）
        return (lat_diff ** 2 + lon_diff ** 2) ** 0.5

    def calculate_statistics(self, data: List[RainfallData]) -> StatisticalResult:
        """计算降雨统计数据"""
        if not data:
            raise ValueError("没有数据可供统计")

        rainfall_values = [d.rainfall_amount for d in data]
        start_time = min(d.measurement_time for d in data)
        end_time = max(d.measurement_time for d in data)

        return StatisticalResult(
            total_stations=len(set(d.station_id for d in data)),
            average_rainfall=round(sum(rainfall_values) / len(rainfall_values), 2),
            max_rainfall=round(max(rainfall_values), 2),
            min_rainfall=round(min(rainfall_values), 2),
            total_rainfall=round(sum(rainfall_values), 2),
            time_period=f"{start_time.strftime('%Y-%m-%d %H:%M')} 到 {end_time.strftime('%Y-%m-%d %H:%M')}"
        )

    def analyze_region(self, region_name: str, station_ids: List[str]) -> RegionalAnalysis:
        """区域降雨分析"""
        # 获取区域内的站点数据
        regional_data = []
        for station_id in station_ids:
            try:
                data = self.get_station_data(station_id, 24)
                regional_data.extend(data[-6:])  # 最近6小时数据
            except ValueError:
                continue

        if not regional_data:
            raise ValueError(f"区域 {region_name} 内没有可用数据")

        # 计算统计信息
        stats = self.calculate_statistics(regional_data)

        # 确定风险等级
        avg_rainfall = stats.average_rainfall
        if avg_rainfall > 20:
            risk_level = "high"
        elif avg_rainfall > 10:
            risk_level = "medium"
        else:
            risk_level = "low"

        return RegionalAnalysis(
            region_name=region_name,
            stations=regional_data[:10],  # 限制返回数量
            statistics=stats,
            risk_level=risk_level
        )

    def generate_forecast(self, hours_ahead: int = 24) -> List[ForecastData]:
        """生成降雨预报数据"""
        forecasts = []
        base_time = datetime.now()

        for i in range(1, hours_ahead + 1):
            forecast_time = base_time + timedelta(hours=i)
            # 模拟预报数据
            predicted_rainfall = max(0, random.normalvariate(3, 2))
            confidence = random.uniform(0.7, 0.95)

            condition = "晴天"
            if predicted_rainfall > 10:
                condition = "暴雨"
            elif predicted_rainfall > 5:
                condition = "中雨"
            elif predicted_rainfall > 1:
                condition = "小雨"

            forecasts.append(ForecastData(
                forecast_time=forecast_time,
                predicted_rainfall=round(predicted_rainfall, 2),
                confidence_level=round(confidence, 2),
                weather_condition=condition
            ))

        return forecasts

    def check_alerts(self) -> List[AlertInfo]:
        """检查降雨预警信息"""
        alerts = []

        # 模拟预警逻辑
        all_data = self.get_all_stations_data(6)  # 最近6小时数据

        # 按站点分组检查
        station_data = {}
        for data in all_data:
            if data.station_id not in station_data:
                station_data[data.station_id] = []
            station_data[data.station_id].append(data)

        for station_id, data_list in station_data.items():
            recent_rainfall = sum(d.rainfall_amount for d in data_list[-3:])  # 最近3小时

            if recent_rainfall > 30:  # 暴雨预警阈值
                station = next(s for s in self.stations if s["id"] == station_id)
                alerts.append(AlertInfo(
                    alert_level="red",
                    affected_areas=[station["name"]],
                    start_time=datetime.now(),
                    end_time=datetime.now() + timedelta(hours=2),
                    description=f"{station['name']}地区出现强降雨，过去3小时降雨量达{recent_rainfall:.1f}mm"
                ))
            elif recent_rainfall > 15:  # 大雨预警阈值
                station = next(s for s in self.stations if s["id"] == station_id)
                alerts.append(AlertInfo(
                    alert_level="orange",
                    affected_areas=[station["name"]],
                    start_time=datetime.now(),
                    end_time=datetime.now() + timedelta(hours=3),
                    description=f"{station['name']}地区降雨较大，过去3小时降雨量达{recent_rainfall:.1f}mm"
                ))

        return alerts

    def get_city_rainfall_time_range(self, city_name, start_time, forecast_hours):
        """获取指定城市、指定时间范围内的降雨数据"""
        import os
        from osgeo import ogr

        datasource = None
        try:
            city_shp_path = self.config.get('paths', 'city_boundary_shp')
            if not os.path.exists(city_shp_path):
                linux_fallback = "/home/ev/data/vector/city_border_in_haihe.shp"
                if os.path.exists(linux_fallback):
                    city_shp_path = linux_fallback
            if not os.path.exists(city_shp_path):
                raise FileNotFoundError(f"城市边界shapefile文件不存在: {city_shp_path}")

            datasource = ogr.Open(city_shp_path)
            if datasource is None:
                raise Exception(f"无法打开城市边界文件: {city_shp_path}")

            layer = datasource.GetLayer()
            city_feature = None
            for feature in layer:
                feature_name = feature.GetField("city")
                if feature_name and city_name.lower() in feature_name.lower():
                    city_feature = feature
                    break
            if city_feature is None:
                raise ValueError(f"未找到城市 '{city_name}' 的边界数据")

            ec_output_path = self.config.get('paths', 'ecOutput')
            raster_path, data_source_label = resolve_forecast_raster_path(
                forecast_hours, start_time, ec_output_path
            )
            if not raster_path:
                return RainfallCityData(
                    city_name=city_name,
                    time_period=f"{start_time.strftime('%Y-%m-%d %H:%M')}未来{forecast_hours}小时",
                    total_grid_points=0,
                    average_rainfall_mm=0,
                    max_rainfall_mm=0,
                    min_rainfall_mm=0,
                    processed_files=0,
                    data_source=data_source_label
                )

            geom = city_feature.GetGeometryRef().Clone()
            try:
                stats = compute_rainfall_stats_for_geometry(
                    geom, raster_path, source_srid=4326, min_exclude_zero=True
                )
                return RainfallCityData(
                    city_name=city_name,
                    time_period=f"{start_time.strftime('%Y-%m-%d %H:%M')}未来{forecast_hours}小时",
                    total_grid_points=stats["valid_count"],
                    average_rainfall_mm=stats["average_rainfall_mm"],
                    max_rainfall_mm=stats["max_rainfall_mm"],
                    min_rainfall_mm=stats["min_rainfall_mm"],
                    processed_files=1,
                    data_source=data_source_label
                )
            except Exception as e:
                logger.warning(f"按城市边界统计失败 ({city_name})，降级为全流域统计：{e}")
                stats = compute_rainfall_stats_for_raster(raster_path, min_exclude_zero=True)
                if stats["valid_count"] == 0:
                    raise ValueError(f"全流域无有效降雨数据")
                return RainfallCityData(
                    city_name=city_name,
                    time_period=f"{start_time.strftime('%Y-%m-%d %H:%M')}未来{forecast_hours}小时",
                    total_grid_points=stats["valid_count"],
                    average_rainfall_mm=stats["average_rainfall_mm"],
                    max_rainfall_mm=stats["max_rainfall_mm"],
                    min_rainfall_mm=stats["min_rainfall_mm"],
                    processed_files=1,
                    data_source=data_source_label
                )
        finally:
            if datasource is not None:
                datasource = None


if __name__ == '__main__':
    config = configparser.ConfigParser()
    config.read('./../config.ini', encoding='utf-8')
    # 创建全局实例
    analyzer = RainfallAnalyzer(config)
    start_time = datetime.strptime("2025-07-01 23:00:00", "%Y-%m-%d %H:%M:%S")
    res = analyzer.get_city_rainfall_time_range("天津", start_time, 24)
    print(json.dumps(res.model_dump(), ensure_ascii=False, indent=2))
