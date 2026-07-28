# -*- coding: utf-8 -*-
"""
预报检验工具函数
包含区域合并、区县名称映射、产品名称映射等
"""

from config import AreaConfig, ProductConfig


# ============================================================
# 工具函数
# ============================================================

def merge_areas(data, merge_groups=None):
    """
    合并区域数据

    Args:
        data: 字典，key为(area_code, data_code)元组，value为数值列表
        merge_groups: 合并规则字典，如 {'市区': {'120101', '120102', ...}}

    Returns:
        dict: 合并后的数据，key为(merged_area_name, data_code)
    """
    if merge_groups is None:
        merge_groups = AreaConfig.TJ_AREA_MERGE_GROUPS

    merged = {}
    for (area_code, data_code), values in data.items():
        merged_to = None
        for merged_name, original_codes in merge_groups.items():
            if area_code in original_codes:
                merged_to = merged_name
                break
        if merged_to:
            key = (merged_to, data_code)
            if key not in merged:
                merged[key] = values
            else:
                # 对应位置求平均
                merged[key] = [
                    ((a or 0) + (b or 0)) / 2 if a is not None and b is not None else (a or b)
                    for a, b in zip(merged[key], values)
                ]
        else:
            merged[(AreaConfig.TJ_AREA_NAMES.get(area_code, area_code), data_code)] = values

    return merged


def get_area_name(area_code):
    """获取区县名称"""
    return AreaConfig.TJ_AREA_NAMES.get(area_code, area_code)


def get_product_name(product_code):
    """获取产品名称"""
    return ProductConfig.PRODUCT_NAMES.get(product_code, product_code)