import os
import pickle
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterator

import matplotlib
import numpy as np
import pandas as pd
import geopandas as gpd
from matplotlib import pyplot as plt

from utils.MusicTool import MusicClient, MusicConfig
from utils.db import engine
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
import matplotlib.font_manager as fm

matplotlib.use("Agg")
font_path = "/usr/share/fonts/simhei.ttf"

# 注册字体文件
fm.fontManager.addfont(font_path)

# 从字体文件读取真实字体名
font_name = fm.FontProperties(fname=font_path).get_name()
print(font_name)

# 设置为默认字体
plt.rcParams["font.family"] = font_name
plt.rcParams["axes.unicode_minus"] = False

_GRAPH_CACHE = None
_GRAPH_CACHE_PATH = None
_GRAPH_CACHE_MTIME = None
_GRAPH_LOCK = threading.RLock()

# 河名 -> 该河在 DAG 中所有边的终点节点（用于下游分析，避免每次全图扫边）
_END_NODES_BY_RIVER: dict[str, set] | None = None
_END_NODES_INDEX_META: tuple | None = None
_RIVER_LOCATE_CACHE: dict[tuple, dict] = {}
_RIVER_LOCATE_CACHE_TTL_SEC = 300

def draw24hrainpic(timestr):
    client = MusicClient(MusicConfig())
    #
    timerange = f"[{(datetime.strptime(timestr, '%Y%m%d%H%M%S') - timedelta(hours=31)).strftime('%Y%m%d%H%M%S')},{(datetime.strptime(timestr, '%Y%m%d%H%M%S') - timedelta(hours=8)).strftime('%Y%m%d%H%M%S')}]"
    res = client.stat_surf_pre_in_basin_new("HHLY_JUECE", timerange,staLevels = '016')

    df = pd.DataFrame(res)
    # df.to_csv("HHLY_JUECE.csv")

    # df = pd.read_csv(r"C:\Users\123\Desktop\HHLY_JUECE.csv")
    # print(df)

    df["Lat"] = df["Lat"].astype("float")
    df["Lon"] = df["Lon"].astype("float")
    df["SUM_PRE_1H"] = df["SUM_PRE_1H"].astype("float")

    # 去掉缺失值
    plot_df = df.dropna(subset=["Lat", "Lon", "SUM_PRE_1H"]).copy()

    # 定义阈值分组
    bins = [
        -np.inf,
        0.1,
        10,
        25,
        50,
        100,
        250,
        # 400,
        # 600,
        np.inf
    ]
    labels = [
        "0-0.1",
        "0.1-10",
        "10-25",
        "25-50",
        "50-100",
        "100-250",
        ">250",
        # "400-600",
        # ">600"
    ]

    plot_df["rain_level"] = pd.cut(
        plot_df["SUM_PRE_1H"],
        bins=bins,
        labels=labels,
        right=False
    )

    # 每个等级对应颜色
    color_map = {
        "0-0.1": "#ffffff00",
        "0.1-10": "#a6f28fff",
        "10-25": "#3dba3dff",
        "25-50": "#61b8ffff",
        "50-100": "#0000ffff",
        "100-250": "#ff00ffff",
        ">250": "#800040ff",
        # "400-600": "#fcaa09ff",
        # ">600": "#fd6905ff"
    }

    # 每个等级对应点大小
    size_map = {
        "0-0.1": 2,
        "0.1-10": 4,
        "10-25": 7,
        "25-50": 11,
        "50-100": 14,
        "100-250": 17,
        ">250": 20,
        # "400-600": 23,
        # ">600": 26,
    }
    print(plot_df)

    plot_df["color"] = plot_df["rain_level"].map(color_map)
    plot_df["size"] = plot_df["rain_level"].map(size_map)

    fig, ax = plt.subplots(figsize=(10, 10))
    ax.set_xlim(112, 120)
    ax.set_ylim(35.5, 43)
    point_gdf = gpd.GeoDataFrame(
        plot_df,
        geometry=gpd.points_from_xy(plot_df["Lon"], plot_df["Lat"]),
        crs="EPSG:4326"
    )

    point_gdf["rain_level"] = pd.cut(
        point_gdf["SUM_PRE_1H"],
        bins=bins,
        labels=labels,
        right=False
    )



    point_gdf["color"] = point_gdf["rain_level"].map(color_map)
    point_gdf["size"] = point_gdf["rain_level"].map(size_map)

    point_gdf.plot(
        ax=ax,
        markersize=point_gdf["size"],
        color=point_gdf["color"],
        alpha=1,
    )

    # admin_gdf = gpd.read_postgis(
    #     """SELECT
    #         geom
    #     FROM haihe_admin_division_bak
    #     """,
    #     engine,
    #     geom_col="geom"
    # )

    province_gdf = gpd.read_file(r'./Service/sheng.geojson')
    province_gdf.boundary.plot(
        ax=ax,
        color="black",
        linewidth=1.5,
        zorder=1
    )

    admin_gdf = gpd.read_file(r'./Service/shi.geojson')

    admin_gdf = admin_gdf.set_crs(epsg=4326)

    admin_gdf.boundary.plot(
        ax=ax,
        color="black",
        linewidth=0.5,
        zorder=1,
        linestyle=':',
    )
    for _, row in admin_gdf.iterrows():
        point = row.geometry.representative_point()
        if point.x >112 and point.x < 120 and point.y > 35.5 and point.y < 43:

            ax.text(
                point.x,
                point.y,
                row["name"],
                fontsize=8,
                color="black",
                ha="center",
                va="center",
                zorder=10
            )

    river_zone_246 = gpd.read_postgis(
        """SELECT gid,geom
           FROM haihe_246_zone
           WHERE 1 = 1""",
        engine,
        geom_col="geom"
    )
    # print(river_zone_246)

    rain_point_gdf = point_gdf[point_gdf['SUM_PRE_1H'] > 50].copy()

    matched_polygons = gpd.sjoin(
        river_zone_246,
        rain_point_gdf,
        how="inner",
        predicate="intersects"
    )
    # print(matched_polygons)

    # 3. 如果一个面内有多个点，会产生重复面；去重
    matched_polygons = matched_polygons.drop_duplicates(subset=["gid"])
    # print(matched_polygons)
    # print(matched_polygons.columns)

    # river_zone_246.boundary.plot(
    #     ax=ax,
    #     color="black",
    #     linewidth=0.8,
    #     zorder=1
    # )


    matched_polygons.plot(
        ax=ax,
        facecolor="yellow",
        alpha=0.2,
        label = "影像河系",
        aspect="auto"
    )

    river_gdf = gpd.read_postgis(
        """
        SELECT id, geom,river_name
        FROM haihe_river_directed_full_v4
        """,
        engine,
        geom_col="geom"
    )
    polygon_union = matched_polygons.geometry.union_all()

    intersect_lines = river_gdf[
        river_gdf.geometry.intersects(polygon_union)
    ].copy()

    print(intersect_lines['river_name'].tolist(),"河流名称")

    indirect_impact_rivers = gpd.read_postgis(
        f"""
SELECT id, geom,river_name from haihe_river_directed_full_v4 where river_name in ('{("','").join(intersect_lines['river_name'].tolist())}')""",
        engine,
        geom_col="geom"
    )
    print(f"""
SELECT id, geom,river_name from haihe_river_directed_full_v4 where river_name in ('{("','").join(intersect_lines['river_name'].tolist())}')""")

    print(indirect_impact_rivers,"间接影响河流")

    indirect_impact_rivers.plot(
        ax=ax,
        linewidth=0.8,
        color="green",
    )

    intersect_lines.plot(
        ax=ax,
        linewidth=0.8,
        color="blue",
    )

    legend_handles = []

    # 降雨等级图例
    for level in labels:
        if level in color_map:
            legend_handles.append(
                mlines.Line2D(
                    [],
                    [],
                    marker="o",
                    linestyle="None",
                    markerfacecolor=color_map[level],
                    # markeredgecolor="black",
                    markersize=max(4, np.sqrt(size_map[level])),
                    label=level
                )
            )

    # 面图层图例：影像河系
    legend_handles.append(
        mpatches.Patch(
            facecolor="yellow",
            edgecolor="none",
            alpha=0.2,
            label="影响河系"
        )
    )

    # 河流线图例
    legend_handles.append(
        mlines.Line2D(
            [],
            [],
            color="blue",
            linewidth=0.8,
            label="影响河流"
        )
    )

    # 行政边界图例
    legend_handles.append(
        mlines.Line2D(
            [],
            [],
            color="black",
            linewidth=0.8,
            label="市界"
        )
    )
    legend_handles.append(
        mlines.Line2D(
            [],
            [],
            color="black",
            linewidth=1.5,
            label="省界"
        )
    )


    ax.legend(handles=legend_handles,title="图例", loc="lower right", frameon=True)

    plt.tight_layout()
    plt.savefig("rain_level.png")


def _get_downstream_impacts_structured(
    river: str,
    attr_name: str = "length_km",
) -> list[dict]:
    """
    返回结构化的下游影响结果，不是自然语言。
    """
    import heapq

    G = get_graph()

    river_end_nodes = set(_get_end_nodes_by_river_map().get(river, ()))

    if not river_end_nodes:
        return []

    best_dist: dict = {node: 0.0 for node in river_end_nodes}
    heap: list[tuple[float, object]] = [(0.0, node) for node in river_end_nodes]
    heapq.heapify(heap)

    impact_distances: dict[str, float] = {}

    while heap:
        current_dist, curr_node = heapq.heappop(heap)

        if current_dist > best_dist.get(curr_node, float("inf")):
            continue

        for _u, next_node, attr in G.out_edges(curr_node, data=True):
            r_name = get_edge_river_name(attr)
            edge_len = get_edge_length_km(attr, attr_name=attr_name)

            if r_name and r_name != river:
                old = impact_distances.get(r_name, float("inf"))
                if current_dist < old:
                    impact_distances[r_name] = current_dist

            next_dist = current_dist if r_name == river else current_dist + edge_len

            if next_dist < best_dist.get(next_node, float("inf")):
                best_dist[next_node] = next_dist
                heapq.heappush(heap, (next_dist, next_node))

    result = []
    for to_river in sorted(impact_distances.keys()):
        result.append({
            "river_name": to_river,
            "impact_distance_km": round(float(impact_distances[to_river]), 3),
        })
    return result

def get_graph(force_reload: bool = False):
    """
    懒加载并缓存河网图。
    - 首次调用时从 pickle 加载
    - 后续直接复用内存对象
    - 如果底层文件 mtime 变化，则自动重新加载
    - force_reload=True 时强制刷新
    """
    global _GRAPH_CACHE, _GRAPH_CACHE_PATH, _GRAPH_CACHE_MTIME

    graph_path = r"./Service/river_directed_v4_asis.pkl"
    current_mtime = os.path.getmtime(graph_path)

    with _GRAPH_LOCK:
        need_reload = (
            force_reload
            or _GRAPH_CACHE is None
            or _GRAPH_CACHE_PATH != graph_path
            or _GRAPH_CACHE_MTIME != current_mtime
        )

        if need_reload:
            with open(graph_path, "rb") as f:
                graph = pickle.load(f)

            _GRAPH_CACHE = graph
            _GRAPH_CACHE_PATH = graph_path
            _GRAPH_CACHE_MTIME = current_mtime


        return _GRAPH_CACHE

def get_edge_river_name(attr: dict) -> str:
    """兼容不同版本边属性字段，提取河流名称。"""
    if not isinstance(attr, dict):
        return ""
    for key in ("rivername", "river_name", "src_name", "name"):
        val = attr.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if s:
            return s
    return ""

def _get_end_nodes_by_river_map() -> dict[str, set]:
    """按 pickle 文件 mtime 失效；与 get_graph 数据源一致。"""
    global _END_NODES_BY_RIVER, _END_NODES_INDEX_META
    graph_path = r"./Service/river_directed_v4_asis.pkl"
    mtime = os.path.getmtime(graph_path)
    meta = (graph_path, mtime)
    with _GRAPH_LOCK:
        if _END_NODES_BY_RIVER is not None and _END_NODES_INDEX_META == meta:
            return _END_NODES_BY_RIVER
        G = get_graph(False)
        by_river: dict[str, set] = defaultdict(set)
        for _u, v, _key, attr in iter_graph_edges(G):
            rn = get_edge_river_name(attr)
            if rn:
                by_river[rn].add(v)
        _END_NODES_BY_RIVER = dict(by_river)
        _END_NODES_INDEX_META = meta
        return _END_NODES_BY_RIVER

def iter_graph_edges(G) -> Iterator[tuple]:
    """
    统一遍历图的边：
    - DiGraph: yield (u, v, None, attr)
    - MultiDiGraph: yield (u, v, key, attr)
    """
    if G.is_multigraph():
        for u, v, key, attr in G.edges(keys=True, data=True):
            yield u, v, key, attr
    else:
        for u, v, attr in G.edges(data=True):
            yield u, v, None, attr


def get_edge_length_km(attr: dict, attr_name: str = "length_km") -> float:
    """兼容 length_km / len_km / length，返回公里值。"""
    if not isinstance(attr, dict):
        return 0.0
    seen: set[str] = set()
    for key in (attr_name, "length_km", "len_km", "length"):
        if not key or key in seen:
            continue
        seen.add(key)
        raw = attr.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        return 0.0 if value < 0 else value
    return 0.0
if __name__ == '__main__':
    draw24hrainpic("20260606200000")

    # res = _get_downstream_impacts_structured("永定河")
    # print(res)