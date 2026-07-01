import configparser
import os
from datetime import datetime

import meteva.base as meb
import numpy as np
import matplotlib.pyplot as plt
import cartopy.crs as ccrs
import cartopy.feature as cfeature
from Util.draw import drawShp
# 定义经纬度距离计算函数（Haversine公式，单位：公里）
def haversine_distance(lon1, lat1, lon2, lat2):
    R = 6371.0  # 地球平均半径
    lon1_rad, lat1_rad = np.radians(lon1), np.radians(lat1)
    lon2_rad, lat2_rad = np.radians(lon2), np.radians(lat2)
    dlon, dlat = lon2_rad - lon1_rad, lat2_rad - lat1_rad
    a = np.sin(dlat/2)**2 + np.cos(lat1_rad)*np.cos(lat2_rad)*np.sin(dlon/2)**2
    c = 2 * np.arcsin(np.sqrt(a))
    return R * c



config = configparser.ConfigParser()
config.read("../config.ini", encoding='utf-8')
root_dir = config.get('common', 'rootDir')
file3path = config.get('common', 'file3pathwsg')
province_path = config.get('common', 'provincePath')
SAVE_DIR = config.get('common', 'saveDir')
LON_MIN = float(config.get('common', 'lonMin'))
LON_MAX = float(config.get('common', 'lonMax'))
LAT_MIN = float(config.get('common', 'latMin'))
LAT_MAX = float(config.get('common', 'latMax'))

def draw_contour_with_labels(timeStr):
    timeObj = datetime.strptime(timeStr, '%Y%m%d%H%M%S')


    fileYearStr = str(timeObj.year)
    fileDayStr = str(timeObj.strftime('%Y%m%d'))
    path_join = os.path.join(root_dir, fileDayStr, file3path)
    fileName = os.path.join(path_join, timeStr + ".000")
    data_path = os.path.join(path_join, fileName)
    # 构造文件名：时间格式化字符串 + "aaa" + 后缀
    save_filename = f"{timeObj.strftime('%Y%m%d%H%M%S')}_位势高.png"
    # 拼接完整保存路径
    save_path_dir = os.path.join(SAVE_DIR,fileYearStr,fileDayStr)
    save_path = os.path.join(save_path_dir, save_filename)
    # 确保保存目录存在（不存在则创建）
    os.makedirs(save_path_dir, exist_ok=True)
    data = meb.read_micaps14(data_path)


    # 提取数据
    lines = data['lines']['line_xyz']
    line_labels = data['lines']['line_label']
    troughs = data['lines_symbol']['linesym_xyz']

    # 标签绘制参数（可按需调整）
    FONT_SIZE = 12          # 标签字体大小
    MIN_LABEL_DIST = 50     # 标签最小间距（公里）
    MIN_LINE_LENGTH = 100   # 线长阈值（公里，小于则不加标签）
    MAX_LABELS_PER_LINE = 3 # 单条线最多标签数
    contour_color = (100/255, 100/255, 255/255)  # 等高线颜色

    # 创建图形
    fig = plt.figure(figsize=(12, 10))
    ax = plt.axes(projection=ccrs.PlateCarree())
    ax.set_extent([LON_MIN, LON_MAX, LAT_MIN, LAT_MAX], crs=ccrs.PlateCarree())

    # 添加地图要素
    ax.add_feature(cfeature.COASTLINE.with_scale('50m'), linewidth=0.8)
    ax.add_feature(cfeature.BORDERS.with_scale('50m'), linewidth=1)
    drawShp(ax, province_path, zorder=1, linewidth=0.4, edgecolor=(133/255,6/255,6/255))
    ax.add_feature(cfeature.LAND.with_scale('50m'), facecolor='white', alpha=0.3)
    ax.add_feature(cfeature.OCEAN.with_scale('50m'), facecolor=(173/255,198/255,231/255), alpha=1)

    # 绘制等高线（动态标签+动态挖空）
    for i, (line_coords, label) in enumerate(zip(lines, line_labels)):
        # 1. 宽松过滤：保留地图附近的点
        loose_mask = (line_coords[:,0] >= LON_MIN-3) & (line_coords[:,0] <= LON_MAX+3) & \
                     (line_coords[:,1] >= LAT_MIN-3) & (line_coords[:,1] <= LAT_MAX+3)
        filtered_coords = line_coords[loose_mask]
        if len(filtered_coords) < 10:
            continue

        # 2. 计算线总长度，判断是否加标签
        total_length = sum(haversine_distance(filtered_coords[j-1,0], filtered_coords[j-1,1],
                                              filtered_coords[j,0], filtered_coords[j,1])
                            for j in range(1, len(filtered_coords)))
        if total_length < MIN_LINE_LENGTH:
            ax.plot(filtered_coords[:,0], filtered_coords[:,1], color=contour_color,
                    linewidth=2, transform=ccrs.PlateCarree())
            continue

        # 3. 动态计算标签数量与位置
        n_labels = min(MAX_LABELS_PER_LINE, max(1, int(total_length/200)))
        label_indices = np.linspace(0, len(filtered_coords)-1, n_labels, dtype=int)

        # 4. 过滤过近的标签
        valid_indices, prev_lon, prev_lat = [], None, None
        for idx in label_indices:
            lon, lat = filtered_coords[idx,0], filtered_coords[idx,1]
            if prev_lon is None or haversine_distance(prev_lon, prev_lat, lon, lat) > MIN_LABEL_DIST:
                valid_indices.append(idx)
                prev_lon, prev_lat = lon, lat
        label_indices = valid_indices
        if not label_indices:
            ax.plot(filtered_coords[:,0], filtered_coords[:,1], color=contour_color,
                    linewidth=2, transform=ccrs.PlateCarree())
            continue

        # 5. 计算挖空区间与标签角度
        gap_intervals, label_info = [], []
        for idx in label_indices:
            # 平滑计算旋转角度
            angle_s, angle_e = max(0, idx-10), min(len(filtered_coords)-1, idx+10)
            dx = filtered_coords[angle_e,0] - filtered_coords[angle_s,0]
            dy = filtered_coords[angle_e,1] - filtered_coords[angle_s,1]
            angle = np.degrees(np.arctan2(dy, dx)) if (dx, dy) != (0,0) else 0
            if angle > 90 or angle < -90:
                angle += 180

            # 动态挖空大小
            gap_size = int(FONT_SIZE/3) + 2
            if abs(angle) > 60:
                gap_size += 1
            gap_s, gap_e = max(0, idx-gap_size), min(len(filtered_coords)-1, idx+gap_size+1)
            gap_intervals.append((gap_s, gap_e))
            label_info.append((filtered_coords[idx,0], filtered_coords[idx,1], angle))

        # 6. 合并重叠挖空
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

        # 7. 分段绘制等高线（挖空）
        prev_end = 0
        for s, e in merged_gaps:
            if prev_end < s:
                ax.plot(filtered_coords[prev_end:s,0], filtered_coords[prev_end:s,1],
                        color=contour_color, linewidth=2, transform=ccrs.PlateCarree())
            prev_end = e
        if prev_end < len(filtered_coords):
            ax.plot(filtered_coords[prev_end:,0], filtered_coords[prev_end:,1],
                    color=contour_color, linewidth=2, transform=ccrs.PlateCarree())

        # 8. 添加标签
        for lon, lat, angle in label_info:
            if LON_MIN <= lon <= LON_MAX and LAT_MIN <= lat <= LAT_MAX:
                ax.text(lon, lat, str(label), fontsize=FONT_SIZE, fontweight='bold',
                        color=contour_color, ha='center', va='center', rotation=angle,
                        transform=ccrs.PlateCarree())

    # 绘制槽线
    trough_added = False
    for trough_coords in troughs:
        mask = (trough_coords[:,0] >= LON_MIN) & (trough_coords[:,0] <= LON_MAX) & \
               (trough_coords[:,1] >= LAT_MIN) & (trough_coords[:,1] <= LAT_MAX)
        filtered_trough = trough_coords[mask]
        if len(filtered_trough) > 1:
            color = (169/255, 50/255, 50/255)
            if not trough_added:
                ax.plot(filtered_trough[:,0], filtered_trough[:,1], color=color,
                        linewidth=2.5, transform=ccrs.PlateCarree(), label='槽线')
                trough_added = True
            else:
                ax.plot(filtered_trough[:,0], filtered_trough[:,1], color=color,
                        linewidth=2.5, transform=ccrs.PlateCarree())

    # 添加高低压中心
    if 'symbols' in data and data['symbols'] is not None:
        symbols = data['symbols']['symbol_xyz']
        codes = data['symbols']['symbol_code']
        for sym, code in zip(symbols, codes):
            x, y = sym[0], sym[1]
            if LON_MIN <= x <= LON_MAX and LAT_MIN <= y <= LAT_MAX:
                ax.text(x, y, 'G' if code==60 else 'D', fontsize=15, fontweight='bold',
                        color='blue' if code==60 else 'red', ha='center', va='center',
                        transform=ccrs.PlateCarree())

    # 经纬度网格
    gl = ax.gridlines(draw_labels=True, linewidth=0.5, color='gray', alpha=0.5)
    gl.top_labels, gl.right_labels = False, False
    gl.xlabel_style, gl.ylabel_style = {'size':10}, {'size':10}

    # 标题
    plt.title(f'500hPa 位势高度场\n时间: {timeObj.strftime("%Y年%m月%d日%H时")}',
              fontsize=14, fontweight='bold')

    plt.tight_layout()
    # 保存图片到指定路径
    plt.savefig(
        save_path,
        dpi=300,  # 分辨率，300dpi保证高清
        bbox_inches='tight',  # 去除图片边缘空白
        facecolor='white'  # 背景色为白色
    )
    plt.close()
    return save_path
# 使用示例
if __name__ == "__main__":
    draw_contour_with_labels('20250628080000')