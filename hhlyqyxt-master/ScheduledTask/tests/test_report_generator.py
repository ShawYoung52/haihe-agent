"""天河报告接口调用测试。"""
import pytest
from unittest import mock

from ScheduledTask.report_generator import trigger_weather_bulletin_report, REPORT_API_URL, REPORT_TEMPLATE


def _mock_ok_response(docx_url="http://x/a.docx", pdf_url="http://x/a.pdf"):
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "template_id": "haihe_weather_bulletin",
        "docx_url": docx_url,
        "pdf_url": pdf_url,
    }
    return mock_resp


def test_trigger_skips_when_level_zero():
    """response_level=0 时不发送 HTTP 请求，返回 None。"""
    with mock.patch("ScheduledTask.report_generator.requests.post") as mock_post:
        result = trigger_weather_bulletin_report(0)
        assert result is None
        mock_post.assert_not_called()


def test_trigger_returns_urls_on_success():
    """response_level>=1 时发送请求并返回 docx/pdf URL。"""
    with mock.patch(
        "ScheduledTask.report_generator.requests.post",
        return_value=_mock_ok_response(),
    ) as mock_post:
        result = trigger_weather_bulletin_report(1)
        assert result == {
            "docx_url": "http://x/a.docx",
            "pdf_url": "http://x/a.pdf",
        }
        mock_post.assert_called_once_with(
            REPORT_API_URL,
            json={"template": REPORT_TEMPLATE},
            timeout=30,
        )


def test_trigger_sends_for_all_levels():
    """I-IV 级全部触发。"""
    with mock.patch(
        "ScheduledTask.report_generator.requests.post",
        return_value=_mock_ok_response(),
    ) as mock_post:
        for level in (1, 2, 3, 4):
            result = trigger_weather_bulletin_report(level)
            assert result is not None
        assert mock_post.call_count == 4


def test_trigger_tolerates_timeout():
    """超时不崩溃，返回 None。"""
    import requests as real_requests
    with mock.patch(
        "ScheduledTask.report_generator.requests.post",
        side_effect=real_requests.exceptions.Timeout(),
    ):
        result = trigger_weather_bulletin_report(2)
        assert result is None


def test_trigger_tolerates_connection_error():
    """连接失败不崩溃，返回 None。"""
    import requests as real_requests
    with mock.patch(
        "ScheduledTask.report_generator.requests.post",
        side_effect=real_requests.exceptions.ConnectionError(),
    ):
        result = trigger_weather_bulletin_report(3)
        assert result is None


def test_trigger_tolerates_http_error():
    """HTTP 5xx 不崩溃，返回 None。"""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    with mock.patch(
        "ScheduledTask.report_generator.requests.post",
        return_value=mock_resp,
    ):
        result = trigger_weather_bulletin_report(4)
        assert result is None


def test_trigger_tolerates_bad_json():
    """响应非 JSON 时不崩溃，返回 None。"""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("not json")
    mock_resp.text = "not json body"
    with mock.patch(
        "ScheduledTask.report_generator.requests.post",
        return_value=mock_resp,
    ):
        result = trigger_weather_bulletin_report(1)
        assert result is None


def test_trigger_urls_can_be_missing_in_response():
    """响应缺 docx_url/pdf_url 时也不崩溃，返回 dict（值为 None）。"""
    mock_resp = mock.MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"template_id": "x"}  # 无 URL 字段
    with mock.patch(
        "ScheduledTask.report_generator.requests.post",
        return_value=mock_resp,
    ):
        result = trigger_weather_bulletin_report(1)
        assert result == {"docx_url": None, "pdf_url": None}
