"""海河流域降雨分析MCP服务器"""

from fastmcp import FastMCP

import fixed_rainfall_impact_tool as rainfall_impact_tool
from tools import register_tools
from custom_tools import (
    register_historical_same_period_rainfall_tool,
    register_last_month_areal_rainfall_tool,
    register_last_year_max_daily_rainfall_tool,
    register_poi_nearest_observation_tool,
    register_risk_warning_tool,
    register_safe_emergency_response_tool,
    register_year_to_date_areal_rainfall_tool,
)


class HaiheWeatherAnalyzerMCP:
    """海河流域降雨分析MCP服务器"""

    def __init__(self):
        self.mcp = FastMCP("海河流域降雨分析服务")
        self._register_tools()

    def _register_tools(self):
        """注册所有工具"""
        register_tools(self.mcp)
        # 对齐牵引智能体 hhlyqyxt-master/utils/rainfall_impact_geojson.py：
        # build_rain24h_impact_river_geojson(..., direct_match_km=10.0)。
        rainfall_impact_tool.DEFAULT_DIRECT_GRAPH_MATCH_KM = 10.0
        rainfall_impact_tool.register_fixed_rainfall_impact_tool(self.mcp)
        register_last_month_areal_rainfall_tool(self.mcp)
        register_last_year_max_daily_rainfall_tool(self.mcp)
        register_historical_same_period_rainfall_tool(self.mcp)
        register_year_to_date_areal_rainfall_tool(self.mcp)
        register_poi_nearest_observation_tool(self.mcp)
        register_risk_warning_tool(self.mcp)
        register_safe_emergency_response_tool(self.mcp)

        @self.mcp.tool()
        def get_service_info() -> dict:
            """获取服务基本信息和功能说明。"""
            return {
                "service_name": "海河流域降雨分析MCP服务",
                "version": "1.0.0",
                "description": "提供多维度降雨数据查询和分析功能",
                "available_tools": [
                    "get_station_history - 获取站点历史数据（历史占位，建议改用新工具）",
                    "query_time_range - 时间范围查询（历史占位，建议改用新工具）",
                    "query_nearby_stations - 位置查询（历史占位，建议改用新工具）",
                    "calculate_rainfall_statistics - 数据统计（历史占位，建议改用新工具）",
                    "analyze_region_rainfall - 区域分析（历史占位，建议改用新工具）",
                    "get_rainfall_forecast - 降雨预报（历史占位，建议改用新工具）",
                    "check_rainfall_alerts - 预警检查（历史占位，建议改用新工具）",
                    "get_available_stations - 可用站点列表（历史占位，建议改用新工具）",
                    "locate_river_regions - 查询河流所在行政区与分区",
                    "locate_downstream_rivers - 查询下游河流及定位",
                    "get_rainstorm_self_context - 暴雨影响拆分工具(本河)",
                    "get_rainstorm_downstream_context - 暴雨影响拆分工具(下游)",
                    "get_rainstorm_leader_view - 暴雨影响拆分工具(领导视图)",
                    "fetch_haihe_observation_response_inputs - 观测判定拆分(fetch)",
                    "filter_haihe_observation_response_records - 观测判定拆分(filter)",
                    "evaluate_haihe_observation_response_records - 观测判定拆分(evaluate)",
                    "report_haihe_observation_response - 观测判定拆分(report)",
                    "safe_evaluate_haihe_emergency_response - 安全版实况应急响应判定",
                    "fetch_haihe_forecast_response_inputs - 预报判定拆分(fetch)",
                    "filter_haihe_forecast_response_inputs - 预报判定拆分(filter)",
                    "evaluate_haihe_forecast_response_inputs - 预报判定拆分(evaluate)",
                    "report_haihe_forecast_response - 预报判定拆分(report)",
                    "create_haihe_forecast_impact_precip_map_job - 预报影响降水专题图任务（CMA色标）",
                    "create_haihe_observation_impact_precip_map_job - 实况影响降水专题图任务（CMA色标）",
                    "analyze_rainstorm_impact - 聚合工具(兼容旧调用)",
                    "get_river_network_for_plot - 获取河网绘图线段",
                    "get_river_network_leader_view - 获取领导可读卡片+表格+地图数据",
                    "reload_river_graph - 重新加载河网缓存",
                    "analyze_rainfall_by_time - 基于天擎站点分析某时刻降雨（行政区划/77分区/河流）",
                    "get_affected_river_network_by_rainfall - 暴雨影响河流专题图（30km直接不截断，下游50km截断；直接河段匹配10km口径对齐牵引智能体）",
                    "query_last_month_areal_rainfall - 查询上一个自然月的海河9分区累计面雨量",
                    "query_year_to_date_areal_rainfall - 查询今年以来海河9分区累计面雨量",
                    "query_last_year_max_daily_rainfall - 查询上一个自然年最大日降雨量",
                    "query_historical_same_period_avg_rainfall - 查询历史同期平均降雨量",
                    "query_poi_nearest_observation - 查询POI经纬度及最近观测站实况值",
                    "query_risk_warning - 查询山洪、地质灾害或中小河流洪水风险预警",
                    "search_poi - 按名称查询 POI 地点/设施/单位",
                    "search_poi_by_distance - 按名称和经纬度范围查询附近 POI",
                    "query_rolling_forecast - 天津滚动预报综合天气查询",
                ],
                "rainfall_impact_rule": {
                    "direct": "30km缓冲区只用于判断直接影响，直接河流完整输出",
                    "downstream": "从直接河流下游端点追踪50km，最后一段截断",
                    "direct_match": "直接命中真实河段与pkl边匹配采用10km阈值，对齐牵引智能体",
                    "geometry": "使用 haihe_river_directed_full_v5 真实河流几何",
                },
                "compatibility_note": "已保留 analyze_rainstorm_impact 等原有调用方式；新增拆分工具用于分阶段调用，前端可按需渐进迁移。",
                "deprecated_aggregated_tools": [
                    "evaluate_haihe_emergency_response",
                    "evaluate_haihe_forecast_emergency_response",
                    "analyze_rainstorm_impact",
                ],
                "recommended_pipelines": {
                    "observation_response": [
                        "fetch_haihe_observation_response_inputs",
                        "filter_haihe_observation_response_records",
                        "evaluate_haihe_observation_response_records",
                        "report_haihe_observation_response",
                    ],
                    "forecast_response": [
                        "fetch_haihe_forecast_response_inputs",
                        "filter_haihe_forecast_response_inputs",
                        "evaluate_haihe_forecast_response_inputs",
                        "report_haihe_forecast_response",
                    ],
                },
                "supported_regions": ["天津市", "塘沽区", "武清区", "静海区", "宝坻区", "蓟州区", "宁河区"],
                "data_update_frequency": "实时更新",
                "contact": "haihe-weather-support@example.com",
            }

        @self.mcp.tool()
        def search_stations_by_name(name_pattern: str) -> list:
            """根据名称模式搜索站点。"""
            from tools import analyzer

            return [
                station for station in analyzer.stations
                if name_pattern.lower() in station["name"].lower()
            ]

    def start(self, host: str = "localhost", port: int = 8000):
        """启动MCP服务器。"""
        print("🚀 启动海河流域降雨分析MCP服务...")
        print(f"📡 服务地址: http://{host}:{port}")
        self.mcp.run(transport="sse", host=host, port=port)


def main():
    """命令行入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="海河流域降雨分析MCP服务")
    parser.add_argument("--host", default="localhost", help="服务器主机地址")
    parser.add_argument("--port", type=int, default=3333, help="服务器端口")
    parser.add_argument("--isAsync", action="store_true", help="使用异步模式运行")
    args = parser.parse_args()

    service = HaiheWeatherAnalyzerMCP()
    service.start(args.host, args.port)


if __name__ == "__main__":
    main()
