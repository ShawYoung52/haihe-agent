from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils.config import DB_CONFIG

engine = create_engine(
    f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
)

Session = sessionmaker(bind=engine)