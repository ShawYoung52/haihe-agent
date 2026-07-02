from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils.config import DB_CONFIG

try:
    from last_month_areal_patch import install_last_month_areal_patch as _load_last_month_areal_route

    _load_last_month_areal_route()
except Exception as exc:
    print(f"[utils.db] last month areal route init failed: {exc}")

engine = create_engine(
    f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
)

Session = sessionmaker(bind=engine)
