# -*- coding: utf-8 -*-
"""
预报检验数据分析模块
提供检验结果解析、统计分析和综述文字生成功能
"""

import json
import re
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from rich import print

from config import Config, PathConfig
from forecast_evaluate import generate_charts


@dataclass
class ExamResult:
    """检验结果数据类"""
    dataCode: str
    examName: str
    elementCode: str
    values: List[float]
    average: float
    timeSession: int


class ForecastAnalyzer:
    """预报检验分析器"""

    # 较差定义标准 — 从 analyzer_analyze.py 融合
    THRESHOLDS = {
        "area": {
            "daily": {
                "temperature": {
                    "accuracy": 80.0, "mae": 1.5, "me": 1.0,
                },
                "precipitation": {
                    "accuracy": 85.0, "ts": "mean",
                    "bias_low": 0.6, "bias_high": 1.4,
                },
            },
        },
        "time_session": {
            "le_72h": {
                "temperature": {
                    "accuracy": 80.0, "mae": 1.5, "me": 1.0,
                },
                "precipitation": {
                    "accuracy": 85.0, "ts": "mean",
                    "bias_low": 0.6, "bias_high": 1.4,
                },
            },
            "gt_72h": {
                "temperature": {
                    "accuracy": 70.0, "mae": 3.0, "me": 1.5,
                },
                "precipitation": {
                    "accuracy": 70.0, "ts": "mean",
                    "bias_low": 0.3, "bias_high": 2.0,
                },
            },
        },
    }

    def __init__(self, response_text: str):
        """初始化分析器
        
        Args:
            response_text: API返回的JSON字符串
        """
        self.raw_data = response_text["raw_response"]
        self.exam_data = self.raw_data.get('data', {}).get('examData', [])
        self.exam_columns = self.raw_data.get('data', {}).get('examColumnName', [])
        #
        metadata_copy = dict(response_text)
        metadata_copy.pop('raw_response', None)
        self.metadata = metadata_copy
        self.json_data = response_text
        
    def parse_results(self) -> Dict[str, List[ExamResult]]:
        """解析检验结果
        
        Returns:
            Dict: 按exam_name分组的结果字典
        """
        results = {}
        
        for item in self.exam_data:
            examName = item.get('examName', '未知指标')
            elementCode = item.get('elementCode', '未知要素')
            timeSession = item.get('timeSession', 0)
            dataCode = item.get('dataCode', '未知预报')
            
            # 转换值为浮点数，None和999999转为np.nan
            values = []
            for v in item.get('values', []):
                if v is None or v == 'null' or v == 999999 or v == '999999':
                    values.append(np.nan)
                else:
                    try:
                        val = float(v)
                        # 999999也是缺测标志
                        if val == 999999:
                            values.append(np.nan)
                        else:
                            values.append(val)
                    except (ValueError, TypeError):
                        values.append(np.nan)
            
            # 计算平均值（排除nan）
            valid_values = [v for v in values if not np.isnan(v)]
            avg_value = np.mean(valid_values) if valid_values else np.nan
            
            result = ExamResult(
                dataCode=dataCode,
                examName=examName,
                elementCode=elementCode,
                values=values,
                average=round(avg_value, 2),
                timeSession=int(timeSession),
            )
            
            if examName not in results:
                results[examName] = []
            results[examName].append(result)
                
        return results
    
    def _get_sort_params(self, exam_name: str) -> Tuple[bool, float, bool]:
        """获取排序参数
        
        Args:
            exam_name: 指标名称
            
        Returns:
            Tuple[use_abs, abs_offset, higher_is_better]
        """
        if 'MAE' in exam_name:
            return (False, 0.0, False)
        elif 'ME' in exam_name:
            return (True, 0.0, False)
        elif 'BIAS' in exam_name:
            return (True, 1.0, False)
        else:
            return (False, 0.0, True)
    
    def _parse_metric_name(self, examName: str, element_desc: str) -> Tuple[str, str]:
        """解析指标名称，返回分类和指标中文名
        
        Args:
            examName: 指标名称（如 MAE, TS_g:2）
            element_desc: 要素描述
            
        Returns:
            Tuple[category, metric_name]: 分类名称和指标中文名
        """
        metric_map = {'PC': '准确率', 'TS': 'TS评分', 'BIAS': '偏差'}
        
        if '_' in examName and ':' in examName:
            rain_key = examName.split('_', 1)[1]
            category = Config.RAIN_SUBTYPE_NAMES.get(rain_key, rain_key)
            metric_key = examName.split('_')[0]
            metric_name = metric_map.get(metric_key, metric_key)
        else:
            category = element_desc
            if category in Config.EXAM_DESCRIPTIONS:
                metric_name = Config.EXAM_DESCRIPTIONS[category].get(examName, examName)
            else:
                metric_name = Config.EXAM_DESCRIPTIONS.get(examName, examName)
        
        return category, metric_name

    def _get_thresholds(self, element_type: str, test_type: str,
                        time_session: int = 0) -> dict:
        """获取当前检验场景的较差判定阈值。

        Args:
            element_type: 'temperature' | 'precipitation'
            test_type: 'daily' | 'time_session' | 'area'
            time_session: 预报时效小时数（逐时效场景用于分 ≤72h vs >72h）
        """
        if test_type in ("area", "daily"):
            threshold_dict = self.THRESHOLDS.get("area", {})
            dimension_key = "daily"  # area 和 daily 共用通用标准
        else:
            threshold_dict = self.THRESHOLDS.get("time_session", {})
            if time_session > 72:
                dimension_key = "gt_72h"
            else:
                dimension_key = "le_72h"

        thresholds_section = threshold_dict.get(dimension_key, {})
        return thresholds_section.get(element_type, {})

    def rank_products(self, results: List[ExamResult]) -> List[Tuple[str, float]]:
        """对产品进行排名（根据exam_name自动判断排序方向）
        
        Args:
            results: 检验结果列表
            
        Returns:
            List: 排名列表 [(产品代码, 平均值), ...]
        """
        if not results:
            return []
        
        examName = results[0].examName
        use_abs, abs_offset, higher_is_better = self._get_sort_params(examName)
        
        # 过滤掉无效值
        valid_results = [(r.dataCode, r.average) for r in results if not np.isnan(r.average)]
        
        # 排序
        if use_abs:
            ranked = sorted(valid_results, key=lambda x: abs(x[1] - abs_offset), reverse=higher_is_better)
        else:
            ranked = sorted(valid_results, key=lambda x: x[1], reverse=higher_is_better)
        return ranked
    
    def format_ranking(self, ranked: List[Tuple[str, float]]) -> str:
        """格式化排名结果
        
        Args:
            ranked: 排名列表
            
        Returns:
            str: 格式化后的排名字符串
        """
        if not ranked:
            return "暂无有效数据"
        
        parts = []
        for i, (code, value) in enumerate(ranked):
            product_name = Config.PRODUCT_NAMES.get(code, code)
            
            if i == 0:
                parts.append(f"{product_name}({value:.2f})")
            else:
                prev_value = ranked[i-1][1]
                if abs(value - prev_value) < 0.01:
                    parts.append(f" ≈ {product_name}({value:.2f})")
                else:
                    parts.append(f" > {product_name}({value:.2f})")
        
        return ''.join(parts)

    def _find_poor_samples(self, results: dict, test_type: str,
                           element_type: str) -> list[dict]:
        """识别天津预报在各维度下的较差样本。

        Args:
            results: parse_results() 返回值
            test_type: daily | time_session | area
            element_type: temperature | precipitation
        Returns:
            [{name, metric, tj_value, threshold, reason}, ...]
        """
        poor = []
        if element_type != "temperature":
            return poor  # 降水较差判定后续补充

        for exam_name, exam_results in results.items():
            for r in exam_results:
                if r.dataCode != 'NAFP_BETJ_DS_NC':
                    continue
                time_session = getattr(r, 'timeSession', 0) or 0
                thresh = self._get_thresholds(element_type, test_type, time_session)

                # 遍历每个列（区域/日期/时效）
                for col_idx, col_name in enumerate(self.exam_columns):
                    val = r.values[col_idx] if col_idx < len(r.values) else None
                    if val is None or (isinstance(val, float) and np.isnan(val)):
                        continue

                    # 判断指标类型并检查阈值
                    if 'MAE' in exam_name:
                        if val > thresh.get('mae', 1.5):
                            poor.append({
                                'name': col_name, 'metric': 'MAE',
                                'tj_value': round(val, 2),
                                'threshold': thresh.get('mae', 1.5),
                                'reason': f"MAE {val:.2f} > {thresh.get('mae', 1.5)}",
                            })
                    elif 'ME' in exam_name:
                        if abs(val) >= thresh.get('me', 1.0):
                            poor.append({
                                'name': col_name, 'metric': 'ME',
                                'tj_value': round(val, 2),
                                'threshold': thresh.get('me', 1.0),
                                'reason': f"|ME| {abs(val):.2f} >= {thresh.get('me', 1.0)}",
                            })
                    elif any(k in exam_name for k in ('PC', '准确率')):
                        if val < thresh.get('accuracy', 80):
                            poor.append({
                                'name': col_name, 'metric': '准确率',
                                'tj_value': round(val, 2),
                                'threshold': thresh.get('accuracy', 80),
                                'reason': f"准确率 {val:.2f}% < {thresh.get('accuracy', 80)}%",
                            })
        return poor

    def generate_summary(self, report: Dict = None) -> str:
        """生成综述文字
        
        Args:
            report: 可选的详细报告数据（嵌套字典结构）
                    
        Returns:
            str: 综述文字
        """
        columns = report.get('columns', [])
        test_type = report.get('test_type_code', '')
        time_range = report.get('time_range')
        begin_time = time_range.get('begin', '')
        end_time = time_range.get('end', '')
        today = datetime.now().strftime('%Y-%m-%d')
        if end_time[:10] > today:
            end_time = today + end_time[10:]
        time_range = begin_time[:10].replace('-', '') + ' 至 ' + end_time[:10].replace('-', '')

        if test_type == 'area':
            columns = [Config.TJ_AREA_NAMES.get(str(col), str(col)) for col in columns]
        
        summaries = [time_range]
        for category, sub_details in report['details'].items():
            if isinstance(sub_details, dict):
                sub_summary = f"{category}：\n"
                for metric_name, data in sub_details.items():
                    ranked = data.get('ranking', [])
                    raw_data = data.get('raw_data', [])
                    
                    if ranked:
                        ranking_str = self.format_ranking(ranked)
                        sub_summary += f"{metric_name}表现为{ranking_str}。"
                    
                    if raw_data:
                        data = np.array([columns] + [item['values'] for item in raw_data]).T
                        df = pd.DataFrame(data, columns=[test_type] + [item['dataCode'] for item in raw_data])
                        sub_summary += f"{metric_name}原始数据：\n{df.to_csv(sep=',', index=False)}"
                #
                summaries.append(sub_summary)   
        return '\n'.join(summaries) if summaries else "暂无检验数据"
    
    def generate_detailed_report(self, *, include_charts: bool = True) -> Dict:
        """生成详细报告

        Args:
            include_charts: 是否生成图表文件。文字查询应设为 False，避免无用的
                Matplotlib 绘图与磁盘写入；命令行等既有调用默认保持 True。
        
        Returns:
            Dict: 包含综述、表格数据、统计信息的字典
        """
        image_paths = generate_charts(self.json_data) if include_charts else {}
        results = self.parse_results()
        
        time_sessions = set()
        for exam_results in results.values():
            for r in exam_results:
                if hasattr(r, 'timeSession') and r.timeSession:
                    time_sessions.add(r.timeSession)
        
        report = self.metadata | {
            'columns': self.exam_columns,
            'timeSessions': sorted(time_sessions) if time_sessions else None,
            'details': {}
        }
        
        for examName, exam_results in results.items():
            elementCode = exam_results[0].elementCode if exam_results else '未知'
            element_desc = Config.ALL_ELEMENTS.get(elementCode, elementCode)
            
            category, metric_name = self._parse_metric_name(examName, element_desc)
            
            ranked = self.rank_products(exam_results)
            
            exam_data = {
                'element': element_desc,
                'examName': examName,
                'ranking': [(Config.PRODUCT_NAMES.get(c, c), round(v, 2)) for c, v in ranked],
                'image_path': image_paths.get(examName) if image_paths else None,
                'raw_data': [
                    {
                        'dataCode': Config.PRODUCT_NAMES.get(r.dataCode, r.dataCode),
                        'timeSession': r.timeSession,
                        'values': [round(v, 2) if not np.isnan(v) else None for v in r.values],
                        'average': round(r.average, 2) if not np.isnan(r.average) else None,
                    }
                    for r in exam_results
                ]
            }
            
            if category not in report['details']:
                report['details'][category] = {}
            report['details'][category][metric_name] = exam_data

        # 识别较差样本
        # 明确获取 element_code，避免依赖 for 循环闭包变量
        element_code = self.metadata.get('element_code', '')
        if not element_code:
            # 回退：从 for 循环最后一个 elementCode 获取（results 非空时有效）
            element_code = elementCode if results else ''
        element_type = 'temperature' if element_code in Config.TEMP_ELEMENTS else 'precipitation'
        test_type_code = self.metadata.get('test_type_code', '')
        report['poor_samples'] = self._find_poor_samples(results, test_type_code, element_type)

        report['summary'] = self.generate_summary(report)
        return report
    
    def format_report_to_markdown(self, report: Dict) -> str:
        """将报告转换为Markdown格式
        
        Args:
            report: 详细报告字典（包含 details 中的 image_path）
            
        Returns:
            str: Markdown格式的报告
        """
        lines = []
        
        element = report.get('element', '')
        test_type = report.get('test_type', '')
        
        lines.append(f"# {element}预报检验")
        lines.append("")
        lines.append(f"**检验类型**: {test_type}")
        
        time_range = report.get('time_range', {})
        if time_range:
            begin = time_range.get('begin', '')
            end = time_range.get('end', '')
            lines.append(f"**时间范围**: {begin} ~ {end}")
        
        time_sessions = report.get('timeSessions')
        if time_sessions:
            lines.append(f"**预报时效**: {', '.join(str(ts) for ts in time_sessions)}")
        
        lines.append("")
        lines.append("## 综述")
        lines.append("")
        
        summary = report.get('summary', '')
        if summary:
            lines.append(summary)
                
        lines.append("")
        lines.append("## 详细结果")
        lines.append("")
        
        columns = report.get('columns', [])
        details = report.get('details', {})
        
        for category, sub_details in details.items():
            lines.append(f"### {category}")
            lines.append("")
            
            # 判断是否为嵌套字典
            if isinstance(sub_details, dict):
                # 嵌套结构：遍历子项
                for metric_name, data in sub_details.items():
                    lines.append(f"#### {metric_name}")
                    lines.append("")
                    
                    ranking = data.get('ranking', [])
                    if ranking:
                        lines.append("**排名**:")
                        for i, (product, value) in enumerate(ranking, 1):
                            lines.append(f"{i}. {product}: {value}")
                        lines.append("")
                    
                    img_path = data.get('image_path')
                    if img_path:
                        lines.append(f"![{Path(img_path).name}]({img_path})")
                        lines.append("")
                    
                    raw_data = data.get('raw_data', [])
                    exam_name = data.get('examName', '')
                    if raw_data and columns:
                        lines.extend(self._format_markdown_table(columns, raw_data, test_type, exam_name))
                        lines.append("")

        return '\n'.join(lines)
    
    def _format_column_name(self, col, test_type: str) -> str:
        """格式化列名
        
        Args:
            col: 列名
            test_type: 测试类型
            
        Returns:
            str: 格式化后的列名
        """
        c_str = str(col)
        if test_type == '分地区':
            return Config.TJ_AREA_NAMES.get(c_str, c_str)
        
        match = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', c_str)
        if match:
            month, day = int(match.group(2)), int(match.group(3))
            return f"{month}月{day}日"
        return c_str
    
    def _format_value(self, v) -> str:
        """格式化数值
        
        Args:
            v: 数值
            
        Returns:
            str: 格式化后的字符串
        """
        if v is None:
            return '-'
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)
    
    def _format_markdown_table(self, columns: List, raw_data: List, test_type: str, exam_name: str = '') -> List[str]:
        """生成Markdown表格
        
        Args:
            columns: 列名列表
            raw_data: 原始数据
            test_type: 测试类型
            exam_name: 指标名称，用于判断排序方向
            
        Returns:
            List[str]: 表格行列表
        """
        lines = []
        processed_columns = [self._format_column_name(c, test_type) for c in columns]
        
        lines.append("| 产品 | " + " | ".join(processed_columns) + " | 平均 |")
        lines.append("| --- " + " | --- " * len(columns) + " | --- |")
        
        # 获取排序参数（与rank_products逻辑一致）
        use_abs, abs_offset, higher_is_better = self._get_sort_params(exam_name)
        
        # 找出每列的最佳值
        best_values = {}
        best_original_values = {}
        for col_idx in range(len(columns)):
            col_values = []
            for item in raw_data:
                values = item.get('values', [])
                if col_idx < len(values) and values[col_idx] is not None:
                    v = values[col_idx]
                    v_transformed = abs(v - abs_offset) if use_abs else v
                    col_values.append((v_transformed, v))
            if col_values:
                best_idx = 0 if not higher_is_better else len(col_values) - 1
                if not higher_is_better:
                    best_idx = min(range(len(col_values)), key=lambda x: col_values[x][0])
                else:
                    best_idx = max(range(len(col_values)), key=lambda x: col_values[x][0])
                best_values[col_idx] = col_values[best_idx][0]
                best_original_values[col_idx] = col_values[best_idx][1]
        
        for item in raw_data:
            data_code = item.get('dataCode', '')
            values = item.get('values', [])
            average = item.get('average', '')
            
            value_strs = []
            for i, v in enumerate(values):
                formatted = self._format_value(v)
                if i in best_original_values and v == best_original_values[i]:
                    formatted = f"**{formatted}**"
                value_strs.append(formatted)
            
            value_strs.extend('-' for _ in range(len(columns) - len(value_strs)))
            avg_str = self._format_value(average)
            
            lines.append(f"| {data_code} | " + " | ".join(value_strs) + f" | {avg_str} |")
        
        return lines
    
    def save_to_obsidian(self, markdown: str, filename: str = None) -> str:
        """将Markdown内容保存到Obsidian vault
        
        Args:
            markdown: Markdown格式的内容
            filename: 文件名（不含扩展名），默认为 {element}_{yearmonth}
            
        Returns:
            str: 保存的文件路径
        """
        vault_path = PathConfig.OBSIDIAN_VAULT_PATH
        if not vault_path.exists():
            vault_path.mkdir(parents=True, exist_ok=True)
        
        if not filename:
            element = self.metadata.get('element', 'forecast')
            time_range = self.metadata.get('time_range', {})
            begin = time_range.get('begin', '')
            test_type = self.metadata.get('test_type', '')
            rain_type = self.metadata.get('rain_type', '')
            if begin:
                year_month = begin[:7].replace('-', '')
            else:
                year_month = datetime.now().strftime('%Y%m')
            
            if rain_type:
                filename = f"{element}_{rain_type.upper()}_{test_type}_{year_month}"
            else:
                filename = f"{element}_{test_type}_{year_month}"
        
        note_path = vault_path / f"{filename}.md"
        
        try:
            # 保存markdown文件
            with open(note_path, 'w', encoding='utf-8') as f:
                f.write(markdown)
            
            # 复制图片到vault（如果需要）
            # if image_paths:
            #     for img_path in image_paths:
            #         src = Path(img_path)
            #         if src.exists():
            #             dst = vault_path / src.name
            #             import shutil
            #             shutil.copy2(src, dst)
            
            return str(note_path)
        except Exception as e:
            return f"保存失败: {str(e)}"


if __name__ == "__main__":
    import argparse
    month = datetime.now().strftime('%Y%m')
    
    # 解析命令行参数
    parser = argparse.ArgumentParser(description='预报检验分析工具')
    parser.add_argument('--path', type=str, default='/Users/merlinq/Workspace/download/JSON',
                        help='JSON文件目录路径')
    parser.add_argument('--element', type=str, default='tmax24',
                        choices=list(Config.TEMP_ELEMENTS.keys()) + list(Config.RAIN_ELEMENTS.keys()),
                        help='要素名称（如 tmax24, tmin24, rain24）')
    parser.add_argument('--test-type', type=str, default='daily',
                        choices=['daily', 'time_session', 'area'],
                        help='检验类型（如 daily/逐日, time_session/逐时效, area/分地区）')
    parser.add_argument('--rain-type', type=str, default='ng',
                       choices=['ng', 'g', 'acc'],
                       help='降水检验类型 (single模式使用，仅降水，如 ng（晴雨不分级）, g（分级降水）, acc（累计降水）)')
    parser.add_argument('--month', type=str, default=month,
                        help='年月（如 202604）')
    parser.add_argument('--predict-hours', type=str, default='08,20',
                        help='起报时间，如 08（早08点）、20（晚20点）、08,20（默认，早晚两次）')
    parser.add_argument('--list', action='store_true',
                        help='列出所有可用的要素和检验类型')
    args = parser.parse_args()
    
    if args.list:
        print("可用要素:")
        for k, v in Config.TEMP_ELEMENTS.items():
            print(f"  {k}: {v}")
        for k, v in Config.RAIN_ELEMENTS.items():
            print(f"  {k}: {v}")
        print("\n可用检验类型:")
        print("  daily: 逐日")
        print("  time_session: 逐时效")
        print("  area: 分地区")
        print("\n可用检验类别:")
        print("  default: 默认（温度）")
        print("  ng: 晴雨不分级（降水）")
        print("  g: 分级检验（降水）")
        print("  acc: 累计检验（降水）")
        exit(0)
    
    # 查找匹配的JSON文件
    base_path = Path(args.path)
    json_file = None
    
    if args.element and args.test_type and args.month:
        # 判断要素类型，设置默认rain_type
        is_temp = args.element in Config.TEMP_ELEMENTS            
        if is_temp:
            rain_type = 'default'
        else:
            rain_type = args.rain_type
        
        # 获取 predict_hours，如果使用默认的 08,20，则不添加到路径中（保持向后兼容）
        predict_hours = args.predict_hours.replace(',', '') if args.predict_hours else ''
        
        if predict_hours and predict_hours not in ['0820', '08,20']:
            search_pattern = f"{args.element}/{rain_type}/{args.test_type}/{predict_hours}/{args.month}.json"
        else:
            search_pattern = f"{args.element}/{rain_type}/{args.test_type}/{args.month}.json"
        
        potential_file = base_path / search_pattern

        if potential_file.exists():
            json_file = str(potential_file)
        else:
            json_file = None
    
    if not json_file:
        print(f"未找到匹配的JSON文件")
        exit(1)
    
    print(f"使用文件: {json_file}")
    
    with open(json_file, "r") as f:
        json_data = json.load(f)

    # 生成详细报告并转换为Markdown（包含图片）
    analyzer = ForecastAnalyzer(json_data)

    # 生成详细报告
    report = analyzer.generate_detailed_report()
    # print("=" * 60)
    # print(f"检验报告summary:\n\n```markdown\n{report['summary']}```")
    # print("=" * 60)

    # 转换为Markdown
    markdown = analyzer.format_report_to_markdown(report)
    print("=" * 60)
    print(f"检验报告(markdown):\n```markdown\n{markdown}```")
    print("=" * 60)
    
    # 保存到Obsidian
    saved_path = analyzer.save_to_obsidian(markdown)
    print("=" * 60)
    print(f"OBSIDIAN_LINK: [{Path(saved_path).name}]({saved_path})")
    print("=" * 60)
    
