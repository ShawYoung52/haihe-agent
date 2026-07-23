import configparser
import json
import logging
import os
import os.path
import os.path

import numpy as np
import pygrib
from osgeo import gdal, osr, ogr

from exception.CustomException import BusinessException
from utils.commonUtils import deletefile

logger = logging.getLogger(__name__)

gdal.SetCacheMax(1024 * 1024 * 1024)  # 设置为 1GB
'''
grib转tif
'''
# 显式禁用异常模式
gdal.DontUseExceptions()
# 使用元组代替列表保证不可变性
RANGE_HAIHE = (111.97344, 34.9934, 119.847425, 42.70596)  # (minX, minY, maxX, maxY)
# 111.9734434266682115,34.9934003587397910 : 119.8474254611268179,42.7059557181767104
# 定义常量
WGS84_PROJ4 = "+proj=longlat +datum=WGS84 +no_defs"
WGS84_EPSG = 4326
# 使用示例
# PROCESS_CONFIG = [
#     {
#         'type': 'temperature_isobaric',
#         'palist': [200, 500, 700, 850, 925]
#     },
#     {
#         'type': 'temperature_2m'
#     },
#     {
#         'type': 'wind_isobaric',
#         'palist': [200, 500, 700, 850, 925]
#     },
#     {
#         'type': 'wind_10m'
#     },
#
#     {
#         'type': 'dewpoint_temperature_2m',
#     },
#     {
#         'type': 'gust_10m'
#     },
#
#     {
#         'type': 'pressure_mse'
#     },
#     {
#         'type': 'geopotential_height',
#         'palist': [200, 500, 700, 850, 925, 950]
#     },
# ]
PROCESS_CONFIG = [
    {
        'type': 'rain_total'
    },

]


# PROCESS_CONFIG_CLOUD = [
#     {
#         'type': 'Cloud cover_isobaric',
#         'palist': [200, 300, 500, 700, 800, 900]
#     },
# ]


def warp_double_line(tifpath, outputtif, scale=0.02, outputBounds=None):
    if os.path.exists(outputtif):
        deletefile(outputtif)
    input_ds = gdal.Open(tifpath)
    wo = gdal.WarpOptions(
        resampleAlg=gdal.GRIORA_Bilinear,
        xRes=scale,
        yRes=scale,
        format='GTiff',
        dstSRS=input_ds.GetProjection(),
        outputBounds=outputBounds
    )
    try:
        gdal.Warp(outputtif, tifpath, options=wo)
    except Exception as e:
        # 重新抛出异常，至少保留原始异常信息
        raise BusinessException(f"GDAL Warp 操作失败: {str(e)}") from e


def get_shp_boundary(shp_path):
    """
    读取shapefile文件的边界范围

    参数:
        shp_path: shapefile文件路径(.shp)

    返回:
        tuple: (minX, minY, maxX, maxY) 边界坐标
    """
    if not os.path.exists(shp_path):
        raise FileNotFoundError(f"Shapefile文件不存在: {shp_path}")

    try:
        # 打开shapefile
        datasource = ogr.Open(shp_path)
        if datasource is None:
            raise BusinessException(f"无法打开shapefile文件: {shp_path}")

        layer = datasource.GetLayer()
        if layer is None:
            raise BusinessException(f"无法获取图层: {shp_path}")

        # 获取边界范围
        extent = layer.GetExtent()
        # extent返回 (minX, maxX, minY, maxY)
        minX, maxX, minY, maxY = extent

        datasource = None  # 关闭数据源
        return (minX, minY, maxX, maxY)

    except Exception as e:
        raise BusinessException(f"读取shapefile边界失败: {str(e)}") from e


def get_default_shp_path():
    """
    获取默认的shapefile路径配置
    从config.ini中读取shp文件路径配置
    """
    config = configparser.ConfigParser()
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'config.ini')

    if os.path.exists(config_path):
        config.read(config_path, encoding='utf-8')
        if 'paths' in config and 'boundary_shp' in config['paths']:
            return config['paths']['boundary_shp']

    # 如果配置文件中没有设置，则使用默认路径
    return r'D:\gzt\天津气象台流域升级\数据\海河流域边界.shp'


def warp_with_shp_boundary(tifpath, outputtif, shp_path=None, scale=0.02):
    """
    使用shapefile矢量边界进行重采样截取

    参数:
        tifpath: 输入TIFF文件路径
        outputtif: 输出TIFF文件路径
        shp_path: shapefile边界文件路径，如果为None则使用默认配置
        scale: 重采样分辨率
    """
    if not os.path.exists(tifpath):
        logger.error(f"输入文件不存在: {tifpath}")
        return

    if os.path.exists(outputtif):
        deletefile(outputtif)

    # 获取shapefile边界路径
    if shp_path is None:
        shp_path = get_default_shp_path()

    try:
        # 读取shapefile边界
        boundary_extent = get_shp_boundary(shp_path)
        logger.info(f"使用shapefile边界进行重采样: {boundary_extent}")

        # 打开输入数据集
        input_ds = gdal.Open(tifpath)
        if input_ds is None:
            raise BusinessException(f"无法打开输入文件: {tifpath}")

        # 创建裁剪选项
        wo = gdal.WarpOptions(
            resampleAlg=gdal.GRIORA_Bilinear,
            xRes=scale,
            yRes=scale,
            format='GTiff',
            dstSRS=input_ds.GetProjection(),
            outputBounds=boundary_extent,  # 使用shapefile边界
            cutlineDSName=shp_path,  # 使用矢量边界裁剪
            cropToCutline=True  # 裁剪到矢量边界
        )

        # 执行重采样和裁剪
        gdal.Warp(outputtif, tifpath, options=wo)
        logger.info(f"重采样并裁剪完成: {outputtif}")

    except Exception as e:
        raise BusinessException(f"使用shapefile边界重采样失败: {str(e)}") from e
    finally:
        if 'input_ds' in locals():
            input_ds = None


def _get_cached_latlon(grb):
    """
    获取经纬度并缓存结果
    """
    # global _cached_latlon
    # if _cached_latlon is None:
    #     _cached_latlon = grb.latlons()
    # return _cached_latlon
    return grb.latlons()


def _create_geo_transform(lon, lat, cols, rows):
    return (
        float(lon[0][0]),  # 左上角经度（西）
        float((lon[0][-1] - lon[0][0]) / cols),  # x 分辨率
        0.0,  # 旋转参数
        float(lat[0][0]),  # 左上角纬度（北）
        0.0,  # 旋转参数
        -float((lat[0][0] - lat[-1][0]) / rows)  # y 分辨率（负值，表示向下递减）
    )


def _create_geotiff(path, data, lon, lat, proj_wkt):
    """创建 GeoTIFF 文件的通用方法"""
    rows, cols = data.shape
    # 确保输出目录存在
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # 更精确的地理变换计算
    geo_transform = _create_geo_transform(lon, lat, cols, rows)

    driver = gdal.GetDriverByName("GTiff")
    outds = driver.Create(path, cols, rows, 1, gdal.GDT_Float32)

    try:
        outds.SetGeoTransform(geo_transform)
        outds.SetProjection(proj_wkt)

        outband_data = outds.GetRasterBand(1)
        outband_data.WriteArray(data)
        outband_data.FlushCache()  # 显式刷新缓存

    finally:
        # 确保数据集被正确关闭
        if outds is not None:
            outds.FlushCache()
            outds = None


def subtract_grb_data(grb1, grb2):
    """
    安全计算两个 GRB 数据的差值，确保类型转换和形状兼容性

    参数:
        grb1: 第一个 GRB 数据对象（需包含 values 属性）
        grb2: 第二个 GRB 数据对象（需包含 values 属性）

    返回:
        差值数组（float 类型）

    异常:
        ValueError: 若数据形状不匹配或类型无法转换为 float
    """
    if grb1.values.shape != grb2.values.shape:
        raise ValueError("输入数据形状不匹配，无法进行减法运算")

    try:
        result = grb1.values.astype(float, copy=False) * 1000 - grb2.values.astype(float, copy=False) * 1000
        return np.clip(result, 0, None)  # 将负值置零
    except TypeError as e:
        raise ValueError("数据类型无法转换为 float") from e


def compute_rain_raster(grib_path_start, grib_path_end, total_tif):
    total_data = None
    spatial_ref = _ensure_spatial_reference()
    with pygrib.open(grib_path_end) as grb_end:
        try:
            # 选择 2m 温度
            matches_total1 = grb_end.select(name='Total precipitation', typeOfLevel='surface', level=0)
            if not matches_total1:
                logger.error(f"未找到 total_rain字段：{grib_path_end}")
                return
            grb1_total = matches_total1[0]
            lat, lon = _get_cached_latlon(grb1_total)
            with pygrib.open(grib_path_start) as grbs_start:
                matches_total2 = grbs_start.select(name='Total precipitation', typeOfLevel='surface', level=0)
                if not matches_total2:
                    logger.error(f"未找到 total_rain字段：{grib_path_start}")
                    return
                grb2_total = matches_total2[0]
            # 使用封装函数计算差值
            total_data = subtract_grb_data(grb1_total, grb2_total)
        except Exception as e:
            raise e

    _create_geotiff(total_tif, total_data, lon, lat, spatial_ref.ExportToWkt())


def warpAndEvserverProcess(unwrapper_path, outputBounds):
    if not os.path.exists(unwrapper_path):
        logger.error(f"文件 {unwrapper_path} 不存在")
        return
    # 执行重采样
    res_path = unwrapper_path.replace("_unwarp.tif", ".tif")
    warp_with_shp_boundary(unwrapper_path, res_path, scale=0.02)
    deletefile(unwrapper_path)
    # publishEvServerTif(res_path)
    return res_path


def ec2tif_rain(grib_path_start, grib_path_end, output_tif):
    # 构造临时输出路径（未重采样）
    output_tif = output_tif.replace("\\", "/")
    try:
        base, ext = os.path.splitext(output_tif)
        unwrapper_path = f"{base}_unwarp{ext}"
        # output_tif2 = output_tif.replace('total', 'convective')
        # base2, ext2 = os.path.splitext(output_tif2)
        # unwrapper_path2 = f"{base2}_unwarp{ext2}"
        # 使用 compute_max_temperature_raster 生成未重采样的 TIF 文件
        compute_rain_raster(grib_path_start, grib_path_end, unwrapper_path)

        return [warpAndEvserverProcess(unwrapper_path, RANGE_HAIHE), ]
        # warpAndEvserverProcess(unwrapper_path2, RANGE_HAIHE)]
    except Exception as e:
        # logger.error(f"累计雨量处理失败  {grib_path_start} {grib_path_end}: {e}", exc_info=True)
        logger.error(f"累计雨量处理失败  {grib_path_start} {grib_path_end}: {e}")
        # raise e
        return None


def _process_general_data(grb, output_path, suffix, spatial_ref, outputBounds):
    """处理数据的通用方法"""
    lat, lon = _get_cached_latlon(grb)
    data = grb.values
    if suffix is not None:
        unwrapper_path = output_path.replace(".tif", f"_{suffix}unWarp.tif")
    else:
        unwrapper_path = output_path.replace(".tif", "unWarp.tif")
    # #deleteEvServer(output_path)
    _create_geotiff(unwrapper_path, data, lon, lat, spatial_ref.ExportToWkt())
    res_path = unwrapper_path.replace("unWarp", "")
    warp_double_line(unwrapper_path, res_path, outputBounds=outputBounds)
    deletefile(unwrapper_path)
    # #publishEvServerTif(output_path)
    return res_path


def _process_temperature_data(grb, output_path, suffix, spatial_ref):
    """处理温度数据的通用方法"""
    lat, lon = _get_cached_latlon(grb)
    data = grb.values - 273.15  # 单位：K -> °C
    if suffix is not None:
        unwrapper_path = output_path.replace(".tif", f"_{suffix}unWarp.tif")
    else:
        unwrapper_path = output_path.replace(".tif", "unWarp.tif")
    # #deleteEvServer(output_path)
    _create_geotiff(unwrapper_path, data, lon, lat, spatial_ref.ExportToWkt())
    res_path = unwrapper_path.replace("unWarp", "")
    if '2m' in unwrapper_path:
        warp_double_line(unwrapper_path, res_path, outputBounds=RANGE_HAIHE)
    else:
        warp_double_line(unwrapper_path, res_path, outputBounds=None)
    deletefile(unwrapper_path)
    # #publishEvServerTif(output_path)
    return res_path


def _process_cloud_data(grb, output_path, suffix, spatial_ref):
    """处理温度数据的通用方法"""
    lat, lon = _get_cached_latlon(grb)
    data = grb.values  # 单位：K -> °C
    if suffix is not None:
        unwrapper_path = output_path.replace(".tif", f"_{suffix}unWarp.tif")
    else:
        unwrapper_path = output_path.replace(".tif", "unWarp.tif")
    # deleteEvServer(output_path)
    _create_geotiff(unwrapper_path, data, lon, lat, spatial_ref.ExportToWkt())
    res_path = unwrapper_path.replace("unWarp", "")
    warp_double_line(unwrapper_path, res_path, outputBounds=RANGE_HAIHE)
    deletefile(unwrapper_path)
    # publishEvServerTif(output_path)
    return res_path


def _select_wind_components(grbs, param_name, level_type, level):
    """通用的风分量选择方法"""
    u_grbs = grbs.select(name=param_name, typeOfLevel=level_type, level=level)
    u_grb = next(iter(u_grbs), None)
    if not u_grb:
        return None, None
        # raise BusinessException(f"未找到 {level_type} {level} 的 U 数据")

    v_grbs = grbs.select(name=param_name.replace("U", "V"), typeOfLevel=level_type, level=level)
    v_grb = next(iter(v_grbs), None)
    if not v_grb:
        return None, None
        # raise BusinessException(f"未找到 {level_type} {level} 的 V 数据")

    return u_grb, v_grb


# def _process_wind_data(output_path, type_suffix, u_data, v_data, lon, lat, spatial_ref_wkt):
#     """通用的风数据处理方法"""
#     base, ext = os.path.splitext(output_path)
#     if type_suffix != 'None':
#         new_base = base.replace("wind", "wind" + type_suffix)
#         unwrapper_path = f"{new_base}_unwarp.tif"
#         output_path = new_base + ".tif"
#     else:
#         unwrapper_path = f"{base}_unwarp.tif"
#     res_path = unwrapper_path.replace("_unwarp.tif", ".tif")
#     # 计算风速和风向
#     if 'speed' in type_suffix:
#         data = calculate_wind_speed(u_data, v_data)
#     elif 'direction' in type_suffix:
#         data = calculate_wind_direction(u_data, v_data)
#     else:
#         raise BusinessException(f"未知的参数类型: {type_suffix}")
#     # 处理风速
#     # deleteEvServer(output_path)
#     _create_geotiff(unwrapper_path, data, lon, lat, spatial_ref_wkt)
#     if '10m' in unwrapper_path:
#         warp_double_line(unwrapper_path, res_path, outputBounds=RANGE_HAIHE)
#     else:
#         warp_double_line(unwrapper_path, res_path, outputBounds=None)
#     deletefile(unwrapper_path)
#     # publishEvServerTif(output_path)
#     return res_path


def _save_u_v_Geojson(output_path, u_data, v_data, rows, cols, min_lat, max_lat, min_lon, max_lon):
    """
    生成两个独立的 JSON 文件，分别存储 U 分量和 V 分量的风速数据

    参数:
        output_path: 输出路径基础名称（含或不含 .tif 后缀）
        u_data: U 分量数据二维数组
        v_data: V 分量数据二维数组
        rows: 数据行数
        cols: 数据列数
        min_lat: 纬度最小值
        max_lat: 纬度最大值
        min_lon: 经度最小值
        max_lon: 经度最大值
    """
    try:
        # 验证数据维度
        if u_data.shape != (rows, cols) or v_data.shape != (rows, cols):
            raise ValueError("u_data 或 v_data 的维度与 rows/cols 不匹配")

        # 安全处理文件路径
        base_path, _ = os.path.splitext(output_path)
        u_output_path = f"{base_path}_u.json"
        v_output_path = f"{base_path}_v.json"

        # 生成经纬度网格
        lat_values = np.linspace(min_lat, max_lat, rows)
        lon_values = np.linspace(min_lon, max_lon, cols)

        # 向量化生成数据（避免嵌套循环）
        lat_grid, lon_grid = np.meshgrid(lat_values, lon_values, indexing='ij')

        # 分别处理 U 和 V 分量
        u_points = [
            {
                "lat": round(float(lat), 6),
                "lon": round(float(lon), 6),
                "value": round(float(u), 3)
            }
            for lat, lon, u in zip(
                lat_grid.flatten(),
                lon_grid.flatten(),
                u_data.flatten()
            )
        ]

        v_points = [
            {
                "lat": round(float(lat), 6),
                "lon": round(float(lon), 6),
                "value": round(float(v), 3)
            }
            for lat, lon, v in zip(
                lat_grid.flatten(),
                lon_grid.flatten(),
                v_data.flatten()
            )
        ]

        # 写入 U 分量文件
        with open(u_output_path, 'w', encoding='utf-8') as f:
            json.dump(u_points, f, ensure_ascii=False, indent=2)
        # 写入 V 分量文件
        with open(v_output_path, 'w', encoding='utf-8') as f:
            json.dump(v_points, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存风速数据时发生错误: {e}")
        raise


# def _save_U_V_json(output_path, u_data, v_data, step=2):
#     jsonfile_u = output_path.replace(".tif", f"_u_step{step}.json")
#     jsonfile_v = output_path.replace(".tif", f"_v_step{step}.json")
#
#     # 数据抽稀
#     u_subsampled = subsample_data(u_data, step)
#     v_subsampled = subsample_data(v_data, step)
#
#     # 保留精度
#     u_subsampled = np.round(u_subsampled, 3)
#     v_subsampled = np.round(v_subsampled, 3)
#
#     # 写入JSON
#     writejsonfromgribdata(u_subsampled, jsonfile_u)
#     writejsonfromgribdata(v_subsampled, jsonfile_v)


# def _process_wind_components(grb_u, grb_v, output_path, spatial_ref):
#     """处理风速风向的通用方法"""
#     lat, lon = _get_cached_latlon(grb_u)
#     u_data = grb_u.values
#     v_data = grb_v.values
#
#     # 筛选指定范围内的数据
#     min_lon, min_lat, max_lon, max_lat = RANGE_HAIHE
#     mask = (lon >= min_lon) & (lon <= max_lon) & (lat >= min_lat) & (lat <= max_lat)
#
#     lon_masked = lon[mask]
#     lat_masked = lat[mask]
#     u_data_masked = u_data[mask]
#     v_data_masked = v_data[mask]
#
#     # 假设数据为规则网格，进行 reshape
#     unique_lats = np.unique(lat_masked)
#     unique_lons = np.unique(lon_masked)
#
#     rows = len(unique_lats)
#     cols = len(unique_lons)
#
#     try:
#         u_data_masked = u_data_masked.reshape(rows, cols)
#         v_data_masked = v_data_masked.reshape(rows, cols)
#     except ValueError as e:
#         raise BusinessException(f"无法将筛选后的数据重塑为 ({rows}, {cols}) 形状，请检查数据完整性: {str(e)}") from e
#     # 保存风速风向json数据
#     _save_U_V_json(output_path, u_data_masked, v_data_masked)
#     # _save_u_v_Geojson(output_path, u_data_masked, v_data_masked, rows, cols, min_lat, max_lat, min_lon, max_lon)
#     # 处理并保存数据
#     speed_path = _process_wind_data(
#         output_path, f"_speed", u_data_masked, v_data_masked, lon, lat, spatial_ref.ExportToWkt()
#     )
#
#     direction_path = _process_wind_data(
#         output_path, f"_direction", u_data_masked, v_data_masked, lon, lat, spatial_ref.ExportToWkt()
#     )
#
#     return [speed_path, direction_path]


def _ensure_spatial_reference():
    """确保空间参考系统的一致性"""
    srs = osr.SpatialReference()
    srs.ImportFromProj4(WGS84_PROJ4)
    return srs


def compute_max_temperature_raster(grib_paths, output_tif):
    """
    遍历多个 GRIB 文件，提取 '2 metre temperature' 数据，
    计算每个像素点的最大值，并输出为 TIF 文件。

    :param grib_paths: GRIB 文件路径列表
    :param output_tif: 输出 TIF 路径
    """
    max_data = None
    geo_transform = None
    rows = cols = 0

    for i, grib_path in enumerate(grib_paths):
        if not os.path.exists(grib_path):
            logger.warning(f"文件不存在，跳过：{grib_path}")
            continue
        matches = None
        with pygrib.open(grib_path) as grbs:
            try:
                # 选择 2m 温度
                try:
                    matches = grbs.select(name='Maximum temperature at 2 metres in the last 3 hours',
                                          typeOfLevel='surface', level=0)
                except Exception as e:
                    try:
                        matches = grbs.select(name='Maximum temperature at 2 metres in the last 6 hours',
                                              typeOfLevel='surface', level=0)
                    except Exception as e:
                        logger.error(f"未找到 2m 温度字段：{grib_path}")
                        continue
                if not matches:
                    logger.error(f"未找到 2m 温度字段：{grib_path}")
                    continue
                grb = matches[0]
                lat, lon = _get_cached_latlon(grb)
                data = grb.values - 273.15  # 单位：K

                # 第一次读取时初始化 max_data
                if max_data is None:
                    rows, cols = data.shape
                    max_data = data.copy()
                    geo_transform = _create_geo_transform(lon, lat, cols, rows)
                    spatial_ref = osr.SpatialReference()
                    spatial_ref.ImportFromProj4(WGS84_PROJ4)
                else:
                    # 更新最大值
                    np.maximum(max_data, data, out=max_data)

            except Exception as e:
                logger.error(f"Maximum temperature at 2 metres处理失败 {grib_path}: {str(e)}")

    if max_data is None:
        logger.error(grib_paths + "没有成功读取到任何有效的最高温度数据")
        return 'error'
    if not os.path.exists(os.path.dirname(output_tif)):
        os.makedirs(os.path.dirname(output_tif))
    # 创建 TIF 文件
    driver = gdal.GetDriverByName('GTiff')
    out_ds = driver.Create(output_tif, cols, rows, 1, gdal.GDT_Float32)
    try:
        # 设置地理变换和投影
        out_ds.SetGeoTransform(geo_transform)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(WGS84_EPSG)  # 使用常量定义
        out_ds.SetProjection(srs.ExportToWkt())
        # 写入最大值数据
        out_band = out_ds.GetRasterBand(1)
        out_band.WriteArray(max_data)
        out_band.FlushCache()

    finally:
        # 确保数据集被正确关闭
        if out_ds is not None:
            out_ds.FlushCache()
            out_ds = None


def ec2tif_tem_max_2m(gribpaths, outfilepath):
    outfilepath = outfilepath.replace("\\", "/")
    # 验证所有文件是否存在
    for gribpath in gribpaths:
        if not os.path.exists(gribpath):
            raise BusinessException(f"grib文件不存在: {gribpath}")

    # 构造临时输出路径（未重采样）
    base, ext = os.path.splitext(outfilepath)
    unwrapper_path = f"{base}_unwarp{ext}"

    # 使用 compute_max_temperature_raster 生成未重采样的 TIF 文件
    res = compute_max_temperature_raster(gribpaths, unwrapper_path)
    if res == 'error':
        return
        # 执行重采样
    res_path = unwrapper_path.replace("_unwarp.tif", ".tif")
    # deleteEvServer(res_path)
    warp_double_line(unwrapper_path, res_path, outputBounds=RANGE_HAIHE)
    deletefile(unwrapper_path)
    # publishEvServerTif(res_path)
    return [res_path]


def process_ec_grib_file(gribpath, output_dir, tif_path_date, process_config=None):
    """
    统一处理 GRIB 文件的接口

    :param gribpath: GRIB 文件路径
    :param output_dir: 输出目录
    :param process_config: 处理配置，定义各种数据的处理方式
    :return: 处理结果路径列表
    """
    if process_config is None:
        process_config = PROCESS_CONFIG
    if not os.path.exists(gribpath):
        logger.error(f"grib文件不存在: {gribpath}")
        return []
    result_paths = []
    spatial_ref = _ensure_spatial_reference()
    with pygrib.open(gribpath) as grbs:
        # 收集所有需要的波段
        all_matches = {}
        # 预处理收集所有需要的波段
        for config in process_config:
            if config['type'] == 'temperature_isobaric':
                for pa in config.get('palist', []):
                    key = f"tem_{pa}"
                    try:
                        matches = grbs.select(name='Temperature', typeOfLevel='isobaricInhPa', level=pa)
                    except Exception as e:
                        logger.error(f"{gribpath}:未找到指定气压层 {pa} hPa 的 Temperature 数据")
                        continue
                    if not matches:
                        continue
                        # raise BusinessException(f"{gribpath}:未找到指定气压层 {pa} hPa 的 Temperature 数据")
                    all_matches[key] = matches[0]
            elif config['type'] == 'temperature_2m':
                key = "tem_2m"
                try:
                    matches = grbs.select(name='2 metre temperature', typeOfLevel='surface', level=0)
                except Exception as e:
                    logger.error(f"{gribpath}:未找到2 metre temperature  数据:{e}")
                    continue
                if not matches:
                    continue
                    # raise BusinessException(f"{gribpath}:未找到2 metre temperature 数据")
                all_matches[key] = matches[0]
            elif config['type'] == 'dewpoint_temperature_2m':
                key = "tem_dp_2m"
                try:
                    matches = grbs.select(name='2 metre dewpoint temperature', typeOfLevel='surface', level=0)
                except Exception as e:
                    logger.error(f"{gribpath}:tem_dp_2m 读取失败: {str(e)}")
                    continue
                if not matches:
                    continue
                    # raise BusinessException(gribpath + " 未找到 2 metre dewpoint temperature 数据")
                all_matches[key] = matches[0]
            elif config['type'] == 'gust_10m':
                key = "gust_10m"
                try:
                    # matches = grbs.select(name='Maximum 10 metre wind gust in the last 3 hours', typeOfLevel='surface',
                    #                       level=0)
                    matches = grbs.select(name='10 metre wind gust in the last 3 hours', typeOfLevel='surface',
                                          level=0)  # 线上读取
                except Exception as e:
                    try:
                        matches = grbs.select(name='10 metre wind gust in the last 6 hours', typeOfLevel='surface',
                                              level=0)
                    except Exception as e:
                        logger.error(f"{gribpath}:gust_10m 读取失败: {str(e)}")
                        continue
                if not matches:
                    continue
                    # raise BusinessException(gribpath + " 未找到 gust_10m 数据")
                all_matches[key] = matches[0]
            elif config['type'] == 'rain_total':
                key = "rain_total"
                try:
                    matches = grbs.select(name='Total precipitation', typeOfLevel='surface', level=0)
                except Exception as e:
                    logger.error(f"{gribpath}:rain_total 读取失败: {str(e)}")
                    continue
                if not matches:
                    continue
                    # raise BusinessException(gribpath + " 未找到 rain_total 数据")
                all_matches[key] = matches[0]
            elif config['type'] == 'rain_convective':
                key = "rain_convective"
                try:
                    matches = grbs.select(name='Convective precipitation', typeOfLevel='surface', level=0)
                except Exception as e:
                    logger.error(f"{gribpath}:rain_convective 读取失败: {str(e)}")
                    continue
                if not matches:
                    continue
                    # raise BusinessException(gribpath + " 未找到 rain_convective 数据")
                all_matches[key] = matches[0]
            elif config['type'] == 'pressure_mse':
                key = "pressure_mse"
                try:
                    matches = grbs.select(name='Mean sea level pressure', typeOfLevel='surface', level=0)
                except Exception as e:
                    logger.error(f"{gribpath}:pressure_mse 读取失败: {str(e)}")
                    continue
                if not matches:
                    continue
                    # raise BusinessException(gribpath + " 未找到 pressure_mse 数据")
                all_matches[key] = matches[0]
            elif config['type'] == 'Cloud cover_isobaric':
                for pa in config.get('palist', []):
                    key = f"cloud_{pa}"
                    try:
                        matches = grbs.select(name='Cloud cover', typeOfLevel='isobaricInhPa', level=pa)
                    except Exception as e:
                        logger.error(f"{gribpath}:未找到指定气压层 {pa} hPa 的 Cloud cover_isobaric 数据")
                        continue
                    if not matches:
                        continue
                        # raise BusinessException(f"{gribpath}:未找到指定气压层 {pa} hPa 的 Cloud cover 数据")
                    all_matches[key] = matches[0]
            elif config['type'] == 'wind_isobaric':
                for pa in config.get('palist', []):
                    key = f"wind_{pa}"
                    try:
                        u_grb, v_grb = _select_wind_components(grbs, 'U component of wind', 'isobaricInhPa', pa)
                    except Exception as e:
                        logger.error(f"{gribpath}: 未找到指定气压层 {pa} hPa 的 wind_isobaric 数据")
                        continue
                    if u_grb is None or v_grb is None:
                        continue
                    all_matches[key] = {'u_grb': u_grb, 'v_grb': v_grb}

            elif config['type'] == 'wind_10m':
                key = "wind_10m"
                try:
                    u_grb, v_grb = _select_wind_components(grbs, '10 metre U wind component', 'surface', 0)
                except Exception as e:
                    logger.error(f"{gribpath}:未找到wind_10m 数据")
                    continue
                if u_grb is None or v_grb is None:
                    continue
                    # raise BusinessException(f"{gribpath}:未找到 10 metre U wind component 数据")
                all_matches[key] = {'u_grb': u_grb, 'v_grb': v_grb}
            elif config['type'] == 'geopotential_height':
                for pa in config.get('palist', []):
                    key = f"geopotential_height_{pa}"
                    try:
                        matches = grbs.select(name='Geopotential height', typeOfLevel='isobaricInhPa', level=pa)
                    except Exception as e:
                        logger.error(f"{gribpath}:Geopotential height 读取失败: {str(e)}")
                        continue
                    if not matches:
                        continue
                        # raise BusinessException(gribpath + f"未找到指定气压层 {pa} hPa 的 Geopotential height 数据")
                    all_matches[key] = matches[0]
        for key, item in all_matches.items():
            # 根据不同类型提交任务
            if key.startswith('tem') or key.startswith('dp_'):
                result_paths.append(_process_temperature_data(
                    item,
                    os.path.join(output_dir, f"ec_{tif_path_date}_{key}.tif"),
                    None,
                    spatial_ref))
            elif key.startswith('cloud'):
                result_paths.append(_process_general_data(
                    item,
                    os.path.join(output_dir, f"ec_{tif_path_date}_{key}.tif"),
                    None,
                    spatial_ref
                ))
            # elif key.startswith('wind'):
            #     result_paths.append(_process_wind_components(
            #         item['u_grb'],
            #         item['v_grb'],
            #         os.path.join(output_dir, f"ec_{tif_path_date}_{key}.tif"),
            #         spatial_ref
            #     ))
            elif key.startswith('gust') or key.startswith('rain'):
                result_paths.append(_process_general_data(
                    item,
                    os.path.join(output_dir, f"ec_{tif_path_date}_{key}.tif"),
                    None,
                    spatial_ref,
                    RANGE_HAIHE
                ))
            # elif key.startswith(
            #         'geopotential_height') or key.startswith('pressure'):
            #     result_paths.append(_process_isoline_data(
            #         item,
            #         os.path.join(output_dir, f"ec_{tif_path_date}_{key}.shp"),
            #         key,
            #         spatial_ref
            #     ))

    return result_paths


def find_xy_index(lon, lat, geo_transform):
    x = int((lon - geo_transform[0]) / geo_transform[1])
    y = int((lat - geo_transform[3]) / geo_transform[5])
    return x, y


def get_value_by_lat_lon(lat: float, lon: float, tif_path_date: str) -> float:
    """
    根据经纬度获取对应 TIFF 图像中的值。

    参数:
        lat (float): 纬度
        lon (float): 经度
        tif_path_date (str): TIFF 文件路径

    返回:
        float: 对应位置的像素值，若出错则抛出异常
    """
    dataset = gdal.Open(tif_path_date)
    if not dataset:
        raise FileNotFoundError(f"无法打开文件: {tif_path_date}")

    data = dataset.ReadAsArray()
    geo_transform = dataset.GetGeoTransform()
    band = dataset.GetRasterBand(1)
    nodata_value = band.GetNoDataValue() if band else None
    del dataset  # 及时释放资源qqqqqqqqqqqqqqqqqqq

    x, y = find_xy_index(lon, lat, geo_transform)

    if not (0 <= x < data.shape[1] and 0 <= y < data.shape[0]):
        raise ValueError(f"经纬度 ({lat}, {lon}) 超出 TIFF 文件范围")

    value = data[y, x]

    if nodata_value is not None and np.isclose(value, nodata_value):
        raise ValueError(f"经纬度 ({lat}, {lon}) 处为 NoData 值: {nodata_value}")

    return float(value)


def generate_contours_from_tif(input_tif, output_shp, interval=5.0, field_name="value", min_val=0.0, round_values=True,
                               vals=None):
    """
    从单波段TIFF生成整型等值线

    :param input_tif: 输入GeoTIFF路径
    :param output_shp: 输出Shapefile路径
    :param interval: 等值线间隔（单位与栅格数据相同）
    :param field_name: 高程字段名
    :param min_val: 起始高程值
    :param round_values: 是否对高程值四舍五入取整
    :raises ValueError: 参数无效时抛出
    """
    # 参数校验
    if interval <= 0:
        raise ValueError("等值线间隔必须大于0")
    if not os.path.exists(input_tif):
        raise FileNotFoundError(f"输入文件不存在: {input_tif}")

    # 打开栅格
    src_ds = gdal.Open(input_tif)
    if src_ds is None:
        raise BusinessException(f"无法打开栅格文件: {input_tif}")

    try:
        band = src_ds.GetRasterBand(1)
        nodata = band.GetNoDataValue()

        # 创建矢量文件
        driver = ogr.GetDriverByName("ESRI Shapefile")
        if os.path.exists(output_shp):
            driver.DeleteDataSource(output_shp)

        dst_ds = driver.CreateDataSource(output_shp)
        srs = osr.SpatialReference()
        srs.ImportFromWkt(src_ds.GetProjectionRef())

        dst_layer = dst_ds.CreateLayer("contour", srs=srs)
        field_defn = ogr.FieldDefn(field_name, ogr.OFTReal)
        dst_layer.CreateField(field_defn)
        # band (GDALRasterBand): 输入栅格波段对象
        # interval (float): 等高线生成间隔值
        # min_val (float): 起始最小高程值
        # [] (list): 固定等高线级别列表（空列表表示不使用）
        # 0 (int): 是否使用nodata值标志（0=不使用）
        # 0 (float): nodata值（当use_no_data为1时生效）
        # dst_layer (OGRLayer): 目标矢量图层对象
        # -1 (int): 属性字段索引（-1=不写入属性）
        # 0 (int): 3D几何生成标志（0=生成2D几何）
        if vals is not None and len(vals) > 0:
            fixed_levels = [float(x) for x in vals]
            gdal.ContourGenerate(band, interval, min_val, fixed_levels, 0, 0.0, dst_layer, -1, 0)
        else:
            gdal.ContourGenerate(band, interval, min_val, [], 0, 0.0, dst_layer, -1, 0)
        # gdal.ContourGenerate(band, interval, min_val, [], 0, 0, dst_layer, -1, 0)
        # 浮点转整型处理
        if round_values:
            for feature in dst_layer:
                value = feature.GetField(field_name)
                # 四舍五入转为整型:ml-citation{ref="2,3" data="citationList"}
                feature.SetField(field_name, int(round(value)))
                dst_layer.SetFeature(feature)
        # logger.info(f"成功生成整型等值线: {output_shp}")

    finally:
        # 资源释放
        if 'dst_ds' in locals():
            dst_ds = None
        src_ds = None


def _get_cloud_png_by_latlon(lat, lon, start_time, end_time, tif_path, palist):
    # 从开始时间到结束时间遍历
    # start_time = datetime.datetime.strptime(start_time, "%Y%m%d%H")
    # end_time = datetime.datetime.strptime(end_time, "%Y%m%d%H")
    # data_res = []
    # times = []
    # while start_time <= end_time:
    #     data_pa = []
    #     timeStr = start_time.strftime("%Y%m%d%H")
    #     times.append(timeStr)
    #     for pa in palist:
    #         tif_path_date = os.path.join(tif_path, timeStr, 'cloud_' + str(pa) + ".tif")
    #         logger.error(tif_path_date)
    #         data_pa.append(get_value_by_lat_lon(lat, lon, tif_path_date))
    #     data_res.append(data_pa)
    #     start_time += datetime.timedelta(hours=3)
    # # 数组行转列
    # logger.error(data_res)
    # data_res = np.array(data_res).T
    # logger.error(data_res)
    # # 插值生成tif tif生成等值面图
    # # 假设 x 表示时间维度，y 表示气压层维度
    # # times = np.arange(len(palist))  # 时间索引
    # pressures = np.array(palist)  # 气压层值

    # 创建 GeoTIFF 文件用于存储插值结果
    output_tif = os.path.join(tif_path, "interpolated_cloud_cover4.tif")
    # times, pressures 转化经纬度
    # driver = gdal.GetDriverByName("GTiff")
    # out_ds = driver.Create(output_tif, len(times), len(pressures), 1, gdal.GDT_Float32)
    # try:
    #     # 设置地理变换信息（假设均匀分布）
    #     geo_transform = [0, 1, 0, 0, 0, -1]  # 示例变换参数
    #     out_ds.SetGeoTransform(geo_transform)
    #
    #     # 设置投影为 WGS84
    #     srs = osr.SpatialReference()
    #     srs.ImportFromEPSG(WGS84_EPSG)
    #     out_ds.SetProjection(srs.ExportToWkt())
    #
    #     # 写入插值数据
    #     band = out_ds.GetRasterBand(1)
    #     band.WriteArray(data_res)
    #     band.FlushCache()
    # finally:
    #     if out_ds is not None:
    #         out_ds.FlushCache()
    #         out_ds = None
    # tif生成等值线
    # 在生成 interpolated_cloud_cover4.tif 后

    # 生成等值线
    contour_shp = os.path.join(tif_path, "cloud_contours.shp")
    generate_contours_from_tif(output_tif, contour_shp, interval=10.0)  # 每隔 10% 生成一条等值线

    # return output_tif


# def _process_isoline_data(grb, output_path, key, spatial_ref):
#     """
#     生成等值线：气压（2hPa间隔）或位势高度（24单位间隔）
#
#     Args:
#         grb: pygrib 消息对象
#         output_path: 输出文件基础路径
#         key: 数据类型标识（geopotential_height/pressure）
#         spatial_ref: 空间参考系统
#
#     Returns:
#         list: 生成的等值线文件路径列表
#     """
#     # 获取经纬度和数据
#     lat, lon = _get_cached_latlon(grb)
#     data = (grb.values)
#     output_path = output_path.replace("tif", "shp")
#     # 特殊处理 - 位势高度单位转换
#     vals = None
#     if key.startswith('geopotential_height'):
#         data = np.round(data / 10).astype(int)
#         min_val = float(np.min(data))
#         max_val = float(np.max(data))
#         contour_interval = 4  # 位势高度间隔32
#         vals = getArrayData(min_val, max_val)
#     elif key.startswith('pressure'):
#         data = data / 100
#         min_val = float(np.min(data))
#         contour_interval = 2  # 气压间隔2hPa
#     else:
#         raise BusinessException(f"不支持的等值线类型: {key}")
#
#     # 创建基础GeoTIFF（用于生成等值线）
#     base_tif = output_path.replace(".shp", ".tif")
#
#     # 生成基础GeoTIFF
#     _create_geotiff(base_tif, data, lon, lat, spatial_ref.ExportToWkt())
#
#     # 生成等值线Shapefile
#     contour_shp = output_path.replace(".shp", "_unSmooth.shp")  # 直接使用传入的输出路径
#     # deleteEvServer(output_path)
#     generate_contours_from_tif(
#         base_tif,
#         contour_shp,
#         interval=contour_interval,
#         field_name="value",
#         min_val=min_val,
#         vals=vals
#
#     )
#
#     # 可选：生成等值面
#     # polygon_shp = contour_shp.replace(".shp", "_poly.shp")
#     # generate_polygons_from_contours(contour_shp, polygon_shp)
#
#     # 等值线平滑处理
#     gaussian_smooth_line_shp(contour_shp, output_path, 1.5)
#
#     # 删除临时基础文件
#     deletefile(base_tif)
#     deleteShp(contour_shp)
#     # publishEvServerShp(output_path)
#     return output_path


def generate_polygons_from_contours(contour_shp, polygon_shp):
    """
    将等值线转换为等值面
    """
    driver = ogr.GetDriverByName("ESRI Shapefile")
    contour_ds = driver.Open(contour_shp, 0)
    contour_lyr = contour_ds.GetLayer()

    if os.path.exists(polygon_shp):
        driver.DeleteDataSource(polygon_shp)
    poly_ds = driver.CreateDataSource(polygon_shp)
    poly_lyr = poly_ds.CreateLayer("contour_poly", geom_type=ogr.wkbPolygon, srs=contour_lyr.GetSpatialRef())

    field_defn = ogr.FieldDefn("min", ogr.OFTReal)
    poly_lyr.CreateField(field_defn)
    field_defn = ogr.FieldDefn("max", ogr.OFTReal)
    poly_lyr.CreateField(field_defn)

    # 多边形化
    gdal.Polygonize(
        srcBand=contour_lyr.GetLayerDefn().GetFieldIndex("ID"),
        maskBand=contour_lyr.GetLayerDefn().GetFieldIndex("ELEV"),
        outLayer=poly_lyr,
        callback=None
    )

    del contour_ds, poly_ds
    logger.error(f"等值面已生成: {polygon_shp}")


if __name__ == '__main__':
    ec2tif_rain(
        r"D:\gzt\data\grib\ec\20260211\W_NAFP_C_ECMF_20260211172257_P_C1D02111200021112011",
        r"D:\gzt\data\grib\ec\20260211\W_NAFP_C_ECMF_20260211173105_P_C1D02111200021209001",
        r'D:\gzt\data\tif\tmp_ec/ec_test_rain12.tif')

    # process_ec_grib_file(
    #     r"C:\Users\33010\Desktop\fsdownload/W_NAFP_C_ECMF_20260211172701_P_C1D02111200021121001",
    #     r"D:\gzt\data\tif/ec", '2026021120')
