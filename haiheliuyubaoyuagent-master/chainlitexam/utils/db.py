from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils.config import DB_CONFIG

try:
    from fast_paths import install_all_fast_paths

    install_all_fast_paths()
except Exception as exc:
    print(f"[utils.db] fast path routes init failed: {exc}")

engine = create_engine(
    f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
)

Session = sessionmaker(bind=engine)
