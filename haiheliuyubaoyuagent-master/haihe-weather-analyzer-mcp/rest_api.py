"""
应急响应 REST API 服务
为同事提供 HTTP 接口调用的应急响应判定能力。

启动方式：
  uvicorn rest_api:app --host 0.0.0.0 --port 8002

或直接运行：
  python rest_api.py
"""

from __future__ import annotations

import configparser
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from constants import DEFAULT_BASIN_CODES

import psycopg2
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

# 导入 MCP 工具核心函数
from haihe_mcp_tools import evaluate_emergency_response_core

# 河网图相关
import networkx as nx
from tools import get_graph, get_edge_river_name, get_edge_length_km


def _river_name_matches(river_name: str, edge_name: str) -> bool:
    """判断河流是否匹配边的名称，逗号河名拆分后互相匹配"""
    if not edge_name:
        return False
    parts = [p.strip() for p in edge_name.replace('，', ',').split(',') if p.strip()]
    # 如果 river_name 也含逗号，拆开分别匹配
    rp = [x.strip() for x in river_name.replace('，', ',').split(',') if x.strip()]
    for r in rp:
        if r in parts:
            return True
    return river_name in parts


def _query_upstream_rivers(river_name: str) -> list[dict]:
    """查询河流的全部上游（所有支流，沿入边递归）"""
    G = get_graph()
    segments = [(u, v, d) for u, v, d in G.edges(data=True) if _river_name_matches(river_name, get_edge_river_name(d))]
    if not segments:
        return []
    all_upstream: set[str] = set()
    for u, v, d in segments:
        try:
            ancs = nx.ancestors(G, u)
        except nx.NetworkXError:
            ancs = set()
        nodes = {u} | ancs
        for node in nodes:
            for pu, pv, pe in G.in_edges(node, data=True):
                name = get_edge_river_name(pe)
                if name and not _river_name_matches(river_name, name):
                    # 对于逗号河名，取第一个名为上游展示名
                    first_name = name.split(',')[0].strip() if ',' in name else name
                    all_upstream.add(first_name)
    result = [{"name": n, "segment_count": len([e for e in G.edges(data=True) if _river_name_matches(n, get_edge_river_name(e[2]) or '')])} for n in sorted(all_upstream)]
    return result


def _query_downstream_rivers(river_name: str) -> dict:
    """查询河流下游（分层：direct=同一节点分叉，indirect=递归后续层级）"""
    G = get_graph()
    from collections import defaultdict

    out_rivers: dict[Any, set[str]] = defaultdict(set)
    for su, sv, sd in G.edges(data=True):
        name = get_edge_river_name(sd)
        if name:
            out_rivers[su].add(name)

    river_ends: dict[str, set[Any]] = defaultdict(set)
    for su, sv, sd in G.edges(data=True):
        name = get_edge_river_name(sd)
        if name:
            river_ends[name].add(sv)

    def _g(d) -> str:
        return get_edge_river_name(d) or ""

    def _sc(n: str) -> int:
        """统计河流的段数，逗号河名按各部分分别统计"""
        names = [p.strip() for p in n.replace('，', ',').split(',') if p.strip()] if ',' in n else [n]
        matched = set()
        for e in G.edges(data=True):
            en = _g(e[2])
            if en and any(_river_name_matches(p, en) for p in names):
                matched.add(e[:2])
        return len(matched)

    def _tl(n: str) -> float:
        """统计河流总长"""
        names = [p.strip() for p in n.replace('，', ',').split(',') if p.strip()] if ',' in n else [n]
        total = 0.0
        for u,v,d in G.edges(data=True):
            en = _g(d)
            if en and any(_river_name_matches(p, en) for p in names):
                total += get_edge_length_km(d)
        return total

    seen: set[str] = {river_name}
    direct: list[dict] = []
    for en in river_ends.get(river_name, set()):
        for nr in out_rivers.get(en, set()):
            if nr not in seen:
                seen.add(nr)
                direct.append({"name": nr, "segment_count": _sc(nr), "total_length_km": round(_tl(nr), 1)})

    indirect: list[dict] = []
    def _walk_and_collect(rn: str, start_node, lv: int):
        """从start_node沿河走到末端，沿途收集分叉河"""
        walked = set()
        cur = start_node
        while cur not in walked:
            walked.add(cur)
            nxt = None
            for su, sv, sd in G.edges(data=True):
                gn = get_edge_river_name(sd)
                if not gn or not _river_name_matches(rn, gn):
                    continue
                if abs(su[0]-cur[0]) < 0.0001 and abs(su[1]-cur[1]) < 0.0001:
                    nxt = sv
                    break
            # 在当前节点收集分叉（非本河的出边）
            for nr in out_rivers.get(cur, set()):
                if nr not in seen and not _river_name_matches(rn, nr):
                    seen.add(nr)
                    indirect.append({"name": nr, "level": lv,
                                    "segment_count": _sc(nr),
                                    "total_length_km": round(_tl(nr), 1)})
                    _rec(nr, lv + 1, from_node=cur)
            if nxt is None or nxt in walked:
                break
            cur = nxt

    def _rec(rn: str, lv: int, from_node=None):
        if from_node:
            _walk_and_collect(rn, from_node, lv)
        else:
            for en in river_ends.get(rn, set()):
                for nr in out_rivers.get(en, set()):
                    if nr not in seen:
                        seen.add(nr)
                        indirect.append({"name": nr, "level": lv,
                                        "segment_count": _sc(nr),
                                        "total_length_km": round(_tl(nr), 1)})
                        _rec(nr, lv + 1, from_node=en)

    for d in direct:
        dn = d["name"]
        # 从父河终点找汇入节点
        for en in river_ends.get(river_name, set()):
            for su, sv, sd in G.edges(data=True):
                gn = get_edge_river_name(sd)
                if gn and (gn == dn or any(p.strip() == gn for p in dn.replace('，', ',').split(',') if p.strip())) and \
                   abs(su[0]-en[0]) < 0.0001 and abs(su[1]-en[1]) < 0.0001:
                    _rec(dn, 2, from_node=su)
                    break
            else:
                continue
            break
        else:
            _rec(dn, 2)

    return {"direct_downstream": direct, "indirect_downstream": indirect}


def _get_pg_conf() -> dict:
    if "postgres" not in _config:
        raise HTTPException(500, "config.ini 中缺少 [postgres] 配置段")
    return dict(_config["postgres"])


def _get_pg_conn():
    pg = _get_pg_conf()
    return psycopg2.connect(
        host=pg.get("host", "127.0.0.1"),
        port=int(pg.get("port", 5432)),
        dbname=pg.get("dbname"),
        user=pg.get("user"),
        password=pg.get("password"),
        sslmode=pg.get("sslmode", "disable"),
        connect_timeout=int(pg.get("connect_timeout", "5")),
    )


def _schema() -> str:
    return _config.get("postgres", "schema", fallback="public")


# ========== 请求/响应模型 ==========

class EmergencyEvaluateRequest(BaseModel):
    start_time: str = Field("", description="开始时间 YYYY-MM-DD HH:MM:SS，默认24h前")
    end_time: str = Field("", description="结束时间 YYYY-MM-DD HH:MM:SS，默认当前")
    basin_codes: str = Field(DEFAULT_BASIN_CODES, description="流域代码，默认海河流域")
    allowed_station_levels: str = Field("", description="站点等级")



logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rest_api")

app = FastAPI(title="海河流域应急响应判定 REST API", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_config = configparser.ConfigParser()
_config.read("config.ini", encoding="utf-8-sig")

ADMIN_DEFAULT_USERNAME = "admin"
ADMIN_DEFAULT_PASSWORD = "admin123"
ADMIN_DEFAULT_ROLE = "admin"


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


ALLOWED_ROLES = {"admin", "forecaster", "external"}
ROLE_LABELS = {
    "admin": "管理员",
    "forecaster": "预报员",
    "external": "外部用户",
}
security = HTTPBasic(auto_error=False)


def _role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def _require_admin(credentials: HTTPBasicCredentials | None = Depends(security)) -> dict:
    """HTTP Basic Auth 校验：仅允许状态 active 的管理员访问。"""
    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少认证信息", headers={"WWW-Authenticate": "Basic"})
    _ensure_auth_tables()
    schema = _schema()
    try:
        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT username, password_hash, role, status
                    FROM {schema}.hh_user_account
                    WHERE username = %s
                    LIMIT 1
                    """,
                    (credentials.username,),
                )
                row = cur.fetchone()
    except Exception as e:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"鉴权失败: {e}")

    if not row or row.get("status") != "active" or row["password_hash"] != _hash_password(credentials.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误", headers={"WWW-Authenticate": "Basic"})
    if row["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可操作")
    return dict(row)


def _upsert_user(username: str, password: str, role: str, allow_admin: bool) -> dict:
    """创建或覆盖用户。allow_admin=False 时禁止创建管理员。"""
    if not username or len(username) > 64:
        raise HTTPException(400, "用户名长度应为 1~64 字符")
    if not password:
        raise HTTPException(400, "密码不能为空")
    role = role or "external"
    if role not in ALLOWED_ROLES:
        raise HTTPException(400, f"无效角色，可选: {', '.join(sorted(ALLOWED_ROLES))}")
    if not allow_admin and role == "admin":
        raise HTTPException(400, "注册接口不允许创建管理员账号")

    _ensure_auth_tables()
    schema = _schema()
    try:
        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                cur.execute(
                    f"""
                    INSERT INTO {schema}.hh_user_account (username, password_hash, role, status, updated_at)
                    VALUES (%s, %s, %s, 'active', NOW())
                    ON CONFLICT (username) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        role = EXCLUDED.role,
                        status = 'active',
                        updated_at = NOW()
                    RETURNING username, role, status, created_at, updated_at
                    """,
                    (username, _hash_password(password), role),
                )
                row = cur.fetchone()
                conn.commit()
        return {
            "username": row["username"],
            "status": row["status"],
            "role": row["role"],
            "role_label": _role_label(row["role"]),
        }
    except Exception as e:
        raise HTTPException(500, f"保存用户失败: {e}")

def _ensure_auth_tables() -> None:
    schema = _schema()
    ddl = f"""
    CREATE TABLE IF NOT EXISTS {schema}.hh_user_account (
        id BIGSERIAL PRIMARY KEY,
        username VARCHAR(64) UNIQUE NOT NULL,
        password_hash VARCHAR(128) NOT NULL,
        role VARCHAR(32) NOT NULL DEFAULT 'admin',
        status VARCHAR(16) NOT NULL DEFAULT 'active',
        created_at TIMESTAMP NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    );
    """
    seed = f"""
    INSERT INTO {schema}.hh_user_account (username, password_hash, role, status)
    VALUES (%s, %s, %s, 'active')
    ON CONFLICT (username) DO NOTHING
    """
    with _get_pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
            cur.execute(seed, (ADMIN_DEFAULT_USERNAME, _hash_password(ADMIN_DEFAULT_PASSWORD), ADMIN_DEFAULT_ROLE))
        conn.commit()


class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class RegisterRequest(BaseModel):
    username: str = Field(..., description="用户名，1~64 字符")
    password: str = Field(..., description="密码")
    role: str = Field("external", description="角色，默认 external")


class AdminCreateUserRequest(BaseModel):
    username: str = Field(..., description="用户名，1~64 字符")
    password: str = Field(..., description="密码")
    role: str = Field("external", description="角色，默认 external")


class StatusUpdateRequest(BaseModel):
    status: str = Field(..., description="状态：active / disabled")


class ResetPasswordRequest(BaseModel):
    password: str = Field(..., description="新密码")

@app.post("/api/v1/auth/login", tags=["认证"])
def login(req: LoginRequest):
    _ensure_auth_tables()
    schema = _schema()
    try:
        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT username, password_hash, role, status
                    FROM {schema}.hh_user_account
                    WHERE username = %s
                    LIMIT 1
                    """,
                    (req.username,),
                )
                row = cur.fetchone()
                if not row or row.get("status") != "active":
                    raise HTTPException(401, "用户名或密码错误")
                if row["password_hash"] != _hash_password(req.password):
                    raise HTTPException(401, "用户名或密码错误")
                return {
                    "code": 200,
                    "data": {
                        "username": row["username"],
                        "role": row["role"],
                    },
                    "message": "success",
                }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"登录失败: {e}")


@app.post("/api/v1/auth/register", tags=["认证"])
def register(req: RegisterRequest):
    """注册普通用户，不允许注册管理员；同名用户会覆盖密码/角色并恢复 active。"""
    result = _upsert_user(req.username, req.password, req.role, allow_admin=False)
    return {"code": 200, "data": result, "message": "success"}


# ========== 管理员用户管理 ==========

@app.get("/api/v1/admin/users", tags=["管理员"])
def list_users(admin: dict = Depends(_require_admin)):
    """获取全部用户列表（仅管理员）。"""
    schema = _schema()
    try:
        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT username, role, status, created_at, updated_at
                    FROM {schema}.hh_user_account
                    ORDER BY created_at DESC, username ASC
                    """
                )
                users = []
                for row in cur.fetchall():
                    user = dict(row)
                    for k in ("created_at", "updated_at"):
                        if isinstance(user.get(k), datetime):
                            user[k] = user[k].strftime("%Y-%m-%d %H:%M:%S")
                    user["role_label"] = _role_label(user["role"])
                    users.append(user)
        return {"code": 200, "data": users, "message": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"查询用户列表失败: {e}")


@app.post("/api/v1/admin/users", tags=["管理员"])
def admin_create_user(req: AdminCreateUserRequest, admin: dict = Depends(_require_admin)):
    """管理员创建/覆盖用户，允许创建管理员角色。"""
    result = _upsert_user(req.username, req.password, req.role, allow_admin=True)
    return {"code": 200, "data": result, "message": "success"}


@app.patch("/api/v1/admin/users/{username}/status", tags=["管理员"])
def update_user_status(
    username: str,
    req: StatusUpdateRequest,
    admin: dict = Depends(_require_admin),
):
    """修改用户状态；默认管理员 admin 不允许被禁用。"""
    if req.status not in {"active", "disabled"}:
        raise HTTPException(400, "状态只能是 active 或 disabled")
    if username == ADMIN_DEFAULT_USERNAME and req.status == "disabled":
        raise HTTPException(403, "默认管理员不允许被禁用")

    schema = _schema()
    try:
        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                cur.execute(
                    f"""
                    UPDATE {schema}.hh_user_account
                    SET status = %s, updated_at = NOW()
                    WHERE username = %s
                    RETURNING username, status
                    """,
                    (req.status, username),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, f"用户 {username} 不存在")
                conn.commit()
        return {"code": 200, "data": {"username": row["username"], "status": row["status"]}, "message": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"更新用户状态失败: {e}")


@app.post("/api/v1/admin/users/{username}/reset-password", tags=["管理员"])
def reset_user_password(
    username: str,
    req: ResetPasswordRequest,
    admin: dict = Depends(_require_admin),
):
    """重置指定用户密码，并恢复 active 状态。"""
    if not req.password:
        raise HTTPException(400, "密码不能为空")

    schema = _schema()
    try:
        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                cur.execute(
                    f"""
                    UPDATE {schema}.hh_user_account
                    SET password_hash = %s, status = 'active', updated_at = NOW()
                    WHERE username = %s
                    RETURNING username, role, status
                    """,
                    (_hash_password(req.password), username),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, f"用户 {username} 不存在")
                conn.commit()
        return {
            "code": 200,
            "data": {
                "username": row["username"],
                "status": row["status"],
                "role": row["role"],
                "role_label": _role_label(row["role"]),
            },
            "message": "success",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"重置密码失败: {e}")


class RainfallAnalysisRequest(BaseModel):
    start_time: str = Field("", description="开始时间 YYYY-MM-DD HH:MM:SS")
    end_time: str = Field("", description="结束时间 YYYY-MM-DD HH:MM:SS")
    zone_type: str = Field("9", description="分区类型 9/11/77/246")


# ========== 健康检查 ==========

@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok", "service": "海河流域应急响应判定REST API", "version": "1.0.0"}


# ========== 应急响应判定 ==========

@app.post("/api/v1/emergency/evaluate", tags=["应急响应"])
def evaluate_emergency(
    req: EmergencyEvaluateRequest,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """
    按时间段判定应急响应事件（I/II/III/IV级）。

    自动扫描时间段内每个整点观测时次（02/08/14/20），
    对每个时次执行 fetch→filter→evaluate→report 流水线，
    返回所有触发应急响应的事件清单。
    """
    now = datetime.now()
    end_time = req.end_time or now.strftime("%Y-%m-%d %H:%M:%S")
    start_time = req.start_time or (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        end_dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")
        start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
    except ValueError as e:
        raise HTTPException(400, f"时间格式错误: {e}")

    if end_dt <= start_dt:
        raise HTTPException(400, "end_time 必须大于 start_time")

    events: list[dict[str, Any]] = []
    synoptic_hours = [2, 8, 14, 20]
    cur = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    while cur <= end_dt:
        for h in synoptic_hours:
            ts = cur.replace(hour=h)
            if ts < start_dt or ts > end_dt:
                continue
            try:
                ts_str = ts.strftime("%Y%m%d%H%M%S")
                result = evaluate_emergency_response_core(
                    basin_codes=req.basin_codes,
                    times=ts_str,
                    allowed_station_levels=req.allowed_station_levels,
                    include_records=False,
                )
                logger.info(f"[{ts_str}] evaluate 结果: level={result.get('level')}, triggered={result.get('triggered')}, keys={list(result.keys())}, evidence_keys={list(result.get('evidence', {}).keys())}")
                lev = result.get("level")
                if lev:
                    ev = result.get("evidence", {})
                    events.append({
                        "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
                        "max_level": lev,
                        "summary": result.get("message", ""),
                        "reached_station_count": ev.get("qualified_station_count", 0),
                        "total_station_count": ev.get("total_station_count", 0),
                    })
            except Exception as e:
                logger.warning(f"[{ts}] 跳过: {e}")
                continue
        cur += timedelta(days=1)

    # 分页
    total = len(events)
    offset = (page - 1) * page_size
    page_events = events[offset: offset + page_size]

    # 最高等级
    level_order = {"I": 0, "II": 1, "III": 2, "IV": 3}
    max_level = "无"
    if events:
        triggered = [e["max_level"] for e in events if e["max_level"] in level_order]
        if triggered:
            max_level = min(triggered, key=lambda x: level_order.get(x, 99))

    return {
        "code": 200,
        "data": {
            "start_time": start_time,
            "end_time": end_time,
            "max_level": max_level,
            "triggered_count": total,
            "page": page,
            "page_size": page_size,
            "events": page_events,
        },
        "message": "success" if events else "该时段内未触发应急响应",
    }


# ========== 应急响应汇总（按日分组） ==========

@app.post("/api/v1/emergency/summary", tags=["应急响应"])
def emergency_summary(
    start_time: str = Query("", description="开始时间"),
    end_time: str = Query("", description="结束时间"),
    basin_codes: str = Query(DEFAULT_BASIN_CODES, description="流域代码"),
    allowed_station_levels: str = Query("", description="站点等级"),
):
    """
    按日汇总应急响应事件。
    与 evaluate 不同，以"天"为单位合并展示，一目了然。
    """
    now = datetime.now()
    end_time = end_time or now.strftime("%Y-%m-%d %H:%M:%S")
    start_time = start_time or (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        end_dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")
        start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
    except ValueError as e:
        raise HTTPException(400, f"时间格式错误: {e}")

    from collections import defaultdict
    daily: dict[str, list[dict]] = defaultdict(list)
    synoptic_hours = [2, 8, 14, 20]
    cur = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)

    while cur <= end_dt:
        for h in synoptic_hours:
            ts = cur.replace(hour=h)
            if ts < start_dt or ts > end_dt:
                continue
            try:
                ts_str = ts.strftime("%Y%m%d%H%M%S")
                result = evaluate_emergency_response_core(
                    basin_codes=basin_codes,
                    times=ts_str,
                    allowed_station_levels=allowed_station_levels or "",
                    include_records=False,
                )
                lev = result.get("level")
                if lev:
                    daily[ts.strftime("%Y-%m-%d")].append({
                        "time": ts.strftime("%H:%M"),
                        "max_level": lev,
                        "reached": result.get("evidence", {}).get("qualified_station_count", 0),
                    })
            except Exception:
                continue
        cur += timedelta(days=1)

    summary = []
    for day, evts in sorted(daily.items()):
        levels = [e["max_level"] for e in evts if e["max_level"] in {"I", "II", "III", "IV"}]
        top = min(levels, key=lambda x: {"I": 0, "II": 1, "III": 2, "IV": 3}.get(x, 99)) if levels else None
        summary.append({"date": day, "max_level": top, "triggered_count": len(evts), "events": evts})

    return {"code": 200, "data": {"start_time": start_time, "end_time": end_time, "daily_summary": summary}}


# ========== 列表可用接口 ==========

@app.get("/api/v1/endpoints", tags=["系统"])
def list_endpoints():
    """列出所有可用 REST 接口"""
    return {
        "code": 200,
        "data": {
            "health": {"method": "GET", "path": "/health", "desc": "健康检查"},
            "auth_login": {"method": "POST", "path": "/api/v1/auth/login", "desc": "用户登录"},
            "auth_register": {"method": "POST", "path": "/api/v1/auth/register", "desc": "用户注册"},
            "admin_users_list": {"method": "GET", "path": "/api/v1/admin/users", "desc": "获取用户列表（管理员）"},
            "admin_users_create": {"method": "POST", "path": "/api/v1/admin/users", "desc": "管理员创建/覆盖用户"},
            "admin_users_update_status": {"method": "PATCH", "path": "/api/v1/admin/users/{username}/status", "desc": "修改用户状态（管理员）"},
            "admin_users_reset_password": {"method": "POST", "path": "/api/v1/admin/users/{username}/reset-password", "desc": "重置用户密码（管理员）"},
            "emergency_evaluate": {"method": "POST", "path": "/api/v1/emergency/evaluate", "desc": "应急响应判定（按时间扫各整点时次）"},
            "emergency_summary": {"method": "POST", "path": "/api/v1/emergency/summary", "desc": "应急响应按日汇总"},
            "events_list": {"method": "GET", "path": "/api/v1/emergency/events", "desc": "查询应急事件列表（含状态）"},
            "event_detail": {"method": "GET", "path": "/api/v1/emergency/events/{event_code}", "desc": "查询单个应急事件详情"},
            "event_confirm": {"method": "POST", "path": "/api/v1/emergency/events/{event_code}/confirm", "desc": "确认签收应急事件"},
            "river_upstream": {"method": "POST", "path": "/api/v1/river/upstream", "desc": "查询河流的上游（body: {\"name\":\"永定河\"}）"},
            "river_downstream": {"method": "POST", "path": "/api/v1/river/downstream", "desc": "查询河流的下游（body: {\"name\":\"永定河\"}）"},
            "river_profile": {"method": "POST", "path": "/api/v1/river/profile", "desc": "查询河段详细信息（body: {\"name\":\"永定河\"}）"},
        },
    }


class RiverQuery(BaseModel):
    name: str = Field(..., description="河流名称", examples=["永定河"])


@app.post("/api/v1/river/upstream", tags=["河系"])
def river_upstream(req: RiverQuery):
    upstream = _query_upstream_rivers(req.name)
    if not upstream:
        return {"code": 200, "data": {"river": req.name, "upstream_count": 0, "upstream": []}, "message": "未找到上游河流或河流不存在"}
    return {"code": 200, "data": {"river": req.name, "upstream_count": len(upstream), "upstream": upstream}}


@app.post("/api/v1/river/downstream", tags=["河系"])
def river_downstream(req: RiverQuery):
    downstream = _query_downstream_rivers(req.name)
    direct = downstream.get("direct_downstream", [])
    indirect = downstream.get("indirect_downstream", [])
    total = len(direct) + len(indirect)
    if total == 0:
        return {"code": 200, "data": {"river": req.name, "downstream_count": 0, "downstream": []}, "message": "未找到下游河流或河流不存在"}
    return {"code": 200, "data": {"river": req.name, "direct_count": len(direct), "indirect_count": len(indirect), "direct_downstream": direct, "indirect_downstream": indirect}}


@app.post("/api/v1/river/profile", tags=["河系"])
def river_profile(req: RiverQuery):
    """查询某条河流的河段详细信息"""
    G = get_graph()
    edges = [(u, v, d) for u, v, d in G.edges(data=True) if _river_name_matches(req.name, get_edge_river_name(d))]
    if not edges:
        raise HTTPException(404, f"未找到河流「{req.name}」")

    total_len = 0.0
    segments = []
    for u, v, d in edges:
        length = get_edge_length_km(d)
        total_len += length
        segments.append({
            "from": list(u) if hasattr(u, '__len__') else str(u),
            "to": list(v) if hasattr(v, '__len__') else str(v),
            "length_km": round(length, 3) if length else None,
            "strahler_order": None,
        })

    # 上下游统计
    upstream = _query_upstream_rivers(req.name)
    ds = _query_downstream_rivers(req.name)
    direct_ds = ds.get("direct_downstream", [])
    indirect_ds = ds.get("indirect_downstream", [])

    return {
        "code": 200,
        "data": {
            "river": req.name,
            "segment_count": len(segments),
            "total_length_km": round(total_len, 2),
            "upstream_count": len(upstream),
            "upstream_rivers": [r["name"] for r in upstream],
            "direct_downstream": direct_ds,
            "indirect_downstream": indirect_ds,
            "segments": segments,
        },
    }


@app.get("/api/v1/emergency/events", tags=["应急响应"])
def list_events(
    status: str = Query("", description="按状态筛选 active/received/resolved/cancelled"),
    event_type: str = Query("", description="按类型筛选 rainstorm/flood/drought"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """查询应急事件列表，同事可查看事件状态、确认签收情况。"""
    schema = _schema()
    conditions: list[str] = []
    params: list[Any] = []

    if status:
        conditions.append("e.status = %s")
        params.append(status)
    if event_type:
        conditions.append("e.event_type = %s")
        params.append(event_type)

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * page_size

    try:
        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                cur.execute(f"SELECT COUNT(*) as total FROM {schema}.hh_emergency_event e{where}", params)
                total = cur.fetchone()["total"]
                cur.execute(f"""
                    SELECT e.id, e.event_code, e.event_type, e.event_level, e.title,
                           e.status, e.start_time, e.end_time,
                           e.created_at, e.updated_at
                    FROM {schema}.hh_emergency_event e{where}
                    ORDER BY e.created_at DESC LIMIT %s OFFSET %s
                """, params + [page_size, offset])
                events = []
                for row in cur.fetchall():
                    ev = dict(row)
                    for k in ("start_time", "end_time", "created_at", "updated_at"):
                        if isinstance(ev.get(k), datetime):
                            ev[k] = ev[k].strftime("%Y-%m-%d %H:%M:%S")
                    events.append(ev)
        return {"code": 200, "data": {"total": total, "page": page, "page_size": page_size, "events": events}}
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


@app.get("/api/v1/emergency/events/{event_code}", tags=["应急响应"])
def get_event_detail(event_code: str):
    """查询单个应急事件详情，含关联产品和站点快照。"""
    schema = _schema()
    try:
        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                cur.execute(f"""
                    SELECT e.*, fc.cycle_key, fc.forecast_time, fc.source_kind
                    FROM {schema}.hh_emergency_event e
                    LEFT JOIN {schema}.hh_emergency_forecast_cycle fc ON fc.id = e.latest_cycle_id
                    WHERE e.event_code = %s
                """, (event_code,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, f"事件 {event_code} 不存在")
                event = dict(row)
                for k in ("start_time", "end_time", "created_at", "updated_at", "forecast_time"):
                    if isinstance(event.get(k), datetime):
                        event[k] = event[k].strftime("%Y-%m-%d %H:%M:%S")

                # 关联产品图
                cur.execute(f"""
                    SELECT product_type, source_kind, product_name, product_uri, product_time
                    FROM {schema}.hh_emergency_product WHERE event_id = %s ORDER BY created_at DESC
                """, (event["id"],))
                event["products"] = [dict(r) for r in cur.fetchall()]

                # 关联站点快照
                cur.execute(f"""
                    SELECT snapshot_kind, station_count, station_json_path
                    FROM {schema}.hh_emergency_station_snapshot WHERE event_id = %s ORDER BY created_at DESC
                """, (event["id"],))
                event["snapshots"] = [dict(r) for r in cur.fetchall()]

        return {"code": 200, "data": event}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"查询失败: {e}")


@app.post("/api/v1/emergency/events/{event_code}/confirm", tags=["应急响应"])
def confirm_event(
    event_code: str,
    confirm_user: str = Query("", description="确认人姓名"),
):
    """
    确认签收应急事件。
    同事在列表中点"确认"后，事件状态从 active → received（已接收）。
    记录确认人和确认时间。
    """
    schema = _schema()
    try:
        with _get_pg_conn() as conn:
            with conn.cursor(cursor_factory=__import__("psycopg2.extras", fromlist=["RealDictCursor"]).RealDictCursor) as cur:
                # 查事件是否存在
                cur.execute(f"SELECT id, status FROM {schema}.hh_emergency_event WHERE event_code = %s", (event_code,))
                row = cur.fetchone()
                if not row:
                    raise HTTPException(404, f"事件 {event_code} 不存在")
                if row["status"] == "received":
                    return {"code": 200, "data": {"event_code": event_code, "status": "received", "message": "该事件已被确认签收"}}

                # 更新状态
                ext_update = {"confirmed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "confirmed_by": confirm_user or "unknown"}
                cur.execute(f"""
                    UPDATE {schema}.hh_emergency_event
                    SET status = 'received', updated_at = NOW(),
                        ext = ext || %s::jsonb
                    WHERE id = %s
                """, (json.dumps(ext_update), row["id"]))
                conn.commit()
        return {
            "code": 200,
            "data": {
                "event_code": event_code,
                "status": "received",
                "confirmed_by": confirm_user or "unknown",
                "confirmed_at": ext_update["confirmed_at"],
                "message": "事件已确认签收（状态→已接收）",
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"确认失败: {e}")


# ========== 主入口 ==========

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)