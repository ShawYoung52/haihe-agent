# -*- coding: utf-8 -*-
"""
预报检验模块

提供降水和温度检验的查询、分析和可视化功能
"""

# 核心功能
from .forecast_evaluate import (
    # 检验执行函数
    run_rain_eva,
    run_rain_eva_all_grades,
    run_temp_eva,
    
    # 请求和数据分析（拆分后的独立函数）
    request_scores,
    generate_charts,
    generate_analysis_report,
    
    # JSON构建函数
    create_rain_test_json,
    create_temp_test_json,
    
    # 数据加载函数
    load_forecast_data,
    analyze_saved_data,
    list_saved_files,
    generate_rain_report,
    generate_temp_report,
)

# 批量下载功能
from .batch_download import (
    download_all_temp_data,
    download_all_rain_data,
    download_all_forecast_data,
    download_temp_only,
    download_rain_only,
    download_by_element,
)

# 目录整理功能
from .organize_files import (
    verify_directory_structure,
    clean_empty_directories,
    generate_structure_report,
)

# 数据分析功能
from .analyzer import ForecastAnalyzer

# 数据加载器
from .data_loader import ForecastDataLoader

# 报告生成功能
from .report_generator import (
    ForecastReport,
    generate_monthly_report,
    print_report,
)

# DOCX 报告生成
from .report_docx import generate_docx_report

# 配置和工具函数
from .config import (
    Config,
    get_json_save_path,
    get_png_save_path,
    parse_json_filename,
)

__version__ = '1.0.0'
__all__ = [
    # 核心检验
    'run_rain_eva',
    'run_rain_eva_all_grades',
    'run_temp_eva',

    # 请求和数据分析（拆分后的独立函数）
    'request_scores',
    'generate_charts',
    'generate_analysis_report',

    # JSON构建
    'create_rain_test_json',
    'create_temp_test_json',
    
    # 批量下载
    'download_all_temp_data',
    'download_all_rain_data',
    'download_all_forecast_data',
    'download_temp_only',
    'download_rain_only',
    'download_by_element',
    
    # 目录整理
    'verify_directory_structure',
    'clean_empty_directories',
    'generate_structure_report',
    
    # 数据加载
    'load_forecast_data',
    'analyze_saved_data',
    'list_saved_files',
    'generate_rain_report',
    'generate_temp_report',
    
    # 类和配置
    'ForecastAnalyzer',
    'ForecastDataLoader',
    'ForecastReport',
    'generate_monthly_report',
    'print_report',
    'generate_docx_report',
    'Config',
    
    # 路径工具函数
    'get_json_save_path',
    'get_png_save_path',
    'parse_json_filename',
]
