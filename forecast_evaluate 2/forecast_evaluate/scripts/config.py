# -*- coding: utf-8 -*-
"""
预报检验配置参数
包含API配置、默认参数、区县代码映射等

配置类结构：
- PathConfig: 路径配置
- ApiConfig: API配置
- RainConfig: 降水检验配置
- TempConfig: 温度检验配置
- ElementConfig: 要素名称映射
- ProductConfig: 产品代码和名称映射
- AreaConfig: 区域代码映射
"""

from datetime import datetime
import calendar
from pathlib import Path


# ============================================================
# 工具函数：获取当前月份的起止时间
# ============================================================

def _get_current_month_range() -> tuple:
    """
    动态计算当前月份的起止时间
    
    Returns:
        tuple: (begin_time, end_time) 格式为 "YYYY-MM-DD 00:00:00" / "YYYY-MM-DD 23:59:59"
    """
    now = datetime.now()
    year, month = now.year, now.month
    _, last_day = calendar.monthrange(year, month)
    begin = f"{year:04d}-{month:02d}-01 00:00:00"
    end = f"{year:04d}-{month:02d}-{last_day:02d} 23:59:59"
    return begin, end


# 获取默认值
_DEFAULT_BEGIN_TIME, _DEFAULT_END_TIME = _get_current_month_range()


# ============================================================
# 路径配置
# ============================================================

class PathConfig:
    """路径配置类"""
    
    BASE_SAVE_DIR = Path('/Users/merlinq/Workspace/download/JSON')
    
    PNG_SAVE_DIR = Path('/Users/merlinq/Workspace/download/PNG')
    
    OBSIDIAN_VAULT_PATH = Path('/Users/merlinq/Documents/Obsidian-Vault/检验报告')
    
    SAVE_DIR = PNG_SAVE_DIR
    
    # 目录结构模板: {element}/{test_type}/{subtype}/{YYYYMM}.json
    # element: 要素代码 (t2m, tmax24, tmin24, rain, rain3, rain12, rain24)
    # test_type: 检验方式 (daily, time_session, area)
    # subtype: 降水为 ng/g/acc，温度为 default


# ============================================================
# API配置
# ============================================================

class ApiConfig:
    """API配置类"""
    
    API_URL = 'http://10.226.107.74:31002/exam/common/api'
    API_ENDPOINT = 'common/forwardAndReceiveMessage'
    API_TIMEOUT = 15

    # 认证信息
    USERNAME = 'H8w76Mt7PGNjVh'
    PASSWORD = 'a9u5DsEDvRH3H2mk7NaMUHkrbvmqjiqh'

    # 消息队列
    TOPIC_STATION = 'exam_service'
    TOPIC_GRID = 'exam_grid_service'

# ============================================================
# 降水检验配置
# ============================================================

class RainConfig:
    """降水检验配置类"""
    
    # 降水默认参数
    RAIN_ELEMENT_CODE = 'rain24'
    RAIN_ALGORITHM_NAME = 'PC,TS,BIAS'
    
    # 降水检验类型
    _RAIN_TYPE_NG = 'ng'      # 晴雨（不分级）
    _RAIN_TYPE_GRADED = 'g'   # 分级检验
    _RAIN_TYPE_ACC = 'acc'    # 累计检验
    RAIN_TYPES_ALL = [_RAIN_TYPE_NG, _RAIN_TYPE_GRADED, _RAIN_TYPE_ACC]


# ============================================================
# 温度检验配置
# ============================================================

class TempConfig:
    """温度检验配置类"""
    
    TEMP_ELEMENT_CODE = 'tmax24'
    TEMP_ALGORITHM_NAME = 'PC:2,MAE,ME'


# ============================================================
# 要素名称映射
# ============================================================

class ElementConfig:
    """要素名称映射配置类"""
    
    # 温度要素
    TEMP_ELEMENTS = {
        't2m': '2米温度',
        'tmax24': '24小时最高温度',
        'tmin24': '24小时最低温度',
    }
    
    # 降水要素
    RAIN_ELEMENTS = {
        'rain': '小时降水',
        'rain3': '3小时降水',
        'rain12': '12小时降水',
        'rain24': '24小时降水',
    }
    
    # 所有要素
    ALL_ELEMENTS = {**RAIN_ELEMENTS, **TEMP_ELEMENTS}
    
    # 检验方式名称
    TEST_TYPE_NAMES = {
        'time_session': '逐时效',
        'daily': '逐日',
        'area': '分地区',
    }
    
    # 降水检验子类型名称
    RAIN_SUBTYPE_NAMES = {
        'ng:0': '晴雨',
        'g:1': '小雨',
        'g:2': '中雨',
        'g:3': '大雨',
        'g:4': '暴雨',
        'g:5': '大暴雨',
        'g:6': '特大暴雨',
        'g:7': '一般性降水',
        'g:8': '暴雨(雪)',
        'acc:1': '累计0.1mm',
        'acc:2': '累计10mm',
        'acc:3': '累计25mm',
        'acc:4': '累计50mm',
        'acc:5': '累计100mm',
        'acc:6': '累计250mm',
    }


# ============================================================
# 产品代码和名称映射
# ============================================================

class ProductConfig:
    """产品代码和名称映射配置类"""

    # 产品名称映射
    PRODUCT_NAMES = {
        'NAFP_SCMOC_NC': '国家指导',
        'NAFP_BETJ_DS_NC': '天津预报',
        'NAFP_ECTHIN_NC': 'ECMWF',
    }
    
    # 检验指标说明
    EXAM_DESCRIPTIONS = {}
    
    # 动态生成温度检验指标说明
    for elem_code, elem_name in ElementConfig.TEMP_ELEMENTS.items():
        EXAM_DESCRIPTIONS[elem_name] = {
            'PC:2': '2℃准确率',
            'PC:1': '1℃准确率',
            'MAE': '平均绝对误差',
            'ME': '平均误差',
        }
    
    # 动态生成降水检验指标说明
    metric_suffixes = {'PC': '准确率', 'TS': 'TS评分', 'BIAS': '偏差'}
    for rain_key, rain_name in ElementConfig.RAIN_SUBTYPE_NAMES.items():
        EXAM_DESCRIPTIONS[rain_name] = {}
        for metric, suffix in metric_suffixes.items():
            exam_key = f"{metric}_{rain_key}"
            EXAM_DESCRIPTIONS[rain_name][exam_key] = f"{rain_name}{suffix}"
    
# ============================================================
# 区域代码映射
# ============================================================

class AreaConfig:
    """区域代码映射配置类"""
    
    # 天津区县代码映射
    TJ_AREA_NAMES = {
        '120000': '天津市',
        '120101': '和平区',
        '120102': '河东区',
        '120103': '河西区',
        '120104': '南开区',
        '120105': '河北区',
        '120106': '红桥区',
        '120110': '东丽区',
        '120111': '西青区',
        '120112': '津南区',
        '120113': '北辰区',
        '120114': '武清区',
        '120115': '宝坻区',
        '120116': '滨海新区',
        '120117': '宁河区',
        '120118': '静海区',
        '120119': '蓟州区'
    }
    
    # 天津市内六区代码
    TJ_CENTER_AREAS = {'120101', '120102', '120103', '120104', '120105', '120106'}
    
    # 区域合并映射
    TJ_AREA_MERGE_GROUPS = {
        '市区': TJ_CENTER_AREAS  # 和平+河东+河西+南开+河北+红桥
    }

# ============================================================
# 通用默认参数
# ============================================================

class DefaultConfig:
    """通用默认参数配置类"""
    
    BEGIN_TIME = _DEFAULT_BEGIN_TIME
    END_TIME = _DEFAULT_END_TIME
    PREDICT_HOURS = '08,20'
    COLLECTION_CODE = 'county_jy'
    AREA_CODES = '120000'
    STATIONS = 'all'
    DATA_CODES = ','.join(ProductConfig.PRODUCT_NAMES.keys())
    # GUIDE_MODE = list(ProductConfig.PRODUCT_NAMES.keys())[0] # 可以缺省
    STATIS_TYPES = 'all'
    SAMPLE_FIELDS = 'all'

# ============================================================
# 统一Config类（兼容旧接口）
# ============================================================

class Config(
    PathConfig,
    ApiConfig,
    DefaultConfig,
    RainConfig,
    TempConfig,
    ElementConfig,
    ProductConfig,
    AreaConfig
):
    """统一配置类，继承所有配置类"""
    pass


# ============================================================
# 目录结构工具函数
# ============================================================

def get_json_save_path(element_code: str, test_type: str, year_month: str, 
                       rain_type: str = None, base_dir: Path = None, predict_hours: str = None) -> Path:
    """
    根据参数生成JSON文件的保存路径
    
    目录结构: {base_dir}/{element}/{test_type}/{subtype}/{YYYYMM}.json
    或: {base_dir}/{element}/{test_type}/{predict_hours}/{YYYYMM}.json (当 predict_hours 不是默认 '08,20' 时)
    
    Args:
        element_code: 要素代码 (t2m, tmax24, tmin24, rain, rain3, rain12, rain24)
        test_type: 检验方式 (daily, time_session, area)
        year_month: 年月 (如: 202604)
        rain_type: 降水检验类型 (ng, g, acc)
        base_dir: 基础目录，默认使用 Config.BASE_SAVE_DIR
        predict_hours: 起报时间，如 '08','20','08,20'（非默认时添加到路径中）
        
    Returns:
        Path: 完整的文件保存路径
    """
    if base_dir is None:
        base_dir = Config.BASE_SAVE_DIR
        
    # 确定subtype
    if rain_type is None:
        # 温度数据
        subtype = 'default'
    else:
        # 降水数据
        subtype = rain_type
    
    # 判断是否需要将 predict_hours 添加到路径中
    # 只有当 predict_hours 不是默认的 '08,20' 时才添加到路径
    use_predict_hours_in_path = False
    if predict_hours:
        # 移除逗号后比较
        ph_normalized = predict_hours.replace(',', '')
        if ph_normalized not in ['0820', '08,20', '']:
            use_predict_hours_in_path = True
    
    # 构建路径
    if use_predict_hours_in_path:
        # element/subtype/test_type/predict_hours/YYYYMM.json
        ph_for_path = predict_hours.replace(',', '')
        file_dir = Path(base_dir) / element_code / subtype / test_type / ph_for_path
    else:
        # element/subtype/test_type/YYYYMM.json
        file_dir = Path(base_dir) / element_code / subtype / test_type
    
    file_dir.mkdir(parents=True, exist_ok=True)
    
    return file_dir / f"{year_month}.json"


def get_png_save_path(element_code: str, test_type: str, time_stamp: str,
                      rain_type: str = None, metric: str = "检验指标") -> Path:
    """
    根据参数生成PNG图片的保存路径

    目录结构: {base_dir}/{element}/{subtype}/{test_type}/{time_stamp}_{metric}.png

    Args:
        element_code: 要素代码 (t2m, tmax24, tmin24, rain, rain3, rain12, rain24)
        test_type: 检验方式 (daily, time_session, area)
        time_stamp: 时间戳 (如: 202604)
        rain_type: 降水检验类型 (ng, g, acc)，温度数据为None
        metric: 检验指标 (如 PC, TS, MAE)，用于生成文件名

    Returns:
        Path: 完整的图片保存路径
    """
    base_dir = Config.PNG_SAVE_DIR

    # 构建路径和文件名
    rain_subtype = metric.split('_')[-1] if rain_type else ''
    subtype = Config.RAIN_SUBTYPE_NAMES.get(rain_subtype, rain_subtype) if rain_subtype else ''
    
    file_dir = Path(base_dir) / element_code / subtype / test_type
    file_dir.mkdir(parents=True, exist_ok=True)

    # 处理逻辑：提取核心指标名称，移除冒号
    metric_short = metric.split('_')[0].replace(':', '')
    test_type_upper = test_type.replace('_', '').upper()
    
    filename = f"{element_code.upper()}_{subtype}_{metric_short}_{test_type_upper}_{time_stamp}.png" if subtype else \
               f"{element_code.upper()}_{metric_short}_{test_type_upper}_{time_stamp}.png"

    return file_dir / filename


def parse_json_filename(filename: str) -> dict:
    """
    解析JSON文件名获取要素、检验方式、年月等信息
    
    Args:
        filename: JSON文件名 (如: 202604.json)
        
    Returns:
        dict: 解析结果
    """
    import re
    result = {}
    
    # 匹配 YYYYMM.json
    match = re.match(r'(\d{6})\.json', filename)
    if match:
        result['year_month'] = match.group(1)
    
    return result