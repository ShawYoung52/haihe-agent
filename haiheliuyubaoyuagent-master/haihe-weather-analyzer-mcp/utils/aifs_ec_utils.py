# test_grib2nc.py

from commonUtils import buildOverview


def batch_process_aifs_rainfall_to_tif(input_dir, output_dir, shp_path=None):
    """
    批量处理 AIFS GRIB2 降雨数据转换为 TIF

    功能：
        1. 支持每日 4 次起报（00/06/12/18 UTC）
        2. 计算 6h/12h/24h 时段累计降雨量（做差）
        3. 输出文件名格式：{start_time}_{forecast_hours}.tif
        4. 重采样为 0.02°分辨率
        5. 使用 shapefile 矢量边界裁剪
        6. 坐标系：WGS84 (EPSG:4326)

    Args:
        input_dir: GRIB2 文件输入目录
        output_dir: TIF 文件输出目录
        shp_path: shapefile 边界文件路径（可选）
    """
    import os
    import re
    from datetime import datetime, timedelta

    # 确保输出目录存在
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)
        print(f"📁 创建输出目录：{output_dir}")

    # 定义需要的预报时效（小时）
    forecast_steps = [0, 6, 12, 18, 24]

    # 获取所有 GRIB2 文件并按起报时间分组 20260308180000-336h-oper-fc.grib2
    pattern = r'^(\d{14})-(\d+)h-oper-fc\.grib2$'
    grib_files_by_base_time = {}

    for filename in os.listdir(input_dir):
        match = re.match(pattern, filename)
        if match:
            date_str = match.group(1)  # 如 20260304120000
            forecast_hour = int(match.group(2))  # 如 0, 6, 12

            # 提取起报时间（前 10 位：YYYYMMDDHH）
            base_time_str = date_str[:10]

            # 只处理 18 UTC 起报的文件
            utc_hour = int(base_time_str[8:10])
            if utc_hour == 18:
                if base_time_str not in grib_files_by_base_time:
                    grib_files_by_base_time[base_time_str] = {}

                file_path = os.path.join(input_dir, filename)
                grib_files_by_base_time[base_time_str][forecast_hour] = {
                    'path': file_path,
                    'filename': filename,
                    'date_str': date_str,
                    'forecast_hour': forecast_hour
                }

    if not grib_files_by_base_time:
        print(f"⚠️ 未找到符合条件的 GRIB2 文件（需要 00/06/12/18 UTC 起报）")
        return []

    # print(f"\n📋 找到 {len(grib_files_by_base_time)} 个起报时次的 GRIB2 数据:")
    # for base_time in sorted(grib_files_by_base_time.keys()):
    #     files = grib_files_by_base_time[base_time]
    #     print(f"   - 起报时间：{base_time}, 可用预报时效：{sorted(files.keys())}h")

    # 处理每个起报时次
    result_files = []
    success_count = 0
    fail_count = 0

    for base_time_str in sorted(grib_files_by_base_time.keys()):
        files = grib_files_by_base_time[base_time_str]

        # print(f"\n{'=' * 60}")
        # print(f"🕐 处理起报时间：{base_time_str}")
        # print(f"{'=' * 60}")

        # 解析起报时间（UTC）
        base_time_utc = datetime.strptime(base_time_str, '%Y%m%d%H')

        # 转换为中国时间（UTC+8）
        base_time_china = base_time_utc + timedelta(hours=8)

        # 检查是否有 0h 的数据作为基准
        if 0 not in files:
            print(f"   ⚠️ 缺少 0h 基准数据，跳过该起报时次")
            fail_count += 1
            continue

        # 存储各时次的 TIF 路径（用于后续处理）
        tif_paths = {}

        # 第一步：将所有预报时效的 GRIB2 转为临时 TIF
        print(f"\n📍 步骤 1: 转换 GRIB2 为临时 TIF...")
        for forecast_hour in sorted(files.keys()):
            try:
                file_info = files[forecast_hour]
                grib_path = file_info['path']

                # 计算有效时间（中国时间）
                valid_time_china = base_time_china + timedelta(hours=forecast_hour)

                # 临时输出路径
                temp_filename = f"temp_{base_time_str}_{forecast_hour}h.tif"
                temp_tif_path = os.path.join(output_dir, temp_filename)

                print(f"   [{forecast_hour}h] {file_info['filename']} -> {temp_filename}")

                # 转换为 TIF（包含重采样和裁剪）
                convert_grib2_to_tif_with_resample(
                    grib_path,
                    temp_tif_path,
                    shp_path=shp_path,
                    scale=0.02
                )

                tif_paths[forecast_hour] = temp_tif_path

            except Exception as e:
                print(f"   ❌ 转换失败 {forecast_hour}h: {str(e)}")
                fail_count += 1
                continue

        # 第二步：计算各时刻未来时段累计降雨量（做差）
        print(f"\n📍 步骤 2: 计算各预报时效时刻的未来时段累计降雨量...")

        # 获取所有可用的预报时效（排序）
        available_hours = sorted(tif_paths.keys())

        if len(available_hours) < 2:
            print(f"   ⚠️ 可用预报时效不足 2 个，无法计算累计值")
            continue

        # print(f"   📊 可用预报时效：{available_hours}h")

        # 定义需要计算的时段长度（未来多少小时）
        period_steps = [6, 12, 24]

        # 对每个可用的起始时刻，计算其未来 6/12/24 小时累计
        for start_hour in available_hours:
            for step in period_steps:
                end_hour = start_hour + step

                # 检查结束时刻是否存在
                if end_hour not in tif_paths:
                    continue

                try:
                    start_tif = tif_paths[start_hour]
                    end_tif = tif_paths[end_hour]

                    # 计算该时刻的有效时间（中国时间）
                    # 起报时间（UTC）+ 预报时效 = 有效时间（UTC）
                    # 再 +8 小时 = 中国时间
                    valid_time_utc = base_time_utc + timedelta(hours=start_hour)
                    valid_time_china = valid_time_utc + timedelta(hours=8)
                    valid_time_str = valid_time_china.strftime('%Y%m%d%H')

                    # 输出文件名：ec_{有效时间}_rain_total_{时段}.tif
                    output_filename = f"ec_{valid_time_str}_rain_total_{step}h.tif"
                    output_tif_path = os.path.join(output_dir, output_filename)

                    # 如果文件已存在，先删除
                    if os.path.exists(output_tif_path):
                        os.remove(output_tif_path)

                    print(f"   计算 {start_hour}h→{end_hour}h ({valid_time_str}): {step}h 累计")
                    print(f"      {os.path.basename(end_tif)} - {os.path.basename(start_tif)}")

                    # 计算差值（end - start）
                    calculate_tif_difference(end_tif, start_tif, output_tif_path)

                    # 创建索引
                    if os.path.exists(output_tif_path):
                        buildOverview(output_tif_path)

                    result_files.append(output_tif_path)
                    success_count += 1

                except Exception as e:
                    print(f"   ❌ 计算 {start_hour}h→{end_hour}h 失败：{str(e)}")
                    fail_count += 1
                    continue

        # 第三步：清理临时文件
        print(f"\n📍 步骤 3: 清理临时文件...")
        for forecast_hour, temp_tif in tif_paths.items():
            try:
                if os.path.exists(temp_tif):
                    os.remove(temp_tif)
                    # print(f"   删除：{os.path.basename(temp_tif)}")
            except Exception as e:
                continue
                # print(f"   ⚠️ 删除失败 {os.path.basename(temp_tif)}: {str(e)}")

    # 打印汇总信息
    print(f"\n{'=' * 60}")
    print(f"✅ 批量处理完成!")
    print(f"   成功：{success_count} 个")
    print(f"   失败：{fail_count} 个")
    print(f"   输出目录：{output_dir}")

    # if result_files:
    #     print(f"\n📁 生成的 TIF 文件列表:")
    #     for tif_file in sorted(result_files):
    #         print(f"   - {os.path.basename(tif_file)}")

    return result_files


def convert_grib2_to_tif_with_resample(grib_file, tif_file, shp_path=None, scale=0.02):
    """
    将 GRIB2 总降水量字段转换为 TIF，包含重采样和裁剪

    Args:
        grib_file: GRIB2 文件路径
        tif_file: 输出 TIF 文件路径
        shp_path: shapefile 边界文件路径（可选）
        scale: 重采样分辨率（默认 0.02°）
    """
    import pygrib
    from osgeo import gdal, osr
    import numpy as np
    import os

    # 临时文件（原始分辨率）
    temp_tif = tif_file.replace('.tif', '_temp.tif')

    try:
        # 打开 GRIB2 文件
        grbs = pygrib.open(grib_file)

        # 选择总降水量字段
        matches = grbs.select(name='Total Precipitation', typeOfLevel='surface', level=0)

        if not matches:
            # 尝试模糊匹配
            matches = [grb for grb in grbs if 'precipitation' in grb.name.lower() and grb.typeOfLevel == 'surface']

        if not matches:
            raise ValueError(f"未找到总降水量字段：{grib_file}")

        grb = matches[0]

        # 获取数据值和经纬度
        data, lats, lons = grb.data()

        # 获取经纬度范围
        lat_min, lat_max = lats.min(), lats.max()
        lon_min, lon_max = lons.min(), lons.max()

        # 创建 GeoTIFF（原始分辨率）
        rows, cols = data.shape
        x_res = (lon_max - lon_min) / (cols - 1)
        y_res = (lat_max - lat_min) / (rows - 1)
        geo_transform = (lon_min, x_res, 0, lat_max, 0, -y_res)

        driver = gdal.GetDriverByName('GTiff')

        # 确保输出目录存在
        output_dir = os.path.dirname(tif_file)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        # 创建临时 TIF
        out_ds = driver.Create(temp_tif, cols, rows, 1, gdal.GDT_Float32)

        if out_ds is None:
            raise RuntimeError("无法创建 TIF 文件")

        # 设置地理变换和投影（WGS84）
        out_ds.SetGeoTransform(geo_transform)
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)
        out_ds.SetProjection(srs.ExportToWkt())

        # 数据处理：单位转换 + NoData 处理
        new_no_data = -9999.0
        data_processed = data.copy().astype(np.float32)

        # 获取原始 NoData 值
        original_no_data = grb.missingValue if hasattr(grb, 'missingValue') else None

        # 标记需要设为 NoData 的像素
        no_data_mask = (data_processed <= 0)
        if original_no_data is not None:
            no_data_mask |= (data_processed == original_no_data)

        # 单位转换：kg·m⁻² → mm
        data_processed = data_processed * 1.0

        # 设为 NoData
        data_processed[no_data_mask] = new_no_data

        # 写入数据
        out_band = out_ds.GetRasterBand(1)
        out_band.WriteArray(data_processed)
        out_band.SetNoDataValue(new_no_data)

        # 设置元数据
        out_band.SetDescription(f"Total_Precipitation_{grb.forecastTime}h")
        out_band.SetMetadataItem("Parameter", grb.name)
        out_band.SetMetadataItem("Units", "mm")
        out_band.SetMetadataItem("ForecastTime", str(grb.forecastTime))
        out_band.SetMetadataItem("StartDate", grb.analDate.strftime('%Y-%m-%d %H:%M'))

        out_ds.FlushCache()
        out_ds = None
        grbs.close()

        # 第二步：重采样和裁剪
        if shp_path and os.path.exists(shp_path):
            warp_with_shp_boundary_for_common(temp_tif, tif_file, shp_path=shp_path, scale=scale)
            # 删除临时文件
            if os.path.exists(temp_tif):
                os.remove(temp_tif)
        else:
            # 如果没有 shapefile，直接重命名
            if os.path.exists(tif_file):
                os.remove(tif_file)
            os.rename(temp_tif, tif_file)

        # print(f"   ✅ 转换完成：{os.path.basename(tif_file)}")

    except Exception as e:
        print(f"   ❌ 转换失败：{str(e)}")
        # 清理临时文件
        if os.path.exists(temp_tif):
            os.remove(temp_tif)
        raise


def calculate_tif_difference(tif_end, tif_start, tif_output):
    """
    计算两个 TIF 文件的差值（end - start），得到时段累计降雨量

    Args:
        tif_end: 结束时间 TIF 文件
        tif_start: 起始时间 TIF 文件
        tif_output: 输出差值 TIF 文件
    """
    from osgeo import gdal
    import numpy as np

    # 打开文件
    ds_end = gdal.Open(tif_end)
    ds_start = gdal.Open(tif_start)

    if ds_end is None or ds_start is None:
        raise RuntimeError("无法打开 TIF 文件")

    # 读取数据
    band_end = ds_end.GetRasterBand(1)
    band_start = ds_start.GetRasterBand(1)

    data_end = band_end.ReadAsArray().astype(np.float32)
    data_start = band_start.ReadAsArray().astype(np.float32)

    # 获取 NoData 值
    no_data_end = band_end.GetNoDataValue()
    no_data_start = band_start.GetNoDataValue()
    no_data_output = -9999.0

    # 计算差值
    diff_data = data_end - data_start

    # 处理 NoData：任意一个为 NoData，结果即为 NoData
    no_data_mask = (data_end == no_data_end) | (data_start == no_data_start) | \
                   (np.isnan(data_end)) | (np.isnan(data_start))
    diff_data[no_data_mask] = no_data_output

    # 确保差值不为负（理论上不应该出现）
    diff_data[diff_data < 0] = 0

    # 获取地理信息
    geo_transform = ds_end.GetGeoTransform()
    projection = ds_end.GetProjection()

    rows, cols = diff_data.shape

    # 创建输出文件
    driver = gdal.GetDriverByName('GTiff')
    out_ds = driver.Create(tif_output, cols, rows, 1, gdal.GDT_Float32,
                           options=['COMPRESS=LZW', 'TILED=YES'])

    if out_ds is None:
        raise RuntimeError("无法创建输出文件")

    # 设置地理信息
    out_ds.SetGeoTransform(geo_transform)
    out_ds.SetProjection(projection)

    # 写入数据
    out_band = out_ds.GetRasterBand(1)
    out_band.WriteArray(diff_data)
    out_band.SetNoDataValue(no_data_output)

    # 设置元数据
    # out_band.SetDescription("Period_Accumulated_Precipitation")
    # out_band.SetMetadataItem("Parameter", "Accumulated_Precipitation")
    # out_band.SetMetadataItem("Units", "mm")
    # out_band.SetMetadataItem("NoDataValue", str(no_data_output))
    # out_band.SetMetadataItem("CalculationMethod", "End_Time - Start_Time")
    #
    # # 统计信息
    # valid_data = diff_data[diff_data > 0]
    # if len(valid_data) > 0:
    #     out_band.SetMetadataItem("Min_Value", f"{valid_data.min():.2f}")
    #     out_band.SetMetadataItem("Max_Value", f"{valid_data.max():.2f}")
    #     out_band.SetMetadataItem("Mean_Value", f"{valid_data.mean():.2f}")
    #     out_band.SetMetadataItem("Sum_Value", f"{valid_data.sum():.2f}")

    out_ds.FlushCache()
    out_ds = None
    ds_end = None
    ds_start = None

    # print(f"      差值计算完成：{os.path.basename(tif_output)}")
    # if len(valid_data) > 0:
    #     print(f"         范围：{valid_data.min():.2f} - {valid_data.max():.2f} mm")
    #     print(f"         平均：{valid_data.mean():.2f} mm")
    #     print(f"         累计：{valid_data.sum():.2f} mm")


def warp_with_shp_boundary_for_common(tifpath, outputtif, shp_path=None, scale=0.02):
    """
    通用版本的 shapefile 矢量边界重采样截取

    参数:
        tifpath: 输入 TIFF 文件路径
        outputtif: 输出 TIFF 文件路径
        shp_path: shapefile 边界文件路径
        scale: 重采样分辨率
    """
    from osgeo import gdal
    import os

    if not os.path.exists(tifpath):
        raise FileNotFoundError(f"输入文件不存在：{tifpath}")

    if os.path.exists(outputtif):
        os.remove(outputtif)

    if shp_path is None or not os.path.exists(shp_path):
        raise FileNotFoundError(f"shapefile 文件不存在：{shp_path}")

    try:
        # 读取 shapefile 边界
        boundary_extent = get_shp_boundary_simple(shp_path)
        # print(f"   📍 使用 shapefile 边界：{boundary_extent}")

        # 打开输入数据集
        input_ds = gdal.Open(tifpath)
        if input_ds is None:
            raise RuntimeError(f"无法打开输入文件：{tifpath}")

        # 创建裁剪选项
        wo = gdal.WarpOptions(
            resampleAlg=gdal.GRIORA_Bilinear,
            xRes=scale,
            yRes=scale,
            format='GTiff',
            dstSRS=input_ds.GetProjection(),
            outputBounds=boundary_extent,
            cutlineDSName=shp_path,
            cropToCutline=True
        )

        # 执行重采样和裁剪
        gdal.Warp(outputtif, tifpath, options=wo)
        # print(f"   ✅ 重采样并裁剪完成：{os.path.basename(outputtif)}")

    except Exception as e:
        raise RuntimeError(f"使用 shapefile 边界重采样失败：{str(e)}") from e
    finally:
        if 'input_ds' in locals():
            input_ds = None


def get_shp_boundary_simple(shp_path):
    """
    简单版本：读取 shapefile 的边界范围

    返回：(minX, minY, maxX, maxY)
    """
    from osgeo import ogr

    datasource = ogr.Open(shp_path)
    if datasource is None:
        raise RuntimeError(f"无法打开 shapefile 文件：{shp_path}")

    try:
        layer = datasource.GetLayer()
        extent = layer.GetExtent()

        # extent 是一个元组 (minX, maxX, minY, maxY)
        minX, maxX, minY, maxY = extent

        # print(f"   📍 Shapefile 边界范围：({minX:.4f}, {minY:.4f}) - ({maxX:.4f}, {maxY:.4f})")

        return (minX, minY, maxX, maxY)

    finally:
        datasource = None



if __name__ == '__main__':
    from datetime import datetime, timedelta

    # 获取未来一天的日期
    tomorrow = datetime.now() - timedelta(days=1)
    date_str = tomorrow.strftime('%Y%m%d')

    # 构建输入输出路径
    input_dir = rf'D:/EC_AIFS/2026/{date_str}'
    output_dir = rf'D:/EC_AIFS/output'
    shp_path = r'D:/gzt/天津气象台流域升级/数据/海河流域边界.shp'  # 海河流域边界

    # 批量处理
    result = batch_process_aifs_rainfall_to_tif(input_dir, output_dir, shp_path)

