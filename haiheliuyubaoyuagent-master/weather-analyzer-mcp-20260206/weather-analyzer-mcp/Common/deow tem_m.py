import configparser
import os
from datetime import timedelta, datetime
import numpy as np
import matplotlib.pyplot as plt
import cartopy.crs as ccrs

import cartopy.feature as cfeature
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
# 定义图片保存的指定位置（请修改为你需要的路径）

# def draw_dew_point_temperature(timeStr):
#     listdir = os.listdir(root_dir)
#     for file2path in listdir:
#         path_join = os.path.join(root_dir, file2path, file3path)
#         fileList = os.listdir(path_join)
#         for fileName in fileList:
#             data_path=os.path.join(path_join,fileName)
#             # 读取数据
#             # data_path = r'E:\BaiduNetdiskDownload\micaps\20250628\UPPER_AIR\MANUAL_ANALYSIS\HGT\500\20250628080000.000'
#             # file_name = os.path.basename(data_path)
#             time_str = fileName.split('.')[0]
#             time = datetime.strptime(time_str, '%Y%m%d%H%M%S')
#             fileYearStr = str(time.year)
#             fileDayStr = str(time.strftime('%Y%m%d'))
#             # 构造文件名：时间格式化字符串 + "aaa" + 后缀
#             save_filename = f"{time.strftime('%Y%m%d%H%M%S')}_露点温度.png"
#             # 拼接完整保存路径
#             save_path_dir = os.path.join(SAVE_DIR,fileYearStr,fileDayStr)
#             save_path = os.path.join(save_path_dir, save_filename)
#             # 确保保存目录存在（不存在则创建）
#             os.makedirs(save_path_dir, exist_ok=True)
#             file_name = os.path.basename(data_path)
#             time_str = file_name.split('.')[0]
#             time = datetime.strptime(time_str, '%Y%m%d%H%M%S')
#             station = mdfs.Station(data_path)
#             dsc = station.level_dsc
#             level = station.level
#
#             data = station.data
#             df = pd.DataFrame(station.data)
#
#             # 创建带有地图投影的图形
#             fig = plt.figure(figsize=(12, 10))
#             # 使用PlateCarree投影（经纬度投影）
#             ax = plt.axes(projection=ccrs.PlateCarree())
#             ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], crs=ccrs.PlateCarree())
#             # 添加地图要素（调整zorder，让等值面在中间层）
#             # 1. 添加海岸线
#             ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8, zorder=4)
#             # 2. 添加国界和省界
#             ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=0.8, linestyle='-', zorder=4)
#             # drawShp(ax, province_path, zorder=3, linewidth=0.4, edgecolor=(133/255,6/255,6/255))
#             drawShp(ax, province_path, zorder=3, linewidth=0.4, edgecolor='black')
#             # 3. 添加陆地和水域颜色（放在最底层）
#             ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.2, zorder=0)
#             ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor='white', alpha=0.5, zorder=0)
#             # ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor=(173/255,198/255,231/255), alpha=0.5, zorder=0)
#
#             # 添加经纬度网格
#             gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5, linestyle='-', zorder=1)
#             gl.top_labels = False  # 关闭顶部的经纬度标签
#             gl.right_labels = False  # 关闭右侧的经纬度标签
#             gl.xlabel_style = {'size': 10}  # 设置经度标签字体大小
#             gl.ylabel_style = {'size': 10}  # 设置纬度标签字体大小
#
#             # ===================== 核心修改：露点温度等值面 =====================
#             # 1. 提取有效数据（计算露点温度：601列 - 803列）
#             lon = df['Lon'].values
#             lat = df['Lat'].values
#             temp = df[601].values
#             td_diff = df[803].values
#             # 仅保留两列都非NaN的有效数据
#             valid_mask = ~np.isnan(temp) & ~np.isnan(td_diff)
#             if not np.any(valid_mask):  # 无有效数据时跳过当前文件
#                 print(f"警告：{data_path} 无有效露点温度数据，跳过")
#                 plt.close(fig)  # 关闭未使用的图形，避免内存泄漏
#                 continue
#             lon_valid = lon[valid_mask]
#             lat_valid = lat[valid_mask]
#             dew_point_valid = temp[valid_mask] - td_diff[valid_mask]  # 计算有效露点温度
#
#             # 2. 创建规则网格（等值线需要网格化数据）
#             # 生成经纬度网格点（200x200的网格，密度可根据需求调整）
#             lon_grid = np.linspace(LON_MIN, LON_MAX, 200)
#             lat_grid = np.linspace(LAT_MIN, LAT_MAX, 200)
#             lon_mesh, lat_mesh = np.meshgrid(lon_grid, lat_grid)
#
#             # 3. 插值离散数据到规则网格
#             # method可选：'cubic'(立方插值，更平滑)、'linear'(线性)、'nearest'(最近邻)
#             dew_point_grid = griddata((lon_valid, lat_valid), dew_point_valid,
#                                  (lon_mesh, lat_mesh), method='cubic')
#             # 兜底填充：即使有NaN（极端情况），用均值填充（彻底杜绝空白）
#             dew_mean = np.nanmean(dew_point_valid)
#             dew_point_grid = np.where(np.isnan(dew_point_grid), dew_mean, dew_point_grid)
#
#             # 4. 绘制露点温度等值面（核心修改）
#             # 调整等值面级别（适配露点温度常见范围：-30~20℃）
#             # dew_levels = np.arange(-30, 20, 2)
#             # ===================== 自动计算等值面级别 =====================
#             # 1. 计算有效露点温度的极值（排除NaN）
#             dew_min = np.nanmin(dew_point_valid)  # 实际最小值
#             dew_max = np.nanmax(dew_point_valid)  # 实际最大值
#
#             # 2. 优化极值：取整到间隔（2）的整数倍，且预留±2的余量（避免边缘紧贴数据）
#             interval = 2  # 等值面间隔，可根据需求调整（如1、2、5）
#             # 向下取整到最近的interval整数倍，再减2（预留余量）
#             dew_level_min = np.floor(dew_min / interval) * interval - 2
#             # 向上取整到最近的interval整数倍，再加2（预留余量）
#             dew_level_max = np.ceil(dew_max / interval) * interval + 2
#
#             # 3. 生成适配数据的等值面级别
#             dew_levels = np.arange(dew_level_min, dew_level_max , interval)
#             # 选择颜色映射（coolwarm适合温度展示，也可以用RdBu_r、viridis等）
#             cmap = plt.cm.coolwarm
#
#             # 绘制填充等值面
#             contour_filled = ax.contourf(lon_mesh, lat_mesh, dew_point_grid,
#                                          levels=dew_levels,
#                                          transform=ccrs.PlateCarree(),
#                                          cmap=cmap,
#                                          alpha=1,  # 透明度
#                                          zorder=2)  # 层级在地图要素之下，省界之上
#
#             # # 可选：添加等值线轮廓（让填充区域边界更清晰）
#             # contour_lines = ax.contour(lon_mesh, lat_mesh, dew_point_grid,
#             #                            levels=dew_levels[::2],  # 每隔一个级别画一条线
#             #                            transform=ccrs.PlateCarree(),
#             #                            colors='black',
#             #                            linewidths=0.3,
#             #                            zorder=3)  # 层级高于填充面
#
#             # # 可选：添加等值线标注
#             # ax.clabel(contour_lines, inline=True, fontsize=7, fmt='%.0f', colors='black')
#
#             # 5. 添加颜色条（图例）
#             cbar = plt.colorbar(contour_filled, ax=ax, shrink=0.8, pad=0.05, orientation='horizontal')
#             cbar.set_label('500hPa 露点温度 (℃)', fontsize=12, fontweight='bold')
#             cbar.ax.tick_params(labelsize=10)
#             # ===================== 露点温度等值面修改结束 =====================
#
#             # 设置图形标题
#             plt.title(f'500hPa 高空填图(露点温度/℃)\n时间:{time.strftime("%Y年%m月%d日%H时")}',
#                       fontsize=14, fontweight='bold', pad=20)  # pad避免标题遮挡网格
#
#             # 显示图形
#             plt.tight_layout()
#             plt.savefig(
#                 save_path,
#                 dpi=300,  # 分辨率，300dpi保证高清
#                 bbox_inches='tight',  # 去除图片边缘空白
#                 facecolor='white'  # 背景色为白色
#             )
#             # plt.show()
#             # print()
#             plt.close()
#             return
def draw_dew_point_temperature(timeStr):
    timeObj=datetime.strptime(timeStr, '%Y%m%d%H%M%S')
    fileYearStr = str(timeObj.year)
    fileDayStr = str(timeObj.strftime('%Y%m%d'))
    path_join = os.path.join(root_dir, fileDayStr, file3path)
    fileName=os.path.join(path_join,timeStr+".000")
    data_path=os.path.join(path_join,fileName)
    # 读取数据
    # data_path = r'E:\BaiduNetdiskDownload\micaps\20250628\UPPER_AIR\MANUAL_ANALYSIS\HGT\500\20250628080000.000'
    # file_name = os.path.basename(data_path)

    # 构造文件名：时间格式化字符串 + "aaa" + 后缀
    save_filename = f"{timeObj.strftime('%Y%m%d%H%M%S')}_露点温度.png"
    # 拼接完整保存路径
    save_path_dir = os.path.join(SAVE_DIR,fileYearStr,fileDayStr)
    save_path = os.path.join(save_path_dir, save_filename)
    # 确保保存目录存在（不存在则创建）
    os.makedirs(save_path_dir, exist_ok=True)
    file_name = os.path.basename(data_path)
    time_str = file_name.split('.')[0]
    time = datetime.strptime(time_str, '%Y%m%d%H%M%S')
    station = mdfs.Station(data_path)
    df = pd.DataFrame(station.data)

    # 创建带有地图投影的图形
    fig = plt.figure(figsize=(12, 10))
    # 使用PlateCarree投影（经纬度投影）
    ax = plt.axes(projection=ccrs.PlateCarree())
    ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], crs=ccrs.PlateCarree())
    # 添加地图要素（调整zorder，让等值面在中间层）
    # 1. 添加海岸线
    ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8, zorder=4)
    # 2. 添加国界和省界
    ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=0.8, linestyle='-', zorder=4)
    # drawShp(ax, province_path, zorder=3, linewidth=0.4, edgecolor=(133/255,6/255,6/255))
    drawShp(ax, province_path, zorder=3, linewidth=0.4, edgecolor='black')
    # 3. 添加陆地和水域颜色（放在最底层）
    ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.2, zorder=0)
    ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor='white', alpha=0.5, zorder=0)
    # ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor=(173/255,198/255,231/255), alpha=0.5, zorder=0)

    # 添加经纬度网格
    gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5, linestyle='-', zorder=1)
    gl.top_labels = False  # 关闭顶部的经纬度标签
    gl.right_labels = False  # 关闭右侧的经纬度标签
    gl.xlabel_style = {'size': 10}  # 设置经度标签字体大小
    gl.ylabel_style = {'size': 10}  # 设置纬度标签字体大小

    # ===================== 核心修改：露点温度等值面 =====================
    # 1. 提取有效数据（计算露点温度：601列 - 803列）
    lon = df['Lon'].values
    lat = df['Lat'].values
    temp = df[601].values
    td_diff = df[803].values
    # 仅保留两列都非NaN的有效数据
    valid_mask = ~np.isnan(temp) & ~np.isnan(td_diff)
    if not np.any(valid_mask):  # 无有效数据时跳过当前文件
        plt.close(fig)  # 关闭未使用的图形，避免内存泄漏
        print(f"警告：{data_path} 无有效露点温度数据，跳过")
        return f"警告：{data_path} 无有效露点温度数据"

    lon_valid = lon[valid_mask]
    lat_valid = lat[valid_mask]
    dew_point_valid = temp[valid_mask] - td_diff[valid_mask]  # 计算有效露点温度

    # 2. 创建规则网格（等值线需要网格化数据）
    # 生成经纬度网格点（200x200的网格，密度可根据需求调整）
    lon_grid = np.linspace(LON_MIN, LON_MAX, 200)
    lat_grid = np.linspace(LAT_MIN, LAT_MAX, 200)
    lon_mesh, lat_mesh = np.meshgrid(lon_grid, lat_grid)

    # 3. 插值离散数据到规则网格
    # method可选：'cubic'(立方插值，更平滑)、'linear'(线性)、'nearest'(最近邻)
    dew_point_grid = griddata((lon_valid, lat_valid), dew_point_valid,
                         (lon_mesh, lat_mesh), method='cubic')
    # 兜底填充：即使有NaN（极端情况），用均值填充（彻底杜绝空白）
    dew_mean = np.nanmean(dew_point_valid)
    dew_point_grid = np.where(np.isnan(dew_point_grid), dew_mean, dew_point_grid)

    # 4. 绘制露点温度等值面（核心修改）
    # 调整等值面级别（适配露点温度常见范围：-30~20℃）
    # dew_levels = np.arange(-30, 20, 2)
    # ===================== 自动计算等值面级别 =====================
    # 1. 计算有效露点温度的极值（排除NaN）
    dew_min = np.nanmin(dew_point_valid)  # 实际最小值
    dew_max = np.nanmax(dew_point_valid)  # 实际最大值

    # 2. 优化极值：取整到间隔（2）的整数倍，且预留±2的余量（避免边缘紧贴数据）
    interval = 2  # 等值面间隔，可根据需求调整（如1、2、5）
    # 向下取整到最近的interval整数倍，再减2（预留余量）
    dew_level_min = np.floor(dew_min / interval) * interval - 2
    # 向上取整到最近的interval整数倍，再加2（预留余量）
    dew_level_max = np.ceil(dew_max / interval) * interval + 2

    # 3. 生成适配数据的等值面级别
    dew_levels = np.arange(dew_level_min, dew_level_max , interval)
    # 选择颜色映射（coolwarm适合温度展示，也可以用RdBu_r、viridis等）
    cmap = plt.cm.coolwarm

    # 绘制填充等值面
    contour_filled = ax.contourf(lon_mesh, lat_mesh, dew_point_grid,
                                 levels=dew_levels,
                                 transform=ccrs.PlateCarree(),
                                 cmap=cmap,
                                 alpha=1,  # 透明度
                                 zorder=2)  # 层级在地图要素之下，省界之上

    # # 可选：添加等值线轮廓（让填充区域边界更清晰）
    # contour_lines = ax.contour(lon_mesh, lat_mesh, dew_point_grid,
    #                            levels=dew_levels[::2],  # 每隔一个级别画一条线
    #                            transform=ccrs.PlateCarree(),
    #                            colors='black',
    #                            linewidths=0.3,
    #                            zorder=3)  # 层级高于填充面

    # # 可选：添加等值线标注
    # ax.clabel(contour_lines, inline=True, fontsize=7, fmt='%.0f', colors='black')

    # 5. 添加颜色条（图例）
    cbar = plt.colorbar(contour_filled, ax=ax, shrink=0.8, pad=0.05, orientation='horizontal')
    cbar.set_label('500hPa 露点温度 (℃)', fontsize=12, fontweight='bold')
    cbar.ax.tick_params(labelsize=10)
    # ===================== 露点温度等值面修改结束 =====================

    # 设置图形标题
    plt.title(f'500hPa 高空填图(露点温度/℃)\n时间:{time.strftime("%Y年%m月%d日%H时")}',
              fontsize=14, fontweight='bold', pad=20)  # pad避免标题遮挡网格

    # 显示图形
    plt.tight_layout()
    plt.savefig(
        save_path,
        dpi=300,  # 分辨率，300dpi保证高清
        bbox_inches='tight',  # 去除图片边缘空白
        facecolor='white'  # 背景色为白色
    )
    # plt.show()
    # print()
    plt.close()
    return save_path
# 使用示例
if __name__ == "__main__":
    draw_dew_point_temperature('20250628080000')