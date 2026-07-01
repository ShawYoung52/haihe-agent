import configparser
import os
from datetime import timedelta, datetime

import numpy as np
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from cartopy.io import shapereader

import Util.MDFS  as mdfs
import pandas as pd
from scipy.interpolate import griddata
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
#         save_filename = f"{time.strftime('%Y%m%d%H%M%S')}_温度.png"
#         # 拼接完整保存路径
#         save_path_dir = os.path.join(SAVE_DIR,fileYearStr,fileDayStr)
#         save_path = os.path.join(save_path_dir, save_filename)
#         # 确保保存目录存在（不存在则创建）
#         os.makedirs(save_path_dir, exist_ok=True)
#         # 读取站点数据
#         station = mdfs.Station(data_path)
#         dsc = station.level_dsc
#         level = station.level
#         data = station.data
#         df = pd.DataFrame(station.data)
#
#
#
#         # 创建带有地图投影的图形
#         fig = plt.figure(figsize=(12, 10))
#         ax = plt.axes(projection=ccrs.PlateCarree())
#         ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], crs=ccrs.PlateCarree())
#
#         # 添加地图要素
#         ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8, zorder=4)
#         ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=0.8, linestyle='-', zorder=4)
#         drawShp(ax, province_path, zorder=4, linewidth=0.4,edgecolor='black')
#         # drawShp(ax, province_path, zorder=4, linewidth=0.4,edgecolor=(133/255,6/255,6/255))
#         ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.3, zorder=0)
#         # ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor=(173/255,198/255,231/255), alpha=1, zorder=1)
#         ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor='white', alpha=1, zorder=1)
#
#         # 添加网格线
#         gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5, linestyle='-')
#         gl.top_labels = False
#         gl.right_labels = False
#         gl.xlabel_style = {'size': 10}
#         gl.ylabel_style = {'size': 10}
#
#         # 1. 提取有效数据（去除缺失值）
#         lon = df['Lon'].values
#         lat = df['Lat'].values
#         temp = df[601].values
#
#         # 过滤掉温度为NaN的无效数据
#         valid_mask = ~np.isnan(temp)
#         lon_valid = lon[valid_mask]
#         lat_valid = lat[valid_mask]
#         temp_valid = temp[valid_mask]
#
#         # 2. 创建规则网格
#         lon_grid = np.linspace(LON_MIN, LON_MAX, 200)
#         lat_grid = np.linspace(LAT_MIN, LAT_MAX, 200)
#         lon_mesh, lat_mesh = np.meshgrid(lon_grid, lat_grid)
#
#         # 3. 插值离散数据到规则网格
#         temp_grid = griddata((lon_valid, lat_valid), temp_valid,
#                              (lon_mesh, lat_mesh), method='cubic')
#
#         # 4. 绘制等值面（核心修改部分）
#         # # 设置等值面级别
#         # temp_levels = np.arange(-40, 5, 2)
#         # 4. 绘制露点温度等值面（核心修改）
#         # 调整等值面级别（适配露点温度常见范围：-30~20℃）
#         # dew_levels = np.arange(-30, 20, 2)
#         # ===================== 自动计算等值面级别 =====================
#         # 1. 计算有效露点温度的极值（排除NaN）
#         temp_min = np.nanmin(temp_valid)  # 实际最小值
#         temp_max = np.nanmax(temp_valid)  # 实际最大值
#
#         # 2. 优化极值：取整到间隔（2）的整数倍，且预留±2的余量（避免边缘紧贴数据）
#         interval = 2  # 等值面间隔，可根据需求调整（如1、2、5）
#         # 向下取整到最近的interval整数倍，再减2（预留余量）
#         temp_level_min = np.floor(temp_min / interval) * interval - 2
#         # 向上取整到最近的interval整数倍，再加2（预留余量）
#         temp_level_max = np.ceil(temp_max / interval) * interval + 2
#
#         # 3. 生成适配数据的等值面级别
#         temp_levels = np.arange(temp_level_min, temp_level_max, interval)
#         # 选择颜色映射（coolwarm适合温度展示，RdBu_r、viridis也是常用选项）
#         cmap = plt.cm.coolwarm
#
#         # 绘制填充等值面（contourf替代原来的contour）
#         contour_filled = ax.contourf(lon_mesh, lat_mesh, temp_grid,
#                                      levels=temp_levels,
#                                      transform=ccrs.PlateCarree(),
#                                      cmap=cmap,  # 颜色映射
#                                      alpha=1,  # 透明度
#                                      zorder=1)
#
#         # # 可选：添加等值线轮廓（让填充区域边界更清晰）
#         # contour_lines = ax.contour(lon_mesh, lat_mesh, temp_grid,
#         #                            levels=temp_levels,
#         #                            transform=ccrs.PlateCarree(),
#         #                            colors='black',  # 轮廓线颜色
#         #                            linewidths=0.3,  # 轮廓线宽度
#         #                            zorder=3)
#
#         # # 可选：添加等值线数值标注
#         # ax.clabel(contour_lines, inline=True, fontsize=7, fmt='%.1f', colors='black')
#
#         # 5. 添加图例（colorbar）
#         # 创建颜色条（图例），调整位置和大小
#         cbar = plt.colorbar(contour_filled, ax=ax, shrink=0.8, pad=0.05, orientation='horizontal')
#         # 设置图例标签
#         cbar.set_label('500hPa 温度 (℃)', fontsize=12, fontweight='bold')
#         # 设置图例刻度字体大小
#         cbar.ax.tick_params(labelsize=10)
#
#         # 设置图形标题
#         plt.title(f'500hPa 高空填图(Tem/℃)\n时间: {time.strftime("%Y年%m月%d日%H时")}',
#                   fontsize=14, fontweight='bold')
#
#         # 调整布局避免元素重叠
#         plt.tight_layout()
#         # 保存图片（可选，建议保存高清图片）
#         # plt.savefig(f'500hPa_temp_contourf_{time_str}.png', dpi=300, bbox_inches='tight')
#         # plt.show()
#         plt.savefig(
#             save_path,
#             dpi=300,  # 分辨率，300dpi保证高清
#             bbox_inches='tight',  # 去除图片边缘空白
#             facecolor='white'  # 背景色为白色
#         )
#         plt.close()

def draw_temperature(timeStr):
    timeObj = datetime.strptime(timeStr, '%Y%m%d%H%M%S')
    fileYearStr = str(timeObj.year)
    fileDayStr = str(timeObj.strftime('%Y%m%d'))
    path_join = os.path.join(root_dir, fileDayStr, file3path)
    fileName = os.path.join(path_join, timeStr + ".000")
    data_path = os.path.join(path_join, fileName)
    # 构造文件名：时间格式化字符串 + "aaa" + 后缀
    save_filename = f"{timeObj.strftime('%Y%m%d%H%M%S')}_温度.png"
    # 拼接完整保存路径
    save_path_dir = os.path.join(SAVE_DIR,fileYearStr,fileDayStr)
    save_path = os.path.join(save_path_dir, save_filename)
    # 确保保存目录存在（不存在则创建）
    os.makedirs(save_path_dir, exist_ok=True)
    # 读取站点数据
    station = mdfs.Station(data_path)
    df = pd.DataFrame(station.data)



    # 创建带有地图投影的图形
    fig = plt.figure(figsize=(12, 10))
    ax = plt.axes(projection=ccrs.PlateCarree())
    ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], crs=ccrs.PlateCarree())

    # 添加地图要素
    ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8, zorder=4)
    ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=0.8, linestyle='-', zorder=4)
    drawShp(ax, province_path, zorder=4, linewidth=0.4,edgecolor='black')
    # drawShp(ax, province_path, zorder=4, linewidth=0.4,edgecolor=(133/255,6/255,6/255))
    ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.3, zorder=0)
    # ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor=(173/255,198/255,231/255), alpha=1, zorder=1)
    ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor='white', alpha=1, zorder=1)

    # 添加网格线
    gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5, linestyle='-')
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {'size': 10}
    gl.ylabel_style = {'size': 10}

    # 1. 提取有效数据（去除缺失值）
    lon = df['Lon'].values
    lat = df['Lat'].values
    temp = df[601].values

    # 过滤掉温度为NaN的无效数据
    valid_mask = ~np.isnan(temp)
    lon_valid = lon[valid_mask]
    lat_valid = lat[valid_mask]
    temp_valid = temp[valid_mask]

    # 2. 创建规则网格
    lon_grid = np.linspace(LON_MIN, LON_MAX, 200)
    lat_grid = np.linspace(LAT_MIN, LAT_MAX, 200)
    lon_mesh, lat_mesh = np.meshgrid(lon_grid, lat_grid)

    # 3. 插值离散数据到规则网格
    temp_grid = griddata((lon_valid, lat_valid), temp_valid,
                         (lon_mesh, lat_mesh), method='cubic')

    # 4. 绘制等值面（核心修改部分）
    # # 设置等值面级别
    # temp_levels = np.arange(-40, 5, 2)
    # 4. 绘制露点温度等值面（核心修改）
    # 调整等值面级别（适配露点温度常见范围：-30~20℃）
    # dew_levels = np.arange(-30, 20, 2)
    # ===================== 自动计算等值面级别 =====================
    # 1. 计算有效露点温度的极值（排除NaN）
    temp_min = np.nanmin(temp_valid)  # 实际最小值
    temp_max = np.nanmax(temp_valid)  # 实际最大值

    # 2. 优化极值：取整到间隔（2）的整数倍，且预留±2的余量（避免边缘紧贴数据）
    interval = 2  # 等值面间隔，可根据需求调整（如1、2、5）
    # 向下取整到最近的interval整数倍，再减2（预留余量）
    temp_level_min = np.floor(temp_min / interval) * interval - 2
    # 向上取整到最近的interval整数倍，再加2（预留余量）
    temp_level_max = np.ceil(temp_max / interval) * interval + 2

    # 3. 生成适配数据的等值面级别
    temp_levels = np.arange(temp_level_min, temp_level_max, interval)
    # 选择颜色映射（coolwarm适合温度展示，RdBu_r、viridis也是常用选项）
    cmap = plt.cm.coolwarm

    # 绘制填充等值面（contourf替代原来的contour）
    contour_filled = ax.contourf(lon_mesh, lat_mesh, temp_grid,
                                 levels=temp_levels,
                                 transform=ccrs.PlateCarree(),
                                 cmap=cmap,  # 颜色映射
                                 alpha=1,  # 透明度
                                 zorder=1)

    # # 可选：添加等值线轮廓（让填充区域边界更清晰）
    # contour_lines = ax.contour(lon_mesh, lat_mesh, temp_grid,
    #                            levels=temp_levels,
    #                            transform=ccrs.PlateCarree(),
    #                            colors='black',  # 轮廓线颜色
    #                            linewidths=0.3,  # 轮廓线宽度
    #                            zorder=3)

    # # 可选：添加等值线数值标注
    # ax.clabel(contour_lines, inline=True, fontsize=7, fmt='%.1f', colors='black')

    # 5. 添加图例（colorbar）
    # 创建颜色条（图例），调整位置和大小
    cbar = plt.colorbar(contour_filled, ax=ax, shrink=0.8, pad=0.05, orientation='horizontal')
    # 设置图例标签
    cbar.set_label('500hPa 温度 (℃)', fontsize=12, fontweight='bold')
    # 设置图例刻度字体大小
    cbar.ax.tick_params(labelsize=10)

    # 设置图形标题
    plt.title(f'500hPa 高空填图(Tem/℃)\n时间: {timeObj.strftime("%Y年%m月%d日%H时")}',
              fontsize=14, fontweight='bold')

    # 调整布局避免元素重叠
    plt.tight_layout()
    # 保存图片（可选，建议保存高清图片）
    # plt.savefig(f'500hPa_temp_contourf_{time_str}.png', dpi=300, bbox_inches='tight')
    # plt.show()
    plt.savefig(
        save_path,
        dpi=300,  # 分辨率，300dpi保证高清
        bbox_inches='tight',  # 去除图片边缘空白
        facecolor='white'  # 背景色为白色
    )
    plt.close()
    return save_path
if __name__ == "__main__":
    draw_temperature('20250628080000')