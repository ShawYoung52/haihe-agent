"""边级裁剪本地测试数据库连接。"""
from __future__ import annotations

import os
import psycopg2


def env_or_default(name: str, default: str) -> str:
    return os.getenv(name, default)


def connect_from_env():
    password = os.getenv("HHLY_DB_PASSWORD", "")
    if not password:
        raise ValueError("缺少数据库密码：请设置 HHLY_DB_PASSWORD")
    return psycopg2.connect(
        host=env_or_default("HHLY_DB_HOST", "211.157.132.19"),
        port=int(env_or_default("HHLY_DB_PORT", "48091")),
        dbname=env_or_default("HHLY_DB_NAME", "hhly"),
        user=env_or_default("HHLY_DB_USER", "postgres"),
        password=password,
        sslmode=env_or_default("HHLY_DB_SSLMODE", "disable"),
        connect_timeout=int(env_or_default("HHLY_DB_CONNECT_TIMEOUT", "5")),
    )
