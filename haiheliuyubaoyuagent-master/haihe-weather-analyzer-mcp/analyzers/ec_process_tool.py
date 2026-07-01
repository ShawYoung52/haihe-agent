import datetime
import logging
import multiprocessing
import os
import time

from utils.commonUtils import buildOverview, pattern_match, flatten_list
from utils.ec_grib2tif_utils import ec2tif_rain

logger = logging.getLogger(__name__)
# KEYWORDS_COMBINE = ['tem_2m', 'wind_direction_10m', 'wind_speed_10m', 'rain_total_12h', 'rain_total_24h',
#                     'rain_convective_12h', 'rain_convective_24h', 'gust_10m',
#                     'tem_dp_2m']
KEYWORDS_COMBINE = ['rain_total_12h', 'rain_total_24h']

cpuNum = int(multiprocessing.cpu_count() / 2)

PREDICTION_HOUR = 240


def extend_ec_process(result_paths, tif_path, datetime_input, keywords=None):
    # tif_path = tif_path.replace("\\", "/")
    # if keywords is None:
    #     keywords = KEYWORDS_COMBINE
    start_total_time = time.time()
    tasks3 = []
    # tasks4 = []
    for result_path in result_paths:
        if result_path.endswith(".tif"):
            tasks3.append((result_path,))
    dateStr = datetime_input.strftime("%Y%m%d%H")
    # date_end = datetime_input + datetime.timedelta(hours=PREDICTION_HOUR)
    # date_end_str = date_end.strftime("%Y%m%d%H")
    # for keyword in keywords:
    #     file_list = filter_keyword(keyword, result_paths)
    #     tasks4.append((file_list, fr"{tif_path}/{keyword}_{dateStr}_{date_end_str}.tif", keyword))

    with multiprocessing.Pool(processes=int(cpuNum / 2)) as pool:
        try:
            pool.starmap(buildOverview, tasks3)
        except Exception as e:
            logger.error(f" ec {dateStr} 文件增加金字任务异常: {e}")
        finally:
            pool.close()
            pool.join()
            elapsed = time.time() - start_total_time
            logger.info(f"✅ ec {dateStr} 文件增加金字塔完成，耗时: {elapsed:.2f} 秒")
    start_time = time.time()
    # with multiprocessing.Pool(processes=int(cpuNum / 2)) as pool:
    #     try:
    #         pool.starmap(combineTif, tasks4)
    #     except Exception as e:
    #         logger.error(f"ec {dateStr} 各要素合并tif任务异常: {e}")
    #     finally:
    #         pool.close()
    #         pool.join()
    #         elapsed = time.time() - start_time
    #         logger.info(f"✅ ec {dateStr} 各要素合并tif完成，耗时: {elapsed:.2f} 秒")


def product_EC(datetime_input, grib_parent_path, tif_path):
    start_total_time = time.time()  # 整体开始时间
    # 日期格式化
    china_time = datetime_input + datetime.timedelta(hours=8)
    dateStr = datetime_input.strftime("%Y%m%d")
    dateStr2 = datetime_input.strftime("%m%d%H")
    # fileParent = fr"{grib_parent_path}{dateStr}"
    logger.info(f"EC-{dateStr}生产任务 开始处理: ")
    result_paths = []
    tasks_rain = []
    i = 0
    while i <= PREDICTION_HOUR:
        # 日期加i小时
        dateHour = datetime_input + datetime.timedelta(hours=i)
        dateHourStr = dateHour.strftime("%m%d%H")
        china_time = dateHour + datetime.timedelta(hours=8)  # 得到中国区时间
        tif_path_date = china_time.strftime("%Y%m%d%H")
        # W_NAFP_C_ECMF_20250616051642_P_C1D061600 00 061600011.bz2
        grib_path_pattern = fr'W_NAFP_C_ECMF_{dateStr}\d{{6}}_P_C1D{dateStr2}00{dateHourStr}001'
        grib_path = pattern_match(grib_parent_path, grib_path_pattern)
        if grib_path is None:
            print(f"ec 未找到匹配的 GRIB 文件: {grib_path_pattern}")
            step = 3 if i < 120 else 6
            i += step
            continue
        if i <= (240 - 12):
            k1 = i + 12
            dateHour = datetime_input + datetime.timedelta(hours=k1)
            dateHourStr = dateHour.strftime("%m%d%H")
            grib_path_pattern = fr'W_NAFP_C_ECMF_{dateStr}\d{{6}}_P_C1D{dateStr2}00{dateHourStr}001'
            gribpath12h = pattern_match(grib_parent_path, grib_path_pattern)
            output_tif_rain_total_12h = os.path.join(tif_path, f"ec_{tif_path_date}_rain_total_12h.tif")
            try:
                if gribpath12h:
                    tasks_rain.append(
                        (os.path.join(grib_parent_path, grib_path),
                         os.path.join(grib_parent_path, gribpath12h),
                         output_tif_rain_total_12h,))
            except Exception as e:
                logger.error(f"检查文件 {output_tif_rain_total_12h} 存在性失败: {e}")
        if i <= (240 - 24):
            k2 = i + 24
            dateHour = datetime_input + datetime.timedelta(hours=k2)
            dateHourStr = dateHour.strftime("%m%d%H")
            grib_path_pattern = fr'W_NAFP_C_ECMF_{dateStr}\d{{6}}_P_C1D{dateStr2}00{dateHourStr}001'
            gribpath24h = pattern_match(grib_parent_path, grib_path_pattern)
            output_tif_rain_total_24h = os.path.join(tif_path, f"ec_{tif_path_date}_rain_total_24h.tif")
            try:
                if gribpath24h:
                    tasks_rain.append(
                        (os.path.join(grib_parent_path, grib_path),
                         os.path.join(grib_parent_path, gribpath24h),
                         output_tif_rain_total_24h,))
            except Exception as e:
                logger.error(f"检查文件 {output_tif_rain_total_24h} 存在性失败: {e}")
        step = 3 if i < 120 else 6
        i += step
    # 第一阶段处理
    start_time = time.time()
    with multiprocessing.Pool(processes=cpuNum) as pool:
        try:
            results = pool.starmap(ec2tif_rain, tasks_rain)
            for res in results:
                if isinstance(res, list):
                    result_paths.extend(res)
                elif res is not None:
                    result_paths.append(res)
        except Exception as e:
            logger.error(f"第一阶段12、24小时累计雨量并行处理任务异常: {e}", exc_info=True)  # 记录完整堆栈
        finally:
            pool.close()
            pool.join()
            elapsed = time.time() - start_time
            logger.info(f"✅ec 所有12、24小时累计雨量并行处理完成，总耗时: {elapsed:.2f} 秒")
    result_paths = flatten_list(result_paths)
    urls = list(set(result_paths))
    extend_ec_process(urls, tif_path, datetime_input=china_time, keywords=None)

    end_time_total = time.time() - start_total_time
    logger.info(f"✅ec 处理完成总耗时: {end_time_total:.2f} 秒")
    return result_paths


# todo 添加到crontab中每日4点执行一次
if __name__ == '__main__':
    # 获取当前时间并减去一天
    # current_time = datetime.datetime.now()
    current_time = datetime.datetime.strptime("2025-07-01 04:00:00", "%Y-%m-%d %H:%M:%S")
    previous_day = current_time - datetime.timedelta(days=1)

    # 设置时间为当天的12:00:00
    date_product = datetime.datetime.combine(
        previous_day.date(),
        datetime.time(12, 0, 0, 0)
    )
    # import pyproj
    #
    # print("pyproj version:", pyproj.__version__)
    # print("PROJ version:", pyproj.proj_version_str)
    #
    # import osgeo.gdal as gdal
    #
    # print("GDAL version:", gdal.__version__)

    # import os
    #
    # # 查看所有环境变量
    # print("所有环境变量:")
    # for key, value in os.environ.items():
    #     print(f"{key}: {value}")
    #
    # 查看特定环境变量
    # print(f"PROJ_LIB: {os.environ.get('PROJ_LIB', '未设置')}")
    # print(f"PATH: {os.environ.get('PATH', '未设置')}")

    product_EC(datetime_input=date_product,
               tif_path=r'D:\gzt\data\tif/ec',
               grib_parent_path=r'D:\gzt\data\grib\ec\20250630')