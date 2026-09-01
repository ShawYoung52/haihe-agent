"""rest_api 区局归属字段（region）parity 测试。

2026-09-01 用户口径：10 区局账号登录后要传 region/region_label 给前端区分页面。
rest_api（MCP 侧 REST 服务）与 chainlitexam/chain_gzt.py 操作同一张 hh_user_account，
两边契约必须一致（chainlitexam/tests/test_user_region.py 锁定另一侧）。

rest_api 顶层 import haihe_mcp_tools/tools/networkx（重依赖链，测试 venv 无 osgeo），
本文件只对真实 import 失败的模块打 stub——若全量套件里其它测试已导入真实模块，
setdefault/try 逻辑不会 clobber。
"""

import sys
import types
from pathlib import Path

import pytest

MCP_DIR = Path(__file__).resolve().parents[1]
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))


def _stub_if_unimportable(name: str, **attrs):
    """真实模块可导入则用真实模块；失败才注册 stub（避免污染全量套件）。"""
    try:
        __import__(name)
        return
    except Exception:
        pass
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


_stub_if_unimportable("networkx")
_stub_if_unimportable("haihe_mcp_tools", evaluate_emergency_response_core=lambda *a, **k: {})
_stub_if_unimportable(
    "tools",
    get_graph=lambda *a, **k: None,
    get_edge_river_name=lambda *a, **k: "",
    get_edge_length_km=lambda *a, **k: 0.0,
)

import rest_api  # noqa: E402
from fastapi import HTTPException  # noqa: E402


EXPECTED_REGION_LABELS = {
    "xiqing": "西青",
    "dongli": "东丽",
    "jinnan": "津南",
    "beichen": "北辰",
    "binhai": "滨海",
    "ninghe": "宁河",
    "jinghai": "静海",
    "jizhou": "蓟州",
    "wuqing": "武清",
    "baodi": "宝坻",
}


class _FakeCursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, row=None, rows=None):
        self._cursor = _FakeCursor(row, rows)
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, **kwargs):
        return self._cursor

    def commit(self):
        self.committed = True


@pytest.fixture
def no_ensure_tables(monkeypatch):
    """跳过建表逻辑。"""
    monkeypatch.setattr(rest_api, "_ensure_auth_tables", lambda: None)


def _account_row(username="xiqing", password="XIQING-2026", role="forecaster", region="xiqing"):
    return {
        "username": username,
        "password_hash": rest_api._hash_password(password),
        "role": role,
        "status": "active",
        "region": region,
    }


def test_region_labels_match_chainlit_side():
    """与 chain_gzt.REGION_LABELS 同值（两侧独立维护，改一边必须同步另一边）。"""
    assert rest_api.REGION_LABELS == EXPECTED_REGION_LABELS


def test_validate_region_accepts_empty_and_known():
    assert rest_api._validate_region("") == ""
    assert rest_api._validate_region(None) == ""
    assert rest_api._validate_region("  BINHAI ") == "binhai"


def test_validate_region_rejects_unknown():
    with pytest.raises(HTTPException) as excinfo:
        rest_api._validate_region("nankai")
    assert excinfo.value.status_code == 400


def test_login_returns_region(no_ensure_tables, monkeypatch):
    """区局账号登录响应带 region/region_label（前端按它区分页面）。"""
    monkeypatch.setattr(rest_api, "_get_pg_conn", lambda: _FakeConn(_account_row()))

    out = rest_api.login(rest_api.LoginRequest(username="xiqing", password="XIQING-2026"))

    assert out["data"]["region"] == "xiqing"
    assert out["data"]["region_label"] == "西青"
    assert out["data"]["role"] == "forecaster"
    assert out["data"]["role_label"] == "预报员"


def test_login_without_region_returns_none(no_ensure_tables, monkeypatch):
    """非区局账号（region NULL）：region/region_label 为 None，不影响登录。"""
    monkeypatch.setattr(
        rest_api,
        "_get_pg_conn",
        lambda: _FakeConn(_account_row(username="admin", password="admin123", role="admin", region=None)),
    )

    out = rest_api.login(rest_api.LoginRequest(username="admin", password="admin123"))

    assert out["data"]["region"] is None
    assert out["data"]["region_label"] is None


def test_ensure_auth_tables_adds_region_column(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(rest_api, "_get_pg_conn", lambda: conn)

    rest_api._ensure_auth_tables()

    sql_text = "\n".join(stmt for stmt, _ in conn._cursor.executed)
    assert "ADD COLUMN IF NOT EXISTS region" in sql_text
    assert conn.committed


def test_upsert_user_inserts_region(no_ensure_tables, monkeypatch):
    row = {"username": "jizhou", "role": "forecaster", "status": "active", "region": "jizhou",
           "created_at": None, "updated_at": None}
    conn = _FakeConn(row)
    monkeypatch.setattr(rest_api, "_get_pg_conn", lambda: conn)

    out = rest_api._upsert_user("jizhou", "JIZHOU-2026", "forecaster", allow_admin=True, region="jizhou")

    assert out["region"] == "jizhou"
    assert out["region_label"] == "蓟州"
    insert_sql, params = conn._cursor.executed[0]
    assert "region" in insert_sql
    assert "jizhou" in params


def test_upsert_user_rejects_unknown_region(no_ensure_tables):
    with pytest.raises(HTTPException) as excinfo:
        rest_api._upsert_user("x", "p", "external", allow_admin=False, region="nankai")
    assert excinfo.value.status_code == 400


def test_list_users_includes_region(no_ensure_tables, monkeypatch):
    rows = [
        {"username": "xiqing", "role": "forecaster", "status": "active", "region": "xiqing",
         "created_at": None, "updated_at": None},
        {"username": "admin", "role": "admin", "status": "active", "region": None,
         "created_at": None, "updated_at": None},
    ]
    monkeypatch.setattr(rest_api, "_get_pg_conn", lambda: _FakeConn(rows=rows))
    # _require_admin 走 Depends——直接调被包函数，绕过 FastAPI 依赖注入
    monkeypatch.setattr(rest_api, "_require_admin", lambda: {"username": "admin"})

    out = rest_api.list_users(admin={"username": "admin"})

    by_name = {u["username"]: u for u in out["data"]}
    assert by_name["xiqing"]["region"] == "xiqing"
    assert by_name["xiqing"]["region_label"] == "西青"
    assert by_name["admin"]["region"] is None
