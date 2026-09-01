"""区局归属字段（region）测试。

2026-09-01 用户口径：10 个区局账号（西青/东丽/津南/北辰/滨海/宁河/静海/蓟州/武清/宝坻，
role 都是 forecaster）需要一个字段在登录后传给前端，前端按它区分不同用户展示的页面。

设计：
- `hh_user_account` 加 `region` 列（pinyin key，如 "xiqing"；非区局账号留 NULL）
- 登录 `auth_callback` 把 `region`/`region_label` 放进 `User.metadata` —— Chainlit
  `create_jwt` 编码 `User.to_dict()`，metadata 随 JWT 到前端，前端解 JWT payload 即可读
- `display_name` 有区局时显示区局中文名（西青），否则回退角色中文名（预报员等）
- 用户管理接口（register/create/list/reset-password）接线 region；
  `_validate_region` 只允许 10 区局 key 或留空
"""

import os
import sys
import types
from pathlib import Path

import pytest

# 跳过 SQLAlchemyDataLayer 初始化，避免 asyncpg 依赖（与 test_build_chat_llm.py 一致）
os.environ["CHAINLIT_ENABLE_DB"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("chainlit.data", reason="chain_gzt tests require the real Chainlit package")

# 只 mock 真正缺失的依赖（不 clobber 真实 chainlit）
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


class _FakeCursor:
    """记录执行 SQL 的假 cursor；fetchone 返回预设行。"""

    def __init__(self, row=None):
        self._row = row
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, row=None):
        self._cursor = _FakeCursor(row)
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
def auth_ready(monkeypatch):
    """跳过建表逻辑（_AUTH_TABLES_READY=True）。"""
    monkeypatch.setattr(cg, "_AUTH_TABLES_READY", True)
    return cg


def _account_row(username="xiqing", password="XIQING-2026", role="forecaster", region="xiqing"):
    return {
        "username": username,
        "password_hash": cg._hash_password(password),
        "role": role,
        "status": "active",
        "region": region,
    }


def test_region_labels_cover_ten_districts():
    assert cg.REGION_LABELS == {
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


def test_validate_region_accepts_empty_and_known():
    assert cg._validate_region("") == ""
    assert cg._validate_region(None) == ""
    assert cg._validate_region("xiqing") == "xiqing"
    assert cg._validate_region("  JIZHOU ") == "jizhou"


def test_validate_region_rejects_unknown():
    with pytest.raises(HTTPException) as excinfo:
        cg._validate_region("nankai")
    assert excinfo.value.status_code == 400


def test_auth_callback_returns_region_metadata(auth_ready, monkeypatch):
    """区局账号登录：metadata 带 region/region_label（经 JWT 传前端），display_name 为区局名。"""
    conn = _FakeConn(_account_row())
    monkeypatch.setattr(cg, "_get_chainlit_pg_conn", lambda: conn)

    user = cg.auth_callback("xiqing", "XIQING-2026")

    assert user is not None
    assert user.identifier == "xiqing"
    assert user.display_name == "西青"
    assert user.metadata == {"role": "forecaster", "region": "xiqing", "region_label": "西青"}


def test_auth_callback_without_region_falls_back_to_role_label(auth_ready, monkeypatch):
    """非区局账号（region 为 NULL）：metadata region 为 None，display_name 回退角色名。"""
    conn = _FakeConn(_account_row(username="admin", password="admin123", role="admin", region=None))
    monkeypatch.setattr(cg, "_get_chainlit_pg_conn", lambda: conn)

    user = cg.auth_callback("admin", "admin123")

    assert user is not None
    assert user.display_name == "管理员"
    assert user.metadata == {"role": "admin", "region": None, "region_label": None}


def test_auth_callback_wrong_password_still_rejected(auth_ready, monkeypatch):
    conn = _FakeConn(_account_row())
    monkeypatch.setattr(cg, "_get_chainlit_pg_conn", lambda: conn)
    assert cg.auth_callback("xiqing", "wrong-password") is None


def test_user_payload_includes_region():
    payload = cg._user_payload("xiqing", "forecaster", "active", region="xiqing")
    assert payload["region"] == "xiqing"
    assert payload["region_label"] == "西青"
    assert payload["role"] == "forecaster"


def test_user_payload_empty_region():
    payload = cg._user_payload("visitor", "external", "active")
    assert payload["region"] is None
    assert payload["region_label"] is None


def test_ensure_tables_adds_region_column(monkeypatch):
    """建表 DDL 必须幂等补 region 列（老库升级）。"""
    monkeypatch.setattr(cg, "_AUTH_TABLES_READY", False)
    conn = _FakeConn()
    monkeypatch.setattr(cg, "_get_chainlit_pg_conn", lambda: conn)

    cg._ensure_chainlit_auth_tables()

    sql_text = "\n".join(stmt for stmt, _ in conn._cursor.executed)
    assert "ADD COLUMN IF NOT EXISTS region" in sql_text
    assert conn.committed


def test_register_user_inserts_region(auth_ready, monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(cg, "_get_chainlit_pg_conn", lambda: conn)

    req = cg.CreateUserRequest(username="jizhou", password="JIZHOU-2026", role="forecaster", region="jizhou")
    out = cg.register_user(req)

    assert out["data"]["region"] == "jizhou"
    assert out["data"]["region_label"] == "蓟州"
    insert_sql, params = conn._cursor.executed[0]
    assert "region" in insert_sql
    assert "jizhou" in params
