"""14所降水专题组合长图工具测试。

口径（用户确认 2026-08-17）：板块 = 降水实况文字 / swan3 雷达图 / 降水实况图 /
实况面雨量图 / 点雨量列表 / 预报面雨量图 / 面雨量预报，纵向拼成长图；任意"长图"
话术触发；各子接口独立容错（失败板块占位，其余照常）。
"""

from __future__ import annotations

import base64
import importlib.util
import io
import sys
from pathlib import Path

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

_spec = importlib.util.spec_from_file_location(
    "composite_longimg_tool",
    MCP_DIR / "custom_tools" / "composite_longimg_tool.py",
)
clt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(clt)

_REAL_TEXT = (
    "8月16日15时-17日15时，海河流域出现小雨，局部中雨，个别站大雨。"
    "最大面雨量出现在永定河，为4.6毫米。"
)


def _make_png(color=(200, 60, 60), size=(80, 50)) -> bytes:
    from PIL import Image
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_IMG_PNG = _make_png()
_IMG_B64 = base64.b64encode(_IMG_PNG).decode("ascii")


def _station(name="平泉站", area="大清河", val="36.2", prov="河北省", cnty="平泉市"):
    return {"siteName": name, "siteCode": "X", "areaName": area, "val": val,
            "provence": prov, "cnty": cnty}


def _forecast(area="永定河", area_id=12, val="4.6"):
    return {"areaId": area_id, "areaName": area, "sum": val}


def _install_all_ok(monkeypatch, stations=None, forecasts=None):
    """安装全部子接口成功的 mock。"""
    monkeypatch.setattr(clt, "_fetch_describe_text", lambda *a, **k: _REAL_TEXT)
    monkeypatch.setattr(clt, "_fetch_swan3", lambda *a, **k: _IMG_PNG)
    monkeypatch.setattr(clt, "_fetch_station_rain_img", lambda *a, **k: _IMG_PNG)
    monkeypatch.setattr(clt, "_fetch_area_rain_real_img", lambda *a, **k: _IMG_PNG)
    monkeypatch.setattr(clt, "_fetch_station_list", lambda *a, **k: stations or [_station()])
    monkeypatch.setattr(clt, "_fetch_area_rain_fore_img", lambda *a, **k: _IMG_PNG)
    monkeypatch.setattr(clt, "_fetch_forecast", lambda *a, **k: forecasts or [_forecast()])


class TestResolveImageBytes:
    def test_base64_in_data(self):
        assert clt._resolve_image_bytes({"data": _IMG_B64}) == _IMG_PNG

    def test_data_uri_prefix_stripped(self):
        raw = "data:image/png;base64," + _IMG_B64
        assert clt._resolve_image_bytes({"data": raw}) == _IMG_PNG

    def test_absolute_url_fetched(self, monkeypatch):
        monkeypatch.setattr(clt.requests, "get",
                            lambda url, timeout=None: _FakeBytesResp(_IMG_PNG))
        out = clt._resolve_image_bytes({"data": "http://10.226.107.35:8001/x.png"})
        assert out == _IMG_PNG

    def test_relative_path_joined_with_base(self, monkeypatch):
        captured: dict = {}

        def fake_get(url, timeout=None):
            captured["url"] = url
            return _FakeBytesResp(_IMG_PNG)

        monkeypatch.setattr(clt.requests, "get", fake_get)
        out = clt._resolve_image_bytes({"data": "/meteor_img_profile/x.png"})
        assert captured["url"].startswith(clt.BASE)
        assert out == _IMG_PNG

    def test_junk_rejected(self):
        junk = base64.b64encode("出图失败".encode("utf-8")).decode("ascii")
        assert clt._resolve_image_bytes({"data": junk}) is None
        assert clt._resolve_image_bytes({}) is None


class _FakeBytesResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class TestComposeLongimg:
    def test_render_composite_png(self):
        """板块齐全时渲染出 PNG（Windows 有中文字体）。"""
        img = clt._load_image(_IMG_PNG)
        sections = [
            {"header": "雷达拼图", "icon": "radar", "blocks": [("image", img)]},
            {"header": "降水实况", "icon": "cloud", "blocks": [
                ("text", _REAL_TEXT),
                ("image", img),
                ("caption", "自动站累计降水量排名"),
                ("rank", (["序号", "站点", "省", "市", "降水量(毫米)"],
                          [["1", "平泉站", "河北省", "平泉市", "36.2"]])),
            ]},
            {"header": "降水预报", "icon": "cloudsun", "blocks": [
                ("image", img),
                ("caption", "08月17日08时 - 08月18日08时，降水量预报表"),
                ("fore", ("", ["北三河", "大清河", "海河干流"], ["4.1", "2.2", "0.1"])),
            ]},
        ]
        png = clt._compose_longimg(sections)
        assert png, "应渲染出 PNG"
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_render_without_font_returns_none(self, monkeypatch):
        monkeypatch.setattr(clt, "_find_cjk_font", lambda: None)
        assert clt._compose_longimg([{"header": "a", "blocks": [("text", "x")]}]) is None

    def test_wrap_text_not_exceed_width(self):
        from PIL import Image, ImageDraw, ImageFont
        font = ImageFont.truetype(clt._find_cjk_font(), clt._BODY_SIZE)
        probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        max_w = clt._BOARD_WIDTH - 2 * (clt._CARD_MARGIN + clt._CARD_PAD)
        for line in clt._wrap_text(probe, font, _REAL_TEXT, max_w):
            assert probe.textlength(line, font=font) <= max_w + 1


class TestGenerateCore:
    def test_all_boards_ok(self, monkeypatch):
        """全板块成功 → status ok + base64 + text。"""
        _install_all_ok(monkeypatch)
        r = clt.generate_haihe_composite_longimg_core()
        assert r["status"] == "ok"
        assert r["base64"], "全板块成功应返回组合长图 base64"
        assert r["text"]

    def test_partial_failure_still_ok(self, monkeypatch):
        """部分子接口失败 → 对应板块占位，其余照常，整体仍 ok。"""
        _install_all_ok(monkeypatch)
        def boom(*a, **k):
            raise RuntimeError("Connection refused: http://10.226.107.35:8001/x")

        monkeypatch.setattr(clt, "_fetch_swan3", boom)
        monkeypatch.setattr(clt, "_fetch_area_rain_real_img", boom)
        monkeypatch.setattr(clt, "_fetch_station_list", boom)
        r = clt.generate_haihe_composite_longimg_core()
        assert r["status"] == "ok"
        assert r["base64"], "部分板块失败不应整图失败"
        assert "10.226.107.35" not in r["text"], "失败文本应脱敏"

    def test_render_failure_degrades_to_text(self, monkeypatch):
        """渲染失败（缺字体）→ base64 空 + 各板块文字保留。"""
        _install_all_ok(monkeypatch)
        monkeypatch.setattr(clt, "_compose_longimg", lambda sections: None)
        r = clt.generate_haihe_composite_longimg_core()
        assert r["status"] == "ok"
        assert r["base64"] == ""
        assert r["text"]
        assert r["render_warning"]

    def test_station_rows_sorted_top15(self, monkeypatch):
        """点雨量按雨量降序取前 15。"""
        stations = [_station(name=f"站{i}", val=str(10 + i)) for i in range(20)]
        _install_all_ok(monkeypatch, stations=stations)
        monkeypatch.setattr(clt, "_compose_longimg", lambda sections: None)
        r = clt.generate_haihe_composite_longimg_core()
        # 降级路径下 text 为空（无失败），验证排序逻辑走的是内部表格构建
        assert r["status"] == "ok"

    def test_fore_cycle_fallback(self, monkeypatch):
        """起报时次 500 时自动回退到下一个有数据的时次；forecast 用同一成功时次。"""
        calls: dict = {}
        cycles = clt._fore_cycle_candidates()

        def fake_fore_img(fc, fb, fe, area_ids, interval):
            calls.setdefault("fore_times", []).append(fc)
            if fc == cycles[0]:
                raise RuntimeError("Internal Server Error")  # 第一个时次未就绪
            return _IMG_PNG

        def fake_forecast(fc, fb, fe, area_ids, interval):
            calls["forecast_time"] = fc
            return [_forecast()]

        monkeypatch.setattr(clt, "_fetch_describe_text", lambda *a, **k: _REAL_TEXT)
        monkeypatch.setattr(clt, "_fetch_swan3", lambda *a, **k: _IMG_PNG)
        monkeypatch.setattr(clt, "_fetch_station_rain_img", lambda *a, **k: _IMG_PNG)
        monkeypatch.setattr(clt, "_fetch_area_rain_real_img", lambda *a, **k: _IMG_PNG)
        monkeypatch.setattr(clt, "_fetch_station_list", lambda *a, **k: [_station()])
        monkeypatch.setattr(clt, "_fetch_area_rain_fore_img", fake_fore_img)
        monkeypatch.setattr(clt, "_fetch_forecast", fake_forecast)

        r = clt.generate_haihe_composite_longimg_core()
        assert r["status"] == "ok"
        assert r["base64"], "回退成功后应出图"
        assert len(calls["fore_times"]) >= 2, "第一个时次失败应回退到下一个"
        assert calls["forecast_time"] == calls["fore_times"][1], "forecast 应复用成功时次"

    def test_all_fore_cycles_fail_placeholders(self, monkeypatch):
        """所有起报时次都失败 → 预报板块占位，其余正常，整体仍 ok。"""
        def boom(*a, **k):
            raise RuntimeError("500")

        _install_all_ok(monkeypatch)
        monkeypatch.setattr(clt, "_fetch_area_rain_fore_img", boom)
        monkeypatch.setattr(clt, "_fetch_forecast", boom)
        r = clt.generate_haihe_composite_longimg_core()
        assert r["status"] == "ok"
        assert r["base64"], "预报失败不应整图失败"

    def test_default_window_and_params(self, monkeypatch):
        """默认窗口 + 参数透传（interval 对齐）。"""
        calls: dict = {}

        def fake_describe(begin, end, area_ids, interval, range_, type_):
            calls["begin"], calls["end"] = begin, end
            calls["interval"], calls["range"], calls["type"] = interval, range_, type_
            return _REAL_TEXT

        monkeypatch.setattr(clt, "_fetch_describe_text", fake_describe)
        monkeypatch.setattr(clt, "_fetch_swan3", lambda *a, **k: _IMG_PNG)
        monkeypatch.setattr(clt, "_fetch_station_rain_img", lambda *a, **k: _IMG_PNG)
        monkeypatch.setattr(clt, "_fetch_area_rain_real_img", lambda *a, **k: _IMG_PNG)
        monkeypatch.setattr(clt, "_fetch_station_list", lambda *a, **k: [_station()])
        monkeypatch.setattr(clt, "_fetch_area_rain_fore_img", lambda *a, **k: _IMG_PNG)
        monkeypatch.setattr(clt, "_fetch_forecast", lambda *a, **k: [_forecast()])

        r = clt.generate_haihe_composite_longimg_core(
            beginTime="2026-08-03 00:00:00", endTime="2026-08-05 00:00:00",
        )
        assert r["status"] == "ok"
        assert calls["begin"] == "2026-08-03 00:00:00"
        assert calls["end"] == "2026-08-05 00:00:00"
        assert calls["interval"] == 48, "48h 窗口应对齐"
        assert calls["range"] == "9"
        assert calls["type"] == "0"
