"""天河报告接口调用测试。"""
import pytest
from unittest import mock

from ScheduledTask.report_generator import trigger_weather_bulletin_report, REPORT_API_URL, REPORT_TEMPLATE


def test_trigger_skips_when_level_zero():
    """response_level=0 时不发送 HTTP 请求。"""
    with mock.patch("ScheduledTask.report_generator.requests.post") as mock_post:
        result = trigger_weather_bulletin_report(0)
        assert result is False
        mock_post.assert_not_called()


def test_trigger_sends_when_level_one():
    """response_level=1 时发送请求并返回 True。"""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    with mock.patch("ScheduledTask.report_generator.requests.post", return_value=mock_resp) as mock_post:
        result = trigger_weather_bulletin_report(1)
        assert result is True
        mock_post.assert_called_once_with(
            REPORT_API_URL,
            json={"template": REPORT_TEMPLATE},
            timeout=30,
        )


def test_trigger_sends_for_all_levels():
    """I-IV 级全部触发。"""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    with mock.patch("ScheduledTask.report_generator.requests.post", return_value=mock_resp) as mock_post:
        for level in (1, 2, 3, 4):
            assert trigger_weather_bulletin_report(level) is True
        assert mock_post.call_count == 4


def test_trigger_tolerates_timeout():
    """超时不崩溃，返回 False。"""
    import requests as real_requests
    with mock.patch("ScheduledTask.report_generator.requests.post", side_effect=real_requests.exceptions.Timeout()):
        result = trigger_weather_bulletin_report(2)
        assert result is False


def test_trigger_tolerates_connection_error():
    """连接失败不崩溃，返回 False。"""
    import requests as real_requests
    with mock.patch("ScheduledTask.report_generator.requests.post", side_effect=real_requests.exceptions.ConnectionError()):
        result = trigger_weather_bulletin_report(3)
        assert result is False


def test_trigger_tolerates_http_error():
    """HTTP 5xx 不崩溃，返回 False。"""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    with mock.patch("ScheduledTask.report_generator.requests.post", return_value=mock_resp):
        result = trigger_weather_bulletin_report(4)
        assert result is False
