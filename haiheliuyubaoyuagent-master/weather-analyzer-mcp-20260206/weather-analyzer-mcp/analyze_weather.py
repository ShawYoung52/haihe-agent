#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
独立气象分析脚本
直接调用 WeatherAnalyzer，避免 MCP 超时问题
支持命令行参数，可生成完整的气象分析报告
"""

import argparse
import configparser
from datetime import datetime
from weather_analyzer import WeatherAnalyzer
import os


def format_time(time_str):
    """格式化时间字符串"""
    try:
        time_obj = datetime.strptime(time_str, '%Y%m%d%H%M%S')
        return time_obj.strftime("%Y年%m月%d日%H时")
    except:
        return time_str


def generate_report(time_str, levels=['500hPa', '700hPa', '850hPa'], save_report=False):
    """
    生成完整的气象分析报告
    
    参数:
        time_str: 时间字符串 (YYYYMMDDHHMISS)
        levels: 要生成的层面列表
        save_report: 是否保存报告为 Markdown 文件
    """
    print("="*70)
    print("🌤️  气象分析脚本")
    print("="*70)
    
    # 加载配置
    config = configparser.ConfigParser()
    config.read('config.ini', encoding='utf-8')
    analyzer = WeatherAnalyzer(config)
    
    # 如果时间为空，获取最新时间
    if not time_str:
        print("\n📅 获取最新气象数据时间...")
        time_str = analyzer.get_latest_time()
        if not time_str:
            print("❌ 未找到可用的气象数据")
            return
        print(f"✅ 最新时间: {format_time(time_str)}")
    else:
        print(f"\n📅 分析时间: {format_time(time_str)}")
    
    # 生成报告
    report = []
    report.append(f"# 气象分析报告 - {format_time(time_str)}\n")
    report.append(f"生成时间: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}\n")
    report.append("---\n")
    
    all_charts = {}
    
    # 逐层面生成
    for level in levels:
        print(f"\n{'='*70}")
        print(f"📊 生成 {level} 图表...")
        print('='*70)
        
        try:
            result = analyzer.generate_charts(time_str, level=level)
            
            # 显示结果
            if result['status'] == 'success':
                print(f"✅ 状态: 成功")
                print(f"✅ 消息: {result['message']}")
            elif result['status'] == 'partial':
                print(f"⚠️  状态: 部分成功")
                print(f"⚠️  消息: {result['message']}")
            else:
                print(f"❌ 状态: 失败")
                print(f"❌ 消息: {result['message']}")
            
            # 显示生成的图表
            if result['charts']:
                print(f"\n生成的图表:")
                chart_names = {
                    'height_field': '位势高度场',
                    'wind_barb': '风羽图',
                    'dew_point': '露点温度'
                }
                for chart_type, path in result['charts'].items():
                    print(f"  - {chart_names.get(chart_type, chart_type)}: {path}")
                    all_charts[f"{level}_{chart_type}"] = path
                
                # 添加到报告
                report.append(f"\n## {level}\n")
                report.append(f"**状态**: {result['status']}\n")
                report.append(f"**消息**: {result['message']}\n")
                report.append(f"\n### 生成的图表\n")
                for chart_type, path in result['charts'].items():
                    chart_name = chart_names.get(chart_type, chart_type)
                    report.append(f"- **{chart_name}**: `{path}`\n")
            else:
                print(f"\n❌ 没有生成任何图表")
                report.append(f"\n## {level}\n")
                report.append(f"**状态**: 失败\n")
                report.append(f"**消息**: {result['message']}\n")
        
        except Exception as e:
            print(f"\n❌ 生成失败: {str(e)}")
            report.append(f"\n## {level}\n")
            report.append(f"**状态**: 错误\n")
            report.append(f"**错误信息**: {str(e)}\n")
    
    # 总结
    print(f"\n{'='*70}")
    print("📋 总结")
    print('='*70)
    print(f"总共生成 {len(all_charts)} 个图表:")
    for key, path in all_charts.items():
        print(f"  ✅ {key}")
    
    # 保存报告
    if save_report:
        time_obj = datetime.strptime(time_str, '%Y%m%d%H%M%S')
        report_filename = f"气象分析报告_{time_obj.strftime('%Y%m%d%H')}.md"
        
        # 添加图表路径总结
        report.append("\n---\n")
        report.append("\n## 图表路径汇总\n")
        for key, path in all_charts.items():
            report.append(f"- `{key}`: {path}\n")
        
        # 添加使用说明
        report.append("\n---\n")
        report.append("\n## 使用说明\n")
        report.append("1. 使用图片查看器打开上述图片文件\n")
        report.append("2. 根据《气象分析词.txt》中的规则进行分析\n")
        report.append("3. 重点关注京津冀地区（112°E–120°E、34°N–40°N）\n")
        
        with open(report_filename, 'w', encoding='utf-8') as f:
            f.writelines(report)
        
        print(f"\n📄 报告已保存: {report_filename}")
    
    print(f"\n{'='*70}")
    print("✅ 完成！")
    print('='*70)
    
    return all_charts


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description='气象分析脚本 - 生成位势高度场、风羽图、露点温度图',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 使用最新时间生成所有层面
  python analyze_weather.py
  
  # 指定时间生成
  python analyze_weather.py -t 20250820200000
  
  # 只生成 500hPa
  python analyze_weather.py -l 500hPa
  
  # 生成多个层面并保存报告
  python analyze_weather.py -l 500hPa 850hPa -s
  
  # 使用最新时间并保存报告
  python analyze_weather.py -s
        """
    )
    
    parser.add_argument(
        '-t', '--time',
        type=str,
        default='',
        help='气象数据时间 (格式: YYYYMMDDHHMISS)，留空则使用最新时间'
    )
    
    parser.add_argument(
        '-l', '--levels',
        nargs='+',
        default=['500hPa', '700hPa', '850hPa'],
        choices=['500hPa', '700hPa', '850hPa'],
        help='要生成的层面 (默认: 所有层面)'
    )
    
    parser.add_argument(
        '-s', '--save',
        action='store_true',
        help='保存分析报告为 Markdown 文件'
    )
    
    args = parser.parse_args()
    
    try:
        generate_report(args.time, args.levels, args.save)
    except KeyboardInterrupt:
        print("\n\n⚠️  已取消")
    except Exception as e:
        print(f"\n\n❌ 错误: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
