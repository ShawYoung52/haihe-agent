"""HTTP images 字段带 14所代理图 URL + _scrub 图片代理 allowlist 测试。

口径（用户确认）：出图结果以代理 URL 展示；答案里 markdown 图链；HTTP images 字段也带 URL。
安全边界：_scrub 默认只放行图片代理主机（10.226.107.35:8080）的 URL，其余内网 IP 照常脱敏。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Skip SQLAlchemyDataLayer init at import time (avoid asyncpg dep)
os.environ["CHAINLIT_ENABLE_DB"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

import qa_http_api

PROXY = "http://10.226.107.35:8080/hhly/img/2026/08/12/DYPQ/ECMF/a.png"


class _FakeEmitter:
    def __init__(self, elements):
        self.elements = elements


class _FakeSession:
    def __init__(self, files):
        self.files = files


class TestBuildImagePayloadExternal:
    def test_appends_external_markdown_url_keeps_local_file(self, monkeypatch, tmp_path):
        """本地文件元素照常输出，answer 里放行主机的 markdown 图链追加为外部 images 条目。"""
        img = tmp_path / "img.png"
        img.write_bytes(b"x")
        files = {"el1": {"path": str(img), "type": "image/png"}}
        emitter = _FakeEmitter([{"type": "image", "chainlitKey": "el1", "name": "本地图"}])
        session = _FakeSession(files)

        images = qa_http_api._build_image_payload(
            emitter, session, answer=f"![9分区图]({PROXY}) 说明文字"
        )
        urls = [i["url"] for i in images]
        assert any(u.startswith("/api/v1/qa/files/") for u in urls), "本地文件元素应保留"
        assert PROXY in urls, "放行主机的 markdown 图链 URL 应进入 images 字段"
        ext = next(i for i in images if i["url"].startswith("http"))
        assert ext["name"] == "9分区图"
        assert ext["mime"] == "image/png"

    def test_disallowed_host_markdown_url_not_appended(self, monkeypatch, tmp_path):
        """非 allowlist 主机的 markdown 图链不进 images（与 _scrub 脱敏边界一致）。"""
        monkeypatch.setattr(qa_http_api, "_IMAGE_URL_ALLOW_HOSTS", ["10.226.107.35:8080"])
        images = qa_http_api._build_image_payload(
            _FakeEmitter([]), _FakeSession({}),
            answer="![内网图](http://10.226.107.36:8080/hhly/x.png)",
        )
        assert images == [], "非 allowlist 主机的图链不应进 images 字段"

    def test_no_markdown_image_unchanged(self, tmp_path):
        """answer 无图链 → 行为与之前一致（仅本地文件条目）。"""
        img = tmp_path / "img.png"
        img.write_bytes(b"x")
        files = {"el1": {"path": str(img), "type": "image/png"}}
        emitter = _FakeEmitter([{"type": "image", "chainlitKey": "el1", "name": "本地图"}])
        images = qa_http_api._build_image_payload(_FakeEmitter(emitter.elements), _FakeSession(files), answer="没有图片的答案")
        assert len(images) == 1
        assert images[0]["url"].startswith("/api/v1/qa/files/")


class TestScrubImageProxyAllowlist:
    def test_scrub_preserves_allowed_proxy_url(self, monkeypatch):
        """默认 allowlist 放行 14所图片代理 URL；其它内网 IP 照常脱敏。"""
        monkeypatch.setattr(qa_http_api, "_IMAGE_URL_ALLOW_HOSTS", ["10.226.107.35:8080"])
        out = qa_http_api._scrub(
            f"图 {PROXY} 其它 http://10.1.2.3:9999/x 纯IP 10.9.8.7"
        )
        assert PROXY in out, "允许的图片代理 URL 应保留"
        assert "10.1.2.3" not in out, "未允许的内网 IP 应脱敏"
        assert "10.9.8.7" not in out, "裸内网 IP 应脱敏"

    def test_scrub_still_removes_disallowed_proxy(self, monkeypatch):
        """不在 allowlist 的图片主机照常脱敏。"""
        monkeypatch.setattr(qa_http_api, "_IMAGE_URL_ALLOW_HOSTS", ["10.226.107.35:8080"])
        out = qa_http_api._scrub("http://10.226.107.36:8080/hhly/x.png")
        assert "10.226.107.36" not in out, "未允许的图片主机应脱敏"

    def test_scrub_allowlist_host_boundary(self, monkeypatch):
        """allowlist 是主机边界匹配，:8080x 等畸形前缀不放行。"""
        monkeypatch.setattr(qa_http_api, "_IMAGE_URL_ALLOW_HOSTS", ["10.226.107.35:8080"])
        out = qa_http_api._scrub("http://10.226.107.35:8080x/hhly/a.png")
        assert "10.226.107.35:8080x" not in out, "主机后带非法字符不应放行"
