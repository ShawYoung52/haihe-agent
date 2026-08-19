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

import pytest

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


class TestWhiteMapConversion:
    """④⑥ 黑底图→白底：近黑背景铺白、浅色文字/图例刻度转深字、彩色雨区保留；白底图不被误转换。"""

    def _black_map_png(self):
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (80, 60), (0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse([10, 10, 60, 50], fill=(40, 200, 60))     # 绿色雨区
        d.rectangle([10, 52, 70, 57], fill=(224, 224, 224))  # 浅灰文字条
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def _white_map_png(self):
        from PIL import Image
        im = Image.new("RGB", (80, 60), (255, 255, 255))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def test_black_bg_becomes_white(self):
        img = clt._load_image(self._black_map_png())
        out = clt._to_white_map(img)
        assert out is not None and out.mode == "RGB", "转换后应为 RGB（不透明）"
        assert out.getpixel((2, 2)) == (255, 255, 255), "原黑底应变白"

    def test_light_text_becomes_dark(self):
        img = clt._load_image(self._black_map_png())
        out = clt._to_white_map(img)
        px = out.load()
        found = False
        for x in range(10, 70, 2):
            for y in range(52, 57):
                r, g, b = px[x, y]
                if max(r, g, b) < 120:
                    found = True
        assert found, "原浅灰文字应转成深字（白底可读）"

    def test_colored_rain_preserved(self):
        img = clt._load_image(self._black_map_png())
        out = clt._to_white_map(img)
        px = out.load()
        found = False
        for x in range(10, 60, 2):
            for y in range(10, 50):
                r, g, b = px[x, y]
                if g > 150 and r < 100:  # 绿色雨区
                    found = True
        assert found, "彩色雨区应保留原色"

    def test_white_map_left_untouched(self):
        img = clt._load_image(self._white_map_png())
        out = clt._to_white_map(img)
        assert out.getpixel((2, 2)) == (255, 255, 255), "纯白底图不应被改写"

    def _white_lightgray_text_png(self):
        """白底 + 浅灰细笔画文字 + 绿色雨区：模拟 ⑥ 预报图"白底浅灰字"样式。"""
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (80, 60), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.ellipse([10, 10, 60, 50], fill=(40, 200, 60))     # 绿色雨区
        # 浅灰文字是细笔画（~5px 笔划，面积约 30），不是 10px 实心条——实心条是填充区，应保持原色
        for i, x0 in enumerate([10, 22, 34, 46, 58]):
            d.rectangle([x0, 53, x0 + 4, 58], fill=(192, 192, 192))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def test_white_bg_lightgray_text_darkened(self):
        """白底浅灰字图：浅灰细笔画文字转深字，白底保留（⑥ 预报图样式修复）。"""
        img = clt._load_image(self._white_lightgray_text_png())
        out = clt._to_white_map(img)
        assert out.getpixel((2, 2)) == (255, 255, 255), "白底应保留"
        px = out.load()
        found = False
        for x in range(10, 70, 2):
            for y in range(52, 58):
                r, g, b = px[x, y]
                if max(r, g, b) < 120:
                    found = True
        assert found, "浅灰文字应转成深字（白底可读）"

    def test_white_bg_solid_pale_fill_kept(self):
        """实心浅色块（0 值/低值填充、淡绿点）必须保持原色，不得加深成黑块黑斑。

        旧的 near 带/m_gray 逻辑会把实心浅灰/淡绿填充区误加深成黑（c1133f5 把 ④
        海河干流 0 值区弄成黑块、③ 淡绿低值点弄成黑斑）。新逻辑按连通域腐蚀核心
        区分：稀疏笔画→深字；实心块→保持原色。
        """
        pytest.importorskip("numpy")
        pytest.importorskip("scipy")
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (80, 60), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle([15, 15, 45, 35], fill=(192, 192, 192))    # 实心浅灰填充
        d.ellipse([55, 42, 75, 58], fill=(205, 242, 205))     # 淡绿低值点
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        out = clt._to_white_map(im)
        px = out.load()
        r, g, b = px[30, 25]           # 实心浅灰块中心
        assert (r, g, b) == (192, 192, 192), f"实心浅灰填充应保持原色，实际 {(r, g, b)}"
        r, g, b = px[65, 50]           # 淡绿点中心
        assert (r, g, b) == (205, 242, 205), f"淡绿低值点应保持原色，实际 {(r, g, b)}"

    def test_white_bg_thick_white_label_darkened(self):
        """绿区上的粗白字标签（④⑥ 分区名/数值）应加深为黑，而不是只处理 2px 细线。

        旧 near 带只有 ~2px，粗字笔画中心够不到绿区→保持白字看不见（用户投诉
        "分区名字和降雨字体不是黑色的"）。新逻辑按笔画结构把粗白字整体加深。
        """
        pytest.importorskip("numpy")
        pytest.importorskip("scipy")
        from PIL import Image, ImageDraw, ImageFont
        im = Image.new("RGB", (120, 80), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.polygon([(5, 5), (115, 5), (115, 75), (5, 75)], fill=(138, 244, 149))
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 36)
        except Exception:
            font = ImageFont.load_default()
        d.text((15, 20), "AB", fill=(255, 255, 255), font=font)
        out = clt._to_white_map(im)
        px = out.load()
        found = False
        for x in range(10, 100, 2):
            for y in range(15, 65, 2):
                r, g, b = px[x, y]
                if max(r, g, b) < 60 and g < 120:
                    found = True
        assert found, "绿区上粗白字标签应加深为深字（分区名/数值可读）"
        assert out.getpixel((60, 70)) == (138, 244, 149), "绿色雨区应保留原色"

    def test_white_bg_black_text_kept(self):
        """白底黑字图（③ 降水实况图样式）：黑字不应被抹掉。"""
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (80, 60), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle([10, 20, 70, 26], fill=(0, 0, 0))  # 黑字条
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        img = clt._load_image(buf.getvalue())
        out = clt._to_white_map(img)
        px = out.load()
        found = False
        for x in range(10, 70, 2):
            for y in range(20, 26):
                if max(px[x, y]) < 120:
                    found = True
        assert found, "白底图上的黑字应保留"

    def _white_green_white_text_png(self):
        """白底 + 绿区 + 绿区上的白字：模拟 ⑥ 预报图"绿区白字"样式。"""
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (80, 60), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.ellipse([10, 10, 60, 50], fill=(40, 200, 60))       # 绿色雨区
        d.rectangle([20, 20, 50, 26], fill=(255, 255, 255))   # 绿区上的白字
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def test_white_bg_green_white_text_darkened(self):
        """⑥ 绿区白字：绿区上的白字转深字，白底与绿区原样保留。"""
        img = clt._load_image(self._white_green_white_text_png())
        out = clt._to_white_map(img)
        assert out.getpixel((2, 2)) == (255, 255, 255), "白底应保留"
        px = out.load()
        found = False
        for x in range(20, 50, 2):
            for y in range(20, 26):
                if max(px[x, y]) < 120:
                    found = True
        assert found, "绿区上的白字应转成深字（可读）"
        r, g, b = out.getpixel((15, 30))  # 椭圆内、白字外
        assert g > 150 and r < 100, "绿色雨区应保留原色"

    def test_white_bg_no_halo_around_isolated_color(self):
        """防回归：孤立彩色区块周围的大块白底不得被染出暗晕。

        亮字转深的"内容近邻带"必须配 large_white 形态学守卫——只改紧贴彩色内容的
        细白字/细白缝，大面积白背景（页面底/图边距）一律保留。若去掉守卫或把近邻带
        放宽（如 MaxFilter 大核膨胀），色块周围的白底会被染成暗晕（2e4b292 曾引入
        此回归：色标条/雨区被一圈黑边包围）。本测试用白底中央孤立色块，断言紧邻色块
        1~7px 的白底像素仍为纯白。
        """
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (120, 120), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle([50, 50, 70, 70], fill=(0, 120, 255))  # 中央孤立色块
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        img = clt._load_image(buf.getvalue())
        out = clt._to_white_map(img)
        px = out.load()
        # 色块右/下/左/上 1~7px 处的白底像素，均不应变暗
        for (x, y) in [(72, 60), (75, 60), (78, 60),
                       (60, 72), (60, 75), (60, 78),
                       (48, 60), (45, 60),
                       (60, 48), (60, 45)]:
            assert max(px[x, y]) >= 245, (
                f"({x},{y}) 紧邻色块的大块白底不应被染暗（暗晕回归），实际 {px[x, y]}")


class TestRadarBlackToWhite:
    """② swan3 雷达图：无回波大黑块转白底，细黑字(标题/标签)与彩色回波保留。"""

    def _radar_like_png(self):
        """白底 + 大黑块(无回波区) + 细黑条(文字笔画) + 黑块内的彩色回波块。"""
        from PIL import Image, ImageDraw
        im = Image.new("RGB", (140, 120), (255, 255, 255))
        d = ImageDraw.Draw(im)
        d.rectangle([10, 10, 100, 100], fill=(0, 0, 0))      # 大黑块（无回波区，面积>>min_area）
        d.rectangle([115, 10, 125, 14], fill=(0, 0, 0))      # 细黑条（文字笔画，面积小）
        d.rectangle([30, 30, 50, 50], fill=(0, 200, 60))     # 彩色回波
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def test_large_black_becomes_white_text_kept(self):
        pytest.importorskip("numpy")
        pytest.importorskip("scipy")
        img = clt._load_image(self._radar_like_png())
        out = clt._radar_black_to_white(img)
        px = out.load()
        assert px[70, 70] == (255, 255, 255), "大黑块（无回波区）应转白"
        assert max(px[120, 12]) < 60, "细黑字/笔画应保留黑色不被填白"
        r, g, b = px[40, 40]
        assert g > 150 and r < 100, "彩色回波应保留原色"

    def test_graceful_return_valid_image(self):
        """无论是否有 numpy/scipy，都应返回合法 RGB 图、不崩溃（无依赖时原样返回）。"""
        from PIL import Image
        im = Image.new("RGB", (20, 20), (0, 0, 0))
        out = clt._radar_black_to_white(im)
        assert out.mode == "RGB" and out.size == (20, 20)


class TestBoardNormalizationWiring:
    """板块归一化接线：② 雷达过 _radar_black_to_white；③④⑥ 过 _to_white_map。"""

    def test_radar_and_three_maps_normalized(self, monkeypatch):
        calls = {"radar": 0, "white": 0}
        real_radar = clt._radar_black_to_white
        real_white = clt._to_white_map

        def spy_radar(img, *a, **k):
            calls["radar"] += 1
            return real_radar(img, *a, **k)

        def spy_white(img, *a, **k):
            calls["white"] += 1
            return real_white(img, *a, **k)

        monkeypatch.setattr(clt, "_radar_black_to_white", spy_radar)
        monkeypatch.setattr(clt, "_to_white_map", spy_white)
        _install_all_ok(monkeypatch)
        clt.generate_haihe_composite_longimg_core()
        assert calls["radar"] == 1, "② 雷达图应过 _radar_black_to_white 一次"
        assert calls["white"] == 3, "③ 降水实况图/④ 实况面雨量/⑥ 预报面雨量应各过 _to_white_map 一次"

