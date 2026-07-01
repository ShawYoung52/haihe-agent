"""
气象分析器核心模块
封装 500hPa、700hPa、850hPa 三个层面的气象图绘制功能
"""

import configparser
import os
from datetime import datetime
from typing import Dict, Optional, List
import numpy as np

if not hasattr(np, 'float'):
    np.float = np.float64
if not hasattr(np, 'int'):
    np.int = np.int_
if not hasattr(np, 'bool'):
    np.bool = np.bool_

import matplotlib
matplotlib.use('Agg')  # 使用非交互式后端
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
import meteva.base as meb
import pandas as pd
from scipy.interpolate import griddata

# 导入辅助模块
import sys
sys.path.append(os.path.dirname(__file__))
from Util.draw import drawShp
import Util.MDFS as mdfs


class WeatherAnalyzer:
    """气象分析器类"""
    
    def __init__(self, config: configparser.ConfigParser):
        """
        初始化气象分析器
        
        参数:
            config: 配置文件对象
        """
        self.config = config
        self.root_dir = config.get('common', 'rootDir')
        self.province_path = config.get('common', 'provincePath')
        self.save_dir = config.get('common', 'saveDir')
        self.lon_min = float(config.get('common', 'lonMin'))
        self.lon_max = float(config.get('common', 'lonMax'))
        self.lat_min = float(config.get('common', 'latMin'))
        self.lat_max = float(config.get('common', 'latMax'))
        
        # 层面配置（数据路径映射）
        self.level_paths = {
            "500hPa": "UPPER_AIR\\PLOT\\500",
            "700hPa": "UPPER_AIR\\PLOT\\700",
            "850hPa": "UPPER_AIR\\PLOT\\850"
        }
        
        # 尝试读取分离的数据路径配置
        self.height_field_paths = {}
        self.station_data_paths = {}
        if config.has_section('paths'):
            for level in ["500", "700", "850"]:
                # 位势高度场路径
                height_key = f'height_field_{level}'
                if config.has_option('paths', height_key):
                    path = config.get('paths', height_key).strip()
                    if path:
                        self.height_field_paths[f"{level}hPa"] = path
                
                # 站点数据路径
                station_key = f'station_data_{level}'
                if config.has_option('paths', station_key):
                    path = config.get('paths', station_key).strip()
                    if path:
                        self.station_data_paths[f"{level}hPa"] = path
    
    def generate_charts(self, time_str: str, level: str = "500hPa") -> Dict:
        """
        生成指定层面的气象分析图
        
        参数:
            time_str: 时间字符串（YYYYMMDDHHMISS）
            level: 气压层面（500hPa/700hPa/850hPa）
        
        返回:
            dict: 包含生成的图片路径和状态信息
        """
        time_obj = datetime.strptime(time_str, '%Y%m%d%H%M%S')
        charts = {}
        
        # 1. 生成位势高度场图
        height_error = None
        try:
            height_path = self._draw_height_field(time_obj, level)
            charts["height_field"] = height_path
        except Exception as e:
            height_error = str(e)
            # 不要立即返回错误，尝试继续生成其他图表
            pass
        
        # 2. 生成风羽图
        wind_error = None
        try:
            wind_path = self._draw_wind_barb(time_obj, level)
            charts["wind_barb"] = wind_path
        except Exception as e:
            wind_error = str(e)
            pass
        
        # 3. 对于 700hPa 和 850hPa，生成露点温度图
        dew_error = None
        if level in ["700hPa", "850hPa"]:
            try:
                dew_path = self._draw_dew_point(time_obj, level)
                charts["dew_point"] = dew_path
            except Exception as e:
                dew_error = str(e)
                pass
        
        # 判断结果状态
        errors = []
        if height_error:
            errors.append(f"位势高度场: {height_error}")
        if wind_error:
            errors.append(f"风羽图: {wind_error}")
        if dew_error:
            errors.append(f"露点温度: {dew_error}")
        
        if not charts:
            # 所有图表都失败
            return {
                "status": "error",
                "level": level,
                "time": time_obj.strftime("%Y年%m月%d日%H时"),
                "charts": charts,
                "message": f"所有图表生成失败。错误: {'; '.join(errors)}"
            }
        elif errors:
            # 部分成功
            return {
                "status": "partial",
                "level": level,
                "time": time_obj.strftime("%Y年%m月%d日%H时"),
                "charts": charts,
                "message": f"部分图表生成成功（{len(charts)}/{len(errors)+len(charts)}）。失败: {'; '.join(errors)}"
            }
        else:
            # 全部成功
            return {
                "status": "success",
                "level": level,
                "time": time_obj.strftime("%Y年%m月%d日%H时"),
                "charts": charts,
                "message": f"{level} 所有图表生成成功"
            }
    
    def _get_data_path(self, time_obj: datetime, level: str, data_type: str = "default") -> str:
        """
        获取数据文件路径
        
        参数:
            time_obj: 时间对象
            level: 气压层面（如 "500hPa"）
            data_type: 数据类型 ("height_field", "station", "default")
        """
        file_day_str = time_obj.strftime('%Y%m%d')
        time_str = time_obj.strftime('%Y%m%d%H%M%S')
        
        # 根据数据类型选择路径
        if data_type == "height_field" and level in self.height_field_paths:
            level_path = self.height_field_paths[level]
        elif data_type == "station" and level in self.station_data_paths:
            level_path = self.station_data_paths[level]
        else:
            level_path = self.level_paths.get(level, self.level_paths["500hPa"])
        
        path_join = os.path.join(self.root_dir, file_day_str, level_path)
        return os.path.join(path_join, f"{time_str}.000")
    
    def _get_save_path(self, time_obj: datetime, chart_type: str, level: str) -> str:
        """获取图片保存路径"""
        file_year_str = str(time_obj.year)
        file_day_str = time_obj.strftime('%Y%m%d')
        time_str = time_obj.strftime('%Y%m%d%H%M%S')
        
        # 文件名映射
        filename_map = {
            "height_field": f"{time_str}_{level}_位势高度.png",
            "wind_barb": f"{time_str}_{level}_风羽.png",
            "dew_point": f"{time_str}_{level}_露点.png"
        }
        
        save_filename = filename_map.get(chart_type, f"{time_str}_{level}_{chart_type}.png")
        save_path_dir = os.path.join(self.save_dir, file_year_str, file_day_str)
        os.makedirs(save_path_dir, exist_ok=True)
        
        return os.path.join(save_path_dir, save_filename)
    
    def _draw_height_field(self, time_obj: datetime, level: str) -> str:
        """绘制位势高度场（等高线、槽线、高低压中心）"""
        # 优先使用位势高度场专用路径
        data_path = self._get_data_path(time_obj, level, "height_field")
        save_path = self._get_save_path(time_obj, "height_field", level)
        
        # 检查文件是否存在
        if not os.path.exists(data_path):
            raise FileNotFoundError(f"数据文件不存在: {data_path}")
        
        # 读取 MICAPS 14 类数据
        data = meb.read_micaps14(data_path)
        
        # 检查数据是否成功读取
        if data is None:
            raise ValueError(f"MICAPS 14 文件读取失败，文件可能格式错误: {data_path}")
        
        # 检查必需的数据字段
        if 'lines' not in data or data['lines'] is None:
            raise ValueError(f"数据文件缺少等高线数据 (lines): {data_path}")
        if 'line_xyz' not in data['lines'] or 'line_label' not in data['lines']:
            raise ValueError(f"数据文件格式不正确，缺少 line_xyz 或 line_label: {data_path}")
        
        # 提取数据
        lines = data['lines']['line_xyz']
        line_labels = data['lines']['line_label']
        troughs = data['lines_symbol']['linesym_xyz'] if 'lines_symbol' in data and data['lines_symbol'] is not None else []
        
        # 创建图形
        fig = plt.figure(figsize=(12, 10))
        ax = plt.axes(projection=ccrs.PlateCarree())
        ax.set_extent([self.lon_min, self.lon_max, self.lat_min, self.lat_max], 
                      crs=ccrs.PlateCarree())
        
        # 添加地图要素
        ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8)
        ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=1)
        drawShp(ax, self.province_path, zorder=1, linewidth=0.4, 
                edgecolor=(133/255, 6/255, 6/255))
        ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.3)
        ax.add_feature(cfeature.OCEAN.with_scale('50m'), 
                       facecolor=(173/255, 198/255, 231/255), alpha=1)
        
        # 绘制等高线（带标签和挖空）
        contour_color = (100/255, 100/255, 255/255)
        self._draw_contours_with_labels(ax, lines, line_labels, contour_color)
        
        # 绘制槽线
        self._draw_troughs(ax, troughs)
        
        # 添加高低压中心
        self._draw_pressure_centers(ax, data)
        
        # 添加网格
        gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5)
        gl.top_labels, gl.right_labels = False, False
        gl.xlabel_style, gl.ylabel_style = {'size': 10}, {'size': 10}
        
        # 标题
        plt.title(f'{level} 位势高度场\n时间: {time_obj.strftime("%Y年%m月%d日%H时")}',
                  fontsize=14, fontweight='bold')
        
        plt.tight_layout()
        plt.savefig(save_path, dpi=300, bbox_inches='tight', facecolor='white')
        plt.close()
        
        return save_path
    
    def _draw_wind_barb(self, time_obj: datetime, level: str) -> str:
        """绘制风羽图"""
        # 使用站点数据路径
        data_path = self._get_data_path(time_obj, level, "station")
        save_path = self._get_save_path(time_obj, "wind_barb", level)
        
        # 检查文件是否存在
        if not os.path.exists(data_path):
            raise FileNotFoundError(f"数据文件不存在: {data_path}")
        
        # 读取站点数据
        try:
            station = mdfs.Station(data_path)
            if station.data is None or len(station.data) == 0:
                raise ValueError(f"站点数据为空: {data_path}")
            df = pd.DataFrame(station.data)
        except Exception as e:
            raise ValueError(f"站点数据读取失败: {data_path}, 错误: {str(e)}")
        
        # 过滤区域内的数据
        df_filtered = df.dropna(subset=['Lon', 'Lat'])
        df_filtered = df_filtered[
            (df_filtered['Lon'] >= self.lon_min) & (df_filtered['Lon'] <= self.lon_max) &
            (df_filtered['Lat'] >= self.lat_min) & (df_filtered['Lat'] <= self.lat_max)
        ]
        
        # 创建图形
        fig = plt.figure(figsize=(12, 10))
        ax = plt.axes(projection=ccrs.PlateCarree())
        ax.set_extent([self.lon_min, self.lon_max, self.lat_min, self.lat_max], 
                      crs=ccrs.PlateCarree())
        
        # 添加地图要素
        ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8)
        ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=0.8)
        drawShp(ax, self.province_path, zorder=1, linewidth=0.4, 
                edgecolor=(133/255, 6/255, 6/255))
        ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.3)
        ax.add_feature(cfeature.OCEAN.with_scale('50m'), 
                       facecolor=(173/255, 198/255, 231/255), alpha=1)
        
        # 计算风场 u/v 分量
        wind_dir = df_filtered[201].values
        wind_speed = df_filtered[203].values
        theta_rad = np.radians(wind_dir)
        u = -wind_speed * np.sin(theta_rad)
        v = -wind_speed * np.cos(theta_rad)
        u = np.nan_to_num(u, nan=0)
        v = np.nan_to_num(v, nan=0)
        
        # 绘制风羽
        ax.barbs(
            df_filtered["Lon"].values, df_filtered["Lat"].values,
            u, v,
            transform=ccrs.PlateCarree(),
            length=4.5,
            pivot='tip',
            barbcolor='black',
            flagcolor='black',
            linewidth=0.3,
            barb_increments={'half': 2.5, 'full': 5, 'flag': 25},
            zorder=5
        )
        
        # 添加网格
        gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5)
        gl.top_labels, gl.right_labels = False, False
        gl.xlabel_style, gl.ylabel_style = {'size': 10}, {'size': 10}
        
        # 标题
        plt.title(f'{level} 风羽图\n时间: {time_obj.strftime("%Y年%m月%d日%H时")}',
                  fontsize=14, fontweight='bold')
        
        plt.tight_layout()
        plt.savefig(save_path, dpi=300, bbox_inches='tight', facecolor='white')
        plt.close()
        
        return save_path
    
    def _draw_dew_point(self, time_obj: datetime, level: str) -> str:
        """绘制露点温度图"""
        # 使用站点数据路径
        data_path = self._get_data_path(time_obj, level, "station")
        save_path = self._get_save_path(time_obj, "dew_point", level)
        
        # 检查文件是否存在
        if not os.path.exists(data_path):
            raise FileNotFoundError(f"数据文件不存在: {data_path}")
        
        # 读取站点数据
        try:
            station = mdfs.Station(data_path)
            if station.data is None or len(station.data) == 0:
                raise ValueError(f"站点数据为空: {data_path}")
            df = pd.DataFrame(station.data)
        except Exception as e:
            raise ValueError(f"站点数据读取失败: {data_path}, 错误: {str(e)}")
        
        # 创建图形
        fig = plt.figure(figsize=(12, 10))
        ax = plt.axes(projection=ccrs.PlateCarree())
        ax.set_extent([self.lon_min, self.lon_max, self.lat_min, self.lat_max], 
                      crs=ccrs.PlateCarree())
        
        # 添加地图要素
        ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8, zorder=4)
        ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=0.8, zorder=4)
        drawShp(ax, self.province_path, zorder=3, linewidth=0.4, edgecolor='black')
        ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.2, zorder=0)
        ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor='white', alpha=0.5, zorder=0)
        
        # 提取并计算露点温度
        lon = df['Lon'].values
        lat = df['Lat'].values
        temp = df[601].values
        td_diff = df[803].values
        
        valid_mask = ~np.isnan(temp) & ~np.isnan(td_diff)
        if not np.any(valid_mask):
            plt.close(fig)
            raise ValueError(f"无有效露点温度数据")
        
        lon_valid = lon[valid_mask]
        lat_valid = lat[valid_mask]
        dew_point_valid = temp[valid_mask] - td_diff[valid_mask]
        
        # 插值到规则网格
        lon_grid = np.linspace(self.lon_min, self.lon_max, 200)
        lat_grid = np.linspace(self.lat_min, self.lat_max, 200)
        lon_mesh, lat_mesh = np.meshgrid(lon_grid, lat_grid)
        
        dew_point_grid = griddata((lon_valid, lat_valid), dew_point_valid,
                                   (lon_mesh, lat_mesh), method='cubic')
        dew_mean = np.nanmean(dew_point_valid)
        dew_point_grid = np.where(np.isnan(dew_point_grid), dew_mean, dew_point_grid)
        
        # 计算等值面级别
        dew_min = np.nanmin(dew_point_valid)
        dew_max = np.nanmax(dew_point_valid)
        interval = 2
        dew_level_min = np.floor(dew_min / interval) * interval - 2
        dew_level_max = np.ceil(dew_max / interval) * interval + 2
        dew_levels = np.arange(dew_level_min, dew_level_max, interval)
        
        # 绘制填充等值面
        contour_filled = ax.contourf(lon_mesh, lat_mesh, dew_point_grid,
                                      levels=dew_levels,
                                      transform=ccrs.PlateCarree(),
                                      cmap=plt.cm.coolwarm,
                                      alpha=1,
                                      zorder=2)
        
        # 添加颜色条
        cbar = plt.colorbar(contour_filled, ax=ax, shrink=0.8, pad=0.05, 
                            orientation='horizontal')
        cbar.set_label(f'{level} 露点温度 (℃)', fontsize=12, fontweight='bold')
        cbar.ax.tick_params(labelsize=10)
        
        # 添加网格
        gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', 
                          alpha=0.5, zorder=1)
        gl.top_labels, gl.right_labels = False, False
        gl.xlabel_style, gl.ylabel_style = {'size': 10}, {'size': 10}
        
        # 标题
        plt.title(f'{level} 露点温度\n时间: {time_obj.strftime("%Y年%m月%d日%H时")}',
                  fontsize=14, fontweight='bold', pad=20)
        
        plt.tight_layout()
        plt.savefig(save_path, dpi=300, bbox_inches='tight', facecolor='white')
        plt.close()
        
        return save_path
    
    def _draw_contours_with_labels(self, ax, lines, line_labels, color):
        """绘制等高线（带动态标签和挖空）"""
        FONT_SIZE = 12
        MIN_LABEL_DIST = 50
        MIN_LINE_LENGTH = 100
        MAX_LABELS_PER_LINE = 3
        
        for line_coords, label in zip(lines, line_labels):
            # 过滤坐标
            loose_mask = (line_coords[:, 0] >= self.lon_min - 3) & \
                         (line_coords[:, 0] <= self.lon_max + 3) & \
                         (line_coords[:, 1] >= self.lat_min - 3) & \
                         (line_coords[:, 1] <= self.lat_max + 3)
            filtered_coords = line_coords[loose_mask]
            if len(filtered_coords) < 10:
                continue
            
            # 计算线总长度
            total_length = sum(
                self._haversine_distance(
                    filtered_coords[j - 1, 0], filtered_coords[j - 1, 1],
                    filtered_coords[j, 0], filtered_coords[j, 1]
                ) for j in range(1, len(filtered_coords))
            )
            
            if total_length < MIN_LINE_LENGTH:
                ax.plot(filtered_coords[:, 0], filtered_coords[:, 1], 
                        color=color, linewidth=2, transform=ccrs.PlateCarree())
                continue
            
            # 计算标签位置
            n_labels = min(MAX_LABELS_PER_LINE, max(1, int(total_length / 200)))
            label_indices = np.linspace(0, len(filtered_coords) - 1, n_labels, dtype=int)
            
            # 过滤过近的标签
            valid_indices, prev_lon, prev_lat = [], None, None
            for idx in label_indices:
                lon, lat = filtered_coords[idx, 0], filtered_coords[idx, 1]
                if prev_lon is None or self._haversine_distance(prev_lon, prev_lat, lon, lat) > MIN_LABEL_DIST:
                    valid_indices.append(idx)
                    prev_lon, prev_lat = lon, lat
            
            if not valid_indices:
                ax.plot(filtered_coords[:, 0], filtered_coords[:, 1], 
                        color=color, linewidth=2, transform=ccrs.PlateCarree())
                continue
            
            # 计算挖空区间和标签角度
            gap_intervals, label_info = [], []
            for idx in valid_indices:
                angle_s, angle_e = max(0, idx - 10), min(len(filtered_coords) - 1, idx + 10)
                dx = filtered_coords[angle_e, 0] - filtered_coords[angle_s, 0]
                dy = filtered_coords[angle_e, 1] - filtered_coords[angle_s, 1]
                angle = np.degrees(np.arctan2(dy, dx)) if (dx, dy) != (0, 0) else 0
                if angle > 90 or angle < -90:
                    angle += 180
                
                gap_size = int(FONT_SIZE / 3) + 2
                if abs(angle) > 60:
                    gap_size += 1
                gap_s, gap_e = max(0, idx - gap_size), min(len(filtered_coords) - 1, idx + gap_size + 1)
                gap_intervals.append((gap_s, gap_e))
                label_info.append((filtered_coords[idx, 0], filtered_coords[idx, 1], angle))
            
            # 合并重叠挖空
            gap_intervals.sort()
            merged_gaps = []
            for s, e in gap_intervals:
                if not merged_gaps:
                    merged_gaps.append([s, e])
                else:
                    last_s, last_e = merged_gaps[-1]
                    if s <= last_e:
                        merged_gaps[-1][1] = max(last_e, e)
                    else:
                        merged_gaps.append([s, e])
            
            # 分段绘制等高线
            prev_end = 0
            for s, e in merged_gaps:
                if prev_end < s:
                    ax.plot(filtered_coords[prev_end:s, 0], filtered_coords[prev_end:s, 1],
                            color=color, linewidth=2, transform=ccrs.PlateCarree())
                prev_end = e
            if prev_end < len(filtered_coords):
                ax.plot(filtered_coords[prev_end:, 0], filtered_coords[prev_end:, 1],
                        color=color, linewidth=2, transform=ccrs.PlateCarree())
            
            # 添加标签
            for lon, lat, angle in label_info:
                if self.lon_min <= lon <= self.lon_max and self.lat_min <= lat <= self.lat_max:
                    ax.text(lon, lat, str(label), fontsize=FONT_SIZE, fontweight='bold',
                            color=color, ha='center', va='center', rotation=angle,
                            transform=ccrs.PlateCarree())
    
    def _draw_troughs(self, ax, troughs):
        """绘制槽线"""
        trough_added = False
        for trough_coords in troughs:
            mask = (trough_coords[:, 0] >= self.lon_min) & \
                   (trough_coords[:, 0] <= self.lon_max) & \
                   (trough_coords[:, 1] >= self.lat_min) & \
                   (trough_coords[:, 1] <= self.lat_max)
            filtered_trough = trough_coords[mask]
            if len(filtered_trough) > 1:
                color = (169/255, 50/255, 50/255)
                if not trough_added:
                    ax.plot(filtered_trough[:, 0], filtered_trough[:, 1], 
                            color=color, linewidth=2.5, 
                            transform=ccrs.PlateCarree(), label='槽线')
                    trough_added = True
                else:
                    ax.plot(filtered_trough[:, 0], filtered_trough[:, 1], 
                            color=color, linewidth=2.5, 
                            transform=ccrs.PlateCarree())
    
    def _draw_pressure_centers(self, ax, data):
        """绘制高低压中心"""
        if 'symbols' in data and data['symbols'] is not None:
            symbols = data['symbols']['symbol_xyz']
            codes = data['symbols']['symbol_code']
            for sym, code in zip(symbols, codes):
                x, y = sym[0], sym[1]
                if self.lon_min <= x <= self.lon_max and self.lat_min <= y <= self.lat_max:
                    ax.text(x, y, 'G' if code == 60 else 'D', 
                            fontsize=15, fontweight='bold',
                            color='blue' if code == 60 else 'red', 
                            ha='center', va='center',
                            transform=ccrs.PlateCarree())
    
    @staticmethod
    def _haversine_distance(lon1, lat1, lon2, lat2):
        """计算两点间的 Haversine 距离（公里）"""
        R = 6371.0
        lon1_rad, lat1_rad = np.radians(lon1), np.radians(lat1)
        lon2_rad, lat2_rad = np.radians(lon2), np.radians(lat2)
        dlon, dlat = lon2_rad - lon1_rad, lat2_rad - lat1_rad
        a = np.sin(dlat / 2) ** 2 + np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon / 2) ** 2
        c = 2 * np.arcsin(np.sqrt(a))
        return R * c
    
    def get_latest_time(self) -> Optional[str]:
        """获取最新可用的气象数据时间"""
        try:
            # 扫描根目录，找到最新的日期目录
            if not os.path.exists(self.root_dir):
                return None
            
            date_dirs = [d for d in os.listdir(self.root_dir) 
                         if os.path.isdir(os.path.join(self.root_dir, d)) 
                         and d.isdigit() and len(d) == 8]
            
            if not date_dirs:
                return None
            
            # 排序获取最新日期
            date_dirs.sort(reverse=True)
            latest_date = date_dirs[0]
            
            # 在最新日期目录下找到最新的数据文件
            level_path = self.level_paths["500hPa"]
            data_dir = os.path.join(self.root_dir, latest_date, level_path)
            
            if not os.path.exists(data_dir):
                return None
            
            files = [f for f in os.listdir(data_dir) 
                     if f.endswith('.000') and len(f.split('.')[0]) == 14]
            
            if not files:
                return None
            
            # 排序获取最新时间
            files.sort(reverse=True)
            latest_time = files[0].split('.')[0]
            
            return latest_time
        except Exception:
            return None
