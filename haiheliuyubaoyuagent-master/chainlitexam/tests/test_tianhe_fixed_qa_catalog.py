from tools.tianhe_fixed_qa_catalog import (
    TIANHE_FIXED_QA_QUESTIONS,
    is_tianhe_fixed_qa_question,
)


NEW_TIANHE_CATALOG_QUESTIONS = (
    "今年7月蓟州区有多少天超过35℃",
    "今年以来我市40℃以上高温出现过几次？",
    "现在市区风大吗？",
    "市区现在气温和风的实况",
    "全市现在下了多少雨",
    "今天雨都下在哪了",
    "暴雨天气的防范建议",
    "大风天气的防范建议",
    "高温天气的防范建议",
    "强对流天气怎么应对",
    "暴雨预警四个等级是什么",
    "高温怎么定义",
    "气温多高算是高温",
    "高温来了公众应该怎么办",
    "高温预警信号及应对措施",
    "降雨量怎么分等级",
    "台风等级",
    "暴雨预警发出后公众该怎么办",
    "暴雨是如何形成的",
    "暴雨等级是如何划分的",
    "暴雨的主要危害有哪些",
    "当前湿度大不大？",
    "今日雨情",
    "今天适合洗车吗？",
    "今天穿衣有什么建议？",
    "今天适不适合晾晒？",
    "什么是短时强降水？",
    "副高代表什么含义？",
    "什么是面雨量？",
    "雷电怎么防御？",
    "高温有哪些危害？",
    "冰雹产生原理？",
    "双偏振雷达干什么用？",
    "自动气象站如何观测？",
    "气象卫星有什么作用？",
    "雾和霾有什么区别？",
    "夏天为何多雨？",
    "为什么打雷下雨？",
    "天津当前的天气情况",
    "预警发布流程是什么？",
    "天气会商包含哪些内容？",
    "面雨量如何计算？",
    "双偏振雷达产品怎么看？",
    "MICAPS 产品怎么分析？",
    "你可以回答哪些问题？",
    "明天出门要不要带伞",
    "哪些问题你无法解答？",
    "你的气象数据来源是什么？",
    "预报可以支持多长时效？",
    "我该怎么向你提问？",
    "降雨对道路交通会带来什么影响？",
)


def test_new_catalog_contains_all_51_unique_questions():
    assert len(NEW_TIANHE_CATALOG_QUESTIONS) == 51
    assert len(set(NEW_TIANHE_CATALOG_QUESTIONS)) == 51
    assert TIANHE_FIXED_QA_QUESTIONS == NEW_TIANHE_CATALOG_QUESTIONS


def test_catalog_accepts_only_spacing_and_terminal_punctuation_variants():
    assert is_tianhe_fixed_qa_question("  当前湿度大不大？  ")
    assert is_tianhe_fixed_qa_question("当前 湿度 大不大?")
    assert not is_tianhe_fixed_qa_question("天津未来三天天气怎么样？")
    assert not is_tianhe_fixed_qa_question("明天蓟州湿度大不大？")
