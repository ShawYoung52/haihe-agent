from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils.config import DB_CONFIG

try:
    from last_month_areal_patch import install_last_month_areal_patch as _load_last_month_areal_route

    _load_last_month_areal_route()
except Exception as exc:
    print(f"[utils.db] last month areal route init failed: {exc}")

try:
    from last_year_max_daily_rainfall_patch import (
        install_last_year_max_daily_rainfall_patch as _load_last_year_max_daily_rainfall_route,
    )

    _load_last_year_max_daily_rainfall_route()
except Exception as exc:
    print(f"[utils.db] last year max daily rainfall route init failed: {exc}")

engine = create_engine(
    f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
)

Session = sessionmaker(bind=engine)
