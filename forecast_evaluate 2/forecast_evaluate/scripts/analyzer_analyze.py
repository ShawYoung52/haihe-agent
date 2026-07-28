#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
分析脚本：根据 SKILL_ANA.md 的"较差定义标准"分析预报检验数据
"""

import re
from typing import Dict, List, Any, Optional


class ForecastAnalyzer:
    THRESHOLDS = {
        "area": {
            "daily": {
                "temperature": {
                    "accuracy": 80.0,
                    "mae": 1.5,
                    "me": 1.0,
                },
                "precipitation": {
                    "accuracy": 85.0,
                    "ts": "mean",
                    "bias_low": 0.6,
                    "bias_high": 1.4,
                },
            },
        },
        "time_session": {
            "le_72h": {
                "temperature": {
                    "accuracy": 80.0,
                    "mae": 1.5,
                    "me": 1.0,
                },
                "precipitation": {
                    "accuracy": 85.0,
                    "ts": "mean",
                    "bias_low": 0.6,
                    "bias_high": 1.4,
                },
            },
            "gt_72h": {
                "temperature": {
                    "accuracy": 70.0,
                    "mae": 3.0,
                    "me": 1.5,
                },
                "precipitation": {
                    "accuracy": 70.0,
                    "ts": "mean",
                    "bias_low": 0.3,
                    "bias_high": 2.0,
                },
            },
        },
    }

    def __init__(self, summary_text: str):
        self.summary_text = summary_text
        self.dimension = self._detect_dimension()
        self.element_type = self._detect_element_type()
        self.data = self._parse_summary()

    def _detect_dimension(self) -> str:
        if "daily" in self.summary_text[:100].lower():
            return "daily"
        elif "time_session" in self.summary_text[:100].lower() or "时效" in self.summary_text[:100]:
            return "time_session"
        else:
            return "area"

    def _detect_element_type(self) -> str:
        temp_keywords = ["温度", "最高温度", "最低温度", "tmax", "tmin", "t2m"]
        rain_keywords = ["降水", "降雨", "rain"]

        for kw in temp_keywords:
            if kw.lower() in self.summary_text.lower():
                return "temperature"
        return "precipitation"

    def _parse_summary(self) -> Dict[str, Any]:
        data = {
            "period": "",
            "element": "",
            "overall": {},
            "details": [],
        }

        lines = self.summary_text.strip().split("\n")

        if lines and "至" in lines[0]:
            data["period"] = lines[0].strip()

        current_metric = None

        for i, line in enumerate(lines):
            line = line.strip()
            if not line:
                current_metric = None
                continue

            if "平均绝对误差" in line or ("MAE" in line.upper() and "原始数据" in line):
                current_metric = "mae"
                self._parse_overall_mae(line, data)
            elif "平均误差" in line and "原始数据" in line:
                current_metric = "me"
                self._parse_overall_me(line, data)
            elif ("准确率" in line or "%" in line) and "原始数据" in line:
                current_metric = "accuracy"
                self._parse_overall_accuracy(line, data)
            elif current_metric and re.match(r"^[\u4e00-\u9fa5a-zA-Z]+[,，]", line):
                self._parse_detail_row(line, current_metric, data)

        return data

    def _parse_overall_mae(self, line: str, data: Dict):
        pattern = r"平均绝对误差表现为([^)]+)\(([^)]+)\)\s*>\s*([^)]+)\(([^)]+)\)\s*>\s*([^)]+)\(([^)]+)\)"
        match = re.search(pattern, line)
        if match:
            products = [match.group(1).strip(), match.group(3).strip(), match.group(5).strip()]
            values = [float(match.group(2)), float(match.group(4)), float(match.group(6))]
            for prod, val in zip(products, values):
                data["overall"][prod] = data.get("overall", {}).get(prod, {})
                data["overall"][prod]["mae"] = val

    def _parse_overall_me(self, line: str, data: Dict):
        pattern = r"平均误差表现为([^)]+)\(([^)]+)\)\s*>\s*([^)]+)\(([^)]+)\)\s*>\s*([^)]+)\(([^)]+)\)"
        match = re.search(pattern, line)
        if match:
            products = [match.group(1).strip(), match.group(3).strip(), match.group(5).strip()]
            values = [float(match.group(2)), float(match.group(4)), float(match.group(6))]
            for prod, val in zip(products, values):
                data["overall"][prod] = data.get("overall", {}).get(prod, {})
                data["overall"][prod]["me"] = val

    def _parse_overall_accuracy(self, line: str, data: Dict):
        pattern = r"准确率表现为([^)]+)\(([^)]+)\)\s*>\s*([^)]+)\(([^)]+)\)\s*>\s*([^)]+)\(([^)]+)\)"
        match = re.search(pattern, line)
        if match:
            products = [match.group(1).strip(), match.group(3).strip(), match.group(5).strip()]
            values = [float(match.group(2)), float(match.group(4)), float(match.group(6))]
            for prod, val in zip(products, values):
                data["overall"][prod] = data.get("overall", {}).get(prod, {})
                data["overall"][prod]["accuracy"] = val

    def _parse_detail_row(self, line: str, metric: str, data: Dict):
        parts = [p.strip() for p in re.split(r"[,，]", line)]
        if len(parts) < 2:
            return

        area_name = parts[0]

        existing = None
        for d in data["details"]:
            if d["type"] == area_name:
                existing = d
                break

        if not existing:
            existing = {"type": area_name, "values": {}}
            data["details"].append(existing)

        if len(parts) >= 4:
            try:
                existing["values"]["国家指导"] = float(parts[1])
            except ValueError:
                existing["values"]["国家指导"] = parts[1]
            try:
                existing["values"]["天津预报"] = float(parts[2])
            except ValueError:
                existing["values"]["天津预报"] = parts[2]
            try:
                existing["values"]["ECMWF"] = float(parts[3])
            except ValueError:
                existing["values"]["ECMWF"] = parts[3]
        elif len(parts) >= 3:
            try:
                existing["values"]["天津预报"] = float(parts[1])
            except ValueError:
                existing["values"]["天津预报"] = parts[1]
            try:
                existing["values"]["ECMWF"] = float(parts[2])
            except ValueError:
                existing["values"]["ECMWF"] = parts[2]

        if metric == "mae" and len(parts) >= 3:
            try:
                existing["values"]["天津预报_MAE"] = float(parts[2])
                if len(parts) >= 4:
                    try:
                        existing["values"]["国家指导_MAE"] = float(parts[1])
                    except:
                        pass
                if len(parts) >= 4:
                    try:
                        existing["values"]["ECMWF_MAE"] = float(parts[3])
                    except:
                        pass
            except ValueError:
                pass
        elif metric == "me" and len(parts) >= 3:
            try:
                existing["values"]["天津预报_ME"] = float(parts[2])
                if len(parts) >= 4:
                    try:
                        existing["values"]["国家指导_ME"] = float(parts[1])
                    except:
                        pass
                if len(parts) >= 4:
                    try:
                        existing["values"]["ECMWF_ME"] = float(parts[3])
                    except:
                        pass
            except ValueError:
                pass
        elif metric == "accuracy" and len(parts) >= 3:
            try:
                existing["values"]["天津预报_准确率"] = float(parts[2])
                if len(parts) >= 4:
                    try:
                        existing["values"]["国家指导_准确率"] = float(parts[1])
                    except:
                        pass
                if len(parts) >= 4:
                    try:
                        existing["values"]["ECMWF_准确率"] = float(parts[3])
                    except:
                        pass
            except ValueError:
                pass

    def _get_thresholds(self) -> Dict:
        if self.dimension == "area" or self.dimension == "daily":
            return self.THRESHOLDS["area"]["daily"].get(self.element_type, {})
        elif self.dimension == "time_session":
            return self.THRESHOLDS["time_session"]["gt_72h"].get(self.element_type, {})
        return {}

    def analyze(self) -> Dict[str, Any]:
        result = {
            "dimension": self.dimension,
            "element_type": self.element_type,
            "period": self.data.get("period", ""),
            "overall": self.data.get("overall", {}),
            "poor_samples": self._find_poor_samples(),
        }
        return result

    def _find_poor_samples(self) -> List[Dict[str, Any]]:
        thresholds = self._get_thresholds()
        poor_samples = []

        if self.element_type == "temperature":
            temp_thresh = thresholds.get("temperature", {})
            acc_thresh = temp_thresh.get("accuracy", 80)
            mae_thresh = temp_thresh.get("mae", 1.5)
            me_thresh = temp_thresh.get("me", 1.0)
        else:
            temp_thresh = {}
            acc_thresh = thresholds.get("precipitation", {}).get("accuracy", 85)
            mae_thresh = 0
            me_thresh = 0

        for detail in self.data.get("details", []):

            is_poor = False
            reasons = []

            accuracy = detail["values"].get("天津预报_准确率")
            mae = detail["values"].get("天津预报_MAE")
            me = detail["values"].get("天津预报_ME")

            if self.element_type == "temperature":
                if accuracy is not None and accuracy < acc_thresh:
                    is_poor = True
                    reasons.append(f"准确率{accuracy}% < {acc_thresh}%")

                if mae is not None and mae > mae_thresh:
                    is_poor = True
                    reasons.append(f"MAE {mae} > {mae_thresh}")

                if me is not None and abs(me) >= me_thresh:
                    is_poor = True
                    reasons.append(f"|ME| {abs(me):.2f} >= {me_thresh}")
            else:
                if accuracy is not None and accuracy < acc_thresh:
                    is_poor = True
                    reasons.append(f"准确率{accuracy}% < {acc_thresh}%")

            if is_poor:
                poor_samples.append({
                    "name": detail["type"],
                    "values": detail["values"],
                    "reasons": reasons,
                })

        def sort_key(x):
            acc = x["values"].get("天津预报_准确率", 100) if x["values"].get("天津预报_准确率") else 100
            mae = x["values"].get("天津预报_MAE", 0) if x["values"].get("天津预报_MAE") else 0
            me = abs(x["values"].get("天津预报_ME", 0)) if x["values"].get("天津预报_ME") else 0
            return (acc, -mae, -me)

        poor_samples.sort(key=sort_key)

        return poor_samples

    def generate_report(self) -> str:
        analysis = self.analyze()
        lines = []

        lines.append("### 总体结论")
        lines.append(self._generate_conclusion(analysis))

        lines.append("")
        lines.append("### 分段分析")

        if self.element_type == "temperature":
            lines.extend(self._generate_temperature分段(analysis))
        else:
            lines.extend(self._generate_precipitation分段(analysis))

        lines.append("")
        lines.append("### 重点定位")
        lines.extend(self._generate_重点定位(analysis))

        return "\n".join(lines)

    def _generate_conclusion(self, analysis: Dict) -> str:
        overall = analysis.get("overall", {})
        period = analysis.get("period", "")

        tianjin = overall.get("天津预报", {})
        if not tianjin:
            return f"{period}，数据解析异常。"

        acc = tianjin.get("accuracy", 0)
        mae = tianjin.get("mae", 0)
        me = tianjin.get("me", 0)

        national = overall.get("国家指导", {})
        national_acc = national.get("accuracy", 0)
        national_mae = national.get("mae", 0)
        national_me = national.get("me", 0)

        ecmwf = overall.get("ECMWF", {})
        ecmwf_acc = ecmwf.get("accuracy", 0)
        ecmwf_mae = ecmwf.get("mae", 0)
        ecmwf_me = ecmwf.get("me", 0)

        element_name = "最高温度" if "最高" in self.summary_text else ("最低温度" if "最低" in self.summary_text else "温度")

        lines = []

        acc_best = acc >= national_acc and acc >= ecmwf_acc
        mae_best = mae <= national_mae and mae <= ecmwf_mae

        if acc_best and mae_best:
            lines.append(f"{period}，**天津预报**24小时{element_name}在2°C准确率（{acc}%）和MAE（{mae}）上均优于")
            lines[-1] += f"**国家指导**（{national_acc}%，{national_mae}）和**ECMWF**（{ecmwf_acc}%，{ecmwf_mae}），整体表现最好。"
        elif acc_best and not mae_best:
            lines.append(f"{period}，**天津预报**24小时{element_name}的2°C准确率（{acc}%）略优于")
            if ecmwf_acc >= national_acc:
                lines[-1] += f"**ECMWF**（{ecmwf_acc}%）和**国家指导**（{national_acc}%），"
            else:
                lines[-1] += f"**国家指导**（{national_acc}%）和**ECMWF**（{ecmwf_acc}%），"
            if mae > ecmwf_mae:
                lines[-1] += f"但平均绝对误差（{mae}）大于**ECMWF**（{ecmwf_mae}），整体误差控制能力居中。"
            else:
                lines[-1] += f"但MAE（{mae}）略大于**ECMWF**（{ecmwf_mae}）。"
        else:
            lines.append(f"{period}，**天津预报**24小时{element_name}准确率（{acc}%）")

        if me != 0 and ecmwf_me != 0:
            if abs(me) > abs(ecmwf_me):
                lines.append(f"平均误差（{me:+.2f}）均大于**ECMWF**（{ecmwf_me:+.2f}），")
            elif abs(me) > abs(national_me):
                lines.append(f"平均误差（{me:+.2f}）大于**国家指导**（{national_me:+.2f}），")

        poor = analysis.get("poor_samples", [])
        if poor:
            poor_acc_names = [p["name"] for p in poor if p["values"].get("准确率") and p["values"].get("准确率") < 80]
            poor_mae_names = [p["name"] for p in poor if p["values"].get("MAE") and p["values"].get("MAE") > 1.5]
            poor_me_names = [p["name"] for p in poor if p["values"].get("ME") and abs(p["values"].get("ME", 0)) >= 1.0]

            issues = []
            if poor_acc_names:
                issues.append(f"准确率偏低（{', '.join(poor_acc_names[:4])}）")
            if poor_mae_names:
                issues.append(f"MAE偏高（{', '.join(poor_mae_names[:3])}）")
            if poor_me_names:
                issues.append(f"ME偏大（{', '.join(poor_me_names[:4])}）")

            if issues:
                lines.append(f"主要不足在于**{', '.join(poor_acc_names[:4]) if poor_acc_names else poor_mae_names[:3]}**的{issues[0]}")

        return "".join(lines)

    def _generate_temperature分段(self, analysis: Dict) -> List[str]:
        lines = []
        overall = analysis.get("overall", {})

        tianjin = overall.get("天津预报", {})
        acc = tianjin.get("accuracy", 0)
        mae = tianjin.get("mae", 0)
        me = tianjin.get("me", 0)

        thresh_acc = 80
        thresh_mae = 1.5
        thresh_me = 1.0

        lines.append(f"**准确率**（**天津预报**整体值{acc}%，低于{thresh_acc}%的区域）")
        poor_acc = [p for p in analysis["poor_samples"] if p["values"].get("准确率") and p["values"].get("准确率") < thresh_acc]
        if poor_acc:
            lines.append(f"| 区域 | " + " | ".join([p["name"] for p in poor_acc]) + " |")
            lines.append(f"| 准确率(%) | " + " | ".join([str(p["values"].get("准确率", "")) for p in poor_acc]) + " |")
        else:
            lines.append("无")

        lines.append("")
        lines.append(f"**MAE**（**天津预报**整体值{mae}，高于{thresh_mae}°C的区域）")
        poor_mae = [p for p in analysis["poor_samples"] if p["values"].get("MAE") and p["values"].get("MAE") > thresh_mae]
        if poor_mae:
            lines.append(f"| 区域 | " + " | ".join([p["name"] for p in poor_mae]) + " |")
            lines.append(f"| MAE(°C) | " + " | ".join([str(p["values"].get("MAE", "")) for p in poor_mae]) + " |")
        else:
            lines.append("无")

        lines.append("")
        lines.append(f"**ME**（**天津预报**整体值{me:+.2f}，绝对值≥{thresh_me}°C的区域）")
        poor_me = [p for p in analysis["poor_samples"] if p["values"].get("ME") and abs(p["values"].get("ME", 0)) >= thresh_me]
        if poor_me:
            lines.append(f"| 区域 | " + " | ".join([p["name"] for p in poor_me]) + " |")
            lines.append(f"| ME(°C) | " + " | ".join([f"{p['values'].get('ME', 0):+.2f}" for p in poor_me]) + " |")
        else:
            lines.append("无")

        return lines

    def _generate_precipitation分段(self, analysis: Dict) -> List[str]:
        lines = []
        lines.append("**降水分析待完善**")
        return lines

    def _generate_重点定位(self, analysis: Dict) -> List[str]:
        lines = []
        overall = analysis.get("overall", {})

        tianjin = overall.get("天津预报", {})
        acc = tianjin.get("accuracy", 0)
        mae = tianjin.get("mae", 0)
        me = tianjin.get("me", 0)

        lines.append(f"**天津预报**在所有区域的平均值：准确率{acc}%，MAE {mae}，ME {me:+0.2f}。")

        poor = analysis.get("poor_samples", [])
        if poor:
            names = [p["name"] for p in poor]
            lines.append(f"根据较差定义标准，表现相对较弱的区域（满足任一条件）有：**{', '.join(names)}**。按严重程度排序，逐一对比其他预报：")

            for p in poor:
                name = p["name"]
                vals = p["values"]

                detail_info = {}
                for detail in self.data.get("details", []):
                    if detail["type"] == name:
                        detail_info = detail["values"]
                        break

                national_acc = detail_info.get("国家指导_准确率", "")
                national_mae = detail_info.get("国家指导_MAE", "")
                national_me = detail_info.get("国家指导_ME", "")

                ecmwf_acc = detail_info.get("ECMWF_准确率", "")
                ecmwf_mae = detail_info.get("ECMWF_MAE", "")
                ecmwf_me = detail_info.get("ECMWF_ME", "")

                desc_parts = []
                detail_parts = []

                acc_thresh = 80
                mae_thresh = 1.5
                me_thresh = 1.0

                if vals.get("天津预报_准确率") and vals.get("天津预报_准确率") < acc_thresh:
                    desc_parts.append(f"准确率{vals.get('天津预报_准确率')}%")
                    if national_acc and ecmwf_acc:
                        detail_parts.append(f"准确率{vals.get('天津预报_准确率')}%，**国家指导**{national_acc}%，**ECMWF**{ecmwf_acc}%")

                if vals.get("天津预报_MAE") and vals.get("天津预报_MAE") > mae_thresh:
                    desc_parts.append(f"MAE {vals.get('天津预报_MAE')}")
                    if national_mae and ecmwf_mae:
                        detail_parts.append(f"MAE {vals.get('天津预报_MAE')}，**国家指导**{national_mae}，**ECMWF**{ecmwf_mae}")

                if vals.get("天津预报_ME") and abs(vals.get("天津预报_ME")) >= me_thresh:
                    desc_parts.append(f"ME {vals.get('天津预报_ME'):+.2f}")
                    if national_me and ecmwf_me:
                        detail_parts.append(f"ME {vals.get('天津预报_ME'):+.2f}，**国家指导**{national_me:+0.2f}，**ECMWF**{ecmwf_me:+0.2f}")

                if detail_parts:
                    lines.append(f"- **{name}**（{', '.join(desc_parts)}）：**天津预报**{'，'.join(detail_parts)}。")
        else:
            lines.append("**天津预报**在所有区域表现良好，无明显薄弱区域。")

        return lines


def analyze_summary(summary_text: str) -> str:
    analyzer = ForecastAnalyzer(summary_text)
    return analyzer.generate_report()


if __name__ == "__main__":
    sample_summary0 = """
20260401 至 20260430
24小时最低温度：
平均绝对误差表现为ECMWF(1.23) > 天津预报(1.36) > 国家指导(1.38)。平均绝对误差原始数据：
area,国家指导,天津预报,ECMWF
北辰区,1.16,1.17,0.93
河西区,1.01,1.07,1.1
武清区,1.3,1.19,1.05
宝坻区,1.79,1.31,1.45
滨海新区,1.96,1.63,1.48
东丽区,1.31,1.25,1.05
西青区,1.82,1.9,1.72
津南区,1.48,1.57,1.25
宁河区,1.42,1.29,1.2
静海区,1.01,1.21,1.16
蓟州区,0.9,1.39,1.13
平均误差表现为ECMWF(0.06) > 国家指导(0.54) > 天津预报(0.69)。平均误差原始数据：
area,国家指导,天津预报,ECMWF
北辰区,-0.29,0.31,-0.2
河西区,0.72,0.68,-0.8
武清区,0.19,0.53,0.2
宝坻区,0.7,0.53,-0.58
滨海新区,1.37,0.78,0.78
东丽区,0.88,1.0,0.08
西青区,1.15,1.52,1.7
津南区,1.01,1.45,1.15
宁河区,-0.01,0.08,-1.0
静海区,0.03,-0.39,-0.86
蓟州区,0.24,1.05,0.22
2℃准确率表现为天津预报(80.08) > ECMWF(79.22) > 国家指导(74.03)。2℃准确率原始数据：
area,国家指导,天津预报,ECMWF
北辰区,85.71,85.71,85.71
河西区,76.19,85.71,90.48
武清区,76.19,85.71,80.95
宝坻区,61.9,71.43,76.19
滨海新区,47.62,61.9,61.9
东丽区,76.19,85.71,85.71
西青区,66.67,66.67,71.43
津南区,66.67,80.95,76.19
宁河区,76.19,85.71,76.19
静海区,85.71,95.24,80.95
蓟州区,95.24,76.19,85.71
"""
    result = analyze_summary(sample_summary0)
    print(result)
    print("-----------------")

    sample_summary1 = """
20260401 至 20260430
24小时最高温度：
平均绝对误差表现为天津预报(1.18) > 国家指导(1.30) > ECMWF(1.33)。平均绝对误差原始数据：
area,国家指导,天津预报,ECMWF
北辰区,1.09,1.08,0.9
河西区,1.2,1.11,1.22
武清区,1.13,1.1,1.04
宝坻区,1.33,1.1,1.21
滨海新区,1.76,1.4,3.02
东丽区,1.29,1.08,1.18
西青区,1.08,1.07,0.95
津南区,1.6,1.33,1.18
宁河区,1.17,1.25,1.12
静海区,1.01,1.09,0.79
蓟州区,1.61,1.32,2.04
平均误差表现为国家指导(0.11) > 天津预报(0.26) > ECMWF(-0.53)。平均误差原始数据：
area,国家指导,天津预报,ECMWF
北辰区,0.23,0.41,-0.06
河西区,0.19,0.4,-0.51
武清区,0.36,0.4,-0.05
宝坻区,0.3,0.03,0.13
滨海新区,-0.53,-0.12,-3.02
东丽区,-0.21,0.2,-0.58
西青区,0.23,0.43,-0.12
津南区,0.43,0.54,-0.24
宁河区,-0.07,0.02,0.23
静海区,0.32,0.41,-0.14
蓟州区,0.01,0.1,-1.5
2°C准确率表现为天津预报(84.09) > 国家指导(79.17) > ECMWF(76.14)。2°C准确率原始数据：
area,国家指导,天津预报,ECMWF
北辰区,79.17,91.67,87.5
河西区,75.0,87.5,79.17
武清区,83.33,87.5,83.33
宝坻区,83.33,79.17,79.17
滨海新区,70.83,70.83,29.17
东丽区,70.83,87.5,91.67
西青区,83.33,91.67,91.67
津南区,75.0,83.33,79.17
宁河区,83.33,87.5,79.17
静海区,87.5,91.67,87.5
蓟州区,79.17,66.67,50.0
"""
    result = analyze_summary(sample_summary1)
    print(result)
