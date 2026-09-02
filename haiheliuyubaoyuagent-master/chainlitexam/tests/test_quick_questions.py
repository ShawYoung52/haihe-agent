"""按角色查询快捷问题测试。

2026-09-02 前端同事需求：「查询指定角色对应的快捷问题」接口。

设计：
- 内容以后端自带 `chainlitexam/config/quickQA.json` 为事实源（随 Chainlit 服务部署），
  `quick_questions` 只读、不读 AgentWeb 那份（AgentWeb 服务器上独立部署、与 Chainlit 不在一起）。
- 角色 → 可见分区策略在 `quick_questions._ROLE_SECTION_IDS`：
  admin / forecaster 全量 8 区；external（公众）只看 01天气资讯/03文旅出行/04气象科普/08系统问答。
- 端点 `GET /api/v1/qa/quick-questions?role=...`：显式 role 优先；不传时回退 cookie JWT
  metadata.role；都没有按 external 兜底。显式非法 role → 400。
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import quick_questions as qq  # noqa: E402

REPO_QUICK_QA = Path(__file__).resolve().parents[1] / "config" / "quickQA.json"


@pytest.fixture(autouse=True)
def _reset_cache():
    qq.reset_cache()
    yield
    qq.reset_cache()


def _write_config(tmp_path, sections):
    path = tmp_path / "quickQA.json"
    path.write_text(json.dumps({"sections": sections}, ensure_ascii=False), encoding="utf-8")
    return path


def _section(sid, n_questions=2):
    return {
        "id": sid,
        "type": f"{sid:02d} 类型",
        "sub": f"(子{sid})",
        "iconKey": "decision",
        "isOpen": False,
        "questions": [f"问题{sid}-{i}" for i in range(n_questions)],
    }


# ---------------------------------------------------------------------------
# 纯模块逻辑（quick_questions，无 chain_gzt 重依赖）
# ---------------------------------------------------------------------------


def test_external_sees_only_public_sections():
    result = qq.get_quick_questions("external")
    ids = [s["id"] for s in result["sections"]]
    assert ids == [1, 3, 4, 8]
    assert result["role"] == "external"


def test_forecaster_and_admin_see_all_sections():
    for role in ("forecaster", "admin"):
        result = qq.get_quick_questions(role)
        assert len(result["sections"]) == 8
        assert result["role"] == role


def test_role_is_case_insensitive_and_stripped():
    result = qq.get_quick_questions("  External ")
    assert result["role"] == "external"
    assert [s["id"] for s in result["sections"]] == [1, 3, 4, 8]


def test_unknown_or_empty_role_falls_back_to_external():
    for role in ("", "  ", "superadmin", "root"):
        result = qq.get_quick_questions(role)
        assert result["role"] == "external"
        assert [s["id"] for s in result["sections"]] == [1, 3, 4, 8]


def test_result_is_deep_copied_from_cache():
    first = qq.get_quick_questions("admin")
    first["sections"][0]["questions"].append("HACK")
    first["sections"][0]["type"] = "HACK"
    second = qq.get_quick_questions("admin")
    assert "HACK" not in second["sections"][0]["questions"]
    assert second["sections"][0]["type"] != "HACK"


def test_missing_config_returns_empty_sections(monkeypatch, tmp_path):
    monkeypatch.setenv(qq.CONFIG_PATH_ENV, str(tmp_path / "nope.json"))
    result = qq.get_quick_questions("admin")
    assert result["sections"] == []


def test_malformed_config_returns_empty_sections(monkeypatch, tmp_path):
    bad = tmp_path / "quickQA.json"
    bad.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv(qq.CONFIG_PATH_ENV, str(bad))
    assert qq.get_quick_questions("admin")["sections"] == []


def test_custom_config_path_filters_by_role(monkeypatch, tmp_path):
    cfg = _write_config(tmp_path, [_section(1), _section(2), _section(5)])
    monkeypatch.setenv(qq.CONFIG_PATH_ENV, str(cfg))
    external = qq.get_quick_questions("external")
    assert [s["id"] for s in external["sections"]] == [1]  # 2/5 不在 external 白名单
    admin = qq.get_quick_questions("admin")
    assert [s["id"] for s in admin["sections"]] == [1, 2, 5]


def test_repo_quick_qa_config_shape():
    """后端自带 config/quickQA.json：8 分区、字段齐全、id 唯一，映射引用的 id 都存在。"""
    data = json.loads(REPO_QUICK_QA.read_text(encoding="utf-8"))
    sections = data["sections"]
    assert len(sections) == 8
    ids = [s["id"] for s in sections]
    assert len(set(ids)) == len(ids)
    for s in sections:
        for key in ("id", "type", "sub", "iconKey", "isOpen", "questions"):
            assert key in s
        assert isinstance(s["questions"], list) and s["questions"]
    # _ROLE_SECTION_IDS 引用的 external 白名单 id 必须都在真实配置里
    assert qq._ROLE_SECTION_IDS["external"] <= set(ids)


# ---------------------------------------------------------------------------
# 端点函数（chain_gzt._qa_quick_questions）——复用 test_user_region 的 stub 前导
# ---------------------------------------------------------------------------

os.environ.setdefault("CHAINLIT_ENABLE_DB", "0")

pytest.importorskip("chainlit.data", reason="chain_gzt tests require the real Chainlit package")

for _mod, _cls in (
    ("langchain_mcp_adapters", "MultiServerMCPClient"),
    ("langchain_mcp_adapters.client", "MultiServerMCPClient"),
    ("langchain_openai", "ChatOpenAI"),
):
    if _mod not in sys.modules:
        m = types.ModuleType(_mod)
        setattr(m, _cls, type(_cls, (), {}))
        sys.modules[_mod] = m

try:
    import psycopg2  # noqa: F401
except ImportError:
    _pg = types.ModuleType("psycopg2")
    _pg.connect = lambda *args, **kwargs: None
    _pg_extras = types.ModuleType("psycopg2.extras")
    _pg_extras.RealDictCursor = object
    _pg_pool = types.ModuleType("psycopg2.pool")
    _pg_pool.ThreadedConnectionPool = object
    _pg.extras = _pg_extras
    _pg.pool = _pg_pool
    sys.modules["psycopg2"] = _pg
    sys.modules["psycopg2.extras"] = _pg_extras
    sys.modules["psycopg2.pool"] = _pg_pool

try:
    import matplotlib.pyplot  # noqa: F401
except ImportError:
    _mpl = types.ModuleType("matplotlib")
    _plt = types.ModuleType("matplotlib.pyplot")
    _plt.rcParams = {}
    _fm = types.ModuleType("matplotlib.font_manager")
    _fm.fontManager = types.SimpleNamespace(addfont=lambda *args, **kwargs: None)
    _fm.FontProperties = lambda **kwargs: types.SimpleNamespace(get_name=lambda: "sans")
    _mpl.pyplot = _plt
    _mpl.font_manager = _fm
    sys.modules["matplotlib"] = _mpl
    sys.modules["matplotlib.pyplot"] = _plt
    sys.modules["matplotlib.font_manager"] = _fm

if "chainlit.data.sql_alchemy" not in sys.modules:
    _cl_sql = types.ModuleType("chainlit.data.sql_alchemy")
    _cl_sql.SQLAlchemyDataLayer = type("SQLAlchemyDataLayer", (), {})
    sys.modules["chainlit.data.sql_alchemy"] = _cl_sql

import chain_gzt as cg  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _FakeRequest:
    def __init__(self, cookies=None):
        self.cookies = cookies or {}


def test_endpoint_explicit_role_filters():
    resp = cg._qa_quick_questions(_FakeRequest(), role="external")
    assert resp["code"] == 200
    assert resp["data"]["role"] == "external"
    assert [s["id"] for s in resp["data"]["sections"]] == [1, 3, 4, 8]


def test_endpoint_explicit_role_all_sections():
    resp = cg._qa_quick_questions(_FakeRequest(), role="forecaster")
    assert len(resp["data"]["sections"]) == 8


def test_endpoint_invalid_role_returns_400():
    with pytest.raises(HTTPException) as exc:
        cg._qa_quick_questions(_FakeRequest(), role="superadmin")
    assert exc.value.status_code == 400


def test_endpoint_no_role_falls_back_to_caller_role(monkeypatch):
    monkeypatch.setattr(cg, "_resolve_caller_role", lambda req: "forecaster")
    resp = cg._qa_quick_questions(_FakeRequest(), role=None)
    assert resp["data"]["role"] == "forecaster"
    assert len(resp["data"]["sections"]) == 8


def test_endpoint_blank_role_treated_as_not_provided(monkeypatch):
    """前端无条件拼 ?role= 空值时按未提供处理，回退 cookie/external，不 400。"""
    monkeypatch.setattr(cg, "_resolve_caller_role", lambda req: "forecaster")
    for blank in ("", "   "):
        resp = cg._qa_quick_questions(_FakeRequest(), role=blank)
        assert resp["data"]["role"] == "forecaster"
        assert len(resp["data"]["sections"]) == 8


def test_resolve_caller_role_no_cookie_defaults_external():
    assert cg._resolve_caller_role(_FakeRequest()) == "external"
