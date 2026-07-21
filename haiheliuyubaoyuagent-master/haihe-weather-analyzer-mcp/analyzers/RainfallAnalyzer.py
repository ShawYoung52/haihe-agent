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
        """获取指定城市、指定时间范围内的降雨数据
            读取 城市shp中对应名称的矢量边界 时间范围内ec多个tif数据、栅格累计计算出边界内累计栅格平均降雨量、栅格最大降雨量、栅格最小降雨量
        """
        from osgeo import gdal, ogr, osr
        import os
        import numpy as np

        datasource = None
        rainfall_tifs = []
        try:
            # 1. 获取城市边界shapefile路径
            city_shp_path = self.config.get('paths', 'city_boundary_shp')

            # Linux 兜底路径
            if not os.path.exists(city_shp_path):
                linux_fallback = "/home/ev/data/vector/city_border_in_haihe.shp"
                if os.path.exists(linux_fallback):
                    city_shp_path = linux_fallback

            if not os.path.exists(city_shp_path):
                raise FileNotFoundError(f"城市边界shapefile文件不存在: {city_shp_path}")

            # 2. 读取指定城市的矢量边界
            datasource = ogr.Open(city_shp_path)
            if datasource is None:
                raise Exception(f"无法打开城市边界文件: {city_shp_path}")

            layer = datasource.GetLayer()

            # 查找匹配城市名称的要素
            city_feature = None
            for feature in layer:
                feature_name = feature.GetField("city")
                if feature_name and city_name.lower() in feature_name.lower():
                    city_feature = feature
                    break

            if city_feature is None:
                raise ValueError(f"未找到城市 '{city_name}' 的边界数据")

            # 3. 获取EC数据路径
            ec_output_path = self.config.get('paths', 'ecOutput')

            # 4. 按数据可用性切换：有滚动预报 .nc 时切片成 2D .nc，否则查找 EC tif
            rainfall_tifs = []
            data_resource_label = "ECMWF AIFS"
            try:
                from rolling_forecast_grid import (
                    materialize_rolling_forecast_to_files,
                    resolve_forecast_grid_source,
                )
                source_info = resolve_forecast_grid_source(ec_output_path=ec_output_path)
                logger.info("get_city_rainfall_time_range 数据源=%s cycle=%s file=%s",
                            source_info.get("source"), source_info.get("cycle"), source_info.get("file"))
            except Exception as e:
                logger.warning("滚动预报数据源解析失败，降级 EC: %s", e, exc_info=True)
                source_info = {"source": "ec", "file": None}
            if source_info.get("source") == "rolling_forecast" and source_info.get("file"):
                nc_path = source_info["file"]
                materialized = materialize_rolling_forecast_to_files(nc_path, [forecast_hours])
                tiff_path = materialized.get(f"{forecast_hours}h")
                if tiff_path:
                    rainfall_tifs.append(tiff_path)
                    data_resource_label = f"滚动预报网格（cycle={source_info.get('cycle')}）"

            if not rainfall_tifs:
                # EC 路径：按文件名规范查找 tif
                time_str = start_time.strftime("%Y%m%d%H")
                pattern = f"ec_{time_str}_rain_total_{forecast_hours}h.tif"
                found_file = None

                # 优先搜索配置路径，搜索不到再搜索 Linux 服务器路径
                search_roots = [ec_output_path]
                linux_path = "/home/ev/data/ec/EC_AIFS/output"
                if os.path.isdir(linux_path) and linux_path != ec_output_path:
                    search_roots.append(linux_path)
                # 兜底：环境变量 EC_AIFS_ROOT
                env_root = os.environ.get("EC_AIFS_ROOT", "")
                if env_root and os.path.isdir(env_root) and env_root not in search_roots:
                    search_roots.append(env_root)
                # 再兜底：EC_AIFS_ROOT/output
                env_output = os.path.join(env_root, "output") if env_root else ""
                if env_output and os.path.isdir(env_output) and env_output not in search_roots:
                    search_roots.append(env_output)

                for root_dir in search_roots:
                    if not os.path.isdir(root_dir):
                        continue
                    for root, dirs, files in os.walk(root_dir):
                        if pattern in files:
                            found_file = os.path.join(root, pattern)
                            break
                        if found_file:
                            break
                    if found_file:
                        rainfall_tifs.append(found_file)
                        break

            if not rainfall_tifs:
                return RainfallCityData(
                    city_name=city_name,
                    time_period=f"{start_time.strftime('%Y-%m-%d %H:%M')}未来{forecast_hours}小时",
                    total_grid_points=0,
                    average_rainfall_mm=0,
                    max_rainfall_mm=0,
                    min_rainfall_mm=0,
                    processed_files=0,
                    data_resource=data_resource_label + "（无预报文件）"
                )

            # 5. 计算城市边界内的栅格统计信息
            all_rainfall_values = []

            for tif_path in rainfall_tifs:
                try:
                    # 打开TIFF文件
                    dataset = gdal.Open(tif_path)
                    if dataset is None:
                        continue

                    # 获取地理变换参数
                    geotransform = dataset.GetGeoTransform()
                    band = dataset.GetRasterBand(1)

                    # 获取NoData值
                    nodata_value = band.GetNoDataValue()

                    # 将城市矢量边界转换为栅格掩膜
                    raster_xsize = dataset.RasterXSize
                    raster_ysize = dataset.RasterYSize

                    # 创建内存中的掩膜数据集
                    mem_driver = gdal.GetDriverByName('MEM')
                    mask_ds = mem_driver.Create('', raster_xsize, raster_ysize, 1, gdal.GDT_Byte)
                    mask_ds.SetGeoTransform(geotransform)
                    mask_ds.SetProjection(dataset.GetProjection())

                    # 创建临时图层用于栅格化单个城市（带空间参考，避免 GDAL 警告）
                    raster_srs = osr.SpatialReference()
                    raster_srs.ImportFromWkt(dataset.GetProjection())
                    try:
                        raster_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    except Exception:
                        pass
                    temp_driver = ogr.GetDriverByName('MEM')
                    temp_datasource = temp_driver.CreateDataSource('temp')
                    temp_layer = temp_datasource.CreateLayer('temp_layer', srs=raster_srs, geom_type=ogr.wkbPolygon)
                    # 复制字段定义
                    layer_defn = layer.GetLayerDefn()
                    for i in range(layer_defn.GetFieldCount()):
                        field_defn = layer_defn.GetFieldDefn(i)
                        temp_layer.CreateField(field_defn)

                    # 添加城市要素到临时图层
                    temp_feature = ogr.Feature(temp_layer.GetLayerDefn())
                    temp_feature.SetGeometry(city_feature.GetGeometryRef().Clone())
                    for i in range(city_feature.GetFieldCount()):
                        temp_feature.SetField(i, city_feature.GetField(i))
                    temp_layer.CreateFeature(temp_feature)

                    # 栅格化城市边界
                    # err = gdal.RasterizeLayer(mask_ds, 1, temp_layer, burnValues=[1], options=['ALL_TOUCHED=TRUE'])
                    err = gdal.RasterizeLayer(dataset=mask_ds, bands=[1], layer=temp_layer, burn_values=[1])

                    if err != 0:
                        raise Exception(f"栅格化城市边界失败，错误码: {err}")

                    mask_array = mask_ds.GetRasterBand(1).ReadAsArray()

                    # 读取降雨数据
                    rainfall_array = band.ReadAsArray()

                    # 正确应用城市边界掩膜 - 保留城市内部区域（值为1）
                    valid_mask = (mask_array == 1) & (rainfall_array != nodata_value) & (~np.isnan(rainfall_array))
                    masked_rainfall = np.ma.masked_where(~valid_mask, rainfall_array)

                    # 收集有效值
                    valid_values = masked_rainfall.compressed()
                    all_rainfall_values.extend(valid_values.tolist())

                    # 清理临时资源
                    temp_feature = None
                    temp_layer = None
                    temp_datasource = None
                    mask_ds = None
                    dataset = None

                except Exception as e:
                    # logger.warning(f"处理文件 {tif_path} 时出错: {str(e)}")
                    # continue
                    raise e

            # 6. 计算统计结果
            if not all_rainfall_values:
                raise ValueError(f"在城市 {city_name} 边界内未找到有效的降雨数据")

            # 计算统计指标
            rainfall_array = np.array(all_rainfall_values)
            avg_rainfall = float(np.mean(rainfall_array))
            max_rainfall = float(np.max(rainfall_array))
            min_rainfall = float(np.min(rainfall_array[rainfall_array > 0])) if np.any(rainfall_array > 0) else 0.0
            total_rainfall = float(np.sum(rainfall_array))

            # 构造结果
            logger.info(f"城市 {city_name} 降雨统计完成: 平均{avg_rainfall:.2f}mm, "
                        f"最大{max_rainfall:.2f}mm, 最小{min_rainfall:.2f}mm")

            return RainfallCityData(
                city_name=city_name,
                time_period=f"{start_time.strftime('%Y-%m-%d %H:%M')}未来{forecast_hours}小时",
                total_grid_points=len(all_rainfall_values),
                average_rainfall_mm=round(avg_rainfall, 2),
                max_rainfall_mm=round(max_rainfall, 2),
                min_rainfall_mm=round(min_rainfall, 2),
                # total_rainfall_mm=round(total_rainfall, 2),
                processed_files=len(rainfall_tifs),
                data_resource=data_resource_label
            )

        except Exception as e:
            logger.warning(f"按城市边界统计失败 ({city_name})，降级为全流域统计：{e}")

            # 降级：打开 tif 直接算全流域统计
            try:
                if not rainfall_tifs:
                    raise ValueError(f"在时间范围 {start_time}未来{forecast_hours}小时 内未找到降雨数据文件")

                all_rainfall_values = []
                for tif_path in rainfall_tifs:
                    dataset = gdal.Open(tif_path)
                    if dataset is None:
                        continue
                    band = dataset.GetRasterBand(1)
                    nodata = band.GetNoDataValue()
                    arr = band.ReadAsArray()
                    valid = arr[~np.isnan(arr)]
                    if nodata is not None:
                        valid = valid[valid != nodata]
                    all_rainfall_values.extend(valid.tolist())
                    dataset = None

                if not all_rainfall_values:
                    raise ValueError(f"全流域无有效降雨数据")

                arr = np.array(all_rainfall_values)
                avg = float(np.mean(arr))
                mx = float(np.max(arr))
                mn = float(np.min(arr[arr > 0])) if np.any(arr > 0) else 0.0

                logger.info(f"全流域统计：平均{avg:.2f}mm, 最大{mx:.2f}mm")

                return RainfallCityData(
                    city_name=city_name,
                    time_period=f"{start_time.strftime('%Y-%m-%d %H:%M')}未来{forecast_hours}小时",
                    total_grid_points=len(all_rainfall_values),
                    average_rainfall_mm=round(avg, 2),
                    max_rainfall_mm=round(mx, 2),
                    min_rainfall_mm=round(mn, 2),
                    processed_files=len(rainfall_tifs),
                    data_resource=data_resource_label
                )
            except Exception as e2:
                logger.error(f"降级统计也失败: {e2}")
                raise
        finally:
            # 确保文件资源被正确关闭
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
