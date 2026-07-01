"""
气象分析 MCP 服务
使用 FastMCP 框架封装气象图绘制工具
支持 500hPa、700hPa、850hPa
"""

import configparser
import os
from datetime import datetime
import numpy as np

# NumPy 兼容性补丁（meteva 依赖旧 API）
if not hasattr(np, 'float'):
    np.float = np.float64
if not hasattr(np, 'int'):
    np.int = np.int_
if not hasattr(np, 'bool'):
    np.bool = np.bool_

from fastmcp import FastMCP
from weather_analyzer import WeatherAnalyzer

# === 初始化 ===
mcp = FastMCP("weather-analyzer")

_dir = os.path.dirname(os.path.abspath(__file__))
_config = configparser.ConfigParser()
_config.read(os.path.join(_dir, "config.ini"), encoding='utf-8')
_analyzer = WeatherAnalyzer(_config)

# === 气象分析词（AI 读图分析的 system prompt） ===
ANALYSIS_PROMPT = """你是一名专业气象分析员，描述内容丰富具体专业,多多描述京津冀地区的情况。你将收到多张气象图，包括：

500hPa：位势高度场、风羽图
700hPa：位势高度场、风羽图、露点图
850hPa：位势高度场、风羽图、露点图
============================================================
【总原则：必须基于图像内容进行判断】 所有判断必须来自图像本身。
============================================================
【区域限定：所有判断仅针对"京津冀及其东南部流域区域"】 不得使用全国或大范围平均风向、湿度或槽线代替局地判断。 若图像范围较大，你必须仅关注 112°E–120°E、34°N–40°N 区域内的风场与湿度。
============================================================
【风向识别规则（必须从风羽图读取）】
风矢符号（最核心的符号）
风向杆：风向杆是一根杆子，其中带有实心小方块的那端为头部，这个头部的指向就是风的来向
（比如头部朝左，代表西风；头部朝上，代表北风；头部朝西南，代表西南风，特别注意：风羽永远在远离小方块的尾部，绝对不要把风羽所在的尾部指向当成风的来向，且风的来向与风羽尾部的去向永远相反，可依此验证判断是否正确）。
风羽：在风向杆末端的右侧，用短线、长线或三角形来表示风速：
    1 条长划线：代表 4 米 / 秒（约 2 级风）
    1 条短划线：代表 2 米 / 秒（约 1 级风）
    1 个三角形：代表 20 米 / 秒（约 9-10 级风）
    多个风羽可以组合，比如 1 长 1 短代表 6 米 / 秒（约 3 级风），1 个三角 + 1 长划代表 24 米 / 秒（约 10 级风）。
必须以图像为准。
============================================================
【位势高度图识别规则】
蓝色的连续曲线为等高线，并且线上标注了580，584，588等；
红色的曲线为槽线，它时根据蓝线变化趋势来画的；
红色字母D为低压，蓝色字母G为高压；
============================================================
【露点图识别规则】
露点图插值成了面，看下面的图例及颜色变化识别
============================================================
【切变线识别规则（必须从风羽图读取）】
切变线是风场中水平风向或风速发生气旋性突变的狭长区域，核心为风的不连续而非高度场特征，识别时需完全基于风羽图判断，不可直接将位势高度场的槽线等同于切变线，具体为：先屏蔽等高线与槽线等高度场信息，仅观察风矢分布，找到风向或风速突变的狭长带状区域，对比该区域两侧的风矢来向（以风矢头部小方块指向为准），若两侧风向呈气旋性对峙（如北侧为弱偏北风、南侧为西南风，或西侧为偏西风、东侧为偏南风），则该带状区域即为切变线，其走向由风场突变带的几何延伸方向确定（如从山西延伸至河北西部的东北 — 西南走向暖切变线），同时可结合急流轴、湿舌等风场与湿度场特征辅助验证，必须以图像为准。
============================================================
【第一部分：500hPa 分析（位势高度场 + 风场图）】
京津冀处于槽前 / 槽后 / 脊前 / 脊后（基于等高线形态与风场视觉判断）
588线、584线在京津冀附近的具体位置
副高中心位置（若图中可见）
槽轴是否随高度前倾（仅根据等高线判断）
京津冀所在的高度场数值带（580/584/588等线）
============================================================
【第二部分：700hPa 分析（位势 + 风场 + 湿度）】
风场结构
切变线描述及位置
京津冀处于槽前/槽后/脊前/脊后（视觉判断）
湿度分布（Td 值、是否连续成带）
是否存在干层
============================================================
【第三部分：850hPa 分析（位势 + 风场 + 湿度）】
风场结构
是否存在低空急流（≥12m/s）
急流轴位置
切变线描述及位置
湿舌位置、方向、是否压到流域 Td 数值
============================================================
【第四部分：高低空综合分析】
基于图像总结三层结构，不得使用语言逻辑覆盖图像。
============================================================
请严格按以上结构输出。"""


# ============================================================
# 工具 1：获取最新可用时间（轻量，秒回）
# ============================================================
@mcp.tool()
def get_latest_analysis_time() -> dict:
    """获取最新可用的气象数据时间。返回 latest_time 字段可直接传给其他工具。"""
    try:
        t = _analyzer.get_latest_time()
        if t:
            return {"status": "success", "latest_time": t,
                    "formatted_time": datetime.strptime(t, '%Y%m%d%H%M%S').strftime("%Y年%m月%d日%H时")}
        return {"status": "error", "latest_time": None, "message": "未找到可用数据"}
    except Exception as e:
        return {"status": "error", "latest_time": None, "message": str(e)}


# ============================================================
# 工具 2：生成位势高度场（单层面，减少单次耗时）
# ============================================================
@mcp.tool()
def draw_height_field(time_str: str, level: str = "500hPa") -> dict:
    """
    生成指定层面的位势高度场图（等高线、槽线、高低压中心）。
    
    参数:
        time_str: 时间，格式 YYYYMMDDHHMISS，如 20250820080000
        level: 层面，可选 500hPa / 700hPa / 850hPa
    返回:
        {"status": "success"|"error", "path": "图片绝对路径", "message": "..."}
    """
    try:
        time_obj = datetime.strptime(time_str, '%Y%m%d%H%M%S')
        path = _analyzer._draw_height_field(time_obj, level)
        return {"status": "success", "path": path,
                "message": f"{level} 位势高度场生成成功"}
    except Exception as e:
        return {"status": "error", "path": None, "message": str(e)}


# ============================================================
# 工具 3：生成风羽图（单层面）
# ============================================================
@mcp.tool()
def draw_wind_barb(time_str: str, level: str = "500hPa") -> dict:
    """
    生成指定层面的风羽图。
    
    参数:
        time_str: 时间，格式 YYYYMMDDHHMISS，如 20250820080000
        level: 层面，可选 500hPa / 700hPa / 850hPa
    返回:
        {"status": "success"|"error", "path": "图片绝对路径", "message": "..."}
    """
    try:
        time_obj = datetime.strptime(time_str, '%Y%m%d%H%M%S')
        path = _analyzer._draw_wind_barb(time_obj, level)
        return {"status": "success", "path": path,
                "message": f"{level} 风羽图生成成功"}
    except Exception as e:
        return {"status": "error", "path": None, "message": str(e)}


# ============================================================
# 工具 4：生成露点温度图（单层面，仅 700/850）
# ============================================================
@mcp.tool()
def draw_dew_point(time_str: str, level: str = "700hPa") -> dict:
    """
    生成指定层面的露点温度图（仅 700hPa 和 850hPa）。
    
    参数:
        time_str: 时间，格式 YYYYMMDDHHMISS，如 20250820080000
        level: 层面，可选 700hPa / 850hPa（500hPa 无露点图）
    返回:
        {"status": "success"|"error", "path": "图片绝对路径", "message": "..."}
    """
    if level not in ("700hPa", "850hPa"):
        return {"status": "error", "path": None,
                "message": f"{level} 无露点温度数据，仅支持 700hPa 和 850hPa"}
    try:
        time_obj = datetime.strptime(time_str, '%Y%m%d%H%M%S')
        path = _analyzer._draw_dew_point(time_obj, level)
        return {"status": "success", "path": path,
                "message": f"{level} 露点温度图生成成功"}
    except Exception as e:
        return {"status": "error", "path": None, "message": str(e)}


# ============================================================
# 工具 5：批量生成某个层面的所有图表
# ============================================================
@mcp.tool()
def generate_level_charts(time_str: str, level: str = "500hPa") -> dict:
    """
    生成指定层面的全部气象图（位势高度场 + 风羽图，700/850hPa 还包括露点图）。
    
    参数:
        time_str: 时间，格式 YYYYMMDDHHMISS，如 20250820080000
        level: 层面，可选 500hPa / 700hPa / 850hPa
    返回:
        {"status": "success"|"partial"|"error", "charts": {"height_field": "路径", ...}, "message": "..."}
    """
    try:
        result = _analyzer.generate_charts(time_str, level=level)
        return result
    except Exception as e:
        return {"status": "error", "level": level, "charts": {}, "message": str(e)}


# ============================================================
# 工具 6：获取气象分析词（AI 读图分析的 prompt）
# ============================================================
@mcp.tool()
def get_analysis_prompt() -> dict:
    """
    获取气象分析词（system prompt）。
    AI 生成气象图后，应调用此工具获取分析规则，然后按规则读图并输出结构化的气象分析报告。
    分析词包含：风向识别规则、位势高度图识别规则、露点图识别规则、切变线识别规则、
    以及 500hPa/700hPa/850hPa 三个层面的分析要点和输出结构。
    """
    return {
        "status": "success",
        "prompt": ANALYSIS_PROMPT,
        "message": "请将此 prompt 作为系统指令，结合生成的气象图进行分析"
    }


# ============================================================
# 启动入口：支持 stdio / sse 两种模式
# ============================================================
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Weather Analyzer MCP Service")
    p.add_argument("--transport", choices=["stdio", "sse"], default="stdio",
                   help="stdio=本地(Cline/Claude Desktop), sse=远程服务器部署")
    p.add_argument("--host", default="0.0.0.0", help="SSE 监听地址 (默认 0.0.0.0)")
    p.add_argument("--port", type=int, default=8000, help="SSE 监听端口 (默认 8000)")
    args = p.parse_args()

    if args.transport == "sse":
        print(f"[weather-analyzer] SSE mode: http://{args.host}:{args.port}/sse")
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run()
