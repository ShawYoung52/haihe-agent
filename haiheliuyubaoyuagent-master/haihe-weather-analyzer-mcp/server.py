"""海河流域降雨分析MCP服务器"""

from fastmcp import FastMCP

from tools import register_tools
from fixed_rainfall_impact_tool import register_fixed_rainfall_impact_tool
from custom_tools import (
    register_historical_same_period_rainfall_tool,
    register_last_month_areal_rainfall_tool,
    register_last_year_max_daily_rainfall_tool,
    register_poi_nearest_observation_tool,
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
        register_fixed_rainfall_impact_tool(self.mcp)
        register_last_month_areal_rainfall_tool(self.mcp)
        register_last_year_max_daily_rainfall_tool(self.mcp)
        register_historical_same_period_rainfall_tool(self.mcp)
        register_year_to_date_areal_rainfall_tool(self.mcp)
        register_poi_nearest_observation_tool(self.mcp)

        @self.mcp.tool()
        def get_service_info() -> dict:
            """获取服务基本信息和功能说明"""
            return {
                "service_name": "海河流域降雨分析MCP服务",
                "version": "1.0.0",
                "description": "提供多维度降雨数据查询和分析功能",
                "rainfall_impact_rule": {
                    "direct": "30km只判断直接影响，直接河流完整输出",
                    "downstream": "下游50km截断",
                    "geometry": "full_v5真实河流几何",
                },
            }

        @self.mcp.tool()
        def search_stations_by_name(name_pattern: str) -> list:
            """根据名称模式搜索站点"""
            from tools import analyzer
            return [
                station for station in analyzer.stations
                if name_pattern.lower() in station["name"].lower()
            ]

    def start(self, host: str = "localhost", port: int = 8000):
        """启动MCP服务器"""
        print("🚀 启动海河流域降雨分析MCP服务...")
        print(f"📡 服务地址: http://{host}:{port}")
        self.mcp.run(transport="sse", host=host, port=port)


def main():
    """主函数 - 命令行入口点"""
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
