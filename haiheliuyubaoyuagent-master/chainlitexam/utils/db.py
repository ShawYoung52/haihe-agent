from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils.config import DB_CONFIG

# 与 message_orchestrator.py 保持一致：快速路径是永久禁用的业务边界，
# 数据库初始化阶段也不得因部署环境变量而安装旧路由。
ENABLE_FAST_PATHS = False
print("[utils.db] fast paths are permanently disabled")

engine = create_engine(
    f"postgresql+psycopg2://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
)

Session = sessionmaker(bind=engine)
