from __future__ import annotations

import configparser
import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import Json, RealDictCursor


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_iso_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _normalize_level(level: Any) -> Optional[str]:
    if level is None:
        return None
    text = str(level).strip().upper()
    return text or None


def _normalize_status(reached: Any) -> str:
    if isinstance(reached, bool):
        ok = reached
    elif reached is None:
        ok = False
    else:
        txt = str(reached).strip().lower()
        if txt in {"1", "true", "yes", "y", "on"}:
            ok = True
        elif txt in {"0", "false", "no", "n", "off", ""}:
            ok = False
        else:
            ok = False
    return "triggered" if ok else "not_triggered"


def _build_product_list(event_type: str, response_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    evidence = response_payload.get("evidence")
    if not isinstance(evidence, dict):
        return products
    if event_type == "forecast":
        ec_files = evidence.get("ec_files")
        if isinstance(ec_files, dict):
            for key in ("6h", "12h", "24h", "48h", "72h"):
                p = ec_files.get(key)
                if p:
                    products.append(
                        {
                            "product_id": f"ec_{key}",
                            "product_type": "forecast_grib",
                            "title": f"EC预报累计降水 {key}",
                            "path": str(p),
                        }
                    )
    return products


class EmergencyEventStore:
    def __init__(self, config_path: str):
        self.config = configparser.ConfigParser()
        self.config.read(config_path, encoding="utf-8")
        if "postgres" not in self.config:
            raise RuntimeError(f"{config_path} 缺少 [postgres] 配置")
        pg = self.config["postgres"]
        self.schema = pg.get("schema", "public")
        self.table = pg.get("emergency_event_table", "haihe_emergency_event")
        self._conn_kwargs = {
            "host": pg.get("host"),
            "port": int(pg.get("port", "5432")),
            "dbname": pg.get("dbname"),
            "user": pg.get("user"),
            "password": pg.get("password"),
            "sslmode": pg.get("sslmode", "disable"),
            "connect_timeout": int(pg.get("connect_timeout", "5")),
        }
        self.ensure_table()

    def _connect(self):
        return psycopg2.connect(**self._conn_kwargs)

    def ensure_table(self) -> None:
        sql = f"""
        CREATE SCHEMA IF NOT EXISTS {self.schema};
        CREATE TABLE IF NOT EXISTS {self.schema}.{self.table} (
            event_id TEXT PRIMARY KEY,
            event_time TIMESTAMP NOT NULL,
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            level TEXT NULL,
            message TEXT NULL,
            created_at TIMESTAMP NOT NULL,
            request_json JSONB NOT NULL,
            response_json JSONB NOT NULL,
            products_json JSONB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_{self.table}_event_time ON {self.schema}.{self.table}(event_time DESC);
        CREATE INDEX IF NOT EXISTS idx_{self.table}_status ON {self.schema}.{self.table}(status);
        CREATE INDEX IF NOT EXISTS idx_{self.table}_level ON {self.schema}.{self.table}(level);
        CREATE INDEX IF NOT EXISTS idx_{self.table}_event_type ON {self.schema}.{self.table}(event_type);
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)

    def append_event(
        self,
        event_type: str,
        query_time: Optional[str],
        request_params: Dict[str, Any],
        response_payload: Dict[str, Any],
    ) -> str:
        event_id = uuid.uuid4().hex
        event_time = _to_iso_time(query_time) or _now_iso()
        created_at = _now_iso()
        level = _normalize_level(response_payload.get("level"))
        status = _normalize_status(response_payload.get("reached"))
        products = _build_product_list(event_type, response_payload)

        sql = f"""
        INSERT INTO {self.schema}.{self.table}
        (event_id, event_time, event_type, status, level, message, created_at, request_json, response_json, products_json)
        VALUES (%(event_id)s, %(event_time)s, %(event_type)s, %(status)s, %(level)s, %(message)s, %(created_at)s,
                %(request_json)s, %(response_json)s, %(products_json)s)
        """
        params = {
            "event_id": event_id,
            "event_time": event_time,
            "event_type": event_type,
            "status": status,
            "level": level,
            "message": response_payload.get("message"),
            "created_at": created_at,
            "request_json": Json(request_params),
            "response_json": Json(response_payload),
            "products_json": Json(products),
        }
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
        return event_id

    def list_events(
        self,
        page: int = 1,
        page_size: int = 20,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        status: str = "",
        level: Optional[str] = None,
        event_type: str = "",
    ) -> Dict[str, Any]:
        page = max(1, int(page))
        page_size = min(max(1, int(page_size)), 200)
        offset = (page - 1) * page_size

        wheres: List[str] = []
        params: Dict[str, Any] = {"limit": page_size, "offset": offset}
        if start_time:
            wheres.append("event_time >= %(start_time)s")
            params["start_time"] = start_time
        if end_time:
            wheres.append("event_time <= %(end_time)s")
            params["end_time"] = end_time
        if status:
            wheres.append("status = %(status)s")
            params["status"] = status
        if level:
            wheres.append("level = %(level)s")
            params["level"] = level
        if event_type:
            wheres.append("event_type = %(event_type)s")
            params["event_type"] = event_type
        where_sql = f"WHERE {' AND '.join(wheres)}" if wheres else ""

        sql_count = f"SELECT COUNT(*) AS c FROM {self.schema}.{self.table} {where_sql}"
        sql_list = f"""
        SELECT event_id, event_time, event_type, status, level, message
        FROM {self.schema}.{self.table}
        {where_sql}
        ORDER BY event_time DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """

        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql_count, params)
                total = int(cur.fetchone()["c"])
                cur.execute(sql_list, params)
                rows = cur.fetchall()

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "list": [
                {
                    "event_id": r["event_id"],
                    "event_time": r["event_time"].strftime("%Y-%m-%d %H:%M:%S") if r.get("event_time") else None,
                    "event_type": r["event_type"],
                    "status": r["status"],
                    "level": r["level"],
                    "message": r["message"],
                }
                for r in rows
            ],
        }

    def get_event_detail(self, event_id: str) -> Optional[Dict[str, Any]]:
        sql = f"""
        SELECT event_id, event_time, event_type, status, level, message, created_at,
               request_json, response_json, products_json
        FROM {self.schema}.{self.table}
        WHERE event_id = %(event_id)s
        LIMIT 1
        """
        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, {"event_id": event_id})
                row = cur.fetchone()
        if not row:
            return None
        return {
            "event_id": row["event_id"],
            "event_time": row["event_time"].strftime("%Y-%m-%d %H:%M:%S") if row.get("event_time") else None,
            "event_type": row["event_type"],
            "status": row["status"],
            "level": row["level"],
            "message": row["message"],
            "created_at": row["created_at"].strftime("%Y-%m-%d %H:%M:%S") if row.get("created_at") else None,
            "request": row.get("request_json") or {},
            "response": row.get("response_json") or {},
            "products": row.get("products_json") or [],
        }

    def find_events_by_trace_id(self, trace_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        tid = str(trace_id or "").strip()
        if not tid:
            return []
        lim = max(1, min(int(limit), 500))
        sql = f"""
        SELECT event_id, event_time, event_type, status, level, message
        FROM {self.schema}.{self.table}
        WHERE request_json->>'trace_id' = %(trace_id)s
        ORDER BY event_time DESC
        LIMIT %(limit)s
        """
        with self._connect() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, {"trace_id": tid, "limit": lim})
                rows = cur.fetchall() or []
        return [
            {
                "event_id": r["event_id"],
                "event_time": r["event_time"].strftime("%Y-%m-%d %H:%M:%S") if r.get("event_time") else None,
                "event_type": r["event_type"],
                "status": r["status"],
                "level": r["level"],
                "message": r["message"],
            }
            for r in rows
        ]

