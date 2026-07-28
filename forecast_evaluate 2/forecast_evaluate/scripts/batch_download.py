# -*- coding: utf-8 -*-
"""
批量下载预报检验数据脚本
支持温度检验和降水检验（包括ng、g、acc三种类型）的批量下载

新目录结构: {base_dir}/{element}/{test_type}/{subtype}/{YYYYMM}.json
"""

import sys
from pathlib import Path
from datetime import datetime
from rich import print as rprint

# 添加当前目录到路径
_current_dir = Path(__file__).parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from forecast_evaluate import run_temp_eva, run_rain_eva
from config import Config


def _download_all_temp_data(save_dir: str = None, time_session: int = None, predict_hours: str = None):
    """
    批量下载所有温度检验数据

    覆盖要素: t2m, tmax24, tmin24
    覆盖检验方式: daily(逐日), time_session(逐时效), area(分地区)

    目录结构: {save_dir}/{element}/{test_type}/default/{YYYYMM}.json

    Args:
        save_dir: 数据保存目录，默认使用 Config.BASE_SAVE_DIR
        time_session: 预报时效 (仅对 test_type 为 daily 和 area 生效)
        predict_hours: 起报时间，如 '08','20','08,20'

    Returns:
        dict: 下载结果统计
    """
    if save_dir is None:
        save_dir = Config.BASE_SAVE_DIR

    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    results = {
        'total': 0,
        'success': 0,
        'failed': 0,
        'files': []
    }

    rprint(f"\n[bold cyan]开始批量下载温度检验数据...[/bold cyan]")
    rprint(f"基础目录: {save_dir}")
    
    for element_code, _ in Config.TEMP_ELEMENTS.items():
        for test_type in list(Config.TEST_TYPE_NAMES.keys()):
            results['total'] += 1
            
            success = download_single_temp(element_code, test_type, time_session=time_session, predict_hours=predict_hours)
            if success:
                results['success'] += 1
            else:
                results['failed'] += 1
    
    # 输出统计
    rprint(f"\n[bold]温度检验下载完成:[/bold]")
    rprint(f"  总计: {results['total']}")
    rprint(f"  成功: {results['success']}")
    rprint(f"  失败: {results['failed']}")
    
    return results


def download_single_temp(element_code: str, test_type: str, time_session: int = None, predict_hours: str = None) -> bool:
    """
    下载单个温度检验数据
    
    Args:
        element_code: 要素代码
        test_type: 检验类型
        time_session: 预报时效 (仅对 test_type 为 daily 和 area 生效)
        predict_hours: 起报时间，如 '08','20','08,20'
        
    Returns:
        bool: 是否成功
    """
    rprint(f"\n[cyan]下载 {element_code} - {test_type}...[/cyan]")
    
    original_element = Config.TEMP_ELEMENT_CODE
    Config.TEMP_ELEMENT_CODE = element_code
    
    try:
        result = run_temp_eva(test_type, time_session=time_session, predict_hours=predict_hours)
        if 'request_success' in result and not result['request_success']:
            rprint(f"[red]失败: {result.get('raw_response', result)}[/red]")
            return False
        elif 'error' in result:
            rprint(f"[red]错误: {result['error']}[/red]")
            return False
        return True
    except Exception as e:
        rprint(f"[red]异常: {str(e)}, result={result}[/red]")
        return False
    finally:
        Config.TEMP_ELEMENT_CODE = original_element


def _download_all_rain_data(save_dir: str = None, time_session: int = None, predict_hours: str = None):
    """
    批量下载所有降水检验数据

    覆盖要素: rain, rain3, rain12, rain24
    覆盖检验方式: daily(逐日), time_session(逐时效), area(分地区)
    覆盖检验类型: ng(晴雨不分级), g(分级), acc(累计)

    目录结构: {save_dir}/{element}/{test_type}/{subtype}/{YYYYMM}.json
    - subtype: ng, g0.1, g10, g25, g50, g100, acc0.1, acc10, acc25, acc50, acc100

    Args:
        save_dir: 数据保存目录，默认使用 Config.BASE_SAVE_DIR
        time_session: 预报时效 (仅对 test_type 为 daily 和 area 生效)
        predict_hours: 起报时间，如 '08','20','08,20'

    Returns:
        dict: 下载结果统计
    """
    if save_dir is None:
        save_dir = Config.BASE_SAVE_DIR

    save_dir = Path(save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)

    results = {
        'total': 0,
        'success': 0,
        'failed': 0,
        'files': []
    }

    rprint(f"\n[bold cyan]开始批量下载降水检验数据...[/bold cyan]")
    rprint(f"基础目录: {save_dir}")
    
    for element_code, _ in Config.RAIN_ELEMENTS.items():
        for test_type in list(Config.TEST_TYPE_NAMES.keys()):
            for rain_type in Config.RAIN_TYPES_ALL:
                results['total'] += 1
                
                success = download_single_rain(element_code, test_type, rain_type, time_session=time_session, predict_hours=predict_hours)
                if success:
                    results['success'] += 1
                else:
                    results['failed'] += 1
    
    # 输出统计
    rprint(f"\n[bold]降水检验下载完成:[/bold]")
    rprint(f"  总计: {results['total']}")
    rprint(f"  成功: {results['success']}")
    rprint(f"  失败: {results['failed']}")
    
    return results


def download_single_rain(element_code: str, test_type: str, rain_type: str, time_session: int = None, predict_hours: str = None) -> bool:
    """
    下载单个降水检验数据
    
    Args:
        element_code: 要素代码
        test_type: 检验类型
        rain_type: 降水检验类型 (ng/g/acc)
        time_session: 预报时效 (仅对 test_type 为 daily 和 area 生效)
        predict_hours: 起报时间，如 '08','20','08,20'
        
    Returns:
        bool: 是否成功
    """
    rprint(f"\n[cyan]下载 {element_code} - {test_type} - {rain_type}...[/cyan]")
    
    original_element = Config.RAIN_ELEMENT_CODE
    Config.RAIN_ELEMENT_CODE = element_code
    
    try:
        result = run_rain_eva(test_type, rain_type=rain_type, time_session=time_session, predict_hours=predict_hours)
        if 'request_success' in result and not result['request_success']:
            rprint(f"[red]失败: {result.get('raw_response', result)}[/red]")
            return False
        elif 'error' in result:
            rprint(f"[red]错误: {result['error']}[/red]")
            return False
        return True
    except Exception as e:
        rprint(f"[red]异常: {str(e)}[/red]")
        return False
    finally:
        Config.RAIN_ELEMENT_CODE = original_element


def download_all_forecast_data(save_dir: str = None, time_session: int = None, predict_hours: str = None):
    """
    批量下载所有预报检验数据（温度+降水）

    目录结构: {save_dir}/{element}/{test_type}/{subtype}/{YYYYMM}.json

    Args:
        save_dir: 数据保存目录，默认使用 Config.BASE_SAVE_DIR
        time_session: 预报时效 (仅对 test_type 为 daily 和 area 生效)
        predict_hours: 起报时间，如 '08','20','08,20'

    Returns:
        dict: 下载结果统计
    """
    rprint(f"\n[bold green]{'='*60}[/bold green]")
    rprint(f"[bold green]批量下载预报检验数据[/bold green]")
    rprint(f"[bold green]{'='*60}[/bold green]")
    rprint(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 下载温度数据
    temp_results = _download_all_temp_data(save_dir, time_session=time_session, predict_hours=predict_hours)

    # 下载降水数据
    rain_results = _download_all_rain_data(save_dir, time_session=time_session, predict_hours=predict_hours)
    
    # 汇总统计
    total_results = {
        'temp': temp_results,
        'rain': rain_results,
        'total_files': temp_results['success'] + rain_results['success'],
        'total_failed': temp_results['failed'] + rain_results['failed'],
        'all_files': temp_results['files'] + rain_results['files']
    }
    
    rprint(f"\n[bold green]{'='*60}[/bold green]")
    rprint(f"[bold green]批量下载完成[/bold green]")
    rprint(f"[bold green]{'='*60}[/bold green]")
    rprint(f"结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    rprint(f"\n统计汇总:")
    rprint(f"  温度检验: {temp_results['success']}/{temp_results['total']}")
    rprint(f"  降水检验: {rain_results['success']}/{rain_results['total']}")
    rprint(f"  总计: {total_results['total_files']}/{temp_results['total'] + rain_results['total']}")
    
    return total_results


# ============ 快捷函数 ============

def download_temp_only(save_dir: str = None, time_session: int = None, predict_hours: str = None):
    """仅下载温度检验数据"""
    return _download_all_temp_data(save_dir, time_session=time_session, predict_hours=predict_hours)


def download_rain_only(save_dir: str = None, time_session: int = None, predict_hours: str = None):
    """仅下载降水检验数据"""
    return _download_all_rain_data(save_dir, time_session=time_session, predict_hours=predict_hours)


# ============ 主函数 ============

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='批量下载当前月份预报检验数据')
    parser.add_argument('--save-dir', type=str, default=None,
                       help='数据保存目录')
    parser.add_argument('--begin-time', type=str, default=None,
                       help='开始时间 (格式: YYYYMMDD)')
    parser.add_argument('--end-time', type=str, default=None,
                       help='结束时间 (格式: YYYYMMDD)')
    
    # 下载类型参数
    parser.add_argument('--type', choices=['all', 'temp', 'rain', 'single'], default='all',
                       help='下载类型: all(全部), temp(仅温度), rain(仅降水), single(单个，需指定 --element 和 --test-type，如 --type single --element tmax24 --test-type daily)')
    # single 模式参数
    parser.add_argument('--element', type=str, default=None, 
                       choices=list(Config.ALL_ELEMENTS.keys()),
                       help='要素代码 (single模式使用，如 tmax24, rain24)')
    parser.add_argument('--test-type', type=str, default=None,
                       choices=list(Config.TEST_TYPE_NAMES.keys()),
                       help='检验类型 (single模式使用，如 daily, time_session, area)')
    parser.add_argument('--rain-type', type=str, default='ng',
                       choices=['ng', 'g', 'acc'],
                       help='降水检验类型 (single模式使用，仅降水，如 ng（晴雨不分级）, g（分级降水）, acc（累计降水）)')
    parser.add_argument('--time-session', type=int, default=24,
                       help='预报时效 (仅对 test_type 为 daily 和 area 生效，如 24, 48, 72)')
    parser.add_argument('--predict-hours', type=str, default='08,20',
                       help='起报时间，如 08（早08点）、20（晚20点）、08,20（默认，早晚两次）')

    args = parser.parse_args()
    
    # 解析时间范围
    if args.begin_time and args.end_time:
        if len(args.begin_time) != 8 or len(args.end_time) != 8:
            print("错误: 时间格式应为 YYYYMMDD（如 20260401）")
            exit(1)
        if not (args.begin_time.isdigit() and args.end_time.isdigit()):
            print("错误: 时间必须是8位数字")
            exit(1)
        begin_formatted = f"{args.begin_time[:4]}-{args.begin_time[4:6]}-{args.begin_time[6:8]} 00:00:00"
        end_formatted = f"{args.end_time[:4]}-{args.end_time[4:6]}-{args.end_time[6:8]} 23:59:59"
        Config.BEGIN_TIME = begin_formatted
        Config.END_TIME = end_formatted
        print(f"下载时间范围: {begin_formatted} ~ {end_formatted}")
    elif args.begin_time or args.end_time:
        print("警告: 请同时指定 begin-time 和 end-time，否则将使用默认值")

    if args.type == 'single':
        if not args.element or not args.test_type:
            print("错误: single模式需要指定 --element 和 --test-type")
            exit(1)
        
        element_code = args.element        
        if element_code in Config.TEMP_ELEMENTS:
            download_single_temp(element_code, args.test_type, time_session=args.time_session, predict_hours=args.predict_hours)
        elif element_code in Config.RAIN_ELEMENTS:
            download_single_rain(element_code, args.test_type, args.rain_type, time_session=args.time_session, predict_hours=args.predict_hours)
        else:
            print(f"错误: 未知的要素代码 {element_code}")
            exit(1)

    elif args.type == 'temp':
        # 仅下载温度
        result = download_temp_only(args.save_dir, time_session=args.time_session, predict_hours=args.predict_hours)

    elif args.type == 'rain':
        # 仅下载降水
        result = download_rain_only(args.save_dir, time_session=args.time_session, predict_hours=args.predict_hours)

    else:
        # 下载全部
        result = download_all_forecast_data(args.save_dir, time_session=args.time_session, predict_hours=args.predict_hours)
