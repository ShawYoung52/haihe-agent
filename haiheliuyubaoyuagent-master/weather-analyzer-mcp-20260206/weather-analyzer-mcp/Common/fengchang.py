import configparser
import os
from datetime import timedelta, datetime

import meteva.base as meb      # 该模块用于IO和基础计算MDFS.py
import numpy as np
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from cartopy.io import shapereader
from metpy.units import units

import Util.MDFS  as mdfs
import pandas as pd
from Util.draw import drawShp


config = configparser.ConfigParser()
config.read("../config.ini", encoding='utf-8')
root_dir = config.get('common', 'rootDir')
file3path = config.get('common', 'file3path')
province_path = config.get('common', 'provincePath')
SAVE_DIR = config.get('common', 'saveDir')
LON_MIN = float(config.get('common', 'lonMin'))
LON_MAX = float(config.get('common', 'lonMax'))
LAT_MIN = float(config.get('common', 'latMin'))
LAT_MAX = float(config.get('common', 'latMax'))
SAFE_NORMAL = 0.07
SAFE_BOUND = 0.04
BOUND_THRESH = 0.15
CLIP_PAD = 0.25
# listdir = os.listdir(root_dir)
# for file2path in listdir:
#     path_join = os.path.join(root_dir, file2path, file3path)
#     fileList = os.listdir(path_join)
#     for fileName in fileList:
#         data_path=os.path.join(path_join,fileName)
#         # 读取数据
#         # data_path = r'E:\BaiduNetdiskDownload\micaps\20250628\UPPER_AIR\MANUAL_ANALYSIS\HGT\500\20250628080000.000'
#         file_name = os.path.basename(data_path)
#         time_str = file_name.split('.')[0]
#         time = datetime.strptime(time_str, '%Y%m%d%H%M%S')
#         fileYearStr = str(time.year)
#         fileDayStr = str(time.strftime('%Y%m%d'))
#         # 构造文件名：时间格式化字符串 + "aaa" + 后缀
#         save_filename = f"{time.strftime('%Y%m%d%H%M%S')}_风场.png"
#         # 拼接完整保存路径
#         save_path_dir = os.path.join(SAVE_DIR,fileYearStr,fileDayStr)
#         save_path = os.path.join(save_path_dir, save_filename)
#         # 确保保存目录存在（不存在则创建）
#         os.makedirs(save_path_dir, exist_ok=True)
#         # data_path=r'E:\BaiduNetdiskDownload\micaps\20250628\UPPER_AIR\PLOT\500\20250628080000.000'
#         file_name = os.path.basename(data_path)
#         time_str = file_name.split('.')[0]
#         time = datetime.strptime(time_str, '%Y%m%d%H%M%S')
#         # data=meb.read_sta_alt_from_micaps3(data_path)
#         # meb.read_gridwind_from_micaps2(data_path)
#         # data=meb.read_stadata_from_micaps1_2_8(data_path)
#         # print(data)
#         # data = {
#         #     "ID": [...],  # 你的 ID 列表
#         #     "Lon": [...],  # 你的经度列表
#         #     "Lat": [...],  # 你的纬度列表
#         #     "Height": [...],  # 3 列（高度）
#         #     "DewPoint": [...],  # 803 列（露点）
#         #     "Temp_K": [...],  # 421 列（温度，K）
#         #     "WindDir": [...],  # 201 列（风向，角度）
#         #     "WindSpeed": [...],  # 203 列（风速）
#         # }
#         station = mdfs.Station(data_path)
#         data = station.data
#         df = pd.DataFrame(station.data)
#         # time = station.utc_time
#         # time=time + timedelta(hours=8)
#         dsc = station.level_dsc
#         level = station.level
#         # df.rename(columns={'ID': 'ID', 'Lon': 'Lon', 'Lat': 'Lat', '3': '3', '803': 'DewPointDif',
#         # '421': 'Height', '601': 'tem','201':"wind_v",'203':"wind_s"}, inplace=True)
#         # df_new = df.rename(columns={'ID': 'ID', 'Lon': 'Lon', 'Lat': 'Lat', '3': '3', '803': 'DewPointDif', '421': 'Height', '601': 'tem','201':"wind_v",'203':"wind_s"}, inplace=True)
#
#
#         df_filtered = df.dropna(subset=['Lon', 'Lat'])  # 移除经纬度空值行
#         df_filtered = df_filtered[
#             (df_filtered['Lon'] >= LON_MIN) & (df_filtered['Lon'] <= LON_MAX) &
#             (df_filtered['Lat'] >= LAT_MIN) & (df_filtered['Lat'] <= LAT_MAX)
#         ]
#         # 创建带有地图投影的图形
#         fig = plt.figure(figsize=(12, 10))
#         # 使用PlateCarree投影（经纬度投影）
#         ax = plt.axes(projection=ccrs.PlateCarree())
#         ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], crs=ccrs.PlateCarree())
#
#         # 添加地图要素
#         # 1. 添加海岸线
#         ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8)
#         # 2. 添加国界和省界
#         ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=0.8, linestyle='-')
#         drawShp(ax, province_path, zorder=1, linewidth=0.4,edgecolor=(133/255,6/255,6/255))
#         # 3. 添加陆地和水域颜色
#         ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.3)
#         ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor=(173/255,198/255,231/255), alpha=1)
#
#         wind_dir = df_filtered[201].values  # 风向（角度）
#         wind_speed = df_filtered[203].values  # 风速（m/s）
#
#         # 气象风→u/v分量（核心公式：角度转弧度，负号适配风向定义）
#         theta_rad = np.radians(wind_dir)  # 必转弧度！
#         u = -wind_speed * np.sin(theta_rad)  # 东向分量
#         v = -wind_speed * np.cos(theta_rad)  # 北向分量
#         # 过滤缺省值（避免风羽异常）
#         u = np.nan_to_num(u, nan=0)
#         v = np.nan_to_num(v, nan=0)
#         ax.barbs(
#             df_filtered["Lon"].values, df_filtered["Lat"].values,
#             u, v,
#             transform=ccrs.PlateCarree(),
#             length=4.5,  # 风羽长度（调短，更紧凑）
#             pivot='tip',  # 风羽尖端在站点（气象标准）
#             barbcolor='black',  # 风羽颜色（黑色）
#             flagcolor='black',  # 三角旗颜色
#             linewidth=0.3,  # 线条更细
#             # 气象标准风羽增量：半羽=2.5m/s，全羽=5m/s，旗=25m/s（核心！决定风羽长度）
#             barb_increments={'half': 2.5, 'full': 5, 'flag': 25},
#             zorder=5  # 风羽在最上层
#         )
#
#
#         # 添加经纬度网格
#         gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5, linestyle='-')
#         gl.top_labels = False  # 关闭顶部标签
#         gl.right_labels = False  # 关闭右侧标签
#         gl.xlabel_style = {'size': 10}
#         gl.ylabel_style = {'size': 10}
#
#         # ----------------------
#         # 4. 美化与输出
#         # ----------------------
#         ax.set_title("风标", fontsize=14)
#         ax.legend(loc="upper right")
#         plt.title(f'500hPa 高空填图(风标)\n时间: {time.strftime("%Y年%m月%d日%H时")}',
#                   fontsize=14, fontweight='bold')
#         plt.tight_layout()
#         plt.savefig(
#             save_path,
#             dpi=300,  # 分辨率，300dpi保证高清
#             bbox_inches='tight',  # 去除图片边缘空白
#             facecolor='white'  # 背景色为白色
#         )
#         # plt.savefig("meteorological_map.png", dpi=300, bbox_inches="tight")
#         plt.close()

def draw_wind_barb(timeStr):
    timeObj=datetime.strptime(timeStr, '%Y%m%d%H%M%S')
    fileYearStr = str(timeObj.year)
    fileDayStr = str(timeObj.strftime('%Y%m%d'))
    path_join = os.path.join(root_dir, fileDayStr, file3path)
    fileName=os.path.join(path_join,timeStr+".000")
    data_path=os.path.join(path_join,fileName)
    save_filename = f"{timeObj.strftime('%Y%m%d%H%M%S')}_风场.png"
    # 拼接完整保存路径
    save_path_dir = os.path.join(SAVE_DIR,fileYearStr,fileDayStr)
    save_path = os.path.join(save_path_dir, save_filename)
    # 确保保存目录存在（不存在则创建）
    os.makedirs(save_path_dir, exist_ok=True)
    # data_path=r'E:\BaiduNetdiskDownload\micaps\20250628\UPPER_AIR\PLOT\500\20250628080000.000'
    file_name = os.path.basename(data_path)
    time_str = file_name.split('.')[0]
    time = datetime.strptime(time_str, '%Y%m%d%H%M%S')
    # data=meb.read_sta_alt_from_micaps3(data_path)
    # meb.read_gridwind_from_micaps2(data_path)
    # data=meb.read_stadata_from_micaps1_2_8(data_path)
    # print(data)
    # data = {
    #     "ID": [...],  # 你的 ID 列表
    #     "Lon": [...],  # 你的经度列表
    #     "Lat": [...],  # 你的纬度列表
    #     "Height": [...],  # 3 列（高度）
    #     "DewPoint": [...],  # 803 列（露点）
    #     "Temp_K": [...],  # 421 列（温度，K）
    #     "WindDir": [...],  # 201 列（风向，角度）
    #     "WindSpeed": [...],  # 203 列（风速）
    # }
    station = mdfs.Station(data_path)
    data = station.data
    df = pd.DataFrame(station.data)
    # time = station.utc_time
    # time=time + timedelta(hours=8)
    dsc = station.level_dsc
    level = station.level
    # df.rename(columns={'ID': 'ID', 'Lon': 'Lon', 'Lat': 'Lat', '3': '3', '803': 'DewPointDif',
    # '421': 'Height', '601': 'tem','201':"wind_v",'203':"wind_s"}, inplace=True)
    # df_new = df.rename(columns={'ID': 'ID', 'Lon': 'Lon', 'Lat': 'Lat', '3': '3', '803': 'DewPointDif', '421': 'Height', '601': 'tem','201':"wind_v",'203':"wind_s"}, inplace=True)


    df_filtered = df.dropna(subset=['Lon', 'Lat'])  # 移除经纬度空值行
    df_filtered = df_filtered[
        (df_filtered['Lon'] >= LON_MIN) & (df_filtered['Lon'] <= LON_MAX) &
        (df_filtered['Lat'] >= LAT_MIN) & (df_filtered['Lat'] <= LAT_MAX)
    ]
    # 创建带有地图投影的图形
    fig = plt.figure(figsize=(12, 10))
    # 使用PlateCarree投影（经纬度投影）
    ax = plt.axes(projection=ccrs.PlateCarree())
    ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], crs=ccrs.PlateCarree())

    # 添加地图要素
    # 1. 添加海岸线
    ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8)
    # 2. 添加国界和省界
    ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=0.8, linestyle='-')
    drawShp(ax, province_path, zorder=1, linewidth=0.4,edgecolor=(133/255,6/255,6/255))
    # 3. 添加陆地和水域颜色
    ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.3)
    ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor=(173/255,198/255,231/255), alpha=1)

    wind_dir = df_filtered[201].values  # 风向（角度）
    wind_speed = df_filtered[203].values  # 风速（m/s）

    # 气象风→u/v分量（核心公式：角度转弧度，负号适配风向定义）
    theta_rad = np.radians(wind_dir)  # 必转弧度！
    u = -wind_speed * np.sin(theta_rad)  # 东向分量
    v = -wind_speed * np.cos(theta_rad)  # 北向分量
    # 过滤缺省值（避免风羽异常）
    u = np.nan_to_num(u, nan=0)
    v = np.nan_to_num(v, nan=0)
    ax.barbs(
        df_filtered["Lon"].values, df_filtered["Lat"].values,
        u, v,
        transform=ccrs.PlateCarree(),
        length=4.5,  # 风羽长度（调短，更紧凑）
        pivot='tip',  # 风羽尖端在站点（气象标准）
        barbcolor='black',  # 风羽颜色（黑色）
        flagcolor='black',  # 三角旗颜色
        linewidth=0.3,  # 线条更细
        # 气象标准风羽增量：半羽=2.5m/s，全羽=5m/s，旗=25m/s（核心！决定风羽长度）
        barb_increments={'half': 2.5, 'full': 5, 'flag': 25},
        zorder=5  # 风羽在最上层
    )


    # 添加经纬度网格
    gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5, linestyle='-')
    gl.top_labels = False  # 关闭顶部标签
    gl.right_labels = False  # 关闭右侧标签
    gl.xlabel_style = {'size': 10}
    gl.ylabel_style = {'size': 10}

    # ----------------------
    # 4. 美化与输出
    # ----------------------
    ax.set_title("风标", fontsize=14)
    ax.legend(loc="upper right")
    plt.title(f'500hPa 高空填图(风标)\n时间: {time.strftime("%Y年%m月%d日%H时")}',
              fontsize=14, fontweight='bold')
    plt.tight_layout()
    plt.savefig(
        save_path,
        dpi=300,  # 分辨率，300dpi保证高清
        bbox_inches='tight',  # 去除图片边缘空白
        facecolor='white'  # 背景色为白色
    )
    # plt.savefig("meteorological_map.png", dpi=300, bbox_inches="tight")
    plt.close()
    return save_path
# 使用示例
if __name__ == "__main__":
    draw_wind_barb('20250628080000')