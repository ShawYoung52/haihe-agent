import logging
import os
import re
import time

# from scipy.ndimage import gaussian_filter1d
# import netCDF4
import numpy as np
import pygrib
import rasterio
from osgeo import gdal, ogr

from CustomException import BusinessException


def pattern_match(fileParent, pattern):
    for filename in os.listdir(fileParent):
        if filename.endswith('.bz2'):
            continue
        match = re.search(pattern, filename)
        if match:
            return filename
    return None


def getArrayData(min_val, max_val):
    # 获取 两个值中间固定步长为4的数组，但是要求必须包含588
    if not (min_val <= 588 <= max_val):
        return None

    diff = (588 - min_val) % 4
    if diff == 0:
        start = min_val
    else:
        start = min_val - (4 - diff)

    result = []
    current = start
    while current <= max_val:
        if current >= min_val:
            result.append(current)
        current += 4
    return result


def checkParams(params):
    if params is None or params == {}:
        raise BusinessException("参数不能为空")
def combineTif(file_list, out_tif, keyword):
    if len(file_list) == 0:
        return
    out_tif = out_tif.replace("\\", "/")
    out_tif = out_tif.replace("/tif", "")
    # 文件排序
    file_list.sort()
    # 执行合并
    combine_tif(file_list, out_tif, keyword)
def filter_keyword(keyword, results):
    file_list = []
    for e in results:
        # 正则匹配
        if isKeywordTif(keyword, e):
            file_list.append(e.replace("\\", "/"))
    return file_list
def isKeywordTif(keyword, fileName):
    # 修改后逻辑：确保 keyword 前有 10 或 12 位数字
    pattern = r'^.*_(\d{10}|\d{12})_%s\.tif$' % re.escape(keyword)
    return re.match(pattern, fileName) is not None

def deletefile(file):
    retry = 5
    for i in range(retry):
        try:
            if os.path.exists(file):
                os.remove(file)
            break
        except PermissionError:
            if i == retry - 1:
                logging.error(f"{file} 删除文件被占用，{retry}次重试后仍然失败...")
                raise BusinessException(file + "文件被占用")
            logging.error(f"{file} 删除文件被占用，{i + 1}/{retry}次重试...")
            time.sleep(2)


def deleteShp(file):
    # 安全获取基础文件名（不含扩展名）
    base_name, ext = os.path.splitext(file)
    if ext.lower() != '.shp':
        print(f"File {file} does not have a .shp extension")
        return

    # 定义关联扩展名列表
    related_extensions = ['.dbf', '.shx', '.prj']

    # 逐个删除关联文件
    for ext in related_extensions:
        target_file = os.path.splitext(file)[0] + ext
        try:
            deletefile(target_file)
        except Exception as e:
            print(f"Failed to delete {target_file}: {e}")
    deletefile(file)


def rename_file(file_path, new_name):
    import os
    # 拼接新的文件路径
    new_file_path = os.path.join(os.path.dirname(file_path), new_name)
    try:
        # 重命名文件
        os.rename(file_path, new_file_path)
        print(f"文件重命名成功，新文件名为：{new_file_path}")
    except Exception as e:
        print(f"文件重命名失败：{e}")


def rename_file_batch(file_directory):
    for root, dirs, files in os.walk(file_directory):
        for file in files:
            if 'dp_tem_2m' in file:
                file_path = os.path.join(root, file)
                new_name = file.replace('dp_tem_2m', 'tem_dp_2m')
                # print(file_path,new_name)
                rename_file(file_path, new_name)


def list_file(fileDir):
    res = []
    for root, dirs, files in os.walk(fileDir):
        for file in files:
            file_path = os.path.join(root, file)
            res.append(file_path)
    return res


def list_file_contain_str(fileDir, contain_str=''):
    res = []
    for root, dirs, files in os.walk(fileDir):
        for file in files:
            if contain_str in file:
                file_path = os.path.join(root, file)
                res.append(file_path)
    return res


def list_file_tif(fileDir):
    res = []
    for root, dirs, files in os.walk(fileDir):
        for file in files:
            if file.endswith('.tif') or file.endswith('.shp'):
                file_path = os.path.join(root, file)
                res.append(file_path)
    return res


def flatten_list(nested_list):
    result = []
    if not isinstance(nested_list, list):  # 增加类型防护
        return [nested_list]
    for item in nested_list:
        if isinstance(item, list):
            result.extend(flatten_list(item))
        else:
            result.append(item)
    return result


def get_grib_info(grib_path):
    with pygrib.open(grib_path) as grbs:
        # 遍历所有波段，打印信息
        # for grb in grbs:
        # print(grb)
        for msg in grbs:
            # print(msg)
            print(f"{msg}")
            # print(msg.name, msg.shortName, msg.typeOfLevel, msg.level)


# def getNcInfo(nc_path):
#     nc = netCDF4.Dataset(nc_path)
#     variables = nc.variables
#
#     print("所有变量名称：", list(variables.keys()))
#
#     for var_name in variables:
#         var = variables[var_name]
#         print(f"\n变量名: {var_name}")
#         print("维度:", var.dimensions)
#         print("数据类型:", var.dtype)
#         print("单位:", getattr(var, 'units', '未知'))
#         print("描述:", getattr(var, 'long_name', '无描述'))
#
#     band_data = nc.variables['time'][:]
#     print(band_data)
#     nc.close()


# 不生效
def create_pyramids(input_tif):
    # 打开TIFF文件
    dataset = gdal.Open(input_tif, gdal.GA_Update)

    # 设置金字塔参数
    gdal.SetConfigOption('USE_OVR', 'YES')  # 关键：强制使用 .ovr 格式
    gdal.SetConfigOption('COMPRESS_OVERVIEW', 'LZW')  # 启用LZW压缩
    gdal.SetConfigOption('USE_RRD', 'YES')  # 对于HFA格式生成.rrd文件

    # 创建金字塔（层级为2/4/8/16倍缩小）
    dataset.BuildOverviews("NEAREST", [2, 4, 8, 16])  # 适合分类数据

    # 释放资源
    dataset = None


def buildOverview(infile):
    ds = gdal.Open(infile)
    ds.BuildOverviews("NEAREST", overviewlist=[2, 4, 8, 16])
    ds = None
    del ds




def getValuesByLatLon_gdal(lat, lon, tif_path):
    """
    使用GDAL获取指定地理坐标的值

    :param lat: 纬度
    :param lon: 经度
    :param tif_path: TIFF文件路径
    :return: 包含波段名称和对应数值的字典列表
    """
    try:
        # 类型安全转换
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError) as e:
        raise ValueError("无效的经纬度格式") from e

    try:
        # 打开TIFF文件
        dataset = gdal.Open(tif_path, gdal.GA_ReadOnly)
        if dataset is None:
            raise IOError(f"无法打开文件：{tif_path}")

        # 获取地理变换参数
        geotransform = dataset.GetGeoTransform()

        # 坐标转行列索引
        col = int((lon - geotransform[0]) / geotransform[1])
        row = int((lat - geotransform[3]) / geotransform[5])

        # 边界检查
        if not (0 <= row < dataset.RasterYSize and 0 <= col < dataset.RasterXSize):
            raise IndexError("坐标超出栅格范围")

        # 读取所有波段在指定位置的值和名称
        band_count = dataset.RasterCount
        results = []

        for i in range(1, band_count + 1):
            band = dataset.GetRasterBand(i)
            # 读取单个像素值
            data = band.ReadAsArray(col, row, 1, 1)

            # 获取波段名称
            band_name = band.GetDescription()
            if not band_name or band_name == "":
                # 如果没有描述，则尝试从元数据获取
                band_name = band.GetMetadataItem("BandName") or f"Band_{i}"

            results.append({
                "time": band_name,
                "value": float(data[0, 0])
            })

        return results

    except Exception as e:
        raise e
        # raise IOError(f"文件处理失败：{tif_path}") from e


def getValuesByLatLon(lat, lon, tif_path):
    """
    获取指定地理坐标在所有波段中的值（高效实现）

    :param lat: 纬度 (float或可转换为float的类型)
    :param lon: 经度 (float或可转换为float的类型)
    :param tif_path: TIFF文件路径
    :return: 按波段顺序排列的数值列表
    """
    try:
        # 类型安全转换
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError) as e:
        raise ValueError("无效的经纬度格式") from e

    try:
        with rasterio.open(tif_path) as src:
            # 坐标转行列索引
            row, col = src.index(lon, lat)

            # 边界检查
            if not (0 <= row < src.height and 0 <= col < src.width):
                raise IndexError("坐标超出栅格范围")

            # 高效读取所有波段在单个位置的值
            # 方法1：使用窗口读取（推荐）
            window = rasterio.windows.Window(col, row, 1, 1)
            band_values = src.read(window=window)

            # 方法2：使用sample生成器
            # point = [(lon, lat)]
            # band_values = next(src.sample(point))

            # 提取并转换为Python原生类型
            return [float(band_values[i, 0, 0]) for i in range(src.count)]

    except rasterio.errors.RasterioIOError as e:
        raise IOError(f"文件打开失败：{tif_path}") from e


def subsample_data(data, step=2):
    """按固定步长抽稀数据"""
    return data[::step, ::step]


def combine_tif(tif_list, out_tif, keyword):
    """
    将多个单波段 TIF 合并为多波段 TIF
    :param tif_list: 输入文件路径列表（按波段顺序排列）
    :param out_tif: 输出文件路径
    :return: None
    """
    if not tif_list:
        raise ValueError("输入文件列表不能为空")
    for tif in tif_list:
        if not os.path.exists(tif):
            print(f"文件不存在：{tif}")
    # 读取首个文件获取元数据
    first_ds = gdal.Open(tif_list[0], gdal.GA_ReadOnly)
    if not first_ds:
        raise RuntimeError(f"无法打开文件：{tif_list[0]}")

    # 获取基础参数
    cols = first_ds.RasterXSize
    rows = first_ds.RasterYSize
    geotransform = first_ds.GetGeoTransform()
    projection = first_ds.GetProjection()
    # data_type = first_ds.GetRasterBand(1).DataType
    data_type = gdal.GDT_Int16
    # 创建输出文件
    driver = gdal.GetDriverByName('GTiff')
    out_ds = driver.Create(
        out_tif,
        cols,
        rows,
        len(tif_list),  # 波段数=输入文件数
        data_type,
        # options=['COMPRESS=LZW', 'TILED=YES', "BIGTIFF=YES"]
        options=[
            'COMPRESS=LZW',
            'TILED=YES',
            'PREDICTOR=2',
            'BLOCKXSIZE=256',
            'BLOCKYSIZE=256',
            'BIGTIFF=YES'
        ]
    )

    if out_ds is None:
        raise RuntimeError("无法创建输出文件")

    # 设置地理参考
    out_ds.SetGeoTransform(geotransform)
    out_ds.SetProjection(projection)

    # 遍历处理每个输入文件
    for band_idx, tif_path in enumerate(tif_list, start=1):
        src_ds = gdal.Open(tif_path, gdal.GA_ReadOnly)
        if not src_ds:
            raise RuntimeError(f"无法打开文件：{tif_path}")

        # 校验地理参数一致性
        if (src_ds.RasterXSize != cols or
                src_ds.RasterYSize != rows or
                src_ds.GetGeoTransform() != geotransform):
            raise ValueError(f"文件 {tif_path} 的地理参数不匹配")

        src_band = src_ds.GetRasterBand(1)
        out_band = out_ds.GetRasterBand(band_idx)

        # 复制数据
        data = src_band.ReadAsArray()
        if keyword == 'tem_2m' or keyword == 'tem_dp_2m' or keyword == 'TMP':
            out_band.WriteArray(np.round(data * 100).astype(int))
        elif keyword == 'wind_speed_10m' or keyword == 'wind_speed':
            out_band.WriteArray(np.round(data).astype(int))
        elif keyword == 'wind_direction_10m':
            out_band.WriteArray(np.round(data).astype(int))
        elif keyword == 'gust_10m':
            out_band.WriteArray(np.round(data).astype(int))
        elif keyword.startswith('rain_total') or keyword.startswith('rain_convective'):
            out_band.WriteArray(np.round(data).astype(int))
        else:
            out_band.WriteArray(np.round(data, 3))
        # 复制元数据
        time_str = os.path.basename(tif_path).split('_')[1]  # 从文件名提取时间戳
        out_band.SetDescription(f"{time_str}")  # 设置波段描述
        out_band.SetMetadataItem("BandName", f"{time_str}")  # 设置波段名称

        out_band.SetNoDataValue(999)
        src_ds = None  # 关闭文件

    out_ds.FlushCache()
    out_ds = None

    print(f"成功生成多波段文件：{out_tif}")


def calculate_wind_speed(u, v):
    return np.sqrt(u ** 2 + v ** 2)


def calculate_wind_direction(u, v):
    """计算风向，返回0-360度，0度为北"""
    # 使用更稳定的实现，避免数值不稳定
    radians = np.arctan2(-u, -v)
    degrees = np.degrees(radians)
    return degrees % 360


# def gaussian_smooth_line_shp(input_path, output_path, sigma=1.0):
#     """
#     使用高斯平滑处理线状SHP文件
#
#     参数:
#     input_path: 输入SHP文件路径
#     output_path: 输出SHP文件路径
#     sigma: 高斯平滑的标准差(控制平滑程度)
#     """
#     # 打开输入文件
#     in_ds = ogr.Open(input_path)
#     if in_ds is None:
#         raise RuntimeError(f"无法打开输入文件: {input_path}")
#     in_layer = in_ds.GetLayer()
#
#     # 创建输出文件
#     driver = ogr.GetDriverByName("ESRI Shapefile")
#     if driver.DeleteDataSource(output_path) != 0:
#         print(f"覆盖现有文件: {output_path}")
#     out_ds = driver.CreateDataSource(output_path)
#     spatial_ref = in_layer.GetSpatialRef()
#     out_layer = out_ds.CreateLayer(
#         "smoothed_line",
#         srs=spatial_ref,
#         geom_type=ogr.wkbLineString
#     )
#
#     # 复制属性字段
#     layer_defn = in_layer.GetLayerDefn()
#     for i in range(layer_defn.GetFieldCount()):
#         out_layer.CreateField(layer_defn.GetFieldDefn(i))
#
#     # 进度计数器
#     feature_count = in_layer.GetFeatureCount()
#     # print(f"开始处理 {feature_count} 个要素...")
#
#     # 高斯平滑处理
#     for i, in_feature in enumerate(in_layer):
#         geom = in_feature.GetGeometryRef()
#         if geom is None:
#             continue
#
#         # 类型检查与日志
#         geom_type = geom.GetGeometryName()
#         # print(f"要素 {i + 1} 的几何类型: {geom_type}, 点数: {geom.GetPointCount()}")
#
#         if geom.GetPointCount() <= 1:
#             print("无效几何：点数不足")
#             continue
#
#         smoothed_parts = []
#
#         # 根据几何类型处理
#         if geom_type == "MULTILINESTRING":
#             for ipart in range(geom.GetGeometryCount()):
#                 line = geom.GetGeometryRef(ipart)
#                 process_line(line, smoothed_parts, sigma)
#         elif geom_type == "LINESTRING":
#             # 直接处理 LineString
#             process_line(geom, smoothed_parts, sigma)
#         else:
#             print(f"不支持的几何类型: {geom_type}")
#             continue
#
#         # 创建输出要素
#         out_feature = ogr.Feature(out_layer.GetLayerDefn())
#
#         # 处理多部分几何
#         if len(smoothed_parts) > 1:
#             multi_line = ogr.Geometry(ogr.wkbMultiLineString)
#             for part in smoothed_parts:
#                 multi_line.AddGeometry(part)
#             out_feature.SetGeometry(multi_line)
#         elif smoothed_parts:
#             out_feature.SetGeometry(smoothed_parts[0])
#         else:
#             print("警告：未生成任何平滑几何")
#             continue  # 跳过无结果的要素
#
#         # 复制属性并写入
#         for j in range(layer_defn.GetFieldCount()):
#             out_feature.SetField(j, in_feature.GetField(j))
#         out_layer.CreateFeature(out_feature)
#
#         # 进度显示
#         # if (i + 1) % 100 == 0 or (i + 1) == feature_count:
#         #     print(f"处理进度: {i + 1}/{feature_count} ({((i + 1) / feature_count) * 100:.1f}%)")
#
#     # 释放资源
#     in_ds = None
#     out_ds = None
#     print(f"处理完成! 输出文件: {output_path}")


# def process_line(line, smoothed_parts, sigma=1.0):
#     if line.GetPointCount() <= 1:
#         return
#
#     points = line.GetPoints()
#     x, y = zip(*points)
#
#     # 如果是闭合环，强制首尾点一致
#     is_closed = (points[0][0] == points[-1][0] and points[0][1] == points[-1][1])
#
#     # 高斯平滑
#     x_smooth = gaussian_filter1d(np.array(x), sigma=sigma)
#     y_smooth = gaussian_filter1d(np.array(y), sigma=sigma)
#
#     # 重建线几何
#     smoothed_line = ogr.Geometry(ogr.wkbLineString)
#     for i in range(len(x_smooth)):
#         smoothed_line.AddPoint(x_smooth[i], y_smooth[i])
#
#     # 如果是闭合环，强制首尾点重合
#     if is_closed:
#         smoothed_line.SetPoint_2D(0, x_smooth[0], y_smooth[0])  # 重新设置第一个点
#         smoothed_line.SetPoint_2D(len(x_smooth) - 1, x_smooth[0], y_smooth[0])  # 设置最后一个点为第一个点
#
#     smoothed_parts.append(smoothed_line)


# if __name__ == '__main__':
    # getNcInfo(
    #     r'D:\gzt\data\grib\predictor-grib\WEA\000\2025\202504\20250402\2025040220\GRID_TJQX_PUB_WEA_AEHH_000_DT_20250402200000_000-240_1003.nc')
    # gaussian_smooth_line_shp(r'D:\gzt\data\shp\Grapes-GFS\gfs_2025042411_geopotential_height_500_unSmooth.shp',
    #                          r'D:\gzt\data\shp\Grapes-GFS\gfs_2025042411_geopotential_height_500_15.shp', 1.5)
