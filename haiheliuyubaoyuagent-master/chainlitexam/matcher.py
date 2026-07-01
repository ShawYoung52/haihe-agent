import jieba
from answers import FAQ_LIST


def _tokenize(text: str) -> set:
    """中文分词并去重"""
    return set(jieba.cut(text))


def match_faq(user_input: str) -> str | None:
    """
    FAQ匹配规则：
    1. 必须命中至少1个主题词
    2. 必须命中至少1个意图词
    3. 多个FAQ同时命中时，按得分最高返回
    """

    user_words = _tokenize(user_input)

    candidates = []

    for item in FAQ_LIST:

        # 主题词命中数
        topic_score = len(
            set(item["topic_keywords"]) & user_words
        )

        # 意图词命中数
        intent_score = len(
            set(item["intent_keywords"]) & user_words
        )

        # 必须同时命中主题词和意图词
        if topic_score == 0 or intent_score == 0:
            continue

        # 主题权重更高
        total_score = topic_score * 2 + intent_score

        candidates.append(
            (total_score, item["answer"])
        )

    if not candidates:
        return None

    # 返回得分最高的答案
    best_score, best_answer = max(
        candidates,
        key=lambda x: x[0]
    )

    return best_answer