"""微信发送文件到群（可插拔契约）。

当前为占位实现：内网文件发送能力已实现，后续将代码拉入项目后
替换本函数体即可，无需改动调用方。返回 bool 表示是否发送成功。
"""
import logging

logger = logging.getLogger(__name__)


def send_file(group: str, file_path: str, caption: str) -> bool:
    """发送文件到微信群。group=群名，file_path=本地文件路径，caption=附带话术。

    占位实现：记 warning 并返回 False。接入内网实现后返回真实发送结果。
    """
    logger.warning(
        "send_file 未实现（占位）：group=%s file=%s caption=%s",
        group, file_path, caption,
    )
    return False