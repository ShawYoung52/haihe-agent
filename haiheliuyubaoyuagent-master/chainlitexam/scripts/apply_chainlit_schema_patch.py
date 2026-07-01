import asyncio
import os
from pathlib import Path

import asyncpg


async def main() -> None:
    host = os.getenv("CHAINLIT_DB_HOST", "211.157.132.19")
    port = int(os.getenv("CHAINLIT_DB_PORT", "48091"))
    user = os.getenv("CHAINLIT_DB_USER", "postgres")
    password = os.getenv("CHAINLIT_DB_PASSWORD", "postgres")
    db_name = os.getenv("CHAINLIT_DB_NAME", "tjznt")

    sql_path = Path(__file__).resolve().parents[1] / "sql" / "chainlit_minimal_schema_patch.sql"
    sql = sql_path.read_text(encoding="utf-8")

    conn = await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=db_name,
    )
    try:
        await conn.execute(sql)
        print("schema patch applied")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

