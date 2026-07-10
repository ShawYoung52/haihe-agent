"""问答智能体本地版暴雨影响河流专题图工具。

该工具把原先只存在于 MCP 服务器（haihe-weather-analyzer-mcp/fixed_rainfall_impact_tool.py）
的暴雨影响河流能力暴露为 chainlitexam 本地 LangChain Tool，使问答智能体可以在 planner-only
模式下直接调度。真正的河网拓扑计算仍由外部 hhlyqyxt-master/utils/rainfall_impact_geojson.py 完成。
"""
from __future__ import annotations

import configparser
import importlib.util
import logging
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_MCP_IMPACT_MOD: Any = None
_MCP_TOOLS_MOD: Any = None


def _load_module_from_path(module_name: str, file_path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块 {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_mcp_modules() -> tuple[Any, Any]:
    """懒加载 MCP 侧的降雨分析与影响河流模块。

    这些模块依赖 networkx、fastmcp、pandas、psycopg2 等包，chainlitexam 开发环境
    可能未安装，因此采用函数内懒加载；生产部署时若依赖已就绪即可直接工作。
    """
    global _MCP_IMPACT_MOD, _MCP_TOOLS_MOD
    if _MCP_IMPACT_MOD is None or _MCP_TOOLS_MOD is None:
        repo_root = Path(__file__).resolve().parents[2]
        mcp_dir = repo_root / "haihe-weather-analyzer-mcp"
        _MCP_IMPACT_MOD = _load_module_from_path(
            "mcp_fixed_rainfall_impact", mcp_dir / "fixed_rainfall_impact_tool.py"
        )
        _MCP_TOOLS_MOD = _load_module_from_path("mcp_tools", mcp_dir / "tools.py")
    return _MCP_IMPACT_MOD, _MCP_TOOLS_MOD


def _load_mcp_config() -> configparser.ConfigParser:
    repo_root = Path(__file__).resolve().parents[2]
    config_path = repo_root / "haihe-weather-analyzer-mcp" / "config.ini"
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8-sig")
    return config


def build_rainfall_river_impact_tools() -> list:
    """返回问答智能体本地暴雨影响河流工具列表。"""

    @tool
    def local_get_affected_river_network_by_rainfall(
        time_str: str,
        start_time: str = "",
        end_time: str = "",
        rainfall_threshold_mm: float = 50.0,
        max_edges: int = 5000,
        include_background: bool = True,
        downstream_km: float = 50.0,
        direct_graph_match_km: float = 3.0,
    ) -> dict:
        """制作暴雨影响河流专题图数据（本地版本）。

        参数与 MCP 版 `get_affected_river_network_by_rainfall` 保持一致：
        - time_str: 查询基准时间（如 20250709080000）。
        - start_time/end_time: 可选自定义起止时间（%Y%m%d%H%M%S）。
        - rainfall_threshold_mm: 降雨阈值，默认 50mm。
        - max_edges: 最大返回河段数，默认 5000。
        - include_background: 是否包含背景河段（透传给外部 builder）。
        - downstream_km: 下游追踪距离，默认 50km。
        - direct_graph_match_km: 直接河段匹配距离，默认 3km。
        """
        try:
            impact_mod, tools_mod = _load_mcp_modules()
        except Exception as exc:
            logger.warning("[RainfallRiverImpact] 加载 MCP 依赖模块失败：%s", exc)
            return {
                "error": "本地暴雨影响河流工具暂不可用：缺少 MCP 依赖模块或外部 builder。",
                "detail": str(exc),
            }

        try:
            config = _load_mcp_config()
            pg_conf = dict(config["postgres"])
            graph_path = config.get("paths", "graph", fallback="")
            return impact_mod.build_affected_river_network_result(
                time_str=time_str,
                start_time=start_time,
                end_time=end_time,
                rainfall_threshold_mm=rainfall_threshold_mm,
                max_edges=max_edges,
                include_background=include_background,
                downstream_km=downstream_km,
                direct_graph_match_km=direct_graph_match_km,
                pg_conf=pg_conf,
                analyze_rainfall_core=tools_mod._analyze_rainfall_core,
                rain_levels=tools_mod.RAIN_LEVELS,
                graph_path=graph_path,
            )
        except Exception as exc:
            logger.exception("[RainfallRiverImpact] 调用失败")
            return {"error": "暴雨影响河流分析执行失败", "detail": str(exc)}

    return [local_get_affected_river_network_by_rainfall]