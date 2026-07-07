import io
import json
import re
import asyncio
import os
import hashlib
import threading
from datetime import datetime
from urllib.parse import quote_plus

import psycopg2
import psycopg2.pool
import chainlit as cl
import matplotlib.pyplot as plt
import chainlit.data as cl_data
from chainlit.data import get_data_layer
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.types import ThreadDict
from chainlit.user import User
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnableConfig
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from prompts import WEATHER_ASSISTANT_PROMPT
from message_orchestrator import process_message, _sanitize_display_text
from external_skill_tools import build_external_skill_tools
from tools.rain_analysis import build_rain_analysis_tools

app = FastAPI(title="海河流域应急响应判定 REST API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 当通过 `chainlit run` 启动时，实际对外服务的是 chainlit.server.app；
# 本地 uvicorn chain_gzt:app 测试时，则使用上面定义的 app。
try:
    from chainlit.server import app as chainlit_app

    _API_APP = chainlit_app
except Exception:
    _API_APP = app

# 前端河网绘图用的 PostgreSQL 连接池（懒加载）
_RIVER_PLOT_PG_POOL: psycopg2.pool.ThreadedConnectionPool | None = None
_RIVER_PLOT_PG_LOCK = threading.Lock()


def _get_river_plot_pg_pool():
    """为河网绘图创建复用的 PostgreSQL 连接池。"""
    global _RIVER_PLOT_PG_POOL
    if _RIVER_PLOT_PG_POOL is not None:
        return _RIVER_PLOT_PG_POOL
    with _RIVER_PLOT_PG_LOCK:
        if _RIVER_PLOT_PG_POOL is not None:
            return _RIVER_PLOT_PG_POOL
        _RIVER_PLOT_PG_POOL = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=int(os.getenv("RIVER_PLOT_PG_POOL_MAXCONN", "5")),
            host=os.getenv("DB_HOST", "10.226.107.130"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("DB_NAME", "postgres"),
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASSWORD", "postgres"),
            connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
        )
        print("[RiverPlot] PostgreSQL 连接池已创建")
        return _RIVER_PLOT_PG_POOL


# 修复 Matplotlib 中文显示问题
import matplotlib.font_manager as fm

_CJK_CANDIDATES = ("simhei.ttf", "wqy-microhei.ttc", "wqy-zenhei.ttc", "notosanscjk.ttc")
_CJK_SEARCH_DIRS = [
    os.path.join(os.sep, "usr", "share", "fonts"),
    os.path.join(os.sep, "usr", "share", "fonts", "truetype"),
    os.path.join(os.sep, "usr", "local", "share", "fonts"),
    os.path.join(os.sep, "tmp"),
    os.path.join(os.path.expanduser("~"), ".fonts"),
]


def _find_cjk_font():
    for d in _CJK_SEARCH_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower() in _CJK_CANDIDATES:
                    fp = os.path.join(root, f)
                    try:
                        fm.fontManager.addfont(fp)
                        return fm.FontProperties(fname=fp).get_name()
                    except Exception:
                        continue
    return None


_chosen = _find_cjk_font()
if _chosen:
    plt.rcParams['font.sans-serif'] = [_chosen, 'DejaVu Sans']
    print(f"[Matplotlib] 使用字体: {_chosen}")
else:
    print("[Font] WARNING: 未找到中文字体，中文显示为方框。")
plt.rcParams['axes.unicode_minus'] = False

_AUTH_TABLES_READY = False
_CHAINLIT_TABLES_READY = False

CHAINLIT_DB_HOST = os.getenv("CHAINLIT_DB_HOST", "10.226.107.130").strip()
CHAINLIT_DB_PORT = os.getenv("CHAINLIT_DB_PORT", "5432").strip()
CHAINLIT_DB_NAME = os.getenv("CHAINLIT_DB_NAME", "tjznt").strip()
CHAINLIT_DB_USER = os.getenv("CHAINLIT_DB_USER", "postgres").strip()
CHAINLIT_DB_PASSWORD = os.getenv("CHAINLIT_DB_PASSWORD", "postgres")
CHAINLIT_DB_SCHEMA = os.getenv("CHAINLIT_DB_SCHEMA", "public").strip()
CHAINLIT_DB_SSLMODE = os.getenv("CHAINLIT_DB_SSLMODE", "disable").strip().lower()
CHAINLIT_AUTH_SECRET = os.getenv("CHAINLIT_AUTH_SECRET", "chainlit-local-dev-secret-change-me")
ADMIN_DEFAULT_USERNAME = os.getenv("CHAINLIT_ADMIN_USERNAME", "admin").strip()
ADMIN_DEFAULT_PASSWORD = os.getenv("CHAINLIT_ADMIN_PASSWORD", "admin123")
ADMIN_DEFAULT_ROLE = "admin"
ALLOWED_USER_ROLES = {"admin", "forecaster", "external"}
ROLE_LABELS = {
    "admin": "管理员",
    "forecaster": "预报员",
    "external": "外部用户",
}

# 启用 password_auth_callback 时，Chainlit 需要 JWT 密钥。
# 本地开发默认给一个兜底值；生产环境请改为安全随机值并走环境变量注入。
os.environ.setdefault("CHAINLIT_AUTH_SECRET", CHAINLIT_AUTH_SECRET)


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _get_chainlit_pg_conn():
    return psycopg2.connect(
        host=CHAINLIT_DB_HOST,
        port=int(CHAINLIT_DB_PORT),
        dbname=CHAINLIT_DB_NAME,
        user=CHAINLIT_DB_USER,
        password=CHAINLIT_DB_PASSWORD,
        sslmode=CHAINLIT_DB_SSLMODE,
        connect_timeout=int(os.getenv("CHAINLIT_DB_CONNECT_TIMEOUT", "5")),
    )


def _patch_chainlit_socket_connect_auth() -> None:
    """
    兼容部分 socket.io 客户端未传 auth 的情况。
    某些版本组合下会触发：
    - TypeError: connect() missing 1 required positional argument: 'auth'
    - AttributeError: 'NoneType' object has no attribute 'get'
    """
    try:
        import chainlit.socket as chainlit_socket
    except Exception as e:
        print(f"[Chainlit] socket 兼容补丁跳过（导入失败）：{e}")
        return

    original_connect = getattr(chainlit_socket, "connect", None)
    if not callable(original_connect):
        print("[Chainlit] socket 兼容补丁跳过（未找到 connect 处理器）。")
        return

    # 防止重复打补丁
    if getattr(original_connect, "__name__", "") == "_connect_with_optional_auth":
        return

    async def _connect_with_optional_auth(sid, environ, auth=None):
        safe_auth = auth if isinstance(auth, dict) else {}
        return await original_connect(sid, environ, safe_auth)

    chainlit_socket.connect = _connect_with_optional_auth
    chainlit_socket.sio.on("connect", _connect_with_optional_auth)
    print("[Chainlit] 已应用 socket auth 兼容补丁。")


_patch_chainlit_socket_connect_auth()


def _ensure_chainlit_auth_tables() -> None:
    global _AUTH_TABLES_READY
    if _AUTH_TABLES_READY:
        return

    ddl = f"""
    CREATE TABLE IF NOT EXISTS {CHAINLIT_DB_SCHEMA}.hh_user_account (
        id BIGSERIAL PRIMARY KEY,
        username VARCHAR(64) UNIQUE NOT NULL,
        password_hash VARCHAR(128) NOT NULL,
        role VARCHAR(32) NOT NULL DEFAULT 'external',
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    );
    """
    seed = f"""
    INSERT INTO {CHAINLIT_DB_SCHEMA}.hh_user_account (username, password_hash, role, status)
    VALUES (%s, %s, %s, 'active')
    ON CONFLICT (username) DO NOTHING
    """
    with _get_chainlit_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS {CHAINLIT_DB_SCHEMA};')
            cur.execute(ddl)
            cur.execute(seed, (ADMIN_DEFAULT_USERNAME, _hash_password(ADMIN_DEFAULT_PASSWORD), ADMIN_DEFAULT_ROLE))
        conn.commit()

    _AUTH_TABLES_READY = True


@cl.password_auth_callback
def auth_callback(username: str, password: str):
    """
    从数据库校验账号密码，并返回角色信息。
    首次启动时会自动初始化默认管理员账号。
    """
    try:
        _ensure_chainlit_auth_tables()
        with _get_chainlit_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT username, password_hash, role, status
                    FROM {CHAINLIT_DB_SCHEMA}.hh_user_account
                    WHERE username = %s
                    LIMIT 1
                    """,
                    (username.strip(),),
                )
                row = cur.fetchone()
                if not row or row.get("status") != "active":
                    return None
                if row["password_hash"] != _hash_password(password):
                    return None
                return User(
                    identifier=row["username"],
                    display_name=ROLE_LABELS.get(row["role"], row["username"]),
                    metadata={"role": row["role"]},
                )
    except Exception as e:
        print(f"[Chainlit] 登录失败: {e}")
        return None


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, description="用户名")
    password: str = Field(..., min_length=1, description="密码")
    role: str = Field("external", description="角色：admin / forecaster / external")


class UpdateUserStatusRequest(BaseModel):
    status: str = Field(..., description="active / disabled")


class ResetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=1, description="新密码")


def _require_admin_current_user() -> None:
    current_user = cl.user_session.get("user")
    if not current_user:
        raise HTTPException(401, "未登录")
    metadata = getattr(current_user, "metadata", None) or {}
    if metadata.get("role") != "admin":
        raise HTTPException(403, "仅管理员可操作")


def _validate_role(role: str) -> str:
    normalized = (role or "").strip().lower()
    if normalized not in ALLOWED_USER_ROLES:
        raise HTTPException(400, "role 只能是 admin、forecaster、external")
    return normalized


def _role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def _role_payload(role: str) -> dict[str, str]:
    return {"role": role, "role_label": _role_label(role)}


def _user_payload(username: str, role: str, status: str) -> dict[str, str]:
    payload = {"username": username, "status": status}
    payload.update(_role_payload(role))
    return payload


def _build_chainlit_postgres_conninfo() -> tuple[str, bool]:
    conninfo = os.getenv("CHAINLIT_DB_CONNINFO", "").strip()
    if conninfo:
        sslmode = os.getenv("CHAINLIT_DB_SSLMODE", CHAINLIT_DB_SSLMODE).strip().lower()
        ssl_require = sslmode in {"require", "verify-ca", "verify-full"}
        return conninfo, ssl_require

    encoded_user = quote_plus(CHAINLIT_DB_USER)
    encoded_password = quote_plus(CHAINLIT_DB_PASSWORD)
    conninfo = f"postgresql+asyncpg://{encoded_user}:{encoded_password}@{CHAINLIT_DB_HOST}:{CHAINLIT_DB_PORT}/{CHAINLIT_DB_NAME}"
    ssl_require = CHAINLIT_DB_SSLMODE in {"require", "verify-ca", "verify-full"}
    return conninfo, ssl_require


@_API_APP.post("/api/v1/auth/register", tags=["认证"])
def register_user(req: CreateUserRequest):
    _ensure_chainlit_auth_tables()
    username = req.username.strip()
    role = _validate_role(req.role)
    if role == "admin":
        raise HTTPException(400, "注册不允许创建管理员账号")
    with _get_chainlit_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {CHAINLIT_DB_SCHEMA}.hh_user_account (username, password_hash, role, status)
                VALUES (%s, %s, %s, 'active')
                ON CONFLICT (username) DO UPDATE
                SET password_hash = EXCLUDED.password_hash,
                    role = EXCLUDED.role,
                    status = 'active',
                    updated_at = NOW()
                """,
                (username, _hash_password(req.password), role),
            )
        conn.commit()
    return {"code": 200, "data": _user_payload(username, role, "active"), "message": "success"}


@_API_APP.post("/api/v1/admin/users/{username}/reset-password", tags=["用户管理"])
def reset_user_password(username: str, req: ResetPasswordRequest):
    _require_admin_current_user()
    _ensure_chainlit_auth_tables()
    normalized_username = username.strip()
    with _get_chainlit_pg_conn() as conn:
        with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT role
                FROM {CHAINLIT_DB_SCHEMA}.hh_user_account
                WHERE username = %s
                LIMIT 1
                """,
                (normalized_username,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "用户不存在")
            cur.execute(
                f"""
                UPDATE {CHAINLIT_DB_SCHEMA}.hh_user_account
                SET password_hash = %s, updated_at = NOW(), status = 'active'
                WHERE username = %s
                """,
                (_hash_password(req.password), normalized_username),
            )
        conn.commit()
    return {"code": 200, "data": _user_payload(normalized_username, row["role"], "active"), "message": "success"}


@_API_APP.get("/api/v1/admin/users", tags=["用户管理"])
def list_users():
    _require_admin_current_user()
    _ensure_chainlit_auth_tables()
    with _get_chainlit_pg_conn() as conn:
        with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT username, role, status, created_at, updated_at
                FROM {CHAINLIT_DB_SCHEMA}.hh_user_account
                ORDER BY created_at ASC, username ASC
                """
            )
            rows = cur.fetchall()
            return {
                "code": 200,
                "data": [
                    {
                        "username": row["username"],
                        "role": row["role"],
                        **_role_payload(row["role"]),
                        "status": row["status"],
                        "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("created_at") else None,
                        "updated_at": row["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("updated_at") else None,
                    }
                    for row in rows
                ],
                "message": "success",
            }


@_API_APP.post("/api/v1/admin/users", tags=["用户管理"])
def create_user(req: CreateUserRequest):
    _require_admin_current_user()
    _ensure_chainlit_auth_tables()
    username = req.username.strip()
    role = _validate_role(req.role)
    with _get_chainlit_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {CHAINLIT_DB_SCHEMA}.hh_user_account (username, password_hash, role, status)
                VALUES (%s, %s, %s, 'active')
                ON CONFLICT (username) DO UPDATE
                SET password_hash = EXCLUDED.password_hash,
                    role = EXCLUDED.role,
                    status = 'active',
                    updated_at = NOW()
                """,
                (username, _hash_password(req.password), role),
            )
        conn.commit()
    return {"code": 200, "data": _user_payload(username, role, "active"), "message": "success"}


@_API_APP.patch("/api/v1/admin/users/{username}/status", tags=["用户管理"])
def update_user_status(username: str, req: UpdateUserStatusRequest):
    _require_admin_current_user()
    _ensure_chainlit_auth_tables()
    status = req.status.strip()
    if status not in {"active", "disabled"}:
        raise HTTPException(400, "status 只能是 active 或 disabled")
    if username.strip() == ADMIN_DEFAULT_USERNAME and status != "active":
        raise HTTPException(400, "默认管理员不能被禁用")
    with _get_chainlit_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {CHAINLIT_DB_SCHEMA}.hh_user_account
                SET status = %s, updated_at = NOW()
                WHERE username = %s
                """,
                (status, username.strip()),
            )
            if cur.rowcount == 0:
                raise HTTPException(404, "用户不存在")
        conn.commit()
    return {"code": 200, "data": {"username": username, "status": status}, "message": "success"}


def _init_chainlit_data_layer() -> None:
    """初始化 Chainlit 数据层（用于历史会话持久化）。"""
    if get_data_layer():
        return

    conninfo, ssl_require = _build_chainlit_postgres_conninfo()
    data_layer = SQLAlchemyDataLayer(
        conninfo=conninfo,
        ssl_require=ssl_require,
        user_thread_limit=1000,
        show_logger=True,
    )
    cl_data._data_layer = data_layer

    # 兼容历史库中 metadata 为 TEXT 的场景：
    # Chainlit 恢复线程时要求 thread.metadata 为 dict，否则会在 resume 阶段报
    # "'str' object has no attribute 'copy'"。
    original_get_all_user_threads = data_layer.get_all_user_threads

    async def _patched_get_all_user_threads(user_id: str | None = None, thread_id: str | None = None):
        rows = await original_get_all_user_threads(user_id=user_id, thread_id=thread_id)
        if not isinstance(rows, list):
            return rows

        for thread in rows:
            if not isinstance(thread, dict):
                continue

            metadata = thread.get("metadata")
            if isinstance(metadata, str):
                m = metadata.strip()
                if m:
                    try:
                        thread["metadata"] = json.loads(m)
                    except Exception as e:
                        print(f"[DB] 解析 thread metadata JSON 失败: {e}")
                        thread["metadata"] = {"raw": m}
                else:
                    thread["metadata"] = {}
            elif metadata is None:
                thread["metadata"] = {}

            steps = thread.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict):
                    continue
                for key in ["metadata", "generation"]:
                    val = step.get(key)
                    if isinstance(val, str):
                        txt = val.strip()
                        if txt:
                            try:
                                step[key] = json.loads(txt)
                            except Exception as e:
                                print(f"[DB] 解析 step {key} JSON 失败: {e}")
                                step[key] = {"raw": txt}
                        else:
                            step[key] = {}
                    elif val is None:
                        step[key] = {}

                # 兼容历史/脏数据：Chainlit 在前端恢复消息时会直接读取 step["output"]。
                # 若缺失该字段会触发 KeyError: 'output'，这里统一做兜底。
                if "output" not in step or step.get("output") is None:
                    fallback_output = step.get("input")
                    step["output"] = "" if fallback_output is None else str(fallback_output)

        return rows

    data_layer.get_all_user_threads = _patched_get_all_user_threads


async def _ensure_chainlit_tables() -> None:
    """
    在 PostgreSQL 中初始化 Chainlit 历史会话基础表。
    仅首次执行。
    """
    global _CHAINLIT_TABLES_READY
    if _CHAINLIT_TABLES_READY:
        return

    data_layer = get_data_layer()
    if not isinstance(data_layer, SQLAlchemyDataLayer):
        return

    ddl_statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            "id" TEXT PRIMARY KEY,
            "identifier" TEXT UNIQUE NOT NULL,
            "createdAt" TEXT NOT NULL,
            "metadata" JSONB
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS threads (
            "id" TEXT PRIMARY KEY,
            "createdAt" TEXT,
            "name" TEXT,
            "userId" TEXT,
            "userIdentifier" TEXT,
            "tags" TEXT[],
            "metadata" JSONB
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS steps (
            "id" TEXT PRIMARY KEY,
            "name" TEXT,
            "type" TEXT,
            "threadId" TEXT,
            "parentId" TEXT,
            "streaming" BOOLEAN,
            "waitForAnswer" BOOLEAN,
            "isError" BOOLEAN,
            "metadata" JSONB,
            "tags" TEXT[],
            "input" TEXT,
            "output" TEXT,
            "createdAt" TEXT,
            "start" TEXT,
            "end" TEXT,
            "generation" JSONB,
            "showInput" TEXT,
            "language" TEXT,
            "indent" INTEGER
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS feedbacks (
            "id" TEXT PRIMARY KEY,
            "forId" TEXT,
            "threadId" TEXT,
            "value" INTEGER,
            "comment" TEXT
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS elements (
            "id" TEXT PRIMARY KEY,
            "threadId" TEXT,
            "type" TEXT NOT NULL,
            "chainlitKey" TEXT,
            "url" TEXT,
            "objectKey" TEXT,
            "name" TEXT NOT NULL,
            "display" TEXT NOT NULL,
            "size" TEXT,
            "language" TEXT,
            "page" INTEGER,
            "forId" TEXT,
            "mime" TEXT,
            "autoPlay" BOOLEAN,
            "playerConfig" TEXT
        );
        """,
        'CREATE INDEX IF NOT EXISTS idx_steps_thread_id ON steps ("threadId");',
        'CREATE INDEX IF NOT EXISTS idx_elements_thread_id ON elements ("threadId");',
        'CREATE INDEX IF NOT EXISTS idx_feedbacks_for_id ON feedbacks ("forId");',
        'CREATE INDEX IF NOT EXISTS idx_threads_user_id ON threads ("userId");',
        """
        ALTER TABLE users
        ALTER COLUMN "metadata" TYPE JSONB
        USING (
            CASE
                WHEN "metadata" IS NULL OR btrim("metadata"::text) = '' THEN '{}'::jsonb
                WHEN "metadata"::text LIKE '{%' OR "metadata"::text LIKE '[%' THEN "metadata"::jsonb
                ELSE jsonb_build_object('raw', "metadata"::text)
            END
        );
        """,
        """
        ALTER TABLE threads
        ALTER COLUMN "metadata" TYPE JSONB
        USING (
            CASE
                WHEN "metadata" IS NULL OR btrim("metadata"::text) = '' THEN '{}'::jsonb
                WHEN "metadata"::text LIKE '{%' OR "metadata"::text LIKE '[%' THEN "metadata"::jsonb
                ELSE jsonb_build_object('raw', "metadata"::text)
            END
        );
        """,
        """
        ALTER TABLE steps
        ALTER COLUMN "metadata" TYPE JSONB
        USING (
            CASE
                WHEN "metadata" IS NULL OR btrim("metadata"::text) = '' THEN '{}'::jsonb
                WHEN "metadata"::text LIKE '{%' OR "metadata"::text LIKE '[%' THEN "metadata"::jsonb
                ELSE jsonb_build_object('raw', "metadata"::text)
            END
        );
        """,
        """
        ALTER TABLE steps
        ALTER COLUMN "generation" TYPE JSONB
        USING (
            CASE
                WHEN "generation" IS NULL OR btrim("generation"::text) = '' THEN '{}'::jsonb
                WHEN "generation"::text LIKE '{%' OR "generation"::text LIKE '[%' THEN "generation"::jsonb
                ELSE jsonb_build_object('raw', "generation"::text)
            END
        );
        """,
    ]

    for ddl in ddl_statements:
        await data_layer.execute_sql(ddl, {})

    _CHAINLIT_TABLES_READY = True


if os.getenv("CHAINLIT_ENABLE_DB", "1").strip() in {"1", "true", "True"}:
    _init_chainlit_data_layer()
else:
    print("[Chainlit] CHAINLIT_ENABLE_DB 未开启，已跳过 SQLAlchemyDataLayer 初始化。")


async def astream_chain_to_message(chain, input_dict, stream_msg: cl.Message, config: RunnableConfig | None = None):
    """
    稳定优先：模型侧禁用流式（规避 Tongyi tool_calls 异常），
    前端以小块刷新实现“流式观感”。
    延迟可通过 CHAINLIT_STREAM_DELAY_MS 环境变量微调，默认几乎无延迟。
    """
    result = await chain.ainvoke(input_dict, config=config)
    text = getattr(result, "content", None) or ""
    if text:
        chunk_size = 32
        delay_ms = float(os.getenv("CHAINLIT_STREAM_DELAY_MS", "0.5"))
        delay = max(delay_ms / 1000.0, 0.0)
        for i in range(0, len(text), chunk_size):
            stream_msg.content += text[i:i + chunk_size]
            await stream_msg.update()
            if delay > 0:
                await asyncio.sleep(delay)
    return result


def _repair_markdown_layout(text: str) -> str:
    """修复模型偶发把 Markdown 标题、表格和编号列表压成一行的问题。"""
    if not isinstance(text, str) or not text:
        return text

    headings = (
        "核心结论",
        "今日发布预警清单",
        "生效预警清单",
        "预警内容",
        "防范建议",
    )
    for heading in headings:
        text = re.sub(rf"\s*(【{heading}】)", rf"\n\n\1", text)

    # 常见压扁形态：...|数据||:---|... 或 ...|数据|【预警内容】
    text = re.sub(r"(【(?:今日发布预警清单|生效预警清单)】)\s*(\|)", r"\1\n\2", text)
    text = text.replace("||", "|\n|")
    text = re.sub(r"\|\s*(【(?:核心结论|今日发布预警清单|生效预警清单|预警内容|防范建议)】)", r"|\n\n\1", text)

    # 编号列表压成一行时，在 2. 3. ... 前补换行；保留 1. 紧跟标题后的情况。
    text = re.sub(r"(?<=[。；;！!？?])\s*(\d{1,2}\.)", r"\n\1", text)
    text = re.sub(r"(【预警内容】)\s*(1\.)", r"\1\n\2", text)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


async def astream_answer_chain_to_message(answer_chain, input_dict, stream_msg: cl.Message, config: RunnableConfig | None = None) -> str:
    """
    对 answer_chain 启用真实流式输出（不适用于带 tool_calls 的 planner_chain）。
    边生成边刷新前端，并返回完整文本用于写入历史消息。
    若流式失败则自动回退到 ainvoke。
    """
    full_text = ""
    try:
        async for chunk in answer_chain.astream(input_dict, config=config):
            text = ""
            if hasattr(chunk, "content"):
                text = chunk.content or ""
            elif isinstance(chunk, str):
                text = chunk
            if text:
                # 实时清理可能泄露的工具调用标记
                text = _sanitize_display_text(text)
                full_text += text
                stream_msg.content += text
                await stream_msg.update()
        # 最终再清理一次，防止跨 chunk 残留
        final_text = _repair_markdown_layout(_sanitize_display_text(stream_msg.content))
        stream_msg.content = final_text
        await stream_msg.update()
        return _sanitize_display_text(full_text)
    except Exception as e:
        print(f"[流式回答] 失败，回退到非流式：{e}")
        if full_text.strip():
            stream_msg.content = _repair_markdown_layout(_sanitize_display_text(stream_msg.content))
            await stream_msg.update()
            return stream_msg.content
        result = await answer_chain.ainvoke(input_dict, config=config)
        text = getattr(result, "content", None) or ""
        text = _sanitize_display_text(text)
        stream_msg.content += text
        await stream_msg.update()
        return text


async def ainvoke_chain(chain, input_dict, config: RunnableConfig | None = None):
    """静默调用模型，用于工具决策阶段（不向前端输出中间指令）。带60秒超时防止卡死，超时后重试一次。"""
    last_exc = None
    for attempt in range(2):
        try:
            return await asyncio.wait_for(chain.ainvoke(input_dict, config=config), timeout=60)
        except (asyncio.TimeoutError, TimeoutError) as e:
            last_exc = e
            print(f"[ainvoke_chain] 第 {attempt + 1} 次调用超时，准备重试...")
            await asyncio.sleep(1)
    raise last_exc


async def astream_planner_think(chain, input_dict, reasoning_step, config: RunnableConfig | None = None):
    """流式调用模型，实时解析 <think> 标签并展示思考过程。

    在 <think>...</think> 内的内容实时追加到 reasoning_step（前端可见），
    标签外的内容累积为最终 AIMessage.content。
    支持带 tool_calls 的响应。
    """
    inside_think = False
    think_buf = ""
    content_buf = ""
    tool_calls_data = []
    final_msg = None

    async for event in chain.astream_events(input_dict, config=config, version="v2"):
        kind = event.get("event", "")

        if kind == "on_chat_model_stream":
            chunk = event.get("data", {}).get("chunk")
            if chunk is None:
                continue
            token = getattr(chunk, "content", None)
            if not token:
                continue

            # <think> 标签状态机
            remaining = token
            while remaining:
                if not inside_think:
                    # 查找 <think> 开始标签
                    idx = remaining.find("<think>")
                    if idx == -1:
                        # 无可开头的 think内容 → 全部算content
                        content_buf += _strip_think_newline_prefix(remaining)
                        break
                    else:
                        content_buf += _strip_think_newline_prefix(remaining[:idx])
                        remaining = remaining[idx + len("<think>"):]
                        inside_think = True
                        think_buf = ""
                else:
                    # 查找 </think> 结束标签
                    idx = remaining.find("</think>")
                    if idx == -1:
                        think_buf += remaining
                        if think_buf.strip():
                            await reasoning_step.append(think_buf)
                            think_buf = ""
                        break
                    else:
                        think_buf += remaining[:idx]
                        if think_buf.strip():
                            await reasoning_step.append(think_buf)
                            think_buf = ""
                        remaining = remaining[idx + len("</think>"):]
                        inside_think = False
                        await reasoning_step.line("")

        elif kind == "on_chat_model_end":
            # 提取 tool_calls
            output = event.get("data", {}).get("output")
            if output is not None:
                tc = getattr(output, "tool_calls", None)
                if tc:
                    tool_calls_data = tc
                if hasattr(output, "content") and output.content:
                    content_buf = output.content
                final_msg = output

        # 也处理 on_chain_end 以防 on_chat_model_end 未触发
        elif kind == "on_chain_end":
            if final_msg is None:
                output = event.get("data", {}).get("output")
                if output is not None and hasattr(output, "content"):
                    content_buf = getattr(output, "content", content_buf) or content_buf
                    final_msg = output

    # 关闭未闭合的 <think>
    if inside_think and think_buf.strip():
        await reasoning_step.append(think_buf)

    # 构造返回的 AIMessage
    from langchain_core.messages import AIMessage
    return AIMessage(
        content=content_buf.strip() if content_buf else "",
        tool_calls=tool_calls_data if tool_calls_data else None,
    )


def _strip_think_newline_prefix(text: str) -> str:
    """去除 <think> 紧跟的单个换行符，保持内容整洁。"""
    if text.startswith("\n"):
        return text[1:]
    return text


async def stream_text_to_message(text: str, stream_msg: cl.Message | None = None, chunk_size: int = 32, delay_ms: float | None = None):
    """
    统一的前端流式输出：
    - 传入现有 stream_msg：在同一条消息上持续刷新
    - 不传 stream_msg：新建一条消息并流式刷新
    默认延迟极低（0.5ms/块），可通过 CHAINLIT_STREAM_DELAY_MS 环境变量调整。

    若文本包含 Markdown 表格，则直接完整发送，避免流式切片破坏表格结构。
    """
    # 清洗 HTML 标签与可能泄露的工具调用标记
    text = text.replace("<br>", "").replace("<br/>", "").replace("</br>", "")
    text = _sanitize_display_text(text)

    # 检测 Markdown 表格：表头行 + 分隔行（如 | 维度 | 内容 |\n| :--- | :--- |）
    has_markdown_table = bool(re.search(r"(?:^|\n)\|[^\n]+\|\n\|[-:\s|]+\|", text))

    if stream_msg is None and has_markdown_table:
        msg = cl.Message(content=text)
        await msg.send()
        return msg

    if stream_msg is None:
        stream_msg = cl.Message(content="")
        await stream_msg.send()

    if not text:
        return stream_msg

    if delay_ms is None:
        delay_ms = float(os.getenv("CHAINLIT_STREAM_DELAY_MS", "0.5"))
    delay = max(delay_ms / 1000.0, 0.0)

    for i in range(0, len(text), chunk_size):
        stream_msg.content += text[i:i + chunk_size]
        await stream_msg.update()
        if delay > 0:
            await asyncio.sleep(delay)
    return stream_msg

def _user_forbids_followup(user_text: str) -> bool:
    if not user_text:
        return False
    t = user_text.strip()
    forbids = [
        "不要扩展", "不要追问", "别追问", "别问我", "不要问我",
        "只要结论", "只要答案", "直接给答案", "不要建议", "不需要建议",
        "不用扩展", "不用追问", "不需要追问", "别给扩展",
        "只专注这个问题", "只回答这个问题", "只说这个问题", "只围绕这个问题",
        "不要发散", "别发散", "不要展开", "别展开", "只聚焦这个问题",
    ]
    return any(k in t for k in forbids)


def _has_followup_line(text: str) -> bool:
    if not text:
        return False
    return "可继续追问：" in text


def _make_followup_question(user_text: str) -> str:
    """
    只生成 1 个强相关追问，避免泛化；不依赖二次 LLM，保证稳定。
    """
    t = (user_text or "").strip()
    if not t:
        return "可继续追问：你希望我按哪个子流域/行政区划来展开分析？"

    # 河网/水系/上下游
    if any(k in t for k in ["河网", "河道", "水系", "拓扑", "流域图", "示意图", "路径", "上下游", "汇入", "干流"]):
        return "可继续追问：你要我聚焦哪条河（或哪个子流域），以及是否需要把下游连锁影响也一起纳入？"

    # 暴雨影响/分区
    if any(k in t for k in ["暴雨", "影响范围", "分区", "区划", "246分区", "11分区", "77分区", "行政区划"]):
        return "可继续追问：你希望我按“行政区划”还是按“分区图层（如246/11/77）”输出完整清单？"

    # 降雨/雨量/预报
    if any(k in t for k in ["降雨", "雨量", "下雨", "降水", "毫米", "面雨量", "累计"]):
        return "可继续追问：你要查询的具体地点（市/县/站点）和时间范围是“今天白天/今夜/未来24小时/未来3天”中的哪一个？"

    # 时间类
    if any(k in t for k in ["今天", "明天", "后天", "周末", "本周", "下周", "现在", "当前", "几点", "日期", "时间"]):
        return "可继续追问：你要我以哪个时间段为准（例如今天白天/今夜/未来24小时），并聚焦哪个区域？"

    return "可继续追问：你更关心哪一项——降雨量大小、影响范围（区划/分区）、还是上下游传导路径？"


def _append_followup_if_needed(answer_text: str, user_text: str) -> str:
    """不再追加扩展追问，仅保留原文本。prompt 已禁止 LLM 自行输出追问。"""
    return answer_text


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(k in text for k in keywords)


def _user_requests_strict_focus(user_text: str) -> bool:
    if not user_text:
        return False
    t = user_text.strip()
    focus_keywords = [
        "只专注这个问题", "只回答这个问题", "只说这个问题", "只围绕这个问题",
        "不要发散", "别发散", "不要展开", "别展开", "只聚焦这个", "只聚焦这个问题",
    ]
    return _contains_any(t, focus_keywords)


def _should_force_admin_units_reply(user_text: str) -> bool:
    """
    只在用户明确问“受影响行政区划/区县”且没有同时追问其它维度时，
    走确定性的工具结果直出，避免模型自行扩展到分区、下游、建议等内容。
    """
    if not user_text:
        return False

    t = user_text.strip()
    admin_keywords = ["行政区划", "行政区", "区县", "县区"]
    focused_admin_phrases = [
        "哪些行政区划", "哪些行政区", "涉及哪些行政区划", "涉及哪些行政区",
        "会影响哪些行政区划", "会影响哪些行政区", "影响哪些行政区划", "影响哪些行政区",
        "哪些区县", "涉及哪些区县", "会影响哪些区县", "影响哪些区县",
        "哪些县区", "涉及哪些县区", "会影响哪些县区", "影响哪些县区",
        "影响的行政区划", "暴雨影响的行政区划", "发生暴雨影响的行政区划",
        "行政区划有哪些", "行政区有哪些", "区县有哪些", "县区有哪些",
    ]
    mixed_scope_keywords = [
        "分区", "246", "11分区", "77分区", "32分区", "9分区",
        "下游", "汇入", "连锁", "传导", "哪些河", "河网", "路径", "示意图", "画图",
        "风险", "风险等级", "建议", "行动", "防御", "应对", "预案",
        "为什么", "原因", "机理", "过程",
    ]

    if not _contains_any(t, admin_keywords):
        return False
    if _contains_any(t, mixed_scope_keywords):
        return False
    if _contains_any(t, focused_admin_phrases) or _user_requests_strict_focus(t):
        return True

    # 兜底：只要明确提到“行政区划/区县”，且未混入分区/下游/建议等其它主题，就走行政区划专用收口
    broad_admin_patterns = [
        "行政区划", "行政区", "区县", "县区",
    ]
    return _contains_any(t, broad_admin_patterns)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _pick_first_text(data: dict, keys: list[str]) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return ""


def _dedupe_joined_parts(parts: list[str]) -> str:
    deduped = []
    for part in parts:
        if part and (not deduped or deduped[-1] != part):
            deduped.append(part)
    return "".join(deduped)


def _match_first_suffix_token(text: str, suffixes: list[str]) -> tuple[str, str]:
    """
    在 text 中匹配“最先出现”的行政后缀。
    若同一位置有多个后缀可匹配，优先更长后缀，避免把“自治州”截成“州”。
    """
    best_idx = None
    best_suffix = ""
    for s in suffixes:
        idx = text.find(s)
        if idx <= 0:
            continue
        if (
            best_idx is None
            or idx < best_idx
            or (idx == best_idx and len(s) > len(best_suffix))
        ):
            best_idx = idx
            best_suffix = s

    if best_idx is not None:
        end = best_idx + len(best_suffix)
        return text[:end], text[end:]
    return "", text


def _is_suspicious_admin_field(value: str, level: str) -> bool:
    v = (value or "").strip()
    if not v:
        return False

    if level == "province":
        return any(k in v for k in ["区", "县", "乡", "镇", "街道"])
    if level == "city":
        return ("市" in v and v.count("市") > 1) or any(k in v for k in ["区", "县", "乡", "镇", "街道"])
    if level == "district":
        return any(k in v for k in ["省", "乡", "镇", "街道"])
    if level == "town":
        return len(v) <= 1
    return False


def _should_replace_with_parsed(existing: str, parsed: str, level: str) -> bool:
    e = (existing or "").strip()
    p = (parsed or "").strip()
    if not p:
        return False
    if not e:
        return True
    if _is_suspicious_admin_field(e, level):
        return True
    if e != p and e.startswith(p):
        # 例如“保定市涿州”应纠正为“保定市”
        return True
    return False


def _split_cn_admin_name(name: str) -> tuple[str, str, str, str]:
    """
    把类似“北京市北京市丰台区卢沟桥镇”拆成省市区乡四级。
    拆分失败的层级返回空串。
    """
    text = (name or "").strip().replace(" ", "")
    if not text:
        return "", "", "", ""

    province_suffixes = ["特别行政区", "自治区", "省", "市"]
    city_suffixes = ["自治州", "地区", "盟", "州", "市"]
    district_suffixes = ["自治县", "旗", "新区", "开发区", "管理区", "区", "县", "市"]
    town_suffixes = ["街道", "镇", "乡", "苏木"]

    province, rest = _match_first_suffix_token(text, province_suffixes)
    city, rest = _match_first_suffix_token(rest, city_suffixes)
    district, rest = _match_first_suffix_token(rest, district_suffixes)
    town, _ = _match_first_suffix_token(rest, town_suffixes)

    # 若无 town 后缀但还有残余，仍作为最末级名称保留
    if not town and rest:
        town = rest

    return province, city, district, town


def _cleanup_admin_parts(province: str, city: str, district: str, town: str) -> tuple[str, str, str, str]:
    province = (province or "").strip()
    city = (city or "").strip()
    district = (district or "").strip()
    town = (town or "").strip()

    # 城市字段里误带区县时，裁到“市/地区/盟/州”结束
    for token in ["自治州", "地区", "盟", "州", "市"]:
        idx = city.find(token)
        if idx > 0:
            city = city[: idx + len(token)]
            break

    # 区县字段里误带完整前缀时，优先取真正区县尾段
    if district and any(k in district for k in ["省", "市", "乡", "镇", "街道"]):
        _, _, parsed_district, parsed_town = _split_cn_admin_name(district)
        if parsed_district:
            district = parsed_district
        if (not town) and parsed_town:
            town = parsed_town

    # 乡镇字段若误带前缀（如“市义和庄乡”），截取最后一个乡镇后缀对应片段
    for suffix in ["街道", "镇", "乡", "苏木"]:
        idx = town.rfind(suffix)
        if idx >= 0:
            start = town[:idx].rfind("市")
            if start >= 0 and (idx - start) <= 5:
                town = town[start + 1 : idx + len(suffix)]
            break

    # 乡镇只剩“镇/乡”等单字时，视为无效
    if town in {"镇", "乡", "街道", "苏木"}:
        town = ""

    return province, city, district, town


def _normalize_admin_unit_item(item) -> dict[str, str] | None:
    if isinstance(item, str):
        name = item.strip()
        if not name:
            return None
        province, city, district, town = _split_cn_admin_name(name)
        return {
            "name": name,
            "code": "",
            "province": province,
            "city": city,
            "district": district,
            "town": town,
        }

    if not isinstance(item, dict):
        return None

    province = _pick_first_text(item, ["province_name", "province"])
    city = _pick_first_text(item, ["city_name", "city"])
    district = _pick_first_text(
        item,
        ["district_name", "district", "county_name", "county", "area_name"],
    )
    town = _pick_first_text(
        item,
        ["town_name", "town", "street_name", "street", "township_name", "township"],
    )

    name = _pick_first_text(
        item,
        [
            "name", "admin_name", "unit_name", "region_name", "label",
            "district_name", "county_name", "area_name",
        ],
    )
    if not name:
        name = _dedupe_joined_parts(
            [
                province,
                city,
                district,
                town,
            ]
        )
    if not name:
        for key, value in item.items():
            if key in {"code", "adcode", "admin_code", "district_code", "county_code"}:
                continue
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
    if not name:
        return None

    # 尝试从拼接名称拆层：用于补全缺失字段，也用于修复明显异常字段
    s_province, s_city, s_district, s_town = _split_cn_admin_name(name)
    if _should_replace_with_parsed(province, s_province, "province"):
        province = s_province
    if _should_replace_with_parsed(city, s_city, "city"):
        city = s_city
    if _should_replace_with_parsed(district, s_district, "district"):
        district = s_district
    if _should_replace_with_parsed(town, s_town, "town"):
        town = s_town

    province, city, district, town = _cleanup_admin_parts(province, city, district, town)

    code = _pick_first_text(
        item,
        ["code", "adcode", "admin_code", "district_code", "county_code", "region_code"],
    )
    return {
        "name": name,
        "code": code,
        "province": province,
        "city": city,
        "district": district,
        "town": town,
    }


def _extract_admin_unit_rows(raw_result) -> tuple[str, list[dict[str, str]]]:
    data = _unwrap_tool_result(raw_result)
    if not isinstance(data, dict):
        return "该河流", []

    river_name = str(data.get("river_name") or data.get("name") or "该河流").strip()
    self_report = data.get("self_report")
    report = self_report if isinstance(self_report, dict) else data

    candidates = []
    for key in ["admin_units", "admin_regions", "administrative_units", "affected_admin_units"]:
        if isinstance(report, dict) and report.get(key):
            candidates = _as_list(report.get(key))
            break
    if not candidates and isinstance(data, dict):
        for key in ["admin_units", "admin_regions", "administrative_units", "affected_admin_units"]:
            if data.get(key):
                candidates = _as_list(data.get(key))
                break

    rows = []
    seen = set()
    for item in candidates:
        row = _normalize_admin_unit_item(item)
        if not row:
            continue
        dedupe_key = (row["name"], row["code"])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        rows.append(row)

    rows.sort(
        key=lambda x: (
            x.get("province", ""),
            x.get("city", ""),
            x.get("district", ""),
            x.get("town", ""),
            x.get("name", ""),
            x.get("code", ""),
        )
    )
    return river_name or "该河流", rows


def _escape_md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _build_admin_units_only_reply(raw_result) -> str | None:
    river_name, rows = _extract_admin_unit_rows(raw_result)
    if not rows:
        return (
            f"当前工具结果中未返回 `{river_name}` 暴雨影响的行政区划清单，"
            "因此暂时无法只按“行政区划”给出确定列表。"
        )

    has_hierarchy = any(
        row.get("province") or row.get("city") or row.get("district") or row.get("town")
        for row in rows
    )

    # 层级字段存在时，按“省-市-区县”合并展示，乡镇/街道聚合到同一格
    if has_hierarchy:
        grouped = {}
        for row in rows:
            province = (row.get("province") or "—").strip() or "—"
            city = (row.get("city") or "—").strip() or "—"
            district = (row.get("district") or row.get("name") or "—").strip() or "—"
            town = (row.get("town") or "").strip()
            if not town and row.get("name") and row.get("name") not in {district, city, province}:
                town = row["name"].strip()

            key = (province, city, district)
            if key not in grouped:
                grouped[key] = {"towns": set()}
            if town:
                grouped[key]["towns"].add(town)

        total_towns = sum(len(v["towns"]) for v in grouped.values())
        lines = [
            f"按当前工具结果，`{river_name}` 暴雨影响涉及 {len(grouped)} 个区县。以下仅列行政区划，不展开分区、下游和防御建议。",
            "",
            f"| 省份 | 地市 | 区县 | 乡镇/街道（共{total_towns}个） |",
            "| :--- | :--- | :--- | :--- |",
        ]

        prev_province = None
        prev_city = None
        for province, city, district in sorted(grouped.keys()):
            towns = sorted(grouped[(province, city, district)]["towns"])
            town_text = "、".join(_escape_md_cell(t) for t in towns) if towns else "—"
            show_province = province if province != prev_province else ""
            show_city = city if (province != prev_province or city != prev_city) else ""
            lines.append(
                f"| {_escape_md_cell(show_province)} | {_escape_md_cell(show_city)} | {_escape_md_cell(district)} | {town_text} |"
            )
            prev_province = province
            prev_city = city
    else:
        include_code = any(row["code"] for row in rows)
        lines = [
            f"按当前工具结果，`{river_name}` 暴雨影响的行政区划共 {len(rows)} 个。以下仅列行政区划，不展开分区、下游和防御建议。",
            "",
        ]

        if include_code:
            lines.extend(
                [
                    "| 序号 | 行政区划 | 代码 |",
                    "| :--- | :--- | :--- |",
                ]
            )
            for idx, row in enumerate(rows, 1):
                lines.append(
                    f"| {idx} | {_escape_md_cell(row['name'])} | {_escape_md_cell(row['code'] or '-')} |"
                )
        else:
            lines.extend(
                [
                    "| 序号 | 行政区划 |",
                    "| :--- | :--- |",
                ]
            )
            for idx, row in enumerate(rows, 1):
                lines.append(f"| {idx} | {_escape_md_cell(row['name'])} |")

    return "\n".join(lines)


def _should_force_structured_impact_reply(user_text: str) -> bool:
    if not user_text:
        return False
    t = user_text.strip()
    if _should_force_partition_table_reply(t):
        return False
    has_rainstorm = _contains_any(t, ["暴雨", "强降雨", "降雨"])
    has_impact = _contains_any(t, ["影响", "影响范围", "会影响", "影响分析"])
    if not (has_rainstorm and has_impact):
        return False
    # 专门问行政区划时走“行政区划专用收口”
    if _should_force_admin_units_reply(t):
        return False
    return True


def _should_force_partition_table_reply(user_text: str) -> bool:
    if not user_text:
        return False
    t = user_text.strip()
    partition_keywords = ["分区表", "影响分区", "分区明细", "分区清单", "分区情况", "分区"]
    admin_keywords = ["行政区划", "行政区", "区县", "县区", "乡镇", "街道"]
    if not _contains_any(t, partition_keywords):
        return False
    if _contains_any(t, admin_keywords):
        return False
    return True


def _extract_partition_groups(raw_result) -> dict[str, list[dict[str, str]]]:
    data = _unwrap_tool_result(raw_result)
    report = data.get("self_report") if isinstance(data, dict) and isinstance(data.get("self_report"), dict) else data
    if not isinstance(report, dict):
        return {}

    groups: dict[str, list[dict[str, str]]] = {
        "海河246分区": [],
        "海河11分区": [],
        "海河32分区": [],
        "海河77分区": [],
        "海河9分区": [],
    }

    def infer_group(key_text: str, code_text: str = "") -> str | None:
        k = (key_text or "").lower()
        c = (code_text or "").lower()
        if ("246" in k) or c.startswith("h246_"):
            return "海河246分区"
        if ("h11" in k) or c.startswith("h11_") or "11分区" in key_text:
            return "海河11分区"
        if ("h32" in k) or c.startswith("h32_") or "32分区" in key_text:
            return "海河32分区"
        if ("h77" in k) or c.startswith("h77_") or "77分区" in key_text:
            return "海河77分区"
        if ("h9" in k) or c.startswith("h9_") or "9分区" in key_text:
            return "海河9分区"
        return None

    def norm_row(item) -> dict[str, str]:
        if isinstance(item, dict):
            name = _pick_first_text(
                item,
                ["name", "partition_name", "subbasin_name", "region_name", "label", "zone_name", "title", "分区名称"],
            )
            code = _pick_first_text(item, ["code", "partition_code", "zone_code", "id", "分区代码"])
            area = _pick_first_text(item, ["region", "所属区域", "area", "group_name"])
            return {"name": name, "code": code, "area": area}
        txt = str(item).strip()
        return {"name": txt, "code": "", "area": ""}

    def walk(obj, parent_key: str = ""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, str(k))
            return
        if isinstance(obj, list):
            parsed_rows = [norm_row(it) for it in obj]
            for r in parsed_rows:
                group = infer_group(parent_key, r.get("code", ""))
                if group:
                    groups[group].append(r)
            for it in obj:
                walk(it, parent_key)

    walk(report)

    # 去重
    for gname in list(groups.keys()):
        seen = set()
        deduped = []
        for row in groups[gname]:
            key = (row.get("name", ""), row.get("code", ""), row.get("area", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        groups[gname] = deduped

    return groups


IMPACT_TIME_TOOL_RESULT_KEY = "__impact_time_tool_result__"


async def _enrich_with_impact_time_tool(observation, tool_args, tools, step=None):
    """
    额外调用“河流影响时间估算”工具，把结果挂载到当前 observation，
    供后续结构化报表读取。
    """
    river_name = ""
    if isinstance(tool_args, dict):
        for k in ["river_name", "source_river", "start_river", "river"]:
            v = tool_args.get(k)
            if isinstance(v, str) and v.strip():
                river_name = v.strip()
                break

    obs_data = _unwrap_tool_result(observation)
    if (not river_name) and isinstance(obs_data, dict):
        river_name = (
            _pick_first_text(obs_data, ["river_name", "name", "source_river"])
            or _pick_first_text(obs_data.get("self_report", {}) if isinstance(obs_data.get("self_report"), dict) else {}, ["river_name", "name"])
        )
    if not river_name:
        return observation

    impact_time_tool = None
    for t in tools:
        name = str(getattr(t, "name", ""))
        low = name.lower()
        if name == "estimate_river_impact_time":
            impact_time_tool = t
            break
        if "影响时间" in name and "河" in name:
            impact_time_tool = t
            break
        if "river" in low and "impact" in low and "time" in low:
            impact_time_tool = t
            break

    if impact_time_tool is None:
        return observation

    trial_args = [
        {"river_name": river_name, "max_rivers": 5},
        {"source_river": river_name, "max_rivers": 5},
        {"start_river": river_name, "max_rivers": 5},
        {"river": river_name, "max_rivers": 5},
        {"river_name": river_name},
    ]

    impact_result = None
    for idx, args in enumerate(trial_args, 1):
        try:
            impact_result = await impact_time_tool.ainvoke(args)
            if step is not None:
                step.input += f"⏱️ 已调用 `{impact_time_tool.name}` 获取下游传导时间（尝试 {idx}）。\n"
            print(f"[影响时间工具] {impact_time_tool.name} 调用成功，args={args}")
            break
        except Exception as e:
            print(f"[影响时间工具] {impact_time_tool.name} 调用失败（尝试 {idx}）：{e}")
            continue

    if impact_result is None:
        return observation

    impact_data = _unwrap_tool_result(impact_result)
    if isinstance(obs_data, dict):
        merged = dict(obs_data)
        merged[IMPACT_TIME_TOOL_RESULT_KEY] = impact_data
        return merged
    return {
        "base_observation": obs_data,
        IMPACT_TIME_TOOL_RESULT_KEY: impact_data,
    }


def _extract_impact_time_window(raw_result) -> str:
    data = _unwrap_tool_result(raw_result)
    report = data.get("self_report") if isinstance(data, dict) and isinstance(data.get("self_report"), dict) else (data if isinstance(data, dict) else {})

    direct_time = _pick_first_text(
        report,
        [
            "impact_time_window", "time_window", "time_range", "forecast_time_range",
            "valid_time", "valid_period", "有效时间", "时间范围",
        ],
    )
    if direct_time:
        return direct_time

    start_time = _pick_first_text(
        report,
        [
            "start_time", "begin_time", "from_time", "forecast_start_time",
            "valid_start_time", "起始时间", "开始时间",
        ],
    )
    end_time = _pick_first_text(
        report,
        [
            "end_time", "finish_time", "to_time", "forecast_end_time",
            "valid_end_time", "截止时间", "结束时间",
        ],
    )
    if start_time and end_time:
        return f"{start_time} ~ {end_time}"

    # 兜底：从顶层再找一次
    if isinstance(data, dict):
        top_start = _pick_first_text(data, ["start_time", "begin_time", "forecast_start_time", "开始时间"])
        top_end = _pick_first_text(data, ["end_time", "finish_time", "forecast_end_time", "结束时间"])
        if top_start and top_end:
            return f"{top_start} ~ {top_end}"
        top_time = _pick_first_text(data, ["time_range", "impact_time_window", "时间范围"])
        if top_time:
            return top_time

    return "待核实"


def _extract_downstream_transport_time(raw_result) -> str:
    data = _unwrap_tool_result(raw_result)
    if not isinstance(data, dict):
        return "待核实"
    impact_data = data.get(IMPACT_TIME_TOOL_RESULT_KEY)
    if impact_data is None:
        return "待核实"
    impact_data = _unwrap_tool_result(impact_data)
    if not isinstance(impact_data, dict):
        return "待核实"

    downstream = impact_data.get("downstream")
    if not isinstance(downstream, list) or not downstream:
        return "待核实"

    parts = []
    for row in downstream[:3]:
        if not isinstance(row, dict):
            continue
        river = str(row.get("downstream_river") or row.get("name") or "下游河流").strip()
        avg = row.get("time_estimates", {}).get("avg", {}) if isinstance(row.get("time_estimates"), dict) else {}
        duration = avg.get("duration", {}) if isinstance(avg, dict) else {}
        human = ""
        if isinstance(duration, dict):
            human = str(duration.get("human") or "").strip()
            if not human:
                try:
                    hours = float(duration.get("hours"))
                    human = f"{hours:.2f} 小时"
                except Exception as e:
                    print(f"[交通时间] 解析时长失败: {e}")
                    human = ""
        if human:
            parts.append(f"{river}:{human}")

    if not parts:
        return "待核实"
    return "；".join(parts)


def _build_structured_impact_reply(raw_result) -> str:
    data = _unwrap_tool_result(raw_result)
    river_name, admin_rows = _extract_admin_unit_rows(raw_result)
    groups = _extract_partition_groups(raw_result)

    report = data.get("self_report") if isinstance(data, dict) and isinstance(data.get("self_report"), dict) else (data if isinstance(data, dict) else {})
    downstream_count = _pick_first_text(report, ["downstream_affected_river_count", "downstream_count", "affected_downstream_river_count"])
    if not downstream_count:
        downstream_count = _pick_first_text(data if isinstance(data, dict) else {}, ["downstream_affected_river_count", "downstream_count"])
    downstream_count = downstream_count or "待核实"
    impact_time_window = _extract_impact_time_window(raw_result)
    transport_time = _extract_downstream_transport_time(raw_result)

    # 概览表
    total_partitions = sum(len(v) for v in groups.values())
    unique_districts = len(
        {
            (r.get("province", ""), r.get("city", ""), r.get("district", ""))
            for r in admin_rows
            if r.get("district")
        }
    )
    lines = [
        f"`{river_name}` 暴雨影响结构化简报（工具结果直出）：",
        "",
        "| 指标 | 数值 | 说明 |",
        "| :--- | :--- | :--- |",
        f"| 影响时间 | {impact_time_window} | 工具返回的时间窗口 |",
        f"| 乡镇/街道数量 | {len(admin_rows)} | 行政区划明细总数 |",
        f"| 区县数量 | {unique_districts or '待核实'} | 去重后的区县层级数量 |",
        f"| 影响分区条目数 | {total_partitions} | 246/11/32/77/9 分区汇总条目 |",
        f"| 下游受影响河流数 | {downstream_count} | 工具返回统计值 |",
        f"| 下游传导时间（平均） | {transport_time} | 由河流影响时间工具估算（距离/流速） |",
        "",
    ]

    # 行政区划表（沿用现有合并逻辑）
    admin_table_text = _build_admin_units_only_reply(raw_result) or ""
    if admin_table_text:
        lines.append("### 行政区划影响表")
        lines.append(admin_table_text)
        lines.append("")

    # 河流影响分区表（新增）
    lines.extend(
        [
            "### 河流影响分区表",
            "| 分区体系 | 条目数 | 代表项（最多3项） |",
            "| :--- | :--- | :--- |",
        ]
    )
    ordered_group_names = ["海河246分区", "海河11分区", "海河32分区", "海河77分区", "海河9分区"]
    for gname in ordered_group_names:
        rows = groups.get(gname, [])
        sample = []
        for row in rows[:3]:
            name = (row.get("name") or row.get("area") or "").strip()
            code = (row.get("code") or "").strip()
            if name and code:
                sample.append(f"{name}({code})")
            elif name:
                sample.append(name)
            elif code:
                sample.append(code)
        sample_text = "、".join(sample) if sample else "—"
        lines.append(f"| {gname} | {len(rows)} | {sample_text} |")

    lines.append("")
    lines.append("如果需要，我可以继续按任一分区体系（如 77 分区）输出完整明细表。")
    return "\n".join(lines)


def _build_partition_only_reply(raw_result) -> str:
    data = _unwrap_tool_result(raw_result)
    river_name = str(data.get("river_name") or data.get("name") or "该河流").strip() if isinstance(data, dict) else "该河流"
    groups = _extract_partition_groups(raw_result)
    total_partitions = sum(len(v) for v in groups.values())
    impact_time_window = _extract_impact_time_window(raw_result)
    transport_time = _extract_downstream_transport_time(raw_result)

    lines = [
        f"`{river_name}` 暴雨影响分区表（工具结果直出）：",
        "",
        f"影响时间：{impact_time_window}",
        f"下游传导时间（平均）：{transport_time}",
        "",
        "| 分区体系 | 条目数 | 代表项（最多3项） |",
        "| :--- | :--- | :--- |",
    ]
    ordered_group_names = ["海河246分区", "海河11分区", "海河32分区", "海河77分区", "海河9分区"]
    for gname in ordered_group_names:
        rows = groups.get(gname, [])
        sample = []
        for row in rows[:3]:
            name = (row.get("name") or row.get("area") or "").strip()
            code = (row.get("code") or "").strip()
            if name and code:
                sample.append(f"{name}({code})")
            elif name:
                sample.append(name)
            elif code:
                sample.append(code)
        sample_text = "、".join(sample) if sample else "—"
        lines.append(f"| {gname} | {len(rows)} | {sample_text} |")

    lines.extend(
        [
            "",
            f"分区条目总数：{total_partitions}",
            "如需，我可以继续输出某一分区体系的完整明细（名称+代码全量）。",
        ]
    )
    return "\n".join(lines)


async def load_sse_tools():
    mcp_url = os.getenv("MCP_SERVER_URL", "http://localhost:3333/sse")
    extrm_url = os.getenv("EXTRM_SERVER_URL", "http://10.226.107.133:8000/sse")
    client = MultiServerMCPClient(
        {
            "weather": {
                "transport": "sse",
                "url": mcp_url,
            },
            "extreme-weather-statistics": {
                "transport": "sse",
                "url": extrm_url,
            },
        }
    )
    try:
        tools = await client.get_tools()
        print(f"✅ MCP 工具加载成功，共 {len(tools)} 个工具：{[t.name for t in tools]}")
        return tools
    except BaseException as e:
        # 后端 MCP SSE 服务异常（包括 ExceptionGroup），打印错误并退化为无工具模式
        print("❌ 加载 MCP 工具失败：", repr(e))
        return []


def _unwrap_tool_result(raw_result):
    """把 MCP tool 返回结果统一拆成 Python 对象"""
    if raw_result is None:
        return None

    if isinstance(raw_result, list) and len(raw_result) > 0 and isinstance(raw_result[0], dict) and "text" in raw_result[0]:
        text = raw_result[0]["text"]
        try:
            return json.loads(text)
        except Exception as e:
            print(f"[工具结果解包] JSON 解析失败: {e}")
            return text

    if isinstance(raw_result, str):
        try:
            return json.loads(raw_result)
        except Exception as e:
            print(f"[工具结果解包] JSON 解析失败: {e}")
            return raw_result

    if hasattr(raw_result, "content"):
        content = raw_result.content
        if isinstance(content, str):
            try:
                return json.loads(content)
            except Exception as e:
                print(f"[工具结果解包] 解析 content JSON 失败: {e}")
                return content
        return content

    return raw_result


def _compact_warning_record(item):
    if not isinstance(item, dict):
        return {
            "content": str(item),
            "eventType": "",
            "department": "",
            "time": "",
            "severity": "",
            "msgType": "",
        }
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    return {
        "content": item.get("content") or raw.get("content") or "",
        "eventType": item.get("eventType") or item.get("event_type") or raw.get("eventType") or "",
        "department": item.get("department") or raw.get("department") or "",
        "time": item.get("time") or item.get("publish_time") or raw.get("time") or "",
        "severity": item.get("severity") or raw.get("severity") or "",
        "msgType": item.get("msgType") or item.get("msg_type") or raw.get("msgType") or "",
    }


def _compact_warning_records(items):
    if not isinstance(items, list):
        return []
    return [_compact_warning_record(item) for item in items]


def _compact_warning_payload(data):
    if not isinstance(data, dict):
        return None

    is_warning_payload = (
        data.get("warning_status") in {"effective", "history", "today_summary"}
        or "today_published_warnings" in data
        or "today_new_or_update_warnings" in data
        or "effective_warnings" in data
        or (
            isinstance(data.get("warnings"), list)
            and any(
                isinstance(item, dict)
                and (
                    "eventType" in item
                    or "event_type" in item
                    or "msgType" in item
                    or "msg_type" in item
                    or "raw" in item
                )
                for item in data.get("warnings", [])
            )
        )
    )
    if not is_warning_payload:
        return None

    compact = {
        "warning_status": data.get("warning_status"),
        "query_time": data.get("query_time"),
        "query_hour_text": data.get("query_hour_text"),
        "count": data.get("count"),
    }
    if data.get("error"):
        compact["error"] = data.get("error")
        compact["message"] = data.get("message")

    if data.get("warning_status") == "today_summary" or "today_new_or_update_warnings" in data:
        compact.update(
            {
                "today": data.get("today"),
                "published_count": data.get("published_count"),
                "new_or_update_count": data.get("new_or_update_count"),
                "effective_count": data.get("effective_count"),
                "event_types": data.get("event_types") or [],
                "today_published_warnings": _compact_warning_records(data.get("today_published_warnings")),
                "today_new_or_update_warnings": _compact_warning_records(data.get("today_new_or_update_warnings")),
                "effective_warnings": _compact_warning_records(data.get("effective_warnings")),
            }
        )
    else:
        compact["warnings"] = _compact_warning_records(data.get("warnings"))

    return compact


def _calc_bbox(xs, ys, pad_ratio=0.08, min_pad=0.05):
    if not xs or not ys:
        return None

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    dx = max_x - min_x
    dy = max_y - min_y

    pad_x = max(dx * pad_ratio, min_pad)
    pad_y = max(dy * pad_ratio, min_pad)

    return {
        "min_x": min_x - pad_x,
        "max_x": max_x + pad_x,
        "min_y": min_y - pad_y,
        "max_y": max_y + pad_y,
    }


def _kwargs_for_admin_division_plot(bbox: dict) -> dict:
    """
    只传视域与缓冲；市/县聚合级别与简化参数由 MCP 工具 get_admin_division_for_plot 按跨度自动决定。
    """
    span = max(bbox["max_x"] - bbox["min_x"], bbox["max_y"] - bbox["min_y"])
    if span >= 1.35:
        buf = 0.18
    elif span >= 0.38:
        buf = 0.12
    else:
        buf = 0.06
    return {
        "min_x": bbox["min_x"],
        "min_y": bbox["min_y"],
        "max_x": bbox["max_x"],
        "max_y": bbox["max_y"],
        "buffer_deg": buf,
    }


async def render_and_send_plot(raw_result, title_suffix="全流域", admin_raw_result=None, highlight_rivers=None, stations=None):
    """画行政区划底图 + 河网图 + 河流名称（直接从数据库读取真实河流几何）

    参数 highlight_rivers：高亮显示的河流名称列表（如 ["永定河", "大清河"]）。
    当传入该列表时，属于列表的河流会用彩色粗线高亮，其余河流退为浅灰背景。
    """
    from collections import defaultdict

    segments = _unwrap_tool_result(raw_result)
    if isinstance(segments, dict):
        segments = segments.get("segments", [])
    admin_features = _unwrap_tool_result(admin_raw_result) if admin_raw_result is not None else []

    if not segments or not isinstance(segments, list):
        await cl.Message(content=f"⚠️ 未找到相关河网数据，无法绘制（{title_suffix}）。").send()
        return

    # 从 MCP 拓扑结果中提取河段名称与拓扑结构
    topo_segments = []
    river_names = set()
    in_degree = defaultdict(int)
    out_degree = defaultdict(int)

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            x1, y1 = float(seg["from_x"]), float(seg["from_y"])
            x2, y2 = float(seg["to_x"]), float(seg["to_y"])
            order = int(seg.get("strahler_order", 1))
            r_name = seg.get("rivername", "").strip()

            start_node, end_node = (x1, y1), (x2, y2)
            out_degree[start_node] += 1
            in_degree[end_node] += 1

            # 保存 objectid 用于数据库精确匹配河流几何
            objectid = seg.get("objectid") or seg.get("objectid_key") or seg.get("id")

            topo_segments.append({
                "from_x": x1, "from_y": y1,
                "to_x": x2, "to_y": y2,
                "order": order,
                "rivername": r_name,
                "objectid": objectid,
                "start": start_node,
                "end": end_node,
            })
            if r_name and r_name not in ("未知", "None", ""):
                river_names.add(r_name)
        except Exception as e:
            print(f"[绘图] 解析拓扑线段失败: {e}")
            continue

    if not topo_segments:
        await cl.Message(content=f"⚠️ 河网数据为空，无法绘制（{title_suffix}）。").send()
        return

    # 从数据库 haihe_river_directed_full_v5 查询真实河流几何
    import psycopg2
    from psycopg2.extras import RealDictCursor

    db_paths = {}
    use_straight_lines = False

    conn = None
    try:
        conn = _get_river_plot_pg_pool().getconn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for seg_idx, seg in enumerate(topo_segments):
                r_name = seg["rivername"]
                fx, fy = seg["from_x"], seg["from_y"]
                tx, ty = seg["to_x"], seg["to_y"]
                oid = seg.get("objectid")

                # 先用 objectid 精确匹配（最可靠）
                if oid:
                    cur.execute("""
                        SELECT ST_AsGeoJSON(geom) as geojson
                        FROM haihe_river_directed_full_v5
                        WHERE objectid::text = %s AND geom IS NOT NULL
                        LIMIT 1
                    """, (str(oid),))
                    row = cur.fetchone()
                    if row and row["geojson"]:
                        db_paths[(fx, fy, tx, ty)] = row["geojson"]
                        continue

                # 回退 1: 用 from_x/to_x 匹配
                cur.execute("""
                    SELECT ST_AsGeoJSON(geom) as geojson
                    FROM haihe_river_directed_full_v5
                    WHERE ABS(from_x - %s) < 0.01 AND ABS(from_y - %s) < 0.01
                      AND ABS(to_x - %s) < 0.01 AND ABS(to_y - %s) < 0.01
                      AND geom IS NOT NULL
                    LIMIT 1
                """, (fx, fy, tx, ty))
                row = cur.fetchone()
                if row and row["geojson"]:
                    db_paths[(fx, fy, tx, ty)] = row["geojson"]
                elif r_name:
                    # 回退 2: 用河流名称+位置匹配
                    cur.execute("""
                        SELECT ST_AsGeoJSON(geom) as geojson
                        FROM haihe_river_directed_full_v5
                        WHERE river_name = %s
                          AND geom IS NOT NULL
                          AND ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s, %s), 4326), 0.1)
                        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                        LIMIT 1
                    """, (r_name, fx, fy, fx, fy))
                    row = cur.fetchone()
                    if row and row["geojson"]:
                        db_paths[(fx, fy, tx, ty)] = row["geojson"]

        matched = len(db_paths)
        total = len(topo_segments)
        print(f"[绘图] 河段几何匹配: {matched}/{total} 条 ({(matched/total*100) if total else 0:.0f}%)")
    except Exception as e:
        print(f"[绘图] 数据库查询河流几何失败，回退到直线：{e}")
        use_straight_lines = True
    finally:
        if conn is not None:
            try:
                _get_river_plot_pg_pool().putconn(conn)
            except Exception as e:
                print(f"[绘图] 归还数据库连接失败: {e}")

    # 解析 GeoJSON 坐标为折线
    def _geojson_to_coords(geojson_str):
        """将 GeoJSON LineString 字符串转为 [(lon,lat),...] 列表"""
        import json
        try:
            geom = json.loads(geojson_str) if isinstance(geojson_str, str) else geojson_str
            if geom.get("type") == "LineString":
                return [(float(p[0]), float(p[1])) for p in geom["coordinates"]]
            elif geom.get("type") == "MultiLineString":
                # 取最长的一条
                lines = []
                for line in geom["coordinates"]:
                    coords = [(float(p[0]), float(p[1])) for p in line]
                    lines.append(coords)
                return max(lines, key=len) if lines else None
        except Exception as e:
            print(f"[绘图] 解析 GeoJSON 坐标失败: {e}")
            return None
        return None

    # 合并绘制数据
    draw_segments = []
    all_x, all_y = [], []

    for seg in topo_segments:
        key = (seg["from_x"], seg["from_y"], seg["to_x"], seg["to_y"])
        path_coords = None

        if not use_straight_lines and key in db_paths:
            path_coords = _geojson_to_coords(db_paths[key])

        if path_coords and len(path_coords) >= 2:
            xs = [p[0] for p in path_coords]
            ys = [p[1] for p in path_coords]
        else:
            xs = [seg["from_x"], seg["to_x"]]
            ys = [seg["from_y"], seg["to_y"]]

        draw_segments.append({
            "xs": xs, "ys": ys,
            "order": seg["order"],
            "rivername": seg["rivername"],
            "start": seg["start"],
            "end": seg["end"],
            "is_curved": len(xs) > 2,
        })
        all_x.extend(xs)
        all_y.extend(ys)

    if not draw_segments:
        await cl.Message(content=f"⚠️ 河网数据为空，无法绘制（{title_suffix}）。").send()
        return

    bbox = _calc_bbox(all_x, all_y)

    fig, ax = plt.subplots(figsize=(12, 10))

    # 高亮集合：优先使用传入列表，同时识别 segments 自带的 is_affected 标记
    highlight_set = set(highlight_rivers) if highlight_rivers else set()
    for seg in segments:
        if isinstance(seg, dict) and seg.get("is_affected"):
            name = str(seg.get("rivername", "")).strip()
            if name and name not in ("未知", "None", ""):
                highlight_set.add(name)

    # 0. 先画行政区划底图
    if isinstance(admin_features, list):
        for feat in admin_features:
            if not isinstance(feat, dict):
                continue
            polygons = feat.get("polygons", [])
            feat_name = feat.get("name", "")
            label_drawn = False

            for poly in polygons:
                if not isinstance(poly, dict):
                    continue
                outer = poly.get("outer", [])
                holes = poly.get("holes", [])

                if len(outer) < 3:
                    continue

                pxs = [p[0] for p in outer]
                pys = [p[1] for p in outer]

                ax.fill(pxs, pys, facecolor="#F2F2F2", edgecolor="#B5B5B5",
                        linewidth=0.8, alpha=0.65, zorder=0)

                for hole in holes:
                    if len(hole) < 3:
                        continue
                    hx = [p[0] for p in hole]
                    hy = [p[1] for p in hole]
                    ax.fill(hx, hy, facecolor="white", edgecolor="#D5D5D5",
                            linewidth=0.4, alpha=1.0, zorder=0.1)

                if (not label_drawn) and feat_name:
                    use_x = pxs[:-1] if len(pxs) > 1 else pxs
                    use_y = pys[:-1] if len(pys) > 1 else pys
                    if use_x and use_y:
                        cx = sum(use_x) / len(use_x)
                        cy = sum(use_y) / len(use_y)
                        if bbox and (bbox["min_x"] <= cx <= bbox["max_x"]) and (bbox["min_y"] <= cy <= bbox["max_y"]):
                            ax.text(cx, cy, feat_name, fontsize=8, color="#666666",
                                    ha="center", va="center", zorder=0.2, clip_on=True,
                                    bbox=dict(facecolor="white", alpha=0.45, edgecolor="none",
                                              boxstyle="round,pad=0.15"))
                        label_drawn = True

    is_thematic = bool(highlight_set or stations)

    # 1. 画河道（真实折线）与流向箭头
    for seg in draw_segments:
        xs, ys = seg["xs"], seg["ys"]
        order = seg["order"]
        is_highlight = bool(highlight_set and seg["rivername"] in highlight_set)

        if is_highlight:
            # 受影响河段统一用一种醒目颜色，不再按 Strahler 等级分色
            color = "#C0392B"
            lw = 2.0 + (order * 0.8)
            alpha = 0.95
            arrow_color = "#8B0000"
            zorder = 2
        else:
            color = "#B8C5D0" if highlight_set else "#2B7EC1"
            lw = 0.6 if highlight_set else 0.8 + (order * 1.5)
            alpha = 0.45 if highlight_set else 0.9
            arrow_color = "#7F8C8D" if highlight_set else "#1A4E7A"
            zorder = 1

        ax.plot(xs, ys, color=color, linewidth=lw, alpha=alpha,
                solid_capstyle='round', zorder=zorder)

        # 流向箭头画在河段中间；专题图模式下只画受影响河段
        n = len(xs)
        if n >= 2 and (not is_thematic or is_highlight):
            arrow_pos = n // 2
            if arrow_pos >= n - 1:
                arrow_pos = max(0, n - 2)
            ax.annotate(
                '',
                xy=(xs[arrow_pos + 1], ys[arrow_pos + 1]),
                xytext=(xs[arrow_pos], ys[arrow_pos]),
                arrowprops=dict(arrowstyle="->", color=arrow_color, lw=1.5, shrinkA=0, shrinkB=0),
                zorder=zorder + 1
            )

    # 2. 专题图模式：不画拓扑节点，改画暴雨及以上站点
    if stations:
        print(f"[绘图] 开始叠加暴雨站点：共 {len(stations)} 个")
        level_colors = {
            "特大暴雨": "#8B0000",
            "大暴雨": "#E67E22",
            "暴雨": "#F1C40F",
        }
        level_groups: dict[str, list[tuple]] = {"特大暴雨": [], "大暴雨": [], "暴雨": []}
        valid_count = 0
        for s in stations:
            if not isinstance(s, dict):
                continue
            try:
                lon = float(s.get("lon", s.get("longitude", s.get("x"))))
                lat = float(s.get("lat", s.get("latitude", s.get("y"))))
                level = str(s.get("level", "暴雨")).strip()
                if level in level_groups:
                    level_groups[level].append((lon, lat))
                    valid_count += 1
            except Exception as e:
                print(f"[绘图] 站点坐标解析失败: {e}")
                continue
        print(f"[绘图] 有效站点坐标：{valid_count} 个")

        for level, coords in level_groups.items():
            if not coords:
                continue
            xs, ys = zip(*coords)
            ax.scatter(xs, ys, c=level_colors[level], s=60, zorder=5,
                       edgecolors='white', linewidths=0.6, label=level)
    elif not is_thematic:
        # 普通水系拓扑图保留拓扑节点
        all_nodes = set(in_degree.keys()) | set(out_degree.keys())
        for node in all_nodes:
            in_d, out_d = in_degree[node], out_degree[node]
            if in_d == 0:
                color, size = "green", 40
            elif out_d == 0:
                color, size = "red", 70
            elif in_d > 1:
                color, size = "orange", 40
            else:
                color, size = "blue", 20

            ax.scatter(node[0], node[1], color=color, s=size, zorder=3,
                       edgecolors='white', linewidths=0.5)

    # 3. 河流名称标注（高亮模式下只标注高亮河流）
    river_label_coords = defaultdict(list)
    for seg in draw_segments:
        name = seg["rivername"]
        if name and name not in ("未知", "None", ""):
            if highlight_set and name not in highlight_set:
                continue
            xs, ys = seg["xs"], seg["ys"]
            mid_idx = len(xs) // 2
            river_label_coords[name].append((xs[mid_idx], ys[mid_idx]))

    for name, coords in river_label_coords.items():
        mid_index = len(coords) // 2
        pos_x, pos_y = coords[mid_index]
        ax.text(pos_x, pos_y, name, fontsize=10, color='#8B0000',
                fontweight='bold', ha='center', va='center', zorder=4,
                bbox=dict(facecolor='white', alpha=0.75, edgecolor='none',
                          boxstyle='round,pad=0.2'))

    if highlight_set:
        title = "海河流域暴雨影响河系专题图"
        subtitle = f"统计时段：{title_suffix}  |  降雨阈值：≥50mm（暴雨）  |  受影响河系：{len(highlight_set)} 条"
    else:
        title = f"海河流域水系拓扑图 - {title_suffix}"
        subtitle = ""
    ax.set_title(title, fontsize=14, fontweight='bold', pad=10)
    if subtitle:
        ax.text(0.5, 1.02, subtitle, transform=ax.transAxes,
                ha='center', va='bottom', fontsize=10, color='#555555')
    ax.set_xlabel("经度")
    ax.set_ylabel("纬度")
    ax.grid(True, linestyle="--", alpha=0.35, color='#CCCCCC')
    ax.set_facecolor('#FAFAFA')
    ax.set_aspect('equal', adjustable='box')

    # 高亮/专题模式下添加完整图例
    if is_thematic:
        from matplotlib.patches import Patch
        from matplotlib.lines import Line2D
        legend_elements = []
        if highlight_set:
            legend_elements.extend([
                Patch(facecolor='#C0392B', edgecolor='#C0392B', label='受影响河段'),
                Patch(facecolor='#B8C5D0', edgecolor='#B8C5D0', label='未受影响河系（背景）'),
                Line2D([0], [0], color='#8B0000', linewidth=2, marker='>',
                       markersize=6, label='河流流向（上游→下游）'),
            ])
        if stations:
            legend_elements.extend([
                Line2D([0], [0], marker='o', color='w', markerfacecolor='#8B0000',
                       markersize=10, label='特大暴雨站点（≥250mm）'),
                Line2D([0], [0], marker='o', color='w', markerfacecolor='#E67E22',
                       markersize=9, label='大暴雨站点（100-250mm）'),
                Line2D([0], [0], marker='o', color='w', markerfacecolor='#F1C40F',
                       markersize=8, label='暴雨站点（50-100mm）'),
            ])
        if legend_elements:
            ax.legend(handles=legend_elements, loc='lower right', fontsize=8,
                      title='图例说明', title_fontsize=9, framealpha=0.95,
                      ncol=1, handlelength=2.5)

    if bbox:
        ax.set_xlim(bbox["min_x"], bbox["max_x"])
        ax.set_ylim(bbox["min_y"], bbox["max_y"])

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=200)
    plt.close(fig)
    buf.seek(0)

    if is_thematic:
        station_count = len(stations) if stations else 0
        river_count = len(highlight_set) if highlight_set else 0
        caption = (
            f"📊 已生成暴雨影响河系专题图：统计时段 **{title_suffix}**，"
            f"识别出 **{station_count} 个暴雨及以上站点**（彩色圆点）"
        )
        if river_count > 0:
            caption += f"及 **{river_count} 条受影响河系**（红色粗线，下游约 50km 跟踪）"
        caption += "，其余河系为浅灰背景。"
    else:
        caption = f"📊 已生成【{title_suffix}】水系拓扑图："

    await cl.Message(
        content=caption,
        elements=[cl.Image(content=buf.getvalue(), name="river_network_with_admin")],
    ).send()


def _build_messages_from_thread(thread: ThreadDict | dict | None):
    """
    将历史线程的 step 记录恢复为 LangChain 消息序列，支持续聊上下文。
    """
    if not thread or not isinstance(thread, dict):
        return []

    resumed_messages = []
    steps = thread.get("steps", [])
    if not isinstance(steps, list):
        return resumed_messages

    for step in steps:
        if not isinstance(step, dict):
            continue
        step_type = str(step.get("type") or "").strip()
        output_text = str(step.get("output") or "").strip()
        input_text = str(step.get("input") or "").strip()

        if step_type == "user_message":
            text = output_text or input_text
            if text:
                resumed_messages.append(HumanMessage(content=text))
        elif step_type == "assistant_message":
            if output_text:
                resumed_messages.append(AIMessage(content=output_text))

    return resumed_messages


async def _init_runtime_session(messages_seed=None):
    """
    初始化会话运行时对象；用于首聊和历史线程恢复。
    """
    await _ensure_chainlit_tables()

    planner_llm = ChatOpenAI(
        model="Qwen3.6-27B",
        streaming=True,
        temperature=0.7,
        openai_api_base="http://10.226.188.156:8000/v1/",
        openai_api_key="EMPTY",
    )
    answer_llm = ChatOpenAI(
        model="Qwen3.6-27B",
        streaming=True,
        temperature=0.7,
        openai_api_base="http://10.226.188.156:8000/v1/",
        openai_api_key="EMPTY",
    )

    tools = await load_sse_tools()
    tools = tools + build_external_skill_tools() + build_rain_analysis_tools()
    print(f"✅ 本地工具已合并，当前工具列表：{[t.name for t in tools]}")
    weekday_map = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    today_str = datetime.now().strftime("%Y年%m月%d日")
    weekday_str = weekday_map[datetime.now().weekday()]
    prompt_prefix = f"【当前日期：{today_str}（{weekday_str}）】请基于这个当前日期来理解用户的相对时间表述（如今天、明天、周末等）。\n\n"
    prompt_template = ChatPromptTemplate.from_messages([
        ("system", f"{prompt_prefix}{WEATHER_ASSISTANT_PROMPT}"),
        MessagesPlaceholder(variable_name="messages"),
    ])
    planner_chain = prompt_template | planner_llm.bind_tools(tools)
    answer_chain = prompt_template | answer_llm

    cl.user_session.set("planner_chain", planner_chain)
    cl.user_session.set("answer_chain", answer_chain)
    cl.user_session.set("tools", tools)
    cl.user_session.set("messages", messages_seed if isinstance(messages_seed, list) else [])


def _need_river_plot(user_text: str) -> bool:
    """识别用户是否在问河网/水系结构可视化"""
    if not user_text:
        return False
    text = user_text.strip()
    # 排除降雨分布图/降水实况图相关查询
    rainfall_img_keywords = ["降雨分布图", "降水实况图", "面雨量分布图", "实况图"]
    if any(k in text for k in rainfall_img_keywords):
        return False
    # 排除历史极端天气图表查询
    weather_chart_keywords = ["浓雾", "高温分布", "寒潮", "大风统计",
                               "低能见度", "热力图", "极端天气", "气象要素",
                               "事件分布图", "统计分析图"]
    if any(k in text for k in weather_chart_keywords):
        return False
    plot_keywords = ["河网", "河道图", "水系图", "拓扑图", "流域图", "路径图", "示意图", "分布图"]
    verb_keywords = ["画", "绘", "生成", "展示", "看", "给出", "出一下", "可视化", "图"]
    has_plot_topic = any(k in text for k in plot_keywords)
    has_visual_intent = any(k in text for k in verb_keywords)
    return has_plot_topic or ("河" in text and has_visual_intent)


def _extract_river_name(user_text: str) -> str:
    """从用户问题里尽量提取“某某河”，提取不到就用全流域"""
    if not user_text:
        return "全流域"

    normalized = re.sub(r"[\s，。！？、,.!?：:；;（）()]", "", user_text)
    action_prefix_pattern = (
        r"(帮我|请|给我|给|麻烦|帮忙|我想|想看|看下|看一下|查看|展示|生成|绘制|画一下|画出|画个|画|一下|把)"
    )

    # 优先：在“河网/拓扑图/水系图”语境中抓取目标河名
    scene_match = re.search(
        r"([\u4e00-\u9fa5]{1,20}?)(?:的)?(?:河网|河道图|水系图|拓扑图|流域图|路径图|示意图|分布图)",
        normalized,
    )
    if scene_match:
        prefix = scene_match.group(1)
        prefix = re.sub(action_prefix_pattern, "", prefix)
        prefix = prefix.rstrip("的")
        tail_match = re.search(r"([\u4e00-\u9fa5]{1,8}河)$", prefix)
        if tail_match:
            return tail_match.group(1)

    # 兜底：遍历“xx河”候选，剔除口语前缀、结构助词污染
    candidates = []
    for m in re.finditer(r"([\u4e00-\u9fa5]{1,12}河)", normalized):
        cand = m.group(1)
        cand = re.sub(rf"^(?:{action_prefix_pattern})+", "", cand)
        if "的" in cand:
            cand = cand.split("的")[0]
        cand = cand.strip()
        if re.fullmatch(r"[\u4e00-\u9fa5]{1,8}河", cand):
            candidates.append(cand)
    if candidates:
        return max(candidates, key=len)

    return "全流域"


async def _build_admin_overlay_for_plot(tools, river_observation):
    """根据河网结果自动计算 bbox 并拉取行政区划底图。"""
    admin_tool = next((t for t in tools if t.name == "get_admin_division_for_plot"), None)
    if not admin_tool:
        return None

    seg_list = _unwrap_tool_result(river_observation)
    xs, ys = [], []
    if isinstance(seg_list, list):
        for seg in seg_list:
            if not isinstance(seg, dict):
                continue
            try:
                xs.extend([float(seg["from_x"]), float(seg["to_x"])])
                ys.extend([float(seg["from_y"]), float(seg["to_y"])])
            except Exception as e:
                print(f"[GIS] 河段坐标解析失败: {e}")
                continue

    bbox = _calc_bbox(xs, ys)
    if not bbox:
        return None

    return await admin_tool.ainvoke(_kwargs_for_admin_division_plot(bbox))


def _tool_observation_to_text(observation):
    data = _unwrap_tool_result(observation)

    if isinstance(data, dict) and isinstance(data.get("contexts"), list):
        parts = []
        sources = [str(s).strip() for s in data.get("sources", []) if str(s).strip()]
        if sources:
            parts.append("知识库来源文件：" + "、".join(f"《{s}》" for s in sources))
            parts.append("回答开头必须使用固定句式：根据知识库中的" + "".join(f"《{s}》" for s in sources) + "……")

        for idx, ctx in enumerate(data["contexts"], 1):
            if not isinstance(ctx, dict):
                continue
            content = str(ctx.get("content") or "").strip()
            if not content:
                continue
            source = str(ctx.get("source") or "").strip()
            score = ctx.get("score")
            parts.append(
                f"[知识库片段{idx}]\n"
                f"来源：《{source}》\n"
                f"分数：{score}\n"
                f"内容：{content}"
            )

        if parts:
            return "\n\n".join(parts)

    # 水位查询结果：直接格式化为 Markdown 表格，避免模型自己生成畸形表格
    if isinstance(data, dict) and isinstance(data.get("records"), list) and data["records"]:
        records = data["records"]
        if records and isinstance(records[0], dict) and ("water_level_m" in records[0] or "水位(m)" in records[0]):
            lines = ["| 站点名称 | 当前水位(m) | 警戒水位(m) | 超警戒(m) | 涨率 | 更新时间 |", "| :--- | :--- | :--- | :--- | :--- | :--- |"]
            for r in records:
                if not isinstance(r, dict):
                    continue
                name = r.get("station_name") or "-"
                wl = r.get("water_level_m")
                if wl is None:
                    wl = r.get("水位(m)")
                warn = r.get("warning_level_m")
                if warn is None:
                    warn = r.get("警戒水位(m)")
                over = r.get("超警戒(m)", "-")
                rate = r.get("涨率", "-")
                if rate == "" or rate is None:
                    rate = "-"
                time = r.get("time") or "-"

                def _fmt_water(v):
                    if isinstance(v, (int, float)):
                        return f"{v:.2f}"
                    return str(v) if v is not None else "-"

                lines.append(f"| {_fmt_water(name)} | {_fmt_water(wl)} | {_fmt_water(warn)} | {_fmt_water(over)} | {_fmt_water(rate)} | {_fmt_water(time)} |")
            source = data.get("source") or "十四所水位接口"
            return "\n".join(lines) + f"\n\n**数据来源**：{source}"

    if isinstance(observation, list) and observation and isinstance(observation[0], dict) and "text" in observation[0]:
        return observation[0].get("text", str(observation))
    if isinstance(observation, dict):
        return observation.get("text", str(observation))
    return str(observation)


def _guess_gis_scene(user_text: str) -> str:
    text = (user_text or "").strip()
    if "下流" in text and "河" in text:
        return "river_downstream"
    if ("分区" in text or "行政区" in text) and "河" in text:
        return "district_rivers"
    # 兼容“天津市有哪些河流”“海淀区有哪些河流”这类口语问法
    if ("哪些河流" in text or "有什么河流" in text or "河流有哪些" in text) and any(k in text for k in ["市", "区", "县"]):
        return "district_rivers"
    if ("站点" in text or "国家站" in text) and ("观测" in text or "气象" in text):
        return "realtime_station"
    if "应急" in text and "河" in text:
        return "emergency_rivers"
    if "应急" in text and ("分区" in text or "行政区" in text):
        return "emergency_districts"
    return "generic"


def _segments_to_geojson(segments):
    features = []
    if not isinstance(segments, list):
        return {"type": "FeatureCollection", "features": []}

    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        try:
            x1, y1 = float(seg["from_x"]), float(seg["from_y"])
            x2, y2 = float(seg["to_x"]), float(seg["to_y"])
        except Exception as e:
            print(f"[GIS] 线段坐标解析失败: {e}")
            continue

        locate_id = seg.get("id") or seg.get("segment_id") or f"river_seg_{i+1}"
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[x1, y1], [x2, y2]]},
                "properties": {
                    "locate_id": str(locate_id),
                    "river_name": seg.get("rivername", ""),
                    "length_km": seg.get("length_km"),
                    # 业务口径：距离列默认展示该河段长度
                    "distance_km": seg.get("distance") or seg.get("length_km") or seg.get("length"),
                    "strahler_order": seg.get("strahler_order"),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _station_rows_to_geojson(rows):
    features = []
    if not isinstance(rows, list):
        return {"type": "FeatureCollection", "features": []}

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        lon = row.get("lon", row.get("lng", row.get("longitude")))
        lat = row.get("lat", row.get("latitude"))
        if lon is None or lat is None:
            continue
        try:
            lon = float(lon); lat = float(lat)
        except Exception as e:
            print(f"[GIS] 站点经纬度解析失败: {e}")
            continue

        locate_id = (
            row.get("locate_id")
            or row.get("station_id")
            or row.get("stcd")
            or row.get("id")
            or f"station_{i+1}"
        )
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {
                    "locate_id": str(locate_id),
                    "station_name": row.get("station_name") or row.get("name") or "",
                    "temperature": row.get("temperature"),
                    "humidity": row.get("humidity"),
                    "wind": row.get("wind"),
                    "pressure": row.get("pressure"),
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


def _extract_station_rows(data):
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []

    candidate_keys = ["stations", "station_list", "station_data", "obs_list", "records", "items"]
    for key in candidate_keys:
        val = data.get(key)
        if isinstance(val, list) and val and isinstance(val[0], dict):
            return val
    return []


def _extract_emergency_tables(data):
    if not isinstance(data, dict):
        return []

    # 兼容你给的 basin_judgment 实例（slots[].judgment.evidence.checks）
    slots = data.get("slots")
    if isinstance(slots, list) and slots:
        latest = slots[-1] if isinstance(slots[-1], dict) else {}
        judgment = latest.get("judgment") if isinstance(latest.get("judgment"), dict) else {}
        evidence = judgment.get("evidence") if isinstance(judgment.get("evidence"), dict) else {}
        checks = evidence.get("checks") if isinstance(evidence.get("checks"), list) else []
        check_rows = []
        for c in checks:
            if not isinstance(c, dict):
                continue
            e = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
            check_rows.append(
                {
                    "candidate_level": c.get("candidate_level"),
                    "triggered": c.get("triggered"),
                    "window_hours": e.get("window_hours"),
                    "rain_label": e.get("rain_label"),
                    "threshold_mm": e.get("threshold_mm"),
                    "qualified_station_count": e.get("qualified_station_count"),
                    "qualified_adjacent_station_count": e.get("qualified_adjacent_station_count"),
                    "sustained_station_count": e.get("sustained_station_count"),
                    "ratio": e.get("ratio"),
                    "locate_id": str(c.get("candidate_level") or ""),
                }
            )

        summary_rows = [
            {
                "times": latest.get("times") or latest.get("times_compact"),
                "reached": judgment.get("reached"),
                "level": judgment.get("level"),
                "message": judgment.get("message"),
                "total_station_count": evidence.get("total_station_count"),
                "neighbor_km": evidence.get("neighbor_km"),
                "sustain_hourly_threshold_mm": evidence.get("sustain_hourly_threshold_mm"),
                "locate_id": str(latest.get("times") or ""),
            }
        ]

        return [
            {
                "id": "emergency_summary",
                "title": "应急响应判定",
                "columns": [
                    "times",
                    "reached",
                    "level",
                    "message",
                    "total_station_count",
                    "neighbor_km",
                    "sustain_hourly_threshold_mm",
                    "locate_id",
                ],
                "rows": summary_rows,
            },
            {
                "id": "emergency_level_checks",
                "title": "等级判定明细",
                "columns": [
                    "candidate_level",
                    "triggered",
                    "window_hours",
                    "rain_label",
                    "threshold_mm",
                    "qualified_station_count",
                    "qualified_adjacent_station_count",
                    "sustained_station_count",
                    "ratio",
                    "locate_id",
                ],
                "rows": check_rows,
            },
        ]

    # 兼容河流应急：直接/间接影响河流
    direct = data.get("direct_rivers") if isinstance(data.get("direct_rivers"), list) else []
    indirect = data.get("indirect_rivers") if isinstance(data.get("indirect_rivers"), list) else []
    if direct or indirect:
        return [
            {
                "id": "direct_rivers",
                "title": "直接影响河流",
                "columns": ["name", "length_km", "distance_km", "locate_id"],
                "rows": direct,
            },
            {
                "id": "indirect_rivers",
                "title": "间接影响河流",
                "columns": ["name", "length_km", "distance_km", "locate_id"],
                "rows": indirect,
            },
        ]

    return []


def _to_float(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _build_table(id_: str, title: str, columns, rows):
    col_keys = [c["key"] for c in columns]
    safe_rows = rows if isinstance(rows, list) else []
    # grid 为按 columns 顺序展开的二维数组，前端可直接用于表格组件渲染
    grid = [
        [r.get(k) if isinstance(r, dict) else None for k in col_keys]
        for r in safe_rows
    ]
    return {
        "id": id_,
        "title": title,
        "columns": col_keys,
        "column_defs": columns,  # 给前端更清晰的列定义（key + label）
        "rows": safe_rows,
        "row_count": len(safe_rows),
        "col_count": len(col_keys),
        "grid": grid,
    }


def _aggregate_river_rows_from_geojson(geojson):
    features = geojson.get("features", []) if isinstance(geojson, dict) else []
    grouped = {}
    for f in features:
        if not isinstance(f, dict):
            continue
        p = f.get("properties", {}) if isinstance(f.get("properties"), dict) else {}
        name = (p.get("river_name") or "未命名河流").strip()
        locate_id = str(p.get("locate_id") or "")
        length = _to_float(p.get("length_km"))
        distance = _to_float(p.get("distance_km"))
        if name not in grouped:
            grouped[name] = {"river_name": name, "length_km": 0.0, "distance_km": None, "locate_id": locate_id}
        if length is not None:
            grouped[name]["length_km"] += length
        if grouped[name]["distance_km"] is None and distance is not None:
            grouped[name]["distance_km"] = distance
        if not grouped[name]["locate_id"] and locate_id:
            grouped[name]["locate_id"] = locate_id

    rows = list(grouped.values())
    # 业务口径统一：距离=河长（聚合后总长度）
    for r in rows:
        r["distance_km"] = r.get("length_km")
    rows.sort(key=lambda r: ((r["distance_km"] is None), r["distance_km"] or 0.0, -(r["length_km"] or 0.0)))
    return rows


def _extract_table_rows(panel_tables, table_id: str):
    if not isinstance(panel_tables, list):
        return []
    for t in panel_tables:
        if isinstance(t, dict) and t.get("id") == table_id and isinstance(t.get("rows"), list):
            return t["rows"]
    return []


def _replace_table_rows(panel_tables, table_id: str, rows):
    if not isinstance(panel_tables, list):
        return
    for t in panel_tables:
        if isinstance(t, dict) and t.get("id") == table_id:
            t["rows"] = rows
            return


def _build_multiline_feature_from_geojson(river_name: str, locate_id: str, geojson: dict, length_km):
    if not isinstance(geojson, dict):
        return None
    feats = geojson.get("features")
    if not isinstance(feats, list) or not feats:
        return None

    lines = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        g = f.get("geometry")
        if not isinstance(g, dict):
            continue
        if g.get("type") == "LineString" and isinstance(g.get("coordinates"), list):
            lines.append(g.get("coordinates"))
        elif g.get("type") == "MultiLineString" and isinstance(g.get("coordinates"), list):
            lines.extend(g.get("coordinates"))

    if not lines:
        return None

    return {
        "type": "Feature",
        "geometry": {"type": "MultiLineString", "coordinates": lines},
        "properties": {
            "locate_id": str(locate_id),
            "river_name": river_name,
            "length_km": length_km,
            "distance_km": length_km,
        },
    }


def _extract_emergency_river_tables(data):
    if not isinstance(data, dict):
        return []

    direct = data.get("direct_rivers") if isinstance(data.get("direct_rivers"), list) else []
    indirect = data.get("indirect_rivers") if isinstance(data.get("indirect_rivers"), list) else []
    if not direct and not indirect:
        return []

    def norm_rows(items, prefix):
        rows = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            rows.append(
                {
                    "river_name": it.get("river_name") or it.get("name"),
                    "length_km": it.get("length_km"),
                    "distance_km": it.get("distance_km") or it.get("distance"),
                    "locate_id": str(it.get("locate_id") or it.get("id") or f"{prefix}_{i+1}"),
                }
            )
        return rows

    cols = [
        {"key": "river_name", "label": "河名称"},
        {"key": "length_km", "label": "河长(km)"},
        {"key": "distance_km", "label": "距离(km)"},
        {"key": "locate_id", "label": "定位ID"},
    ]
    return [
        _build_table("direct_rivers", "直接影响河流", cols, norm_rows(direct, "direct")),
        _build_table("indirect_rivers", "间接影响河流", cols, norm_rows(indirect, "indirect")),
    ]


def _extract_emergency_river_geojson(data):
    if not isinstance(data, dict):
        return {"type": "FeatureCollection", "features": []}

    features = []
    for group, color in [("direct_rivers", "#27AE60"), ("indirect_rivers", "#2F80ED")]:
        items = data.get(group) if isinstance(data.get(group), list) else []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            geom = it.get("geometry")
            if isinstance(geom, dict) and geom.get("type") and geom.get("coordinates"):
                features.append(
                    {
                        "type": "Feature",
                        "geometry": geom,
                        "properties": {
                            "group": group,
                            "style_color": color,
                            "river_name": it.get("river_name") or it.get("name"),
                            "length_km": it.get("length_km"),
                            "distance_km": it.get("distance_km") or it.get("distance"),
                            "locate_id": str(it.get("locate_id") or it.get("id") or f"{group}_{i+1}"),
                        },
                    }
                )
                continue

            # 兼容 from_x/from_y/to_x/to_y 的线段格式
            try:
                x1, y1 = float(it.get("from_x")), float(it.get("from_y"))
                x2, y2 = float(it.get("to_x")), float(it.get("to_y"))
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[x1, y1], [x2, y2]]},
                        "properties": {
                            "group": group,
                            "style_color": color,
                            "river_name": it.get("river_name") or it.get("name"),
                            "length_km": it.get("length_km"),
                            "distance_km": it.get("distance_km") or it.get("distance"),
                            "locate_id": str(it.get("locate_id") or it.get("id") or f"{group}_{i+1}"),
                        },
                    }
                )
            except Exception as e:
                print(f"[GIS] 应急河流坐标提取失败: {e}")
                continue

    return {"type": "FeatureCollection", "features": features}


def _extract_emergency_district_tables(data):
    if not isinstance(data, dict):
        return []

    # 优先读结构化分区/行政区字段
    candidates = (
        data.get("affected_districts")
        or data.get("affected_admin_units")
        or data.get("districts")
        or data.get("admin_units")
    )
    rows = []
    if isinstance(candidates, list):
        for i, it in enumerate(candidates):
            if not isinstance(it, dict):
                continue
            rows.append(
                {
                    "district_name": it.get("district_name") or it.get("name") or it.get("admin_name"),
                    "time_range": it.get("time_range") or it.get("times"),
                    "areal_rainfall_mm": it.get("areal_rainfall_mm") or it.get("accumulated_rainfall"),
                    "locate_id": str(it.get("locate_id") or it.get("id") or f"district_{i+1}"),
                }
            )

    # 兼容 basin_judgment：至少返回时段与等级信息，不让前端拿到空表
    if not rows and isinstance(data.get("slots"), list):
        for i, slot in enumerate(data.get("slots", [])):
            if not isinstance(slot, dict):
                continue
            judgment = slot.get("judgment") if isinstance(slot.get("judgment"), dict) else {}
            rows.append(
                {
                    "district_name": "海河流域",
                    "time_range": slot.get("times") or slot.get("times_compact"),
                    "areal_rainfall_mm": None,
                    "locate_id": str(slot.get("times") or f"slot_{i+1}"),
                    "level": judgment.get("level"),
                    "reached": judgment.get("reached"),
                }
            )

    cols = [
        {"key": "district_name", "label": "分区/行政区"},
        {"key": "time_range", "label": "时段"},
        {"key": "areal_rainfall_mm", "label": "面累计降水量(mm)"},
        {"key": "locate_id", "label": "定位ID"},
    ]
    return [_build_table("emergency_districts", "应急影响分区/行政区", cols, rows)] if rows else []


def _extract_emergency_district_geojson(data):
    if not isinstance(data, dict):
        return {"type": "FeatureCollection", "features": []}

    # 优先使用标准 geojson
    if isinstance(data.get("geojson"), dict):
        return data.get("geojson")

    # 兼容 districts/admin_units 内嵌 geometry
    candidates = (
        data.get("affected_districts")
        or data.get("affected_admin_units")
        or data.get("districts")
        or data.get("admin_units")
        or []
    )
    features = []
    if isinstance(candidates, list):
        for i, it in enumerate(candidates):
            if not isinstance(it, dict):
                continue
            geom = it.get("geometry")
            if not (isinstance(geom, dict) and geom.get("type") and geom.get("coordinates")):
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {
                        "district_name": it.get("district_name") or it.get("name") or it.get("admin_name"),
                        "time_range": it.get("time_range") or it.get("times"),
                        "areal_rainfall_mm": it.get("areal_rainfall_mm") or it.get("accumulated_rainfall"),
                        "locate_id": str(it.get("locate_id") or it.get("id") or f"district_{i+1}"),
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


def _extract_geojson_and_tables(tool_name: str, observation, scene: str):
    data = _unwrap_tool_result(observation)
    geojson = {"type": "FeatureCollection", "features": []}
    tables = []
    layers = []

    # 河网工具：后端常返回河段列表，这里统一转 geojson
    if tool_name == "get_river_network_for_plot":
        geojson = _segments_to_geojson(data)
        if scene == "river_downstream":
            layers = [
                {"id": "current_river", "color": "#2F80ED", "name": "当前河流"},
                {"id": "downstream_rivers", "color": "#27AE60", "name": "下流河系"},
            ]
            rows = _aggregate_river_rows_from_geojson(geojson)
            tables.append(
                _build_table(
                    "downstream_rivers",
                    "下流河系列表",
                    [
                        {"key": "river_name", "label": "河名称"},
                        {"key": "length_km", "label": "河长(km)"},
                        {"key": "distance_km", "label": "距离(km)"},
                        {"key": "locate_id", "label": "定位ID"},
                    ],
                    rows,
                )
            )
        elif scene == "district_rivers":
            rows = []
            for f in geojson["features"]:
                p = f.get("properties", {})
                rows.append(
                    {
                        "river_name": p.get("river_name") or "未命名河段",
                        "length_km": p.get("length_km"),
                        "locate_id": p.get("locate_id"),
                    }
                )
            tables.append(_build_table("district_rivers", "河系列表", [
                {"key": "river_name", "label": "河名称"},
                {"key": "length_km", "label": "河长(km)"},
                {"key": "locate_id", "label": "定位ID"},
            ], rows))
    elif tool_name == "get_xialiu_rivername":
        # 兼容仅返回“逗号分隔河名”的场景，至少保证前端面板有可渲染表格
        names = []
        if isinstance(data, str):
            names = [s.strip() for s in data.split(",") if s.strip()]
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item.strip():
                    names.append(item.strip())
                elif isinstance(item, dict):
                    n = str(item.get("name") or item.get("river") or "").strip()
                    if n:
                        names.append(n)
        elif isinstance(data, dict):
            raw_names = data.get("downstream_rivers") or data.get("rivers") or []
            if isinstance(raw_names, list):
                for item in raw_names:
                    if isinstance(item, str) and item.strip():
                        names.append(item.strip())
                    elif isinstance(item, dict):
                        n = str(item.get("name") or item.get("river") or "").strip()
                        if n:
                            names.append(n)

        if names:
            tables = [
                _build_table("downstream_rivers", "下流河系列表", [
                    {"key": "river_name", "label": "河名称"},
                    {"key": "length_km", "label": "河长(km)"},
                    {"key": "distance_km", "label": "距离(km)"},
                    {"key": "locate_id", "label": "定位ID"},
                ], [
                    {"river_name": n, "length_km": None, "distance_km": None, "locate_id": f"downstream_{i+1}"}
                    for i, n in enumerate(names)
                ]),
            ]

    # 行政区河流定位工具：返回行政区匹配结果 + 河流清单
    # 约定：panel.rows[*].locate_id 与 map.geojson.features[*].properties.locate_id 一致，方便前端联动。
    if tool_name == "locate_region_rivers":
        rows = []
        features = []

        def _split_river_names(name_text: str) -> list[str]:
            # 兼容“潮白新河,蓟运河”“京杭大运河、子牙河”这种复合名称
            if not isinstance(name_text, str):
                return []
            text = name_text.replace("，", ",").replace("、", ",").strip()
            if not text:
                return []
            return [s.strip() for s in text.split(",") if s.strip()]

        def _pick_first_numeric(item: dict, keys: list[str]):
            if not isinstance(item, dict):
                return None
            for k in keys:
                v = _to_float(item.get(k))
                if v is not None:
                    return v
            return None

        def _build_row_from_item(item: dict | str, i: int, default_locate_prefix: str):
            if isinstance(item, str):
                names = _split_river_names(item)
                return [
                    {
                        "river_name": n,
                        "length_km": None,
                        "distance_km": None,
                        "locate_id": f"{default_locate_prefix}_{i+1}_{j+1}",
                    }
                    for j, n in enumerate(names)
                ]

            if not isinstance(item, dict):
                return []

            raw_name = str(
                item.get("river_name")
                or item.get("name")
                or item.get("river")
                or item.get("river_names")
                or ""
            ).strip()
            names = _split_river_names(raw_name)
            if not names:
                return []

            length_km = _pick_first_numeric(item, ["length_km", "length", "river_length_km", "len_km"])
            distance_km = _pick_first_numeric(
                item,
                ["distance_km", "distance", "river_distance_km", "dist_km", "downstream_distance_km"],
            )
            if distance_km is None:
                # 业务口径：距离=河段长度
                distance_km = length_km
            # 某些工具只返回“河段数量”，先透传给长度列会误导，这里不强行映射到 length_km
            base_locate = str(item.get("locate_id") or item.get("id") or f"{default_locate_prefix}_{i+1}")

            built = []
            for j, n in enumerate(names):
                locate_id = base_locate if len(names) == 1 else f"{base_locate}_{j+1}"
                built.append(
                    {
                        "river_name": n,
                        "length_km": length_km,
                        "distance_km": distance_km,
                        "locate_id": locate_id,
                    }
                )
            return built

        if isinstance(data, dict):
            river_items = (
                data.get("rivers")
                or data.get("river_list")
                or data.get("matched_rivers")
                or data.get("river_names")
                or []
            )
            if isinstance(river_items, list):
                for i, item in enumerate(river_items):
                    rows.extend(_build_row_from_item(item, i, "district_river"))

            # 若工具直接给了 geojson，确保其 locate_id 与 rows 对齐
            raw_geojson = data.get("geojson")
            if isinstance(raw_geojson, dict) and isinstance(raw_geojson.get("features"), list):
                for idx, f in enumerate(raw_geojson.get("features", [])):
                    if not isinstance(f, dict):
                        continue
                    props = f.get("properties")
                    if not isinstance(props, dict):
                        props = {}
                        f["properties"] = props
                    props["locate_id"] = str(props.get("locate_id") or props.get("id") or f"district_river_{idx+1}")
                geojson = raw_geojson

            # 兼容工具返回“线段数组”格式（含 from_x/to_x 等）
            elif isinstance(data.get("segments"), list):
                geojson = _segments_to_geojson(data.get("segments"))

            # 如果有行数据但没有几何，前端仍可用 locate_id 做表内交互（地图层留空）
            if isinstance(geojson, dict) and isinstance(geojson.get("features"), list):
                # 用 geojson 属性回填 rows 的长度/距离，优先使用几何侧更可信数据
                by_locate = {}
                for f in geojson.get("features", []):
                    if not isinstance(f, dict):
                        continue
                    p = f.get("properties") if isinstance(f.get("properties"), dict) else {}
                    lid = str(p.get("locate_id") or "")
                    if lid:
                        by_locate[lid] = p
                if by_locate and rows:
                    for r in rows:
                        p = by_locate.get(str(r.get("locate_id") or ""))
                        if not p:
                            continue
                        if r.get("length_km") is None:
                            r["length_km"] = _to_float(p.get("length_km") or p.get("length"))
                        if r.get("distance_km") is None:
                            r["distance_km"] = _to_float(p.get("distance_km") or p.get("distance"))
                        if r.get("distance_km") is None:
                            r["distance_km"] = r.get("length_km")

        if rows:
            tables = [
                _build_table(
                    "district_rivers",
                    "河系列表",
                    [
                        {"key": "river_name", "label": "河名称"},
                        {"key": "length_km", "label": "河长(km)"},
                        {"key": "distance_km", "label": "距离(km)"},
                        {"key": "locate_id", "label": "定位ID"},
                    ],
                    rows,
                )
            ]
            layers = [{"id": "district_rivers", "color": "#2F80ED", "name": "分区河流"}]

    # 实时监测：站点温湿风压
    if scene == "realtime_station":
        station_rows = _extract_station_rows(data)
        if station_rows:
            geojson = _station_rows_to_geojson(station_rows)
            layers = [{"id": "national_stations", "color": "#E67E22", "name": "国家站点"}]
            table_rows = []
            for i, r in enumerate(station_rows):
                locate_id = r.get("locate_id") or r.get("station_id") or r.get("stcd") or r.get("id") or f"station_{i+1}"
                table_rows.append(
                    {
                        "station_name": r.get("station_name") or r.get("name"),
                        "temperature": r.get("temperature"),
                        "humidity": r.get("humidity"),
                        "wind": r.get("wind"),
                        "pressure": r.get("pressure"),
                        "locate_id": str(locate_id),
                    }
                )
            tables = [
                _build_table("station_observations", "站点观测数据", [
                    {"key": "station_name", "label": "站点名称"},
                    {"key": "temperature", "label": "温度"},
                    {"key": "humidity", "label": "湿度"},
                    {"key": "wind", "label": "风"},
                    {"key": "pressure", "label": "气压"},
                    {"key": "locate_id", "label": "定位ID"},
                ], table_rows)
            ]

    # 应急响应：河流/分区/判定明细
    if scene in {"emergency_rivers", "emergency_districts"}:
        emergency_tables = _extract_emergency_river_tables(data) if scene == "emergency_rivers" else _extract_emergency_district_tables(data)
        if not emergency_tables:
            emergency_tables = _extract_emergency_tables(data)
        if emergency_tables:
            tables = emergency_tables
        if scene == "emergency_rivers":
            layers = [
                {"id": "direct_rivers", "color": "#27AE60", "name": "直接影响河流"},
                {"id": "indirect_rivers", "color": "#2F80ED", "name": "间接影响河流"},
            ]
            geo_candidate = _extract_emergency_river_geojson(data)
            if isinstance(geo_candidate, dict) and geo_candidate.get("features"):
                geojson = geo_candidate
        else:
            layers = [{"id": "emergency_districts", "color": "#8E44AD", "name": "应急影响分区/行政区"}]
            geo_candidate = _extract_emergency_district_geojson(data)
            if isinstance(geo_candidate, dict) and geo_candidate.get("features"):
                geojson = geo_candidate

    # 其它工具：优先透传已有标准结构
    if isinstance(data, dict):
        if isinstance(data.get("geojson"), dict) and (not isinstance(geojson, dict) or not geojson.get("features")):
            geojson = data["geojson"]
        if isinstance(data.get("panel_tables"), list) and not tables:
            tables = data["panel_tables"]
        if isinstance(data.get("layers"), list) and not layers:
            layers = data["layers"]

    return geojson, tables, layers


async def _send_gis_linkage(tool_name: str, tool_args, observation, user_text: str, tools=None):
    scene = _guess_gis_scene(user_text)
    geojson, panel_tables, layers = _extract_geojson_and_tables(tool_name, observation, scene)
    # 下流河系场景：若本轮只拿到文本型下游河名（无空间要素），自动补调河网工具拉空间线段
    if (
        scene == "river_downstream"
        and tool_name == "get_xialiu_rivername"
        and isinstance(geojson, dict)
        and not geojson.get("features")
    ):
        candidate_tools = tools if isinstance(tools, list) else cl.user_session.get("tools")
        if isinstance(candidate_tools, list):
            river_tool = next((t for t in candidate_tools if getattr(t, "name", "") == "get_river_network_for_plot"), None)
            if river_tool is not None:
                river_name = ""
                if isinstance(tool_args, dict):
                    river_name = str(tool_args.get("river") or tool_args.get("start_river") or "").strip()
                if not river_name:
                    river_name = _extract_river_name(user_text)
                if river_name:
                    try:
                        river_obs = await river_tool.ainvoke({"start_river": river_name})
                        river_geojson, _, river_layers = _extract_geojson_and_tables(
                            "get_river_network_for_plot", river_obs, scene
                        )
                        if isinstance(river_geojson, dict) and river_geojson.get("features"):
                            geojson = river_geojson
                        if river_layers:
                            layers = river_layers
                    except Exception as e:
                        print(f"[GIS补拉河网] 失败：{e}")

    # 下流河系场景：若已拿到河网几何，则用几何聚合结果覆盖面板表，
    # 避免出现“地图有长度/距离，表格为空值”的不一致。
    if scene == "river_downstream" and isinstance(geojson, dict) and geojson.get("features"):
        agg_rows = _aggregate_river_rows_from_geojson(geojson)
        if agg_rows:
            panel_tables = [
                _build_table(
                    "downstream_rivers",
                    "下流河系列表",
                    [
                        {"key": "river_name", "label": "河名称"},
                        {"key": "length_km", "label": "河长(km)"},
                        {"key": "distance_km", "label": "距离(km)"},
                        {"key": "locate_id", "label": "定位ID"},
                    ],
                    agg_rows,
                )
            ]

    # 分区河流场景：若 locate_region_rivers 仅返回河名，自动逐河补调河网以回填长度/距离，
    # 并构建与表格 locate_id 对齐的地图要素，方便前端点击联动。
    if scene == "district_rivers" and tool_name == "locate_region_rivers":
        rows = _extract_table_rows(panel_tables, "district_rivers")
        candidate_tools = tools if isinstance(tools, list) else cl.user_session.get("tools")
        river_tool = None
        if isinstance(candidate_tools, list):
            river_tool = next((t for t in candidate_tools if getattr(t, "name", "") == "get_river_network_for_plot"), None)

        if rows and river_tool is not None:
            merged_features = []
            for i, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                # 已有长度则不再重复补查
                if _to_float(r.get("length_km")) is not None:
                    continue
                river_name = str(r.get("river_name") or "").strip()
                if not river_name:
                    continue
                try:
                    river_obs = await river_tool.ainvoke({"start_river": river_name})
                    river_geojson, _, _ = _extract_geojson_and_tables("get_river_network_for_plot", river_obs, "district_rivers")
                    agg = _aggregate_river_rows_from_geojson(river_geojson)
                    length_km = _to_float(agg[0].get("length_km")) if agg else None
                    r["length_km"] = length_km
                    r["distance_km"] = length_km  # 业务口径：距离=河长

                    locate_id = str(r.get("locate_id") or f"district_river_{i+1}")
                    f = _build_multiline_feature_from_geojson(river_name, locate_id, river_geojson, length_km)
                    if f:
                        merged_features.append(f)
                except Exception as e:
                    print(f"[GIS补拉分区河流] {river_name} 失败：{e}")

            _replace_table_rows(panel_tables, "district_rivers", rows)
            if merged_features:
                geojson = {"type": "FeatureCollection", "features": merged_features}
                layers = [{"id": "district_rivers", "color": "#2F80ED", "name": "分区河流"}]

    packet = {
        "type": "gis_linkage",
        "schema_version": "v2",
        "scene": scene,
        "query": user_text,
        "tool": {"name": tool_name, "args": tool_args},
        "map": {
            "geojson": geojson,
            "layers": layers,
        },
        "panel": {
            "tables": panel_tables,
        },
    }

    payload_str = json.dumps(packet, ensure_ascii=False)
    # 先打到控制台，便于联调时直接抓包核对
    print(f"[GIS_JSON]{payload_str}")

    await cl.send_window_message(payload_str)

    # 全局广播事件：给 iframe 外层页面（独立 socket 连接）监听
    # 事件名固定为 gis_linkage_broadcast，前端可直接订阅。
    try:
        from chainlit.server import sio
        await sio.emit("gis_linkage_broadcast", {"message": payload_str})
    except Exception as e:
        print(f"[GIS广播] 发送失败：{e}")


    


def _build_river_network_brief(raw_result, river_name: str) -> str:
    """基于河网结果生成更细的领导简报（不依赖二次LLM）"""
    segments = _unwrap_tool_result(raw_result)
    if not isinstance(segments, list) or not segments:
        return f"`{river_name}` 暂无可用河网数据，建议稍后重试。"

    valid = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            x1, y1 = float(seg["from_x"]), float(seg["from_y"])
            x2, y2 = float(seg["to_x"]), float(seg["to_y"])
            order = int(seg.get("strahler_order", 1))
            name = (seg.get("rivername") or "").strip()
            valid.append((x1, y1, x2, y2, order, name))
        except Exception as e:
            print(f"[绘图] 河网线段解析失败: {e}")
            continue

    if not valid:
        return f"`{river_name}` 河网数据格式异常，建议稍后重试。"

    from collections import Counter, defaultdict
    import math
    in_degree = defaultdict(int)
    out_degree = defaultdict(int)
    order_counter = Counter()
    river_counter = Counter()
    river_len_km = defaultdict(float)
    total_len_km = 0.0

    for x1, y1, x2, y2, order, name in valid:
        out_degree[(x1, y1)] += 1
        in_degree[(x2, y2)] += 1
        order_counter[order] += 1

        # 粗略长度估算：经纬度差按局地尺度换算为公里（用于相对比较）
        mean_lat = (y1 + y2) / 2.0
        dx_km = (x2 - x1) * 111.0 * math.cos(math.radians(mean_lat))
        dy_km = (y2 - y1) * 111.0
        seg_len = math.sqrt(dx_km * dx_km + dy_km * dy_km)
        total_len_km += seg_len

        if name and name not in {"未知", "None"}:
            river_counter[name] += 1
            river_len_km[name] += seg_len

    nodes = set(in_degree.keys()) | set(out_degree.keys())
    sources = sum(1 for n in nodes if in_degree[n] == 0)
    sinks = sum(1 for n in nodes if out_degree[n] == 0)
    confluences = sum(1 for n in nodes if in_degree[n] > 1)
    avg_len_km = total_len_km / len(valid) if valid else 0.0
    all_orders = "；".join(
        [f"{k}级共{v}段" for k, v in sorted(order_counter.items(), key=lambda x: x[0])]
    ) or "待核实"

    unnamed_seg = sum(1 for *_, name in valid if not name or name in {"未知", "None"})
    named_count = len(river_counter)

    # 摘要行仍给领导看；长度/段数前三仅作「摘要」，完整表见下文
    top_len_rivers = "、".join(
        [f"{k}({v:.1f}km)" for k, v in sorted(river_len_km.items(), key=lambda x: x[1], reverse=True)[:3]]
    ) or "（无具名河段）"

    # 完整具名河段清单（按估算河长降序，专业人士可核对无遗漏）
    detail_lines = [
        "| 河流名称 | 河段数 | 估算河长(km) |",
        "| :--- | :--- | :--- |",
    ]
    for rname, rlen in sorted(river_len_km.items(), key=lambda x: (-x[1], x[0])):
        detail_lines.append(f"| {rname} | {river_counter[rname]} | {rlen:.2f} |")
    if unnamed_seg:
        detail_lines.append(f"| （河段无有效名称） | {unnamed_seg} | — |")
    if len(detail_lines) == 2:
        detail_lines.append("| （本次河段均无有效名称，无法逐河列举） | — | — |")

    full_table = "\n".join(detail_lines)
    detail_note = (
        "**完整河段统计表（上表已全部列出，无「等」省略）**\n\n"
        f"* 具名河流 {named_count} 条；无名称河段 {unnamed_seg} 段。\n\n"
    )

    return (
        f"`{river_name}` 河网影响简报：共识别 {len(valid)} 条河段、{len(nodes)} 个节点，"
        f"估算河网总长约 {total_len_km:.1f} km。\n\n"
        "| 关注点 | 现状 |\n"
        "| :--- | :--- |\n"
        f"| 河段规模 | {len(valid)} 条；平均每段 {avg_len_km:.2f} km；具名河流 {named_count} 条 |\n"
        f"| 河网长度 | 总长约 {total_len_km:.1f} km；河长前三（摘要）：{top_len_rivers} |\n"
        f"| 汇流复杂度 | 汇流点 {confluences} 个 |\n"
        f"| 源汇分布 | 源头 {sources} 个，末端 {sinks} 个 |\n"
        f"| 水系等级结构（全量） | {all_orders} |\n\n"
        f"{detail_note}"
        f"{full_table}\n\n"
        "建议行动：\n"
        "1) 先盯汇流点与末端卡口，布设高频巡查；\n"
        "2) 按上表具名河段与等级结构加密监测；\n"
        "3) 每30分钟滚动复核雨情、水情和积涝反馈。"
    )

# ===============================
# 1. 会话开始
# ===============================
@cl.on_chat_start
async def on_chat_start():
    await _init_runtime_session(messages_seed=[])


@cl.on_chat_resume
async def on_chat_resume(thread: ThreadDict):
    resumed_messages = _build_messages_from_thread(thread)
    await _init_runtime_session(messages_seed=resumed_messages)


# ===============================
# 2. 接收用户消息并流式回复
# ===============================
@cl.on_message
async def on_message(message: cl.Message):
    planner_chain = cl.user_session.get("planner_chain")
    answer_chain = cl.user_session.get("answer_chain")
    messages = cl.user_session.get("messages")
    tools = cl.user_session.get("tools")

    # 兜底：恢复线程或服务热重载后，若运行时对象缺失则即时重建，避免“可看历史但无法继续聊”。
    if planner_chain is None or answer_chain is None or tools is None:
        await _init_runtime_session(messages_seed=messages if isinstance(messages, list) else [])
        planner_chain = cl.user_session.get("planner_chain")
        answer_chain = cl.user_session.get("answer_chain")
        tools = cl.user_session.get("tools")
        messages = cl.user_session.get("messages")

    if not isinstance(messages, list):
        messages = []
        cl.user_session.set("messages", messages)

    callbacks = {
        "need_river_plot": _need_river_plot,
        "extract_river_name": _extract_river_name,
        "build_admin_overlay_for_plot": _build_admin_overlay_for_plot,
        "render_and_send_plot": render_and_send_plot,
        "build_river_network_brief": _build_river_network_brief,
        "append_followup_if_needed": _append_followup_if_needed,
        "stream_text_to_message": stream_text_to_message,
        "user_forbids_followup": _user_forbids_followup,
        "make_followup_question": _make_followup_question,
        "ainvoke_chain": ainvoke_chain,
        "astream_planner_think": astream_planner_think,
        "astream_answer_chain_to_message": astream_answer_chain_to_message,
        "should_force_admin_units_reply": _should_force_admin_units_reply,
        "should_force_partition_table_reply": _should_force_partition_table_reply,
        "should_force_structured_impact_reply": _should_force_structured_impact_reply,
        "build_admin_units_only_reply": _build_admin_units_only_reply,
        "build_partition_only_reply": _build_partition_only_reply,
        "build_structured_impact_reply": _build_structured_impact_reply,
        "enrich_with_impact_time_tool": _enrich_with_impact_time_tool,
        "tool_observation_to_text": _tool_observation_to_text,
        "send_gis_linkage": _send_gis_linkage,
    }

    await process_message(
        message=message,
        planner_chain=planner_chain,
        answer_chain=answer_chain,
        tools=tools,
        messages=messages,
        callbacks=callbacks,
    )
