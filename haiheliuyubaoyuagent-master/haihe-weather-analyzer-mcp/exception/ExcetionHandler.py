# bp_exception = Blueprint('exception', __name__)
from flask import current_app as app

from App.dto.Result import error
import logging

logger = logging.getLogger(__name__)
"""
    异常处理
"""


def handle_business_exception(e):
    # print('BusinessException')
    logger.error(f"BusinessException: {str(e)}",
                 exc_info=True)
    return error(message=str(e), status_code=500)


def handle_uncaught_exception(e):
    # from flask import current_app as app  # 延迟导入，避免循环依赖
    logger.error(
        f"Unhandled Exception: {str(e)}",
        exc_info=True
    )
    return error(str(e))
#
# @app.before_request
# def log_request_start():
#     request.start_time = time.time()
#     logger.info(
#         f"Request Started | URL: {request.url} | Method: {request.method} | IP: {request.remote_addr}"
#     )
#
#
# @app.after_request
# def log_request_end(response):
#     duration = (time.time() - request.start_time) * 1000  # 转毫秒
#     print(duration, request.start_time)
#     logger.info(
#         f"Request Completed | Status: {response.status_code} | Duration: {duration:.2f}ms"
#     )
#     return response
