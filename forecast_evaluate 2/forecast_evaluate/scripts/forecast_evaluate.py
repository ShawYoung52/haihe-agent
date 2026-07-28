# -*- coding: utf-8 -*-
"""
预报检验模块
提供降水和温度检验的查询、分析和可视化功能
"""

import os
import re
import json
import base64
import requests
import platform
import sys
from pathlib import Path
from datetime import datetime

import matplotlib.pyplot as plt
import numpy as np
from rich import print as rprint

from config import Config

debug = False

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei' if platform.system() == 'Windows' else 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

# 添加当前目录到路径，确保可以从任意位置导入
_current_dir = Path(__file__).parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

# 导入配置类
try:
    from config import Config, get_json_save_path, get_png_save_path
except ImportError:
    # 如果相对导入失败，尝试直接导入
    import importlib.util
    _config_path = _current_dir / 'config.py'
    _spec = importlib.util.spec_from_file_location('config', _config_path)
    _config_module = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_config_module)
    Config = _config_module.Config
    get_json_save_path = _config_module.get_json_save_path
    get_png_save_path = _config_module.get_png_save_path


def _parse_and_plot(response_data):
    """解析响应数据并创建柱状图
    
    Args:
        response_data: API响应数据
    """    

    # rprint(response_data)
    
    # 提取数据
    element_name = response_data.get('element', '')
    element_code = response_data.get('element_code', '')
    test_type = response_data.get('test_type_code', '')
    rain_type = response_data.get('rain_type', None)
    time_range = response_data.get('time_range', {})
    raw_response = response_data.get('raw_response', {})
    
    # 从响应数据中获取 predict_hours，如果没有则尝试从 json_file 路径推断
    predict_hours = response_data.get('predict_hours')
    if not predict_hours:
        json_file = response_data.get('json_file', '')
        if json_file:
            path_parts = Path(json_file).parts
            for i, part in enumerate(path_parts):
                if part in ['08', '20']:
                    if i > 0 and path_parts[i-1] in ['daily', 'time_session', 'area']:
                        predict_hours = part
                        break
    #
    data = raw_response.get('data', {})
    exam_data = data.get('examData', [])
    exam_column_name = data.get('examColumnName', [])

    if not exam_data:
        rprint("[yellow]没有找到 examData 数据[/yellow]")
        return []

    if not exam_column_name:
        rprint("[yellow]没有找到 examColumnName 数据[/yellow]")
        return []
    
    # 当test_type为area时，替换列为地区名称
    if test_type == 'area':
        exam_column_name = [Config.TJ_AREA_NAMES.get(str(col), str(col)) for col in exam_column_name]

    # 从API响应数据中提取年月（从metadata获取）
    begin_time = time_range.get('begin', '')
    end_time = time_range.get('end', '')
    year_month = begin_time[:7].replace('-', '')
    today = datetime.now().strftime('%Y-%m-%d')
    if end_time[:10] > today:
        end_time = today + end_time[10:]
    time_range = begin_time[:10].replace('-', '') + ' 至 ' + end_time[:10].replace('-', '')

    # 按 examName 分组
    exam_groups = {}
    for item in exam_data:
        exam_name = item.get('examName', '未知')
        if exam_name not in exam_groups:
            exam_groups[exam_name] = []
        exam_groups[exam_name].append(item)

    save_paths = {}
    for exam_name, items in exam_groups.items():
        if rain_type:
            subtype = exam_name.split('_')[-1]
            subtype = Config.RAIN_SUBTYPE_NAMES.get(subtype, subtype)
        else:
            subtype = ""
        time_session = items[0].get('timeSession', None)

        data_codes = []
        values_list = []

        for item in items:
            data_codes.append(item.get('dataCode', '未知'))
            valid_values = [float(v) if v is not None else 0 for v in item.get('values', [])]
            if len(valid_values) < len(exam_column_name):
                valid_values.extend([0] * (len(exam_column_name) - len(valid_values)))
            elif len(valid_values) > len(exam_column_name):
                valid_values = valid_values[:len(exam_column_name)]
            values_list.append(valid_values)

        # 创建图表
        fig_width, fig_height = 14, 8
        plt.figure(figsize=(fig_width, fig_height))
        x = np.arange(len(exam_column_name))

        max_width = 0.8
        width = min(0.25, max_width / len(data_codes))
        colors = ['#3498db', '#e74c3c', '#2ecc71', '#9b59b6', '#f39c12', '#1abc9c', '#e91e63', '#00bcd4']

        for i, (data_code, values) in enumerate(zip(data_codes, values_list)):
            color = colors[i % len(colors)]
            offset = (i - (len(data_codes) - 1) / 2) * width
            label = Config.PRODUCT_NAMES.get(data_code, data_code)
            bars = plt.bar(x + offset, values, width, label=label, color=color, alpha=0.85)

            for j, bar in enumerate(bars):
                height = bar.get_height()
                if height > 0:
                    plt.text(bar.get_x() + bar.get_width()/2., height * 1.02,
                            f'{values[j]:.1f}', ha='center', va='bottom', fontsize=10)

        plt.ylabel(f'{exam_name.split("_")[0].upper()}', fontsize=14, fontweight='bold')

        test_type = Config.TEST_TYPE_NAMES.get(test_type, test_type)
        
        if not predict_hours:
            predict_hours = response_data.get('predict_hours', '08,20')
        if predict_hours and predict_hours != '08,20':
            predict_hours_display = f"{predict_hours}起报"
        else:
            predict_hours_display = "08,20起报"
        
        if time_session is not None:
            title = f'{element_name}检验  {subtype}({time_session}h) {test_type}'
        else:
            title = f'{element_name}检验  {subtype} {test_type}'
        
        plt.title(f'{title} \n {time_range} ({predict_hours_display})',  fontsize=16, fontweight='bold', pad=30)

        # 处理横轴标签
        processed_labels = []
        for label in exam_column_name:
            if isinstance(label, str) and re.search(r'^\d{4}-\d{2}-\d{2}$', label):
                processed_labels.append(label[5:])
            else:
                processed_labels.append(label)

        num_labels = len(exam_column_name)
        if num_labels > 12:
            step = max(1, num_labels // 12)
            display_indices = list(range(0, num_labels, step))
            display_labels = [processed_labels[i] for i in display_indices]
            display_positions = [x[i] for i in display_indices]
            plt.xticks(display_positions, display_labels, rotation=0, ha='center', fontsize=14)
        else:
            plt.xticks(x, processed_labels, rotation=0, ha='center', fontsize=14)

        plt.yticks(fontsize=12)
        plt.grid(axis='y', linestyle='--', alpha=0.2)
        plt.legend(loc='upper center', bbox_to_anchor=(0.5, 1.08), ncol=min(4, len(data_codes)),
                  fancybox=False, shadow=False, frameon=False, fontsize=14)
        plt.subplots_adjust(top=0.85, bottom=0.2, left=0.08, right=0.98)

        # 使用新的目录结构生成保存路径（包含指标名称）        
        save_path = get_png_save_path(
            element_code=element_code,
            test_type=test_type,
            time_stamp=year_month,
            rain_type=rain_type,
            metric=exam_name
        )
        
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        save_paths[exam_name] = str(save_path)

    return save_paths


def generate_charts(response_data):
    """从API响应数据生成检验图表

    Args:
        response_data: API响应数据（dict格式，即 response_json）

    Returns:
        list: 图表文件路径列表

    Example:
        >>> result = request_scores(json_data)
        >>> if result['success']:
        ...     charts = generate_charts(result['response_json'])
    """
    return _parse_and_plot(response_data)


def request_scores(json_data, topic=None):
    """发送检验请求并获取原始响应数据

    Args:
        json_data: 检验参数JSON
        topic: 消息队列主题，默认使用 Config.TOPIC_STATION

    Returns:
        dict: 包含以下键的字典:
            - success (bool): 请求是否成功
            - response_text (str): 原始响应文本
            - response_json (dict): 解析后的响应JSON
            - error (str): 错误信息（如果有）

    Example:
        >>> result = request_scores(json_data)
        >>> if result['success']:
        ...     data = result['response_json']
        ...     # 处理数据
        """
    rprint(json_data)

    json_base64 = base64.b64encode(json.dumps(json_data).encode('utf-8')).decode('utf-8')
    url = f"{Config.API_URL}?funname={Config.API_ENDPOINT}"

    if topic is None:
        topic = Config.TOPIC_STATION

    params_json = {
        "u": Config.USERNAME,
        "p": Config.PASSWORD,
        "params": {
            "topic": topic,
            "jsonData": json_base64,
            "timeOut": 0
        }
    }

    try:
        response = requests.post(url, json=params_json, timeout=Config.API_TIMEOUT)
        rprint(f"[cyan]响应内容: [/cyan]")
        rprint(response.text[:200] + '...')

        response_json = response.json()
        if response_json.get('code') == '0':
            return {
                'success': True,
                'response_text': response.text,
                'response_json': response_json,
                'error': None
            }
        else:
            return {
                'success': False,
                'response_text': response.text,
                'response_json': response_json,
                'error': f"API返回错误码: {response_json.get('code')}"
            }
    except Exception as e:
        return {
            'success': False,
            'response_text': None,
            'response_json': None,
            'error': f"请求出错: {str(e)}"
        }


def _apply_test_type_config(base_json: dict, test_type: str, time_session: int = 24) -> dict:
    """根据检验类型配置JSON参数
    
    Args:
        base_json: 基础JSON配置
        test_type: 检验方式 ('daily', 'time_session', 'area')
        time_session: 指定预报时效，默认24h（仅对test_type为'daily'和'area'生效），仅能传递一个值
        
    Returns:
        dict: 更新后的JSON配置
    """
    if test_type == 'daily':
        base_json.update({"timeSessions": f"{time_session}", "scoreType": "daily", "columnType": "daily"})
    elif test_type == 'time_session':
        base_json.update({"timeSessions": "all", "scoreType": "composite", "columnType": "timeSession"})
    elif test_type == 'area':
        base_json.update({"timeSessions": f"{time_session}", "statisTypes": "county", "columnType": "area"})
    else:
        raise ValueError("test_type 必须是 'daily', 'time_session' 或 'area'")
    return base_json


def create_rain_test_json(test_type, rain_type='ng', threshold=None, time_session=None, **kwargs):
    """创建降水检验的JSON数据

    Args:
        test_type: 检验方式 ('daily', 'time_session', 'area')
        rain_type: 降水检验类型 ('ng': 晴雨不分级, 'g': 分级, 'acc': 累计)
        threshold: 分级/累计检验的阈值（如 0.1, 10, 25, 50, 100）
        time_session: 预报时效 (仅对 test_type 为 daily 和 area 生效)
        **kwargs: 其他自定义参数，可覆盖默认配置

    支持的 kwargs 参数:
        beginTime (str): 开始时间，格式 'YYYY-MM-DD HH:MM:SS'
        endTime (str): 结束时间，格式 'YYYY-MM-DD HH:MM:SS'
        predictHours (str): 起报时间，如 '08','20','08,20' 或 'all'
        areaCodes (str): 区域代码，如 '120000'（天津）
        stations (str): 站点，如 'all' 或具体站点代码
        dataCodes (str): 数据产品代码，多个用逗号分隔
            - NAFP_ECTHIN_NC: ECMWF模式
            - NAFP_SCMOC_NC: 智能网格预报指导报(SCMOC)
            - NAFP_BETJ_DS_NC: 天津预报

    Returns:
        dict: 检验请求JSON数据
    """
    base_json = {
        "beginTime": Config.BEGIN_TIME,
        "endTime": Config.END_TIME,
        "predictHours": Config.PREDICT_HOURS,   # default 08,20
        "collectionCode": Config.COLLECTION_CODE,
        "areaCodes": Config.AREA_CODES,
        "stations": Config.STATIONS,
        "dataCodes": Config.DATA_CODES,
        # "guideMode": Config.GUIDE_MODE,
        "statisTypes": Config.STATIS_TYPES,
        "algorithmName": Config.RAIN_ALGORITHM_NAME,
        "elementCode": Config.RAIN_ELEMENT_CODE,
        "sampleFields": Config.SAMPLE_FIELDS,
        "algorithmType": rain_type
    }
    
    # 分级或累计检验需要指定阈值
    if rain_type in ('g', 'acc') and threshold is not None:
        base_json["algorithmArgs"] = str(threshold)

    _apply_test_type_config(base_json, test_type, time_session=time_session)

    base_json.update(kwargs)
    return base_json


def create_temp_test_json(test_type, time_session=None, **kwargs):
    """创建温度检验的JSON数据

    Args:
        test_type: 检验方式 ('daily', 'time_session', 'area')
        time_session: 预报时效 (仅对 test_type 为 daily 和 area 生效)
        **kwargs: 其他自定义参数，可覆盖默认配置

    支持的 kwargs 参数:
        beginTime (str): 开始时间，格式 'YYYY-MM-DD HH:MM:SS'
        endTime (str): 结束时间，格式 'YYYY-MM-DD HH:MM:SS'
        predictHours (str): 起报时间，如 '08','20','08,20' 或 'all'
        areaCodes (str): 区域代码，如 '120000'（天津）
        stations (str): 站点，如 'all' 或具体站点代码
        dataCodes (str): 数据产品代码，多个用逗号分隔
            - NAFP_ECTHIN_NC: ECMWF模式
            - NAFP_SCMOC_NC: 智能网格预报指导报(SCMOC)
            - NAFP_BETJ_DS_NC: 天津预报
    """
    base_json = {
        "beginTime": Config.BEGIN_TIME,
        "endTime": Config.END_TIME,
        "predictHours": Config.PREDICT_HOURS,   # default 08,20
        "collectionCode": Config.COLLECTION_CODE,
        "areaCodes": Config.AREA_CODES,
        "stations": Config.STATIONS,
        "dataCodes": Config.DATA_CODES,
        # "guideMode": Config.GUIDE_MODE,
        "statisTypes": Config.STATIS_TYPES,
        "algorithmName": Config.TEMP_ALGORITHM_NAME,
        "elementCode": Config.TEMP_ELEMENT_CODE,
        "sampleFields": Config.SAMPLE_FIELDS,
    }

    _apply_test_type_config(base_json, test_type, time_session=time_session)

    base_json.update(kwargs)
    return base_json

# ============ 便捷函数 ============

def run_rain_eva(test_type, rain_type='ng', threshold=None,
                 begin_time=None, end_time=None,
                 save_json=True, base_dir=None,
                 time_session=None, predict_hours=None,
                 area_codes=None):
    """运行降水检验并获取结果

    Args:
        test_type: 检验方式 ('daily', 'time_session', 'area')
        rain_type: 降水检验类型 ('ng': 晴雨不分级, 'g': 分级, 'acc': 累计)
        threshold: 分级/累计检验的阈值（如 0.1, 10, 25, 50, 100）
        begin_time: 开始时间 (如: '2026-03-01 00:00:00')，默认使用 Config.BEGIN_TIME
        end_time: 结束时间 (如: '2026-03-31 23:59:59')，默认使用 Config.END_TIME
        save_json: 是否保存JSON文件到目录结构，默认True
        base_dir: JSON保存基础目录，默认使用 Config.BASE_SAVE_DIR
        time_session: 预报时效 (仅对 test_type 为 daily 和 area 生效)
        predict_hours: 起报时间，如 '08','20','08,20'

    Returns:
        dict: 包含完整元数据、raw_response、analysis的统一格式
    """

    # 构建 API 请求参数
    api_kwargs = {}
    if begin_time is not None:
        api_kwargs['beginTime'] = begin_time
    if end_time is not None:
        api_kwargs['endTime'] = end_time
    if predict_hours is not None:
        api_kwargs['predictHours'] = predict_hours
    if area_codes is not None:
        api_kwargs['areaCodes'] = area_codes

    try:
        json_data = create_rain_test_json(test_type, rain_type, threshold, time_session=time_session, **api_kwargs)
        
        # 获取要素代码和名称
        element_code = json_data.get('elementCode', 'unknown')
        element_names = Config.RAIN_ELEMENTS
        element_name = element_names.get(element_code, element_code)
        
        # 检验方式名称
        test_type_names = Config.TEST_TYPE_NAMES
        test_type_name = test_type_names.get(test_type, test_type)
        
        # 生成时间范围描述
        begin = json_data.get('beginTime', '')
        end = json_data.get('endTime', '')

        # 从 API 响应中提取 year_month（用于文件路径）
        year_month = begin[:4] + begin[5:7] if begin else datetime.now().strftime('%Y%m')

        # 构建JSON文件路径
        filepath = get_json_save_path(
            element_code=element_code,
            test_type=test_type,
            year_month=year_month,
            rain_type=rain_type,
            base_dir=base_dir,
            predict_hours=predict_hours
        )

        if debug and os.path.exists(filepath):
            rprint(f"[yellow]JSON文件已存在: {filepath}[/yellow]")
            with open(filepath, 'r', encoding='utf-8') as f:
                result = json.load(f)   
        else:
            # 1. 发送请求获取数据
            request_result = request_scores(json_data)

            # 解析API响应为字典
            try:
                raw_response = request_result.get('response_json', {})
            except:
                raw_response = {"error": "解析失败"}

            # 构建统一格式的返回结果
            result = {
                "element": element_name,
                "element_code": element_code,
                "test_type": test_type_name,
                "test_type_code": test_type,
                "rain_type": rain_type,
                "predict_hours": predict_hours,
                "time_range": {
                    "begin": begin,
                    "end": end
                },
                "query_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "raw_response": raw_response,
                "request_success": request_result['success'],
            }

            # 保存JSON文件到目录结构
            if save_json and result['request_success']:
                try:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                    result['json_file'] = str(filepath)
                    rprint(f"[green]JSON已保存: {filepath}[/green]")
                except Exception as e:
                    rprint(f"[yellow]JSON保存失败: {e}[/yellow]")
        
        return result
    except Exception as e:
        return {"error": f"运行失败: {str(e)}"}


def run_temp_eva(test_type,
                 begin_time=None, end_time=None,
                 save_json=True, base_dir=None,
                 time_session=None, predict_hours=None,
                 area_codes=None):
    """运行温度检验并获取结果

    Args:
        test_type: 检验方式 ('daily', 'time_session', 'area')
        begin_time: 开始时间 (如: '2026-03-01 00:00:00')，默认使用 Config.BEGIN_TIME
        end_time: 结束时间 (如: '2026-03-31 23:59:59')，默认使用 Config.END_TIME
        save_json: 是否保存JSON文件到目录结构，默认True
        base_dir: JSON保存基础目录，默认使用 Config.BASE_SAVE_DIR
        time_session: 预报时效 (仅对 test_type 为 daily 和 area 生效)
        predict_hours: 起报时间，如 '08','20','08,20'

    Returns:
        dict: 包含完整元数据、raw_response、analysis的统一格式
    """

    # 构建 API 请求参数
    api_kwargs = {}
    if begin_time is not None:
        api_kwargs['beginTime'] = begin_time
    if end_time is not None:
        api_kwargs['endTime'] = end_time
    if predict_hours is not None:
        api_kwargs['predictHours'] = predict_hours

    try:
        json_data = create_temp_test_json(test_type, time_session=time_session, **api_kwargs)

        # 获取要素代码和名称
        element_code = json_data.get('elementCode', 'unknown')
        element_names = Config.TEMP_ELEMENTS
        element_name = element_names.get(element_code, element_code)

        # 检验方式名称
        test_type_names = Config.TEST_TYPE_NAMES
        test_type_name = test_type_names.get(test_type, test_type)

        # 生成时间范围描述
        begin = json_data.get('beginTime', '')
        end = json_data.get('endTime', '')

        # 从 API 响应中提取 year_month（用于文件路径）
        year_month = begin[:4] + begin[5:7] if begin else datetime.now().strftime('%Y%m')

        # 生成JSON文件路径
        filepath = get_json_save_path(
            element_code=element_code,
            test_type=test_type,
            year_month=year_month,
            rain_type=None,  # 温度数据无rain_type
            base_dir=base_dir,
            predict_hours=predict_hours
        )

        if debug and os.path.exists(filepath):
            rprint(f"[yellow]JSON文件已存在: {filepath}[/yellow]")
            with open(filepath, 'r', encoding='utf-8') as f:
                result = json.load(f)   
        else:
            # 1. 发送请求获取数据
            request_result = request_scores(json_data)

            # 解析API响应为字典
            try:
                raw_response = request_result.get('response_json', {})
            except:
                raw_response = {"error": "解析失败"}

            # 构建统一格式的返回结果
            result = {
                "element": element_name,
                "element_code": element_code,
                "test_type": test_type_name,
                "test_type_code": test_type,
                "predict_hours": predict_hours,
                "time_range": {
                    "begin": begin,
                    "end": end
                },
                "query_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "raw_response": raw_response,
                "request_success": request_result['success'],
            }

            # 保存JSON文件到目录结构
            if save_json and result['request_success']:
                try:
                    with open(filepath, 'w', encoding='utf-8') as f:
                        json.dump(result, f, ensure_ascii=False, indent=2)
                    result['json_file'] = str(filepath)
                    rprint(f"[green]JSON已保存: {filepath}[/green]")
                except Exception as e:
                    rprint(f"[yellow]JSON保存失败: {e}[/yellow]")
        
        return result
    except Exception as e:
        return {"error": f"运行失败: {str(e)}"}


def run_rain_eva_all_grades(test_type, **kwargs):
    """运行所有分级级别的降水检验

    Args:
        test_type: 检验方式 ('daily', 'time_session', 'area')
        **kwargs: 其他参数（beginTime, endTime等）

    支持的 kwargs 参数:
        begin_time (str): 开始时间，格式 'YYYY-MM-DD HH:MM:SS'
        end_time (str): 结束时间，格式 'YYYY-MM-DD HH:MM:SS'
        save_json (bool): 是否保存JSON文件，默认 True
        base_dir (str): JSON保存基础目录

    Returns:
        dict: 各分级级别的检验结果，包含:
            - ng: 晴雨不分级
            - g_light/g_moderate/g_heavy/g_storm/g_downpour: 各雨量等级
            - acc_0.1/acc_10/acc_25/acc_50/acc_100: 各累计阈值
    """
    results = {}
    
    # 1. 晴雨不分级检验
    rprint(f"\n[cyan]运行晴雨不分级检验 ({test_type})...[/cyan]")
    results['ng'] = run_rain_eva(test_type, rain_type='ng', **kwargs)
    
    # note：一次返回所有量级检验结果
    # 2. 分级检验（各雨量等级）
    rprint(f"\n[cyan]运行分级检验 ({test_type})...[/cyan]")
    results[f'g'] = run_rain_eva(test_type, rain_type='g', **kwargs)

    # 3. 累计检验（各阈值）
    rprint(f"\n[cyan]运行累计检验 ({test_type})...[/cyan]")
    results[f'acc'] = run_rain_eva(
        test_type, rain_type='acc', **kwargs
    )
    return results

# ============ 主函数 ============

# if __name__ == "__main__":
#     """
#     仅供演示用，支持不完整。请使用batch_download.py下载数据。
#     """

#     import argparse
#     parser = argparse.ArgumentParser(description="预报检验工具")
#     parser.add_argument("--debug", action="store_true", help="开启调试模式")
#     args = parser.parse_args()
#     debug = args.debug
#     if debug:
#         rprint("[bold]调试模式[/bold]")
#     else:
#         debug = False
#     rprint("[bold]预报检验工具[/bold]")

#     # rprint("\n[cyan]降水逐时效检验...[/cyan]")
#     # json_data = create_rain_test_json('time_session')
#     # request_scores(json_data)
#     # json_data = create_temp_test_json('time_session')
#     # request_scores(json_data)

#     rprint("\n[cyan]温度检验...[/cyan]")
#     run_temp_eva('time_session')
#     run_temp_eva('daily')
#     run_temp_eva('area')

#     rprint("\n[cyan]降水检验...[/cyan]")
#     run_rain_eva_all_grades('time_session')
#     run_rain_eva_all_grades('daily')
#     run_rain_eva_all_grades('area')
