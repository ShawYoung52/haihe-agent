import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils.config import DB_CONFIG

# Keep in sync with message_orchestrator.py. Defined locally to avoid importing that heavy module at db init time.
ENABLE_FAST_PATHS = os.environ.get("ENABLE_FAST_PATHS", "false").strip().lower() in ("1", "true", "yes")

if ENABLE_FAST_PATHS:
    try:
        from fast_paths import install_all_fast_paths

        install_all_fast_paths()
    except Exception as exc:
        print(f"[utils.db] fast path routes init failed: {exc}")
else:
    print("[utils.db] fast paths are disabled (ENABLE_FAST_PATHS is not set)")

engine = create_engine(
    f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
)

Session = sessionmaker(bind=engine)